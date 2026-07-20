// ── Voice input (Web Speech API + MediaRecorder fallback) ───────────────────
function _micIsLocalhostOrLoopback(hostname){
  const host=String(hostname||'').toLowerCase().replace(/^\[|\]$/g,'');
  return host==='localhost'
    || host.endsWith('.localhost')
    || host==='::1'
    || host==='0:0:0:0:0:0:0:1'
    || /^127\./.test(host);
}

function _micOriginNeedsSecureContext(){
  if(window.isSecureContext===true) return false;
  const loc=window.location||{};
  const protocol=loc.protocol||'';
  return protocol==='http:'&&!_micIsLocalhostOrLoopback(loc.hostname);
}

function _micToastKeyForRecognitionError(error){
  if((error==='not-allowed'||error==='service-not-allowed'||error==='audio-capture')
      && _micOriginNeedsSecureContext()){
    return 'mic_insecure_origin';
  }
  const msgs={
    'not-allowed':'mic_denied',
    'service-not-allowed':'mic_denied',
    'no-speech':'mic_no_speech',
    'network':'mic_network',
  };
  return msgs[error]||null;
}

const speechCapture=(()=>{
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const _canRecordAudio=!!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder);
  if(!SpeechRecognition&&!_canRecordAudio) return Object.freeze({}); // Browser unsupported — mic button stays hidden

  // Persist SR failure across reloads (e.g. Tailscale/network error)
  const _micForceMediaRecorderKey='mic_force_mediarecorder';
  const _micForceMediaRecorderStored=localStorage.getItem(_micForceMediaRecorderKey);
  // Prefer Hermes server-side STT (MediaRecorder -> /api/transcribe) only
  // after the server confirms an STT provider is available. No stored key must
  // keep browser SpeechRecognition as the first-click default until then; that
  // avoids dropping the first dictation on installs without server STT.
  let _serverSttAvailable=false;
  let _forceMediaRecorder=!SpeechRecognition||(_micForceMediaRecorderStored===null?(_serverSttAvailable&&_canRecordAudio):_micForceMediaRecorderStored==='1');

  // Raw audio mode preference: send audio file instead of transcribing
  let _rawAudioMode = localStorage.getItem('hermes-raw-audio-mode') === 'true';
  // Append-on-commit preference: when ON (default), dictated text is appended
  // to any text already in the composer. When OFF, dictated text replaces the
  // composer content (the pre-existing behavior).
  let _dictationAppend = localStorage.getItem('hermes-dictation-append') !== 'false';
  // Capture backend pinned at recording start ('speech' | 'media' | null) so
  // _stopMic / onstop act on the backend that actually started, even if the
  // raw-audio toggle changes mid-recording (#3169 Codex review).
  let _activeCaptureMode = null;

  const btn=$('btnMic');
  const status=$('micStatus');
  const ta=$('msg');
  const statusText=status?status.querySelector('.status-text'):null;
  btn.style.display=''; // Show button — browser supports speech recognition or recording fallback

  let recognition=null;
  let mediaRecorder=null;
  let mediaStream=null;
  let audioChunks=[];
  let _finalText='';
  let _prefix='';
  let _isRecording=false;
  // #5294 salvage — mobile composer-mic dictation continuity.
  // _speechStopRequested distinguishes an intentional stop (send/toggle) from a
  // natural pause so onend only auto-restarts on real pauses. _micWakeLock keeps
  // the screen awake while dictating; _micWakeLockOp serializes acquire/release
  // so rapid start/stop/visibility churn can't leak a lock. _micRestartCount is
  // bounded by _micMaxRestarts to stop a tight loop if the audio session is stolen.
  let _speechStopRequested=false;
  let _micWakeLock=null;
  let _micWakeLockOp=null;
  let _micRestartCount=0;
  const _micMaxRestarts=20;
  let _micHoldTimer=null;
  let _micHoldActive=false;
  let _micPointerDown=false;
  let _micStartSeq=0;
  const _micHoldThresholdMs=300;

  function _setButtonTooltipAndKey(btn, key){
    const text = t(key);
    btn.setAttribute('data-i18n-title', key);
    if(btn.hasAttribute('data-tooltip')){
      btn.setAttribute('data-tooltip', text);
      if(btn.hasAttribute('title')) btn.removeAttribute('title');
    } else {
      btn.title = text;
    }
  }

  function _setRecording(on){
    window._micActive=on;
    btn.classList.toggle('recording',on);
    // Active-state title flips so the tooltip is honest about what
    // pressing the button will do (#1488).
    _setButtonTooltipAndKey(btn, on ? (_rawAudioMode ? 'voice_recording_active' : 'voice_dictate_active') : (_rawAudioMode ? 'voice_send_raw' : 'voice_dictate'));
    status.style.display=on?'':'none';
    if(statusText) statusText.textContent=on?'Listening':'Listening';
    if(!on){ _finalText=''; _prefix=''; }
  }

  function _updateMicTooltip(){
    if(!window._micActive){
      _setButtonTooltipAndKey(btn, _rawAudioMode ? 'voice_send_raw' : 'voice_dictate');
    }
  }

  function _applyRawAudioModePreference(enabled){
    _rawAudioMode=!!enabled;
    try{localStorage.setItem('hermes-raw-audio-mode',_rawAudioMode?'true':'false');}catch(_){}
    const rawAudioCheckbox=document.getElementById('settingsRawAudio');
    if(rawAudioCheckbox) rawAudioCheckbox.checked=_rawAudioMode;
    _updateMicTooltip();
  }

  function _applyDictationAppendPreference(enabled){
    _dictationAppend=!!enabled;
    try{localStorage.setItem('hermes-dictation-append',_dictationAppend?'true':'false');}catch(_){}
    const cb=document.getElementById('settingsDictationAppend');
    if(cb) cb.checked=_dictationAppend;
  }

  async function _sendRawAudio(blob){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const file=new File([blob],`voice-input-${Date.now()}.${ext}`,{type:blob.type||`audio/${ext}`});
    S.pendingFiles.push(file);
    renderTray();
    // An explicit Send-button click while recording sets _micPendingSend — that
    // is an unambiguous send intent, so honor it even when the composer already
    // has text (mirrors the transcribe path). Otherwise (manual mic-stop): send
    // immediately only if the composer is empty, else just attach + toast so the
    // user can keep composing.
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }else if(!ta.value.trim()){
      send();
    }else{
      showToast(t('voice_raw_attached'));
    }
  }

  function _commitTranscript(text, prefixOverride){
    // `prefixOverride` is the composer content captured at recording start,
    // passed only by the async server-STT path (recorder.onstop → _transcribeBlob).
    // The sync browser-SR path doesn't call this function — it commits inline
    // in sr.onend using _prefix directly.
    //
    // Three concerns this function has to balance (Greptile reviews):
    //   1. Race condition: user types during async transcription → preserve those
    //      keystrokes. Read live ta.value, not the stale snapshot.
    //   2. Clear-during-transcription: user clears textarea during async wait →
    //      respect that intent. Live ta.value (even empty) wins over snapshot.
    //   3. Browser-SR fallback: if a future caller passes no prefixOverride
    //      and live ta.value is empty, fall back to _prefix as a safety net.
    //
    // Resolution: when prefixOverride IS provided (server-STT path), trust live
    // ta.value unconditionally — even when empty. Otherwise fall back to _prefix.
    const clean=(text||'').trim();
    let committed;
    if(!clean){
      committed = ta.value;
    }else if(_dictationAppend){
      const base = prefixOverride !== undefined ? ta.value : (ta.value || _prefix);
      if(!base){
        committed = clean;
      }else{
        committed = (!base.endsWith(' ') && !base.endsWith('\n'))
          ? base+' '+clean.trimStart()
          : base+clean;
      }
    }else{
      // Replace mode (explicit): dictated text overwrites the composer.
      committed = clean;
    }
    ta.value=committed;
    autoResize();
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }
  }

  function _isServerSttUnavailable(err){
    const status=err&&err.status;
    if(status===404||status===503||status>=500) return true;
    if(!status) return true;
    const msg=String((err&&err.message)||'').toLowerCase();
    return msg.includes('unavailable')||msg.includes('not configured');
  }

  function _allowBrowserSttFallback(){
    return !!(SpeechRecognition&&localStorage.getItem(_micForceMediaRecorderKey)!=='1');
  }

  async function _transcribeBlob(blob, prefixSnapshot){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const form=new FormData();
    form.append('file',new File([blob],`voice-input.${ext}`,{type:blob.type||`audio/${ext}`}));
    // Snapshot is passed in from the recorder.onstop handler — taken there
    // BEFORE _setRecording(false) clears _prefix (async server STT path).
    setComposerStatus('Transcribing…');
    try{
      const res=await fetch('api/transcribe',{method:'POST',body:form});
      const data=await res.json().catch(()=>({}));
      if(!res.ok){
        const err=new Error(data.error||'Transcription failed');
        err.status=res.status;
        throw err;
      }
      _commitTranscript(data.transcript||'', prefixSnapshot);
    }catch(err){
      if(_isServerSttUnavailable(err)&&_allowBrowserSttFallback()){
        window._micPendingSend=false;
        localStorage.setItem(_micForceMediaRecorderKey,'0');
        _forceMediaRecorder=false;
        recognition=_ensureSpeechRecognition();
        showToast(err.message||t('mic_network'));
        return;
      }
      window._micPendingSend=false;
      showToast(err.message||t('mic_network'));
    }finally{
      setComposerStatus('');
    }
  }

  function _stopTracks(stream=mediaStream){
    if(stream){
      stream.getTracks().forEach(track=>track.stop());
      if(mediaStream===stream) mediaStream=null;
    }
  }

  // Gate continuous dictation to MOBILE so desktop stays one-shot (single
  // utterance). An explicit hermes-mic-continuous flag wins in both directions,
  // mirroring the hermes-voice-continuous pattern: 'true' opts a desktop in,
  // 'false' opts a mobile out. Absent a flag, coarse-pointer (touch) devices get
  // continuity and everything else stays single-utterance.
  function _micDictationContinuous(){
    try{
      const flag=localStorage.getItem('hermes-mic-continuous');
      if(flag==='true') return true;
      if(flag==='false') return false;
    }catch(_){}
    try{ return window.matchMedia('(pointer:coarse)').matches; }catch(_){ return false; }
  }

  // Only auto-restart the composer-mic session on a natural pause: continuity
  // must be enabled (mobile/opt-in), the session must still be active speech,
  // the stop must not have been requested, and we must be under the restart cap.
  function _micShouldRestartDictation(){
    return _micDictationContinuous()
      && !_speechStopRequested
      && !!window._micActive
      && _activeCaptureMode==='speech'
      && _micRestartCount<_micMaxRestarts;
  }

  // Screen Wake Lock while dictating. All acquire/release ops are chained onto a
  // single in-flight promise (_micWakeLockOp) so rapid start/stop/visibility
  // churn runs strictly in order and can't interleave to leak a lock or null
  // _micWakeLock mid-request.
  function _acquireMicWakeLock(){
    if(!navigator.wakeLock) return Promise.resolve();
    _micWakeLockOp=Promise.resolve(_micWakeLockOp).then(async()=>{
      if(_micWakeLock) return;
      if(!window._micActive||_activeCaptureMode!=='speech') return;
      try{
        const lock=await navigator.wakeLock.request('screen');
        // If the session ended while awaiting, don't hold a stale lock.
        if(!window._micActive||_activeCaptureMode!=='speech'){
          try{ await lock.release(); }catch(_){}
          return;
        }
        _micWakeLock=lock;
        _micWakeLock.addEventListener?.('release',()=>{ _micWakeLock=null; },{once:true});
      }catch(_){
        _micWakeLock=null;
      }
    });
    return _micWakeLockOp;
  }

  function _releaseMicWakeLock(){
    _micWakeLockOp=Promise.resolve(_micWakeLockOp).then(async()=>{
      const lock=_micWakeLock;
      _micWakeLock=null;
      if(!lock) return;
      try{ await lock.release(); }catch(_){}
    });
    return _micWakeLockOp;
  }

  // The OS drops a screen wake lock when the tab is hidden; reacquire on return
  // if we're still dictating (release on hide is a no-op if none is held).
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='hidden'){
      void _releaseMicWakeLock();
      return;
    }
    if(window._micActive&&_activeCaptureMode==='speech'){
      void _acquireMicWakeLock();
    }
  });

  function _stopMic(){
    _micStartSeq+=1;
    _isRecording=false;
    if(!window._micActive) return;
    // Stop the backend that was ACTIVE WHEN RECORDING STARTED — not whatever
    // _rawAudioMode says now. The user can toggle Settings → Sound mid-recording,
    // which would otherwise make us stop the wrong backend and orphan the other
    // (#3169 Codex review). _activeCaptureMode is pinned at start.
    if(recognition && _activeCaptureMode==='speech'){
      _speechStopRequested=true;
      recognition.stop();
      return;
    }
    if(mediaRecorder&&mediaRecorder.state!=='inactive'){
      mediaRecorder.stop();
      return;
    }
    _setRecording(false);
    _stopTracks();
  }

  function _ensureSpeechRecognition(){
    if(!SpeechRecognition) return null;
    const sr=recognition||new SpeechRecognition();
    // Desktop dictation stays one-shot (single utterance); mobile / opt-in
    // devices run continuous so a natural pause doesn't end the session (#5294).
    sr.continuous=_micDictationContinuous();
    sr.interimResults=true;
    sr.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    sr.onstart=()=>{ _finalText=''; };

    sr.onresult=(event)=>{
      // #5294: a real result means the continuity restarts are PRODUCTIVE, not a
      // stolen-audio-session tight loop — reset the restart budget so a long
      // dictation with many natural pauses isn't silently capped at
      // _micMaxRestarts. The cap still guards the failure case: consecutive
      // restarts that yield no speech (onend without an intervening onresult)
      // keep incrementing and trip the bound.
      _micRestartCount=0;
      let interim='';
      let final=_finalText;
      for(let i=event.resultIndex;i<event.results.length;i++){
        const t=event.results[i][0].transcript;
        if(event.results[i].isFinal){ final+=t; _finalText=final; }
        else{ interim+=t; }
      }
      ta.value=_prefix+(final||interim);
      autoResize();
    };

    sr.onend=()=>{
      const committed=_finalText
        ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
            ? _prefix+' '+_finalText.trimStart()
            : _prefix+_finalText)
        : ta.value;
      ta.value=committed;
      autoResize();
      // Mobile / opt-in continuity: a natural pause ends this recognition run but
      // the user is still dictating, so restart to keep the session alive. Desktop
      // (one-shot) and intentional stops (_speechStopRequested) skip this and
      // finalize. Bounded by _micMaxRestarts so a stolen audio session can't loop.
      if(_micShouldRestartDictation()){
        _prefix=committed&&!committed.endsWith(' ')&&!committed.endsWith('\n')
          ? committed+' '
          : committed;
        _finalText='';
        _micRestartCount++;
        try{
          sr.start();
          return;
        }catch(err){
          // Restart failed (e.g. the audio session was taken by another app).
          // Surface it instead of silently dropping to idle (Greptile P2).
          showToast(t('mic_error')+String((err&&err.message)||'restart'));
        }
      }
      _speechStopRequested=false;
      _isRecording=false;
      _micRestartCount=0;
      void _releaseMicWakeLock();
      _setRecording(false);
      if(window._micPendingSend){
        window._micPendingSend=false;
        send();
      }
      _applyDeferredServerSttFlip();
    };

    sr.onerror=(event)=>{
      // While dictating with continuity on, a no-speech/aborted error is a normal
      // pause or transient audio-session hiccup — swallow it and let onend restart
      // (bounded by _micMaxRestarts). Desktop one-shot still surfaces the toast.
      if((event.error==='no-speech'||event.error==='aborted')
          && _micDictationContinuous()
          && window._micActive
          && _activeCaptureMode==='speech'
          && !_speechStopRequested
          && _micRestartCount<_micMaxRestarts){
        return;
      }
      _speechStopRequested=false;
      _setRecording(false);
      window._micPendingSend=false;
      _isRecording=false;
      _micRestartCount=0;
      void _releaseMicWakeLock();
      if(event.error==='network'||event.error==='not-allowed'
          ||event.error==='service-not-allowed'||event.error==='audio-capture'){
        // Persist SR failure: next reload will skip SpeechRecognition
        localStorage.setItem(_micForceMediaRecorderKey,'1');
        _forceMediaRecorder=true;
        recognition=null;
      }
      const messageKey=_micToastKeyForRecognitionError(event.error);
      showToast(messageKey?t(messageKey):t('mic_error')+event.error);
    };

    return sr;
  }

  if(!_forceMediaRecorder){
    recognition=_ensureSpeechRecognition();
  }

  async function _probeServerSttCapability(){
    if(!_canRecordAudio||_micForceMediaRecorderStored!==null) return;
    try{
      const res=await fetch('api/transcribe/capability',{cache:'no-store'});
      const data=await res.json().catch(()=>({}));
      if(res.ok&&data&&data.available){
        _serverSttAvailable=true;
        if(!window._micActive){
          _forceMediaRecorder=true;
          recognition=null;
        }
      }
    }catch(_err){
      // Keep browser SpeechRecognition as the safe first-click default when the
      // passive capability probe fails.
    }
  }

  // If the capability probe resolved WHILE a session was active, the flip to
  // server STT was deferred to protect that in-flight session. Apply it once the
  // session ends so subsequent clicks use the configured server STT as intended.
  // Reads LIVE localStorage (not the init-time const) so a fallback that just
  // persisted '0' is respected and not re-flipped.
  function _applyDeferredServerSttFlip(){
    if(_serverSttAvailable&&!_forceMediaRecorder&&!window._micActive
        &&localStorage.getItem(_micForceMediaRecorderKey)===null){
      _forceMediaRecorder=true;
      recognition=null;
    }
  }

  _probeServerSttCapability();

  function _clearMicHoldTimer(){
    if(_micHoldTimer){
      clearTimeout(_micHoldTimer);
      _micHoldTimer=null;
    }
  }

  function _resetMicHoldState(){
    _clearMicHoldTimer();
    _micHoldActive=false;
    _micPointerDown=false;
  }

  function _micButtonAvailable(){
    if(!btn||btn.disabled) return false;
    if(btn.style.display==='none') return false;
    if(btn.classList.contains('composer-control-hidden')) return false;
    if(btn.getAttribute('aria-hidden')==='true') return false;
    if(window.getComputedStyle&&window.getComputedStyle(btn).display==='none') return false;
    return true;
  }

  async function _startMicCapture(holdRequired=false){
    if(!_micButtonAvailable()) return;
    const startSeq=++_micStartSeq;
    // Race-condition guard: ignore rapid double-clicks
    if(_isRecording){
      _stopMic();
      _isRecording=false;
      return;
    }
    if(window._micActive){
      _stopMic();
      return;
    }
    _isRecording=true;
    _finalText='';
    _prefix=ta.value;
    if(_micOriginNeedsSecureContext()){
      _isRecording=false;
      window._micPendingSend=false;
      showToast(t('mic_insecure_origin'));
      return;
    }
    if(recognition && !_forceMediaRecorder && !_rawAudioMode){
      _activeCaptureMode='speech';
      _speechStopRequested=false;
      _micRestartCount=0;
      // Refresh continuity gate at start so a settings/orientation change takes
      // effect for this session (desktop stays one-shot, mobile stays continuous).
      recognition.continuous=_micDictationContinuous();
      recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';
      recognition.start();
      void _acquireMicWakeLock();
      _setRecording(true);
      return;
    }
    if(!_canRecordAudio){
      _isRecording=false;
      showToast(t('mic_network'));
      return;
    }
    try{
      const captureStream=await navigator.mediaDevices.getUserMedia({audio:true});
      if(startSeq!==_micStartSeq||!_micButtonAvailable()||(holdRequired&&!_micHoldActive)){
        _isRecording=false;
        _stopTracks(captureStream);
        return;
      }
      mediaStream=captureStream;
      const preferredTypes=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/ogg'];
      const mimeType=preferredTypes.find(type=>window.MediaRecorder.isTypeSupported?.(type))||'';
      const captureMode=_rawAudioMode?'media-raw':'media-transcribe';
      const recorder=new MediaRecorder(captureStream,mimeType?{mimeType}:undefined);
      audioChunks=[];
      const captureChunks=audioChunks;
      recorder.ondataavailable=e=>{if(e.data&&e.data.size)captureChunks.push(e.data);};
      recorder.onerror=()=>{
        const isCurrentCapture=mediaRecorder===recorder||mediaStream===captureStream;
        _isRecording=false;
        if(mediaRecorder===recorder) mediaRecorder=null;
        if(isCurrentCapture) _setRecording(false);
        window._micPendingSend=false;
        _stopTracks(captureStream);
        showToast(t('mic_network'));
      };
      recorder.onstop=async()=>{
        const isCurrentCapture=mediaRecorder===recorder||mediaStream===captureStream;
        if(mediaRecorder===recorder) mediaRecorder=null;
        _isRecording=false;
        // Capture the composer prefix BEFORE _setRecording(false) clears _prefix.
        // The await on _transcribeBlob runs after this sync block, so by the
        // time _transcribeBlob is called, _prefix is already ''. Passing the
        // snapshot through keeps append-mode working on the async server-STT
        // path. See _commitTranscript() for how the snapshot is consumed.
        const prefixSnapshot = _prefix;
        const blob=new Blob(captureChunks,{type:recorder.mimeType||mimeType||'audio/webm'});
        if(isCurrentCapture) _setRecording(false);
        _stopTracks(captureStream);
        if(blob.size){
          if(captureMode==='media-raw'){
            await _sendRawAudio(blob);
          }else{
            await _transcribeBlob(blob, prefixSnapshot);
          }
        }
        else if(window._micPendingSend){
          window._micPendingSend=false;
        }
        _applyDeferredServerSttFlip();
      };
      _activeCaptureMode=captureMode;
      mediaRecorder=recorder;
      recorder.start();
      _setRecording(true);
    }catch(err){
      if(startSeq!==_micStartSeq) return;
      _isRecording=false;
      window._micPendingSend=false;
      _stopTracks();
      showToast(t(_micToastKeyForRecognitionError('not-allowed')||'mic_denied'));
    }
  }

  async function _toggleMicCapture(){
    if(!_micButtonAvailable()) return;
    if(window._micActive){
      _stopMic();
      return;
    }
    await _startMicCapture();
  }

  btn.addEventListener('pointerdown',e=>{
    if(e.button!==0) return;
    if(!_micButtonAvailable()) return;
    _resetMicHoldState();
    _micPointerDown=true;
    _micHoldTimer=setTimeout(async()=>{
      _micHoldTimer=null;
      if(!_micPointerDown||window._micActive) return;
      _micHoldActive=true;
      await _startMicCapture(true);
    },_micHoldThresholdMs);
  });

  btn.addEventListener('pointerup',async e=>{
    if(e.button!==0||!_micPointerDown) return;
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
    await _toggleMicCapture();
  });

  btn.addEventListener('pointerleave',()=>{
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
  });

  btn.addEventListener('pointercancel',()=>{
    _clearMicHoldTimer();
    if(_micHoldActive){
      _micHoldActive=false;
      _micPointerDown=false;
      _stopMic();
      return;
    }
    _micPointerDown=false;
  });

  btn.addEventListener('click',async e=>{
    if(e.detail!==0) return;
    await _toggleMicCapture();
  });

  // Wire up the settings checkbox
  const rawAudioCheckbox = document.getElementById('settingsRawAudio');
  if(rawAudioCheckbox){
    rawAudioCheckbox.checked = _rawAudioMode;
    rawAudioCheckbox.addEventListener('change', function(){
      _applyRawAudioModePreference(this.checked);
    });
  }
  const appendCheckbox = document.getElementById('settingsDictationAppend');
  if(appendCheckbox){
    appendCheckbox.checked = _dictationAppend;
    appendCheckbox.addEventListener('change', function(){
      _applyDictationAppendPreference(this.checked);
    });
  }
  _updateMicTooltip();
  return Object.freeze({
    applyDictationAppendPreference:_applyDictationAppendPreference,
    applyRawAudioModePreference:_applyRawAudioModePreference,
    stopMic:_stopMic,
    toggleMicCapture:_toggleMicCapture,
  });
})();
window._micActive=window._micActive||false;
window._micPendingSend=window._micPendingSend||false;

export {_micOriginNeedsSecureContext,_micToastKeyForRecognitionError,speechCapture};
