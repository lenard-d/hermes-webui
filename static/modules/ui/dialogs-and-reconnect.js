import { _startActivityElapsedTimer } from './activity-and-scroll.js';
import { normalizeLiveActivityGroupPlacement } from './anchor-scenes.js';
import { showToast } from './composer.js';
import { _postProcessWithAnchorSuppression } from './content-postprocessing.js';
import { _assistantTurnBlocks, _rehydrateTransparentStreamDom } from './presentation.js';
import { $, INFLIGHT, S, esc } from './state.js';
import { _dedupeLiveProcessedWorklogAnchors } from './transparent-worklog.js';

// ── Shared app dialogs ───────────────────────────────────────────────────────
// showConfirmDialog(opts) and showPromptDialog(opts) replace browser-native dialog calls
// throughout the UI. Both return Promises and support: title, message, confirmLabel,
// cancelLabel, danger (confirm only), placeholder/value/inputType (prompt only).

const APP_DIALOG={resolve:null,kind:null,lastFocus:null};
let _appDialogBound=false;

function _isAppDialogOpen(){
  const overlay=$('appDialogOverlay');
  return !!(overlay&&overlay.style.display!=='none');
}

function _getAppDialogFocusable(){
  return [$('appDialogInput'), $('appDialogCancel'), $('appDialogConfirm'), $('appDialogClose')]
    .filter(el=>el&&el.style.display!=='none'&&!el.disabled);
}

function _finishAppDialog(result, restoreFocus=true){
  const overlay=$('appDialogOverlay');
  const dialog=$('appDialog');
  const input=$('appDialogInput');
  const confirmBtn=$('appDialogConfirm');
  const resolve=APP_DIALOG.resolve;
  const lastFocus=APP_DIALOG.lastFocus;
  APP_DIALOG.resolve=null;
  APP_DIALOG.kind=null;
  APP_DIALOG.lastFocus=null;
  if(overlay){overlay.style.display='none';overlay.setAttribute('aria-hidden','true');}
  if(dialog) dialog.setAttribute('role','dialog');
  if(input){input.value='';input.style.display='none';input.placeholder='';}
  if(confirmBtn){confirmBtn.classList.remove('danger');confirmBtn.textContent=t('dialog_confirm_btn');}
  if(restoreFocus&&lastFocus&&typeof lastFocus.focus==='function'){setTimeout(()=>lastFocus.focus(),0);}
  if(resolve) resolve(result);
}

function _ensureAppDialogBindings(){
  if(_appDialogBound) return;
  _appDialogBound=true;
  const overlay=$('appDialogOverlay');
  const cancelBtn=$('appDialogCancel');
  const confirmBtn=$('appDialogConfirm');
  const closeBtn=$('appDialogClose');
  if(overlay){
    overlay.addEventListener('click',e=>{
      if(e.target===overlay) _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
    });
  }
  if(cancelBtn) cancelBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(closeBtn)  closeBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(confirmBtn){
    confirmBtn.addEventListener('click',()=>{
      if(APP_DIALOG.kind==='prompt'){
        const input=$('appDialogInput');
        _finishAppDialog(input?input.value:null);
      }else{
        _finishAppDialog(true);
      }
    });
  }
  document.addEventListener('keydown',e=>{
    if(!_isAppDialogOpen()) return;
    if(e.key==='Escape'){
      e.preventDefault();
      _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
      return;
    }
    if(e.key==='Enter'){
      if(window._isImeEnter&&window._isImeEnter(e)) return;
      const target=e.target;
      const isTextarea=target&&target.tagName==='TEXTAREA';
      if(!isTextarea){
        e.preventDefault();
        if(target===cancelBtn||target===closeBtn){
          _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
        }else if(APP_DIALOG.kind==='prompt'){
          const input=$('appDialogInput');
          _finishAppDialog(input?input.value:null);
        }else{
          _finishAppDialog(true);
        }
      }
      return;
    }
    if(e.key==='Tab'){
      const nodes=_getAppDialogFocusable();
      if(!nodes.length) return;
      const idx=nodes.indexOf(document.activeElement);
      let nextIdx=idx;
      if(e.shiftKey){nextIdx=idx<=0?nodes.length-1:idx-1;}
      else{nextIdx=idx===-1||idx===nodes.length-1?0:idx+1;}
      e.preventDefault();
      nodes[nextIdx].focus();
    }
  }, true);
}

function showConfirmDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(false,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='confirm';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_confirm_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){input.style.display='none';input.value='';}
  if(cancelBtn){
    if(opts.hideCancel){cancelBtn.style.display='none';}
    else{cancelBtn.style.display='';cancelBtn.textContent=opts.cancelLabel||t('cancel');}
  }
  if(confirmBtn){
    confirmBtn.textContent=opts.confirmLabel||t('dialog_confirm_btn');
    confirmBtn.classList.toggle('danger',!!opts.danger);
  }
  if(dialog) dialog.setAttribute('role',opts.danger?'alertdialog':'dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>((opts.focusCancel?cancelBtn:confirmBtn)||confirmBtn||cancelBtn).focus(),0);
  });
}

function showPromptDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(null,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='prompt';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_prompt_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){
    input.type=opts.inputType||'text';input.style.display='';
    // Pre-fill: prefer `value`, accept `defaultValue` as alias for callers that
    // mirror the standard HTMLInputElement.defaultValue naming. Both empty →
    // blank field (the default rename-from-scratch flow stays unchanged).
    const prefill=(opts.value!=null?opts.value:(opts.defaultValue!=null?opts.defaultValue:''));
    input.value=prefill;input.placeholder=opts.placeholder||'';
    input.autocomplete='off';input.spellcheck=false;
  }
  if(cancelBtn){
    // A prior showConfirmDialog({hideCancel:true}) (e.g. the outside-symlink info
    // dialog, #4581) may have hidden the shared Cancel button; always restore it
    // so a subsequent prompt keeps its Cancel affordance.
    cancelBtn.style.display='';
    cancelBtn.textContent=opts.cancelLabel||t('cancel');
  }
  if(confirmBtn){
    confirmBtn.textContent=opts.confirmLabel||t('create');
    confirmBtn.classList.toggle('danger',!!opts.danger);
  }
  if(dialog) dialog.setAttribute('role',opts.danger?'alertdialog':'dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>{
      if(input&&input.style.display!=='none'){
        input.focus();
        // Selection behavior on focus:
        //   selectStem:true → select everything before the LAST '.' (e.g. for
        //     'report.txt' selects 'report' so a user can retype the basename
        //     without losing the extension; matches macOS Finder rename UX).
        //     Falls back to selecting the full value when there's no '.' or
        //     the dot is at index 0 ('.gitignore' → full select).
        //   selectAll:true → select the entire prefilled value.
        //   default       → caret at end (current behavior).
        const v=input.value||'';
        if(opts.selectStem && v){
          const dot=v.lastIndexOf('.');
          if(dot>0) input.setSelectionRange(0,dot);
          else input.select();
        } else if(opts.selectAll && v){
          input.select();
        }
      } else if(confirmBtn) confirmBtn.focus();
    },0);
  });
}


function _copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(text).catch(()=>{
      // Fallback if clipboard API fails (e.g. permissions)
      return _fallbackCopy(text);
    });
  }
  return _fallbackCopy(text);
}
function _fallbackCopy(text){
  return new Promise((resolve,reject)=>{
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.cssText='position:fixed;left:0;top:0;width:2em;height:2em;padding:0;border:none;outline:none;box-shadow:none;background:transparent;z-index:-1';
    document.body.appendChild(ta);
    ta.focus();ta.select();
    try{document.execCommand('copy');resolve();}
    catch(e){reject(e);}
    finally{document.body.removeChild(ta);}
  });
}
function copyStatusSessionId(btn){
  const text=btn&&btn.getAttribute('data-copy-status-session');
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=(typeof li==='function')?li('check',13):t('copied');
    btn.classList.add('copied');
    setTimeout(()=>{btn.innerHTML=orig;btn.classList.remove('copied');},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function copyMsg(btn){
  const row=btn.closest('[data-raw-text]');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML=li('check',13);btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function _copyThinkingText(btn){
  const card=btn&&btn.closest?btn.closest('.thinking-card'):null;
  if(!card)return;
  const pre=card.querySelector('.thinking-card-body pre');
  const text=pre?pre.textContent:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=li('check',12);
    btn.style.color='var(--accent)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}

// ── TTS: Text-to-Speech via Web Speech API (#499) ──
// Strips markdown, code blocks, and MEDIA: paths for clean speech output.
function _stripForTTS(text){
  // Remove code blocks entirely (```) — line-anchored to match #1438 fix
  text=text.replace(/(^|\n)[ ]{0,3}```(?:[\s\S]*?\n)?[ ]{0,3}```(?=\n|$)/g,' ');
  // Remove inline code
  text=text.replace(/`[^`]+`/g,' ');
  // Strip bold/italic
  text=text.replace(/\*\*(.+?)\*\*/g,'$1');
  text=text.replace(/\*(.+?)\*/g,'$1');
  text=text.replace(/__(.+?)__/g,'$1');
  text=text.replace(/_(.+?)_/g,'$1');
  // Strip headings
  text=text.replace(/^#{1,6}\s+/gm,'');
  // Strip links, keep text
  text=text.replace(/\[([^\]]+)\]\([^)]+\)/g,'$1');
  // Replace MEDIA: paths with a simple label
  text=text.replace(/MEDIA:[^\s]+/g,'a file');
  // Strip emoji and emoticons
  text=text.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{200D}]/gu,'');
  // Strip HTML tags that may leak through markdown
  text=text.replace(/<[^>]+>/g,' ');
  // Collapse whitespace
  text=text.replace(/\s+/g,' ').trim();
  return text;
}

function _splitForTTS(text, maxChars){
  // Split long text into chunks at natural sentence/paragraph boundaries
  // to avoid browser SpeechSynthesis truncation on long texts.
  maxChars=maxChars||300;
  if(text.length<=maxChars) return [text];
  const chunks=[];
  let remaining=text;
  while(remaining.length>0){
    if(remaining.length<=maxChars){ chunks.push(remaining); break; }
    let splitAt=maxChars;
    const sentencePattern=new RegExp('^[\\s\\S]{0,'+(maxChars-1)+'}[。！？.!？](?=\\s|$)','g');
    const m=sentencePattern.exec(remaining);
    if(m) splitAt=m.index+m[0].length;
    else{
      const sub=remaining.slice(0,maxChars);
      const lastSpace=Math.max(sub.lastIndexOf(' '),sub.lastIndexOf('\n'),sub.lastIndexOf(','),sub.lastIndexOf('，'));
      if(lastSpace>maxChars*0.5) splitAt=lastSpace+1;
    }
    chunks.push(remaining.slice(0,splitAt).trim());
    remaining=remaining.slice(splitAt).trim();
  }
  return chunks.filter(Boolean);
}

let _ttsSpeaking=false;
let _ttsCurrentUtterance=null;
let _ttsChunkQueue=[];
let _ttsChunkIndex=0;
let _ttsActiveBtn=null;
let _playingEdgeAudio=null;

function _buildBrowserUtterance(text, btn){
  const utter=new SpeechSynthesisUtterance(text);
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
    _ttsChunkIndex++;
    if(_ttsChunkIndex<_ttsChunkQueue.length){
      const next=new SpeechSynthesisUtterance(_ttsChunkQueue[_ttsChunkIndex]);
      next.voice=utter.voice; next.rate=utter.rate; next.pitch=utter.pitch;
      next.onend=utter.onend; next.onerror=utter.onerror;
      _ttsCurrentUtterance=next;
      speechSynthesis.speak(next);
    } else {
      _ttsSpeaking=false; _ttsCurrentUtterance=null;
      _ttsChunkQueue=[]; _ttsChunkIndex=0; _ttsActiveBtn=null;
      if(btn) btn.dataset.speaking='0';
    }
  };
  utter.onerror=()=>{
    _ttsSpeaking=false; _ttsCurrentUtterance=null;
    _ttsChunkQueue=[]; _ttsChunkIndex=0; _ttsActiveBtn=null;
    if(btn) btn.dataset.speaking='0';
  };
  return utter;
}

function _playEdgeTtsChunked(text, btn){
  _ttsSpeaking=true;
  if(btn) btn.dataset.speaking='1';
  const chunks=_splitForTTS(text);
  const _playOne=function(idx){
    if(idx>=chunks.length){
      _ttsSpeaking=false;_playingEdgeAudio=null;
      if(btn) btn.dataset.speaking='0';
      return;
    }
    const chunk=chunks[idx];
    const voice=localStorage.getItem('hermes-tts-voice')||'zh-CN-XiaoxiaoNeural';
    const savedRate=parseFloat(localStorage.getItem('hermes-tts-rate'));
    const savedPitch=parseFloat(localStorage.getItem('hermes-tts-pitch'));
    let rate='', pitch='';
    if(!isNaN(savedRate)){const pct=Math.round((savedRate-1)*100);const sign=pct>=0?'+':'';rate=sign+pct+'%';}
    if(!isNaN(savedPitch)){const hz=Math.round((savedPitch-1)*50);const sign=hz>=0?'+':'';pitch=sign+hz+'Hz';}
    fetch(new URL('api/tts', document.baseURI || location.href).href, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:chunk, voice:voice, rate:rate, pitch:pitch})
    })
    .then(function(r){
      if(!r.ok){
        return r.json().catch(function(){return {};}).then(function(j){
          throw new Error((j&&j.error)||('TTS request failed: '+r.status));
        });
      }
      return r.blob();
    })
    .then(function(blob){
      if(!_ttsSpeaking) return;
      const url=URL.createObjectURL(blob);
      const audio=new Audio(url);
      _playingEdgeAudio=audio;
      audio.onended=function(){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        if(_ttsSpeaking) _playOne(idx+1);
      };
      audio.onerror=function(){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        _ttsSpeaking=false;
        if(btn) btn.dataset.speaking='0';
      };
      audio.play().catch(function(e){
        URL.revokeObjectURL(url);
        _playingEdgeAudio=null;
        _ttsSpeaking=false;
        if(btn) btn.dataset.speaking='0';
        if(typeof showToast==='function') showToast('Edge TTS error: '+(e&&e.message||e));
      });
    })
    .catch(function(e){
      _ttsSpeaking=false;_playingEdgeAudio=null;
      if(btn) btn.dataset.speaking='0';
      if(typeof showToast==='function') showToast('Edge TTS failed: '+(e&&e.message||e));
    });
  };
  _playOne(0);
}

function speakMessage(btn){
  if(btn&&btn.dataset.speaking==='1'){
    stopTTS();
    return;
  }
  stopTTS();

  const row=btn?btn.closest('[data-raw-text]'):null;
  const text=row?row.dataset.rawText:'';
  if(!text) return;

  const clean=_stripForTTS(text);
  if(!clean) return;

  const engine=localStorage.getItem('hermes-tts-engine')||'browser';
  if(engine==='openai'){
    _playOpenaiTts(clean, btn);
    return;
  }
  if(engine==='elevenlabs'){
    _playElevenLabsTts(clean, btn);
    return;
  }
  if(engine==='edge'){
    _playEdgeTtsChunked(clean, btn);
    return;
  }
  // Extension-registered TTS engine (window.registerHermesTtsEngine). Synthesize
  // via the extension, then play through the shared audio-buffer path.
  if(typeof window._hermesTtsIsRegistered==='function' && window._hermesTtsIsRegistered(engine)){
    if(btn) btn.dataset.speaking='1';
    _ttsSpeaking=true;
    const _failReg=function(msg){
      _ttsSpeaking=false;_playingEdgeAudio=null;
      if(btn)btn.dataset.speaking='0';
      if(msg&&typeof showToast==='function') showToast(msg,4000,'error');
    };
    const _opts={
      voice: localStorage.getItem('hermes-tts-voice')||'',
      rate: parseFloat(localStorage.getItem('hermes-tts-rate')),
      pitch: parseFloat(localStorage.getItem('hermes-tts-pitch')),
    };
    Promise.resolve(window._hermesTtsSynth(engine, clean, _opts))
      .then(function(buf){ return _playAudioBuf(buf, btn, 'TTS'); })
      .catch(function(e){ _failReg((e&&e.message)||'TTS engine failed'); });
    return;
  }

  if(!('speechSynthesis' in window)){
    showToast(t('tts_not_supported')||'Speech synthesis not supported in this browser.');
    return;
  }

  _ttsChunkQueue=_splitForTTS(clean);
  _ttsChunkIndex=0;
  _ttsActiveBtn=btn;
  _ttsSpeaking=true;
  if(btn) btn.dataset.speaking='1';

  const utter=_buildBrowserUtterance(_ttsChunkQueue[0], btn);
  _ttsCurrentUtterance=utter;
  speechSynthesis.speak(utter);
}

function _playElevenLabsTts(text, btn){
  if(btn) btn.dataset.speaking='1';
  _ttsSpeaking=true;
  const _fail=function(msg){
    _ttsSpeaking=false;_playingEdgeAudio=null;
    if(btn)btn.dataset.speaking='0';
    if(msg&&typeof showToast==='function') showToast(msg,4000,'error');
  };
  fetch(new URL('api/tts', document.baseURI || location.href).href, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text, engine:'elevenlabs'})
  })
  .then(function(r){
    if(!r.ok){
      return r.json().catch(function(){return {};}).then(function(j){
        throw new Error((j&&j.error)||('TTS request failed: '+r.status));
      });
    }
    return r.arrayBuffer();
  })
  .then(function(buf){
    return _playAudioBuf(buf, btn, 'ElevenLabs TTS');
  })
  .catch(function(e){ _fail((e&&e.message)||'ElevenLabs TTS failed'); });
}

function _playOpenaiTts(text, btn){
  if(btn) btn.dataset.speaking='1';
  _ttsSpeaking=true;
  const _fail=function(msg){
    _ttsSpeaking=false;_playingEdgeAudio=null;
    if(btn)btn.dataset.speaking='0';
    if(msg&&typeof showToast==='function') showToast(msg,4000,'error');
  };
  fetch(new URL('api/tts', document.baseURI || location.href).href, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text, engine:'openai'})
  })
  .then(function(r){
    if(!r.ok){
      return r.json().catch(function(){return {};}).then(function(j){
        throw new Error((j&&j.error)||('TTS request failed: '+r.status));
      });
    }
    return r.arrayBuffer();
  })
  .then(function(buf){
    return _playAudioBuf(buf, btn, 'OpenAI TTS');
  })
  .catch(function(e){ _fail((e&&e.message)||'OpenAI TTS failed'); });
}

// ── Shared AudioContext for TTS playback (no blob URLs needed) ──
let _ttsAudioCtx=null;
function _getTtsAudioCtx(){
  if(!_ttsAudioCtx){
    const C=window.AudioContext||window.webkitAudioContext;
    if(!C) return null;
    _ttsAudioCtx=new C();
  }
  if(_ttsAudioCtx.state==='suspended') _ttsAudioCtx.resume();
  return _ttsAudioCtx;
}

function _playAudioBuf(arrayBuffer, btn, label){
  const ctx=_getTtsAudioCtx();
  if(!ctx){
    if(btn)btn.dataset.speaking='0';
    _ttsSpeaking=false;
    showToast(label+': Web Audio API not available');
    return;
  }
  return new Promise(function(resolve){
    ctx.decodeAudioData(arrayBuffer.slice(0), function(audioBuffer){
      const src=ctx.createBufferSource();
      src.buffer=audioBuffer;
      src.connect(ctx.destination);
      _playingEdgeAudio=src;
      const _cleanup=function(){
        _ttsSpeaking=false;_playingEdgeAudio=null;
        if(btn)btn.dataset.speaking='0';
        try{src.stop();src.disconnect();}catch(_){}
        resolve();
      };
      src.onended=_cleanup;
      src.start(0);
    }, function(e){
      _ttsSpeaking=false;
      if(btn)btn.dataset.speaking='0';
      showToast(label+' error: '+(e&&e.message||e));
      resolve(); // prevent permanently pending Promise on decode failure
    });
  });
}
function stopTTS(){
  if('speechSynthesis' in window){
    speechSynthesis.cancel();
  }
  // Stop Web Audio API playback (AudioBufferSourceNode)
  if(_playingEdgeAudio){
    try{
      if(typeof _playingEdgeAudio.stop==='function'){
        _playingEdgeAudio.stop(); _playingEdgeAudio.disconnect();
      }else{
        _playingEdgeAudio.pause(); _playingEdgeAudio.currentTime=0;
      }
    }catch(_){}
    _playingEdgeAudio=null;
  }
  _ttsSpeaking=false;
  _ttsCurrentUtterance=null;
  _ttsChunkQueue=[];
  _ttsChunkIndex=0;
  _ttsActiveBtn=null;
  // Reset all speaking buttons
  document.querySelectorAll('[data-speaking="1"]').forEach(btn=>{ btn.dataset.speaking='0'; });
}

function autoReadLastAssistant(){
  const engine=localStorage.getItem('hermes-tts-engine')||'browser';
  if(engine==='browser'&&!('speechSynthesis' in window)) return;
  const pref=localStorage.getItem('hermes-tts-auto-read');
  if(pref!=='true') return;
  // Find the last assistant message segment in the DOM
  const rows=document.querySelectorAll('.msg-row[data-role="assistant"], .assistant-segment[data-raw-text]');
  if(!rows.length) return;
  const last=rows[rows.length-1];
  const text=last.dataset.rawText||'';
  if(!text.trim()) return;
  const clean=_stripForTTS(text);
  if(!clean) return;
  if(engine==='openai'){
    _playOpenaiTts(clean, null);
    return;
  }
  if(engine==='elevenlabs'){
    _playElevenLabsTts(clean, null);
    return;
  }
  if(engine==='edge'){
    _playEdgeTtsChunked(clean, null);
    return;
  }
  // Extension-registered TTS engine (window.registerHermesTtsEngine): synth via
  // the extension, then play through the shared audio-buffer path. Mirrors the
  // registered-engine branch in speakMessage() so auto-read honors the selection.
  if(typeof window._hermesTtsIsRegistered==='function' && window._hermesTtsIsRegistered(engine)){
    _ttsSpeaking=true;
    const _opts={
      voice: localStorage.getItem('hermes-tts-voice')||'',
      rate: parseFloat(localStorage.getItem('hermes-tts-rate')),
      pitch: parseFloat(localStorage.getItem('hermes-tts-pitch')),
    };
    Promise.resolve(window._hermesTtsSynth(engine, clean, _opts))
      .then(function(buf){ return _playAudioBuf(buf, null, 'TTS'); })
      .catch(function(){ _ttsSpeaking=false; _playingEdgeAudio=null; });
    return;
  }
  // Unknown/unregistered engine (e.g. an extension engine that's no longer
  // registered) — fall back to browser TTS only if it's available.
  if(!('speechSynthesis' in window)) return;
  // Use chunked playback for browser TTS
  _ttsChunkQueue=_splitForTTS(clean);
  _ttsChunkIndex=0;
  _ttsSpeaking=true;
  const utter=_buildBrowserUtterance(_ttsChunkQueue[0], null);
  _ttsCurrentUtterance=utter;
  speechSynthesis.speak(utter);
}

// ── Reconnect banner (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state'; // localStorage snapshots for mid-stream reload recovery
const INFLIGHT_STATE_DEFAULT_LIMITS = {
  maxSessions:8,
  messages:24,
  toolCalls:48,
  stringChars:60000,
  jsonChars:1500000,
};

function _boundedInflightInt(value, fallback, min, max){
  const n=parseInt(value,10);
  if(!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}
function _getInflightStateLimits(){
  const configured=(typeof window!=='undefined'&&window._inflightStateLimits&&typeof window._inflightStateLimits==='object')?window._inflightStateLimits:{};
  return {
    maxSessions:_boundedInflightInt(configured.maxSessions, INFLIGHT_STATE_DEFAULT_LIMITS.maxSessions, 1, 25),
    messages:_boundedInflightInt(configured.messages, INFLIGHT_STATE_DEFAULT_LIMITS.messages, 1, 100),
    toolCalls:_boundedInflightInt(configured.toolCalls, INFLIGHT_STATE_DEFAULT_LIMITS.toolCalls, 1, 200),
    stringChars:_boundedInflightInt(configured.stringChars, INFLIGHT_STATE_DEFAULT_LIMITS.stringChars, 1000, 500000),
    jsonChars:_boundedInflightInt(configured.jsonChars, INFLIGHT_STATE_DEFAULT_LIMITS.jsonChars, 100000, 4000000),
  };
}

function _readInflightStateMap(){
  try{
    const raw=localStorage.getItem(INFLIGHT_STATE_KEY);
    const parsed=raw?JSON.parse(raw):{};
    return parsed&&typeof parsed==='object'?parsed:{};
  }catch(_){
    return {};
  }
}
function _isStorageQuotaError(err){
  return !!err && (
    err.name==='QuotaExceededError' ||
    err.name==='NS_ERROR_DOM_QUOTA_REACHED' ||
    err.code===22 ||
    err.code===1014
  );
}
function _truncateInflightValue(value, maxChars){
  const limits=_getInflightStateLimits();
  const stringLimit=_boundedInflightInt(maxChars, limits.stringChars, 1000, 500000);
  if(typeof value==='string'){
    if(value.length<=stringLimit) return value;
    return value.slice(0,stringLimit)+'\n\n[truncated for browser recovery storage]';
  }
  if(Array.isArray(value)) return value.map(v=>_truncateInflightValue(v, Math.max(2000, Math.floor(stringLimit/2))));
  if(value&&typeof value==='object'){
    const out={};
    for(const [k,v] of Object.entries(value)) out[k]=_truncateInflightValue(v, stringLimit);
    return out;
  }
  return value;
}
function _compactInflightState(state){
  const limits=_getInflightStateLimits();
  const messages=Array.isArray(state.messages)?state.messages.slice(-limits.messages):[];
  const toolCalls=Array.isArray(state.toolCalls)?state.toolCalls.slice(-limits.toolCalls):[];
  // Phase 2: persist the live todo snapshot so reload / SSE reattach
  // restores the panel without waiting for the next live `todo` write.
  // The list is bounded by the agent (typically <20 items) and each
  // item is small, so no per-list cap is needed beyond the existing
  // stringChars truncation in _truncateInflightValue.
  const todos=Array.isArray(state.todos)?state.todos:null;
  const todoStateMeta=(state.todoStateMeta&&typeof state.todoStateMeta==='object')?state.todoStateMeta:null;
  return _truncateInflightValue({
    streamId:state.streamId||null,
    messages,
    uploaded:Array.isArray(state.uploaded)?state.uploaded.slice(-20):[],
    toolCalls,
    lastAssistantText:state.lastAssistantText||'',
    lastReasoningText:state.lastReasoningText||'',
    lastRunJournalSeq:state.lastRunJournalSeq||0,
    lastRunJournalEventId:state.lastRunJournalEventId||'',
    journalReplayFromStart:!!state.journalReplayFromStart,
    currentActivityBurstId:state.currentActivityBurstId||0,
    currentLiveSegmentSeq:state.currentLiveSegmentSeq||0,
    activityBurstAnchors:Array.isArray(state.activityBurstAnchors)?state.activityBurstAnchors.slice(-50):[],
    todos,
    todoStateMeta,
  }, limits.stringChars);
}
function _writeInflightStateMap(all){
  const limits=_getInflightStateLimits();
  const entries=Object.entries(all||{})
    .sort((a,b)=>Number(b[1]&&b[1].updated_at||0)-Number(a[1]&&a[1].updated_at||0))
    .slice(0,limits.maxSessions);
  const compact={};
  for(const [sid,entry] of entries) compact[sid]=entry;
  let json=JSON.stringify(compact);
  if(json.length>limits.jsonChars){
    const current=entries[0];
    json=JSON.stringify(current?{[current[0]]:current[1]}:{});
  }
  if(json.length>limits.jsonChars){
    localStorage.removeItem(INFLIGHT_STATE_KEY);
    return false;
  }
  localStorage.setItem(INFLIGHT_STATE_KEY,json);
  return true;
}
function saveInflightState(sid, state){
  if(!sid||!state) return;
  const entry={..._compactInflightState(state),updated_at:Date.now()};
  try{
    const all=_readInflightStateMap();
    all[sid]=entry;
    _writeInflightStateMap(all);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      _writeInflightStateMap({[sid]:entry});
    }catch(_){
      try{localStorage.removeItem(INFLIGHT_STATE_KEY);}catch(__){}
    }
  }
}
function loadInflightState(sid, streamId){
  if(!sid) return null;
  const all=_readInflightStateMap();
  const entry=all[sid];
  if(!entry) return null;
  if(streamId&&entry.streamId&&entry.streamId!==streamId) return null;
  if(entry.updated_at&&Date.now()-entry.updated_at>10*60*1000){
    clearInflightState(sid);
    return null;
  }
  return entry;
}
function clearInflightState(sid){
  if(!sid) return;
  try{
    const all=_readInflightStateMap();
    if(!(sid in all)) return;
    delete all[sid];
    if(Object.keys(all).length) localStorage.setItem(INFLIGHT_STATE_KEY, JSON.stringify(all));
    else localStorage.removeItem(INFLIGHT_STATE_KEY);
  }catch(_){ }
}

// ─── Todo state: single source of truth + render scheduling ─────────────────
//
// Three concerns live together so they can share state cleanly:
//
//   1. _todosHash(items)  — cheap content fingerprint; skips re-render when
//      a snapshot would paint the same DOM.  Used both as a short-circuit
//      and as the hash that compares "rendered vs current" snapshots.
//
//   2. scheduleTodosRefresh() — coalesces multiple `todo_state` events that
//      land in the same animation frame into a single refresh pass. It keeps
//      the left sidebar Todos behavior unchanged, and also lets the workspace
//      Todos tab repaint when that tab is enabled and currently visible.
//
//   3. _hydrateTodosFromSession(session) — applies cold-load todo_state
//      from the session GET payload, or clears the panel when neither a
//      cold-load nor an INFLIGHT signal is available.  Called at every
//      `S.session = ...` settle point so cross-session navigation never
//      leaves a stale list visible.
//
// The hash is keyed on (id, content/text, status); the render itself uses
// `esc()` for any user-controlled string, so XSS surface is the same as
// any other innerHTML path in this file.
let _todosLastRenderedHash=null;
let _todosRenderRafId=0;

function _todosHash(items){
  if(!Array.isArray(items)) return '';
  // String concat outperforms JSON.stringify on small arrays in V8 (no
  // intermediate object allocation) and is exact enough — the field set
  // matches what the renderer reads, so any visible change in DOM
  // implies a hash change.  Field separators (\x1f, \x1e) are control
  // chars unlikely to appear in real todo content, so collisions across
  // boundaries are not realistic.
  let h=items.length+'|';
  for(let i=0;i<items.length;i++){
    const t=items[i]||{};
    const content=t.content==null?(t.text==null?'':t.text):t.content;
    h+=String(t.id==null?'':t.id)+'\x1f'+String(content)+'\x1f'+String(t.status==null?'':t.status)+'\x1e';
  }
  return h;
}

const TODO_STATUS_RENDERING=Object.freeze({
  pending:Object.freeze({icon:'square',color:'var(--muted)'}),
  in_progress:Object.freeze({icon:'loader',color:'var(--blue)'}),
  completed:Object.freeze({icon:'check',color:'rgba(100,200,100,.8)'}),
  cancelled:Object.freeze({icon:'x',color:'rgba(200,100,100,.5)'}),
});

function todoStatusKey(status){
  const key=String(status||'pending');
  return Object.prototype.hasOwnProperty.call(TODO_STATUS_RENDERING,key)?key:'pending';
}

function todoStatusVisual(status){
  const key=todoStatusKey(status);
  return TODO_STATUS_RENDERING[key];
}

function renderTodoStatusIcon(status,size=14){
  const visual=todoStatusVisual(status);
  return typeof li==='function'?li(visual.icon,size):'';
}

function todoContent(todo){
  if(!todo) return '';
  return todo.content==null?(todo.text==null?'':todo.text):todo.content;
}

function renderTodoEmptyState(options={}){
  const centered=!!(options&&options.centered);
  const style=centered
    ? 'padding:24px 12px;text-align:center;color:var(--muted);font-size:12px'
    : 'color:var(--muted);font-size:12px;padding:4px 0';
  return `<div style="${style}">${esc(t('todos_no_active'))}</div>`;
}

function renderTodoRow(todo,options={}){
  const td=todo||{};
  const status=todoStatusKey(td.status);
  const visual=todoStatusVisual(status);
  const showMetadata=!(options&&options.metadata===false);
  const isCompleted=status==='completed';
  const isCancelled=status==='cancelled';
  const contentColor=(isCompleted||isCancelled)?'var(--muted)':'var(--text)';
  const completedStyle=(isCompleted||isCancelled)?'text-decoration:line-through;opacity:.5':'';
  const metadata=showMetadata
    ? `<div style="font-size:10px;color:var(--muted);margin-top:2px;opacity:.6">${esc(td.id)} · ${esc(status)}</div>`
    : '';
  return `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:14px;display:inline-flex;align-items:center;flex-shrink:0;margin-top:1px;color:${visual.color}">${renderTodoStatusIcon(status,14)}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;color:${contentColor};${completedStyle};line-height:1.4">${esc(todoContent(td))}</div>
        ${metadata}
      </div>
    </div>`;
}

function renderTodoRows(todos,options={}){
  const items=Array.isArray(todos)?todos:[];
  return items.map(td=>renderTodoRow(td,options)).join('');
}

function _todosPanelIsActive(){
  if(typeof document==='undefined') return false;
  const panel=document.getElementById('panelTodos');
  return !!(panel&&panel.classList&&panel.classList.contains('active'));
}

function scheduleTodosRefresh(){
  // Idempotent: many `todo_state` events fire on each tool result, but
  // only the latest snapshot needs to paint.  RAF lets us coalesce
  // without timer drift.
  if(_todosRenderRafId) return;
  if(typeof requestAnimationFrame!=='function'){
    if(typeof loadTodos==='function') loadTodos();
    if(typeof _refreshWorkspacePanelTodos==='function') _refreshWorkspacePanelTodos();
    return;
  }
  _todosRenderRafId=requestAnimationFrame(()=>{
    _todosRenderRafId=0;
    const sidebarActive=_todosPanelIsActive();
    if(sidebarActive&&typeof loadTodos==='function') loadTodos();
    if(typeof _refreshWorkspacePanelTodos==='function') _refreshWorkspacePanelTodos();
  });
}

function _resetTodosRenderCache(){
  // Clear after every cross-session navigation so the next render is
  // never short-circuited against a hash from a different session.
  _todosLastRenderedHash=null;
  if(typeof _resetWorkspaceTodosRenderCache==='function') _resetWorkspaceTodosRenderCache();
}

function _hydrateTodosFromSession(session){
  // Three input cases, three deterministic outcomes:
  //   a) cold-load AND inflight both present  → pick newer by ts so a
  //      stale cold-load from the session GET cannot regress a fresher
  //      INFLIGHT snapshot persisted from a still-running stream
  //      (avoids visible rollback on reload).
  //   b) only one of cold-load / inflight is present  → use it.
  //   c) neither  → reset to empty + sentinel so loadTodos() falls
  //      through to the legacy reverse-scan or paints the empty state.
  const sid=(session&&session.session_id)||'';
  const inflight=(typeof INFLIGHT==='object'&&INFLIGHT&&sid)?INFLIGHT[sid]:null;
  const cold=session&&session.todo_state;
  const coldOk=!!(cold&&Array.isArray(cold.todos));
  const inflightOk=!!(inflight&&Array.isArray(inflight.todos)&&inflight.todoStateMeta);
  const coldTs=coldOk?(Number(cold.ts)||0):0;
  const inflightTs=inflightOk?(Number(inflight.todoStateMeta&&inflight.todoStateMeta.ts)||0):0;
  // Whether a live stream currently owns this session. This is the signal
  // that disambiguates a ts-less cold-load (see below); it comes from the
  // session GET payload (mirrors sessions.js `S.session.active_stream_id`).
  const streamActive=!!(session&&session.active_stream_id);
  if(coldOk&&inflightOk){
    // Reconcile the server's settled cold-load snapshot against the
    // locally-persisted INFLIGHT snapshot.
    //
    // coldTs===0 means the cold-load carries NO usable timestamp, so we
    // cannot order it against INFLIGHT by recency. A todo tool message can
    // legitimately lose its `timestamp` during context compression/rebuild
    // (the on-disk message ends up timestamp=None), and derive_todo_state
    // (api/todo_state.py) then returns the correct latest-by-POSITION todos
    // but omits `ts`. The tie-break depends on who owns the INFLIGHT tail:
    //
    //   - stream ACTIVE → INFLIGHT is the live tail. The most recent todo
    //     write may still be in flight and not yet settled into the message
    //     list derive_todo_state scans, so a ts-less cold-load can be an
    //     OLDER (pre-latest-write) view. Letting cold win here rolls the
    //     panel back to a stale list, and since the stream may have just
    //     ended on that very write there is no guaranteed forward SSE event
    //     to self-heal. So prefer INFLIGHT. If cold is in fact newer, the
    //     reattach replay (sessions.js attachLiveStream, reconnecting) re-
    //     emits the journaled `todo_state` events which reconcile forward by
    //     ts, so any transient discrepancy corrects itself.
    //
    //   - stream IDLE → INFLIGHT is leftover from a finished/crashed stream
    //     (idle sessions purge it shortly after, sessions.js), and there is
    //     no replay to correct anything. The settled cold-load is the
    //     authoritative latest-by-position view, so prefer cold. This also
    //     preserves the original fix for the "shows an old todo list" bug,
    //     where a stale prior-turn INFLIGHT must not beat a ts-less cold-load.
    //
    // When coldTs>0 the original recency rule stands: strict ">", and on a
    // tie prefer INFLIGHT for the freshest in-tab edits.
    const coldWins=(coldTs===0)?(!streamActive):(coldTs>inflightTs);
    if(coldWins){
      S.todos=cold.todos;
      S.todoStateMeta={
        ts:coldTs,
        source:'cold-load',
        version:Number(cold.version)||1,
      };
    }else{
      S.todos=inflight.todos;
      S.todoStateMeta=inflight.todoStateMeta;
    }
  }else if(coldOk){
    S.todos=cold.todos;
    S.todoStateMeta={
      ts:coldTs,
      source:'cold-load',
      version:Number(cold.version)||1,
    };
  }else if(inflightOk){
    S.todos=inflight.todos;
    S.todoStateMeta=inflight.todoStateMeta;
  }else{
    S.todos=[];
    S.todoStateMeta=null;
  }
  _resetTodosRenderCache();
  if(typeof scheduleTodosRefresh==='function') scheduleTodosRefresh();
}

function snapshotLiveTurnHtmlForSession(sid){
  // Keep the DOM snapshot memory-only. Persisted INFLIGHT state intentionally
  // stores structured stream state, not outerHTML, so a hard reload still uses
  // the safer flat replay path instead of reviving stale nodes/listeners.
  if(!sid||!INFLIGHT[sid]) return;
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  if(turn.dataset&&turn.dataset.sessionId&&turn.dataset.sessionId!==sid) return;
  INFLIGHT[sid].liveTurnHtml=turn.outerHTML;
}

function _liveAssistantSegmentTextLength(seg){
  if(!seg) return 0;
  const body=seg.querySelector('.msg-body')||seg;
  return String(body.textContent||'').trim().length;
}

function _mergeRestoredLiveAssistantSegment(restored, existing){
  if(!restored||!existing) return;
  const existingLive=existing.querySelector('[data-live-assistant="1"]');
  if(!existingLive) return;
  const restoredLive=restored.querySelector('[data-live-assistant="1"]');
  const existingLen=_liveAssistantSegmentTextLength(existingLive);
  const restoredLen=_liveAssistantSegmentTextLength(restoredLive);
  if(existingLen<=restoredLen) return;
  const replacement=existingLive.cloneNode(true);
  if(restoredLive){
    restoredLive.replaceWith(replacement);
    return;
  }
  const blocks=_assistantTurnBlocks(restored);
  if(!blocks) return;
  const anchor=Array.from(blocks.children).filter(el=>
    el.matches('.tool-call-group,.tool-card-row,.agent-activity-thinking,.thinking-card-row,[data-live-assistant="1"]')
  ).pop();
  if(anchor) anchor.insertAdjacentElement('afterend', replacement);
  else blocks.appendChild(replacement);
}

function restoreLiveTurnHtmlForSession(sid){
  const inflight=INFLIGHT[sid];
  if(!sid||!inflight||!inflight.liveTurnHtml) return false;
  const inner=$('msgInner');
  if(!inner) return false;
  const template=document.createElement('template');
  template.innerHTML=String(inflight.liveTurnHtml||'').trim();
  const restored=template.content.firstElementChild;
  if(!restored) return false;
  restored.id='liveAssistantTurn';
  if(S.session) restored.dataset.sessionId=S.session.session_id;
  const existing=$('liveAssistantTurn');
  _mergeRestoredLiveAssistantSegment(restored, existing);
  if(existing) existing.replaceWith(restored);
  else inner.appendChild(restored);
  // Transparent Stream: liveTurnHtml is restored via template.innerHTML, which
  // drops the property-bound onclick/onkeydown handlers wired by
  // _wireTransparentHeaderToggle / _attachCopyButton / _syncTransparentEventControls /
  // _wireTransparentTurnToggle. The settled cache fast-path re-runs the rehydrate;
  // this active-session live-turn restore path must too, or row toggles, copy
  // buttons, expand/collapse, and the turn chevron silently stop working after a
  // session-switch/reconnect restore. (Codex trifecta finding C1.)
  if(typeof _rehydrateTransparentStreamDom==='function') _rehydrateTransparentStreamDom(restored);
  if(typeof normalizeLiveActivityGroupPlacement==='function') normalizeLiveActivityGroupPlacement(restored);
  if(typeof _dedupeLiveProcessedWorklogAnchors==='function') _dedupeLiveProcessedWorklogAnchors(restored);
  const liveGroup=restored.querySelector('.tool-call-group[data-live-tool-call-group="1"]');
  if(liveGroup&&typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(liveGroup);
  if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
  requestAnimationFrame(()=>_postProcessWithAnchorSuppression(restored));
  return true;
}

function markInflight(sid, streamId) {
  const payload=JSON.stringify({sid, streamId, ts: Date.now()});
  try{
    localStorage.setItem(INFLIGHT_KEY, payload);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      localStorage.setItem(INFLIGHT_KEY, payload);
    }catch(_){}
  }
}
function clearInflight() {
  localStorage.removeItem(INFLIGHT_KEY);
}
function showReconnectBanner(msg) {
  $('reconnectMsg').textContent = msg || 'A response may have been in progress when you last left.';
  $('reconnectBanner').classList.add('visible');
}
function dismissReconnect() {
  $('reconnectBanner').classList.remove('visible');
  clearInflight();
}



export {
  _isAppDialogOpen,
  _getAppDialogFocusable,
  _finishAppDialog,
  _ensureAppDialogBindings,
  showConfirmDialog,
  showPromptDialog,
  _copyText,
  _fallbackCopy,
  copyStatusSessionId,
  copyMsg,
  _copyThinkingText,
  _stripForTTS,
  _splitForTTS,
  _buildBrowserUtterance,
  _playEdgeTtsChunked,
  speakMessage,
  _playElevenLabsTts,
  _playOpenaiTts,
  _getTtsAudioCtx,
  _playAudioBuf,
  stopTTS,
  autoReadLastAssistant,
  _boundedInflightInt,
  _getInflightStateLimits,
  _readInflightStateMap,
  _isStorageQuotaError,
  _truncateInflightValue,
  _compactInflightState,
  _writeInflightStateMap,
  saveInflightState,
  loadInflightState,
  clearInflightState,
  _todosHash,
  todoStatusKey,
  todoStatusVisual,
  renderTodoStatusIcon,
  todoContent,
  renderTodoEmptyState,
  renderTodoRow,
  renderTodoRows,
  _todosPanelIsActive,
  scheduleTodosRefresh,
  _resetTodosRenderCache,
  _hydrateTodosFromSession,
  snapshotLiveTurnHtmlForSession,
  _liveAssistantSegmentTextLength,
  _mergeRestoredLiveAssistantSegment,
  restoreLiveTurnHtmlForSession,
  markInflight,
  clearInflight,
  showReconnectBanner,
  dismissReconnect,
  APP_DIALOG,
  INFLIGHT_KEY,
  INFLIGHT_STATE_KEY,
  INFLIGHT_STATE_DEFAULT_LIMITS,
  TODO_STATUS_RENDERING,
  _appDialogBound,
  _ttsSpeaking,
  _ttsCurrentUtterance,
  _ttsChunkQueue,
  _ttsChunkIndex,
  _ttsActiveBtn,
  _playingEdgeAudio,
  _ttsAudioCtx,
  _todosLastRenderedHash,
  _todosRenderRafId,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _isAppDialogOpen: { enumerable: true, get: () => _isAppDialogOpen, set: (value) => { _isAppDialogOpen = value; } },
  _getAppDialogFocusable: { enumerable: true, get: () => _getAppDialogFocusable, set: (value) => { _getAppDialogFocusable = value; } },
  _finishAppDialog: { enumerable: true, get: () => _finishAppDialog, set: (value) => { _finishAppDialog = value; } },
  _ensureAppDialogBindings: { enumerable: true, get: () => _ensureAppDialogBindings, set: (value) => { _ensureAppDialogBindings = value; } },
  showConfirmDialog: { enumerable: true, get: () => showConfirmDialog, set: (value) => { showConfirmDialog = value; } },
  showPromptDialog: { enumerable: true, get: () => showPromptDialog, set: (value) => { showPromptDialog = value; } },
  _copyText: { enumerable: true, get: () => _copyText, set: (value) => { _copyText = value; } },
  _fallbackCopy: { enumerable: true, get: () => _fallbackCopy, set: (value) => { _fallbackCopy = value; } },
  copyStatusSessionId: { enumerable: true, get: () => copyStatusSessionId, set: (value) => { copyStatusSessionId = value; } },
  copyMsg: { enumerable: true, get: () => copyMsg, set: (value) => { copyMsg = value; } },
  _copyThinkingText: { enumerable: true, get: () => _copyThinkingText, set: (value) => { _copyThinkingText = value; } },
  _stripForTTS: { enumerable: true, get: () => _stripForTTS, set: (value) => { _stripForTTS = value; } },
  _splitForTTS: { enumerable: true, get: () => _splitForTTS, set: (value) => { _splitForTTS = value; } },
  _buildBrowserUtterance: { enumerable: true, get: () => _buildBrowserUtterance, set: (value) => { _buildBrowserUtterance = value; } },
  _playEdgeTtsChunked: { enumerable: true, get: () => _playEdgeTtsChunked, set: (value) => { _playEdgeTtsChunked = value; } },
  speakMessage: { enumerable: true, get: () => speakMessage, set: (value) => { speakMessage = value; } },
  _playElevenLabsTts: { enumerable: true, get: () => _playElevenLabsTts, set: (value) => { _playElevenLabsTts = value; } },
  _playOpenaiTts: { enumerable: true, get: () => _playOpenaiTts, set: (value) => { _playOpenaiTts = value; } },
  _getTtsAudioCtx: { enumerable: true, get: () => _getTtsAudioCtx, set: (value) => { _getTtsAudioCtx = value; } },
  _playAudioBuf: { enumerable: true, get: () => _playAudioBuf, set: (value) => { _playAudioBuf = value; } },
  stopTTS: { enumerable: true, get: () => stopTTS, set: (value) => { stopTTS = value; } },
  autoReadLastAssistant: { enumerable: true, get: () => autoReadLastAssistant, set: (value) => { autoReadLastAssistant = value; } },
  _boundedInflightInt: { enumerable: true, get: () => _boundedInflightInt, set: (value) => { _boundedInflightInt = value; } },
  _getInflightStateLimits: { enumerable: true, get: () => _getInflightStateLimits, set: (value) => { _getInflightStateLimits = value; } },
  _readInflightStateMap: { enumerable: true, get: () => _readInflightStateMap, set: (value) => { _readInflightStateMap = value; } },
  _isStorageQuotaError: { enumerable: true, get: () => _isStorageQuotaError, set: (value) => { _isStorageQuotaError = value; } },
  _truncateInflightValue: { enumerable: true, get: () => _truncateInflightValue, set: (value) => { _truncateInflightValue = value; } },
  _compactInflightState: { enumerable: true, get: () => _compactInflightState, set: (value) => { _compactInflightState = value; } },
  _writeInflightStateMap: { enumerable: true, get: () => _writeInflightStateMap, set: (value) => { _writeInflightStateMap = value; } },
  saveInflightState: { enumerable: true, get: () => saveInflightState, set: (value) => { saveInflightState = value; } },
  loadInflightState: { enumerable: true, get: () => loadInflightState, set: (value) => { loadInflightState = value; } },
  clearInflightState: { enumerable: true, get: () => clearInflightState, set: (value) => { clearInflightState = value; } },
  _todosHash: { enumerable: true, get: () => _todosHash, set: (value) => { _todosHash = value; } },
  todoStatusKey: { enumerable: true, get: () => todoStatusKey, set: (value) => { todoStatusKey = value; } },
  todoStatusVisual: { enumerable: true, get: () => todoStatusVisual, set: (value) => { todoStatusVisual = value; } },
  renderTodoStatusIcon: { enumerable: true, get: () => renderTodoStatusIcon, set: (value) => { renderTodoStatusIcon = value; } },
  todoContent: { enumerable: true, get: () => todoContent, set: (value) => { todoContent = value; } },
  renderTodoEmptyState: { enumerable: true, get: () => renderTodoEmptyState, set: (value) => { renderTodoEmptyState = value; } },
  renderTodoRow: { enumerable: true, get: () => renderTodoRow, set: (value) => { renderTodoRow = value; } },
  renderTodoRows: { enumerable: true, get: () => renderTodoRows, set: (value) => { renderTodoRows = value; } },
  _todosPanelIsActive: { enumerable: true, get: () => _todosPanelIsActive, set: (value) => { _todosPanelIsActive = value; } },
  scheduleTodosRefresh: { enumerable: true, get: () => scheduleTodosRefresh, set: (value) => { scheduleTodosRefresh = value; } },
  _resetTodosRenderCache: { enumerable: true, get: () => _resetTodosRenderCache, set: (value) => { _resetTodosRenderCache = value; } },
  _hydrateTodosFromSession: { enumerable: true, get: () => _hydrateTodosFromSession, set: (value) => { _hydrateTodosFromSession = value; } },
  snapshotLiveTurnHtmlForSession: { enumerable: true, get: () => snapshotLiveTurnHtmlForSession, set: (value) => { snapshotLiveTurnHtmlForSession = value; } },
  _liveAssistantSegmentTextLength: { enumerable: true, get: () => _liveAssistantSegmentTextLength, set: (value) => { _liveAssistantSegmentTextLength = value; } },
  _mergeRestoredLiveAssistantSegment: { enumerable: true, get: () => _mergeRestoredLiveAssistantSegment, set: (value) => { _mergeRestoredLiveAssistantSegment = value; } },
  restoreLiveTurnHtmlForSession: { enumerable: true, get: () => restoreLiveTurnHtmlForSession, set: (value) => { restoreLiveTurnHtmlForSession = value; } },
  markInflight: { enumerable: true, get: () => markInflight, set: (value) => { markInflight = value; } },
  clearInflight: { enumerable: true, get: () => clearInflight, set: (value) => { clearInflight = value; } },
  showReconnectBanner: { enumerable: true, get: () => showReconnectBanner, set: (value) => { showReconnectBanner = value; } },
  dismissReconnect: { enumerable: true, get: () => dismissReconnect, set: (value) => { dismissReconnect = value; } },
  APP_DIALOG: { enumerable: true, get: () => APP_DIALOG },
  INFLIGHT_KEY: { enumerable: true, get: () => INFLIGHT_KEY },
  INFLIGHT_STATE_KEY: { enumerable: true, get: () => INFLIGHT_STATE_KEY },
  INFLIGHT_STATE_DEFAULT_LIMITS: { enumerable: true, get: () => INFLIGHT_STATE_DEFAULT_LIMITS },
  TODO_STATUS_RENDERING: { enumerable: true, get: () => TODO_STATUS_RENDERING },
  _appDialogBound: { enumerable: true, get: () => _appDialogBound, set: (value) => { _appDialogBound = value; } },
  _ttsSpeaking: { enumerable: true, get: () => _ttsSpeaking, set: (value) => { _ttsSpeaking = value; } },
  _ttsCurrentUtterance: { enumerable: true, get: () => _ttsCurrentUtterance, set: (value) => { _ttsCurrentUtterance = value; } },
  _ttsChunkQueue: { enumerable: true, get: () => _ttsChunkQueue, set: (value) => { _ttsChunkQueue = value; } },
  _ttsChunkIndex: { enumerable: true, get: () => _ttsChunkIndex, set: (value) => { _ttsChunkIndex = value; } },
  _ttsActiveBtn: { enumerable: true, get: () => _ttsActiveBtn, set: (value) => { _ttsActiveBtn = value; } },
  _playingEdgeAudio: { enumerable: true, get: () => _playingEdgeAudio, set: (value) => { _playingEdgeAudio = value; } },
  _ttsAudioCtx: { enumerable: true, get: () => _ttsAudioCtx, set: (value) => { _ttsAudioCtx = value; } },
  _todosLastRenderedHash: { enumerable: true, get: () => _todosLastRenderedHash, set: (value) => { _todosLastRenderedHash = value; } },
  _todosRenderRafId: { enumerable: true, get: () => _todosRenderRafId, set: (value) => { _todosRenderRafId = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
