import { loadSession } from './lifecycle.js';
import { _isCliSession } from './message-loading.js';
import { _messageComparableText } from './message-timeline.js';
import { _sessionListSnapshotById, _sessionListSourceById, _sessionStreamingById, sessionStateStoreBindings } from './session-state-store.js';
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
import { sidebarStateBindings } from './sidebar-state.js';
import { _deferActiveSessionExternalRefresh, refreshActiveSessionIfExternallyUpdated } from './session-list.js';
import { renderSessionListFromCache } from './sidebar-renderer.js';

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
  if (changed&&typeof _scheduleActiveSessionIdleReload==='function') _scheduleActiveSessionIdleReload(sid);
  return changed;
}

function _scheduleActiveSessionIdleReload(sid) {
  if(!sid) return;
  setTimeout(async () => {
    if(!S||!S.session||S.session.session_id !== sid) return;
    // #5409: skip idle reload while any loadSession() is in flight — avoids
    // a race where the idle reload overwrites sessionStateStoreBindings._loadingSessionId and silently
    // cancels an in-progress session switch (most visible on iOS PWA with
    // large sessions where Phase 1 metadata fetch is slow).
    if(typeof sessionStateStoreBindings._loadingSessionId !== 'undefined' && sessionStateStoreBindings._loadingSessionId) return;
    if(S.busy || S.activeStreamId) return;
    if(typeof _isMessageReaderUnpinned==='function'&&_isMessageReaderUnpinned()){
      _deferActiveSessionExternalRefresh('idle-reconcile');
      return;
    }
    try{
      // Avoid an unconditional same-session force reload the moment streaming
      // settles. On mobile PWA this produces a visible end-of-turn flash and can
      // briefly restore the pane with stale layout geometry. Reconcile against
      // server metadata for the just-finished active turn first
      // (ignoreStreamJustFinished bypasses only the post-stream cooldown; the
      // reconcile still reloads ONLY when the message count actually changed).
      // The 'idle-reconcile' reason is non-'poll', so it coexists with the
      // #3916/#4195 poll-only external gate without bypassing it. Preserve the
      // original forced reload as a fallback when the probe request itself fails.
      const outcome = await refreshActiveSessionIfExternallyUpdated('idle-reconcile', {
        ignoreStreamJustFinished: true,
      });
      if(outcome === 'failed'){
        await loadSession(sid, {force:true, externalRefreshReason:'idle-reconcile'});
      }
    }catch(_){}
  },0);
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
  if (!session || !Array.isArray(sidebarStateBindings._allSessions)) return;
  const finalSid = session.session_id || previousSid;
  if (!finalSid) return;
  const finalIdx = sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === finalSid);
  const previousIdx = previousSid ? sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === previousSid) : -1;
  const idx = finalIdx >= 0 ? finalIdx : previousIdx;
  if (idx < 0) return;
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
  renderSessionListFromCache();
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


function _inflightHasVisibleLiveState(inflight) {
  if (!inflight || typeof inflight !== 'object') return false;
  if (String(inflight.lastAssistantText || '').trim()) return true;
  if (String(inflight.lastReasoningText || '').trim()) return true;
  if (String(inflight.liveTurnHtml || '').trim()) return true;
  if (Array.isArray(inflight.toolCalls) && inflight.toolCalls.length) return true;
  if (Array.isArray(inflight.activityBurstAnchors) && inflight.activityBurstAnchors.length) return true;
  if (Array.isArray(inflight.messages)) {
    return inflight.messages.some((msg) => {
      if (!msg) return false;
      if (msg.role === 'user') return Boolean(_messageComparableText(msg));
      if (msg.role !== 'assistant') return false;
      const content = msg.content;
      if (typeof content === 'string') return content.trim();
      if (Array.isArray(content)) return content.length > 0;
      return Boolean(content);
    });
  }
  return false;
}

function _serverLiveSnapshotToolId(tc){
  return String(tc&&(tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'')||'').trim();
}

function _serverLiveSnapshotInflight(snapshot, uploaded){
  if(!snapshot||typeof snapshot!=='object') return null;
  const rawMessages=Array.isArray(snapshot.messages)?snapshot.messages:[];
  const messages=rawMessages
    .filter(m=>m&&m.role)
    .map(m=>({...m,_live:m._live!==false,_journal_snapshot:true}));
  const rawToolCalls=Array.isArray(snapshot.tool_calls)?snapshot.tool_calls:[];
  const toolCalls=rawToolCalls
    .filter(tc=>tc&&tc.name)
    .map(tc=>{
      const next={...tc,_live:true,_journal_snapshot:true};
      const tid=_serverLiveSnapshotToolId(next);
      if(tid&&!next.tid) next.tid=tid;
      return next;
    });
  let lastAssistantText=String(snapshot.last_assistant_text||snapshot.lastAssistantText||'');
  let lastReasoningText=String(snapshot.last_reasoning_text||snapshot.lastReasoningText||'');
  const lastLiveAssistant=[...messages].reverse().find(m=>m&&m.role==='assistant'&&m._live);
  if(lastLiveAssistant){
    if(!lastAssistantText&&typeof lastLiveAssistant.content==='string') lastAssistantText=lastLiveAssistant.content;
    if(!lastReasoningText&&typeof lastLiveAssistant.reasoning==='string') lastReasoningText=lastLiveAssistant.reasoning;
  }
  if((lastAssistantText||lastReasoningText)&&!lastLiveAssistant){
    messages.push({
      role:'assistant',
      content:lastAssistantText,
      reasoning:lastReasoningText||undefined,
      _live:true,
      _journal_snapshot:true,
    });
  }
  const replayAfterSeq=Number(snapshot.last_seq||0);
  const activityBurstAnchors=Array.isArray(snapshot.activity_burst_anchors)
    ? snapshot.activity_burst_anchors
    : (Array.isArray(snapshot.activityBurstAnchors)?snapshot.activityBurstAnchors:[]);
  const anchorActivityScene=(snapshot.anchor_activity_scene&&snapshot.anchor_activity_scene.version==='activity_scene_v1')
    ? snapshot.anchor_activity_scene
    : ((snapshot.anchorActivityScene&&snapshot.anchorActivityScene.version==='activity_scene_v1')?snapshot.anchorActivityScene:null);
  const hasAnchorActivityScene=!!(anchorActivityScene&&Array.isArray(anchorActivityScene.activity_rows)&&anchorActivityScene.activity_rows.length);
  if(!messages.length&&!toolCalls.length&&!lastAssistantText&&!lastReasoningText&&!hasAnchorActivityScene) return null;
  return {
    streamId:String(snapshot.stream_id||snapshot.streamId||''),
    messages,
    uploaded:Array.isArray(uploaded)?[...uploaded]:[],
    toolCalls,
    todos:null,
    todoStateMeta:null,
    reattach:true,
    journalSnapshot:true,
    lastAssistantText,
    lastReasoningText,
    lastRunJournalSeq:Number.isFinite(replayAfterSeq)?Math.max(0,replayAfterSeq):0,
    lastRunJournalEventId:String(snapshot.last_event_id||snapshot.lastEventId||''),
    anchorActivityScene,
    currentActivityBurstId:Number(snapshot.current_activity_burst_id||snapshot.currentActivityBurstId||0)||0,
    currentLiveSegmentSeq:Number(snapshot.current_live_segment_seq||snapshot.currentLiveSegmentSeq||0)||0,
    activityBurstAnchors,
  };
}

function _selectLiveRecoveryInflight(localInflight, serverLiveSnapshot, activeStreamId){
  if(!serverLiveSnapshot) return localInflight||null;
  if(!localInflight||!_inflightHasVisibleLiveState(localInflight)) return serverLiveSnapshot;

  // The run journal owns the Worklog projection. A same-stream browser tail
  // wins only when it advanced after the metadata snapshot was read.
  const requestedActiveId=String(activeStreamId||'').trim();
  const localId=String(localInflight.streamId||'').trim();
  const serverId=String(serverLiveSnapshot.streamId||'').trim();
  const activeId=requestedActiveId||serverId;
  const selectDurableSnapshot=()=>{
    if(activeId&&localId===activeId&&Array.isArray(localInflight.todos)&&localInflight.todoStateMeta){
      return {...serverLiveSnapshot,todos:localInflight.todos,todoStateMeta:localInflight.todoStateMeta};
    }
    return serverLiveSnapshot;
  };
  if(requestedActiveId&&serverId&&serverId!==requestedActiveId){
    return localId===requestedActiveId?localInflight:null;
  }
  if(activeId&&localId!==activeId) return selectDurableSnapshot();

  const localSeq=Math.max(0,Number(localInflight.lastRunJournalSeq)||0);
  const serverSeq=Math.max(0,Number(serverLiveSnapshot.lastRunJournalSeq)||0);
  return serverSeq>=localSeq?selectDurableSnapshot():localInflight;
}

function _anchorActivitySceneStreamId(scene){
  if(!scene||typeof scene!=='object') return '';
  const identity=scene.identity&&typeof scene.identity==='object'?scene.identity:null;
  return String(scene.stream_id||scene.streamId||(identity&&(identity.stream_id||identity.streamId))||'').trim();
}

function _anchorActivitySceneMatchesStream(scene, activeStreamId){
  const activeId=String(activeStreamId||'').trim();
  if(!activeId) return true;
  const sceneId=_anchorActivitySceneStreamId(scene);
  return !sceneId||sceneId===activeId;
}

function _runtimeJournalAnchorActivitySceneForSession(sid, activeStreamId){
  const inflight=INFLIGHT&&sid?INFLIGHT[sid]:null;
  if(inflight&&inflight.anchorActivityScene&&inflight.anchorActivityScene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(inflight.anchorActivityScene, activeStreamId)){
    return inflight.anchorActivityScene;
  }
  const snapshot=S.session&&S.session.runtime_journal_snapshot;
  const scene=snapshot&&(snapshot.anchor_activity_scene||snapshot.anchorActivityScene);
  return scene&&scene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(scene, activeStreamId)?scene:null;
}

function _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid){
  if(!activeStreamId||typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneSnapshotForStream!=='function') return false;
  const scene=_runtimeJournalAnchorActivitySceneForSession(sid, activeStreamId);
  if(!scene) return false;
  return !!window._renderLiveAnchorActivitySceneSnapshotForStream(activeStreamId, scene, sid);
}

export const sessionRuntime=Object.freeze({
  isStreaming:_isSessionEffectivelyStreaming,
  purgeStaleInflight:_purgeStaleInflightEntries,
  reconcileActiveIdle:_reconcileActiveSessionIdleStateFromList,
  markCompleted:_markSessionCompletedInList,
  recordPollingTransitions:_markPollingCompletionUnreadTransitions,
  hasVisibleLiveState:_inflightHasVisibleLiveState,
  fromServerSnapshot:_serverLiveSnapshotInflight,
  selectLiveRecovery:_selectLiveRecoveryInflight,
  renderAnchorScene:_renderRuntimeJournalAnchorActivityScene,
});

export {
  _inflightHasVisibleLiveState,
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
  _renderRuntimeJournalAnchorActivityScene,
  _selectLiveRecoveryInflight,
  _serverLiveSnapshotInflight,
};
