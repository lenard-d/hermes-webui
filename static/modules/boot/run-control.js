import {showServerStopped} from './server-lifecycle.js';
// Early boot initialization that must run before any other code.
// These run during script evaluation to handle server-stopped state
// and cross-tab shutdown broadcasts as early as possible.
(function(){
  // Clear stale stop-server flag on successful page load (server is reachable)
  try{localStorage.removeItem('hermes-webui-server-stopped');}catch(_){}
  // Listen for shutdown broadcast from other tabs
  try {
    var _stopChan = new BroadcastChannel('hermes-webui-shutdown');
    _stopChan.onmessage = function() { showServerStopped(); };
  } catch(_) {}
})();

// cancelStream: stop the active chat stream.
// See docs/rfcs/webui-run-state-consistency-contract.md (Invariants #2, #4)
// for the owner-aware + terminal-settle rationale.
async function cancelStream(reason){
  const sid = S.session && S.session.session_id;
  const streamId = S.activeStreamId;
  if(!streamId) return;
  // Interrupt provenance: log WHY the active run is being cancelled so operators
  // can tell an explicit Stop / interrupt from any other trigger when they see a
  // SIGINT/exit-code-130 in the backend logs. Only explicit user paths reach
  // this function (Stop button, /stop, /interrupt, busy-interrupt); passive
  // lifecycle events — session switch, tab hide, page unload — tear down the
  // LOCAL SSE transport via closeLiveStream() and never call /api/chat/cancel,
  // so they never interrupt the backend agent/tool run. (#5345)
  const _reason = reason || 'explicit-cancel';
  if(typeof console !== 'undefined' && console.info){
    console.info('[stream] cancel requested', {reason:_reason, streamId, sessionId:sid});
  }
  let respBody=null;
  try{
    const r=await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
    try{respBody=await r.json();}catch(_){}
  }catch(e){
    if(typeof console !== 'undefined' && console.warn){
      console.warn('cancelStream: /api/chat/cancel request failed', e);
    }
  }
  // Active-session cancel should not tear down the current SSE transport before
  // the backend emits its terminal event; do that only for stale owner paths
  // where the user moved on to a different stream before this request
  // completed.
  if(sid && S.activeStreamId !== streamId && typeof closeLiveStream==='function'){
    closeLiveStream(sid, streamId);
  }
  // Owner guard: if the backend accepted the active-session cancel, leave
  // the current SSE transport and owner state intact so the terminal
  // `cancel` event can clear INFLIGHT, render "Task cancelled", and refresh
  // the sidebar. Only clear locally when the backend says there is no active
  // stream left to settle.
  if(respBody && respBody.cancelled===false && S.activeStreamId===streamId){
    S.activeStreamId=null;
    setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    else setStatus('');
    // /api/chat/cancel only exposes `cancelled:bool`, so we cannot
    // distinguish reasons — keep the toast generic and short.
    if(typeof showToast==='function') showToast('Stream is no longer active',2000);
  }
}

async function cancelSessionStream(session){
  const streamId = session&&session.active_stream_id;
  const sid = session&&session.session_id;
  if(!streamId||!sid) return;
  // Explicit sidebar "Stop response" — log provenance for the same reason as
  // cancelStream(). (#5345)
  if(typeof console !== 'undefined' && console.info){
    console.info('[stream] cancel requested', {reason:'sidebar-stop', streamId, sessionId:sid});
  }
  try{
    await fetch(new URL(`api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{credentials:'include'});
  }catch(e){/* close local stream; keep UI state honest below */}
  if(typeof closeLiveStream==='function') closeLiveStream(sid, streamId);
  session.active_stream_id=null;
  delete INFLIGHT[sid];
  clearInflightState(sid);
  if(S.session&&S.session.session_id===sid){
    S.activeStreamId=null;
    if(S.session) S.session.active_stream_id=null;
    clearInflight();
    setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    else setStatus('');
  }
  if(typeof _approvalSessionId!=='undefined' && _approvalSessionId===sid){
    stopApprovalPolling();
    hideApprovalCard(true);
  }
  if(typeof _clarifySessionId!=='undefined' && _clarifySessionId===sid){
    stopClarifyPolling();
    hideClarifyCard(true, 'cancelled');
  }
  if(typeof renderSessionList==='function') renderSessionList();
}

async function _savedSessionShouldStaySidebarOnly(sid){
  const state = await _savedSessionSidebarOnlyState(sid);
  return !!(state&&state.sidebarOnly);
}

async function _savedSessionSidebarOnlyState(sid){
  if(!sid) return false;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    const session = data&&data.session;
    const archived = !!(session&&session.archived);
    const running = !!(session&&(session.active_stream_id||session.pending_user_message));
    return {sidebarOnly:archived||running, archived};
  }catch(e){
    return null;
  }
}

export {cancelSessionStream,cancelStream,_savedSessionSidebarOnlyState};
