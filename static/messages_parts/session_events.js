var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

// ── Session-scoped SSE stream (Option X) ──────────────────────────────────
// Long-lived EventSource bound to /api/session/stream?session_id=<sid>.
// Lives across agent turns (unlike the per-turn /api/chat/stream which is
// torn down at end-of-turn). Carries bg_task_complete events fired while no
// turn is active — the architectural fix for the notify_on_complete wakeup
// gap that #2242 + #2279 papered over.
//
// Lifecycle: opened on session mount (loadSession / newSession), closed on
// session switch / unmount. The browser closes it implicitly on tab close
// (server detects disconnect via the SSE read-loop and unsubscribes).
let _sessionEventSource = null;
let _sessionStreamSessionId = null;
let _sessionStreamReconnectTimer = null;
// Holds the session id across a hidden-tab close so the visibility handler can
// reopen the per-session SSE on re-show (stopSessionStream nulls _sessionStreamSessionId).
let _sessionStreamHiddenSid = null;
// Hidden-tab active-stream poll (Defect B continuation): while the tab is
// hidden we do NOT hold the persistent per-session SSE open (connection-pool
// budget — see #3992/#4151). But a server-initiated turn (self-wake / cron /
// restart hook) fans `server_turn_started` onto that channel, so a hidden tab
// would miss it and only reconcile on the next interaction ("过了15秒没弹出来").
// Bridge the gap with a lightweight poll of /api/session/status (a single
// short-lived GET, NOT a held connection) that attaches the live renderer when
// it sees an active_stream_id. Cleared on re-show (the real SSE takes over) and
// on session switch.
let _sessionStreamHiddenPollTimer = null;
let _sessionStreamHiddenPollSid = null;
// Bounded-retry budget for the hidden poll's "attach returned false → keep
// polling" path (PR #5266 follow-up gate). A never-current pane (multi-pane:
// another session stays on screen) would otherwise poll /api/session/status
// every 6s forever. Cap the consecutive-false retries per (sid, streamId); once
// the budget is exhausted, stop the hidden poll and rely on the normal
// session-switch / loadSession / visible-tab SSE to reattach later.
let _sessionStreamHiddenPollFalseStreamId = null;
let _sessionStreamHiddenPollFalseCount = 0;
const _SESSION_STREAM_HIDDEN_POLL_MAX_FALSE = 20; // ~2 min at the 6s cadence

// Attach the existing chat-stream renderer to a server-created stream. Shared
// by the `server_turn_started` SSE handler (visible tab) and the hidden-tab
// active-stream poll. Idempotent per (sid, streamId): bails if this tab is
// already rendering that stream. `recovered` routes through the reconnecting
// (replay) path so the renderer rebuilds from the run journal mid-flight.
//
// Returns true when this tab is now responsible for rendering the stream
// (either the renderer was attached, OR a renderer was already attached to
// the same (sid,streamId) — both are "stream is in good hands"). Returns
// false when the attach was NOT consummated — sid not on screen (multi-pane:
// the active pane is a different session) or input invalid or thrown — so
// the hidden-tab poll caller can keep polling and try again on a later tick
// (e.g. once the user switches the pane back to `sid`). Without this
// signal, a poll that fires while another session is in the current pane
// would attach nothing AND stop polling, leaving the turn invisible until
// the next user interaction.
function _attachServerInitiatedStream(sid, streamId, recovered) {
  let handedOff = false;
  try {
    streamId = String(streamId || '');
    if (!streamId) return false;
    const isCurrent = (typeof _isSessionCurrentPane === 'function')
      ? _isSessionCurrentPane(sid)
      : (S.session && S.session.session_id === sid);
    // Multi-pane edge: caller's sid is not the active pane. Don't attach to
    // a different pane's UI; tell the poll to keep trying.
    if (!isCurrent) return false;
    // Already rendering this exact stream — treat as success so the poll
    // stops cleanly (renderer owns the stream from here).
    if (S.activeStreamId === streamId) return true;
    const existingLive = (typeof LIVE_STREAMS !== 'undefined') ? LIVE_STREAMS[sid] : null;
    if (existingLive && existingLive.streamId === streamId) return true;
    S.busy = true;
    S.activeStreamId = streamId;
    if (S.session && S.session.session_id === sid) {
      S.session.active_stream_id = streamId;
      if (!S.session.pending_started_at) S.session.pending_started_at = Date.now()/1000;
    }
    if (typeof ensureLiveWorklogShell === 'function') ensureLiveWorklogShell();
    else if (typeof appendThinking === 'function') appendThinking();
    if (typeof updateSendBtn === 'function') updateSendBtn();
    if (typeof setComposerStatus === 'function') setComposerStatus('');
    if (typeof syncTopbar === 'function') syncTopbar();
    if (typeof startApprovalPolling === 'function') startApprovalPolling(sid);
    if (typeof startClarifyPolling === 'function') startClarifyPolling(sid);
    if (typeof attachLiveStream === 'function') {
      attachLiveStream(
        sid, streamId,
        (S.session && S.session.pending_attachments) || [],
        recovered ? {reconnecting: true} : {},
      );
      // The renderer now owns the stream. Any failure AFTER this point (e.g. a
      // post-attach UI refresh throwing) is NOT an attach failure — the stream is
      // in good hands, so the caller must not be told to keep polling/retrying.
      handedOff = true;
    }
    if (typeof renderSessionList === 'function') void renderSessionList();
    return true;
  } catch (_) {
    try { console.error('hidden-tab server-initiated attach failed', _); } catch (_e) {}
    // If the stream was already handed to the renderer, the attach DID succeed;
    // a later UI-refresh throw must not be reported as failure (would make the
    // poll keep retrying an already-attached stream).
    if (handedOff) return true;
    // Otherwise the attach threw mid-setup, leaving partial state (S.busy /
    // S.activeStreamId / S.session.active_stream_id were set before the DOM
    // calls). Clear it so the next hidden-poll tick doesn't see a stale
    // activeStreamId, exit early as "already attached", and wedge the turn
    // invisible with the composer stuck busy. Only clear when THIS pane/session
    // still owns the sid/streamId we were attaching (don't stomp a newer stream
    // that a concurrent path may have started).
    try {
      const stillOurs = (S.session && S.session.session_id === sid) &&
        (S.activeStreamId === streamId || (S.session && S.session.active_stream_id === streamId));
      if (stillOurs) {
        S.busy = false;
        S.activeStreamId = null;
        if (S.session) S.session.active_stream_id = null;
        if (typeof updateSendBtn === 'function') updateSendBtn();
        if (typeof syncTopbar === 'function') syncTopbar();
        if (typeof renderSessionList === 'function') void renderSessionList();
      }
    } catch (_e2) {}
    return false;
  }
}

// Poll /api/session/status (~6s) for an active stream while the tab is hidden.
// One short GET per tick — does not consume a persistent connection-pool slot.
// On hit, attach via the reconnecting/replay path (the turn is already
// mid-flight) and stop polling; the renderer owns it from here.
function _startHiddenActiveStreamPoll(sid) {
  if (!sid) return;
  _stopHiddenActiveStreamPoll();
  _sessionStreamHiddenPollSid = sid;
  const tick = () => {
    // Stop conditions: tab became visible (real SSE takes over), session
    // switched, or we're already rendering a stream.
    if (typeof document !== 'undefined' && !document.hidden) { _stopHiddenActiveStreamPoll(); return; }
    if (_sessionStreamHiddenPollSid !== sid) { _stopHiddenActiveStreamPoll(); return; }
    if (S.activeStreamId) return; // already rendering; wait it out
    try {
      fetch(_apiUrl('api/session/status?session_id=' + encodeURIComponent(sid)), {credentials: 'same-origin'})
        .then(r => r.ok ? r.json() : null)
        .then(d => {
          if (!d || _sessionStreamHiddenPollSid !== sid) return;
          const streamId = d.active_stream_id;
          if (streamId && S.activeStreamId !== String(streamId)) {
            // Server-initiated turn in flight while hidden → attach as replay.
            // Only stop polling when the attach actually consummated. In the
            // multi-pane edge where the active pane is a different session,
            // _attachServerInitiatedStream returns false (sid not on screen), so
            // we keep polling and re-try on a later tick — e.g. once the user
            // switches the active pane back to `sid`, or by then the turn has
            // finished and status returns active_stream_id=null naturally.
            const streamKey = String(streamId);
            // Reset the bounded-retry budget whenever the stream id changes (a new
            // server-initiated turn deserves a fresh set of retries).
            if (_sessionStreamHiddenPollFalseStreamId !== streamKey) {
              _sessionStreamHiddenPollFalseStreamId = streamKey;
              _sessionStreamHiddenPollFalseCount = 0;
            }
            const attached = _attachServerInitiatedStream(sid, streamId, true);
            if (attached) {
              _stopHiddenActiveStreamPoll();
            } else {
              // Bounded give-up: a never-current pane would poll forever otherwise.
              // Count consecutive false attaches for this stream id; once the
              // budget is spent, stop the hidden poll. A later session-switch /
              // loadSession / visible-tab SSE will reattach if the turn is still
              // live by then.
              _sessionStreamHiddenPollFalseCount += 1;
              if (_sessionStreamHiddenPollFalseCount >= _SESSION_STREAM_HIDDEN_POLL_MAX_FALSE) {
                _stopHiddenActiveStreamPoll();
              }
            }
          }
        })
        .catch(() => {});
    } catch (_) {}
  };
  _sessionStreamHiddenPollTimer = setInterval(tick, 6000);
  // Fire one immediately so a turn already running when we go hidden is caught
  // without waiting a full interval.
  tick();
}

function _stopHiddenActiveStreamPoll() {
  if (_sessionStreamHiddenPollTimer) { clearInterval(_sessionStreamHiddenPollTimer); _sessionStreamHiddenPollTimer = null; }
  _sessionStreamHiddenPollSid = null;
  // Reset the bounded-retry budget so a fresh poll never inherits a stale count.
  _sessionStreamHiddenPollFalseStreamId = null;
  _sessionStreamHiddenPollFalseCount = 0;
}

function _chatStreamActiveForSession(sid) {
  if (!sid) return false;
  if (typeof LIVE_STREAMS !== 'undefined' && LIVE_STREAMS[sid]) return true;
  return !!(
    S &&
    S.session &&
    S.session.session_id === sid &&
    S.activeStreamId
  );
}

function _suspendSessionStreamForLiveChat(sid) {
  if (!sid) return;
  if (_sessionStreamSessionId !== sid) return;
  _sessionStreamHiddenSid = sid;
  stopSessionStream();
}

function _resumeSessionStreamAfterLiveChat(sid) {
  if (!sid) return;
  setTimeout(() => {
    if (!S || !S.session || S.session.session_id !== sid) return;
    if (_chatStreamActiveForSession(sid)) return;
    _sessionStreamHiddenSid = null;
    startSessionStream(sid);
  }, 0);
}

function startSessionStream(sid) {
  if (!sid) return;
  // Already on this session? No-op (loadSession is a no-op when re-selecting
  // the same session; this defends against external re-callers).
  if (_sessionStreamSessionId === sid && _sessionEventSource) return;
  // While the visible conversation has a live token stream, /api/chat/stream
  // already carries bg_task_complete and terminal events for this turn. Keeping
  // /api/session/stream open at the same time burns one of Chrome's six
  // same-origin HTTP/1.1 sockets and can starve ordinary /api/session fetches.
  if (_chatStreamActiveForSession(sid)) {
    _sessionStreamHiddenSid = sid;
    return;
  }
  stopSessionStream();
  _sessionStreamSessionId = sid;
  // Visibility hook (install once) — mirror ensureSessionEventsSSE() pattern.
  // Capture the active session id into a dedicated var BEFORE closing, because
  // stopSessionStream() nulls _sessionStreamSessionId — so the reopen path can't
  // rely on it (that was the bug: the stream never reopened on tab re-show).
  if (typeof document !== 'undefined' && !document._hermesSessionStreamVisibilityHook) {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        _sessionStreamHiddenSid = _sessionStreamSessionId;
        stopSessionStream();
        // Tab went to background: don't hold the SSE, but bridge
        // server-initiated turns (self-wake / cron / restart) with the
        // lightweight status poll so they still render without interaction.
        // (stopSessionStream cleared any prior poll; start a fresh one for the
        // session we just hid.)
        if (_sessionStreamHiddenSid) _startHiddenActiveStreamPoll(_sessionStreamHiddenSid);
      } else if (_sessionStreamHiddenSid) {
        const resumeSid = _sessionStreamHiddenSid;
        _sessionStreamHiddenSid = null;
        void startSessionStream(resumeSid);
      }
    });
    document._hermesSessionStreamVisibilityHook = true;
  }
  // Don't open when tab is hidden — saves connection pool slots. Preserve the
  // pending session id so the visibility handler reopens it on re-show (a session
  // loaded/restored while the tab is already hidden must still reattach).
  if (typeof document !== 'undefined' && document.hidden) {
    _sessionStreamHiddenSid = sid;
    // Don't hold the SSE open while hidden, but DO bridge server-initiated
    // turns (self-wake / cron / restart) with a lightweight status poll so the
    // turn renders without waiting for the user to interact.
    _startHiddenActiveStreamPoll(sid);
    return;
  }
  // Tab is visible — the real SSE owns live-view; ensure no stale hidden poll.
  _stopHiddenActiveStreamPoll();
  try {
    // Report our last-known message_count so the server's on-subscribe
    // self-heal can detect a server-initiated turn (self-wake / cron / restart)
    // that started AND finished entirely inside an SSE gap — the fire-and-forget
    // `server_turn_started` was missed AND the run already cleared from
    // ACTIVE_RUNS by the time we reconnect, so the `server_turn_started` replay
    // finds nothing. When the server is ahead of this count it emits a
    // `session-updated` frame (handled below) and we incrementally sync. Use the
    // session's full message_count (same basis the server persists), NOT
    // S.messages.length, which is only the rendered tail window for long
    // sessions and would false-trigger a reload on every reconnect.
    let _knownCount = '';
    try {
      if (S.session && S.session.session_id === sid && Number.isFinite(Number(S.session.message_count))) {
        _knownCount = String(Number(S.session.message_count));
      }
    } catch (_) {}
    const _streamUrl = 'api/session/stream?session_id=' + encodeURIComponent(sid)
      + (_knownCount !== '' ? '&known_count=' + encodeURIComponent(_knownCount) : '');
    const es = new EventSource(_apiUrl(_streamUrl));
    _sessionEventSource = es;
    es.addEventListener('initial', () => { /* connection confirmed */ });
    es.addEventListener('bg_task_complete', e => {
      // Shared handler — same dedupe set as the in-turn STREAMS path.
      if (typeof _handleBgTaskCompleteEvent === 'function') {
        _handleBgTaskCompleteEvent(e, sid, {source: 'session'});
      }
    });
    // ── Visible-tab self-heal: a server-initiated turn finished during an SSE
    // gap ─────────────────────────────────────────────────────────────────
    // Distinct from `server_turn_started` (which attaches a LIVE stream): this
    // frame fires when the server detected, at our (re)subscribe, that a turn
    // started AND finished while our EventSource was momentarily down (so we
    // missed the live broadcast and the run already cleared). The turn is
    // persisted but our transcript is stale and there is NO live stream left to
    // attach. Incrementally sync via the SAME swap-in-place path #5189 uses for
    // the hidden-tab return (loadSession force + keepStaleUntilLoaded): the new
    // transcript replaces the old in a single render frame — NO clear+refetch,
    // so the #5177/#5189 blank-gap "jump" is not reintroduced.
    es.addEventListener('session-updated', e => {
      try {
        const d = JSON.parse(e.data || '{}');
        const evSid = d.session_id || sid;
        if (evSid !== sid) return;
        // Only act when this session is the one on screen and we're idle (no
        // live turn rendering — that path owns its own message updates).
        const isCurrent = (typeof _isSessionCurrentPane === 'function')
          ? _isSessionCurrentPane(sid)
          : (S.session && S.session.session_id === sid);
        if (!isCurrent) return;
        if (S.activeStreamId) return;
        // Re-check against our CURRENT known count — a concurrent load may have
        // already caught us up between the server's emit and now.
        const localCount = (S.session && S.session.session_id === sid && Number.isFinite(Number(S.session.message_count)))
          ? Number(S.session.message_count)
          : (Array.isArray(S.messages) ? S.messages.length : 0);
        const serverCount = Number(d.message_count);
        if (!Number.isFinite(serverCount) || serverCount <= localCount) return;
        if (typeof loadSession === 'function') {
          void loadSession(sid, {force: true, externalRefreshReason: 'session-updated', keepStaleUntilLoaded: true});
        }
      } catch (_) {}
    });
    // ── Defect B: live-view of server-initiated (Option Z) turns ──────────
    // The drain thread starts the wakeup turn server-side and the server
    // fans a `server_turn_started` {stream_id} frame onto this per-session
    // channel. No browser POSTed /api/chat/start, so nothing is attached to
    // that STREAMS[stream_id] yet. Attach the EXISTING chat-stream renderer
    // (attachLiveStream — the exact path /api/chat/start uses) to the
    // server-created stream so the open tab renders the turn live. Reuses
    // the one renderer; does NOT hand-roll a second one.
    es.addEventListener('server_turn_started', e => {
      try {
        const d = JSON.parse(e.data || '{}');
        const evSid = d.session_id || sid;
        const streamId = String(d.stream_id || '');
        if (!streamId || evSid !== sid) return;
        // `recovered` marks an on-subscribe replay from the server: the tab
        // (re)connected to /api/session/stream AFTER the original
        // fire-and-forget server_turn_started had already been broadcast, so
        // the live stream is mid-flight. Attach via the reconnecting (replay)
        // path so the renderer rebuilds from the run journal instead of
        // expecting token 0 (which would render a truncated turn). A fresh
        // (non-recovered) frame still attaches from the first token.
        const recovered = !!d.recovered;
        // Only drive the renderer when this session is the one on screen.
        const isCurrent = (typeof _isSessionCurrentPane === 'function')
          ? _isSessionCurrentPane(sid)
          : (S.session && S.session.session_id === sid);
        if (!isCurrent) return;
        // A turn is already rendering in this tab (user-initiated, or we
        // already attached to this very stream). attachLiveStream is
        // idempotent per (sid, streamId); bail if we're already on it.
        if (S.activeStreamId === streamId) return;
        const existingLive = (typeof LIVE_STREAMS !== 'undefined') ? LIVE_STREAMS[sid] : null;
        if (existingLive && existingLive.streamId === streamId) return;
        // Mirror the loadSession reattach setup. For a fresh frame the turn
        // renders from its first token; for a recovered (replay) frame
        // attachLiveStream reconstructs the in-progress stream.
        S.busy = true;
        S.activeStreamId = streamId;
        if (S.session && S.session.session_id === sid) {
          S.session.active_stream_id = streamId;
          if (typeof d.pending_started_at === 'number') S.session.pending_started_at = d.pending_started_at;
          else if (!S.session.pending_started_at) S.session.pending_started_at = Date.now()/1000;
        }
        if (typeof ensureLiveWorklogShell === 'function') ensureLiveWorklogShell();
        else if (typeof appendThinking === 'function') appendThinking();
        if (typeof updateSendBtn === 'function') updateSendBtn();
        if (typeof setComposerStatus === 'function') setComposerStatus('');
        if (typeof syncTopbar === 'function') syncTopbar();
        if (typeof startApprovalPolling === 'function') startApprovalPolling(sid);
        if (typeof startClarifyPolling === 'function') startClarifyPolling(sid);
        if (typeof attachLiveStream === 'function') {
          attachLiveStream(
            sid, streamId,
            (S.session && S.session.pending_attachments) || [],
            recovered ? {reconnecting: true} : {},
          );
        }
        if (typeof renderSessionList === 'function') void renderSessionList();
      } catch (_) {}
    });
    es.onerror = () => {
      // Browser already auto-reconnects EventSource on most transient
      // failures. We only intervene if the connection has been closed for
      // good (readyState === 2) — schedule a one-shot re-open after 5s.
      if (es.readyState === 2 && _sessionStreamSessionId === sid) {
        if (_sessionStreamReconnectTimer) clearTimeout(_sessionStreamReconnectTimer);
        // The CLOSED EventSource (readyState === 2) will never reconnect on
        // its own, and startSessionStream's top guard
        // (`_sessionStreamSessionId === sid && _sessionEventSource`) would
        // short-circuit the re-open while this dead object is still pinned.
        // Drop our reference (and close it for good measure) so the timer's
        // startSessionStream() reaches stopSessionStream() and builds a FRESH
        // EventSource instead of reusing the closed one. Only clear if `es`
        // is still the active source — a newer connection may have replaced
        // it in the interim (stale onerror from a superseded stream), in
        // which case we must not stomp the live one.
        if (_sessionEventSource === es) {
          try { if(es.readyState!==2)es.close(); } catch (_) {}
          _sessionEventSource = null;
        }
        _sessionStreamReconnectTimer = setTimeout(() => {
          _sessionStreamReconnectTimer = null;
          if (_sessionStreamSessionId === sid) startSessionStream(sid);
        }, 5000);
      }
    };
  } catch(_) {
    // EventSource ctor threw — silently disabled; the in-turn STREAMS path
    // still works for events that fire during an active turn.
    _sessionEventSource = null;
  }
}

function stopSessionStream() {
  if (_sessionStreamReconnectTimer) { clearTimeout(_sessionStreamReconnectTimer); _sessionStreamReconnectTimer = null; }
  _stopHiddenActiveStreamPoll();
  if (_sessionEventSource) {
    try { if(_sessionEventSource.readyState!==2)_sessionEventSource.close(); } catch(_){}
    _sessionEventSource = null;
  }
  _sessionStreamSessionId = null;
}

// Shared bg_task_complete handler — invoked from BOTH the in-turn STREAMS
// channel (legacy path, still kept as defense-in-depth) AND the session-
// scoped channel (Option X primary path). Dedupes by (session_id, event_id)
// via the Map+TTL ring buffer declared at the top of this module.
// Events without `event_id` are ignored — the server contract guarantees one
// on every completion emit, so a missing key signals a malformed or replayed
// payload we should not surface or ack.
// PR (c) UX surface: post-dedupe the handler marks the session viewed (when
// the session pane is current and the doc is visible+focused), then runs the
// T4 drop-when-focused gate; only out-of-focus or off-pane completions spawn
// a toast. The diagnostic ack POST still fires for both focused and
// unfocused viewers so the server receives the delivery/cleanup signal;
// the focus gate suppresses UI noise only.
function _handleBgTaskCompleteEvent(e, expectedSid, opts) {
  try {
    const d = JSON.parse(e.data || '{}');
    const sid = d.session_id || expectedSid;
    if (sid !== expectedSid) return;
    const evt_id = d.event_id ? String(d.event_id) : '';
    if (!evt_id) return;  // server contract requires event_id; ignore otherwise
    if (_bgTaskCompleteRingBufferAdd(sid, evt_id)) return;  // duplicate
    const pid = String(d.task_id || '');
    const _viewed = typeof _isSessionActivelyViewed === 'function' && _isSessionActivelyViewed(sid);
    if (_viewed) {
      try { _markSessionViewed(sid, (S&&S.session&&S.session.session_id===sid)?(S.session.message_count??(S.messages&&S.messages.length)??0):0); } catch(_){}
      try { if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(sid); } catch(_){}
    } else {
      // T4 drop-when-focused: suppress toast only; ack below still fires.
      try {
        const tid = (d.task_id || '').slice(0, 8) || '?';
        const tail = d.summary ? `: ${String(d.summary).slice(0, 80)}` : '';
        showToast(`Task ${tid} done${tail}`, 2600);
      } catch (_) {}
    }

    // Fire-and-forget ack (diagnostic only — Option Z made this a no-op for
    // state. The agent wakeup is now started SERVER-SIDE by the drain thread
    // in api/background_process._process_one → start_session_turn; the
    // browser is no longer in the wakeup path at all.)
    try {
      fetch(_apiUrl('api/bg-task-complete-ack'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify({session_id: sid, task_id: pid, event_id: evt_id}),
      }).catch(() => {});
    } catch(_) {}

    // Option Z PIVOT: the browser NO LONGER re-POSTs the chat-start endpoint
    // to wake the agent. Server-side wakeup is the PRIMARY mechanism — the
    // drain thread starts the turn directly (no tab required), so the
    // closed-tab case works (parity with CLI/Telegram). The per-session SSE
    // channel this handler is wired into is DEMOTED to a pure live-view
    // layer: if a tab is open the server-initiated turn streams live via the
    // existing chat-stream EventSource; if the tab is closed the turn still
    // runs server-side and the result is persisted to the session store.
    // The user-facing toast + drop-when-focused gate land in PR (c).
  } catch(_) {}
}

Object.assign(HermesMessages, {
  startSessionStream,
  stopSessionStream,
});
