import { _resumeSessionStreamAfterLiveChat } from './session-events.js';

export const LIVE_STREAMS={};
export const _STREAM_NOTIFICATION_BACKGROUND={};

// #4416: track whether the tab was hidden at ANY point during a live stream, so
// the response-complete notification fires for a backgrounded tab even when
// Chromium throttles the background-tab SSE and delivers the `done` event LATE
// (after the user returns, when document.hidden already reads false). Each entry
// is STREAM-OWNED ({streamId, wasHidden}) so a stale entry left by a non-`done`
// terminal path (apperror/cancel/stream-error/reconnect-no-active) can never be
// mis-attributed to a later stream for the same session id — a reconnect only
// keeps the prior state when the streamId matches. One idempotent
// visibilitychange listener (never leaks) flips wasHidden on all active entries.
export const _STREAM_WAS_HIDDEN={};
let _streamHiddenTrackerBound=false;
export function _bindStreamHiddenTracker(){
  if(_streamHiddenTrackerBound||typeof document==='undefined'||typeof document.addEventListener!=='function') return;
  _streamHiddenTrackerBound=true;
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){ for(const k in _STREAM_WAS_HIDDEN){ const e=_STREAM_WAS_HIDDEN[k]; if(e) e.wasHidden=true; } }
  });
}
export function _clearStreamHidden(sid, streamId){
  // Clear only when we own the current stream's entry (or unconditionally when
  // streamId is omitted). Prevents a terminal path for an old stream from wiping
  // a newer stream's tracker.
  if(!sid) return;
  const e=_STREAM_WAS_HIDDEN[sid];
  if(!e) return;
  if(streamId&&e.streamId&&e.streamId!==streamId) return;
  delete _STREAM_WAS_HIDDEN[sid];
}
export function _clearStreamNotificationBackground(sid, streamId){
  if(!sid) return;
  const e=_STREAM_NOTIFICATION_BACKGROUND[sid];
  if(!e) return;
  if(streamId&&e.streamId&&e.streamId!==streamId) return;
  delete _STREAM_NOTIFICATION_BACKGROUND[sid];
}
export function _shouldForceCompletionNotification(sid, streamId){
  const hiddenEntry=_STREAM_WAS_HIDDEN[sid];
  const backgroundEntry=_STREAM_NOTIFICATION_BACKGROUND[sid];
  const wasHidden=!!(hiddenEntry&&hiddenEntry.wasHidden);
  const wasBackgrounded=!!(backgroundEntry&&backgroundEntry.wasBackgrounded);
  _clearStreamHidden(sid, streamId);
  _clearStreamNotificationBackground(sid, streamId);
  return wasHidden||wasBackgrounded;
}

export function closeLiveStream(sessionId, streamId, source){
  const live=LIVE_STREAMS[sessionId];
  if(!live) return;
  if(streamId&&live.streamId!==streamId) return;
  if(source&&live.source!==source) return;
  // Snapshot the current live-turn DOM BEFORE tearing the stream down. The
  // per-event snapshot (snapshotLiveTurn) only fires on content/tool_complete
  // SSE events, so switching away during a quiet window (mid tool-exec, silent
  // thinking) would leave a stale-or-absent snapshot — on switch-back
  // restoreLiveTurnHtmlForSession() then fails and loadSession()'s fallback
  // rebuilds with an EMPTY appendThinking(), permanently losing the streamed
  // thinking/tool content (only the elapsed clock survives). Capturing here
  // guarantees switch-back restores the exact state shown at switch-away. (#3668)
  if(typeof snapshotLiveTurnHtmlForSession==='function') snapshotLiveTurnHtmlForSession(sessionId);
  // Stop the live footer timer/status for the pane that is being detached; the
  // reattach path will rebuild it from INFLIGHT/server state if the user returns.
  if(typeof _clearLiveRunStatusTimer==='function') _clearLiveRunStatusTimer(sessionId);
  if(typeof hideLiveRunStatus==='function') hideLiveRunStatus(sessionId);
  try{if(live.source&&live.source.readyState!==2)live.source.close();}catch(_){ }
  delete LIVE_STREAMS[sessionId];
  _resumeSessionStreamAfterLiveChat(sessionId);
  // closeLiveStream() is called during session-switch teardown for any session
  // the user is no longer viewing. The stream is still active on the server,
  // so mark the in-memory INFLIGHT entry for reattach — otherwise
  // loadSession() returning to this session skips the reattach branch
  // (`INFLIGHT.reattach` was only set by the storage-load path) and the SSE
  // is never reopened. The user then sees no streamed tokens until the LLM
  // finishes and a metadata refresh swaps in the final reply.
  // If the stream is terminating cleanly, _clearOwnerInflightState() has
  // already deleted INFLIGHT[sessionId], so this is a safe no-op.
  if(INFLIGHT[sessionId]){
    INFLIGHT[sessionId].reattach=true;
    // The browser-side INFLIGHT snapshot is only a compact tail cache. After a
    // session switch it cannot be treated as the full live turn; rebuild from
    // the durable run journal instead so earlier prose/tool rows are not lost.
    INFLIGHT[sessionId].journalReplayFromStart=true;
    if(typeof saveInflightState==='function'){
      saveInflightState(sessionId,{
        streamId:live.streamId||streamId||null,
        messages:INFLIGHT[sessionId].messages||[],
        uploaded:INFLIGHT[sessionId].uploaded||[],
        toolCalls:INFLIGHT[sessionId].toolCalls||[],
        lastAssistantText:INFLIGHT[sessionId].lastAssistantText||'',
        lastReasoningText:INFLIGHT[sessionId].lastReasoningText||'',
        lastRunJournalSeq:INFLIGHT[sessionId].lastRunJournalSeq||0,
        lastRunJournalEventId:INFLIGHT[sessionId].lastRunJournalEventId||'',
        journalReplayFromStart:true,
        currentActivityBurstId:INFLIGHT[sessionId].currentActivityBurstId||0,
        currentLiveSegmentSeq:INFLIGHT[sessionId].currentLiveSegmentSeq||0,
        activityBurstAnchors:Array.isArray(INFLIGHT[sessionId].activityBurstAnchors)?INFLIGHT[sessionId].activityBurstAnchors:[],
      });
    }
  }
}

export function closeOtherLiveStreams(activeSid){
  // Keep the live token SSE connection scoped to the conversation pane the user
  // is actually viewing. Background sessions still show running/finished state
  // through the session list and can reattach when selected, but they should not
  // keep one EventSource each and exhaust the browser connection pool (#2313).
  for(const sid of Object.keys(LIVE_STREAMS)){
    if(sid!==activeSid) closeLiveStream(sid);
  }
}
