import { _isCliSession } from './session-source.js';
import { sessionRunRegistry } from './session-run-registry.js';
import {
  _cronCompletionUnreadMetaForSession,
  _cronMarkerProfileMatchesActive,
  _forgetObservedStreamingSession,
  _getSessionObservedStreaming,
  _isSessionActivelyViewedForList,
  _markSessionCompletionUnread,
  _rememberObservedStreamingSession,
  _setSessionViewedCount,
} from './session-unread.js';
import { sidebarStateBindings } from './sidebar-store.js';

const _sessionListSnapshotById=sessionRunRegistry.snapshotById;
const _sessionListSourceById=sessionRunRegistry.sourceById;
const _sessionStreamingById=sessionRunRegistry.streamingById;

function _isSessionLocallyStreaming(s) {
  if (!s || !s.session_id) return false;
  const isActive = S.session && s.session_id === S.session.session_id;
  // For the active session, rely on S.busy to indicate an ongoing stream.
  // INFLIGHT entries for non-active sessions are artifacts of interrupted
  // streams (page refresh, network disconnect, gateway restart) where
  // `delete INFLIGHT[sid]` was never reached — they should NOT cause the
  // sidebar spinner to appear on completed sessions. (#2066)
  return isActive && Boolean(S.busy);
}

function _isSessionEffectivelyStreaming(s) {
  return Boolean(s && (
    s.is_streaming ||
    _hasPendingUserMessageSignal(s) ||
    _isSessionLocallyStreaming(s)
  ));
}

function _hasPendingUserMessageSignal(s) {
  return Boolean(s && (s.pending_user_message || s.has_pending_user_message));
}

function _isServerIdleSessionRow(s) {
  return Boolean(s && s.session_id && !s.is_streaming && !s.active_stream_id && !s.pending_user_message && !s.has_pending_user_message && !s.pending_started_at);
}

function _reconcileActiveSessionIdleStateFromList(serverRows) {
  if (!S || !S.session || !S.session.session_id) return false;
  if (!Array.isArray(serverRows)) return false;
  const sid=S.session.session_id;
  // #4354: clear a stuck indicator when the server reports idle — server
  // is_streaming/active_stream_id is authoritative. BUT skip the ONE session
  // that is actively mid-send (#2689 start-race): during the /api/chat/start
  // round-trip the server row is still idle while the client owns the optimistic
  // turn, so reconciling it here would blank the just-sent bubble + queue a
  // spurious force-reload. A long-hung session has _sendInProgress===false, so
  // it still gets unstuck — only the in-flight start window is protected.
  if (typeof _sendInProgress !== 'undefined' && _sendInProgress && sid === _sendInProgressSid) return false;
  const serverRow=serverRows.find(s=>s&&s.session_id===sid);
  if (!serverRow) return false;
  if (!_isServerIdleSessionRow(serverRow)) return false;
  let changed=false;
  if (S.busy) { S.busy=false; changed=true; }
  if (S.activeStreamId) { S.activeStreamId=null; changed=true; }
  if (INFLIGHT&&INFLIGHT[sid]) {
    delete INFLIGHT[sid];
    if (typeof clearInflightState==='function') clearInflightState(sid);
    changed=true;
  }
  if (S.session) {
    S.session.active_stream_id=null;
    S.session.pending_user_message=null;
  }
  _sessionStreamingById.set(sid, false);
  _forgetObservedStreamingSession(sid);
  if (typeof hideApprovalCard==='function') hideApprovalCard(true);
  if (typeof hideLiveRunStatus==='function') hideLiveRunStatus(sid);
  if (typeof clearLiveToolCards==='function') clearLiveToolCards();
  if (changed&&typeof updateSendBtn==='function') updateSendBtn();
  return changed;
}

function _purgeStaleInflightEntries() {
  // Clean up INFLIGHT entries for sessions the server confirms are NOT
  // streaming. This prevents the in-memory cache from growing unbounded
  // when streams end abnormally. (#2066)  Additionally, any INFLIGHT entry
  // whose session id is no longer present in the current _allSessions list
  // (deleted / archived / filtered out) is also removed so that ghost entries
  // from deleted sessions do not accumulate. (#2092)
  if (typeof INFLIGHT !== 'object' || !INFLIGHT) return;
  const sessionsById = new Map();
  if (Array.isArray(sidebarStateBindings._allSessions)) {
    for (const s of sidebarStateBindings._allSessions) {
      if (s && s.session_id) sessionsById.set(s.session_id, s);
    }
  }
  const sourceById = typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.get === 'function'
    ? _sessionListSourceById
    : null;
  const currentSidebarSource = typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string'
    ? sidebarStateBindings._allSessionsScope.sidebarSource
    : null;
  for (const sid of Object.keys(INFLIGHT)) {
    // #4354: purge stale INFLIGHT even for a hung/idle session, BUT skip the one
    // session actively mid-send (#2689 start-race) — during /api/chat/start the
    // server row is briefly idle while the client owns the optimistic INFLIGHT
    // entry; purging it here would drop the in-flight turn's local state.
    if (typeof _sendInProgress !== 'undefined' && _sendInProgress && sid === _sendInProgressSid) {
      continue;
    }
    if (!sessionsById.has(sid)) {
      const knownSource = sourceById ? sourceById.get(sid) : null;
      if (currentSidebarSource && (!knownSource || knownSource !== currentSidebarSource)) {
        continue;
      }
      // Session is absent from _allSessions — it was deleted / archived /
      // filtered and can never stream again, so drop the entry.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
      continue;
    }
    const s = sessionsById.get(sid);
    if (!s.is_streaming) {
      // Session exists but is not streaming — purge it.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
    }
    // Sessions that exist and are still streaming are preserved.
  }
}

function _rememberSessionListSource(s, sid = null, allowScopeFallback = true) {
  const resolvedSid = sid || (s && s.session_id);
  if (!resolvedSid) return;
  let source = null;
  if (s && typeof _isCliSession === 'function') {
    source = _isCliSession(s) ? 'cli' : 'webui';
  }
  if (!source && Array.isArray(sidebarStateBindings._allSessions)) {
    const cached = sidebarStateBindings._allSessions.find(item => item && item.session_id === resolvedSid);
    if (cached && typeof _isCliSession === 'function') {
      source = _isCliSession(cached) ? 'cli' : 'webui';
    }
  }
  if (!source
    && allowScopeFallback
    && typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string') {
    source = sidebarStateBindings._allSessionsScope.sidebarSource;
  }
  if (source
    && typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.set === 'function') {
    _sessionListSourceById.set(resolvedSid, source);
  }
}

function _rememberRenderedStreamingState(s, isStreaming) {
  if (!s || !s.session_id || !isStreaming) return;
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s);
  _sessionStreamingById.set(s.session_id, true);
  _rememberObservedStreamingSession(s);
}

function _rememberRenderedSessionSnapshot(s) {
  if (!s || !s.session_id) return;
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s);
  const previous = _sessionListSnapshotById.get(s.session_id);
  if (previous) return;
  _sessionListSnapshotById.set(s.session_id, {
    message_count: Number(s.message_count || 0),
    last_message_at: Number(s.last_message_at || 0),
  });
}

function _markSessionCompletedInList(session, previousSid = null) {
  if (!session || !Array.isArray(sidebarStateBindings._allSessions)) return false;
  const finalSid = session.session_id || previousSid;
  if (!finalSid) return false;
  const finalIdx = sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === finalSid);
  const previousIdx = previousSid ? sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === previousSid) : -1;
  const idx = finalIdx >= 0 ? finalIdx : previousIdx;
  if (idx < 0) return false;
  const {messages: _messages, tool_calls: _toolCalls, ...sessionMeta} = session;
  const messageCount = Number(
    session.message_count != null
      ? session.message_count
      : (Array.isArray(session.messages) ? session.messages.length : (sidebarStateBindings._allSessions[idx].message_count || 0))
  );
  const lastMessageAt = Number(session.last_message_at || session.updated_at || sidebarStateBindings._allSessions[idx].last_message_at || 0);
  sidebarStateBindings._allSessions[idx] = {
    ...sidebarStateBindings._allSessions[idx],
    ...sessionMeta,
    session_id: finalSid,
    message_count: messageCount,
    last_message_at: lastMessageAt,
    active_stream_id: null,
    pending_user_message: null,
    pending_started_at: null,
    is_streaming: false,
  };
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(sidebarStateBindings._allSessions[idx], finalSid);
  _sessionStreamingById.set(finalSid, false);
  _forgetObservedStreamingSession(finalSid);
  if (previousSid && previousSid !== finalSid) {
    for (let i = sidebarStateBindings._allSessions.length - 1; i >= 0; i--) {
      if (i !== idx && sidebarStateBindings._allSessions[i] && sidebarStateBindings._allSessions[i].session_id === previousSid) {
        sidebarStateBindings._allSessions.splice(i, 1);
      }
    }
    _sessionStreamingById.delete(previousSid);
    _forgetObservedStreamingSession(previousSid);
    _sessionListSnapshotById.delete(previousSid);
    _sessionListSourceById.delete(previousSid);
  }
  _sessionListSnapshotById.set(finalSid, {
    message_count: messageCount,
    last_message_at: lastMessageAt,
  });
  return true;
}

function _markPollingCompletionUnreadTransitions(sessions) {
  if (!Array.isArray(sessions)) return;
  const seen = new Set();
  const sourceById = typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.get === 'function'
    && typeof _sessionListSourceById.keys === 'function'
    && typeof _sessionListSourceById.delete === 'function'
    ? _sessionListSourceById
    : new Map();
  const currentSidebarSource = typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string'
    ? sidebarStateBindings._allSessionsScope.sidebarSource
    : null;
  for (const s of sessions) {
    if (!s || !s.session_id) continue;
    const sid = s.session_id;
    seen.add(sid);
    if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s, sid);
    const wasStreaming = _sessionStreamingById.get(sid);
    const isStreaming = _isSessionEffectivelyStreaming(s);
    const previousSnapshot = _sessionListSnapshotById.get(sid);
    const observedStreaming = _getSessionObservedStreaming()[sid];
    const messageCount = Number(s.message_count || 0);
    const lastMessageAt = Number(s.last_message_at || 0);
    const hasServerRunSignal=Boolean(s.is_streaming||_hasPendingUserMessageSignal(s));
    const canMarkCompletedStream=Boolean(hasServerRunSignal||previousSnapshot||observedStreaming);
    const completedObservedStream = canMarkCompletedStream&&wasStreaming === true && !isStreaming;
    const completedWithNewMessages = Boolean(
      (previousSnapshot || observedStreaming)
      && !isStreaming
      && (
        messageCount > Number((previousSnapshot || observedStreaming).message_count || 0)
        || lastMessageAt > Number((previousSnapshot || observedStreaming).last_message_at || 0)
      )
    );
    const completedPersistedObservedStream = Boolean(observedStreaming && !isStreaming);
    if (completedObservedStream || completedPersistedObservedStream || completedWithNewMessages) {
      if (!_isSessionActivelyViewedForList(sid)) {
        // Tag cron session-list markers with source+profile so profile-switch
        // reset can clear only inactive-profile cron dots (#5960 / #5975 re-gate).
        const meta = (typeof _cronCompletionUnreadMetaForSession === 'function')
          ? _cronCompletionUnreadMetaForSession(s)
          : null;
        // Defense: never re-create a cron unread for a non-active profile while
        // the sidebar is single-profile (stale pre-switch payloads).
        const allProfilesOn = (typeof sidebarStateBindings._showAllProfiles !== 'undefined' && !!sidebarStateBindings._showAllProfiles);
        if (
          meta
          && meta.source === 'cron'
          && meta.profile
          && !allProfilesOn
          && typeof _cronMarkerProfileMatchesActive === 'function'
          && !_cronMarkerProfileMatchesActive(meta.profile, (typeof S !== 'undefined' && S && S.activeProfile) || 'default')
        ) {
          // Skip mark for inactive-profile cron row.
        } else {
          _markSessionCompletionUnread(sid, s.message_count, meta);
        }
      } else {
        // Sync viewed count so we don't flag stale unread on tab switch (#3020)
        _setSessionViewedCount(sid, messageCount);
      }
    }
    _sessionStreamingById.set(sid, isStreaming);
    if (isStreaming) {
      _rememberObservedStreamingSession(s);
    } else {
      _forgetObservedStreamingSession(sid);
    }
    _sessionListSnapshotById.set(sid, {
      message_count: messageCount,
      last_message_at: lastMessageAt,
    });
  }
  const staleRuntimeStateSids = new Set([
    ...Array.from(_sessionStreamingById.keys()),
    ...Array.from(_sessionListSnapshotById.keys()),
    ...Array.from(sourceById.keys()),
  ]);
  for (const sid of staleRuntimeStateSids) {
    if (seen.has(sid)) continue;
    const knownSource = sourceById.get(sid);
    if (currentSidebarSource && (!knownSource || knownSource !== currentSidebarSource)) continue;
    _sessionStreamingById.delete(sid);
    _sessionListSnapshotById.delete(sid);
    sourceById.delete(sid);
  }
}



export const sessionRunState=Object.freeze({
  isStreaming:_isSessionEffectivelyStreaming,
  purgeStaleInflight:_purgeStaleInflightEntries,
  reconcileActiveIdle:_reconcileActiveSessionIdleStateFromList,
  markCompleted:_markSessionCompletedInList,
  recordPollingTransitions:_markPollingCompletionUnreadTransitions,
});

export {
  _isServerIdleSessionRow,
  _isSessionEffectivelyStreaming,
  _isSessionLocallyStreaming,
  _markPollingCompletionUnreadTransitions,
  _markSessionCompletedInList,
  _purgeStaleInflightEntries,
  _reconcileActiveSessionIdleStateFromList,
  _rememberRenderedSessionSnapshot,
  _rememberRenderedStreamingState,
  _rememberSessionListSource,
};
