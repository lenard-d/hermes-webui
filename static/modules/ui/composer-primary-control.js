import { _clearActivityElapsedTimer } from './activity-and-scroll.js';
import { renderTray } from './upload-tray.js';
import { updateQueueBadge } from './composer-queue.js';
import { _composerLockState, _compressionPlaceholderSaved, compatibilityBindings as composerStateBindings } from './composer-state.js';
import { isCompressionUiRunning } from './compression-ui.js';
import { syncModelChip } from './model-catalog.js';
import { _applyModelToDropdown } from './model-state.js';
import { $, S, _queueDrainSid, assistantDisplayName, queueSessionMessage, shiftQueuedSessionMessage, compatibilityBindings as stateBindings } from './state.js';
import { setStatus } from './toast-notifications.js';

function setComposerStatus(t){
  const el=$('composerStatus');
  if(!el)return;
  const statusHidden=!!(window._composerControlVisibility&&window._composerControlVisibility.hide_composer_status);
  if(statusHidden){
    el.style.display='none';
    el.textContent='';
    return;
  }
  if(!t){
    el.style.display='none';
    el.textContent='';
    return;
  }
  // Defensive reset: a stale hidden class should never block live status text.
  el.classList.remove('composer-control-hidden');
  el.removeAttribute('aria-hidden');
  el.textContent=t;
  el.style.display='';
}

function lockComposerForClarify(placeholderText){
  const input=$('msg');
  if(!input) return;
  // Save the current composer text as a server-side draft before locking,
  // so the user's draft is preserved if they switch sessions while a clarify
  // card is active (and survives page refresh / syncs across clients).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraftNow === 'function') {
    _saveComposerDraftNow(sid, input.value || '', S.pendingFiles ? [...S.pendingFiles] : []);
  }
  if(!_composerLockState){
    composerStateBindings._composerLockState={
      disabled: input.disabled,
      placeholder: input.placeholder,
    };
  }
  input.disabled=true;
  if(placeholderText) input.placeholder=placeholderText;
  updateSendBtn();
}

function unlockComposerForClarify(){
  const input=$('msg');
  if(!input) return;
  if(_composerLockState){
    input.disabled=!!_composerLockState.disabled;
    if(typeof _composerLockState.placeholder==='string'){
      input.placeholder=_composerLockState.placeholder;
    }
    composerStateBindings._composerLockState=null;
  }else{
    input.disabled=false;
  }
  updateSendBtn();
}

function _composerHasContent(){
  const msg=$('msg');
  return !!((msg&&msg.value.trim().length>0)||S.pendingFiles.length>0||(typeof window._hasPendingSelections==='function'&&window._hasPendingSelections()));
}

function _getExplicitBusyCommandAction(text){
  const trimmed=(text||'').trim();
  if(!trimmed.startsWith('/')) return null;
  const body=trimmed.slice(1);
  const name=(body.split(/\s+/)[0]||'').toLowerCase();
  const args=body.slice(name.length).trim();
  if(!args) return null;
  if(name==='queue') return 'queue';
  if(name==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(name==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return null;
}

function getComposerPrimaryAction(){
  const msg=$('msg');
  const hasContent=_composerHasContent();
  const locked=!!(msg&&msg.disabled);
  if(locked) return 'disabled';
  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  const isBusy=!!S.busy||compressionRunning;
  if(!isBusy) return hasContent?'send':'disabled';
  if(!hasContent){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'stop';
    if(compressionRunning) return 'queue';
    return 'disabled';
  }
  const explicitAction=_getExplicitBusyCommandAction(msg&&msg.value);
  if(explicitAction) return explicitAction;
  const defaultMessageMode=window._defaultMessageMode||'steer';
  if(defaultMessageMode==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(defaultMessageMode==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return 'queue';
}

function _applyBusyComposerPlaceholder(){
  const input=$('msg');
  if(!input) return;
  if(_compressionPlaceholderSaved!==null) return;
  if(input.disabled) return;
  if(_composerHasContent()) return;
  const idlePlaceholder='Message '+assistantDisplayName()+'\u2026';
  if(!window._showBusyPlaceholderHint||!S.busy){
    input.placeholder=idlePlaceholder;
    return;
  }
  const busyMode=window._defaultMessageMode||'steer';
  const busyPlaceholderKey=busyMode==='interrupt'
    ? 'composer_placeholder_busy_interrupt'
    : busyMode==='steer'
      ? 'composer_placeholder_busy_steer'
      : 'composer_placeholder_busy_queue';
  const busyPlaceholderFallback=busyMode==='interrupt'
    ? 'Enter = interrupt | /queue | /background | /steer'
    : busyMode==='steer'
      ? 'Enter = steer | /queue | /background | /interrupt'
      : 'Enter = queue | /interrupt | /background | /steer';
  input.placeholder=typeof t==='function'
    ? (t(busyPlaceholderKey)||busyPlaceholderFallback)
    : busyPlaceholderFallback;
}

function _setComposerPrimaryButtonIcon(btn,action){
  // Queue/interrupt/steer icons are inline Lucide SVGs (ISC):
  // https://lucide.dev/icons/
  const icons={
    send:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
    queue:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 5H3"/><path d="M16 12H3"/><path d="M9 19H3"/><path d="m16 16-3 3 3 3"/><path d="M21 5v12a2 2 0 0 1-2 2h-6"/></svg>',
    interrupt:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 4v16"/><path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"/></svg>',
    steer:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/></svg>',
    stop:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"></rect></svg>',
    disabled:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>'
  };
  const next=icons[action]||icons.send;
  if(btn.innerHTML!==next) btn.innerHTML=next;
}

function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn){
    if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
    return;
  }
  const action=getComposerPrimaryAction();
  btn.dataset.action=action;
  btn.classList.toggle('stop',action==='stop');
  btn.classList.toggle('queue',action==='queue');
  btn.classList.toggle('interrupt',action==='interrupt');
  btn.classList.toggle('steer',action==='steer');
  const _tt=(key,fb)=>{if(typeof t!=='function')return fb;const val=t(key);return val===key?fb:(val||fb);};
  let _btnTitle;
  if(action==='disabled'){
    const _dmsg=$('msg');
    if(_dmsg&&_dmsg.disabled) _btnTitle=_tt('composer_disabled_clarify','Respond to the clarification request');
    else _btnTitle=_tt('composer_disabled_empty','Type a message to send');
  }else if(action==='queue'&&typeof isCompressionUiRunning==='function'&&isCompressionUiRunning()){
    _btnTitle=_tt('composer_compression_will_queue','Type a message — it will queue and send after compression');
  }else{
    const _tmap={send:'Send message',queue:'Queue message',interrupt:'Interrupt and send',steer:'Steer current response',stop:'Stop generation'};
    _btnTitle=_tt('composer_'+action,_tmap[action]||'Send message');
  }
  btn.title=_btnTitle;
  btn.setAttribute('aria-label',_btnTitle);
  _setComposerPrimaryButtonIcon(btn,action);
  if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
  // Single primary action button: while busy/no-draft it becomes the red Stop
  // action; while busy with a draft it reflects queue/interrupt/steer.
  btn.style.display='';
  btn.disabled=action==='disabled';
  if(action!=='disabled'&&!btn.classList.contains('visible')){
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(action==='disabled'){
    btn.classList.remove('visible');
  }
}

async function handleComposerPrimaryAction(){
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  const action=typeof getComposerPrimaryAction==='function'?getComposerPrimaryAction():'send';
  if(action==='disabled') return;
  if(action==='stop'){
    if(typeof cancelStream==='function') await cancelStream('composer-stop');
    return;
  }
  await send();
}

function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
    setStatus('');
    setComposerStatus('');
    const sid=_queueDrainSid||(S.session&&S.session.session_id);
    stateBindings._queueDrainSid=null;
    updateQueueBadge(sid);
    // Drain one queued message for the finished session after UI settles
    const _isViewedSid=!S.session||sid===S.session.session_id;
    const next=sid&&_isViewedSid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        // Guard: if the user switched away from the drain session during
        // the 120ms settle window, the queued message must NOT go to the
        // wrong chat.  Put it back into the original session's queue and
        // skip sending — it will drain when the user returns to that session
        // or when its next stream completes while it is the active view.
        if(S.session&&S.session.session_id!==sid){
          queueSessionMessage(sid,next);
          updateQueueBadge(sid);
          return;
        }
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
        }
        if(next.model_provider&&S.session) S.session.model_provider=next.model_provider;
        if(next.model&&S.session){
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'),S.session.model_provider||null);
          if(typeof syncModelChip==='function') syncModelChip();
        }
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
}

if(typeof document!=='undefined'&&typeof document.addEventListener==='function'){
  document.addEventListener('hermes-composer-content-change',updateSendBtn);
}

export { setComposerStatus, lockComposerForClarify, unlockComposerForClarify, _composerHasContent, _getExplicitBusyCommandAction, getComposerPrimaryAction, _applyBusyComposerPlaceholder, _setComposerPrimaryButtonIcon, updateSendBtn, setBusy, handleComposerPrimaryAction };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  setComposerStatus: { enumerable: true, get: () => setComposerStatus, set: (value) => { setComposerStatus = value; } },
  lockComposerForClarify: { enumerable: true, get: () => lockComposerForClarify, set: (value) => { lockComposerForClarify = value; } },
  unlockComposerForClarify: { enumerable: true, get: () => unlockComposerForClarify, set: (value) => { unlockComposerForClarify = value; } },
  _composerHasContent: { enumerable: true, get: () => _composerHasContent, set: (value) => { _composerHasContent = value; } },
  _getExplicitBusyCommandAction: { enumerable: true, get: () => _getExplicitBusyCommandAction, set: (value) => { _getExplicitBusyCommandAction = value; } },
  getComposerPrimaryAction: { enumerable: true, get: () => getComposerPrimaryAction, set: (value) => { getComposerPrimaryAction = value; } },
  _applyBusyComposerPlaceholder: { enumerable: true, get: () => _applyBusyComposerPlaceholder, set: (value) => { _applyBusyComposerPlaceholder = value; } },
  _setComposerPrimaryButtonIcon: { enumerable: true, get: () => _setComposerPrimaryButtonIcon, set: (value) => { _setComposerPrimaryButtonIcon = value; } },
  updateSendBtn: { enumerable: true, get: () => updateSendBtn, set: (value) => { updateSendBtn = value; } },
  setBusy: { enumerable: true, get: () => setBusy, set: (value) => { setBusy = value; } },
  handleComposerPrimaryAction: { enumerable: true, get: () => handleComposerPrimaryAction, set: (value) => { handleComposerPrimaryAction = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
