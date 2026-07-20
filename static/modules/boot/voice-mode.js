import {_setButtonTooltip} from './navigation.js';
import {_micOriginNeedsSecureContext,_micToastKeyForRecognitionError} from './speech-capture.js';
import {_hermesTtsIsRegistered,_hermesTtsSynth} from './public-interfaces.js';

// ── Turn-based voice mode (#1333) ────────────────────────────────────────
// Chained flow: listen → send → (agent processes) → TTS response → listen again
const voiceMode=(()=>{
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const hasSTT=!(!SpeechRecognition);
  const hasTTS=!!('speechSynthesis' in window);

  // Need both STT and TTS for turn-based voice mode
  if(!hasSTT||!hasTTS) return Object.freeze({});

  const modeBtn=$('btnVoiceMode');
  const bar=$('voiceModeBar');
  const indicator=$('voiceModeIndicator');
  const label=$('voiceModeLabel');
  const micBtn=$('btnMic');
  const ta=$('msg');

  if(!modeBtn||!bar||!indicator||!label) return Object.freeze({});

  // Voice-mode button is gated behind a Preferences toggle (#1488).
  // Default off — keeps the composer footer uncluttered for users who
  // only need plain dictation. The hands-free conversation feature is
  // a power-user surface; explicit opt-in avoids the visual confusion
  // of two near-identical mic icons.
  function _voiceModePrefEnabled(){
    try{ return localStorage.getItem('hermes-voice-mode-button')==='true'; }
    catch(_){ return false; }
  }
  let _voiceModeActive=false;

  function _applyVoiceModePref(){
    const enabled = _voiceModePrefEnabled();
    modeBtn.style.display = enabled ? '' : 'none';
    if(!enabled && _voiceModeActive) _deactivate();
  }
  _applyVoiceModePref();
  let _voiceModeState='idle'; // idle | listening | thinking | speaking
  let _recognition=null;
  let _silenceTimer=null;
  // Capture the session id at thinking-time so the TTS callback won't read
  // a different session's last assistant reply if the user navigated away
  // between send and stream completion. (Opus pre-release advisor.)
  let _voiceModeThinkingSid=null;
  let _browserTtsKeepAlive=null;
  let _browserTtsWatchdog=null;
  let _browserTtsSuppressNextErrorRearm=false;
  // Configurable via localStorage keys (set from dev console or a future settings panel).
  //   hermes-voice-silence-ms, pause duration before auto-send (ms, default 1800)
  //   hermes-voice-continuous, keep mic open across natural pauses ("true"/"false", default false)
  function _voiceSilenceMs(){
    const _silenceMsRaw=parseInt(localStorage.getItem('hermes-voice-silence-ms'),10);
    return (Number.isFinite(_silenceMsRaw)&&_silenceMsRaw>0)?Math.max(200,_silenceMsRaw):1800;
  }

  function _clearBrowserTtsRecovery(){
    if(_browserTtsKeepAlive){
      clearInterval(_browserTtsKeepAlive);
      _browserTtsKeepAlive=null;
    }
    if(_browserTtsWatchdog){
      clearTimeout(_browserTtsWatchdog);
      _browserTtsWatchdog=null;
    }
  }

  function _armBrowserTtsRecovery(clean, rate){
    _clearBrowserTtsRecovery();
    _browserTtsSuppressNextErrorRearm=false;
    const safeRate=(Number.isFinite(rate)&&rate>0)?rate:1;
    // Chromium can drop utter.onend on later turns, so force a recovery path.
    const watchdogMs=Math.max(4000,Math.round((String(clean||'').length/(12*safeRate))*1000)+10000);
    _browserTtsWatchdog=setTimeout(()=>{
      if(!_voiceModeActive||_voiceModeState!=='speaking') return;
      _browserTtsSuppressNextErrorRearm=true;
      try{ speechSynthesis.cancel(); }catch(_){}
      _clearBrowserTtsRecovery();
      _startListening();
    },watchdogMs);
    _browserTtsKeepAlive=setInterval(()=>{
      if(!_voiceModeActive||_voiceModeState!=='speaking'){
        _clearBrowserTtsRecovery();
        return;
      }
      if(!speechSynthesis.speaking) return;
      try{
        speechSynthesis.pause();
        speechSynthesis.resume();
      }catch(_){}
    },10000);
  }

  function _setState(state){
    _voiceModeState=state;
    indicator.className='voice-mode-indicator '+state;
    label.textContent=state==='listening'?t('voice_listening')
      :state==='speaking'?t('voice_speaking')
      :state==='thinking'?t('voice_thinking')
      :'';
    bar.style.display=_voiceModeActive?(state==='idle'?'none':''):'none';
  }

  function _startListening(){
    if(!_voiceModeActive) return;
    if(_micOriginNeedsSecureContext()){
      _deactivate();
      showToast(t('mic_insecure_origin'));
      return;
    }
    _clearBrowserTtsRecovery();
    _setState('listening');

    _recognition=new SpeechRecognition();
    _recognition.continuous=localStorage.getItem('hermes-voice-continuous')==='true';
    _recognition.interimResults=true;
    _recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    let _finalText='';

    _recognition.onstart=()=>{ _finalText=''; };

    _recognition.onresult=(event)=>{
      // Reset silence timer on any result
      clearTimeout(_silenceTimer);
      let interim='';
      let final=_finalText;
      for(let i=event.resultIndex;i<event.results.length;i++){
        const txt=event.results[i][0].transcript;
        if(event.results[i].isFinal){ final+=txt; _finalText=final; }
        else{ interim+=txt; }
      }
      ta.value=final||interim;
      autoResize();

      // Auto-send on silence after final result
      if(_finalText){
        _silenceTimer=setTimeout(()=>{
          _voiceModeSend();
        },_voiceSilenceMs());
      }
    };

    _recognition.onend=()=>{
      clearTimeout(_silenceTimer);
      // If we have text and haven't sent yet, send it
      if(_finalText&&_voiceModeActive&&_voiceModeState==='listening'){
        _voiceModeSend();
      } else if(_voiceModeActive&&_voiceModeState==='listening'){
        // No speech detected — restart listening
        setTimeout(()=>{ if(_voiceModeActive) _startListening(); },500);
      }
    };

    _recognition.onerror=(event)=>{
      clearTimeout(_silenceTimer);
      if(event.error==='no-speech'||event.error==='aborted'){
        // Restart if still active
        if(_voiceModeActive){
          setTimeout(()=>{ if(_voiceModeActive) _startListening(); },800);
        }
        return;
      }
      if(event.error==='not-allowed'||event.error==='service-not-allowed'||event.error==='audio-capture'){
        _deactivate();
        const messageKey=_micToastKeyForRecognitionError(event.error);
        showToast(messageKey?t(messageKey):t('mic_error')+event.error);
        return;
      }
      // Other errors — try to restart
      if(_voiceModeActive){
        setTimeout(()=>{ if(_voiceModeActive) _startListening(); },1500);
      }
    };

    try{ _recognition.start(); }catch(e){
      // Already started or other error — retry shortly
      setTimeout(()=>{ if(_voiceModeActive) _startListening(); },1000);
    }
  }

  function _voiceModeSend(){
    if(!_voiceModeActive) return;
    const text=(ta.value||'').trim();
    if(!text){
      ta.value='';
      setTimeout(()=>{ if(_voiceModeActive) _startListening(); },300);
      return;
    }
    _setState('thinking');
    // Pin the active session id so the TTS callback won't speak a different
    // session's reply if the user navigates away mid-stream.
    _voiceModeThinkingSid=(typeof S!=='undefined'&&S.session)?S.session.session_id:null;
    try{ if(_recognition) _recognition.abort(); }catch(_){}
    _recognition=null;
    // send() is global from boot.js
    if(typeof send==='function') send();
  }

  function _speakResponse(){
    if(!_voiceModeActive) return;
    // Bail out if the user navigated to a different session between send and
    // stream completion. The patched autoReadLastAssistant fires globally;
    // without this guard it would TTS-read the wrong session's last assistant
    // message. Drop back to listening on the new session instead.
    const currentSid=(typeof S!=='undefined'&&S.session)?S.session.session_id:null;
    if(_voiceModeThinkingSid && currentSid && currentSid!==_voiceModeThinkingSid){
      _voiceModeThinkingSid=null;
      _startListening();
      return;
    }
    _voiceModeThinkingSid=null;
    _setState('speaking');

    // Find last assistant message
    const rows=document.querySelectorAll('.msg-row[data-role="assistant"], .assistant-segment[data-raw-text]');
    if(!rows.length){ _startListening(); return; }
    const last=rows[rows.length-1];
    const rawText=last.dataset.rawText||'';
    if(!rawText.trim()){ _startListening(); return; }

    // Strip for TTS (reuse existing helper if available)
    let clean=rawText;
    if(typeof _stripForTTS==='function') clean=_stripForTTS(rawText);
    else{
      // Basic strip: remove code blocks, images, links
      clean=clean.replace(/```[\s\S]*?```/g,' code block ')
        .replace(/`([^`]*)`/g,'$1')
        .replace(/!\[([^\]]*)\]\([^)]*\)/g,'$1')
        .replace(/\[([^\]]*)\]\([^)]*\)/g,'$1')
        .replace(/#{1,6}\s/g,'')
        .replace(/[*_~]+/g,'')
        .replace(/\n{2,}/g,'. ')
        .replace(/\n/g,' ')
        .trim();
    }
    if(!clean){ _startListening(); return; }
    const engine=localStorage.getItem("hermes-tts-engine")||"browser";
    // Extension-registered TTS engine (window.registerHermesTtsEngine): synth
    // via the extension, then play through the same Audio lifecycle as edge.
    if(_hermesTtsIsRegistered(engine)){
      _ttsSpeaking=true;
      const _opts={
        voice: localStorage.getItem("hermes-tts-voice")||'',
        rate: parseFloat(localStorage.getItem("hermes-tts-rate")),
        pitch: parseFloat(localStorage.getItem("hermes-tts-pitch")),
      };
      Promise.resolve(_hermesTtsSynth(engine, clean, _opts))
        .then(function(buf){
          const blob=new Blob([buf]);
          const url=URL.createObjectURL(blob);
          const audio=new Audio(url);
          _playingEdgeAudio=audio;
          audio.onended=function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},500);
          };
          audio.onerror=function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
          };
          audio.play().catch(function(){
            _ttsSpeaking=false;
            if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
            URL.revokeObjectURL(url);
            if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
          });
        })
        .catch(function(){
          _ttsSpeaking=false;
          if(_voiceModeActive) setTimeout(function(){_startListening();},1000);
        });
      return;
    }
    if(engine==="elevenlabs"){
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, engine: 'elevenlabs'})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(e => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    if(engine==="openai"){
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, engine: 'openai'})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(() => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    if(engine==="edge"){
      const voice=localStorage.getItem("hermes-tts-voice")||"zh-CN-XiaoxiaoNeural";
      const savedRate=parseFloat(localStorage.getItem("hermes-tts-rate"));
      const savedPitch=parseFloat(localStorage.getItem("hermes-tts-pitch"));
      let rate='', pitch='';
      if(!isNaN(savedRate)){const pct=Math.round((savedRate-1)*100);const sign=pct>=0?'+':'';rate=sign+pct+'%';}
      if(!isNaN(savedPitch)){const hz=Math.round((savedPitch-1)*50);const sign=hz>=0?'+':'';pitch=sign+hz+'Hz';}
      _ttsSpeaking=true;
      fetch(new URL('api/tts', document.baseURI || location.href).href, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, voice, rate, pitch})
      })
      .then(r => {
        if(!r.ok) throw new Error('TTS request failed: ' + r.status);
        return r.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        // Register with the shared handle (declared in ui.js, same global scope;
        // both scripts are fully evaluated before any voice interaction) so
        // stopTTS() — called from _deactivate() — can actually pause hands-free
        // Edge playback. Without this the audio is local here and unstoppable.
        _playingEdgeAudio=audio;
        audio.onended = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),500);
        };
        audio.onerror = () => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          URL.revokeObjectURL(url);
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        };
        audio.play().catch(e => {
          _ttsSpeaking=false;
          if(_playingEdgeAudio===audio) _playingEdgeAudio=null;
          if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
        });
      })
      .catch(() => {
        _ttsSpeaking=false;
        if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
      });
      return;
    }
    const utter=new SpeechSynthesisUtterance(clean);

    // Apply saved voice preferences
    const savedVoice=localStorage.getItem('hermes-tts-voice');
    const voices=speechSynthesis.getVoices();
    if(savedVoice&&voices.length){
      const match=voices.find(v=>v.name===savedVoice);
      if(match) utter.voice=match;
    }
    const savedRate=parseFloat(localStorage.getItem('hermes-tts-rate'));
    if(!isNaN(savedRate)) utter.rate=Math.min(2,Math.max(0.5,savedRate));
    const savedPitch=parseFloat(localStorage.getItem('hermes-tts-pitch'));
    if(!isNaN(savedPitch)) utter.pitch=Math.min(2,Math.max(0,savedPitch));

    utter.onend=()=>{
      _browserTtsSuppressNextErrorRearm=false;
      _clearBrowserTtsRecovery();
      // After speaking, go back to listening
      if(_voiceModeActive&&_voiceModeState==='speaking') setTimeout(()=>_startListening(),500);
    };
    utter.onerror=()=>{
      _clearBrowserTtsRecovery();
      if(_browserTtsSuppressNextErrorRearm){
        _browserTtsSuppressNextErrorRearm=false;
        return;
      }
      if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
    };

    _armBrowserTtsRecovery(clean, utter.rate);
    try{
      speechSynthesis.speak(utter);
    }catch(_){
      _clearBrowserTtsRecovery();
      if(_voiceModeActive) setTimeout(()=>_startListening(),1000);
    }
  }

  // Hook into response completion — observe when the agent finishes
  // We patch setComposerStatus to detect when a response completes
  const _origSetComposerStatus=(typeof setComposerStatus==='function')?setComposerStatus.bind(window):null;

  function _voiceModeOnResponseComplete(){
    if(_voiceModeActive&&_voiceModeState==='thinking'){
      // Small delay to let DOM render the final message
      setTimeout(()=>{
        if(_voiceModeActive&&_voiceModeState==='thinking'){
          _speakResponse();
        }
      },400);
    }
  }

  // Observe S.busy changes to detect response completion
  // The existing code calls setBusy(false) when response completes
  const _origSetBusy=(typeof setBusy==='function')?setBusy.bind(window):null;
  if(_origSetBusy){
    // We use a MutationObserver-style approach via polling S.busy
    // Actually, we'll use a simpler approach: hook into the message stream completion
  }

  // Most reliable hook: use the existing autoReadLastAssistant call site.
  // We override autoReadLastAssistant so that if voice mode is active, we use our
  // own speak-and-resume flow instead of the default auto-read.
  const _origAutoRead=(typeof autoReadLastAssistant==='function')?autoReadLastAssistant:null;
  function voiceAutoReadLastAssistant(){
    if(_voiceModeActive&&_voiceModeState==='thinking'){
      _speakResponse();
      return;
    }
    if(_origAutoRead) _origAutoRead.apply(this,arguments);
  }

  function _activate(){
    if(_micOriginNeedsSecureContext()){
      showToast(t('mic_insecure_origin'));
      return;
    }
    _voiceModeActive=true;
    modeBtn.classList.add('active');
    _setButtonTooltip(modeBtn, t('voice_mode_toggle_active'));
    showToast(t('voice_mode_active'),1500);
    // If the agent is busy, wait — state will be 'thinking' and we'll detect completion
    if(typeof S!=='undefined'&&S.busy){
      _setState('thinking');
      return;
    }
    // Cancel any existing TTS
    if(typeof stopTTS==='function') stopTTS();
    _startListening();
  }

  function _deactivate(){
    _voiceModeActive=false;
    _voiceModeState='idle';
    _voiceModeThinkingSid=null;
    _browserTtsSuppressNextErrorRearm=false;
    modeBtn.classList.remove('active');
    _setButtonTooltip(modeBtn, t('voice_mode_toggle'));
    bar.style.display='none';
    clearTimeout(_silenceTimer);
    _clearBrowserTtsRecovery();
    try{ if(_recognition) _recognition.abort(); }catch(_){}
    _recognition=null;
    if(typeof stopTTS==='function') stopTTS();
    // Restore original autoReadLastAssistant
    if(_origAutoRead) window.autoReadLastAssistant=_origAutoRead;
    // Clear textarea if it was only voice input
    ta.value='';
    autoResize();
  }

  modeBtn.onclick=()=>{
    if(_voiceModeActive){
      _deactivate();
      showToast(t('voice_mode_off'),1500);
    }else{
      _activate();
    }
  };

  return Object.freeze({
    applyPreference:_applyVoiceModePref,
    autoReadLastAssistant:voiceAutoReadLastAssistant,
    deactivate:_deactivate,
    isActive:()=>_voiceModeActive,
    onResponseComplete:_voiceModeOnResponseComplete,
    sendImmediately:_voiceModeSend,
  });
})();

export {voiceMode};
