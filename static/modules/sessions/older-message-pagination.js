import { sessionLoadState } from './session-load-state.js';
import { _INITIAL_MSG_LIMIT, _syncToolCallsForLoadedMessages } from './transcript-loading.js';
import { _sameTranscriptMessage } from './current-turn-transcript.js';
import { transcriptWindowState } from './transcript-window-state.js';

// Load older messages when the user scrolls to the top of the conversation.
// The owner preserves session identity, generation, and viewport position across every await.
async function _loadOlderMessages() {
  if (transcriptWindowState.loadingOlder || !transcriptWindowState.messagesTruncated) return;
  const sid = S.session ? S.session.session_id : null;
  if (!sid || !S.messages.length) return;
  if (transcriptWindowState.oldestIdx <= 0) { transcriptWindowState.messagesTruncated = false; return; }
  transcriptWindowState.loadingOlder = true;
  // Snapshot the generation BEFORE we await. If S.messages is wholesale
  // replaced while the request is in flight, the post-await check below
  // bails out so we never prepend stale older messages onto a freshly
  // rebuilt transcript (#1937).
  const startGeneration = transcriptWindowState.generation;
  try {
    // Two strategies, chosen by whether the growing tail window still fits under
    // the server's msg_limit ceiling (_MSG_LIMIT_MAX, mirroring backend
    // _MAX_MSG_LIMIT):
    //
    //  - Below the ceiling: ask for a larger authoritative tail window
    //    (currentLoaded + _INITIAL_MSG_LIMIT). Post-#2716 the backend runs the
    //    full append-only merge, so a larger msg_limit produces the same merged
    //    transcript we'd get by stitching pages, without client-side index
    //    bookkeeping. The newly exposed head is what we expose to the user.
    //
    //  - At/above the ceiling: the server clamps msg_limit, so the tail window
    //    stops growing and this strategy would stall (the same clamped tail is
    //    returned, olderMsgs -> 0). Switch to msg_before paging — a fixed
    //    _INITIAL_MSG_LIMIT backward page keyed off transcriptWindowState.oldestIdx — which is
    //    bounded and never hits the ceiling, so the head stays reachable for
    //    arbitrarily long transcripts. (This is the same paging request the
    //    race-fallback below uses, proven correct there.)
    const requestedLimit = Math.max(_INITIAL_MSG_LIMIT, (S.messages || []).length + _INITIAL_MSG_LIMIT);
    const useBeforePaging = requestedLimit >= transcriptWindowState.msgLimitMax;
    const data = useBeforePaging
      ? await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${transcriptWindowState.oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`,
          {timeoutMs:120000}
        )
      : await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_limit=${requestedLimit}`,
          {timeoutMs:120000}
        );
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) { transcriptWindowState.loadingOlder = false; return; }
    //  - response shape sane
    //  - the active session is still the one we issued the request for.
    //    Compare against S.session.session_id, NOT _loadingSessionId — the
    //    latter is null between session loads, leaving a window where a
    //    stale response could prepend onto the new session's S.messages.
    if (!data || !data.session) return;
    if (!S.session || S.session.session_id !== sid) return;
    if (sessionLoadState.loadingSessionId !== null && sessionLoadState.loadingSessionId !== sid) return;
    // Generation guard: another code path (typically jumpToSessionStart →
    // _ensureAllMessagesLoaded) may have replaced S.messages while we were
    // awaiting. Prepending older messages onto that replacement would
    // duplicate the head of the transcript. Detect via the generation
    // counter and abort cleanly. transcriptWindowState.oldestIdx and _messagesTruncated were
    // already reset by the wholesale-replace path, so no rollback needed.
    if (transcriptWindowState.generation !== startGeneration) return;
    let responseSession = data.session;
    let expandedMsgs = (responseSession.messages || []).filter(m => m && m.role);
    const currentMsgs = (S.messages || []).filter(m => m && m.role);
    const currentLen = currentMsgs.length;
    // Suffix-continuity check: the cumulative tail is only safe to wholesale-
    // replace when our currently-displayed messages are still its suffix. If
    // the server appended new messages (or merge filtered something) while we
    // were awaiting, the suffix won't line up — fall back to the legacy
    // msg_before page so we never drop visible older messages on the floor.
    // When useBeforePaging is true, `data` is a bounded msg_before OLDER page,
    // not a cumulative tail. A raw-row-heavy older page whose visible text
    // repeats the current tail could otherwise pass the suffix check below and
    // be wholesale-replaced AS IF it were the full tail — silently discarding
    // the current (newer) rows and marking history complete. Gate the suffix
    // heuristic on !useBeforePaging so every msg_before page is always treated
    // as an older page and prepended (Codex gate #6154, silent row-loss).
    let tailMatches = !useBeforePaging && expandedMsgs.length >= currentLen;
    if (tailMatches && currentLen > 0) {
      const start = expandedMsgs.length - currentLen;
      for (let i = 0; i < currentLen; i++) {
        if (!_sameTranscriptMessage(expandedMsgs[start + i], currentMsgs[i])) {
          tailMatches = false;
          break;
        }
      }
    }
    let olderCount = Math.max(0, expandedMsgs.length - currentLen);
    let olderMsgs = expandedMsgs.slice(0, olderCount);
    let nextMessages = expandedMsgs;
    if (!tailMatches) {
      // Race fallback (or the over-ceiling msg_before primary path): keep the
      // legacy index-page request as the correctness-preserving alternative.
      // When useBeforePaging is true we already fetched a msg_before page as
      // the primary `data`, so reuse it instead of re-fetching. Same guards
      // reapplied because we just awaited again (skipped for the reuse case).
      if (!useBeforePaging) {
        const fallback = await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${transcriptWindowState.oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`,
          {timeoutMs:120000}
        );
        if (!fallback || !fallback.session) { transcriptWindowState.loadingOlder = false; return; }
        if (!S.session || S.session.session_id !== sid) return;
        if (sessionLoadState.loadingSessionId !== null && sessionLoadState.loadingSessionId !== sid) return;
        if (transcriptWindowState.generation !== startGeneration) return;
        responseSession = fallback.session;
      }
      olderMsgs = (responseSession.messages || []).filter(m => m && m.role);
      nextMessages = [...olderMsgs, ...S.messages];
    }
    if (!olderMsgs.length) { transcriptWindowState.messagesTruncated = !!responseSession._messages_truncated; return; }
    // Replace with the larger tail window and preserve scroll as if older
    // messages were prepended. When the suffix check fails, nextMessages
    // already encodes the legacy prepend fallback so the visible behavior
    // matches the old msg_before page path exactly.
    // Use $('messages') — the scrollable container (#msgInner is not scrollable).
    const container = $('messages');
    const prevScrollH = container ? container.scrollHeight : 0;
    const oldTop = container ? container.scrollTop : 0;
    const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')
      ? _captureMessageViewportAnchor()
      : null;
    // Carry forward ephemeral turn fields (_turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard/_anchor_stream_id) before the wholesale replace so the badge
    // does not briefly appear and disappear during older-message expansion.
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      nextMessages = window._carryForwardEphemeralTurnFields(S.messages || [], nextMessages);
    }
    S.messages = nextMessages;
    _syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);
    // renderMessages() windows long transcripts from the end. If we do not
    // expand that window before rendering, the newly prepended page stays
    // hidden and the "hidden" counter rises while the viewport appears stuck.
    // Count by the same visible-message rules used by renderMessages(); the
    // virtual fallback below uses this as a pixel-height prefix length.
    const addedRenderable = olderMsgs.filter(m=>{
      if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);
      if(!m||!m.role||m.role==='tool') return false;
      if(typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m)) return false;
      if(typeof _isPreservedCompressionTaskListMessage==='function'&&_isPreservedCompressionTaskListMessage(m)) return false;
      if(typeof _isRecoveryControlMessage==='function'&&_isRecoveryControlMessage(m)) return false;
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
      return !!(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||hasPartialTc||(typeof _messageHasReasoningPayload==='function'&&_messageHasReasoningPayload(m))||(typeof _assistantMessageHasVisibleContent==='function'&&_assistantMessageHasVisibleContent(m)))));
    }).length;
    _messageRenderWindowSize=_currentMessageRenderWindowSize()+Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT);
    transcriptWindowState.messagesTruncated = !!responseSession._messages_truncated;
    transcriptWindowState.oldestIdx = responseSession._messages_offset || 0;
    renderMessages({ preserveScroll: true });
    if (container) {
      // Prepending older messages must not teleport the reader. Anchor to the
      // first visible rendered row and restore that row's top offset after the
      // prepend so synthetic virtual spacer heights cannot skew the delta.
      const restoredViaAnchor = (viewportAnchor && typeof _restoreMessageViewportAnchor === 'function')
        ? _restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)
        : false;
      if (!restoredViaAnchor) {
        const virtualAddedHeight = (typeof _messageVirtualPrependedHeightDelta === 'function')
          ? _messageVirtualPrependedHeightDelta(addedRenderable)
          : null;
        const newScrollH = container.scrollHeight;
        const addedHeight = Number.isFinite(virtualAddedHeight)
          ? virtualAddedHeight
          : Math.max(0, newScrollH - prevScrollH);
        _programmaticScroll = true;
        container.scrollTop = oldTop + addedHeight;
        requestAnimationFrame(()=>{ _programmaticScroll = false; });
      }
    }
    _scrollPinned = false;
  } catch(e) {
    console.warn('_loadOlderMessages failed:', e);
  } finally {
    // Always clear the loading lock. If the user switched sessions while
    // this request was in flight, loadSession() already set transcriptWindowState.loadingOlder=false
    // (see line ~122), so this is a harmless double-reset.
    transcriptWindowState.loadingOlder = false;
  }
}

// Ensure the full message history is loaded (for undo, export, etc).
// If the session was loaded with msg_limit, this fetches all messages.
//
// Race-safety (#1937): with the endless-scroll opt-in, _loadOlderMessages
// may be in flight when this runs (e.g. user scrolled near the top, then
// hit the Start jump pill). Two coordinated guards prevent the prefetch
// from prepending duplicate messages onto our wholesale replacement:
//   1. Hold the transcriptWindowState.loadingOlder mutex around the body so a NEW prefetch
//      cannot start mid-replace (entry-gate check at line ~1003 returns
//      early). The mutex is also self-protecting against concurrent
//      ensure-all calls from rapid double-clicks on Start.
//   2. Bump transcriptWindowState.generation before mutating S.messages so any
//      in-flight prefetch's post-await generation check bails out.
async function _ensureAllMessagesLoaded() {
  if (!transcriptWindowState.messagesTruncated || !S.session) return;
  if (transcriptWindowState.loadingOlder) {
    // A prefetch is mid-flight (between the `transcriptWindowState.loadingOlder = true` line
    // and its post-await guards). Bumping the generation token now
    // poisons that prefetch's continuation, but we still need to claim
    // the mutex AFTER it releases. Yield until the prefetch finishes
    // (its finally-block clears transcriptWindowState.loadingOlder) before fetching the full
    // history ourselves. The generation bump below ensures any other
    // future race against this same continuation also fails closed.
    transcriptWindowState.bumpGeneration();
    while (transcriptWindowState.loadingOlder) {
      await new Promise(resolve => setTimeout(resolve, 16));
    }
    if (!transcriptWindowState.messagesTruncated || !S.session) return;
  }
  transcriptWindowState.loadingOlder = true;
  try {
    const sid = S.session.session_id;
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0`, {timeoutMs:120000});
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) return;
    // Session may have been switched while we awaited. Bail rather than
    // overwrite the new session's messages.
    if (!S.session || S.session.session_id !== sid) return;
    if (sessionLoadState.loadingSessionId !== null && sessionLoadState.loadingSessionId !== sid) return;
    const msgs = (data.session.messages || []).filter(m => m && m.role);
    // Bump the generation BEFORE the wholesale replace so any racing
    // prefetch (whose snapshot was taken before this call's mutex
    // acquisition) sees the new value and aborts.
    transcriptWindowState.bumpGeneration();
    // #3306: Same ephemeral-field carry-forward as _ensureMessagesLoaded.
    // Loading older messages also does a wholesale replace of S.messages
    // and would otherwise drop _turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard/_anchor_stream_id on the existing turns.
    let _msgsToAssign = msgs;
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      _msgsToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], msgs);
    }
    S.messages = _msgsToAssign;
    transcriptWindowState.messagesTruncated = false;
    transcriptWindowState.oldestIdx = 0;
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
    if (S.session && S.session.session_id === sid) {
      S.session.message_count = Number(data.session.message_count || msgs.length);
    }
  } finally {
    transcriptWindowState.loadingOlder = false;
  }
}

export const olderMessagePagination=Object.freeze({loadOlder:_loadOlderMessages,ensureAllLoaded:_ensureAllMessagesLoaded});

export { _ensureAllMessagesLoaded, _loadOlderMessages };
