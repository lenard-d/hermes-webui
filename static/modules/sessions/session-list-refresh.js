import { sessionStateStoreBindings as sessionStateBindings } from './session-state-store.js';
import { loadSession } from './lifecycle.js';
import { _isExternalSession } from './message-loading.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { renderSessionList } from './session-list-loader.js';

const _streamingPollMs = 30000;
const _sessionTimeRefreshMs = 60000;
// #3107: the active-session "is it externally updated?" poll used to fire
// every 5 s. On long sessions this caused visible scroll jitter and a
// noticeable network/CPU floor because the SSE session-events stream
// already pushes invalidations in real time; this poll exists only as a
// fallback for the case where SSE is broken/unavailable. Bump to 30 s
// to keep the safety net without turning it into a primary refresh path.
const _activeSessionExternalRefreshMs = 30000;
let _streamingPollTimer = null;
let _sessionTimeRefreshTimer = null;
let _streamingPollVisibilityHandler = null;
let _sessionTimeRefreshVisibilityHandler = null;
let _activeSessionExternalRefreshTimer = null;
let _activeSessionExternalRefreshInFlight = false;
let _deferredActiveSessionExternalRefreshReason = '';
let _sessionListRefreshInFlight = false;
let _sessionListRefreshPendingRequest = null;

function _mergeSessionListRefreshOptions(prev, next){
  const merged = {...(prev||{}), ...(next||{})};
  if((prev&&prev.force===true)||(next&&next.force===true)) merged.force = true;
  if((prev&&prev.refreshActive===true)||(next&&next.refreshActive===true)) merged.refreshActive = true;
  return merged;
}

function startStreamingPoll(){
  if(_streamingPollTimer) return;
  _streamingPollTimer = setInterval(() => {
    // Skip while the tab is hidden: this poll fetches /api/sessions and rebuilds
    // the sidebar, work the user cannot see. The visibilitychange handler below
    // brings the list current the moment the tab is shown again, so no update is
    // lost — the background tab just stops burning network + DOM churn.
    if(typeof document !== 'undefined' && document.hidden) return;
    void renderSessionList({deferWhileInteracting:true});
  }, _streamingPollMs);
  if(typeof document !== 'undefined' && !_streamingPollVisibilityHandler){
    _streamingPollVisibilityHandler = () => {
      if(!document.hidden) void renderSessionList({deferWhileInteracting:true});
    };
    document.addEventListener('visibilitychange', _streamingPollVisibilityHandler);
  }
}

function stopStreamingPoll(){
  if(_streamingPollVisibilityHandler && typeof document !== 'undefined'){
    document.removeEventListener('visibilitychange', _streamingPollVisibilityHandler);
    _streamingPollVisibilityHandler = null;
  }
  if(!_streamingPollTimer) return;
  clearInterval(_streamingPollTimer);
  _streamingPollTimer = null;
}

function ensureSessionTimeRefreshPoll(){
  if(_sessionTimeRefreshTimer) return;
  _sessionTimeRefreshTimer = setInterval(() => {
    // Relative-time labels only matter when visible; the visibilitychange
    // handler below refreshes timestamps immediately when the tab is shown.
    if(typeof document !== 'undefined' && document.hidden) return;
    renderSessionListFromCache();
  }, _sessionTimeRefreshMs);
  if(typeof document !== 'undefined' && !_sessionTimeRefreshVisibilityHandler){
    _sessionTimeRefreshVisibilityHandler = () => {
      if(!document.hidden) renderSessionListFromCache();
    };
    document.addEventListener('visibilitychange', _sessionTimeRefreshVisibilityHandler);
  }
}

function _deferActiveSessionExternalRefresh(reason){
  const nextReason = reason || 'poll';
  if(_deferredActiveSessionExternalRefreshReason==='idle-reconcile'&&nextReason==='poll') return;
  _deferredActiveSessionExternalRefreshReason = nextReason;
}

function _clearDeferredActiveSessionExternalRefresh(){
  _deferredActiveSessionExternalRefreshReason = '';
}

function _flushDeferredActiveSessionExternalRefresh(){
  const reason = _deferredActiveSessionExternalRefreshReason;
  if(!reason) return;
  _deferredActiveSessionExternalRefreshReason = '';
  void refreshActiveSessionIfExternallyUpdated(reason);
}

// Reconcile the active session against server-side metadata. Returns a status
// string so callers (notably the post-stream idle reconcile) can decide how to
// react:
//   'skipped'   — a guard short-circuited before any network probe ran
//   'unchanged' — server metadata matched local (or only a non-transcript bump)
//   'reloaded'  — the transcript was force-reloaded to re-sync
//   'failed'    — the probe request threw (transient); caller may fall back
//
// opts.ignoreStreamJustFinished — bypass the post-stream cooldown. Only the
//   idle-reconcile path sets this: it runs once for the just-finished active
//   turn and probes server metadata FIRST (reloading only on an actual count
//   change), so it is safe to look even right after the "done" event without
//   the unconditional force reload that produced the mobile-PWA end-of-turn
//   flash (#3976). This intentionally COEXISTS with the #3916/#4195 poll-only
//   external gate below, which is untouched.
async function refreshActiveSessionIfExternallyUpdated(reason){
  // opts read via arguments[1] (same pattern as loadSession) so the public
  // signature stays (reason) — callers like the poll/focus/visibility hooks and
  // refreshSessionList keep passing a single reason. Only the post-stream idle
  // reconcile passes opts (see _scheduleActiveSessionIdleReload).
  const opts = arguments[1] || {};
  if(_activeSessionExternalRefreshInFlight) return 'skipped';
  if(!S.session || !S.session.session_id) return 'skipped';
  if(S.busy || S.activeStreamId) return 'skipped';
  if(typeof _isMessageReaderUnpinned==='function'&&_isMessageReaderUnpinned()){
    _deferActiveSessionExternalRefresh(reason||'poll');
    return 'skipped';
  }
  // #3916/#4195: the 30s timer is only a fallback for imported/external sessions.
  // WebUI-native sessions should not keep probing forever when the sidebar SSE
  // is healthy, but they still must reconcile when an actual sessions_changed
  // event, focus, or visibility recovery says another client/process mutated
  // the active transcript (#4205 follow-up shape). The idle-reconcile path uses
  // a non-'poll' reason, so it already sails through this gate untouched.
  if((reason||'poll')==='poll' && !_isExternalSession(S.session)) return 'skipped';
  // Cooldown: don't force-reload immediately after streaming ends — the
  // "done" event already delivered the final messages. Reloading here would
  // clear S.toolCalls and lose Activity. The idle-reconcile path may bypass
  // this guard (opts.ignoreStreamJustFinished) because it probes server
  // metadata first and only reloads when the count actually changed (#3976).
  if(!opts.ignoreStreamJustFinished && typeof window !== 'undefined' && window._streamJustFinished) return 'skipped';
  if(typeof document !== 'undefined' && document.hidden) return 'skipped';
  const sid = S.session.session_id;
  const localCount = Number(S.session.message_count || (Array.isArray(S.messages)?S.messages.length:0) || 0);
  const localLast = Number(S.session.last_message_at || S.session.updated_at || 0);
  _activeSessionExternalRefreshInFlight = true;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`,{timeoutToast:false});
    if(!data || !data.session) return 'unchanged';
    if(!S.session || S.session.session_id !== sid) return 'skipped';
    if(S.busy || S.activeStreamId) return 'skipped';
    const remoteCount = Number(data.session.message_count || 0);
    const remoteLast = Number(data.session.last_message_at || data.session.updated_at || 0);
    // Force-reload the whole transcript whenever the visible conversation's
    // message count CHANGED in either direction. A higher count means new
    // messages; a LOWER count means another tab/client truncated, undid,
    // retried, or regenerated the transcript (/api/session/truncate, /retry,
    // /undo all shrink s.messages and write a lower message_count) — both must
    // re-sync or this tab silently keeps a stale transcript.
    //
    // A bump in last_message_at WITHOUT a count change means a non-transcript
    // write touched the session — most commonly the post-turn background
    // skill/memory review, which rewrites memory/skills and advances updated_at
    // but adds no chat messages. Reloading on that bump tears down and re-fetches
    // the transcript: loadSession(force) clears S.messages and awaits a
    // round-trip before re-rendering, so the whole conversation visibly
    // disappears and "reappears a moment later" with no new content. Skip the
    // destructive reload in that case and just refresh the lightweight sidebar
    // list metadata, advancing the local last-seen marker so the same metadata
    // bump doesn't re-trigger on every subsequent poll.
    if(remoteCount !== localCount){
      // Hidden-tab return / visibility / focus recovery commonly trips
      // remoteCount !== localCount when the post-turn bg-review thread or a
      // sibling tab persisted messages while the tab was hidden. The default
      // loadSession(force) path clears S.messages synchronously and waits for
      // the full transcript round-trip before re-rendering, producing the
      // user-visible "everything disappears, then reappears after a moment"
      // gap that #5061 (metadata-only) and #5122 (SSE 4-probe) DO NOT cover
      // (#5177). Pass keepStaleUntilLoaded so the destructive clear is
      // deferred to swap-in-place when the new transcript actually arrives.
      // Restrict to the recovery reasons that produced the field repro; the
      // post-stream idle reconcile and external/imported-session polls keep
      // the original behaviour (no DOM is on-screen long enough for the gap
      // to matter, and any change there would have to re-verify their own
      // tradeoffs).
      const _recoveryReasons = {visible:true, focus:true};
      const _keepStaleUntilLoaded = !!_recoveryReasons[String(reason||'')];
      // #5409: skip force-reload while a different session's loadSession()
      // is in flight — avoids overwriting _loadingSessionId and silently
      // cancelling an in-progress session switch. All four call paths
      // (idle-reconcile, poll, visibility, focus) funnel through here.
      if(typeof sessionStateBindings._loadingSessionId !== 'undefined' && sessionStateBindings._loadingSessionId && sessionStateBindings._loadingSessionId !== sid) return 'skipped';
      await loadSession(sid, {force:true, externalRefreshReason:reason||'poll', keepStaleUntilLoaded:_keepStaleUntilLoaded});
      if(typeof renderSessionList==='function') void renderSessionList();
      return 'reloaded';
    }else if(remoteLast > localLast){
      if(S.session && S.session.session_id === sid){
        S.session.last_message_at = remoteLast;
        if(data.session.updated_at) S.session.updated_at = data.session.updated_at;
      }
      if(typeof renderSessionList==='function') void renderSessionList();
    }
    return 'unchanged';
  }catch(e){
    // Ignore transient refresh failures; the next poll/focus event will retry.
    return 'failed';
  }finally{
    _activeSessionExternalRefreshInFlight = false;
  }
}

function ensureActiveSessionExternalRefreshPoll(){
  if(_activeSessionExternalRefreshTimer) return;
  _activeSessionExternalRefreshTimer = setInterval(() => {
    void refreshActiveSessionIfExternallyUpdated('poll');
  }, _activeSessionExternalRefreshMs);
  if(typeof document !== 'undefined' && !document._hermesExternalRefreshVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(!document.hidden) void refreshActiveSessionIfExternallyUpdated('visible');
    });
    document._hermesExternalRefreshVisibilityHook = true;
  }
  if(typeof window !== 'undefined' && !window._hermesExternalRefreshFocusHook){
    window.addEventListener('focus', () => { void refreshActiveSessionIfExternallyUpdated('focus'); });
    window._hermesExternalRefreshFocusHook = true;
  }
}

async function refreshSessionList(reason='manual', opts={}){
  const force = !!(opts && opts.force);
  const refreshActive = !!(opts && opts.refreshActive);
  if(!force && typeof document !== 'undefined' && document.hidden) return;
  if(_sessionListRefreshInFlight){
    _sessionListRefreshPendingRequest = {
      reason: reason || 'session-list',
      opts:_mergeSessionListRefreshOptions(_sessionListRefreshPendingRequest && _sessionListRefreshPendingRequest.opts, opts),
    };
    return;
  }
  _sessionListRefreshInFlight = true;
  try{
    await renderSessionList({deferWhileInteracting:!force});
    if(refreshActive) await refreshActiveSessionIfExternallyUpdated(reason||'session-list');
  }finally{
    _sessionListRefreshInFlight = false;
    const pendingRequest = _sessionListRefreshPendingRequest;
    _sessionListRefreshPendingRequest = null;
    if(pendingRequest) _scheduleSessionEventsRefresh(pendingRequest.reason, pendingRequest.opts);
  }
}


if(typeof window!=='undefined') window.refreshSessionList = refreshSessionList;

export { _clearDeferredActiveSessionExternalRefresh, _deferActiveSessionExternalRefresh, _flushDeferredActiveSessionExternalRefresh, _mergeSessionListRefreshOptions, ensureActiveSessionExternalRefreshPoll, ensureSessionTimeRefreshPoll, refreshActiveSessionIfExternallyUpdated, refreshSessionList, startStreamingPoll, stopStreamingPoll };
