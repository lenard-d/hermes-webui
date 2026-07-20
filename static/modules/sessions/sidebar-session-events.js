import { _sessionEventProfilesMatch } from './session-state-store.js';
import { _externalImportPayload, _isCliImportRefreshPrefixMatch, _isCliSession, _isExternalSession, _isMessagingSession } from './message-loading.js';
import { sidebarStateBindings } from './sidebar-store.js';
import { renderSessionList } from './session-list-loader.js';
import { _mergeSessionListRefreshOptions, refreshSessionList } from './session-list-refresh.js';

let _gatewaySSE = null;
let _gatewayPollTimer = null;
let _gatewayProbeInFlight = false;
let _gatewaySSEWarningShown = false;
const _gatewayFallbackPollMs = 30000;
let _sessionEventsSSE = null;
let _sessionEventsRefreshTimer = 0;
let _sessionEventsRefreshPendingRequest = null;
let _sessionEventsReconnectTimer = 0;
let _sessionEventsNeedsRefreshOnOpen = false;
let _sessionEventsReconnectAttempt = 0;
const _sessionEventsReconnectBaseMs = 5000;
const _sessionEventsReconnectMaxMs = 30000;

function _sessionEventsReconnectDelayMs(){
  const attempt = Math.max(0, Number(_sessionEventsReconnectAttempt || 0));
  const base = Math.min(_sessionEventsReconnectMaxMs, _sessionEventsReconnectBaseMs * Math.pow(2, attempt));
  const jitter = Math.floor(Math.random() * Math.max(1, Math.floor(base * 0.35)));
  return Math.min(_sessionEventsReconnectMaxMs, Math.floor(base * 0.75) + jitter);
}

function _refreshSessionListAfterSidebarResume(reason){
  _sessionEventsNeedsRefreshOnOpen = false;
  void refreshSessionList(reason, {force:true});
}

function _scheduleSessionEventsRefresh(reason, opts={}){
  _sessionEventsRefreshPendingRequest = {
    reason: reason || (_sessionEventsRefreshPendingRequest && _sessionEventsRefreshPendingRequest.reason) || 'event',
    opts:_mergeSessionListRefreshOptions(_sessionEventsRefreshPendingRequest && _sessionEventsRefreshPendingRequest.opts, opts),
  };
  if(_sessionEventsRefreshTimer) return;
  _sessionEventsRefreshTimer = setTimeout(() => {
    _sessionEventsRefreshTimer = 0;
    const request = _sessionEventsRefreshPendingRequest || {reason:'event', opts:{}};
    _sessionEventsRefreshPendingRequest = null;
    void refreshSessionList(request.reason||'event', request.opts);
  }, 300);
}

function _sessionEventTargetsActiveSession(payload){
  const eventSessionId = payload && typeof payload.session_id === 'string' ? payload.session_id : '';
  if(!eventSessionId) return false;
  return !!(S.session && S.session.session_id && S.session.session_id === eventSessionId);
}

// ── #4151: focus-aware close for the two GLOBAL sidebar SSE streams ──────────
// Each WebUI window holds up to three persistent SSE connections (session-events
// + gateway + the per-session stream). #3992/#3996 close them on the Page
// Visibility API (`visibilitychange` / `document.hidden`) so a hidden tab frees
// HTTP/1.1 pool slots. But a PWA *standalone* window does NOT reliably fire
// `visibilitychange` when it merely loses focus to another window of the same
// app — `document.hidden` only flips on minimize. So two side-by-side PWA windows
// both stay `visibilityState==='visible'`, each keeps its sidebar streams open,
// and 2x3 = 6 = the per-origin HTTP/1.1 connection limit; every later fetch()
// (the 30s polls) queues behind the saturated pool and times out (#4151).
// `document.hasFocus()` is the signal `visibilitychange` misses — only one window
// holds focus at a time.
//
// Scope: ONLY the two global sidebar streams (session-events + gateway). The
// per-session live stream (messages.js `startSessionStream`) deliberately stays
// visibility-only — it carries live `bg_task_complete` toasts and
// `server_turn_started` live-view that an unfocused-but-VISIBLE window must still
// receive (the OS-notification path is gated on `document.hidden`, so the in-app
// toast is the only completion signal a visible-unfocused window gets). Closing
// it on blur would regress the multi-window live-view UX.
function _sidebarSseBackgrounded(){
  if(typeof document === 'undefined') return false;
  if(document.hidden) return true;
  if(typeof document.hasFocus === 'function' && !document.hasFocus()) return true;
  return false;
}

let _sidebarSseBlurCloseTimer = 0;
// Debounce the blur-close so a transient blur (native dialog, quick alt-tab and
// back) doesn't thrash the streams; a sustained blur frees the pool slots.
const _SIDEBAR_SSE_BLUR_CLOSE_MS = 1000;

function _installSidebarSseFocusHook(){
  if(typeof window === 'undefined' || typeof document === 'undefined') return;
  if(document._hermesSidebarSseFocusHook) return;
  document._hermesSidebarSseFocusHook = true;
  window.addEventListener('blur', () => {
    if(_sidebarSseBlurCloseTimer) return;
    _sidebarSseBlurCloseTimer = setTimeout(() => {
      _sidebarSseBlurCloseTimer = 0;
      // Re-check at fire time — focus may have returned during the debounce.
      if(_sidebarSseBackgrounded()){
        _closeSessionEventsSSE();
        stopGatewaySSE();
      }
    }, _SIDEBAR_SSE_BLUR_CLOSE_MS);
  });
  window.addEventListener('focus', () => {
    if(_sidebarSseBlurCloseTimer){ clearTimeout(_sidebarSseBlurCloseTimer); _sidebarSseBlurCloseTimer = 0; }
    // Reopen and catch up on anything missed while blurred. ensureSessionEventsSSE()
    // is idempotent (`if(_sessionEventsSSE) return`), but startGatewaySSE() is NOT — it
    // begins with an unconditional stopGatewaySSE(). So only reopen the gateway when it
    // was actually closed; otherwise a transient blur shorter than the debounce (where
    // the blur-close timer was cleared and the stream was never torn down) would
    // drop+reconnect the live gateway on every window switch, cancelling its poll
    // fallback and resetting probe/warning state — the exact thrash the debounce exists
    // to prevent, in the multi-window scenario this fix targets (#4151).
    ensureSessionEventsSSE();
    if(!_gatewaySSE) startGatewaySSE();
    void _refreshSessionListAfterSidebarResume('focus');
  });
}

function _closeSessionEventsSSE(){
  if(_sessionEventsSSE){
    try{if(_sessionEventsSSE.readyState!==2)_sessionEventsSSE.close();}catch(_){ }
    _sessionEventsSSE = null;
    _sessionEventsNeedsRefreshOnOpen = true;
  }
}

function ensureSessionEventsSSE(){
  if(typeof document !== 'undefined' && !document._hermesSessionEventsVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(document.hidden){
        _closeSessionEventsSSE();
      }else{
        ensureSessionEventsSSE();
        void _refreshSessionListAfterSidebarResume('visible');
      }
    });
    document._hermesSessionEventsVisibilityHook = true;
  }
  _installSidebarSseFocusHook();
  if(typeof EventSource==='undefined') return;
  if(_sidebarSseBackgrounded()) return;
  if(_sessionEventsSSE) return;
  try{
    // Same-origin relative URL preserves subpath mounts and normal WebUI cookies.
    _sessionEventsSSE = new EventSource('api/sessions/events');
    _sessionEventsSSE.onopen = () => {
      _sessionEventsReconnectAttempt = 0;
      if(!_sessionEventsNeedsRefreshOnOpen) return;
      _sessionEventsNeedsRefreshOnOpen = false;
      void _refreshSessionListAfterSidebarResume('reconnect');
    };
    _sessionEventsSSE.addEventListener('sessions_changed', (ev) => {
      const activeProfile = S.activeProfile || 'default';
      let eventTargetsActiveSession = false;
      try {
        const payload = typeof ev?.data === 'string' ? JSON.parse(ev.data) : {};
        const eventProfile = payload && typeof payload.profile === 'string' ? payload.profile : '';
        if (!_sessionEventProfilesMatch(eventProfile, activeProfile)) {
          return;
        }
        eventTargetsActiveSession = _sessionEventTargetsActiveSession(payload);
      } catch (_err) {
        // Non-JSON payload (or transient malformed event). Keep legacy behavior:
        // refresh once event was seen.
      }
      _scheduleSessionEventsRefresh(eventTargetsActiveSession?'event-active-session':'event', {force:true, refreshActive:true});
    });
    _sessionEventsSSE.onerror = () => {
      _sessionEventsNeedsRefreshOnOpen = true;
      _closeSessionEventsSSE();
      if(_sessionEventsReconnectTimer) return;
      const delayMs = _sessionEventsReconnectDelayMs();
      _sessionEventsReconnectAttempt = Math.min(_sessionEventsReconnectAttempt + 1, 6);
      _sessionEventsReconnectTimer = setTimeout(() => {
        _sessionEventsReconnectTimer = 0;
        ensureSessionEventsSSE();
      }, delayMs);
    };
  }catch(e){
    _closeSessionEventsSSE();
  }
}

if(typeof window!=='undefined') window.refreshSessionList = refreshSessionList;

let _gatewayPollVisibilityHandler = null; // saved so stopGatewayPollFallback can remove it

function startGatewayPollFallback(ms){
  const intervalMs = Math.max(5000, Number(ms) || _gatewayFallbackPollMs);
  if(_gatewayPollTimer) clearInterval(_gatewayPollTimer);
  _gatewayPollTimer = setInterval(() => {
    // Skip poll when tab is hidden or a stream is active — saves CPU
    // and avoids redundant DOM renders during active streaming (#4704).
    if(typeof document !== 'undefined' && document.hidden) return;
    if(typeof S !== 'undefined' && (S.busy || S.activeStreamId)) return;
    renderSessionList({deferWhileInteracting:true});
  }, intervalMs);
  // Visibility catch-up: refresh immediately when tab re-gains focus,
  // so no gateway updates are dropped during hidden-skip periods.
  // Save the handler so stopGatewayPollFallback can removeEventListener it (#4730 review).
  if(typeof document !== 'undefined' && !_gatewayPollVisibilityHandler){
    _gatewayPollVisibilityHandler = () => {
      if(!document.hidden && typeof renderSessionList === 'function'){
        void renderSessionList({deferWhileInteracting:false});
      }
    };
    document.addEventListener('visibilitychange', _gatewayPollVisibilityHandler);
  }
}

function stopGatewayPollFallback(){
  if(_gatewayPollTimer){
    clearInterval(_gatewayPollTimer);
    _gatewayPollTimer = null;
  }
  if(_gatewayPollVisibilityHandler && typeof document !== 'undefined'){
    document.removeEventListener('visibilitychange', _gatewayPollVisibilityHandler);
    _gatewayPollVisibilityHandler = null;
  }
}

function _gatewaySessionSnapshotKey(sessions){
  return (Array.isArray(sessions)?sessions:[])
    .filter(s=>s&&s.session_id)
    .map(s=>`${s.session_id}:${s.updated_at||0}:${s.message_count||0}`)
    .sort()
    .join('|');
}

function _isGatewaySessionForSnapshot(session){
  if(!session) return false;
  if(typeof _isCliSession==='function'&&_isCliSession(session)) return true;
  if(typeof _isMessagingSession==='function'&&_isMessagingSession(session)) return true;
  const source=String(session.session_source||session.raw_source||session.source_tag||session.source||'').toLowerCase();
  return !!source&&source!=='webui';
}

function _isDuplicateGatewaySessionSnapshot(sessions){
  const incoming=(Array.isArray(sessions)?sessions:[]).filter(_isGatewaySessionForSnapshot);
  const currentGatewaySessions=(Array.isArray(sidebarStateBindings._allSessions)?sidebarStateBindings._allSessions:[]).filter(_isGatewaySessionForSnapshot);
  if(!incoming.length&&!currentGatewaySessions.length) return true;
  return _gatewaySessionSnapshotKey(incoming)===_gatewaySessionSnapshotKey(currentGatewaySessions);
}

async function probeGatewaySSEStatus(){
  if(_gatewayProbeInFlight || !window._showCliSessions) return;
  _gatewayProbeInFlight = true;
  try{
    const resp = await fetch(new URL('api/sessions/gateway/stream?probe=1', document.baseURI || location.href).href, { credentials:'same-origin' });
    const data = await resp.json().catch(() => ({}));
    if(resp.ok && data.watcher_running){
      stopGatewayPollFallback();
      _gatewaySSEWarningShown = false;
      if(!_gatewaySSE && typeof EventSource!=='undefined' && !(document&&document.hidden)) startGatewaySSE();
      return;
    }
    if(resp.status === 503 || data.watcher_running === false){
      startGatewayPollFallback(data.fallback_poll_ms || _gatewayFallbackPollMs);
      renderSessionList({deferWhileInteracting:true});
      if(!_gatewaySSEWarningShown && typeof showToast === 'function'){
        showToast('Gateway sync unavailable — falling back to periodic refresh.', 5000);
        _gatewaySSEWarningShown = true;
      }
    }
  }catch(e){
    // Network error during probe — server may be unreachable.
    // Start fallback polling as a safe default; it will self-cancel
    // when the SSE connection recovers and sessions_changed fires.
    startGatewayPollFallback(_gatewayFallbackPollMs);
    renderSessionList({deferWhileInteracting:true});
  }finally{
    _gatewayProbeInFlight = false;
  }
}

function startGatewaySSE(){
  stopGatewaySSE();
  if(!window._showCliSessions) return;
  // Visibility hook (install once) — mirror ensureSessionEventsSSE() pattern
  if(typeof document !== 'undefined' && !document._hermesGatewaySSEVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(document.hidden){
        stopGatewaySSE();
      }else{
        void startGatewaySSE();
      }
    });
    document._hermesGatewaySSEVisibilityHook = true;
  }
  _installSidebarSseFocusHook();
  // Don't open when tab is hidden OR the window has lost focus (PWA blur) —
  // saves connection pool slots (#4151).
  if(_sidebarSseBackgrounded()) return;
  try{
    _gatewaySSE = new EventSource('api/sessions/gateway/stream');
    _gatewaySSE.addEventListener('sessions_changed', (ev) => {
      try{
        const data = JSON.parse(ev.data);
        if(data.sessions){
          stopGatewayPollFallback();
          _gatewaySSEWarningShown = false;
          if(!_isDuplicateGatewaySessionSnapshot(data.sessions)){
            renderSessionList({deferWhileInteracting:true}); // re-fetch and re-render
          }
          // If the active session received new gateway messages, refresh the conversation view.
          // S.busy check prevents stomping on an in-progress WebUI response.
          // _isExternalSession covers CLI-originated and messaging-source sessions
          // that need a server-side import before WebUI can read them.
          if(S.session && !S.busy && _isExternalSession(S.session)){
            const changedIds = new Set((data.sessions||[]).map(s=>s.session_id));
            if(changedIds.has(S.session.session_id)){
              // Capture active session ID before async fetch — race guard.
              // If the user switches sessions while the fetch is in-flight, discard the result.
              const activeSid = S.session.session_id;
              api('/api/session/import_cli',{method:'POST',body:JSON.stringify(_externalImportPayload(S.session))})
                .then(res=>{
                  if(!S.session || S.session.session_id !== activeSid) return;
                  if(res && res.session && Array.isArray(res.session.messages)){
                    const prev = S.messages.length;
                    const next = res.session.messages.filter(m => m && m.role);
                    if (next.length < prev) return;
                    if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;
                    // Carry forward ephemeral turn fields (_turnUsage/
                    // _turnDuration/_turnTps/_gatewayRouting/_statusCard/
                    // _anchor_stream_id) so
                    // gateway-driven CLI refreshes do not drop the badge.
                    let _nextToAssign = next;
                    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
                      _nextToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], next);
                    }
                    S.messages = _nextToAssign;
                    if(S.session && S.session.session_id === activeSid){
                      S.session.message_count = next.length;
                      const newest = next.length ? next[next.length - 1] : null;
                      const newestTs = Number((newest && (newest.timestamp || newest._ts)) || 0);
                      if(newestTs){
                        S.session.last_message_at = newestTs;
                        S.session.updated_at = newestTs;
                      }
                    }
                    if(S.messages.length !== prev){
                      renderMessages({preserveScroll:true});
                      if(typeof highlightCode==='function') highlightCode();
                    }
                  }
                })
                .catch(()=>{ /* ignore — next poll will retry */ });
            }
          }
        }
      }catch(e){ /* ignore parse errors */ }
    });
    _gatewaySSE.onerror = () => {
      if(typeof recordClientSSEError==='function') recordClientSSEError('gateway-sessions',{ready_state:_gatewaySSE?_gatewaySSE.readyState:null,reason:'gateway EventSource.onerror'});
      if(_gatewaySSE){
        try{if(_gatewaySSE.readyState!==2)_gatewaySSE.close();}catch(_){ }
        _gatewaySSE = null;
      }
      void probeGatewaySSEStatus();
    };
  }catch(e){
    void probeGatewaySSEStatus();
  }
}

function stopGatewaySSE(){
  if(_gatewaySSE){
    try{if(_gatewaySSE.readyState!==2)_gatewaySSE.close();}catch(_){ }
    _gatewaySSE = null;
  }
  stopGatewayPollFallback();
  _gatewayProbeInFlight = false;
  _gatewaySSEWarningShown = false;
}

export { _scheduleSessionEventsRefresh, ensureSessionEventsSSE, probeGatewaySSEStatus, startGatewaySSE, startGatewayPollFallback, stopGatewayPollFallback, stopGatewaySSE };
