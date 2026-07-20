import { _inflightHasVisibleLiveState, _renderRuntimeJournalAnchorActivityScene, _selectLiveRecoveryInflight, _serverLiveSnapshotInflight } from './session-live-recovery.js';
import { sessionLoadState } from './session-load-state.js';
import { _dropCurrentTurnAssistantMessages, _ensureInflightLiveAssistantMessage, _hasCurrentTailUserDuplicate, _mergeInflightTailMessages, _prepareRunningLiveTail, _projectInflightMessagesForActivityBursts } from './current-turn-transcript.js';
import { _deferWorkspaceRefreshForSession } from './session-post-load.js';
import { _ensureMessagesLoaded } from './transcript-loading.js';

// #2971 (Greptile P1 r3377162160): loadSession() tears down the live
// per-session SSE at the top via stopSessionStream() (line ~754), but only the
// success path re-arms it via startSessionStream() (line ~875). Every
// early-return exit (fetch error, auth-redirect undefined) — and the
// same-session no-op guard, which returns BEFORE the teardown — could leave
// the session the user actually remains on with a permanently null
// EventSource, silently dropping bg_task_complete delivery until a full page
// reload or a forced loadSession. This helper re-arms the stream for whatever
// session is currently on screen (S.session). startSessionStream() is
// idempotent — it no-ops when already live for that sid (top guard
// `_sessionStreamSessionId === sid && _sessionEventSource`) — so this never
// double-arms the success path, which arms the *newly assigned* S.session
// only after this point.
function _rearmActiveSessionStream(){
  if(typeof startSessionStream!=='function') return;
  const activeSid = S.session ? S.session.session_id : null;
  if(activeSid) startSessionStream(activeSid);
}


async function _restoreLoadedSession(ctx){
  const {sid,_keepStaleUntilLoaded,_loadGeneration,_isCurrentLoad,sameSessionForceReload}=ctx;
  let activeStreamId=ctx.activeStreamId;
  function _mergePendingSessionMessage(session,messages){
    if(!Array.isArray(messages)) return false;
    const pendingMsg=typeof getPendingSessionMessage==='function'?getPendingSessionMessage(session,messages):null;
    if(!pendingMsg) return false;
    const liveAssistantIdx=messages.findIndex(m=>m&&m.role==='assistant'&&m._live);
    const currentTurnMessages=liveAssistantIdx>=0?messages.slice(0,liveAssistantIdx):messages;
    if(_hasCurrentTailUserDuplicate(currentTurnMessages,pendingMsg)) return false;
    if(liveAssistantIdx>=0) messages.splice(liveAssistantIdx,0,pendingMsg);
    else messages.push(pendingMsg);
    return true;
  }

  // Phase 2a: If session is streaming, restore the persisted transcript first,
  // then merge the local INFLIGHT live tail. INFLIGHT is a recovery tail, not a
  // complete transcript; treating it as the full source makes long sessions look
  // like they lost history after switching away and back.
  if(!INFLIGHT[sid]&&activeStreamId&&typeof loadInflightState==='function'){
    const stored=loadInflightState(sid, activeStreamId);
    if(stored){
      INFLIGHT[sid]={
        streamId:String(stored.streamId||''),
        messages:Array.isArray(stored.messages)&&stored.messages.length?stored.messages:[],
        uploaded:Array.isArray(stored.uploaded)?stored.uploaded:[],
        toolCalls:Array.isArray(stored.toolCalls)?stored.toolCalls:[],
        // Phase 2: restore the live todo snapshot from persisted INFLIGHT
        // so the panel does not flicker to empty when a mid-stream
        // browser reload reattaches before the next `todo_state` event
        // fires.  Both fields are optional; missing values fall back to
        // cold-load via session.todo_state.
        todos:Array.isArray(stored.todos)?stored.todos:null,
        todoStateMeta:stored.todoStateMeta||null,
        reattach:true,
        lastAssistantText:String(stored.lastAssistantText||''),
        lastReasoningText:String(stored.lastReasoningText||''),
        lastRunJournalSeq:Number(stored.lastRunJournalSeq||0)||0,
        lastRunJournalEventId:String(stored.lastRunJournalEventId||''),
        journalReplayFromStart:!!stored.journalReplayFromStart,
        anchorActivityScene:(stored.anchorActivityScene&&stored.anchorActivityScene.version==='activity_scene_v1')?stored.anchorActivityScene:null,
        currentActivityBurstId:Number(stored.currentActivityBurstId||0)||0,
        currentLiveSegmentSeq:Number(stored.currentLiveSegmentSeq||0)||0,
        activityBurstAnchors:Array.isArray(stored.activityBurstAnchors)?stored.activityBurstAnchors:[],
      };
    }
  }

  if(INFLIGHT[sid]&&INFLIGHT[sid].journalReplayFromStart&&activeStreamId){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(activeStreamId&&INFLIGHT[sid]&&!_inflightHasVisibleLiveState(INFLIGHT[sid])){
    // A stale cursor-only INFLIGHT entry is worse than no cache: replay would
    // resume after lastRunJournalSeq while the pane has no prose/tool DOM to
    // preserve, making a session switch look like the live turn vanished.
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  const serverLiveSnapshot=activeStreamId
    ? _serverLiveSnapshotInflight(S.session.runtime_journal_snapshot, S.session.pending_attachments||[])
    : null;
  const hadLiveRecoveryInflight=!!INFLIGHT[sid];
  const liveRecoveryInflight=_selectLiveRecoveryInflight(INFLIGHT[sid], serverLiveSnapshot, activeStreamId);
  if(liveRecoveryInflight) INFLIGHT[sid]=liveRecoveryInflight;
  else if(hadLiveRecoveryInflight&&activeStreamId){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(INFLIGHT[sid]){
    _ensureInflightLiveAssistantMessage(INFLIGHT[sid]);
    const inflightMessages=_projectInflightMessagesForActivityBursts(INFLIGHT[sid]);
    S.toolCalls=[];
    // Switching between active sessions should rebuild the live worklog from
    // this session's INFLIGHT snapshot, not leave prior-session rows in place.
    if(typeof clearLiveToolCards==='function') clearLiveToolCards();
    try {
      await _ensureMessagesLoaded(sid, {force:_keepStaleUntilLoaded, loadGeneration:_loadGeneration});
    } catch(e) {
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      S.messages=inflightMessages;
    }
    if (!_isCurrentLoad()) {
      _rearmActiveSessionStream();
      return;
    }
    const liveTailPrepared=_prepareRunningLiveTail(S.messages,inflightMessages);
    if(liveTailPrepared){
      S.messages=_dropCurrentTurnAssistantMessages(S.messages);
    }
    S.messages=_mergeInflightTailMessages(S.messages,inflightMessages);
    S.toolCalls=(INFLIGHT[sid].toolCalls||[]);
    if(_mergePendingSessionMessage(S.session,S.messages)&&inflightMessages===(INFLIGHT[sid].messages||[])){
      INFLIGHT[sid].messages=S.messages;
    }
    // Refresh todos from cold-load or persisted INFLIGHT before painting.
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
    S.busy=!!activeStreamId;  // #4354: Only assert busy if server confirms active stream.
    // appendLiveToolCard() is guarded by S.activeStreamId; restore it before
    // replaying persisted live tools so the compact Activity count survives
    // switching away from and back to an active chat (#1715).
    S.activeStreamId=activeStreamId;
    const liveToolReplayId=(tc)=>String(tc&&(tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'')||'').trim();
    const replayPersistedLiveToolCards=(opts)=>{
      const liveToolCalls=Array.isArray(S.toolCalls)
        ? S.toolCalls
        : (Array.isArray(INFLIGHT[sid]&&INFLIGHT[sid].toolCalls)?INFLIGHT[sid].toolCalls:[]);
      const skipUnkeyedRestoredDuplicates=!!(opts&&opts.skipUnkeyedRestoredDuplicates);
      const restoredLiveTurn=skipUnkeyedRestoredDuplicates?document.getElementById('liveAssistantTurn'):null;
      const hasRestoredLiveToolRows=!!(restoredLiveTurn&&restoredLiveTurn.querySelector('.tool-card-row'));
      for(const tc of (liveToolCalls||[])){
        if(skipUnkeyedRestoredDuplicates&&hasRestoredLiveToolRows&&!liveToolReplayId(tc)) continue;
        if(tc&&tc.name) appendLiveToolCard(tc,{sessionId:sid,streamId:activeStreamId});
      }
    };
    let didReconnect=false;
    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function'){
      INFLIGHT[sid].reattach=false;
      if (!_isCurrentLoad()) return;
      didReconnect=true;
      attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
    }
    syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
    const restoredAnchorScene=activeStreamId&&typeof window!=='undefined'
      ? ((typeof window._renderLiveAnchorActivitySceneForStream==='function'&&window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid))||
        _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid))
      : false;
    if(typeof ensureRunActivityForCurrentTurn==='function') ensureRunActivityForCurrentTurn();
    const hasStructuredLiveState=!!(INFLIGHT[sid]&&(
      String(INFLIGHT[sid].lastAssistantText||'').trim()||
      String(INFLIGHT[sid].lastReasoningText||'').trim()||
      !!(INFLIGHT[sid].anchorActivityScene&&Array.isArray(INFLIGHT[sid].anchorActivityScene.activity_rows)&&INFLIGHT[sid].anchorActivityScene.activity_rows.length)||
      (Array.isArray(INFLIGHT[sid].activityBurstAnchors)&&INFLIGHT[sid].activityBurstAnchors.length)||
      (Array.isArray(INFLIGHT[sid].toolCalls)&&INFLIGHT[sid].toolCalls.length)
    ));
    let restoredLiveTurn=!!restoredAnchorScene;
    if(!restoredLiveTurn&&typeof restoreLiveTurnHtmlForSession==='function'){
      if(!hasStructuredLiveState){
        restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }else{
        const liveTurn=document.getElementById('liveAssistantTurn');
        const hasCurrentWorklogContent=!!(liveTurn&&liveTurn.querySelector(
          '.live-worklog[data-live-worklog-shell="1"] .tool-card-row,'+
          '.live-worklog[data-live-worklog-shell="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-call-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-call-group="1"] .wl-reason'
        ));
        if(hasCurrentWorklogContent) restoredLiveTurn=true;
        else restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }
    }
    if(restoredLiveTurn&&didReconnect){
      replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});
    }
    if(!restoredLiveTurn){
      clearLiveToolCards();
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
      else appendThinking();
      replayPersistedLiveToolCards();
    }
    if(!restoredAnchorScene&&typeof ensureLiveWorklogShell==='function'){
      const liveTurn=document.getElementById('liveAssistantTurn');
      if(!liveTurn||!liveTurn.querySelector('.tool-call-group[data-tool-worklog-group="1"]')) ensureLiveWorklogShell();
    }
    _deferWorkspaceRefreshForSession(sid);
    setBusy(true);setComposerStatus('');
    startApprovalPolling(sid);
    if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
    if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
  }else{
    // Phase 2b: Idle session — load full messages lazily for rendering.
    // _ensureMessagesLoaded is idempotent; it skips if S.messages already populated.
    // #5177: when the caller asked us to keep stale messages until the new ones
    // arrive (visibility/focus recovery), force the fetch so the
    // "messages already populated" early-return inside _ensureMessagesLoaded
    // does NOT skip the swap to the new transcript.
    try {
      await _ensureMessagesLoaded(sid, {force:_keepStaleUntilLoaded, loadGeneration:_loadGeneration});
    } catch (e) {
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      // Network errors, server failures, or SSE drops (Chrome error codes 4/5)
      // can cause _ensureMessagesLoaded to throw. Without a try/catch here the
      // "Loading conversation..." div injected at the top of loadSession would
      // persist forever with no recovery path.
      const _msgInner = $('msgInner');
      if (_msgInner) {
        _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load messages. Try switching sessions or refreshing.</div>';
      }
      if (typeof showToast === 'function') showToast('Failed to load conversation messages', 3000, 'error');
      if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
      return;
    }
    // Stale? A newer loadSession() call has already started (#1060).
    if (!_isCurrentLoad()) return;

    // Restore any queued message that survived page refresh or tab restore.
    if(typeof queueSessionMessage==='function'){
      try{
        const _entries=typeof _readPersistedSessionQueue==='function'
          ? _readPersistedSessionQueue(sid)
          : [];
        if(Array.isArray(_entries)&&_entries.length){
          const _lastMsg=S.messages.slice().reverse()
            .find(m=>m&&m.role==='assistant');
          const _lastAsst=_lastMsg?(_lastMsg.timestamp||_lastMsg._ts||0)*1000:0;
          const _fresh=_entries.filter(e=>!e._queued_at||e._queued_at>_lastAsst);
          if(_fresh.length){
            const _first=_fresh[0];
            const _msg=$&&$('msg');
            if(_msg&&_first.text&&!_msg.value){
              _msg.value=_first.text||'';
              if(typeof autoResize==='function') autoResize();
              if(typeof showToast==='function') showToast((_fresh.length>1?`${_fresh.length} queued messages restored (showing first)`:'Queued message restored')+' — review and send when ready');
            }
          }
          if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);
        }
      }catch(_){if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);}
    }

    // Reconstruct tool calls from message metadata, or fall back to session-level summary.
    // (hasMessageToolMetadata already computed inside _ensureMessagesLoaded; S.toolCalls set there.)
    updateQueueBadge(sid);

    // Attach pending user message if one is queued.
    _mergePendingSessionMessage(S.session,S.messages);

    // Self-heal-vs-live-render race guard (maintainer/Codex-reproduced; verified
    // in an isolated instance). `activeStreamId` was snapshotted BEFORE the
    // awaited _ensureMessagesLoaded above. During a force reload (the
    // `session-updated` self-heal or any keepStaleUntilLoaded recovery), a
    // server-initiated turn can fire `server_turn_started` mid-await and set
    // S.activeStreamId for THIS sid. Without re-reading, the idle branch below
    // would clear S.activeStreamId/S.busy off the stale (null) snapshot and
    // silently kill the live turn's render. Fold a concurrently-attached
    // same-session stream into activeStreamId so the existing attach branch
    // (and all its `attachLiveStream(sid, activeStreamId, ...)` calls) keeps it.
    activeStreamId = activeStreamId || ((S.activeStreamId && S.session && S.session.session_id===sid) ? S.activeStreamId : null);

    if(activeStreamId){
      S.busy=true;
      S.activeStreamId=activeStreamId;
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      const restoredAnchorScene=activeStreamId&&typeof window!=='undefined'
        ? ((typeof window._renderLiveAnchorActivitySceneForStream==='function'&&window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid))||
          _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid))
        : false;
      let restoredLiveTurn=!!restoredAnchorScene;
      if(!restoredLiveTurn&&typeof restoreLiveTurnHtmlForSession==='function'){
        restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }
      if(!restoredLiveTurn){
        if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
        else appendThinking();
      }
      _deferWorkspaceRefreshForSession(sid);
      updateQueueBadge(sid);
      startApprovalPolling(sid);
      if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
      if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
    }else{
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      updateQueueBadge(sid);
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      if(typeof resumeManualCompressionForSession==='function') resumeManualCompressionForSession(sid);
      // Workspace refresh is guarded by session id inside loadDir(); keep it
      // after the transcript's first paint so chat switching is not competing
      // with file-tree / git badge IO.
      _deferWorkspaceRefreshForSession(sid);
    }
  }

  return true;
}

export { _rearmActiveSessionStream, _restoreLoadedSession };
