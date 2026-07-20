import { showToast } from './toast-notifications.js';
import { syncTopbar } from './topbar-presentation.js';
import { $, S, esc } from './state.js';

function _compressionSessionLock(){
  return window._compressionLockSid||null;
}
function _setCompressionSessionLock(sid){
  window._compressionLockSid=sid||null;
}
function _compressionRecoveryHtml(recovery, sessionId){
  if(!recovery||typeof recovery!=='object') return '';
  if(String(recovery.terminal_state||'')!=='compression_exhausted') return '';
  const action=String(recovery.recommended_action||'');
  if(action!=='start_focused_continuation') return '';
  const sid=String(recovery.source_session_id||sessionId||'');
  const title=String(recovery.title||'Context compression exhausted');
  const summary=String(recovery.summary||'Start a focused continuation, then describe the next narrow task.');
  const actionLabel=String(recovery.action_label||'Start focused continuation');
  const icon=(typeof li==='function')?li('git-branch',14):'';
  return `<div class="compression-recovery-card" data-compression-recovery-card="1">
    <div class="compression-recovery-copy">
      <div class="compression-recovery-title">${esc(title)}</div>
      <div class="compression-recovery-summary">${esc(summary)}</div>
    </div>
    <button class="compression-recovery-action" type="button" data-recovery-session-id="${esc(sid)}" onclick="startCompressionRecovery(this);event.stopPropagation()">${icon}<span>${esc(actionLabel)}</span></button>
  </div>`;
}

function _activeCompressionRecoveryPayload(){
  if(!S||!S.session) return null;
  const recovery=S.session.compression_recovery;
  if(recovery&&typeof recovery==='object'&&String(recovery.terminal_state||'')==='compression_exhausted') return recovery;
  // A cleared session-level recovery payload is authoritative. Only scan
  // message metadata for older sessions that never exposed this field.
  if(Object.prototype.hasOwnProperty.call(S.session,'compression_recovery')) return null;
  const messages=Array.isArray(S.messages)?S.messages:[];
  for(let i=messages.length-1;i>=0;i--){
    const msg=messages[i];
    const msgRecovery=msg&&msg._compressionRecovery;
    if(msgRecovery&&typeof msgRecovery==='object'&&String(msgRecovery.terminal_state||'')==='compression_exhausted') return msgRecovery;
  }
  return null;
}

function isGenericCompressionContinuationIntent(text){
  const raw=String(text||'').trim().toLowerCase();
  if(!raw) return false;
  const normalized=raw.replace(/[^\p{L}\p{N}]+/gu,' ').trim();
  const generic=new Set(['continue','continue please','go on','keep going','resume','proceed','carry on','继续','继续吧','接着','接着做','继续做','继续执行']);
  if(generic.has(normalized)) return true;
  const parts=normalized.split(/\s+/).filter(Boolean);
  return !!parts.length&&parts.length<=2&&parts.every(part=>generic.has(part));
}

function shouldInterceptCompressionRecoveryContinuation(text, files){
  const hasFiles=Array.isArray(files)&&files.length>0;
  if(hasFiles||!isGenericCompressionContinuationIntent(text)) return false;
  const recovery=_activeCompressionRecoveryPayload();
  return !!(recovery&&String(recovery.recommended_action||'')==='start_focused_continuation');
}

function showCompressionRecoveryContinuationHint(){
  const card=document.querySelector('[data-compression-recovery-card="1"]');
  if(card&&typeof card.scrollIntoView==='function'){
    try{card.scrollIntoView({block:'center',behavior:'smooth'});}catch(_){card.scrollIntoView();}
    const btn=card.querySelector('.compression-recovery-action');
    if(btn&&typeof btn.focus==='function') setTimeout(()=>btn.focus(),120);
  }
  if(typeof showToast==='function') showToast('This session exhausted context compression. Start a focused continuation, then describe the next narrow task.',4500,'warning');
}

async function startCompressionRecovery(btn){
  const sourceSid=String((btn&&btn.dataset&&btn.dataset.recoverySessionId)||(S.session&&S.session.session_id)||'').trim();
  if(!sourceSid) return;
  let retiredRecoveryCard=false;
  if(btn){btn.disabled=true;btn.classList.add('loading');}
  try{
    const data=await api('/api/session/compression-recovery/start',{method:'POST',body:JSON.stringify({session_id:sourceSid})});
    const sid=data&&data.session&&data.session.session_id;
    if(!sid) throw new Error('Compression recovery did not return a session.');
    try{localStorage.setItem('hermes-webui-session',sid);}catch(_){}
    if(typeof loadSession==='function') await loadSession(sid,{preserveActiveInput:false});
    else if(data.session){S.session=data.session;S.messages=data.session.messages||[];syncTopbar();renderMessages();}
    if(typeof renderSessionList==='function') await renderSessionList();
    if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(sid);
    if(typeof showToast==='function') showToast((data&&data.message)||'Started focused continuation.',3000,'success');
    const composer=$('msg');
    if(composer&&typeof composer.focus==='function') composer.focus();
  }catch(e){
    // A 409 means this session no longer has an active recovery action (the
    // session already moved on — e.g. a substantive prompt cleared it). The
    // persisted card in the transcript is stale, so retire it and show a neutral
    // note instead of a raw error. The server is authoritative on availability.
    if(e&&e.status===409){
      const staleCard=(btn&&btn.closest&&btn.closest('.compression-recovery-card'))
        ||document.querySelector('[data-compression-recovery-card="1"]');
      if(staleCard){
        staleCard.setAttribute('data-compression-recovery-consumed','1');
        const staleBtn=staleCard.querySelector('.compression-recovery-action');
        if(staleBtn){staleBtn.disabled=true;staleBtn.classList.remove('loading');}
        retiredRecoveryCard=true;
      }
      if(typeof showToast==='function') showToast('This conversation already moved on — the focused-continuation action is no longer available.',4000,'info');
      return;
    }
    if(typeof showToast==='function') showToast('Compression recovery failed: '+(e&&e.message||e),5000,'error');
  }finally{
    // Do NOT re-enable a button we deliberately retired in the 409 branch.
    if(btn){if(!retiredRecoveryCard) btn.disabled=false;btn.classList.remove('loading');}
  }
}

export {
  _compressionSessionLock,
  _setCompressionSessionLock,
  _compressionRecoveryHtml,
  _activeCompressionRecoveryPayload,
  isGenericCompressionContinuationIntent,
  shouldInterceptCompressionRecoveryContinuation,
  showCompressionRecoveryContinuationHint,
  startCompressionRecovery,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _compressionSessionLock: { enumerable: true, get: () => _compressionSessionLock, set: value => { _compressionSessionLock = value; } },
  _setCompressionSessionLock: { enumerable: true, get: () => _setCompressionSessionLock, set: value => { _setCompressionSessionLock = value; } },
  _compressionRecoveryHtml: { enumerable: true, get: () => _compressionRecoveryHtml, set: value => { _compressionRecoveryHtml = value; } },
  _activeCompressionRecoveryPayload: { enumerable: true, get: () => _activeCompressionRecoveryPayload, set: value => { _activeCompressionRecoveryPayload = value; } },
  isGenericCompressionContinuationIntent: { enumerable: true, get: () => isGenericCompressionContinuationIntent, set: value => { isGenericCompressionContinuationIntent = value; } },
  shouldInterceptCompressionRecoveryContinuation: { enumerable: true, get: () => shouldInterceptCompressionRecoveryContinuation, set: value => { shouldInterceptCompressionRecoveryContinuation = value; } },
  showCompressionRecoveryContinuationHint: { enumerable: true, get: () => showCompressionRecoveryContinuationHint, set: value => { showCompressionRecoveryContinuationHint = value; } },
  startCompressionRecovery: { enumerable: true, get: () => startCompressionRecovery, set: value => { startCompressionRecovery = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
