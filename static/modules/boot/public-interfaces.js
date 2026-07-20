// ── Default message mode eager default (#5167 / #5145) ──────────────────────
// The Default message mode preference (queue/interrupt/steer) is read on the
// send path via `window._defaultMessageMode||'steer'`. The authoritative value
// only arrives once the async boot IIFE below resolves the `/api/settings`
// fetch. Without an eager value, every send during that boot window silently
// falls back, ignoring a saved 'queue'/'interrupt' preference (worse on
// slow/contended environments like WSL2, see #5132). Mirror the resolved value
// into localStorage — the same synchronous-source pattern used by hermes-lang /
// hermes-theme — so the very first send after a reload honors the saved choice.
const _DEFAULT_MESSAGE_MODES=['queue','interrupt','steer'];
// Legacy localStorage key (pre-#5145 rename); read it as a fallback so an
// existing user's persisted busy-input-mode preference survives the rename.
const _LEGACY_DEFAULT_MESSAGE_MODE_KEY='hermes-busy-input-mode';
const _DEFAULT_MESSAGE_MODE_KEY='hermes-default-message-mode';
function _normalizeDefaultMessageMode(mode){
  return _DEFAULT_MESSAGE_MODES.includes(mode)?mode:'steer';
}
function _persistDefaultMessageMode(mode){
  const m=_normalizeDefaultMessageMode(mode);
  try{localStorage.setItem(_DEFAULT_MESSAGE_MODE_KEY,m);}catch(_){}
  return m;
}
function _readPersistedDefaultMessageMode(){
  let stored=null;
  try{
    // Prefer the new key; fall back to the legacy key so a pre-rename
    // preference is honored until the next explicit save rewrites the new key.
    stored=localStorage.getItem(_DEFAULT_MESSAGE_MODE_KEY);
    if(stored===null||stored===undefined) stored=localStorage.getItem(_LEGACY_DEFAULT_MESSAGE_MODE_KEY);
  }catch(_){}
  return _normalizeDefaultMessageMode(stored);
}
// Eager default set BEFORE the async settings fetch resolves so first sends in
// the boot window honor the persisted preference instead of the raw default.
const eagerDefaultMessageMode=_readPersistedDefaultMessageMode();

// ── Extension TTS-engine registry (registerHermesTtsEngine) ──────────────────
// Defined at MODULE scope (not inside the voice-mode IIFE below) so the public
// API exists even on browsers without SpeechRecognition / speechSynthesis — an
// extension can register a TTS engine regardless of STT/browser-TTS support.
// Lets a trusted local extension contribute a TTS engine that appears in the
// Settings -> TTS Engine dropdown and is used by BOTH playback paths (voice-mode
// auto-read and the per-message Listen button). The extension provides an async
// synthesize(text, opts) that returns audio bytes (ArrayBuffer or Blob); core
// handles selection, the dropdown option, and playback. Mirrors registerHermesSkin.
//
//   window.registerHermesTtsEngine({
//     id: 'voicevox',            // [a-z0-9_-], not a built-in (browser/edge/elevenlabs/openai)
//     label: 'VOICEVOX (local)',
//     synthesize(text, opts) { return Promise<ArrayBuffer|Blob>; }
//   }) -> true on success, false if rejected
var _HERMES_TTS_ENGINES = Object.create(null);
var _HERMES_TTS_RESERVED = { browser:1, edge:1, elevenlabs:1, openai:1 };
function _hermesTtsValidId(id){ return typeof id==='string' && /^[a-z0-9][a-z0-9_-]{0,31}$/.test(id); }
function _hermesAddTtsOption(id, label){
  var sel=document.getElementById('settingsTtsEngine');
  if(!sel) return;
  if(sel.querySelector('option[value="'+id+'"]')) return;
  var opt=document.createElement('option');
  opt.value=id;
  opt.textContent=label;   // textContent — never innerHTML (no injection)
  sel.appendChild(opt);
}
function registerHermesTtsEngine(desc){
  try{
    if(!desc||typeof desc!=='object') return false;
    var id=String(desc.id||'').toLowerCase();
    if(!_hermesTtsValidId(id)) return false;
    if(_HERMES_TTS_RESERVED[id]) return false;          // can't shadow a built-in
    if(typeof desc.synthesize!=='function') return false;
    var label=(typeof desc.label==='string' && desc.label.trim()) ? desc.label.trim().slice(0,48) : id;
    _HERMES_TTS_ENGINES[id]={ id:id, label:label, synthesize:desc.synthesize };
    _hermesAddTtsOption(id, label);
    return true;
  }catch(_){ return false; }
}
function _hermesTtsIsRegistered(id){ return !!_HERMES_TTS_ENGINES[id]; }
// List registered engines (for the settings panel to re-add options on render).
function _hermesTtsEngineOptions(){
  return Object.keys(_HERMES_TTS_ENGINES).map(function(k){
    return { id:_HERMES_TTS_ENGINES[k].id, label:_HERMES_TTS_ENGINES[k].label };
  });
}
// Returns a Promise<ArrayBuffer> or null if the engine isn't registered.
function _hermesTtsSynth(id, text, opts){
  var eng=_HERMES_TTS_ENGINES[id];
  if(!eng) return null;
  return Promise.resolve()
    .then(function(){ return eng.synthesize(text, opts||{}); })
    .then(function(out){
      if(!out) throw new Error('empty TTS result');
      if(out instanceof ArrayBuffer) return out;
      if(typeof Blob!=='undefined' && out instanceof Blob) return out.arrayBuffer();
      if(out.buffer instanceof ArrayBuffer) return out.buffer;   // typed array
      throw new Error('TTS engine returned an unsupported type');
    });
}

// ── Session-open hook (for extensions) ────────────────────────────────────
var _HERMES_SESSION_OPEN_HANDLERS=[];
function registerHermesSessionOpenHandler(fn){
  if(typeof fn!=='function') return false;
  if(_HERMES_SESSION_OPEN_HANDLERS.indexOf(fn)>=0) return false;
  _HERMES_SESSION_OPEN_HANDLERS.push(fn);
  return true;
}
function _hermesNotifySessionOpen(sid, data, opts){
  opts=opts||{};
  for(var i=0;i<_HERMES_SESSION_OPEN_HANDLERS.length;i++){
    try{
      var result=_HERMES_SESSION_OPEN_HANDLERS[i](sid, data, opts);
      if(opts.preload===true && result&&result.cancel===true) return {cancel:true};
    }catch(_){}
  }
  return {};
}

// ── Transcript renderer (for extensions) ───────────────────────────────────
function renderTranscript(container, messages, opts){
  if(!container||!Array.isArray(messages)) return container;
  opts=opts||{};
  container.innerHTML='';
  var md=window.renderMd||null;
  for(var i=0;i<messages.length;i++){
    var msg=messages[i];
    if(!msg||!msg.role||msg.role==='tool') continue;
    var content;
    if(typeof msg.content==='string'){
      content=msg.content;
    }else if(msg.content==null){
      content='';
    }else if(Array.isArray(msg.content)){
      // Multi-part content (OpenAI/Anthropic API style) — concatenate text parts.
      content=msg.content.map(function(p){return (p&&typeof p.text==='string')?p.text:''}).join('');
    }else{
      content=String(msg.content);
    }
    if(!content&&opts.skipEmpty) continue;
    var row=document.createElement('div');
    row.className='msg-row';
    row.setAttribute('data-role',msg.role);
    var body=document.createElement('div');
    body.className='msg-body';
    try{
      if(md){
        var html=md(content);
        if(html!=null){body.innerHTML=html}else{body.textContent=content}
      }else{
        body.textContent=content;
      }
    }catch(_){body.textContent=content}
    row.appendChild(body);
    container.appendChild(row);
  }
  if(typeof _rehydrateTransparentStreamDom==='function'){
    try{_rehydrateTransparentStreamDom(container);}catch(_){}
  }
  return container;
}

export {
  _hermesNotifySessionOpen,
  _hermesTtsEngineOptions,
  _hermesTtsIsRegistered,
  _hermesTtsSynth,
  _normalizeDefaultMessageMode,
  _persistDefaultMessageMode,
  _readPersistedDefaultMessageMode,
  eagerDefaultMessageMode,
  registerHermesSessionOpenHandler,
  registerHermesTtsEngine,
  renderTranscript,
};
