import { showToast } from './composer.js';

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

export {
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
  _ttsSpeaking,
  _ttsCurrentUtterance,
  _ttsChunkQueue,
  _ttsChunkIndex,
  _ttsActiveBtn,
  _playingEdgeAudio,
  _ttsAudioCtx,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _stripForTTS: { enumerable: true, get: () => _stripForTTS, set: value => { _stripForTTS = value; } },
  _splitForTTS: { enumerable: true, get: () => _splitForTTS, set: value => { _splitForTTS = value; } },
  _buildBrowserUtterance: { enumerable: true, get: () => _buildBrowserUtterance, set: value => { _buildBrowserUtterance = value; } },
  _playEdgeTtsChunked: { enumerable: true, get: () => _playEdgeTtsChunked, set: value => { _playEdgeTtsChunked = value; } },
  speakMessage: { enumerable: true, get: () => speakMessage, set: value => { speakMessage = value; } },
  _playElevenLabsTts: { enumerable: true, get: () => _playElevenLabsTts, set: value => { _playElevenLabsTts = value; } },
  _playOpenaiTts: { enumerable: true, get: () => _playOpenaiTts, set: value => { _playOpenaiTts = value; } },
  _getTtsAudioCtx: { enumerable: true, get: () => _getTtsAudioCtx, set: value => { _getTtsAudioCtx = value; } },
  _playAudioBuf: { enumerable: true, get: () => _playAudioBuf, set: value => { _playAudioBuf = value; } },
  stopTTS: { enumerable: true, get: () => stopTTS, set: value => { stopTTS = value; } },
  autoReadLastAssistant: { enumerable: true, get: () => autoReadLastAssistant, set: value => { autoReadLastAssistant = value; } },
  _ttsSpeaking: { enumerable: true, get: () => _ttsSpeaking, set: value => { _ttsSpeaking = value; } },
  _ttsCurrentUtterance: { enumerable: true, get: () => _ttsCurrentUtterance, set: value => { _ttsCurrentUtterance = value; } },
  _ttsChunkQueue: { enumerable: true, get: () => _ttsChunkQueue, set: value => { _ttsChunkQueue = value; } },
  _ttsChunkIndex: { enumerable: true, get: () => _ttsChunkIndex, set: value => { _ttsChunkIndex = value; } },
  _ttsActiveBtn: { enumerable: true, get: () => _ttsActiveBtn, set: value => { _ttsActiveBtn = value; } },
  _playingEdgeAudio: { enumerable: true, get: () => _playingEdgeAudio, set: value => { _playingEdgeAudio = value; } },
  _ttsAudioCtx: { enumerable: true, get: () => _ttsAudioCtx, set: value => { _ttsAudioCtx = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
