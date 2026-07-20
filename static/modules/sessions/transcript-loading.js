import { sessionLoadState } from './session-load-state.js';
import { _isSessionActivelyViewedForList, _setSessionViewedCount } from './session-unread.js';
import { INITIAL_MESSAGE_LIMIT, MESSAGE_LIMIT_FALLBACK, transcriptWindowState } from './transcript-window-state.js';

let _sameSessionForceReloadHint = null;

function _currentLoadedRenderableMessageCount(){
  if(typeof _messageRenderableMessageCount==='function'){
    try{return Math.max(0,Number(_messageRenderableMessageCount())||0);}
    catch(_){}
  }
  let count=0;
  for(const m of (S.messages||[])){
    if(m&&m.role&&m.role!=='tool') count++;
  }
  return count;
}

function _captureSameSessionForceReloadHint(sid){
  const loadedRenderableCount=_currentLoadedRenderableMessageCount();
  const loadedMessageCount=Array.isArray(S.messages)?S.messages.length:0;
  const knownMessageCount=Number(S.session&&S.session.session_id===sid&&S.session.message_count)||loadedMessageCount;
  if(!sid || (loadedRenderableCount<=0 && loadedMessageCount<=0)){
    _sameSessionForceReloadHint=null;
    return;
  }
  _sameSessionForceReloadHint={
    session_id:sid,
    loaded_renderable_count:loadedRenderableCount,
    loaded_message_count:loadedMessageCount,
    message_count:knownMessageCount,
    truncated:!!transcriptWindowState.messagesTruncated,
  };
}

function _clearSameSessionForceReloadHint(sid){
  if(!_sameSessionForceReloadHint) return;
  if(!sid || _sameSessionForceReloadHint.session_id===sid) _sameSessionForceReloadHint=null;
}

function _messageReloadLimitForSession(sid){
  const hint=_sameSessionForceReloadHint;
  if(hint&&hint.session_id===sid){
    const loadedRenderableCount=Math.max(0,Number(hint.loaded_renderable_count)||0);
    const loadedMessageCount=Math.max(0,Number(hint.loaded_message_count)||0);
    if(loadedRenderableCount>0 || loadedMessageCount>0){
      if(!hint.truncated) return null;
      const previousMessageCount=Math.max(0,Number(hint.message_count)||0);
      const currentMessageCount=Math.max(0,Number(S.session&&S.session.session_id===sid&&S.session.message_count)||0);
      const appendedMessageCount=Math.max(0,currentMessageCount-previousMessageCount);
      return Math.max(INITIAL_MESSAGE_LIMIT,loadedRenderableCount,loadedMessageCount+appendedMessageCount);
    }
  }
  return INITIAL_MESSAGE_LIMIT;
}

function _syncToolCallsForLoadedMessages(messages, sessionToolCalls){
  const msgs=Array.isArray(messages)?messages:[];
  // During active streaming, skip — clearing S.toolCalls would lose Activity
  // and the renderMessages fallback is blocked by S.busy=true.
  if(S.busy||S.activeStreamId) return;
  // Persist the loaded compact tool summary onto S.session so the renderMessages
  // derived rebuild can use it as a durable per-tid snippet fallback on cold
  // load (#4927). loadSession keeps the messages=0 session object (tool_calls
  // []), and the messages=1 summary arrives only as this argument — without
  // copying it across, the fallback source is empty exactly on the cold-load
  // path it's meant to repair.
  if(S.session&&Array.isArray(sessionToolCalls)) S.session.tool_calls=sessionToolCalls.map(tc=>({...tc}));
  const hasMessageToolMetadata=msgs.some(m=>{
    if(!m) return false;
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    // `_partial_tool_calls` are emitted by interrupted/partial turns and must also
    // anchor rendering to the owning assistant message, so we can reconstruct
    // settled tool cards from the message history when available.
    const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    return hasTc||hasPartialTc||hasTu;
  });
  if(!hasMessageToolMetadata&&Array.isArray(sessionToolCalls)&&sessionToolCalls.length){
    S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));
  }else{
    S.toolCalls=[];
  }
}

async function _ensureMessagesLoaded(sid, opts) {
  // `opts` is an explicit named parameter (vs loadSession's arguments[1]
  // pattern) because _ensureMessagesLoaded is a module-private helper: it is
  // only called from inside loadSession, so the public signature does not need
  // to be preserved. Strict-mode engines optimize named params more reliably
  // than arguments-indexing, and a named opts is self-documenting for static
  // analysis. Callers pass {force:true} when they need to BYPASS the
  // "messages already populated" early-return — currently only the #5177
  // keep-stale-until-loaded path, which intentionally leaves the old messages
  // in place (to avoid a visible disappear/reappear gap) and relies on
  // _ensureMessagesLoaded to fetch and SWAP the new transcript into
  // S.messages in a single frame.
  opts = opts || {};
  const _loadGeneration = Number.isFinite(opts.loadGeneration) ? Number(opts.loadGeneration) : null;
  const _ownsLoad = () => sessionLoadState.loadingSessionId === sid && (_loadGeneration === null || sessionLoadState.generation === _loadGeneration);
  if (!_ownsLoad()) return;
  // Already have messages? (e.g. from INFLIGHT restore path, already set)
  if (!opts.force && S.messages && S.messages.length > 0 && S.messages[0] && S.messages[0].role) {
    _clearSameSessionForceReloadHint(sid);
    return;
  }
  // Fetch session messages with a tail window for fast initial load.
  const reloadLimit = _messageReloadLimitForSession(sid); // defaults to INITIAL_MESSAGE_LIMIT
  // A reload window above the server's msg_limit ceiling would be clamped by
  // the backend (returning only the last MESSAGE_LIMIT_FALLBACK rows), which can
  // silently SHRINK an already-loaded transcript that had more than the ceiling
  // of rows visible (rows 400–999 replaced by 500–999). When the requested
  // window exceeds the ceiling, fall back to the bare full-transcript request
  // (no msg_limit / no expand_renderable) so a same-session refresh never drops
  // already-loaded older rows (Codex gate #6154, silent row-loss).
  const boundedReloadLimit = (reloadLimit && reloadLimit <= transcriptWindowState.msgLimitMax) ? reloadLimit : null;
  const reloadLimitParam = boundedReloadLimit ? `&msg_limit=${boundedReloadLimit}` : '';
  // Older frontends used expand_renderable=1 to request visible-row expansion.
  // The server now counts msg_limit by visible transcript rows by default; keep
  // the flag for compatibility with mixed-version deployments.
  const expandParam = boundedReloadLimit ? '&expand_renderable=1' : '';
  let data;
  try {
    data = await api(
      `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0${reloadLimitParam}${expandParam}`,
      {timeoutMs:120000}
    );
  } finally {
    if (_ownsLoad()) _clearSameSessionForceReloadHint(sid);
  }
  if (!_ownsLoad()) return;
  // Guard: api() may have redirected (401) and returned undefined.
  if (!data || !data.session) return;
  transcriptWindowState.messagesTruncated = !!data.session._messages_truncated;
  transcriptWindowState.oldestIdx = data.session._messages_offset || 0;
  transcriptWindowState.msgLimitMax = data.session._msg_limit_max || MESSAGE_LIMIT_FALLBACK;
  // #3162: `msgs` is reassigned below by the #3018 ephemeral-field carry-forward,
  // so it must be `let`, not `const`. The `const` form threw a TypeError inside
  // _ensureMessagesLoaded() that surfaced as a "Failed to load conversation messages"
  // toast on every mobile message (SSE/visibility events trigger this reload path
  // more aggressively on mobile).
  let msgs = (data.session.messages || []).filter(m => m && m.role);
  // Skip _syncToolCalls when INFLIGHT exists — the INFLIGHT restore path
  // (loadSession line ~871) will overwrite S.toolCalls from INFLIGHT[sid].toolCalls.
  // Clearing here and then overwriting is wasteful, and if S.busy becomes true
  // before the next render, the fallback can't re-derive from messages.
  if(!(typeof INFLIGHT !== 'undefined' && INFLIGHT && INFLIGHT[sid])){
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
  }
  clearLiveToolCards();
  // #3018: preserve client-side ephemeral turn fields (_turnUsage, _turnDuration,
  // _turnTps, _gatewayRouting, _statusCard, _anchor_stream_id) across the loadSession replace.
  if(typeof window._carryForwardEphemeralTurnFields==='function'){
    // #3306: Prefer the pre-clear snapshot stashed by loadSession() on a
    // force-reload of the active session; S.messages was reset to [] there
    // and would otherwise yield an empty carry-forward.
    const _prev = (Array.isArray(sessionLoadState.pendingCarryForwardSnapshot) && sessionLoadState.pendingCarryForwardSnapshot.length)
      ? sessionLoadState.pendingCarryForwardSnapshot
      : (S.messages || []);
    msgs=window._carryForwardEphemeralTurnFields(_prev, msgs);
    sessionLoadState.pendingCarryForwardSnapshot = null;
  }
  if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();
  S.messages = msgs;
  // Expand render window to cover all loaded messages so the next
  // renderMessages() doesn't hide most of them behind a tiny window.
  if(typeof _messageRenderableMessageCount==='function'&&typeof _currentMessageRenderWindowSize==='function'){
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(), _messageRenderableMessageCount());
  }
  if(S.session&&S.session.session_id===sid){
    S.session.message_count=Number(data.session.message_count || msgs.length);
    S.lastUsage={...(data.session.last_usage||S.lastUsage||{})};
    // Phase 2: the messages=1 response carries the canonical cold-load
    // `todo_state` snapshot, derived server-side from the FULL untruncated
    // message list (api/routes.py + api/todo_state.py). The earlier
    // messages=0 fetch in loadSession() does not include this field —
    // attach_todo_state is gated on `load_messages`. Without applying it
    // here, long sessions whose latest todo write falls outside the
    // INITIAL_MESSAGE_LIMIT tail would lose the panel on refresh: the
    // legacy reverse-scan in _legacyTodosFromMessages() can only see the
    // tail S.messages, while the authoritative snapshot was already
    // computed by the server and is sitting in this very response.
    // _hydrateTodosFromSession is idempotent and picks newer of
    // cold-load vs INFLIGHT by timestamp, so calling it again here is
    // safe even when an INFLIGHT snapshot was already restored.
    if(data.session.todo_state !== undefined){
      S.session.todo_state = data.session.todo_state;
    }else{
      delete S.session.todo_state;
    }
    if(typeof _hydrateTodosFromSession === 'function'){
      _hydrateTodosFromSession(S.session);
    }
    if(typeof scheduleTodosRefresh === 'function'){
      scheduleTodosRefresh();
    }
    // Only sync the viewed count (which also clears any completion-unread
    // marker via _setSessionViewedCount -> _clearSessionCompletionUnread)
    // when the session is STILL actively viewed. A hidden-tab completion that
    // lands during the awaited message fetch above must NOT be silently marked
    // read here — mirror the same _isSessionActivelyViewedForList(sid) guard
    // used on the post-load re-ack in loadSession(). (#5917 gate finding)
    if(typeof _isSessionActivelyViewedForList !== 'function' || _isSessionActivelyViewedForList(sid)){
      _setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));
    }
    if(typeof syncTopbar==='function') syncTopbar();
  }
}

export const transcriptLoading=Object.freeze({ensureLoaded:_ensureMessagesLoaded,captureReloadHint:_captureSameSessionForceReloadHint,clearReloadHint:_clearSameSessionForceReloadHint,syncToolCalls:_syncToolCallsForLoadedMessages});

export const _INITIAL_MSG_LIMIT=INITIAL_MESSAGE_LIMIT;
export { _captureSameSessionForceReloadHint, _clearSameSessionForceReloadHint, _ensureMessagesLoaded, _syncToolCallsForLoadedMessages };
