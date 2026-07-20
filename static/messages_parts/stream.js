var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  const reconnecting=!!options.reconnecting;
  // #4416: start (or, on reconnect for the SAME stream, keep) tracking whether
  // the tab was hidden during this stream so the done-notification fires for a
  // backgrounded tab. A reconnect with a different streamId re-seeds (the old
  // entry belonged to a prior stream).
  _bindStreamHiddenTracker();
  {
    const _prev=_STREAM_WAS_HIDDEN[activeSid];
    const _keep=reconnecting&&_prev&&_prev.streamId===streamId;
    if(!_keep){
      _STREAM_WAS_HIDDEN[activeSid]={streamId,wasHidden:(typeof document!=='undefined'&&!!document.hidden)};
    }
    const _prevBackground=_STREAM_NOTIFICATION_BACKGROUND[activeSid];
    const _keepBackground=reconnecting&&_prevBackground&&_prevBackground.streamId===streamId;
    if(!_keepBackground){
      _STREAM_NOTIFICATION_BACKGROUND[activeSid]={streamId,wasBackgrounded:_desktopBackgroundedForNotifications};
    }
  }
  if(!INFLIGHT[activeSid]) INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[...uploaded],toolCalls:[]};
  else {
    if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded];
    if(!Array.isArray(INFLIGHT[activeSid].toolCalls)) INFLIGHT[activeSid].toolCalls=[];
  }
  const _priorInflightStreamId=String(INFLIGHT[activeSid].streamId||'');
  if(_priorInflightStreamId&&_priorInflightStreamId!==streamId){
    INFLIGHT[activeSid].lastRunJournalSeq=0;
    INFLIGHT[activeSid].lastRunJournalEventId='';
  }
  INFLIGHT[activeSid].streamId=streamId;
  if(!Array.isArray(INFLIGHT[activeSid].activityBurstAnchors)) INFLIGHT[activeSid].activityBurstAnchors=[];
  if(INFLIGHT[activeSid].currentActivityBurstId===undefined) INFLIGHT[activeSid].currentActivityBurstId=0;
  if(INFLIGHT[activeSid].currentLiveSegmentSeq===undefined) INFLIGHT[activeSid].currentLiveSegmentSeq=0;
  let assistantText='';
  let reasoningText='';
  if(S.session&&S.session.session_id===activeSid&&S.activeStreamId===streamId&&typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
  const existingLive=LIVE_STREAMS[activeSid];
  if(
    existingLive&&existingLive.streamId===streamId&&existingLive.source&&
    // During explicit reconnects, only reuse a proven-open transport. A stale
    // CONNECTING EventSource can survive in page state while the server has no
    // subscriber, which leaves the live pane blank forever.
    (typeof EventSource==='undefined'||
      existingLive.source.readyState===EventSource.OPEN||
      (!reconnecting&&existingLive.source.readyState===EventSource.CONNECTING))
  ){
    // Phase D: restore bottom run status on reattach after the Worklog shell
    // exists. There is no stale transport teardown in this branch.
    if(reconnecting && S.activeStreamId && typeof showLiveRunStatus==='function'){
      const _startedAt=(S.session&&S.session.pending_started_at)||Date.now()/1000;
      showLiveRunStatus(activeSid,{startedAt:_startedAt});
    }
    return;
  }
  closeOtherLiveStreams(activeSid);
  closeLiveStream(activeSid);
  if(!reconnecting&&typeof resetTurnWorkspaceMutations==='function') resetTurnWorkspaceMutations();
  if(!reconnecting&&typeof _resetStreamScrollFollow==='function') _resetStreamScrollFollow();
  // Phase D: restore bottom run status after closeLiveStream(); that helper
  // hides the status while tearing down stale EventSource ownership.
  if(reconnecting && S.activeStreamId && typeof showLiveRunStatus==='function'){
    const _startedAt=(S.session&&S.session.pending_started_at)||Date.now()/1000;
    showLiveRunStatus(activeSid,{startedAt:_startedAt});
  }
  _suspendSessionStreamForLiveChat(activeSid);

  // On reconnect, restore accumulated text from INFLIGHT so we don't lose
  // progress made before the session switch. Without this the closure starts
  // empty and tokens arriving on the new SSE connection append to nothing —
  // the already-rendered content vanishes.
  const _liveInflightAssistantMessages = reconnecting
    ? ((INFLIGHT[activeSid]&&Array.isArray(INFLIGHT[activeSid].messages))
      ? INFLIGHT[activeSid].messages.filter(m=>m&&m.role==='assistant'&&m._live)
      : [])
    : [];
  const _liveInflightAssistant = _liveInflightAssistantMessages.length===1
    ? _liveInflightAssistantMessages[0]
    : null;
  const _fullInflightAssistant = (INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastAssistantText) || '';
  const _joinedInflightSegments = _liveInflightAssistantMessages.length>1
    ? _liveInflightAssistantMessages.map(m=>m&&m.content?String(m.content).trim():'').filter(Boolean).join('\n\n')
    : '';
  const _lastLiveAssistant = reconnecting
    ? (_liveInflightAssistantMessages.length>1
      ? (_fullInflightAssistant || _joinedInflightSegments)
      : (_liveInflightAssistant
        ? (_fullInflightAssistant || _liveInflightAssistant.content || '')
        : _fullInflightAssistant))
    : '';
  const _lastLiveReasoning = reconnecting
    ? (_liveInflightAssistant&&_liveInflightAssistant.reasoning)
      || (INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastReasoningText)
      || ''
    : '';
  assistantText = _lastLiveAssistant ? _lastLiveAssistant : '';
  reasoningText=_lastLiveReasoning ? _lastLiveReasoning : '';
  let liveReasoningText = reasoningText;
  let visibleInterimSnippets=[];
  let _latestGoalStatus=null;
  let _pendingGoalContinuation=null;
  let assistantRow=null;
  let assistantBody=null;
  // On reconnect with recorded burst anchors, the rendered DOM has multiple
  // live assistant segments — one per anchor plus a tail. New tokens belong to
  // the TAIL segment only.
  let segmentStart=(()=>{
    if(!reconnecting) return 0;
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return 0;
    const anchors=Array.isArray(inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[];
    const textLen=String(assistantText||'').length;
    let lastEnd=0;
    for(const a of anchors){
      const end=Number(a&&a.textEnd);
      if(Number.isFinite(end)&&end>lastEnd&&end<=textLen) lastEnd=end;
    }
    return lastEnd;
  })();
  // If reconnect resumes exactly at the last recorded boundary, there is no
  // projected tail segment yet. The next token must create a fresh segment
  // after the last Activity group instead of rewriting the previous burst's
  // text segment.
  let _freshSegment=reconnecting&&segmentStart>0&&segmentStart>=String(assistantText||'').length;
  // streaming-markdown state: incremental DOM-building parser per segment
  let _smdParser=null;     // current smd parser instance (null until first content)
  let _smdWrittenLen=0;    // how many chars of displayText have been fed to smd parser
  let _smdWrittenText='';  // exact displayText snapshot used for prefix-alignment checks
  let _streamingKatexTimer=null; // throttles live KaTeX scans while smd writes deltas
  // On reconnect, the assistantBody already has partial smd-rendered content.
  // We clear it on first new token and restart the parser from the reconnect point.
  let _smdReconnect=reconnecting;
  function _isActiveSession(){
    return !!(S.session&&S.session.session_id===activeSid);
  }
  function _ownsActiveStreamOrBackground(){
    return !_isActiveSession() || S.activeStreamId===streamId;
  }
  function _bailOutOfTerminalEventsFromStaleStream(source){
    if(_ownsActiveStreamOrBackground()) return false;
    // This stale stream no longer owns the session — schedule cleanup of ITS own
    // anchor registry (identity-guarded, so it can't clobber the newer stream's
    // registry for the same session) before closing. (Codex leak catch.)
    _scheduleAnchorRegistryCleanup(120000);
    _closeSource(source);
    return true;
  }
  function _clearActivePaneInflightIfOwner(){
    if(_isActiveSession()) clearInflight();
  }
  function _approvalBelongsToOwner(){
    return _approvalSessionId===activeSid||(!_approvalSessionId&&_isActiveSession());
  }
  function _clarifyBelongsToOwner(){
    return _clarifySessionId===activeSid||(!_clarifySessionId&&_isActiveSession());
  }
  function _clearApprovalForOwner(){
    _clearApprovalPendingForSession(activeSid);
    if(!_approvalBelongsToOwner()) return;
    stopApprovalPolling();
    hideApprovalCard(true);
  }
  function _clearClarifyForOwner(reason){
    _clearClarifyPendingForSession(activeSid);
    if(!_clarifyBelongsToOwner()) return;
    stopClarifyPolling();
    hideClarifyCard(true, reason||'terminal');
  }
  function _clearOwnerInflightState(){
    if(_isActiveSession() && S.activeStreamId!==streamId) return;
    delete INFLIGHT[activeSid];
    clearInflightState(activeSid);
    _clearActivePaneInflightIfOwner();
    _resumeSessionStreamAfterLiveChat(activeSid);
  }
  function _isMarkerOnlyAssistantMessage(m){
    if(!m||m.role!=='assistant') return false;
    const text=String(typeof msgContent==='function'?msgContent(m):(m.content||''));
    return typeof _isPreservedCompressionTaskListMarkerOnlyText==='function'
      && _isPreservedCompressionTaskListMarkerOnlyText(text);
  }
  function _streamRecoveryControlMessageText(text){
    const normalized=String(text||'').replace(/\s+/g,' ').trim();
    if(!normalized) return false;
    const systemRecovery=/^\[System:/i.test(normalized)
      && (/continue exactly where you left off/i.test(normalized)
        || /do not retry the same tool call/i.test(normalized));
    const backendRecovery=/^the live worker stopped before this run finished\.?$/i.test(normalized);
    return !!(systemRecovery || backendRecovery);
  }
  function _streamRecoveryControlMessage(m){
    if(!m||m.role==='tool') return false;
    if(m.recovery_control===true) return true;
    // Backward-compat ONLY for pre-marker persisted sessions: match the two
    // fully-anchored synthetic recovery strings. Do NOT fall back to
    // provider_details_label — a genuine "Response interrupted" card the user
    // SHOULD see also carries the 'Interruption details' label, and filtering
    // on it would drop a real interruption from the transcript (the inverse
    // data-loss class flagged on the sibling #3300). Marker + strict text only.
    const text=String(typeof msgContent==='function'?msgContent(m):(m.content||''));
    return _streamRecoveryControlMessageText(text);
  }
  function _filterRecoveryControlMessages(messages){
    if(!Array.isArray(messages)) return [];
    return messages.filter((m)=>!_streamRecoveryControlMessage(m));
  }
  function _replaceMarkerOnlyAssistantWithStreamError(messages){
    if(!Array.isArray(messages)) return false;
    const msg=[...messages].reverse().find(m=>m&&m.role==='assistant');
    if(!_isMarkerOnlyAssistantMessage(msg)) return false;
    msg.content='**Error:** No response received after context compression. Please retry.';
    msg.provider_details='The only assistant text returned for this turn was the internal preserved-task-list compression marker, so the WebUI replaced it with an explicit error instead of rendering the marker as a model response.';
    return true;
  }
  function _isTerminalStreamErrorMarkerMessage(message){
    return message&&message.role==='assistant'&&typeof message.content==='string'&&
      message.content.startsWith('**Connection interrupted:** The browser lost the live SSE connection before the response finished.');
  }
  function _ensureSingleTerminalStreamErrorMarker(messages){
    if(!Array.isArray(messages)) return;
    while(messages.length && _isTerminalStreamErrorMarkerMessage(messages[messages.length-1])){
      messages.pop();
    }
    messages.push({
      role:'assistant',
      content:'**Connection interrupted:** The browser lost the live SSE connection before the response finished. If the worker completed, reopening this session should restore the settled transcript.',
    });
  }
  function _setActivePaneIdleIfOwner(){
    if(_isActiveSession()||!S.session||!INFLIGHT[S.session.session_id]){
      setBusy(false);
      setComposerStatus('');
      if(typeof setStatus==='function') setStatus('');
    }
  }
  function persistInflightState(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight||typeof saveInflightState!=='function') return;
    saveInflightState(activeSid,{
      streamId,
      messages:inflight.messages||[],
      uploaded:inflight.uploaded||[...uploaded],
      toolCalls:inflight.toolCalls||[],
      lastAssistantText:inflight.lastAssistantText||'',
      lastReasoningText:inflight.lastReasoningText||'',
      lastRunJournalSeq:inflight.lastRunJournalSeq||0,
      lastRunJournalEventId:inflight.lastRunJournalEventId||'',
      journalReplayFromStart:!!inflight.journalReplayFromStart,
      anchorActivityScene:inflight.anchorActivityScene||null,
      currentActivityBurstId:inflight.currentActivityBurstId||0,
      currentLiveSegmentSeq:inflight.currentLiveSegmentSeq||0,
      activityBurstAnchors:Array.isArray(inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[],
      todos:Array.isArray(inflight.todos)?inflight.todos:S.todos,
      todoStateMeta:inflight.todoStateMeta||S.todoStateMeta||null,
    });
  }
  function snapshotLiveTurn(){
    if(typeof snapshotLiveTurnHtmlForSession==='function') snapshotLiveTurnHtmlForSession(activeSid);
  }
  // Throttled per-frame variant. snapshotLiveTurnHtmlForSession serializes the
  // whole (growing) live turn via turn.outerHTML — O(n)/frame -> O(n^2) over a
  // long answer, and a real GC-pressure source. The snapshot only backs
  // mid-stream session-switch restore, and the switch path (sessions.js) plus
  // the stream event boundaries (tool/done) already capture synchronously, so a
  // coarse trailing snapshot during streaming is sufficient. (#5455 WS2.2)
  let _snapshotLiveTurnTimer=null;
  function _throttledSnapshotLiveTurn(){
    if(_snapshotLiveTurnTimer) return;
    _snapshotLiveTurnTimer=setTimeout(()=>{_snapshotLiveTurnTimer=null;snapshotLiveTurn();},700);
  }
  function _cancelThrottledSnapshotTimer(){
    if(_snapshotLiveTurnTimer){clearTimeout(_snapshotLiveTurnTimer);_snapshotLiveTurnTimer=null;}
  }
  // Throttled variant for token-by-token updates. persistInflightState()
  // calls saveInflightState() which does JSON.parse + JSON.stringify + write
  // on the entire inflight map every call. On a fast model at 60 tok/s with
  // a 10KB messages array this is ~36MB of JSON churn per second — a major
  // GC pressure source that causes the renderer to crash under load.
  // State transitions (tool events, done, error) still call persistInflightState()
  // directly so no more than 2s of progress is lost on a crash.
  let _persistTimer=null;
  function _throttledPersist(){
    if(_persistTimer) return;
    _persistTimer=setTimeout(()=>{_persistTimer=null;persistInflightState();},2000);
  }
  function _closeSource(source){
    closeLiveStream(activeSid, streamId, source);
  }
  function _clearStreamEndRecovery(){
    if(_streamEndRecoveryTimer){
      clearTimeout(_streamEndRecoveryTimer);
      _streamEndRecoveryTimer=null;
    }
    _pendingStreamEndRecovery=false;
    _streamEndRecoveryAttempts=0;
  }
  function _liveStreamEndScenePresent(){
    if(assistantText||assistantRow) return true;
    if(String(liveReasoningText||reasoningText||'').trim()) return true;
    const inflight=INFLIGHT[activeSid];
    if(inflight&&Array.isArray(inflight.toolCalls)&&inflight.toolCalls.length) return true;
    if(!_isActiveSession()||typeof document==='undefined') return false;
    const turn=$('liveAssistantTurn');
    return !!(turn&&turn.querySelector(
      '[data-live-assistant="1"],'+
      '.live-worklog[data-live-worklog-shell="1"],'+
      '.tool-card-row[data-live-tid],'+
      '.agent-activity-thinking[data-thinking-active="1"]'
    ));
  }
  function _scheduleStreamEndRecovery(source, delay=180){
    if(_streamEndRecoveryTimer) clearTimeout(_streamEndRecoveryTimer);
    _pendingStreamEndRecovery=true;
    _streamEndRecoveryTimer=setTimeout(()=>{void _runStreamEndRecovery(source);},delay);
  }
  function _finalizeStreamEndFallback(source){
    _clearStreamEndRecovery();
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _cancelThrottledSnapshotTimer();
    _terminalStateReached=true;
    _streamFinalized=true;
    _cancelAnimationFramePendingStreamRender();
    _streamFadeCleanupReduceMotionListener();
    _smdEndParser();
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    _clearOwnerInflightState();
    _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
    _clearStreamNotificationBackground(activeSid, streamId);
    _flushReasoningToAnchor();
    _scheduleAnchorRegistryCleanup();
    _clearAnchorProseIncrementalNode();
    _clearApprovalForOwner();
    _clearClarifyForOwner('terminal');
    if(_isActiveSession()){
      S.activeStreamId=null;
      clearLiveToolCards();if(!assistantText)removeThinking();
      renderMessages({preserveScroll:true});
    }
    renderSessionList();
    _setActivePaneIdleIfOwner();
    _closeSource(source);
  }
  async function _runStreamEndRecovery(source){
    if(_streamFinalized || _terminalStateReached || !_pendingStreamEndRecovery){
      _clearStreamEndRecovery();
      return;
    }
    _streamEndRecoveryTimer=null;
    const status=await _restoreSettledSession(source,{status:true});
    if(status==='restored'){
      _clearStreamEndRecovery();
      return;
    }
    if(status==='active'&&_streamEndRecoveryAttempts<10){
      _streamEndRecoveryAttempts+=1;
      _scheduleStreamEndRecovery(source,200);
      return;
    }
    _finalizeStreamEndFallback(source);
  }
  function _stripLiveVisibleAssistantEchoFromThinking(text, snippets){
    let out=String(text||'');
    (Array.isArray(snippets)?snippets:[]).forEach(snippet=>{
      const visible=String(snippet||'').trim();
      if(visible.length<20) return;
      out=out.split(visible).join('');
    });
    return out.trim();
  }
  function _liveThinkingText(){
    return String(liveReasoningText||'').trim() || 'Thinking…';
  }
  function _liveThinkingPlacement(){
    const activeSeq=Number(_assistantSegmentSeq||0);
    const nextSeq=Number(_currentLiveSegmentSeq||0)+1;
    const segmentSeq=(!assistantRow||_freshSegment||!activeSeq)?nextSeq:activeSeq;
    return {
      activityKey:S.activeStreamId?'live:'+S.activeStreamId:null,
      segmentSeq,
      burstId:_currentActivityBurstId,
    };
  }
  function _updateLiveThinkingCard(text, options){
    const opts={
      ..._liveThinkingPlacement(),
      ...((options&&typeof options==='object')?options:{}),
    };
    if(typeof updateThinking==='function') updateThinking(text, opts);
    else appendThinking(text, opts);
  }
  // Split a content string into {reasoning, content} by extracting any <think>...
  // blocks (or other known reasoning-tag pairs). If reasoning is already
  // populated on the message (e.g. from a separate on_reasoning stream), the
  // inline blocks are stripped but the existing reasoning field is preserved.
  // Provider-bug workaround: M3 (and similar reasoning models) emit the
  // thinking inline in the OpenAI-compat content stream instead of a separate
  // reasoning channel, which would otherwise bloat the persisted session
  // message by 30-50% and miss the m.reasoning field used by the thinking card.
  function _splitThinkFromContent(rawContent, existingReasoning){
    return _extractInlineThinkingFromContent(rawContent, existingReasoning, {streaming:false});
  }
  function syncInflightAssistantMessage(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    inflight.lastAssistantText=assistantText;
    inflight.lastReasoningText=reasoningText;
    if(!Array.isArray(inflight.messages)) inflight.messages=[];
    let assistantIdx=-1;
    for(let i=inflight.messages.length-1;i>=0;i--){
      const msg=inflight.messages[i];
      if(msg&&msg.role==='assistant'&&msg._live){assistantIdx=i;break;}
    }
    const ts=Date.now()/1000;
    // Split inline <think> blocks into m.reasoning so the persisted inflight
    // state stays compact and the thinking card has a proper source field.
    const split=_splitThinkFromContent(assistantText, reasoningText);
    if(assistantIdx>=0){
      inflight.messages[assistantIdx].content=split.content;
      inflight.messages[assistantIdx].reasoning=split.reasoning||undefined;
      inflight.messages[assistantIdx]._ts=inflight.messages[assistantIdx]._ts||ts;
      _throttledPersist();
      return;
    }
    inflight.messages.push({role:'assistant',content:split.content,reasoning:split.reasoning||undefined,_live:true,_ts:ts});
    _throttledPersist();
  }
  function recordActivityBoundary(){
    const inflight=INFLIGHT[activeSid];
    if(!inflight) return;
    if(!Array.isArray(inflight.activityBurstAnchors)) inflight.activityBurstAnchors=[];
    if(!assistantRow||!assistantRow.isConnected){
      assistantRow=null;
      assistantBody=null;
    }
    const textEnd=String(assistantText||'').length;
    const lastTextEnd=inflight.activityBurstAnchors.reduce((max,a)=>{
      const n=Number(a&&a.textEnd);
      return Number.isFinite(n)?Math.max(max,n):max;
    },0);
    if(textEnd<=lastTextEnd){
      inflight.currentActivityBurstId=_currentActivityBurstId;
      if(assistantRow) assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
      persistInflightState();
      return;
    }
    _currentActivityBurstId+=1;
    inflight.currentActivityBurstId=_currentActivityBurstId;
    const existing=inflight.activityBurstAnchors.find(a=>Number(a&&a.id)===_currentActivityBurstId);
    if(existing) existing.textEnd=textEnd;
    else inflight.activityBurstAnchors.push({id:_currentActivityBurstId,textEnd});
    if(assistantRow) assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
    persistInflightState();
  }
  function ensureAssistantRow(force=false){
    if(!_isActiveSession()) return;
    if(assistantRow&&!assistantRow.isConnected){assistantRow=null;assistantBody=null;}
    if(!force&&!assistantRow){
      const parsed=_parseStreamState();
      if(!String((parsed&&parsed.displayText)||'').trim()) return;
    }
    let turn=$('liveAssistantTurn');
    if(!turn){
      appendThinking();
      turn=$('liveAssistantTurn');
    }
    const blocks=(typeof _assistantTurnBlocks==='function')?_assistantTurnBlocks(turn):null;
    if(!blocks) return;
    if(!assistantRow){
      // After a tool call _freshSegment=true, so we always create a new segment
      // below the tool card rather than re-attaching to the old one above it.
      if(!_freshSegment){
        const liveSegments=blocks.querySelectorAll('[data-live-assistant="1"]');
        const existing=liveSegments.length?liveSegments[liveSegments.length-1]:null;
        if(existing){
          assistantRow=existing;
          assistantBody=existing.querySelector('.msg-body');
          const existingSeq=Number(existing.getAttribute('data-live-segment-seq')||'');
          if(Number.isFinite(existingSeq)&&existingSeq>0){
            _assistantSegmentSeq=existingSeq;
            if(_assistantSegmentSeq>_currentLiveSegmentSeq) _currentLiveSegmentSeq=_assistantSegmentSeq;
          }
        }
      }
    }
    if(assistantRow){
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
      return;
    }

    const tr=$('toolRunningRow');if(tr)tr.remove();
    $('emptyState').style.display='none';
    assistantRow=document.createElement('div');
    assistantRow.className='assistant-segment';
    _currentLiveSegmentSeq+=1;
    _assistantSegmentSeq=_currentLiveSegmentSeq;
    assistantRow.setAttribute('data-live-assistant','1');
    assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId));
    assistantRow.setAttribute('data-live-segment-seq',String(_assistantSegmentSeq));
    assistantBody=document.createElement('div');assistantBody.className='msg-body';
    assistantRow.appendChild(assistantBody);
    blocks.appendChild(assistantRow);
    if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
    if(INFLIGHT[activeSid]){
      INFLIGHT[activeSid].currentLiveSegmentSeq=_currentLiveSegmentSeq;
    }
    _freshSegment=false; // consumed — next reuse check is normal again
  }

  // ── Shared SSE handler wiring (used for initial connection and reconnect) ──
  let _reconnectAttempted=false;
  let _terminalStateReached=false;
  let _deferredStreamRecoveryBound=false;
  let _pendingStreamEndRecovery=false;
  let _streamEndRecoveryTimer=null;
  let _streamEndRecoveryAttempts=0;

  function _pageHiddenForStreamError(){
    return (typeof document!=='undefined'&&document.visibilityState==='hidden')||
      (typeof document!=='undefined'&&document.wasDiscarded===true);
  }

  function _reattachOrRestoreAfterDeferredStreamError(source){
    if(_terminalStateReached||_streamFinalized) return;
    if((S.session&&S.session.session_id)!==activeSid) return;
    (async()=>{
      try{
        if(streamId){
          const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
          if(st.active){
            setComposerStatus('Reconnected');
            _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
            return;
          }
        }
      }catch(_){
        if(_deferStreamErrorIfOffline()||_pageHiddenForStreamError()) return;
      }
      if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;
      if(_deferStreamErrorIfOffline()||_pageHiddenForStreamError()) return;
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup(120000);
      _handleStreamError(source);
    })();
  }

  function _deferStreamErrorIfPageHidden(source){
    if(!_pageHiddenForStreamError()) return false;
    setComposerStatus('Connection paused. Reconnecting when this tab returns…');
    if(S.session&&S.session.session_id===activeSid&&streamId) S.activeStreamId=streamId;
    if(!_deferredStreamRecoveryBound){
      _deferredStreamRecoveryBound=true;
      const resume=()=>{
        if(_pageHiddenForStreamError()) return;
        window.removeEventListener('focus',resume);
        window.removeEventListener('pageshow',resume);
        document.removeEventListener('visibilitychange',resume);
        _deferredStreamRecoveryBound=false;
        _reattachOrRestoreAfterDeferredStreamError(source);
      };
      document.addEventListener('visibilitychange',resume);
      window.addEventListener('focus',resume);
      window.addEventListener('pageshow',resume);
    }
    return true;
  }

  // Bug A fix (#631): track whether the stream has been finalized so any rAF
  // scheduled by a trailing 'token'/'reasoning' event that arrives in the same
  // microtask batch as 'done' does not fire after renderMessages() has already
  // settled the DOM — which was causing the thinking card to reappear below
  // the final answer or the response to render twice.
  let _streamFinalized=false;
  let _pendingRafHandle=null;
  let _streamFadeVisibleText='';
  let _streamFadeLastTickMs=0;
  let _streamFadeWordCarry=0;
  let _streamFadeStartedAt=0;
  let _streamFadeLastTargetWords=0;
  let _streamFadeLastArrivalMs=0;
  let _streamFadeArrivalWps=0;
  let _streamFadeLatestAnimationEndAt=0;
  let _streamFadeVisibleWords=0;
  let _streamFadeHoldUntilMs=0;
  let _streamFadeCurrentMs=620;
  let _streamFadeDomText='';
  let _streamFadeReduceMotionMql=null;
  let _streamFadeReduceMotion=false;
  let _streamFadeReduceMotionOnChange=null;
  let _currentActivityBurstId=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentActivityBurstId)||0)||0;
  let _currentLiveSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  let _assistantSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  let _lastRunJournalSeq=reconnecting
    ? Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastRunJournalSeq)||0)
    : 0;
  let _lastRunJournalEventId=reconnecting
    ? String((INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastRunJournalEventId)||'')
    : '';
  const _STREAM_FADE_MS=620;
  const _STREAM_FADE_MAX_MS=900;
  const _STREAM_FADE_DONE_MAX_MS=1000;
  const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400;
  const _anchorApi=(typeof window!=='undefined'&&window.HermesAssistantTurnAnchors)
    ? window.HermesAssistantTurnAnchors
    : null;
  const _anchorRegistryMap=(typeof window!=='undefined')
    ? (window._liveAnchorRegistries=window._liveAnchorRegistries||new Map())
    : null;
  const _existingAnchorRegistry=_anchorRegistryMap?_anchorRegistryMap.get(streamId):null;
  const _anchorRegistry=_existingAnchorRegistry||(_anchorApi&&typeof _anchorApi.createAssistantTurnAnchorRegistry==='function'
    ? _anchorApi.createAssistantTurnAnchorRegistry({
      session_id:activeSid,
      stream_id:streamId,
      run_id:null,
    })
    : null);
  let _anchorShadowWarned=false;
  let _anchorReasoningFlushed=false;
  let _anchorLocalSeq=0;
  if(_anchorRegistryMap&&_anchorRegistry) _anchorRegistryMap.set(streamId,_anchorRegistry);
  function _scheduleAnchorRegistryCleanup(delayMs=600000){
    if(!_anchorRegistryMap||!_anchorRegistry) return;
    setTimeout(()=>{
      if(_anchorRegistryMap.get(streamId)===_anchorRegistry) _anchorRegistryMap.delete(streamId);
    },delayMs);
  }
  // Backstop: schedule an identity-guarded cleanup at creation so this shadow
  // registry self-expires no matter which teardown path the stream takes
  // (incl. external ones like sidebar cancelSessionStream() that bypass the
  // in-closure SSE handlers). Explicit terminal-path calls above just expire it
  // sooner; this guarantees window._liveAnchorRegistries can't grow unbounded.
  _scheduleAnchorRegistryCleanup(600000);
  // Applying an event and painting it are separate outcomes. Reasoning uses the
  // optional holder to decide whether a temporary visible fallback is needed.
  function _applyToAnchor(sourceEventType, rawEventData, sseEvent, renderOutcome){
    if(renderOutcome&&typeof renderOutcome==='object') renderOutcome.rendered=false;
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return null;
    const raw=(rawEventData&&typeof rawEventData==='object')?rawEventData:{};
    const eventId=(sseEvent&&sseEvent.lastEventId)||raw.event_id||raw.lastEventId||raw.last_event_id||'';
    const sourceEvent={
      ...raw,
      source_event_type:sourceEventType,
      // Persist a creation timestamp the FIRST time we see this source event, so
      // the worklog event timestamp (#5700/#5739) survives settlement. Reasoning
      // events carry no server timestamp; without this, the live DOM shows a
      // fallback time but the settled scene row rebuilds with created_at:null and
      // the timestamp disappears. Prefer any real server-supplied stamp; fall back
      // to now only when none exists. (#5739 gate finding.)
      created_at:raw.created_at??raw.timestamp??raw.ts??(Date.now()/1000),
      activitySegmentSeq:raw.activitySegmentSeq??raw.activity_segment_seq??_assistantSegmentSeq,
      activityBurstId:raw.activityBurstId??raw.activity_burst_id??_currentActivityBurstId,
    };
    if(eventId) sourceEvent.event_id=eventId;
    try{
      const result=_anchorApi.applyAssistantTurnAnchorSourceEvent(
        _anchorRegistry,
        sourceEvent,
        {session_id:activeSid,stream_id:streamId}
      );
      const rendered=_renderAnchorLiveScene();
      if(renderOutcome&&typeof renderOutcome==='object') renderOutcome.rendered=rendered;
      return result;
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live shadow feed failed',err);
      }
      return null;
    }
  }
  function _anchorActivityEvents(){
    const anchor=_anchorRegistry&&_anchorRegistry.anchor;
    return anchor&&Array.isArray(anchor.activity_events)?anchor.activity_events:null;
  }
  function _findAnchorActivityEventByLocalId(localId, sourceEventType){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      return event;
    }
    return null;
  }
  function _latestAnchorCompressionEventIndex(sourceEventType){
    const events=_anchorActivityEvents();
    if(!events) return -1;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(event&&event.source_event_type===sourceEventType) return i;
    }
    return -1;
  }
  function _anchorCompressionCompletedAfter(index){
    const events=_anchorActivityEvents();
    if(!events) return false;
    for(let i=events.length-1;i>index;i--){
      const event=events[i];
      if(event&&event.source_event_type==='compressed') return true;
    }
    return false;
  }
  function _ensureAnchorCompressionCompletedOnLiveProgress(sessionId){
    if(!_anchorRegistry||!_anchorApi) return false;
    const sid=String(sessionId||activeSid||'');
    const events=_anchorActivityEvents();
    const runningIndex=_latestAnchorCompressionEventIndex('compressing');
    if(runningIndex>=0&&_anchorCompressionCompletedAfter(runningIndex)) return true;
    const runningEvent=(events&&runningIndex>=0)?events[runningIndex]:null;
    const basis=String((runningEvent&&(runningEvent.local_id||runningEvent.event_id))||streamId||sid||'compression');
    const localId=`live-compression-complete:${basis}`;
    if(_findAnchorActivityEventByLocalId(localId,'compressed')) return true;
    const eventId=`synthetic:${localId}`;
    const result=_applyToAnchor('compressed',{
      event_id:eventId,
      local_id:localId,
      session_id:sid,
      old_session_id:sid,
      automatic:true,
      synthetic:true,
      status:'completed',
      phase:'done',
      message:'Context auto-compressed',
    },{lastEventId:eventId});
    return !!(result&&(result.applied||result.reason==='duplicate'));
  }
  function _replaceAnchorActivityEventByLocalId(localId, sourceEventType, patch){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      const next={
        ...event,
        ...(patch||{}),
        payload:{
          ...(event.payload&&typeof event.payload==='object'?event.payload:{}),
          ...((patch&&patch.payload&&typeof patch.payload==='object')?patch.payload:{}),
        },
      };
      events[i]=next;
      return next;
    }
    return null;
  }
  function _nextAnchorLocalSeq(){
    _anchorLocalSeq+=1;
    const cursor=Number(_runJournalReplayAfterSeq&&_runJournalReplayAfterSeq());
    return (Number.isFinite(cursor)?cursor:0)+_anchorLocalSeq;
  }
  function _anchorSegmentSeq(){
    const seq=Number(_assistantSegmentSeq||_currentLiveSegmentSeq||0);
    return Number.isFinite(seq)&&seq>0?seq:1;
  }
  function _anchorSceneActiveMode(){
    const normalize=value=>value==='transparent_stream'||value==='compact_worklog'||value==='hide_all_activity'?value:'';
    if(typeof window!=='undefined'){
      if(typeof window.chatActivityMode==='function'){
        try{
          const mode=normalize(window.chatActivityMode());
          if(mode) return mode;
        }catch(_){}
      }
      const displayMode=normalize(window._chatActivityDisplayMode);
      if(displayMode) return displayMode;
      if(window._transparentStream) return 'transparent_stream';
    }
    return 'compact_worklog';
  }
  function _anchorSceneRowDisplayHintForMode(row, sceneMode){
    const hints=row&&typeof row==='object'&&row.display_hints&&typeof row.display_hints==='object'
      ? row.display_hints
      : null;
    if(sceneMode==='transparent_stream') return (hints&&hints.transparent_stream)||'chronological_activity';
    if(sceneMode==='compact_worklog') return (hints&&hints.compact_worklog)||row.display_hint||'activity_row';
    if(sceneMode==='hide_all_activity') return (hints&&hints.hidden_activity)||'hidden_activity';
    return row&&row.display_hint||'activity_row';
  }
  function _renderAnchorLiveScene(){
    if(!_anchorRegistry||!_isActiveSession()) return false;
    if(typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneForStream!=='function') return false;
    try{
      return !!window._renderLiveAnchorActivitySceneForStream(streamId, activeSid, {
        mode:_anchorSceneActiveMode(),
      });
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live scene render failed',err);
      }
      return false;
    }
  }
  function _projectLiveAnchorActivityScene(){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.projectAssistantTurnAnchorActivityScene!=='function') return null;
    try{
      return _anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:_anchorSceneActiveMode()});
    }catch(_){
      return null;
    }
  }
  function _anchorSceneMessageRef(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    const payload={
      role:String(message.role||''),
      content:String(content||'').replace(/\s+/g,' ').trim(),
      timestamp:message._ts||message.timestamp||'',
    };
    return JSON.stringify(payload);
  }
  function _anchorSceneMessageText(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    return typeof content==='string'?content:String(content||'');
  }
  function _anchorSceneContentText(part){
    if(part===undefined||part===null) return '';
    if(typeof part==='string') return part;
    if(typeof part!=='object') return String(part||'');
    return String(part.text||part.content||part.input_text||part.output_text||part.thinking||part.reasoning||part.summary||'');
  }
  function _anchorSceneContentVisibleText(part){
    if(part===undefined||part===null) return '';
    if(typeof part==='string') return part;
    if(typeof part!=='object') return String(part||'');
    const partType=String(part.type||'');
    if(partType==='thinking'||partType==='reasoning') return '';
    const contentText=(partType==='text'||partType==='input_text'||partType==='output_text')?part.content:'';
    return String(part.text||part.input_text||part.output_text||contentText||'');
  }
  function _anchorSceneMessageHasContentToolUse(message){
    return !!(message&&Array.isArray(message.content)&&message.content.some(part=>part&&typeof part==='object'&&part.type==='tool_use'));
  }
  function _anchorSceneFinalAnswerText(message){
    if(!_anchorSceneMessageHasContentToolUse(message)) return _anchorSceneMessageText(message);
    const content=Array.isArray(message.content)?message.content:[];
    let lastToolIndex=-1;
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(part&&typeof part==='object'&&part.type==='tool_use') lastToolIndex=i;
    }
    const tailText=content.slice(lastToolIndex+1)
      .map(part=>_anchorSceneContentVisibleText(part))
      .filter(text=>_anchorSceneCleanText(text))
      .join('\n');
    return _anchorSceneCleanText(tailText)?tailText:'';
  }
  function _anchorSceneCleanText(value){
    return String(value||'').replace(/\s+/g,' ').trim();
  }
  function _anchorSceneTextKey(value){
    return _anchorSceneCleanText(value).toLowerCase();
  }
  function _anchorSceneSafePayload(value){
    if(value===undefined) return undefined;
    if(value===null||typeof value!=='object') return value;
    try{
      return JSON.parse(JSON.stringify(value));
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneToolId(tool){
    return String(tool&&(tool.tid||tool.id||tool.tool_call_id||tool.tool_use_id||tool.call_id)||'').trim();
  }
  function _anchorSceneToolName(tool){
    const fn=tool&&tool.function&&typeof tool.function==='object'?tool.function:{};
    return String(tool&&(tool.name||tool.tool_name)||fn.name||'tool').trim()||'tool';
  }
  function _anchorSceneToolArgs(tool){
    if(!tool||typeof tool!=='object') return {};
    if(tool.args&&typeof tool.args==='object') return _anchorSceneSafePayload(tool.args)||{};
    if(tool.input&&typeof tool.input==='object') return _anchorSceneSafePayload(tool.input)||{};
    const fn=tool.function&&typeof tool.function==='object'?tool.function:{};
    if(typeof fn.arguments==='string'&&fn.arguments.trim()){
      try{
        const parsed=JSON.parse(fn.arguments);
        return parsed&&typeof parsed==='object'?_anchorSceneSafePayload(parsed):{};
      }catch(_){}
    }
    return {};
  }
  function _anchorSceneContentTool(part){
    if(!part||typeof part!=='object') return {};
    const fn=part.function&&typeof part.function==='object'?part.function:{};
    return {
      id:part.id||part.tid||part.tool_call_id||part.tool_use_id||part.call_id,
      tid:part.tid||part.id||part.tool_call_id||part.tool_use_id||part.call_id,
      tool_call_id:part.tool_call_id,
      tool_use_id:part.tool_use_id,
      call_id:part.call_id,
      name:part.name||part.tool_name||fn.name||'tool',
      tool_name:part.tool_name,
      args:part.args,
      input:part.input,
      function:part.function,
      command:part.command||part.raw_command||part.original_command||part.display_command,
      preview:part.preview||part.summary,
      snippet:part.snippet||part.result||part.output,
      result:part.result,
      output:part.output,
      is_error:part.is_error,
      error:part.error,
      duration:part.duration,
      started_at:part.started_at,
    };
  }
  function _anchorSceneStringPayload(value){
    if(value===undefined||value===null) return '';
    if(typeof value==='string') return value;
    try{
      return JSON.stringify(value);
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneRowBase(role, kind, sourceEventType, orderIndex, messageIndex){
    const groupKey=Number.isFinite(Number(messageIndex))?`assistant:${Number(messageIndex)}`:`activity:${orderIndex}`;
    return {
      row_id:`settled:${activeSid||'session'}:${streamId||'stream'}:${role}:${messageIndex}:${orderIndex}`,
      order_index:orderIndex,
      kind,
      role,
      display_hint:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
      display_hints:{
        compact_worklog:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
        transparent_stream:'chronological_activity',
      },
      source_event_type:sourceEventType,
      event_id:null,
      local_id:null,
      run_id:null,
      stream_id:streamId||null,
      seq:orderIndex,
      status:role==='terminal'?'completed':'completed',
      created_at:null,
      identity:{event_id:null,local_id:null,run_id:null,stream_id:streamId||null,seq:orderIndex},
      group:{
        group_key:groupKey,
        activity_burst_id:null,
        activity_segment_seq:null,
        assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null,
      },
      text:'',
      thinking:null,
      tool_call_id:null,
      tool:null,
      payload:{assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null},
    };
  }
  function _anchorSceneProseRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('prose','process_prose','settled_message',orderIndex,messageIndex);
    row.text=String(text||'');
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneThinkingRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('thinking','reasoning','reasoning',orderIndex,messageIndex);
    row.text=String(text||'');
    const preview=_anchorSceneCleanText(text);
    row.thinking={
      text:row.text,
      preview:preview.length>180?`${preview.slice(0,177)}...`:preview,
      dedupe_key:preview?`thinking:${preview.toLowerCase()}`:'',
    };
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneToolRowFromCall(tool, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('tool','tool_completed','tool_complete',orderIndex,messageIndex);
    const tid=_anchorSceneToolId(tool);
    const name=_anchorSceneToolName(tool);
    const args=_anchorSceneToolArgs(tool);
    const command=_anchorSceneStringPayload(tool&&(tool.command||tool.raw_command||tool.original_command||tool.display_command))||_anchorSceneStringPayload(args&&(args.cmd||args.command));
    const preview=_anchorSceneStringPayload(tool&&(tool.preview||tool.summary));
    const snippet=_anchorSceneStringPayload(tool&&(tool.snippet||tool.result||tool.output));
    const isError=!!(tool&&(tool.is_error||tool.error));
    row.row_id=tid?`settled:${activeSid||'session'}:${streamId||'stream'}:tool:${tid}`:row.row_id;
    row.tool_call_id=tid||null;
    row.tool={
      id:tid||null,
      name,
      args,
      command,
      preview,
      snippet,
      result:_anchorSceneSafePayload(tool&&tool.result)??null,
      output:_anchorSceneSafePayload(tool&&tool.output)??null,
      done:true,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:null,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:null,
      signature:[name,tid||'',JSON.stringify(args||{})].join('|'),
    };
    row.payload={
      ...row.payload,
      tid:tid||undefined,
      id:tid||undefined,
      name,
      args,
      command,
      preview,
      snippet,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:undefined,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:undefined,
    };
    return row;
  }
  function _anchorSceneToolRowName(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    return String(tool.name||payload.name||'tool').trim().toLowerCase();
  }
  function _anchorSceneToolRowId(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    return String(
      (row&&row.tool_call_id)||
      tool.id||
      tool.tid||
      tool.tool_call_id||
      tool.tool_use_id||
      tool.call_id||
      payload.tid||
      payload.id||
      ''
    ).trim();
  }
  function _anchorSceneToolRowsHaveNonConflictingIds(existing, incoming){
    const existingId=_anchorSceneToolRowId(existing);
    const incomingId=_anchorSceneToolRowId(incoming);
    return !existingId||!incomingId||existingId===incomingId;
  }
  function _anchorSceneToolRowsHaveDifferentExplicitIds(existing, incoming){
    const existingId=_anchorSceneToolRowId(existing);
    const incomingId=_anchorSceneToolRowId(incoming);
    return !!existingId&&!!incomingId&&existingId!==incomingId;
  }
  function _anchorSceneToolRowStartedAt(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const value=tool.started_at!==undefined&&tool.started_at!==null&&tool.started_at!==''?tool.started_at:payload.started_at;
    return value!==undefined&&value!==null&&value!==''?String(value):'';
  }
  function _anchorSceneToolRowsHaveSameStartedAt(existing, incoming){
    const existingStartedAt=_anchorSceneToolRowStartedAt(existing);
    const incomingStartedAt=_anchorSceneToolRowStartedAt(incoming);
    return !!existingStartedAt&&!!incomingStartedAt&&existingStartedAt===incomingStartedAt;
  }
  function _anchorSceneToolRowBodyText(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    for(const value of [tool.snippet,payload.snippet,tool.output,payload.output,tool.result,payload.result,tool.preview,payload.preview]){
      const text=_anchorSceneStringPayload(value).trim();
      if(text) return text;
    }
    return '';
  }
  function _anchorSceneToolRowsHaveCompatibleBody(existing, incoming){
    const existingBody=_anchorSceneToolRowBodyText(existing);
    const incomingBody=_anchorSceneToolRowBodyText(incoming);
    return !!existingBody&&!!incomingBody&&(
      existingBody===incomingBody||
      existingBody.startsWith(incomingBody)||
      incomingBody.startsWith(existingBody)
    );
  }
  function _anchorSceneToolRowsHaveCompatibleNames(existing, incoming){
    const existingName=_anchorSceneToolRowName(existing);
    const incomingName=_anchorSceneToolRowName(incoming);
    return !existingName||!incomingName||existingName==='tool'||incomingName==='tool'||existingName===incomingName;
  }
  function _anchorSceneToolRowArgs(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const args=(tool.args&&typeof tool.args==='object'&&!Array.isArray(tool.args))?tool.args:payload.args;
    return args&&typeof args==='object'&&!Array.isArray(args)?args:null;
  }
  function _anchorSceneObjectContainsSubset(base, subset){
    if(!base||!subset||typeof base!=='object'||typeof subset!=='object') return false;
    const stableStringify=(candidate)=>{
      const normalize=(value)=>{
        if(!value||typeof value!=='object') return value;
        if(Array.isArray(value)) return value.map(normalize);
        const normalized={};
        Object.keys(value).sort().forEach((key)=>{normalized[key]=normalize(value[key]);});
        return normalized;
      };
      try{return JSON.stringify(normalize(candidate));}catch(_){return JSON.stringify(candidate);}
    };
    for(const [key,value] of Object.entries(subset)){
      if(!Object.prototype.hasOwnProperty.call(base,key)) return false;
      if(stableStringify(base[key])!==stableStringify(value)) return false;
    }
    return true;
  }
  function _anchorSceneToolRowsHaveCompatibleInvocation(existing, incoming){
    const existingTool=existing&&existing.tool&&typeof existing.tool==='object'?existing.tool:{};
    const incomingTool=incoming&&incoming.tool&&typeof incoming.tool==='object'?incoming.tool:{};
    const existingPayload=existing&&existing.payload&&typeof existing.payload==='object'?existing.payload:{};
    const incomingPayload=incoming&&incoming.payload&&typeof incoming.payload==='object'?incoming.payload:{};
    const existingCommand=_anchorSceneStringPayload(existingTool.command||existingPayload.command).trim();
    const incomingCommand=_anchorSceneStringPayload(incomingTool.command||incomingPayload.command).trim();
    if(existingCommand&&incomingCommand) return existingCommand===incomingCommand;
    const existingArgs=_anchorSceneToolRowArgs(existing);
    const incomingArgs=_anchorSceneToolRowArgs(incoming);
    if(!existingArgs||!incomingArgs||!Object.keys(existingArgs).length||!Object.keys(incomingArgs).length) return false;
    return _anchorSceneObjectContainsSubset(existingArgs,incomingArgs)||_anchorSceneObjectContainsSubset(incomingArgs,existingArgs);
  }
  function _anchorSceneToolRowHasInvocationEvidence(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const command=_anchorSceneStringPayload(tool.command||payload.command).trim();
    const args=_anchorSceneToolRowArgs(row);
    return !!command||!!(args&&Object.keys(args).length);
  }
  function _anchorSceneToolRowsCanNameMatch(existing, incoming){
    if(!_anchorSceneToolRowsHaveCompatibleNames(existing,incoming)) return false;
    if(_anchorSceneToolRowHasInvocationEvidence(existing)&&_anchorSceneToolRowHasInvocationEvidence(incoming)){
      return _anchorSceneToolRowsHaveCompatibleInvocation(existing,incoming);
    }
    return true;
  }
  function _anchorSceneMatchingContentToolRow(contentToolRows, incomingRow, ordinal, usedRows, incomingTotal, idFlexibleRows){
    if(!Array.isArray(contentToolRows)||!incomingRow) return null;
    const incomingTid=incomingRow.tool_call_id||(incomingRow.tool&&incomingRow.tool.id);
    for(const row of contentToolRows){
      if(!row||usedRows.has(row)) continue;
      const tid=row.tool_call_id||(row.tool&&row.tool.id);
      if(tid&&incomingTid&&tid===incomingTid) return row;
    }
    if(contentToolRows.length===1&&Number(incomingTotal)===1){
      const onlyRow=contentToolRows[0];
      if(onlyRow&&!usedRows.has(onlyRow)&&_anchorSceneToolRowsCanNameMatch(onlyRow,incomingRow)) return onlyRow;
    }
    const availableRows=contentToolRows.filter(row=>row&&!usedRows.has(row));
    if(availableRows.length===1){
      if(Number(incomingTotal)===1&&_anchorSceneToolRowsCanNameMatch(availableRows[0],incomingRow)) return availableRows[0];
      if(
        _anchorSceneToolRowsHaveCompatibleNames(availableRows[0],incomingRow)&&
        _anchorSceneToolRowsHaveCompatibleInvocation(availableRows[0],incomingRow)
      ) return availableRows[0];
    }
    const reusableRows=contentToolRows.filter(row=>row&&usedRows.has(row));
    if(
      reusableRows.length===1&&
      Number(incomingTotal)===1&&
      (
        (
          _anchorSceneToolRowId(reusableRows[0])&&
          _anchorSceneToolRowId(incomingRow)&&
          _anchorSceneToolRowId(reusableRows[0])===_anchorSceneToolRowId(incomingRow)
        )||
        (
          idFlexibleRows&&
          idFlexibleRows.has(reusableRows[0])&&
          _anchorSceneToolRowsHaveSameStartedAt(reusableRows[0],incomingRow)&&
          _anchorSceneToolRowsHaveCompatibleBody(reusableRows[0],incomingRow)
        )
      )&&
      _anchorSceneToolRowsHaveCompatibleNames(reusableRows[0],incomingRow)&&
      _anchorSceneToolRowsHaveCompatibleInvocation(reusableRows[0],incomingRow)
    ) return reusableRows[0];
    for(const row of contentToolRows){
      if(!row||usedRows.has(row)) continue;
      const tid=row.tool_call_id||(row.tool&&row.tool.id);
      if(!tid&&!incomingTid&&_anchorSceneToolRowsCanNameMatch(row,incomingRow)) return row;
    }
    return null;
  }
  function _anchorSceneMessageReasoningText(message){
    if(!message||typeof message!=='object') return '';
    return String(message.reasoning||message._reasoning||message.reasoning_content||message.thinking||'');
  }
  function _anchorSceneRowsFromContentParts(message, messageIndex, options){
    if(!_anchorSceneMessageHasContentToolUse(message)) return null;
    options=(options&&typeof options==='object')?options:{};
    const isFinalMessage=!!options.isFinalMessage;
    const rows=[];
    const content=Array.isArray(message.content)?message.content:[];
    let lastToolIndex=-1;
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(part&&typeof part==='object'&&part.type==='tool_use') lastToolIndex=i;
    }
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(!part||typeof part!=='object'){
        if(isFinalMessage&&i>lastToolIndex) continue;
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneProseRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='text'||part.type==='input_text'||part.type==='output_text'){
        if(isFinalMessage&&i>lastToolIndex&&_anchorSceneContentVisibleText(part)) continue;
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneProseRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='thinking'||part.type==='reasoning'){
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneThinkingRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='tool_use'){
        rows.push(_anchorSceneToolRowFromCall(_anchorSceneContentTool(part),rows.length,messageIndex));
      }
    }
    return rows;
  }
  // #4622: a settled tool row built from messages[].tool_calls (state.db/sidecar)
  // can lack the result body — terminal stdout, or the diff/output that a
  // patch/edit card renders — because the persisted row carries only a short
  // preview (or, on a cold/paginated load, nothing). The full body lives on the
  // live S.toolCalls entry at settle time. When a settled row and a live call
  // match by tool id, restore the missing body fields from the live call onto
  // the settled row's tool+payload (only when the settled value is empty — never
  // clobber a genuine persisted body), so the rebuilt card shows full output +
  // the Show-more expander + the rendered diff. Returns true if it enriched.
  function _enrichSettledToolRowBodyFromLive(row, live){
    if(!row||typeof row!=='object'||!live||typeof live!=='object') return false;
    const tool=(row.tool&&typeof row.tool==='object')?row.tool:(row.tool={});
    const payload=(row.payload&&typeof row.payload==='object')?row.payload:(row.payload={});
    let enriched=false;
    const _empty=v=>v===undefined||v===null||v==='';
    // Result body: _anchorSceneToolCallFromRow renders tool.snippet||payload.snippet
    // (||payload.result||payload.output) as the card output + diff source, so
    // restore the snippet onto both tool+payload when the settled row has none.
    const liveSnippet=_anchorSceneStringPayload(live.snippet||live.result||live.output);
    // Restore the live body when the settled snippet is missing OR is a bounded
    // preview of the live one. The backend persists a capped preview
    // (_TOOL_RESULT_SNIPPET_MAX = 4000 chars in api/streaming.py), so a long
    // terminal/tool output settles to that 4000-char prefix, not to empty —
    // #4622's actual symptom. Treat a settled snippet as restorable when the
    // live snippet is strictly longer AND the settled value is a prefix of it
    // AND the settled value is at/over the persistence cap (i.e. it's a
    // truncated preview, not a genuinely short real value we must not clobber).
    const _SETTLED_SNIPPET_CAP=4000;
    const _isBoundedPreview=(settled,full)=>(
      typeof settled==='string'&&typeof full==='string'&&
      full.length>settled.length&&settled.length>=_SETTLED_SNIPPET_CAP&&
      full.startsWith(settled)
    );
    const _settledSnippet=(!_empty(tool.snippet)?tool.snippet:(!_empty(payload.snippet)?payload.snippet:''));
    const _snippetRestorable=(_empty(tool.snippet)&&_empty(payload.snippet))||_isBoundedPreview(_settledSnippet,liveSnippet);
    if(liveSnippet&&_snippetRestorable){
      tool.snippet=liveSnippet; payload.snippet=liveSnippet; enriched=true;
    }
    // Command (shell detail-lead) + args (diff/input reconstruction, the "Full" tab).
    const liveCommand=_anchorSceneStringPayload(live.command||live.raw_command);
    if(liveCommand&&_empty(tool.command)&&_empty(payload.command)){
      tool.command=liveCommand; payload.command=liveCommand; enriched=true;
    }
    if(!_empty(live.started_at)&&_empty(tool.started_at)&&_empty(payload.started_at)){
      tool.started_at=live.started_at; payload.started_at=live.started_at; enriched=true;
    }
    const liveArgs=_anchorSceneToolArgs(live);
    if(liveArgs&&typeof liveArgs==='object'&&Object.keys(liveArgs).length){
      const mergeMissingArgs=(existing)=>{
        const base=(existing&&typeof existing==='object'&&!Array.isArray(existing))?{...existing}:{};
        let changed=!(existing&&typeof existing==='object'&&!Array.isArray(existing));
        for(const [key,value] of Object.entries(liveArgs)){
          if(!Object.prototype.hasOwnProperty.call(base,key)){
            base[key]=value;
            changed=true;
          }
        }
        return changed?base:existing;
      };
      const nextToolArgs=mergeMissingArgs(tool.args);
      const nextPayloadArgs=mergeMissingArgs(payload.args);
      if(nextToolArgs!==tool.args){ tool.args=nextToolArgs; enriched=true; }
      if(nextPayloadArgs!==payload.args){ payload.args=nextPayloadArgs; enriched=true; }
    }
    return enriched;
  }
  function _anchorSceneRowsByMessageIndex(messages, turnStart, lastAsstIndex, options){
    options=(options&&typeof options==='object')?options:{};
    const byIdx=new Map();
    const add=(idx,row)=>{
      if(!byIdx.has(idx)) byIdx.set(idx,[]);
      byIdx.get(idx).push(row);
    };
    // Pre-index S.toolCalls by assistant_msg_idx for O(m+n) lookup
    const toolsByIdx=new Map();
    if(S.toolCalls) for(const tc of S.toolCalls){
      const ti=typeof tc.toolIdx==='number'? tc.toolIdx : parseInt(tc.assistant_msg_idx,10);
      if(Number.isFinite(ti)){
        if(!toolsByIdx.has(ti)) toolsByIdx.set(ti,[]);
        toolsByIdx.get(ti).push(tc);
      }
    }
    let encounter=0;
    const endIndex=options&&options.includeFinal?lastAsstIndex+1:lastAsstIndex;
    for(let idx=turnStart+1;idx<endIndex;idx+=1){
      const message=messages[idx];
      if(!message||message.role!=='assistant') continue;
      const pool=[];
      const text=_anchorSceneMessageText(message);
      const contentRows=_anchorSceneRowsFromContentParts(message,idx,{isFinalMessage:idx===lastAsstIndex});
      const hasOrderedContentRows=Array.isArray(contentRows)&&contentRows.length>0;
      const contentToolRows=[];
      const usedContentToolRows=new Set();
      const idFlexibleContentToolRows=new Set();
      const seenToolIds=new Set();
      const rowByToolId=new Map();
      if(hasOrderedContentRows){
        for(const row of contentRows){
          pool.push({...row,_phase:1,_encounter:encounter++,_fromContent:true});
          const tid=row.tool_call_id||(row.tool&&row.tool.id);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
          if(row.role==='tool') contentToolRows.push(row);
        }
      }else if(_anchorSceneCleanText(text)){
        pool.push({..._anchorSceneProseRow(text,0,idx),_phase:2,_encounter:encounter++});
      }
      const reasoning=_anchorSceneMessageReasoningText(message);
      if(_anchorSceneCleanText(reasoning)&&_anchorSceneTextKey(reasoning)!==_anchorSceneTextKey(text)){
        pool.push({..._anchorSceneThinkingRow(reasoning,0,idx),_phase:0,_encounter:encounter++});
      }
      const messageTools=[];
      if(Array.isArray(message.tool_calls)) messageTools.push(...message.tool_calls);
      if(Array.isArray(message._partial_tool_calls)) messageTools.push(...message._partial_tool_calls);
      let messageToolOrdinal=0;
      for(const tool of messageTools){
        const row=_anchorSceneToolRowFromCall(tool,0,idx);
        const tid=row.tool_call_id||(row.tool&&row.tool.id);
        if(tid&&seenToolIds.has(tid)){
          const existing=rowByToolId.get(tid);
          if(existing){
            _enrichSettledToolRowBodyFromLive(existing, tool);
            if(contentToolRows.includes(existing)) usedContentToolRows.add(existing);
          }
          messageToolOrdinal+=1;
          continue;
        }
        const contentMatch=_anchorSceneMatchingContentToolRow(contentToolRows,row,messageToolOrdinal,usedContentToolRows,messageTools.length,idFlexibleContentToolRows);
        if(contentMatch){
          if(_anchorSceneToolRowsHaveDifferentExplicitIds(contentMatch,row)) idFlexibleContentToolRows.add(contentMatch);
          _enrichSettledToolRowBodyFromLive(contentMatch, tool);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,contentMatch); }
          usedContentToolRows.add(contentMatch);
          messageToolOrdinal+=1;
          continue;
        }
        pool.push({...row,_phase:1,_encounter:encounter++});
        if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
        messageToolOrdinal+=1;
      }
      // Merge S.toolCalls for this index, dedup by tool id. When a live call
      // matches a settled row already in the pool, don't just skip it —
      // restore any result body the settled row is missing (#4622): the live
      // S.toolCalls entry carries the full terminal output / patch diff that the
      // persisted state.db row may have dropped to a short preview or nothing.
      let liveToolOrdinal=0;
      for(const tool of (toolsByIdx.get(idx)||[])){
        if(!tool||typeof tool!=='object') continue;
        const toolIdx=Number(tool.assistant_msg_idx);
        if(!Number.isFinite(toolIdx)||toolIdx!==idx) continue;
        const row=_anchorSceneToolRowFromCall(tool,0,idx);
        const tid=row.tool_call_id||(row.tool&&row.tool.id);
        if(tid&&seenToolIds.has(tid)){
          const existing=rowByToolId.get(tid);
          if(existing){
            _enrichSettledToolRowBodyFromLive(existing, tool);
            if(contentToolRows.includes(existing)) usedContentToolRows.add(existing);
          }
          liveToolOrdinal+=1;
          continue;
        }
        const liveTools=toolsByIdx.get(idx)||[];
        const contentMatch=_anchorSceneMatchingContentToolRow(contentToolRows,row,liveToolOrdinal,usedContentToolRows,liveTools.length,idFlexibleContentToolRows);
        if(contentMatch){
          if(_anchorSceneToolRowsHaveDifferentExplicitIds(contentMatch,row)) idFlexibleContentToolRows.add(contentMatch);
          _enrichSettledToolRowBodyFromLive(contentMatch, tool);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,contentMatch); }
          usedContentToolRows.add(contentMatch);
          liveToolOrdinal+=1;
          continue;
        }
        if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
        pool.push({...row,_phase:1,_encounter:encounter++});
        liveToolOrdinal+=1;
      }
      // Stable sort by (phase, started_at, encounter). Once a message has an
      // ordered content[] scene, preserve that content bucket order exactly.
      const useStartedAt=!hasOrderedContentRows;
      pool.sort((a,b)=>{
        if(a._phase!==b._phase) return a._phase-b._phase;
        if(useStartedAt){
          const aTime=(a.tool&&a.tool.started_at!=null)?a.tool.started_at:Infinity;
          const bTime=(b.tool&&b.tool.started_at!=null)?b.tool.started_at:Infinity;
          if(aTime!==bTime) return aTime-bTime;
        }
        return a._encounter-b._encounter;
      });
      // Emit with sequential order_index values, strip temp props.
      // Rows were built with orderIndex=0, so their row_id/seq still encode 0.
      // Rewrite order_index AND regenerate the index-derived identity fields
      // (row_id/seq) from the final per-bucket position, so two anonymous rows
      // (no tool id) at the same message index don't collide on the same row_id
      // and get silently deduped by _completeSettledAnchorSceneForTurn().
      for(const row of pool){
        const {_phase,_encounter,_fromContent,...clean}=row;
        const oi=byIdx.has(idx)?byIdx.get(idx).length:0;
        clean.order_index=oi;
        clean.seq=oi;
        if(clean.identity&&typeof clean.identity==='object') clean.identity={...clean.identity,seq:oi};
        // Tool rows with a tool id carry a tid-based row_id (already unique) —
        // only regenerate the default index-based row_id form.
        const indexRowId=`settled:${activeSid||'session'}:${streamId||'stream'}:${clean.role}:${idx}:0`;
        if(clean.row_id===indexRowId){
          clean.row_id=`settled:${activeSid||'session'}:${streamId||'stream'}:${clean.role}:${idx}:${oi}`;
        }
        add(idx,clean);
      }
    }
    return byIdx;
  }
  function _anchorSceneExistingRowKey(row){
    if(!row||typeof row!=='object') return '';
    if(row.role==='tool'){
      const tool=row.tool&&typeof row.tool==='object'?row.tool:{};
      return `tool:${row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id||row.row_id||''}`;
    }
    if(row.role==='prose'||row.role==='thinking') return `${row.role}:${_anchorSceneTextKey(row.text)}`;
    return `${row.role||row.kind}:${row.source_event_type||''}:${row.status||''}:${row.row_id||''}`;
  }
  function _anchorSceneRowHasLiveIdentity(row){
    if(!row||typeof row!=='object') return false;
    const identity=row.identity&&typeof row.identity==='object'?row.identity:{};
    const values=[row.row_id,row.local_id,row.event_id,identity.local_id,identity.event_id];
    return values.some(value=>String(value||'').startsWith('live-'));
  }
  function _anchorSceneMessageRowsHaveThinking(messageRows){
    if(!(messageRows instanceof Map)) return false;
    for(const bucket of messageRows.values()){
      if(Array.isArray(bucket)&&bucket.some(row=>row&&row.role==='thinking')) return true;
    }
    return false;
  }
  function _anchorSceneSettleLiveRunningRow(row, hasSettledThinking){
    if(!row||typeof row!=='object') return row;
    if(row.role!=='thinking'&&row.role!=='prose'&&row.role!=='tool') return row;
    if(String(row.status||'').toLowerCase()!=='running') return row;
    if(!_anchorSceneRowHasLiveIdentity(row)) return row;
    if(row.role==='thinking'&&hasSettledThinking) return null;
    return {...row,status:'completed'};
  }
  function _anchorSceneRowLooksLikeFinalAnswer(rowTextKey, finalKey){
    if(!rowTextKey||!finalKey) return false;
    if(rowTextKey===finalKey) return true;
    // #4587: align with the renderer's _anchorSceneProseMatchesFinalAnswer — a
    // prefix-like overlap only counts as "the final answer" (and is dropped from
    // the scene) when it's a NEAR-complete match (ratio>=0.9). A shorter
    // intermediate-prose row that merely happens to be a prefix of the final
    // answer is legitimate progress narration and must be PRESERVED, not dropped.
    if(!(finalKey.startsWith(rowTextKey)||rowTextKey.startsWith(finalKey))) return false;
    const shorter=Math.min(rowTextKey.length,finalKey.length);
    const longer=Math.max(rowTextKey.length,finalKey.length);
    return shorter>=80&&longer>0&&(shorter/longer)>=0.9;
  }
  function _anchorSceneRowTextOverlapsExisting(rowTextKey, seenTextKeys){
    if(!rowTextKey||!Array.isArray(seenTextKeys)) return false;
    for(const existing of seenTextKeys){
      if(!existing) continue;
      if(rowTextKey===existing) return true;
      const minLen=Math.min(rowTextKey.length,existing.length);
      if(minLen>=80&&(rowTextKey.includes(existing)||existing.includes(rowTextKey))) return true;
    }
    return false;
  }
  function _anchorSceneTurnDurationForSettlement(lastAsst, base){
    if(lastAsst&&lastAsst._turnDuration!==undefined&&lastAsst._turnDuration!==null) return lastAsst._turnDuration;
    if(base&&base.turn_duration!==undefined&&base.turn_duration!==null) return base.turn_duration;
    const session=(typeof S!=='undefined'&&S&&S.session)?S.session:null;
    // The `pending_started_at` fallback below is the START of an IN-FLIGHT turn.
    // For a SETTLED turn that recorded no live duration, computing
    // `now - pending_started_at` is wrong: pending_started_at is either stale
    // (left over from an earlier turn / a session that sat idle) or belongs to a
    // different, still-pending turn — which rendered a bogus "Processed 15h 32m"
    // on fresh conversations (#4930). Only use it while a turn is actually in
    // flight; otherwise show no duration rather than a fabricated one.
    const turnInFlight=!!(session&&(session.active_stream_id||session.pending_user_message));
    if(!turnInFlight) return undefined;
    const candidates=[
      session&&session.pending_started_at,
      session&&session.active_started_at,
      session&&session.run_started_at,
      session&&session.started_at,
    ];
    for(const raw of candidates){
      const started=Number(raw);
      if(Number.isFinite(started)&&started>0){
        const elapsed=(Date.now()/1000)-started;
        if(Number.isFinite(elapsed)&&elapsed>=0) return elapsed;
      }
    }
    return undefined;
  }
  function _completeSettledAnchorSceneForTurn(messages, lastAsstIndex, projectedScene){
    if(!Array.isArray(messages)||lastAsstIndex<0) return projectedScene;
    const lastAsst=messages[lastAsstIndex];
    if(!lastAsst||lastAsst.role!=='assistant') return projectedScene;
    let turnStart=-1;
    for(let idx=lastAsstIndex-1;idx>=0;idx-=1){
      if(messages[idx]&&messages[idx].role==='user'){
        turnStart=idx;
        break;
      }
    }
    const base=(projectedScene&&typeof projectedScene==='object')?projectedScene:{};
    const sceneMode=base.mode==='transparent_stream'||base.mode==='hide_all_activity' ? base.mode : _anchorSceneActiveMode();
    const messageFinalAnswer=_anchorSceneFinalAnswerText(lastAsst);
    const finalAnswer=_anchorSceneCleanText(messageFinalAnswer)
      ? messageFinalAnswer
      : (typeof base.final_answer==='string'?base.final_answer:'');
    const finalKey=_anchorSceneTextKey(finalAnswer);
    const messageRows=_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex,{includeFinal:true});
    const hasSettledThinking=_anchorSceneMessageRowsHaveThinking(messageRows);
    const rows=[];
    const seen=new Set();
    const seenTextKeys=[];
    const projectedRows=Array.isArray(base.activity_rows)?base.activity_rows:[];
    const orderedRows=[];
    for(const row of projectedRows){
      if(row&&row.role==='terminal') continue;
      orderedRows.push(row);
    }
    for(let idx=turnStart+1;idx<=lastAsstIndex;idx+=1){
      const bucket=messageRows.get(idx)||[];
      for(const row of bucket) orderedRows.push(row);
    }
    for(const row of projectedRows){
      if(row&&row.role==='terminal') orderedRows.push(row);
    }
    // #5758 gap: final-segment eligibility must be judged against the LIVE
    // projection's own chronology. The settled per-message tool rows appended
    // into orderedRows above re-list tools that ran EARLIER in the turn, so an
    // index over the combined list pushes the "after the last tool row"
    // boundary past the final segment's live-prose accumulator — its stale
    // prefix snapshot then survives into the persisted scene and renders as a
    // duplicate of the answer's beginning. A live-prose row belongs to the
    // final segment iff no PROJECTED tool row follows it; pre-tool narration
    // that happens to prefix the final answer stays protected.
    const lastProjectedToolIndex=projectedRows.reduce((last,row,idx)=>(row&&row.role==='tool')?idx:last,-1);
    const finalSegmentLiveProseRows=new WeakSet();
    projectedRows.forEach((row,idx)=>{
      if(idx>lastProjectedToolIndex&&row&&row.role==='prose'&&row.kind==='process_prose'&&String(row.source_event_type||'')==='token'&&String(row.local_id||'').startsWith('live-prose:')) finalSegmentLiveProseRows.add(row);
    });
    const rowIsLiveTokenFinalPrefix=(row,textKey,finalSegmentEligible)=>finalSegmentEligible&&row&&row.role==='prose'&&row.kind==='process_prose'&&String(row.source_event_type||'')==='token'&&String(row.local_id||'').startsWith('live-prose:')&&textKey&&finalKey&&textKey.length<finalKey.length&&finalKey.startsWith(textKey);
    const pushRow=(row)=>{
      if(!row||typeof row!=='object') return;
      const finalSegmentEligible=finalSegmentLiveProseRows.has(row);
      row=_anchorSceneSettleLiveRunningRow(row,hasSettledThinking);
      if(!row||typeof row!=='object') return;
      const textKey=_anchorSceneTextKey(row.text);
      if(rowIsLiveTokenFinalPrefix(row,textKey,finalSegmentEligible)) return;
      const isTextual=row.role==='prose'||row.role==='thinking';
      if(isTextual&&_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)) return;
      if(isTextual&&_anchorSceneRowTextOverlapsExisting(textKey,seenTextKeys)) return;
      const key=_anchorSceneExistingRowKey(row);
      if(key&&seen.has(key)) return;
      if(key) seen.add(key);
      if(isTextual&&textKey) seenTextKeys.push(textKey);
      rows.push({
        ...row,
        display_hint:_anchorSceneRowDisplayHintForMode(row,sceneMode),
        order_index:rows.length,
        seq:rows.length,
      });
    };
    orderedRows.forEach((row)=>pushRow(row));
    const scene={
      ...base,
      version:'activity_scene_v1',
      mode:sceneMode,
      identity:{
        ...((base.identity&&typeof base.identity==='object')?base.identity:{}),
        source_message_refs:messages.slice(turnStart+1,lastAsstIndex+1)
          .filter(m=>m&&m.role==='assistant')
          .map(m=>_anchorSceneMessageRef(m)),
      },
      lifecycle:(base.lifecycle&&typeof base.lifecycle==='object')?{...base.lifecycle}:{},
      final_answer:_anchorSceneCleanText(finalAnswer)?finalAnswer:'',
      final_message_ref:_anchorSceneMessageRef(lastAsst),
      turn_duration:_anchorSceneTurnDurationForSettlement(lastAsst,base),
      terminal_state:base.terminal_state||((base.lifecycle&&base.lifecycle.terminal_state)||null),
      activity_rows:rows,
    };
    return scene;
  }
  let _persistAnchorSceneWarned=false;
  function _anchorSceneMessageOffsetForPersist(){
    const raw=(typeof _oldestIdx!=='undefined')?_oldestIdx:0;
    const offset=Number(raw);
    return Number.isFinite(offset)&&offset>0?Math.floor(offset):0;
  }
  function _anchorSceneAbsoluteMessageIndexForPersist(messageIndex, offset){
    const idx=Number(messageIndex);
    const off=Number(offset);
    if(!Number.isFinite(idx)||idx<0) return messageIndex;
    return idx+(Number.isFinite(off)&&off>0?Math.floor(off):0);
  }
  function _persistSettledAnchorScene(message, scene, messageIndex){
    if(!activeSid||!message||!scene||typeof api!=='function') return;
    try{
      const messageOffset=_anchorSceneMessageOffsetForPersist();
      api('/api/session/anchor-scene',{
        method:'POST',
        timeoutMs:8000,
        timeoutToast:false,
        body:JSON.stringify({
          session_id:activeSid,
          stream_id:streamId,
          message_index:_anchorSceneAbsoluteMessageIndexForPersist(messageIndex,messageOffset),
          message_window_index:messageIndex,
          message_offset:messageOffset,
          message_ref:_anchorSceneMessageRef(message),
          scene,
        }),
      }).catch(err=>{
        if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
          _persistAnchorSceneWarned=true;
          console.warn('anchor activity scene persistence failed',err);
        }
      });
    }catch(err){
      if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
        _persistAnchorSceneWarned=true;
        console.warn('anchor activity scene persistence failed',err);
      }
    }
  }
  function _anchorSceneHasWorklogWorthyRows(scene){
    if(scene&&scene.mode==='hide_all_activity') return false;
    if(typeof window!=='undefined'&&typeof window.isFinalAnswerOnlyMode==='function'&&window.isFinalAnswerOnlyMode()) return false;
    // A worklog (the collapsible "已处理 …" rail) is only meaningful when the turn
    // actually DID worklog-worthy work — a tool call, a thinking/reasoning pass, or
    // a compression lifecycle card. A turn that only streamed prose (e.g. a long
    // plain-text answer, or a degeneration burst that flooded the body with repeated
    // tokens) projects an activity scene whose rows are ALL `prose`/`terminal`. Folding
    // such a turn into a collapsed worklog hides the whole answer and, at STREAM_DONE,
    // shrinks the transcript by the full streamed height → the browser clamps a
    // bottom-pinned viewport back to the top (the "jump back" report). Require at least
    // one genuinely worklog-worthy row before promoting the turn to a worklog.
    const rows=Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[];
    for(const row of rows){
      if(!row||typeof row!=='object') continue;
      const role=String(row.role||'');
      if(role==='tool'||role==='thinking') return true;
      if(role==='lifecycle'){
        const source=String(row.source_event_type||'');
        // compression cards are worklog-worthy; a bare terminal/done lifecycle is not.
        if(source==='compressing'||source==='compressed') return true;
      }
    }
    return false;
  }
  function _attachProjectedAnchorSceneToLastAssistant(messages){
    if(!_anchorRegistry||!Array.isArray(messages)) return false;
    let lastAsst=null;
    let lastAsstIndex=-1;
    for(let i=messages.length-1;i>=0;i--){
      const candidate=messages[i];
      if(candidate&&candidate.role==='assistant'){
        lastAsst=candidate;
        lastAsstIndex=i;
        break;
      }
    }
    if(!lastAsst) return false;
    const projectedScene=_projectLiveAnchorActivityScene();
    const scene=_completeSettledAnchorSceneForTurn(messages,lastAsstIndex,projectedScene);
    if(scene&&Array.isArray(scene.activity_rows)&&scene.activity_rows.length){
      const hasWorklogRows=_anchorSceneHasWorklogWorthyRows(scene);
      const shouldPersistScene=hasWorklogRows||scene.mode==='hide_all_activity';
      if(!shouldPersistScene) return false;
      lastAsst._anchor_stream_id=streamId;
      lastAsst._anchor_activity_scene=scene;
      _persistSettledAnchorScene(lastAsst, scene, lastAsstIndex);
      return hasWorklogRows;
    }
    return false;
  }
  function _upsertAnchorProcessProse(displayText, options={}){
    const text=String(displayText||'').trim();
    if(!text||!_anchorRegistry) return null;
    const segmentSeq=Number(options.segmentSeq||_anchorSegmentSeq());
    const localId=`live-prose:${streamId}:${segmentSeq}`;
    const existing=_findAnchorActivityEventByLocalId(localId,'token');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'token',{
        status:options.sealed?'completed':'running',
        payload:{text,activitySegmentSeq:segmentSeq,activityBurstId:_currentActivityBurstId},
      });
      _renderAnchorLiveScene();
      return replaced;
    }
    _applyToAnchor('token',{
      text,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_currentActivityBurstId,
    },null);
    return _findAnchorActivityEventByLocalId(localId,'token');
  }
  // Persistent incremental renderer for anchor-scene live prose rows. The compact
  // worklog re-renders the whole scene each frame; rendering the growing prose via
  // renderMd(fullText) every frame is O(n^2) over a long answer. Instead keep a
  // per-segment smd parser + node (the SAME safe renderer as the main live body)
  // and feed only the delta, then hand the persistent node back to the ui.js scene
  // builder. Returns null whenever smd or a stable key is unavailable so the caller
  // falls back to the full renderMd path — identical structure, just not
  // incremental. (#5455 WS2.1)
  const _anchorProseSmdCache = new Map();
  function _anchorProseIncrementalNode(key, text){
    if(!window.smd || !key || typeof _safeSmdRenderer!=='function') return null;
    const value=String(text||'');
    const fade=typeof _shouldUseLiveProseFade==='function'&&_shouldUseLiveProseFade();
    try{
      let st=_anchorProseSmdCache.get(key);
      // Self-heal desyncs (edit/sanitize made the text no longer a pure append):
      // rebuild the parser+node from scratch, mirroring the _smdWrite guard.
      if(st && st.writtenText && !value.startsWith(st.writtenText)) st=null;
      if(st && st.fade!==fade) st=null;
      if(!st){
        const node=document.createElement('div');
        node.className='assistant-segment';
        node.setAttribute('data-anchor-scene-prose','1');
        const body=document.createElement('div');
        body.className='msg-body';
        if(body.classList) body.classList.toggle('stream-fade-active',fade);
        node.appendChild(body);
        const baseRenderer=fade?_streamFadeRenderer(body):_safeSmdRenderer(body);
        const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
        st={node,parser:window.smd.parser(renderer),writtenText:'',fade};
        _smdBindParserIdentity(renderer,st.parser,body);
        _anchorProseSmdCache.set(key,st);
        // Bound memory across turns: keys embed the stream id, so stale entries
        // from finished streams age out here.
        if(_anchorProseSmdCache.size>32){
          const oldest=_anchorProseSmdCache.keys().next().value;
          if(oldest!==key) _anchorProseSmdCache.delete(oldest);
        }
      }
      const body=st.node&&st.node.querySelector&&st.node.querySelector('.msg-body');
      if(body&&body.classList) body.classList.toggle('stream-fade-active',fade);
      const delta=value.slice(st.writtenText.length);
      if(delta){
        window.smd.parser_write(st.parser,delta);
        st.writtenText=value;
      }
      st.node.dataset.rawText=value;
      return st.node;
    }catch(_){
      _anchorProseSmdCache.delete(key);
      return null;
    }
  }
  window.__anchorProseIncrementalNode=_anchorProseIncrementalNode;
  function _clearAnchorProseIncrementalNode(){
    if(typeof window!=='undefined'&&window.__anchorProseIncrementalNode===_anchorProseIncrementalNode) window.__anchorProseIncrementalNode=null;
    // Clear the per-parser MEDIA tail for each cached smd parser.
    // _anchorProseSmdCache is a Map<key, {parser, ...}>; we can't
    // iterate a WeakMap to clean up, but WeakMap keys become eligible
    // for GC once the parser objects are released by the cache clear
    // below, so the WeakMap entries are automatically removed. The
    // explicit _smdMediaTailClear per-parser is a best-effort guard
    // for cache entries that may hold the last strong reference.
    if(typeof _anchorProseSmdCache!=='undefined'&&_anchorProseSmdCache.size){
      _anchorProseSmdCache.forEach(function(st){
        if(st&&st.parser&&typeof _smdMediaTailFlush==='function'){
          _smdMediaTailFlush(st.parser);
        }
        if(st&&st.parser&&typeof _smdMediaTailClear==='function'){
          _smdMediaTailClear(st.parser);
        }
      });
    }
    _anchorProseSmdCache.clear();
  }
  function _anchorHasReasoningEvents(){
    const events=_anchorActivityEvents();
    return !!(events&&events.some(event=>event&&event.source_event_type==='reasoning'));
  }
  function _upsertAnchorReasoning(text, options={}){
    const clean=String(text||'').trim();
    const placement=_liveThinkingPlacement();
    const segmentSeq=Number(options.segmentSeq||placement.segmentSeq||_anchorSegmentSeq());
    const localId=String(options.localId||`live-reasoning:${streamId}:${segmentSeq}`);
    if(options&&typeof options==='object'){
      options.anchorReasoningLocalId=localId;
      options.segmentSeq=segmentSeq;
      if(options.burstId===undefined) options.burstId=_currentActivityBurstId;
    }
    if(!clean||!_anchorRegistry||window._showThinking===false) return null;
    const existing=_findAnchorActivityEventByLocalId(localId,'reasoning');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'reasoning',{
        status:options.sealed?'completed':'running',
        payload:{text:clean,activitySegmentSeq:segmentSeq,activityBurstId:_currentActivityBurstId},
      });
      return _renderAnchorLiveScene()?replaced:null;
    }
    const renderOutcome={rendered:false};
    _applyToAnchor('reasoning',{
      text:clean,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_currentActivityBurstId,
    },null,renderOutcome);
    return renderOutcome.rendered?_findAnchorActivityEventByLocalId(localId,'reasoning'):null;
  }
  function _compactVisibleEchoText(value){
    return String(value||'').replace(/\s+/g,'');
  }
  function _stripCompactEchoSuffix(value, suffix){
    const raw=String(value||'');
    const candidate=_compactVisibleEchoText(suffix);
    if(!raw||!candidate) return {text:raw,removed:false};
    const windowSize=Math.max(String(suffix||'').length*3,4096);
    const offset=Math.max(0,raw.length-windowSize);
    const tail=raw.slice(offset);
    for(let idx=0;idx<=tail.length;idx+=1){
      if(_compactVisibleEchoText(tail.slice(idx))===candidate){
        return {text:raw.slice(0,offset+idx).trimEnd(),removed:true};
      }
    }
    return {text:raw,removed:false};
  }
  function _stripAnchorReasoningEcho(visible){
    const events=_anchorActivityEvents();
    if(!events||!visible) return false;
    for(let i=events.length-1;i>=0;i-=1){
      const event=events[i];
      if(!event||event.source_event_type!=='reasoning') continue;
      const payload=(event.payload&&typeof event.payload==='object')?event.payload:{};
      const rawText=String(payload.text||payload.reasoning||payload.thinking||'');
      const stripped=_stripCompactEchoSuffix(rawText, visible);
      if(!stripped.removed) continue;
      const nextText=String(stripped.text||'').trim();
      if(nextText){
        _replaceAnchorActivityEventByLocalId(event.local_id,'reasoning',{
          payload:{text:nextText},
        });
      }else{
        events.splice(i,1);
      }
      _renderAnchorLiveScene();
      return true;
    }
    return false;
  }
  function _removeLiveReasoningEchoRows(visible){
    const turn=$('liveAssistantTurn');
    const blocks=turn&&typeof _assistantTurnBlocks==='function'?_assistantTurnBlocks(turn):null;
    if(!blocks||!visible) return false;
    let removed=false;
    const selector=[
      '.agent-activity-thinking[data-anchor-scene-row="1"]',
      '.agent-activity-thinking[data-live-thinking="1"]',
      '.wl-reason[data-worklog-anchor-reason="1"]',
      '.wl-reason[data-worklog-reason-source="reasoning"]'
    ].join(',');
    blocks.querySelectorAll(selector).forEach(row=>{
      const textNode=row.querySelector&&(
        row.querySelector('.thinking-card-body pre') ||
        row.querySelector('.thinking-card-body')
      );
      const text=String((textNode&&textNode.textContent)||row.textContent||'');
      if(!_stripCompactEchoSuffix(text, visible).removed) return;
      row.remove();
      removed=true;
    });
    if(removed&&typeof _syncToolCallGroupSummary==='function'){
      blocks.querySelectorAll('.tool-worklog-group,.tool-call-group').forEach(group=>{
        _syncToolCallGroupSummary(group);
      });
    }
    return removed;
  }
  function _stripLiveReasoningEcho(visible){
    let removed=false;
    const durable=_stripCompactEchoSuffix(reasoningText, visible);
    if(durable.removed){
      reasoningText=durable.text;
      removed=true;
    }
    const live=_stripCompactEchoSuffix(liveReasoningText, visible);
    if(live.removed){
      liveReasoningText=live.text;
      removed=true;
    }
    const anchorRemoved=_stripAnchorReasoningEcho(visible);
    const domRemoved=_removeLiveReasoningEchoRows(visible);
    if(removed) syncInflightAssistantMessage();
    if((removed||anchorRemoved||domRemoved)&&!String(liveReasoningText||'').trim()&&typeof removeThinking==='function'){
      removeThinking();
    }
    return removed||anchorRemoved||domRemoved;
  }
  function _flushReasoningToAnchor(){
    if(_anchorReasoningFlushed||!reasoningText) return;
    _anchorReasoningFlushed=true;
    if(_anchorHasReasoningEvents()) return;
    _upsertAnchorReasoning(reasoningText,{sealed:true,localId:`live-reasoning:${streamId}:final`});
  }
  function _sourceEventTypeForSnapshotAnchorRow(row){
    const source=String(row&&row.source_event_type||'').trim();
    if(source&&source!=='runtime_journal_snapshot') return source;
    const role=String(row&&row.role||'').trim();
    const kind=String(row&&row.kind||'').trim();
    if(role==='prose'||kind==='process_prose') return 'token';
    if(role==='thinking'||kind==='reasoning') return 'reasoning';
    if(role==='tool') return row&&row.status==='running'?'tool':'tool_complete';
    // Terminal statuses are done/cancel/error/apperror — never invent a
    // compression start from a running terminal row (false "Compressing context").
    if(role==='terminal'||kind==='terminal_status'){
      const termStatus=String(row&&row.status||'').trim().toLowerCase();
      if(termStatus==='cancelled'||termStatus==='canceled'||termStatus==='interrupted') return 'cancel';
      if(termStatus==='error'||termStatus==='failed'||termStatus==='errored') return 'error';
      if(termStatus==='running') return '';
      return 'done';
    }
    // lifecycle_status is shared by compressing + compressed. Prefer explicit
    // cues; do not default every lifecycle row to a running compress divider.
    if(role==='lifecycle'||kind==='lifecycle_status'){
      const phase=String(row&&(row.phase||row.status)||'').trim().toLowerCase();
      const text=String(row&&(row.text||row.message||row.label)||'').trim().toLowerCase();
      if(
        phase==='done'||phase==='completed'||phase==='compressed'
        || text.includes('auto-compressed')
        || text.includes('compression finished')
        || (text.includes('compressed')&&!text.includes('compressing'))
      ) return 'compressed';
      if(
        phase==='running'||phase==='compressing'
        || text.includes('compressing context')
        || text.includes('compacting context')
        || text.includes('preflight compression')
        || text.includes('pre-api compression')
        || text.includes('context too large')
        || text.includes('compression attempt')
        || (text.includes('compressing')&&!text.includes('skipping'))
      ) return 'compressing';
      return '';
    }
    return '';
  }
  function _hydrateAnchorRegistryFromActivityScene(scene){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return false;
    if(!scene||scene.version!=='activity_scene_v1'||!Array.isArray(scene.activity_rows)||!scene.activity_rows.length) return false;
    const sceneKey=[
      scene.identity&&scene.identity.stream_id||streamId||'',
      scene.activity_rows.length,
      scene.activity_rows.map(row=>row&&row.row_id||row&&row.local_id||'').join('|'),
    ].join(':');
    if(_anchorRegistry._hydrated_activity_scene_key===sceneKey) return true;
    const rows=scene.activity_rows;
    for(let i=0;i<rows.length;i+=1){
      const row=rows[i];
      if(!row||typeof row!=='object') continue;
      const sourceType=_sourceEventTypeForSnapshotAnchorRow(row);
      if(!sourceType) continue;
      const payload={
        ...((row.payload&&typeof row.payload==='object')?row.payload:{}),
      };
      if(row.text&&!payload.text) payload.text=row.text;
      if(row.tool&&typeof row.tool==='object'){
        payload.name=payload.name||row.tool.name;
        payload.args=payload.args||row.tool.args;
        payload.preview=payload.preview||row.tool.preview;
        payload.snippet=payload.snippet||row.tool.snippet;
        payload.tid=payload.tid||row.tool.tid||row.tool.id;
        payload.id=payload.id||row.tool.id||row.tool.tid;
        payload.is_error=payload.is_error||row.tool.is_error;
        payload.duration=payload.duration||row.tool.duration;
      }
      if(row.group&&typeof row.group==='object'){
        payload.activitySegmentSeq=payload.activitySegmentSeq||row.group.activity_segment_seq;
        payload.activityBurstId=payload.activityBurstId||row.group.activity_burst_id;
      }
      const sourceEvent={
        ...payload,
        source_event_type:sourceType,
        local_id:row.local_id||row.row_id||`snapshot:${streamId}:${i}`,
        event_id:row.event_id||null,
        seq:row.seq??undefined,
        status:row.status||undefined,
        stream_id:row.stream_id||streamId,
        run_id:row.run_id||streamId,
        // Carry the row's persisted creation timestamp through hydration so the
        // worklog event timestamp (#5700/#5739) survives a settled-snapshot rebuild
        // (payload may not carry created_at even when the row does). (#5739 gate.)
        created_at:payload.created_at??row.created_at??undefined,
      };
      try{
        _anchorApi.applyAssistantTurnAnchorSourceEvent(_anchorRegistry,sourceEvent,{session_id:activeSid,stream_id:streamId,run_id:streamId});
      }catch(err){
        if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
          _anchorShadowWarned=true;
          console.warn('assistant turn anchor snapshot hydration failed',err);
        }
        return false;
      }
    }
    _anchorRegistry._hydrated_activity_scene_key=sceneKey;
    return true;
  }
  _hydrateAnchorRegistryFromActivityScene(INFLIGHT[activeSid]&&INFLIGHT[activeSid].anchorActivityScene);

  function _mergeSettledToolCallsWithLiveMetadata(rawCalls){
    const liveCalls=Array.isArray(S.toolCalls)?S.toolCalls:[];
    const byTid=new Map();
    liveCalls.forEach((tc,idx)=>{
      if(!tc||typeof tc!=='object') return;
      const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
      if(tid&&!byTid.has(tid)) byTid.set(tid,{tc,idx});
    });
    const used=new Set();
    return (rawCalls||[]).map((raw,idx)=>{
      const next={...(raw||{}),done:true};
      const tid=next.tid||next.id||next.tool_call_id||next.tool_use_id||next.call_id||'';
      let matchEntry=tid?byTid.get(tid):null;
      if(!matchEntry){
        const name=next.name||((next.function||{}).name)||'';
        const matchIdx=liveCalls.findIndex((tc,i)=>tc&&!used.has(i)&&(!name||tc.name===name));
        if(matchIdx>=0) matchEntry={tc:liveCalls[matchIdx],idx:matchIdx};
      }
      if(matchEntry){
        used.add(matchEntry.idx);
        const live=matchEntry.tc||{};
        for(const key of ['activityBurstId','duration','started_at']){
          if((next[key]===undefined||next[key]===null)&&live[key]!==undefined&&live[key]!==null) next[key]=live[key];
        }
      }
      return next;
    });
  }

  // rAF-throttled rendering: buffer tokens, render at most once per frame
  let _renderPending=false;
  // Extract display text from assistantText, stripping completed thinking blocks
  // and hiding content still inside an open thinking block.
  function _stripXmlToolCalls(s){
    // Strip <function_calls>...</function_calls> blocks (DeepSeek XML tool syntax).
    // These are processed as tool calls server-side; showing them raw in the bubble
    // looks broken. Also handles orphaned opening tags mid-stream. (#702)
    // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
    // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
    if(!s) return s;
    // Case-insensitive presence check without allocating a full lowercased copy
    // of the (growing) text on every call — cuts per-token/per-frame GC pressure.
    // Equivalent to the previous toLowerCase()+indexOf gate. (#5455 WS2.3)
    if(!/function_calls|dsml/i.test(String(s))) return s;
    // Support both plain <function_calls> and DSML-prefixed variants.
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
    // Also remove truncated opening tags (missing closing ">" at stream tail).
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
    // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
    s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
    return s.trim();
  }
  function _streamDisplay(){
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true}).content;
  }
  function _parseStreamState(){
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true});
  }
  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      _updateLiveThinkingCard(text||'Thinking…');
      return;
    }
    // Only remove thinking if we're not in an active reasoning phase.
    // When reasoningText is set but liveReasoningText was just reset (post-tool),
    // don't wipe the finalized thinking card — it has no id anymore so
    // removeThinking() won't find it anyway, but guard explicitly.
    if(!reasoningText) removeThinking();
  }
  // Helper: create (or recreate) the smd parser bound to a given DOM element.
  // Called when assistantBody is first created and after each tool-call segment reset.
  function _smdNewParser(el, fade=false){
    _smdWrittenLen=0;
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    const baseRenderer=fade ? _streamFadeRenderer(el) : _safeSmdRenderer(el);
    const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
    _smdParser=window.smd.parser(renderer);
    _smdBindParserIdentity(renderer,_smdParser,el);
  }
  function _smdRendererWithoutUnderscoreEmphasis(renderer){
    if(!renderer||!window.smd) return renderer;
    const baseAddToken=renderer.add_token;
    const baseEndToken=renderer.end_token;
    const baseAddText=renderer.add_text;
    const tokenStack=[];
    renderer.add_token=(data,token)=>{
      if(token===window.smd.ITALIC_UND||token===window.smd.STRONG_UND){
        const marker=token===window.smd.STRONG_UND?'__':'_';
        tokenStack.push(marker);
        baseAddText(data,marker);
        return;
      }
      tokenStack.push(null);
      baseAddToken(data,token);
    };
    renderer.end_token=(data)=>{
      const marker=tokenStack.pop();
      if(marker){
        baseAddText(data,marker);
        return;
      }
      baseEndToken(data);
    };
    return renderer;
  }
  // Helper: end the current smd parser (flushes remaining state) and null it out.
  function _smdEndParser(){
    if(_streamingKatexTimer){clearTimeout(_streamingKatexTimer);_streamingKatexTimer=null;}
    if(_smdParser&&window.smd){
      try{window.smd.parser_end(_smdParser);}catch(_){}
    }
    // parser_end may emit one final add_text chunk; flush MEDIA tails after it
    // so a final extensionless URL is rendered before the settled re-render.
    if(typeof _smdMediaTailFlush==='function') _smdMediaTailFlush(_smdParser);
    if(typeof _smdMediaTailFlush==='function') _smdMediaTailFlush(__SMD_PARSER_FALLBACK);
    // parser_end / tail flush may create new links/images — re-sanitize the
    // body before the DOM is handed off to highlightCode / renderMessages.
    if(assistantBody){_sanitizeSmdLinks(assistantBody);enhanceMarkdownTables(assistantBody);}
    // Clear the per-parser MEDIA tail buffer — any incomplete MEDIA
    // prefix the parser was holding is no longer relevant.
    if(typeof _smdMediaTailClear==='function') _smdMediaTailClear(_smdParser);
    if(typeof _smdClearParserIdentity==='function') _smdClearParserIdentity(assistantBody,_smdParser);
    _smdParser=null;
    _smdWrittenLen=0;
    _smdWrittenText='';
    // Clear the fallback MEDIA tail buffer too; fallback chunks are keyed
    // by __SMD_PARSER_FALLBACK, not null.
    if(typeof _smdMediaTailClear==='function') _smdMediaTailClear(__SMD_PARSER_FALLBACK);
  }
  function _scheduleStreamingKatex(){
    if(_streamingKatexTimer) return;
    _streamingKatexTimer=setTimeout(()=>{
      _streamingKatexTimer=null;
      if(assistantBody&&typeof renderKatexBlocks==='function') renderKatexBlocks(assistantBody,{streaming:true});
    },150);
  }
  // Helper: feed new displayText delta to the smd parser.
  // Only feeds chars beyond what has already been written (_smdWrittenLen).
  function _smdWrite(displayText, fade=false){
    if(!_smdParser||!window.smd) return;
    displayText=String(displayText||'');
    // Self-heal desyncs: if displayText no longer starts with what we've already
    // written (e.g. due to stream sanitization/tag stripping), incremental slicing
    // can skip characters. Rebuild parser from the full current displayText.
    if(_smdWrittenText && !displayText.startsWith(_smdWrittenText)){
      _smdParser=null;
      _smdWrittenLen=0;
      _smdWrittenText='';
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody,fade);
      if(!_smdParser) return;
    }
    const delta=displayText.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenLen=displayText.length;
    _smdWrittenText=displayText;
    // URL scheme safety is handled by the renderer's set_attr hook
    // (_safeSmdRenderer or _streamFadeRenderer), applied inline as smd
    // creates each DOM node — no post-hoc full-DOM scan needed.
    _scheduleStreamingKatex();
  }
  // Allowed URL schemes for anchors and images rendered from agent-streamed markdown.
  // Raw file:// anchors are rewritten to /api/media before the user can click them.
  const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|\/|#|\?|\.|api|session\/)/i;
  // ui.js owns the image-only data URI policy. It loads before this script;
  // fail closed if that contract is unavailable rather than inventing a second
  // allowlist that can drift from settled rendering.
  const _SMD_SAFE_IMG_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;
  function _smdImgSrcAllowed(v){
    const s=String(v||'');
    if(/^data:/i.test(s)) return typeof _isSafeDataImageUri==='function'&&_isSafeDataImageUri(s);
    return _SMD_SAFE_IMG_URL_RE.test(s);
  }
  function _smdLinkHref(raw){
    const href=String(raw||'');
    if(/^session:\/\//i.test(href)){
      const sid=href.replace(/^session:\/\//i,'').split(/[?#]/)[0];
      try{
        const decoded=decodeURIComponent(sid);
        if(typeof _sessionUrlForSid==='function') return _sessionUrlForSid(decoded);
        return 'session/'+encodeURIComponent(decoded);
      }catch(_){
        return 'session/'+encodeURIComponent(sid);
      }
    }
    if(/^workspace:\/\//i.test(href)){
      try{
        const rel=decodeURIComponent(href.replace(/^workspace:\/\//i,'')).replace(/^~\//,'').replace(/^\.\//,'');
        return '#workspace='+encodeURIComponent(rel);
      }catch(_){
        return '#';
      }
    }
    if(!/^file:\/\//i.test(href)) return href;
    try{
      const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
      return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
    }catch(_){
      return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
    }
  }
  function _smdFileHref(raw){
    return _smdLinkHref(raw);
  }
  function _sanitizeSmdLinks(root){
    if(!root||!root.querySelectorAll) return;
    const _a=root.querySelectorAll('a[href]');
    for(let i=0;i<_a.length;i++){
      const n=_a[i],v=n.getAttribute('href')||'';
      if(/^(file|workspace|session):\/\//i.test(v)){n.setAttribute('href',_smdLinkHref(v));n.classList&&/^session:\/\//i.test(v)&&n.classList.add('session-link');continue;}
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('href');n.setAttribute('data-blocked-scheme','1');}
    }
    const _im=root.querySelectorAll('img[src]');
    for(let i=0;i<_im.length;i++){
      const n=_im[i],v=n.getAttribute('src')||'';
      if(!_smdImgSrcAllowed(v)){n.removeAttribute('src');n.setAttribute('data-blocked-scheme','1');}
    }
  }

  function _resetStreamFadeState(){
    _streamFadeVisibleText='';
    _streamFadeLastTickMs=0;
    _streamFadeWordCarry=0;
    _streamFadeStartedAt=0;
    _streamFadeLastTargetWords=0;
    _streamFadeLastArrivalMs=0;
    _streamFadeArrivalWps=0;
    _streamFadeLatestAnimationEndAt=0;
    _streamFadeVisibleWords=0;
    _streamFadeHoldUntilMs=0;
    _streamFadeCurrentMs=_STREAM_FADE_MS;
    _streamFadeDomText='';
  }
  function _cancelAnimationFramePendingStreamRender(){
    if(_pendingRafHandle===null) return;
    cancelAnimationFrame(_pendingRafHandle);
    clearTimeout(_pendingRafHandle);
    _pendingRafHandle=null;
    _renderPending=false;
  }
  function _shouldUseStreamFade(){
    return window._fadeTextEffect===true;
  }
  function _shouldUseTransparentStreamFade(){
    return typeof isTransparentStream==='function'&&isTransparentStream();
  }
  function _shouldUseLiveProseFade(){
    return !_streamFadeReduceMotionEnabled() && (_shouldUseStreamFade() || _shouldUseTransparentStreamFade());
  }
  function _streamFadeSkipNode(node){
    if(!node||node.nodeType!==1) return false;
    const tag=(node.tagName||'').toLowerCase();
    return tag==='pre'||tag==='code'||tag==='script'||tag==='style'||tag==='textarea'||tag==='svg'||tag==='math';
  }
  function _streamFadeReduceMotionEnabled(){
    if(!window.matchMedia) return false;
    if(!_streamFadeReduceMotionMql){
      _streamFadeReduceMotionMql=window.matchMedia('(prefers-reduced-motion: reduce)');
      _streamFadeReduceMotion=!!_streamFadeReduceMotionMql.matches;
      _streamFadeReduceMotionOnChange=e=>{_streamFadeReduceMotion=!!e.matches;};
      try{_streamFadeReduceMotionMql.addEventListener('change',_streamFadeReduceMotionOnChange);}
      catch(_){try{_streamFadeReduceMotionMql.addListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    }
    return _streamFadeReduceMotion;
  }
  function _streamFadeCleanupReduceMotionListener(){
    if(!_streamFadeReduceMotionMql||!_streamFadeReduceMotionOnChange) return;
    try{_streamFadeReduceMotionMql.removeEventListener('change',_streamFadeReduceMotionOnChange);}
    catch(_){try{_streamFadeReduceMotionMql.removeListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    _streamFadeReduceMotionMql=null;
    _streamFadeReduceMotionOnChange=null;
  }
  function _streamFadeBindCleanup(el){
    if(!el||el._streamFadeCleanupBound) return;
    el._streamFadeCleanupBound=true;
    el.addEventListener('animationend',e=>{
      const span=e.target;
      if(!span||!span.classList||!span.classList.contains('stream-fade-word')) return;
      span.replaceWith(document.createTextNode(span.textContent||''));
    });
  }
  function _streamFadeRenderer(el){
    _streamFadeBindCleanup(el);
    const renderer=window.smd.default_renderer(el);
    const baseAddText=renderer.add_text;
    const baseSetAttr=renderer.set_attr;
    const parserFor = (data)=>{
      return _smdParserKey(data, el);
    };
    const writeFadeText=(writeParent, writeData, writeText)=>{
      if(!writeParent||_streamFadeSkipNode(writeParent)){
        _smdAppendPlainText(writeParent, writeData, writeText, baseAddText);
        return;
      }
      _streamFadeAppendText(writeParent, writeText);
    };
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      if(!parent||_streamFadeSkipNode(parent)){baseAddText(data,text);return;}
      // MEDIA-in-stream: if this chunk carries a MEDIA:<ref> token, defer to
      // the shared interceptor so the token becomes a real media element
      // instead of plain text. The fade renderer would otherwise wrap every
      // word in a stream-fade-word span, leaving MEDIA: paths visible.
      const parser=parserFor(data);
      const hasMediaTail=!!(_SMD_MEDIA_TAIL&&parser&&_SMD_MEDIA_TAIL.has&&_SMD_MEDIA_TAIL.has(parser));
      const value=String(text||'');
      const hasMediaPrefixTail=!!_smdMediaPrefixTail(value);
      if(/MEDIA:/.test(value)||hasMediaTail||hasMediaPrefixTail){
        _smdMediaAwareAddText(baseAddText, parent, data, text, _SMD_MEDIA_TAIL, parser, writeFadeText);
        return;
      }
      const frag=document.createDocumentFragment();
      const wordRe=/(\S+)(\s*)/g;
      const reduceMotion=_streamFadeReduceMotionEnabled();
      const appendStartedAt=performance.now();
      let last=0, match, changed=false;
      while((match=wordRe.exec(value))){
        if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
        if(reduceMotion){
          frag.appendChild(document.createTextNode(match[1]));
          if(match[2]) frag.appendChild(document.createTextNode(match[2]));
          last=match.index+match[0].length;
          changed=true;
          continue;
        }
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+fadeMs);
        if(match[2]) frag.appendChild(document.createTextNode(match[2]));
        last=match.index+match[0].length;
        changed=true;
      }
      if(!changed){baseAddText(data,text);return;}
      if(last<value.length) frag.appendChild(document.createTextNode(value.slice(last)));
      parent.appendChild(frag);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      const allowed=isSrc?_smdImgSrcAllowed(value):_SMD_SAFE_URL_RE.test(String(value||''));
      if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
        baseSetAttr(data,attr,_smdLinkHref(value));
        if(/^session:\/\//i.test(String(value||''))){
          const node=data&&data.nodes&&data.nodes[data.index];
          if(node&&node.classList) node.classList.add('session-link');
        }
        return;
      }
      if((isHref||isSrc)&&!allowed){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  // Safe renderer: wraps default_renderer with a set_attr hook that validates
  // href/src URL schemes inline — no post-hoc DOM-wide querySelectorAll needed.
  // Unlike _streamFadeRenderer, this does NOT wrap add_text, so smd adds new
  // DOM nodes as plain text nodes (no animation spans). Used on the non-fade
  // streaming path to eliminate _sanitizeSmdLinks(assistantBody) O(DOM) scans
  // on every token event (#WebUI-perf).
  // MEDIA-in-stream fix: also wraps add_text so MEDIA:<ref> tokens that arrive
  // mid-turn are converted to inline media elements at insert time, matching
  // what the full renderMd() pipeline does on the settled assistant message.
  // Without this, streamed prose shows MEDIA:C:\... as literal text until the
  // turn settles and the full re-render swaps it for the real <img>.
  // SAFETY & CROSS-CHUNK SPLITS (Greptile #1 + #2):
  //   1. Prose slices go back to the owning text writer (text nodes or
  //      fade spans), NOT through DOMParser — mixed prose with HTML entities /
  //      malicious <img onerror> stays as literal text.
  //   2. Each MEDIA token's HTML (from _inlineMediaHtmlForRef) is handed
  //      to DOMParser one at a time — only trusted markup is parsed.
  //   3. A MEDIA prefix split across smd flushes (e.g. "MEDIA:" then
  //      "foo.png") is buffered in a per-parser tail buffer and completed
  //      on the next add_text call.
  const _MEDIA_TAIL_MAX = 4096; // bytes; defensive cap on per-parser buffer
  const _SMD_MEDIA_PREFIX = 'MEDIA:';
  function _smdMediaPrefixTail(value){
    const text=String(value||'');
    const max=Math.min(_SMD_MEDIA_PREFIX.length,text.length);
    for(let len=max;len>0;len-=1){
      const suffix=text.slice(text.length-len);
      if(_SMD_MEDIA_PREFIX.startsWith(suffix)) return suffix;
    }
    return '';
  }
  function _smdAppendPlainText(parent, data, text, baseAddText){
    const value=String(text||'');
    if(parent&&parent.appendChild&&typeof document!=='undefined'&&document.createTextNode){
      parent.appendChild(document.createTextNode(value));
      return;
    }
    if(baseAddText) baseAddText(data,value);
  }
  function _smdMediaWriteText(parent, data, baseAddText, writeText, text){
    if(writeText){
      writeText(parent, data, String(text||''));
      return;
    }
    if(baseAddText) baseAddText(data,String(text||''));
  }
  function _smdMediaTailSet(tailMap, parser, chunk, parent, baseAddText, data, writeText){
    if(!tailMap||!parser) return;
    if(chunk) tailMap.set(parser, {chunk, parent, baseAddText, data, writeText});
    else tailMap.delete(parser);
  }
  function _smdMediaTailEntryChunk(entry){
    return entry && typeof entry==='object' && Object.prototype.hasOwnProperty.call(entry,'chunk') ? entry.chunk : entry;
  }
  function _smdMediaTailSameOwner(entry, parent, baseAddText, writeText){
    return !!entry && entry.parent===parent && entry.baseAddText===baseAddText && entry.writeText===writeText;
  }
  function _smdMediaRefHasReliableBoundary(rawRef){
    const raw=String(rawRef||'');
    if(/[?#]$/.test(raw)) return false;
    const ref=raw.split(/[?#]/,1)[0];
    return /\.(?:png|jpe?g|gif|webp|bmp|ico|svg|avif|mp4|webm|mov|m4v|mkv|avi|ogv|mp3|wav|ogg|m4a|aac|wma|opus|flac|oga|pdf|html?|csv|diff|patch|excalidraw)$/i.test(ref);
  }
  function _smdMediaTailFlushEntry(entry){
    const chunk=_smdMediaTailEntryChunk(entry);
    if(!chunk) return;
    const m=/^MEDIA:([^\s\)\]]+)$/.exec(String(chunk));
    const emitted=!!(m && entry && entry.parent && _smdAppendMediaNode(entry.parent, m[1]));
    if(!emitted && entry) _smdMediaWriteText(entry.parent, entry.data, entry.baseAddText, entry.writeText, chunk);
  }
  function _smdMediaTailFlush(parser){
    if(!_SMD_MEDIA_TAIL||!parser||!_SMD_MEDIA_TAIL.get) return;
    const entry=_SMD_MEDIA_TAIL.get(parser);
    if(!entry) return;
    _SMD_MEDIA_TAIL.delete(parser);
    _smdMediaTailFlushEntry(entry);
  }
  function _smdMediaAwareAddText(baseAddText, parent, data, text, tailMap, parser, writeText){
    const value=String(text||'');
    const tails=tailMap||(typeof _SMD_MEDIA_TAIL!=='undefined'&&_SMD_MEDIA_TAIL)||null;
    const writeCurrent=(chunk)=>_smdMediaWriteText(parent, data, baseAddText, writeText, chunk);
    if(!value){
      writeCurrent('');
      return;
    }
    // Pull any pending tail from a previous (split) chunk, then clear it;
    // this call will either complete it, re-buffer it, or flush it as text.
    let leadEntry = tails && parser && tails.get ? tails.get(parser) : null;
    let lead = _smdMediaTailEntryChunk(leadEntry);
    if(lead && !_smdMediaTailSameOwner(leadEntry, parent, baseAddText, writeText)){
      if(tails && parser && tails.delete) tails.delete(parser);
      _smdMediaTailFlushEntry(leadEntry);
      leadEntry=null;
      lead='';
    }else if(lead && tails && parser && tails.delete){
      tails.delete(parser);
    }
    const combined = lead ? lead + value : value;
    // Fast path: no MEDIA tokens in the (possibly combined) string.
    if(!/MEDIA:/.test(combined)){
      const prefixTail=_smdMediaPrefixTail(combined);
      if(prefixTail && tails && parser && prefixTail.length < _MEDIA_TAIL_MAX){
        const stable=combined.slice(0, combined.length-prefixTail.length);
        if(stable) writeCurrent(stable);
        _smdMediaTailSet(tails, parser, prefixTail, parent, baseAddText, data, writeText);
        return;
      }
      writeCurrent(combined);
      return;
    }
    // Walk the combined string, slicing into prose + MEDIA token runs.
    // Prose runs go through the owning text writer. MEDIA tokens go through
    // the single-token DOMParser helper only after a delimiter or
    // reliable filename suffix proves the ref is complete.
    const re=/MEDIA:([^\s\)\]]+)/g;
    let last=0, m;
    let unmatchedTail=null;
    while((m=re.exec(combined))){
      const matchEnd = m.index + m[0].length;
      if(m.index>last){
        const slice = combined.slice(last, m.index);
        writeCurrent(slice);
      }
      if(matchEnd===combined.length && !_smdMediaRefHasReliableBoundary(m[1])){
        const candidate = combined.slice(m.index);
        if(candidate.length < _MEDIA_TAIL_MAX){
          unmatchedTail = candidate;
        } else {
          writeCurrent(candidate);
        }
        last = combined.length;
        break;
      }
      if(!_smdAppendMediaNode(parent, m[1])) writeCurrent(m[0]);
      last = matchEnd;
    }
    // Tail buffer — hold trailing bytes that look like an unterminated
    // MEDIA prefix; flush any prose before the partial MEDIA suffix.
    const rest = combined.slice(last);
    if(rest){
      const tailMatch = /MEDIA:[^\s\)\]]*$/.exec(rest);
      const prefixTail = tailMatch ? '' : _smdMediaPrefixTail(rest);
      const tailValue = tailMatch ? tailMatch[0] : prefixTail;
      if(tailValue && rest.length < _MEDIA_TAIL_MAX){
        const tailStart = tailMatch ? tailMatch.index : rest.length-prefixTail.length;
        if(tailStart>0) writeCurrent(rest.slice(0, tailStart));
        unmatchedTail = tailValue;
      } else {
        writeCurrent(rest);
      }
    }
    if(tails && parser){
      _smdMediaTailSet(tails, parser, unmatchedTail, parent, baseAddText, data, writeText);
    }
  }
  // Single-token DOM splice. Only ever fed the output of
  // _inlineMediaHtmlForRef (trusted markup fragment). Plain text
  // goes through baseAddText → createTextNode — NEVER here.
  function _smdAppendMediaNode(parent, rawRef){
    if(!parent||!rawRef) return false;
    const mediaHtml = (typeof _inlineMediaHtmlForRef==='function')
      ? _inlineMediaHtmlForRef(String(rawRef))
      : '';
    if(!mediaHtml) return false;
    let host=null;
    try{
      const doc=new DOMParser().parseFromString('<div>'+mediaHtml+'</div>','text/html');
      host=doc.body&&doc.body.firstChild;
    }catch(_){ host=null; }
    if(!host||!host.childNodes||!host.childNodes.length) return false;
    const frag=document.createDocumentFragment();
    while(host.firstChild) frag.appendChild(host.firstChild);
    parent.appendChild(frag);
    _smdScheduleMediaPostProcess(parent);
    return true;
  }
  function _smdScheduleMediaPostProcess(root){
    if(!root) return;
    if(typeof _postProcessWithAnchorSuppression!=='function'
      && typeof postProcessRenderedMessages!=='function'
      && typeof _applyMediaPlaybackPreferences!=='function') return;
    const run=()=>{
      try{
        if(typeof _postProcessWithAnchorSuppression==='function') _postProcessWithAnchorSuppression(root);
        else if(typeof postProcessRenderedMessages==='function') postProcessRenderedMessages(root);
        if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(root);
      }catch(_){}
    };
    if(typeof requestAnimationFrame==='function') requestAnimationFrame(run);
    else if(typeof setTimeout==='function') setTimeout(run,0);
    else run();
  }
  // Per-parser tail buffer keyed by parser instance so concurrent
  // smd parsers (live prose + anchor-scene rows + tool-card streams)
  // keep their own pending bytes. Cleared inside _smdEndParser /
  // _clearAnchorProseIncrementalNode on stream end.
  const _SMD_MEDIA_TAIL = (typeof WeakMap!=='undefined') ? new WeakMap() : new Map();
  // Sentinel for parserFor fallback — a dedicated object instead of
  // a string, so WeakMap.set doesn't throw TypeError when all three
  // parser-identity sources are unavailable (Greptile #3).
  const __SMD_PARSER_FALLBACK = {};
  function _smdParserKey(data, el){
    return (data && data.parser) || (el && el.__smdParser) || __SMD_PARSER_FALLBACK;
  }
  function _smdBindParserIdentity(renderer, parser, el){
    if(renderer&&renderer.data) renderer.data.parser=parser;
    if(el) el.__smdParser=parser;
  }
  function _smdClearParserIdentity(el, parser){
    if(!el || (parser && el.__smdParser!==parser)) return;
    try{delete el.__smdParser;}catch(_){el.__smdParser=null;}
  }
  function _smdMediaTailClear(parser){
    if(_SMD_MEDIA_TAIL && parser) _SMD_MEDIA_TAIL.delete(parser);
    // Also clear the fallback key if it was ever set
    if(_SMD_MEDIA_TAIL && parser === __SMD_PARSER_FALLBACK) _SMD_MEDIA_TAIL.delete(parser);
  }
  function _safeSmdRenderer(el){
    const renderer=window.smd.default_renderer(el);
    const baseSetAttr=renderer.set_attr;
    const baseAddText=renderer.add_text;
    const writePlainText=(writeParent, writeData, writeText)=>{
      _smdAppendPlainText(writeParent, writeData, writeText, baseAddText);
    };
    const parserFor = (data)=>{
      return _smdParserKey(data, el);
    };
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      _smdMediaAwareAddText(baseAddText, parent, data, text, _SMD_MEDIA_TAIL, parserFor(data), writePlainText);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      const allowed=isSrc?_smdImgSrcAllowed(value):_SMD_SAFE_URL_RE.test(String(value||''));
      if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
        baseSetAttr(data,attr,_smdLinkHref(value));
        if(/^session:\/\//i.test(String(value||''))){
          const node=data&&data.nodes&&data.nodes[data.index];
          if(node&&node.classList) node.classList.add('session-link');
        }
        return;
      }
      if((isHref||isSrc)&&!allowed){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  function _streamFadeWordCountOf(text){
    const m=String(text||'').match(/\S+/g);
    return m?m.length:0;
  }
  function _streamFadeAppendText(el, text){
    if(!el) return;
    const value=String(text||'');
    if(!value) return;
    const reduceMotion=_streamFadeReduceMotionEnabled();
    const frag=document.createDocumentFragment();
    const wordRe=/(\S+)(\s*)/g;
    const appendStartedAt=performance.now();
    let last=0, match, changed=false;
    while((match=wordRe.exec(value))){
      if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
      if(reduceMotion){
        frag.appendChild(document.createTextNode(match[1]));
      }else{
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+fadeMs);
      }
      if(match[2]) frag.appendChild(document.createTextNode(match[2]));
      last=match.index+match[0].length;
      changed=true;
    }
    if(!changed){
      frag.appendChild(document.createTextNode(value));
    }else if(last<value.length){
      frag.appendChild(document.createTextNode(value.slice(last)));
    }
    el.appendChild(frag);
  }
  function _streamFadePauseAfter(text, paragraphBreakIndex){
    if(paragraphBreakIndex>=0) return 90;
    const trimmed=String(text||'').trimEnd();
    if(/[.!?]["\x27)\]]*$/.test(trimmed)) return 45;
    if(/[:;]["\x27)\]]*$/.test(trimmed)) return 30;
    return 0;
  }
  function _streamFadeNextText(targetText){
    targetText=String(targetText||'');
    const now=performance.now();
    if(!targetText){
      const hadVisible=!!_streamFadeVisibleText;
      _resetStreamFadeState();
      return {text:'', caughtUp:true, changed:hadVisible};
    }
    if(!_streamFadeVisibleText||!targetText.startsWith(_streamFadeVisibleText)){
      // Markdown/tool stripping can rewrite the visible prefix. Reset safely rather than
      // trying to animate across incompatible strings or stale word birth timestamps.
      _resetStreamFadeState();
    }
    if(!_streamFadeLastTickMs){
      _streamFadeLastTickMs=now;
      _streamFadeStartedAt=now;
    }
    if(_streamFadeVisibleText===targetText) return {text:_streamFadeVisibleText,caughtUp:true,changed:false};

    const remaining=targetText.slice(_streamFadeVisibleText.length);
    const backlogWords=_streamFadeWordCountOf(remaining);
    const targetWords=_streamFadeVisibleWords+backlogWords;
    const elapsedMs=Math.max(16,Math.min(120,now-_streamFadeLastTickMs));
    _streamFadeLastTickMs=now;

    // OpenWebUI fades the actual arriving tokens, so long/fast responses naturally
    // appear to accelerate. Hermes has a playout buffer, so track incoming word
    // velocity and play out faster than it instead of using a metronomic cadence.
    // LLM telemetry is usually tokens/sec, but the UI reveals words. A fixed word
    // cadence can look stuck even when token throughput is high, so combine:
    //   1) live target-word arrival velocity, 2) backlog pressure, 3) time ramp.
    if(!_streamFadeLastArrivalMs){
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords>_streamFadeLastTargetWords){
      const arrivalElapsedMs=Math.max(16, now-_streamFadeLastArrivalMs);
      const instantArrivalWps=(targetWords-_streamFadeLastTargetWords)*1000/arrivalElapsedMs;
      // EWMA smooths bursty token chunks without hiding sustained fast output.
      _streamFadeArrivalWps=_streamFadeArrivalWps
        ? (_streamFadeArrivalWps*0.65 + instantArrivalWps*0.35)
        : instantArrivalWps;
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords<_streamFadeLastTargetWords){
      _streamFadeLastTargetWords=targetWords;
      _streamFadeLastArrivalMs=now;
      _streamFadeArrivalWps=0;
    }

    if(now<_streamFadeHoldUntilMs){
      return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    }

    const streamAgeSeconds=Math.max(0, (now-(_streamFadeStartedAt||now))/1000);
    const baseWps=22 + Math.min(streamAgeSeconds*2.5, 28); // 22 → 50 wps over long answers
    const arrivalWps=_streamFadeArrivalWps ? Math.min(_streamFadeArrivalWps*1.05 + 8, 160) : 0;
    const backlogWps=backlogWords>0 ? Math.min(22 + backlogWords*1.1, 160) : 0;
    const wordsPerSecond=Math.min(160, Math.max(baseWps, arrivalWps, backlogWps));
    const speedFadeRatio=Math.max(0,Math.min(1,(wordsPerSecond-50)/(160-50)));
    _streamFadeCurrentMs=Math.round(_STREAM_FADE_MS+(_STREAM_FADE_MAX_MS-_STREAM_FADE_MS)*speedFadeRatio);

    _streamFadeWordCarry+=elapsedMs*wordsPerSecond/1000;
    if(!_streamFadeVisibleText) _streamFadeWordCarry=Math.max(_streamFadeWordCarry,1);
    let wordsToReveal=Math.floor(_streamFadeWordCarry);
    // At very high throughput, cap each frame to a small readable wave. Sustained
    // playback still catches up, but whole paragraphs no longer pop in at once.
    const waveCap=backlogWords>=160?3:2;
    wordsToReveal=Math.min(wordsToReveal,waveCap,backlogWords);
    if(wordsToReveal<1) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    _streamFadeWordCarry=Math.max(0,_streamFadeWordCarry-wordsToReveal);

    let cut=0;
    const wordRe=/(\s*\S+\s*)/g;
    let match;
    while(wordsToReveal>0&&(match=wordRe.exec(remaining))){
      cut=wordRe.lastIndex;
      wordsToReveal-=1;
    }
    if(cut<=0) cut=Math.min(remaining.length,4);
    const chunk=remaining.slice(0,cut);
    const paragraphMatch=chunk.match(/\n\s*\n/);
    const paragraphBreak=paragraphMatch ? paragraphMatch.index : -1;
    if(paragraphMatch) cut=paragraphBreak+paragraphMatch[0].length;
    const revealed=remaining.slice(0,cut);
    _streamFadeVisibleText+=revealed;
    _streamFadeVisibleWords+=_streamFadeWordCountOf(revealed);
    const pauseMs=_streamFadePauseAfter(revealed,paragraphBreak);
    if(pauseMs) _streamFadeHoldUntilMs=now+pauseMs;
    if(_streamFadeVisibleText.length>targetText.length) _streamFadeVisibleText=targetText;
    return {text:_streamFadeVisibleText,caughtUp:_streamFadeVisibleText===targetText,changed:true};
  }
  function _renderStreamingFadeMarkdown(displayText){
    if(!assistantBody) return true;
    const next=_streamFadeNextText(displayText);
    if(!next.changed) return next.caughtUp;
    assistantBody.classList.add('stream-fade-active');
    if(!_shouldUseTransparentStreamFade()){
      if(!_smdParser&&window.smd){
        if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
        _smdNewParser(assistantBody,true);
      }
      if(_smdParser){
        _smdWrite(next.text,true);
      }else{
        assistantBody.innerHTML=renderMd ? renderMd(next.text||'') : esc(next.text||'');
        _sanitizeSmdLinks(assistantBody);
      }
      _streamFadeDomText=String(next.text||'');
      return next.caughtUp;
    }
    if(_smdParser){
      _smdEndParser();
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    _smdReconnect=false;
    if(!_streamFadeDomText&&assistantBody.textContent){
      assistantBody.textContent='';
    }
    if(!String(next.text||'').startsWith(_streamFadeDomText)){
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    const delta=String(next.text||'').slice(_streamFadeDomText.length);
    if(delta) assistantBody.appendChild(document.createTextNode(delta));
    _streamFadeDomText=String(next.text||'');
    return next.caughtUp;
  }
  function _streamFadeCurrentDisplayText(){
    const parsed=_parseStreamState();
    return segmentStart===0
      ? parsed.displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
  }
  function _drainStreamFadeBeforeDone(onDone){
    const drainStartedAt=performance.now();
    let forcedDone=false;
    const step=()=>{
      if(!assistantBody){onDone();return;}
      const target=_streamFadeCurrentDisplayText();
      const caughtUp=_renderStreamingFadeMarkdown(target);
      const anchorProcessText=_streamFadeDomText||target;
      if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText);
      scrollIfPinned();
      if(caughtUp){
        // parser_end can flush pending markdown text; include that final text in
        // the fade wait instead of replacing it immediately in renderMessages().
        if(_smdParser) _smdEndParser();
        // Let the last released words visibly finish their stagger + fade before
        // the final renderMessages() DOM replacement removes the live spans.
        const remainingAnimationMs=Math.max(_STREAM_FADE_MS, _streamFadeLatestAnimationEndAt-performance.now());
        setTimeout(onDone, Math.min(remainingAnimationMs, _STREAM_FADE_DONE_MAX_MS));
        return;
      }
      // Final SSE `done` means the canonical completed session is available.
      // The optional word-fade playout must not keep that completed answer
      // hidden behind the live Thinking state for large/bursty responses.
      if(!forcedDone&&performance.now()-drainStartedAt>=_STREAM_FADE_DONE_DRAIN_MAX_MS){
        forcedDone=true;
        if(_smdParser) _smdEndParser();
        onDone();
        return;
      }
      setTimeout(()=>requestAnimationFrame(step), 33);
    };
    step();
  }
  function _flushPendingSegmentRender(options={}){
    const force=!!(options&&options.force);
    const skipAnchorProcessProse=!!(options&&options.skipAnchorProcessProse);
    if(!assistantBody||(!force&&!_renderPending)) return;
    if(_renderPending) _cancelAnimationFramePendingStreamRender();
    const displayText=segmentStart===0
      ? _parseStreamState().displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
    if(_smdParser){
      _smdWrite(displayText);
    } else if(window.smd){
      // Parser was nulled out (e.g. by a prior segment end) but smd is
      // available — recreate it on the existing element. Uses the non-fade
      // renderer to match standard rendering, avoiding O(n²) innerHTML
      // churn on long responses (#4704). Clear any content the renderMd()
      // fallback already wrote first: _smdNewParser resets _smdWrittenText to
      // '' but does NOT clear the element, so a following _smdWrite(displayText)
      // would append the full accumulated segment ON TOP of the existing
      // fallback render and duplicate the live text.
      assistantBody.innerHTML='';
      _smdNewParser(assistantBody, false);
      if(_smdParser) _smdWrite(displayText);
    } else if(renderMd){
      assistantBody.innerHTML=renderMd(displayText);
    } else {
      assistantBody.innerHTML=esc(displayText);
    }
    if(!skipAnchorProcessProse) _upsertAnchorProcessProse(displayText,{sealed:force});
    if(typeof _syncLiveWorklogReasonsForAnchor==='function') _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
  }
  function _resetAssistantSegment(){
    assistantRow=null;
    assistantBody=null;
    segmentStart=assistantText.length;
    _freshSegment=true;
    _smdEndParser();
    _resetStreamFadeState();
  }
  function _rememberRunJournalCursor(e){
    const raw=String(e&&e.lastEventId||'').trim();
    if(!raw) return;
    const tail=raw.includes(':')?raw.slice(raw.lastIndexOf(':')+1):raw;
    const seq=Number.parseInt(tail,10);
    if(Number.isFinite(seq)&&seq>_lastRunJournalSeq){
      _lastRunJournalSeq=seq;
      _lastRunJournalEventId=raw;
      // Mirror the advanced cursor onto the persisted INFLIGHT entry. persistInflightState()
      // saves `inflight.lastRunJournalSeq`, and a hard reload / reattach reads it back as the
      // `after_seq` replay floor (see attachLiveStream reconnecting init). Without this write
      // the persisted seq stayed 0, so a reload restored `lastAssistantText` and then replayed
      // the run journal from the zero floor (after_seq of 0) ON TOP of it — duplicating
      // already-rendered live reply content. Throttled persist keeps this off the hot token path. (#3401 reconnect dup)
      const inflight=INFLIGHT[activeSid];
      if(inflight){
        inflight.lastRunJournalSeq=seq;
        inflight.lastRunJournalEventId=raw;
        if(typeof _throttledPersist==='function') _throttledPersist();
      }
    }
  }
  function _runJournalReplayAfterSeq(){
    return Math.max(0,_lastRunJournalSeq||0);
  }
  function _runJournalReplayParams(){
    // `replay=1` documents frontend intent. The server selects replay when the
    // stream id no longer has a live worker; `after_seq` prevents duplicated
    // journal events after this EventSource has already rendered part of the
    // same run. `after_event_id` keeps that cursor run-aware so a stale cursor
    // from an earlier interrupted stream cannot suppress a newer stream whose
    // sequence numbers started over from 1.
    return `&replay=1&after_seq=${encodeURIComponent(String(_runJournalReplayAfterSeq()))}&after_event_id=${encodeURIComponent(_lastRunJournalEventId||'')}`;
  }

  function _stableStringify(value){
    const normalize=(v)=>{
      if(v===null||typeof v!=='object') return v;
      if(Array.isArray(v)) return v.map(normalize);
      const obj={};
      const keys=Object.keys(v).sort();
      for(const key of keys){
        obj[key]=normalize(v[key]);
      }
      return obj;
    };
    try{
      return JSON.stringify(normalize(value));
    }catch(_){
      return String(value||'');
    }
  }

  function _hashString(value){
    let hash=2166136261;
    for(let i=0;i<String(value||'').length;i++){
      hash^=String(value||'').charCodeAt(i);
      hash=Math.imul(hash,16777619);
    }
    return (hash>>>0).toString(16);
  }

  function _toolCallSignature(d, activityBurstId, activitySegmentSeq){
    const name=String(d&&d.name||'').trim().toLowerCase();
    const bid=Number(activityBurstId);
    const seq=Number(activitySegmentSeq);
    const args=d&&d.args;
    return `${name}|${Number.isFinite(bid)?bid:0}|${Number.isFinite(seq)?seq:0}|${_stableStringify(args)}`;
  }

  function _liveToolTid(d, activityBurstId, activitySegmentSeq){
    const explicit=String(d&&(d.tid||d.id||d.tool_call_id||d.tool_use_id||d.call_id)||'').trim();
    if(explicit) return explicit;
    return `live-${activeSid}-${_hashString(_toolCallSignature(d,activityBurstId,activitySegmentSeq))}`;
  }

  function _coerceLiveToolCallSignature(tc, activityBurstId, activitySegmentSeq){
    if(tc&&typeof tc==='object' && !tc._liveToolCallSignature){
      tc._liveToolCallSignature=_toolCallSignature(tc,activityBurstId,activitySegmentSeq);
    }
    return tc&&tc._liveToolCallSignature||'';
  }

  function _findPendingLiveToolCallIndex(toolCalls, opts){
    if(!Array.isArray(toolCalls)) return -1;
    const wantedTid=opts&&opts.tid||'';
    const wantedName=String(opts&&opts.name||'');
    const wantedSig=opts&&opts.signature||'';
    const wantedBurst=Number(opts&&opts.activityBurstId);
    const wantedSeq=Number(opts&&opts.activitySegmentSeq);
    const allowDone=!!(opts&&opts.allowDone);
    const matchName=(candidate)=>{
      return !candidate||!candidate.name||!wantedName ? false : String(candidate.name)===wantedName;
    };
    if(wantedTid){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const candidateTid=String(candidate.tid||candidate.id||candidate.tool_call_id||candidate.tool_use_id||candidate.call_id||'');
        if(candidateTid&&candidateTid===wantedTid) return i;
      }
    }
    if(wantedSig){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const canonicalSig=_coerceLiveToolCallSignature(
          candidate,
          Number.isFinite(wantedBurst)?wantedBurst:activityBurstFallbackFromCandidate(candidate),
          Number.isFinite(wantedSeq)?wantedSeq:activitySegmentSeqFallbackFromCandidate(candidate),
        );
        if(canonicalSig&&canonicalSig===wantedSig) return i;
      }
    }
    for(let i=toolCalls.length-1;i>=0;i--){
      const candidate=toolCalls[i];
      if(!candidate||typeof candidate!=='object') continue;
      if(!allowDone&&candidate.done===true) continue;
      if(!matchName(candidate)) continue;
      const candidateSeq=Number(candidate.activitySegmentSeq);
      const candidateBid=Number(candidate.activityBurstId);
      if(Number.isFinite(wantedSeq)&&Number.isFinite(candidateSeq)&&candidateSeq!==wantedSeq) continue;
      if(Number.isFinite(wantedBurst)&&Number.isFinite(candidateBid)&&candidateBid!==wantedBurst) continue;
      return i;
    }
    return -1;
  }

  function activityBurstFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activityBurstId);
  }
  function activitySegmentSeqFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activitySegmentSeq);
  }

  function _coerceLiveToolCallSeq(candidate){
    const raw=Number.isFinite(candidate)?candidate:Number(candidate&&candidate.activitySegmentSeq);
    return Number.isFinite(raw)&&raw>0?raw:undefined;
  }

  function _currentLiveToolAnchor(){
    const segmentSeq=Number(
      assistantRow&&assistantRow.getAttribute('data-live-segment-seq')||
      _assistantSegmentSeq||
      _currentLiveSegmentSeq||
      0
    );
    const burst=Number(_currentActivityBurstId);
    return {
      segmentSeq:Number.isFinite(segmentSeq)&&segmentSeq>0?segmentSeq:undefined,
      burstId:Number.isFinite(burst)?burst:0,
    };
  }

  function upsertLiveToolCall(d, phase){
    if(!d||d.name==='clarify') return null;
    const name=String(d&&d.name||'').trim();
    if(!name) return null;
    const current=_currentLiveToolAnchor();
    const inflight=INFLIGHT[activeSid] || (INFLIGHT[activeSid]={
      messages:[...S.messages],
      uploaded:[...uploaded],
      toolCalls:[],
    });
    if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
    if(!Array.isArray(inflight.messages)) inflight.messages=[...(inflight.messages||[])];

    const explicitTid=String(d&&d.tid||d&&d.id||d&&d.tool_call_id||d&&d.tool_use_id||d&&d.call_id||'').trim();
    const isComplete=phase==='complete';
    let signature=_toolCallSignature(d,current.burstId,current.segmentSeq);
    let index=-1;

    if(explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        tid:explicitTid,
        allowDone:isComplete,
      });
    }
    if(index<0){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        signature,
        name,
        activityBurstId:current.burstId,
        activitySegmentSeq:current.segmentSeq,
        allowDone:isComplete,
      });
    }
    if(index<0 && isComplete && !explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        name,
        activityBurstId:current.burstId,
        allowDone:true,
      });
    }

    let tc=null;
    if(index>=0&&inflight.toolCalls[index]){
      tc=inflight.toolCalls[index];
    }

    if(!tc){
      tc={
        name,
        preview:String(d.preview||''),
        args:d.args||{},
        snippet:'',
        done:isComplete,
        tid:explicitTid||_liveToolTid(d,current.burstId,current.segmentSeq),
        activityBurstId:current.burstId,
        activitySegmentSeq:_coerceLiveToolCallSeq(current.segmentSeq),
      };
      if(!isComplete){
        tc.started_at=Date.now()/1000;
      }
      if(isComplete) tc._createdByComplete=true;
      inflight.toolCalls.push(tc);
      if(!signature){
        signature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    } else {
      if(!tc.name) tc.name=name;
      if(!tc._liveToolCallSignature){
        tc._liveToolCallSignature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    }

    if(isComplete){
      if(d.preview){
        tc.snippet=tc.snippet||String(d.preview||'');
        if(!tc.preview) tc.preview=String(d.preview||'');
      }
    } else {
      tc.preview=String(d.preview||tc.preview||'');
    }
    if(d.args!==undefined) tc.args=d.args;
    if(d.snippet!==undefined) tc.snippet=d.snippet;
    tc._liveToolCallSignature = _toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
    tc.activityBurstId = Number.isFinite(Number(tc.activityBurstId))
      ? Number(tc.activityBurstId)
      : current.burstId;

    const currentSegmentSeq=_coerceLiveToolCallSeq(current.segmentSeq);
    const startSeq=_coerceLiveToolCallSeq(tc._toolCallStartSeq);
    const inferredSeq=_coerceLiveToolCallSeq(tc.activitySegmentSeq);
    if(!isComplete){
      if(inferredSeq===undefined && currentSegmentSeq!==undefined){
        tc.activitySegmentSeq=currentSegmentSeq;
      } else if(inferredSeq!==undefined){
        tc.activitySegmentSeq=inferredSeq;
      }
      tc._toolCallStartSeq=tc.activitySegmentSeq;
    } else if(startSeq!==undefined){
      tc.activitySegmentSeq=startSeq;
    } else if(inferredSeq!==undefined){
      tc.activitySegmentSeq=inferredSeq;
    }

    if(isComplete){
      tc.done=true;
      if(typeof d.is_error==='boolean') tc.is_error=d.is_error;
      if(d.duration!==undefined) tc.duration=d.duration;
      if(tc.started_at===undefined||tc.started_at===null) tc.started_at=Date.now()/1000;
      if(!tc.tid) tc.tid=explicitTid||_liveToolTid(d,tc.activityBurstId,tc.activitySegmentSeq);
    } else {
      tc.done=false;
      tc.started_at=tc.started_at||Date.now()/1000;
    }

    S.toolCalls=inflight.toolCalls;
    persistInflightState();
    return tc;
  }

  let _lastRenderMs=0;
  // Parse-result cache: _scheduleRender can accept a pre-computed _parseStreamState()
  // from the token event handler, avoiding a duplicate O(n) scan inside _doRender
  // when the rAF fires before the next token arrives.
  let _cachedParsed=null;
  let _cachedParsedText='';
  let _cachedParsedReasoning='';
  function _scheduleRender(parsed){
    // If caller provides a pre-computed parse result, cache it for _doRender.
    if(parsed){
      _cachedParsed=parsed;
      _cachedParsedText=assistantText;
      _cachedParsedReasoning=liveReasoningText;
    }
    if(_renderPending) return;
    if(_streamFinalized) return; // Bug A: don't schedule new rAF after stream finalized
    _renderPending=true;
    // Cap render rate to ~15fps. The browser's rAF fires at 60fps, but each DOM
    // update takes 50-150ms on large sessions. During GC pauses, rAF callbacks
    // accumulate and then execute all at once, blocking the main thread for
    // multi-second stretches and crashing the renderer (Chrome error code 4/5).
    // Throttling to 66ms intervals prevents this pileup without noticeable
    // visual degradation — streaming text updates still feel immediate.
    // performance.now() is monotonic so tab suspend/resume and NTP adjustments
    // cannot produce negative or enormous deltas.
    const sinceLastMs=performance.now()-_lastRenderMs;
    const _doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      // Guard: a pending setTimeout+rAF can outlive stream finalization.
      if(_streamFinalized) return;
      // Mobile scroll-jank guard: temporarily disable overflow-anchor before DOM
      // writes to suppress Chromium scroll re-anchoring during streaming growth.
      if(typeof window._fixMobileScrollJank==='function') window._fixMobileScrollJank();
      _lastRenderMs=performance.now();
      const parsed=_cachedParsed&&_cachedParsedText===assistantText&&_cachedParsedReasoning===liveReasoningText ? _cachedParsed : _parseStreamState();
      _cachedParsed=null;
      _renderLiveThinking(parsed);
      const displayText = segmentStart===0
        ? parsed.displayText                          // first segment: uses think-tag stripping
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      let anchorProcessText=displayText;
      if(assistantBody){
        if(_shouldUseLiveProseFade()){
          const caughtUp=_renderStreamingFadeMarkdown(displayText);
          anchorProcessText=_streamFadeDomText||'';
          if(!caughtUp&&!_streamFinalized){
            setTimeout(()=>_scheduleRender(), 33);
          }
        } else {
          assistantBody.classList.remove('stream-fade-active');
          _resetStreamFadeState();
          if(!_smdParser&&window.smd){
            // On reconnect: prior content in assistantBody came from a different smd parser run.
            // Clear it and start fresh — renderMessages() on done will restore the full content.
            if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
            _smdNewParser(assistantBody);
          }
        if(_smdParser){
          _smdWrite(displayText);
        } else {
            // Fallback: smd not loaded yet, reconnect session, or smd unavailable — use renderMd
            // for every live segment. Without this, the first segment inserts raw
            // parsed.displayText and users see unformatted markdown until done.
            const fallbackText = segmentStart===0
              ? parsed.displayText
              : _stripXmlToolCalls(assistantText.slice(segmentStart));
            assistantBody.innerHTML = renderMd ? renderMd(fallbackText) : esc(fallbackText);
          }
        }
        if(typeof _syncLiveWorklogReasonsForAnchor==='function') _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
      }
      if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText);
      scrollIfPinned();
      _throttledSnapshotLiveTurn();
    };
    const frameIntervalMs=_shouldUseLiveProseFade()?33:66;
    if(sinceLastMs>=frameIntervalMs){
      _pendingRafHandle=requestAnimationFrame(_doRender);
    } else {
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(_doRender), frameIntervalMs-sinceLastMs);
    }
  }

  function _completeAutomaticCompressionOnLiveProgress(sessionId){
    const sid=String(sessionId||'');
    const hasRunningLiveCard=!!document.querySelector('[data-live-compression-card="1"][data-compression-started-at]');
    const hasRunningState=!!(window._compressionUi&&window._compressionUi.automatic&&window._compressionUi.phase==='running'&&(!sid||!window._compressionUi.sessionId||String(window._compressionUi.sessionId)===sid));
    if(!hasRunningLiveCard&&!hasRunningState) return false;
    _ensureAnchorCompressionCompletedOnLiveProgress(sid);
    if(typeof appendLiveCompressionCard==='function'){
      appendLiveCompressionCard({
        sessionId:sid,
        phase:'done',
        automatic:true,
        message:'Context auto-compressed',
      });
    }
    return true;
  }

  function _wireSSE(source){
    const existingLive=LIVE_STREAMS[activeSid];
    if(existingLive&&existingLive.source&&existingLive.source!==source){
      try{if(existingLive.source.readyState!==2)existingLive.source.close();}catch(_){ }
    }
    LIVE_STREAMS[activeSid]={streamId,source};

    // Note on #631 Bug B: the original PR description stated the server
    // "replays buffered token events" on reconnect, and proposed resetting
    // the accumulators here so the re-sent tokens wouldn't double the prefix.
    // That is NOT how the server actually works — api/routes._handle_sse_stream
    // reads a one-shot queue.Queue() that delivers each event to exactly one
    // consumer; a reconnect picks up from the current queue position and gets
    // only events produced during the outage.  Resetting the accumulators here
    // would wipe the already-displayed content and restart the response from
    // the first post-reconnect token — a real data-loss regression.
    //
    // The "doubled response" / "stuck cursor" symptom is fully explained by
    // Bug A (trailing rAF after `done` inserting a new live-turn wrapper) —
    // the fixes below (_streamFinalized guard + cancelAnimationFrame in the
    // terminal handlers) address it without needing a reset here.

    source.addEventListener('token',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      assistantText+=d.text;
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid) return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      if(_freshSegment) appendThinking('', _liveThinkingPlacement());
      // Once the assistant row exists its creation gate is already satisfied, and
      // the throttled _doRender re-parses once per frame anyway — so the per-token
      // full-text parse here is pure waste (O(n)/token -> O(n^2) over the answer).
      // Still call ensureAssistantRow() every token exactly as before (cheap; it
      // also starts a new segment on a post-tool _freshSegment). Only the parse is
      // skipped, and only once the row exists. (#5455 WS2.3)
      if(assistantRow){
        ensureAssistantRow();
        _scheduleRender();
      }else{
        const parsed=_parseStreamState();
        if(String((parsed&&parsed.displayText)||'').trim()) ensureAssistantRow();
        _scheduleRender(parsed);
      }
    });

    source.addEventListener('interim_assistant',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      const d=JSON.parse(e.data);
      const visible=String(d&&d.text?d.text:'').trim();
      const alreadyStreamed=!!(d&&d.already_streamed);
      const reasoningEcho=!!(d&&d.reasoning_echo);
      if(!visible){
        return;
      }
      if(reasoningEcho) _stripLiveReasoningEcho(visible);
      liveReasoningText='';
      if(alreadyStreamed){
        if(!S.session||S.session.session_id!==activeSid){
          recordActivityBoundary();
          _resetAssistantSegment();
          return;
        }
        _completeAutomaticCompressionOnLiveProgress(activeSid);
        const parsed=_parseStreamState();
        if(String((parsed&&parsed.displayText)||'').trim()||assistantRow){
          ensureAssistantRow(true);
          _flushPendingSegmentRender({force:true});
          if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
          if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
          recordActivityBoundary();
        }
        _resetAssistantSegment();
        return;
      }
      assistantText += assistantText ? `\n\n${visible}` : visible;
      visibleInterimSnippets.push(visible);
      syncInflightAssistantMessage();
      if(!S.session||S.session.session_id!==activeSid){
        recordActivityBoundary();
        _resetAssistantSegment();
        return;
      }
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      ensureAssistantRow(true);
      if(assistantRow) assistantRow.setAttribute('data-interim','1');
      _flushPendingSegmentRender({force:true,skipAnchorProcessProse:true});
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
      _applyToAnchor('interim_assistant',d,e);
      // Collapse old interim notes once more than INTERIM_COLLAPSE_THRESHOLD accumulate.
      const INTERIM_COLLAPSE_THRESHOLD=3;
      if(visibleInterimSnippets.length>INTERIM_COLLAPSE_THRESHOLD&&assistantRow){
        const blocks=assistantRow.parentElement;
        if(blocks){
          const anchorSceneOwnsLive=!!(blocks.closest&&blocks.closest('[data-anchor-scene-live-owner="1"]'));
          if(anchorSceneOwnsLive){
            blocks.querySelectorAll('.interim-collapse-toggle').forEach(el=>el.remove());
          }else{
            const allInterim=Array.from(blocks.querySelectorAll('[data-interim="1"]'));
            const toHide=allInterim.slice(0,allInterim.length-INTERIM_COLLAPSE_THRESHOLD);
            let toggle=blocks.querySelector('.interim-collapse-toggle');
            if(!toggle){
              toggle=document.createElement('span');
              toggle.className='interim-collapse-toggle';
              // No per-element listener: clicks are handled by a delegated
              // document-level handler (see _interimCollapseDelegatedClick) so
              // the toggle keeps working after a live-turn DOM restore
              // (snapshotLiveTurnHtmlForSession/restoreLiveTurnHtmlForSession
              // rebuild via innerHTML, which would drop a direct listener and
              // leave the collapsed notes permanently unreachable). The
              // threshold rides on the markup so the handler stays stateless.
              toggle.dataset.threshold=String(INTERIM_COLLAPSE_THRESHOLD);
              if(toHide.length) toHide[0].before(toggle);
            }
            // Skip re-collapse when the user expanded manually; always update the stored count.
            if(!toggle.dataset.expanded){
              toHide.forEach(el=>el.classList.add('interim-collapsed'));
            }
            const stillHidden=blocks.querySelectorAll('[data-interim="1"].interim-collapsed').length;
            if(stillHidden) toggle.textContent='Show '+stillHidden+' earlier update'+(stillHidden===1?'':'s');
          }
        }
      }
      recordActivityBoundary();
      _resetAssistantSegment();
      _scheduleRender();
    });

    source.addEventListener('reasoning',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!_ownsActiveStreamOrBackground()) return;
      const d=JSON.parse(e.data);
      const text=d.text||'';
      reasoningText += text;
      liveReasoningText += text;
      if(d.text&&S.session&&S.session.session_id===activeSid) _completeAutomaticCompressionOnLiveProgress(activeSid);
      syncInflightAssistantMessage();
      if(text&&S.session&&S.session.session_id===activeSid&&S.activeStreamId===streamId){
        const liveThinkingText=_liveThinkingText();
        const anchorReasoningFallback={};
        if(!_upsertAnchorReasoning(liveThinkingText, anchorReasoningFallback)){
          _updateLiveThinkingCard(liveThinkingText,{
            ...anchorReasoningFallback,
            anchorRenderFallback:true,
            sessionId:activeSid,
            streamId,
          });
        }
      }
    });

    source.addEventListener('tool',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid||S.activeStreamId!==streamId) return;
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      const tc=upsertLiveToolCall(d,'start');
      if(!tc) return;
      const pendingDisplayTextBeforeTool=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if(String(pendingDisplayTextBeforeTool||'').trim()) _upsertAnchorProcessProse(pendingDisplayTextBeforeTool,{sealed:true});
      _applyToAnchor('tool',{...d,...tc},e);

      if(S.session&&S.session.session_id===activeSid&&typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();
      if(!S.session||S.session.session_id!==activeSid) return;
      // Provider reasoning/thinking is a Worklog Thinking Card, separate from
      // tool cards. Close the current live card before appending a tool row.
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      liveReasoningText='';
      const oldRow=$('toolRunningRow');if(oldRow)oldRow.remove();
      const pendingDisplayText=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if((assistantRow&&assistantBody)||String(pendingDisplayText||'').trim()){
        ensureAssistantRow(true);
      }
      _flushPendingSegmentRender({force:true});
      appendLiveToolCard(tc,{sessionId:activeSid,streamId});
      snapshotLiveTurn();
      _freshSegment=true;
      _smdEndParser();
      _resetAssistantSegment();
      scrollIfPinned();
    });

    source.addEventListener('tool_complete',e=>{
      if(_terminalStateReached||_streamFinalized) return;
      if(!S.session||S.session.session_id!==activeSid||S.activeStreamId!==streamId) return;
      const d=JSON.parse(e.data);
      if(d.name==='clarify') return;
      _completeAutomaticCompressionOnLiveProgress(activeSid);
      const tc=upsertLiveToolCall(d,'complete');
      if(!tc) return;
      tc.is_error=!!d.is_error;
      const pendingDisplayTextBeforeComplete=segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      if(String(pendingDisplayTextBeforeComplete||'').trim()) _upsertAnchorProcessProse(pendingDisplayTextBeforeComplete,{sealed:true});
      _applyToAnchor('tool_complete',{...d,...tc,is_error:!!d.is_error},e);
      if(typeof noteWorkspaceMutationsFromToolCall==='function') noteWorkspaceMutationsFromToolCall(tc);
      if(S.session&&S.session.session_id===activeSid&&typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();
      if(!S.session||S.session.session_id!==activeSid) return;
      _maybeNotifyPersistentStateSaved(tc);
      if(typeof refreshOpenPreviewIfMutated==='function') refreshOpenPreviewIfMutated();
      if(tc._createdByComplete){
        const pendingDisplayText=segmentStart===0
          ? (_parseStreamState().displayText||'')
          : _stripXmlToolCalls(assistantText.slice(segmentStart));
        if((assistantRow&&assistantBody)||String(pendingDisplayText||'').trim()){
          ensureAssistantRow(true);
          _flushPendingSegmentRender({force:true});
        }
        appendLiveToolCard(tc,{sessionId:activeSid,streamId});
        _freshSegment=true;
        _smdEndParser();
        _resetAssistantSegment();
      } else {
        appendLiveToolCard(tc,{sessionId:activeSid,streamId});
      }
      snapshotLiveTurn();
      scrollIfPinned();
    });

    // Phase 2: dedicated `todo_state` event carries a full snapshot of
    // the upstream TodoStore.  We treat it as the single source of truth
    // for the Todos panel — never merge, always replace.  The handler
    // is intentionally cheap: parse, validate, write S.todos, mirror to
    // INFLIGHT, schedule a RAF render.  Out-of-order events are filtered
    // by ts; SSE journal replay is idempotent because snapshots are full.
    // Cross-session protection mirrors every other live listener:
    // payload.session_id must match activeSid or the event is dropped.
    source.addEventListener('todo_state',e=>{
      let d;
      try{ d=JSON.parse(e.data||'{}'); }catch(_){ return; }
      if(!d||typeof d!=='object') return;
      // Cross-session double check: payload.session_id is the SSE-side
      // filter (some legacy emissions omit it), and S.session.session_id
      // is the UI-side filter (a late event that arrives after the user
      // already navigated to another session must not pollute S.todos).
      // Both must agree with activeSid before we touch global state.
      if(d.session_id&&d.session_id!==activeSid) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      if(!Array.isArray(d.todos)) return;
      const incomingTs=Number(d.ts)||0;
      const currentTs=(S.todoStateMeta&&Number(S.todoStateMeta.ts))||0;
      // Strictly older snapshots are discarded; equal-ts events still
      // apply so a compression-source refresh can land on the same
      // second as the tool emit it follows.
      if(incomingTs&&currentTs&&incomingTs<currentTs) return;
      S.todos=d.todos;
      S.todoStateMeta={
        ts:incomingTs||(Date.now()/1000),
        source:String(d.source||'tool'),
        version:Number(d.version)||1,
      };
      const inflight=INFLIGHT[activeSid];
      if(inflight){
        inflight.todos=S.todos;
        inflight.todoStateMeta=S.todoStateMeta;
      }
      if(typeof persistInflightState==='function') persistInflightState();
      if(typeof scheduleTodosRefresh==='function') scheduleTodosRefresh();
    });

    source.addEventListener('approval',e=>{
      const d=JSON.parse(e.data);
      _applyToAnchor('approval',d,e);
      showApprovalForSession(activeSid, d, 1);
      playAttentionSound(_attentionSoundKey(activeSid,'approval',1));
      sendBrowserNotification('Approval required',d.description||'Tool approval needed',{sid:activeSid});
    });

    source.addEventListener('clarify',e=>{
      const d=JSON.parse(e.data);
      _applyToAnchor('clarify',d,e);
      showClarifyForSession(activeSid, d);
      playAttentionSound(_attentionSoundKey(activeSid,'clarify',1));
      sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed',{sid:activeSid});
    });

    source.addEventListener('state_saved',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      _showPersistentStateToast(d.kind, d.name||'', {created:String(d.action||'').toLowerCase()==='created'});
    });

    source.addEventListener('title',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      applySessionTitleUpdate(activeSid, d.title);
    });

    source.addEventListener('title_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      try{
        console.info('[title]', {
          status:String(d.status||''),
          reason:String(d.reason||''),
          title:String(d.title||''),
          raw_preview:String(d.raw_preview||''),
          session_id:String(d.session_id||activeSid)
        });
      }catch(_){}
    });

    source.addEventListener('context_status',e=>{
      let d={};
      try{ d=JSON.parse(e.data||'{}'); }catch(_){}
      if((d.session_id||activeSid)!==activeSid) return;
      const prefill=d.prefill||{};
      const status=String(prefill.status||'not_configured');
      const label=String(prefill.label||'session recall');
      if(status==='loaded'){
        setComposerStatus(`Context loaded: ${label}`);
      }else if(status==='error'){
        setComposerStatus(`Context unavailable: ${label}`);
        if(typeof showToast==='function') showToast(`Context unavailable: ${String(prefill.error||label)}`,3600,'warning');
      }
    });

    function _resolveGoalMessage(d){
      const key=String(d && d.message_key ? d.message_key : '').trim();
      const args=Array.isArray(d && d.message_args) ? d.message_args : [];
      const raw=String(d&&d.message||'').trim();
      if(key && typeof t==='function'){
        try{
          const translated=String(t(key,...args));
          if(translated && translated!==key)return translated;
        }catch(_){}
      }
      return raw;
    }

    source.addEventListener('goal',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        const goalState=String(d.state||'').trim();
        const goalEvaluatingMessage=t('goal_evaluating_progress');
        if(goalState==='evaluating'){
          setComposerStatus(goalEvaluatingMessage);
          return;
        }
        const msg=_resolveGoalMessage(d);
        if(!msg)return;
        _latestGoalStatus={message:msg,decision:d.decision||null,state:goalState||null};
        setComposerStatus(msg);
        showToast(msg.split('\n')[0],2600);
      }catch(_){}
    });

    source.addEventListener('goal_continue',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const continuation_prompt=String(d.continuation_prompt||d.text||'').trim();
        if(!continuation_prompt||sid!==activeSid)return;
        _applyToAnchor('goal_continue',d,e);
        const _modelState=_chatPayloadModelState();
        _pendingGoalContinuation={
          sid,
          text:continuation_prompt,
          model:_modelState.model,
          model_provider:_modelState.model_provider,
          profile:S.activeProfile||'default',
        };
        const toast=t('goal_continuing_toast');
        const cmsg=_resolveGoalMessage(d);
        showToast((toast&&cmsg&&cmsg!==toast)?cmsg.split('\n')[0]:toast,2200);
      }catch(_){}
    });

    // bg_task_complete: terminal(notify_on_complete=true) background process
    // exited. Option Z PIVOT: the agent wakeup is started SERVER-SIDE by the
    // drain thread (api/background_process._process_one →
    // routes.start_session_turn) with NO browser round-trip — so the
    // closed-tab case works (parity with CLI/Telegram). The browser does NOT
    // re-POST /api/chat/start anymore. This SSE event is pure LIVE-VIEW: if
    // a tab is open the server-initiated turn streams live via the normal
    // /api/chat/stream EventSource; if the tab is closed the turn still runs
    // server-side and persists to the session store.
    //
    // Idempotency: dedupe by (session_id, event_id) via a Map+TTL ring
    // buffer (`_bgTaskCompleteRingBufferAdd`).
    //
    // Option X: this handler is the in-turn (STREAMS-bound) path. The server
    // dual-emits to the persistent session-scoped channel too — the
    // `_handleBgTaskCompleteEvent` function below is shared between both
    // paths (dedupe only; the wakeup itself is server-side).
    source.addEventListener('bg_task_complete',e=>{
      if(typeof _handleBgTaskCompleteEvent==='function'){
        _handleBgTaskCompleteEvent(e, activeSid, {source:'stream'});
      }
    });

    source.addEventListener('done',e=>{
      if(_streamFinalized) return;
      _clearStreamEndRecovery();
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      // Set _streamFinalized IMMEDIATELY — before any fade delay. Without this,
      // a stream_end event arriving during the fade window sees
      // _streamFinalized=false, calls _restoreSettledSession(), and overwrites
      // S.messages with stale server data (issue #3195).
      _streamFinalized=true;
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _cancelThrottledSnapshotTimer();
      const _doneData=JSON.parse(e.data);
      const _doneEvent=e;
      const _finishDone=()=>{
        // Bug A fix: cancel any pending rAF and mark stream finalized before
        // the DOM is settled by renderMessages, so no trailing token/reasoning rAF
        // can reintroduce a stale thinking card or duplicate content.
        _streamFinalized=true;
        _cancelAnimationFramePendingStreamRender();
        _streamFadeCleanupReduceMotionListener();
        if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
        // Finalize smd parser — flushes any remaining buffered markdown state
        // and runs Prism + copy buttons on the live segment before the DOM is replaced
        if(assistantBody){
          const _finBody=assistantBody;
          _smdEndParser();
          requestAnimationFrame(()=>{
            if(typeof highlightCode==='function') highlightCode(_finBody);
            if(typeof addCopyButtons==='function') addCopyButtons(_finBody);
            if(typeof renderKatexBlocks==='function') renderKatexBlocks();
          });
        } else {
          _smdEndParser();
        }
        const d=_doneData;
        _flushReasoningToAnchor();
        _applyToAnchor('done',{
          status:d.status||'completed',
          usage:d.usage||null,
          created_at:d.created_at||null,
        },_doneEvent);
        _scheduleAnchorRegistryCleanup();
        _clearAnchorProseIncrementalNode();
        const isActiveSession=_isSessionCurrentPane(activeSid);
        const isSessionViewed=_isSessionActivelyViewed(activeSid);
        const completedSession=d.session||{session_id:activeSid};
        const completedSid=completedSession.session_id||activeSid;
        const completedMessageCount=completedSession.message_count != null
          ? completedSession.message_count
          : (
            Array.isArray(completedSession.messages)
              ? completedSession.messages.length
              : (
                (S.session&&((S.session.session_id||activeSid)===completedSid)&&S.session.message_count != null)
                  ? S.session.message_count
                  : ((Array.isArray(S.messages)&&S.messages.length)||0)
              )
          );
        if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
          _markSessionCompletionUnread(completedSid, completedMessageCount);
        }
        if(isSessionViewed) _markSessionViewed(completedSid, completedMessageCount);
        _clearOwnerInflightState();
        if(typeof _markSessionCompletedInList==='function'){
          _markSessionCompletedInList(completedSession, activeSid);
        }
        _clearApprovalForOwner();
        _clearClarifyForOwner('terminal');
        const shouldFollowOnDone=isActiveSession&&((typeof _shouldFollowMessagesOnDomReplace==='function')
          ? _shouldFollowMessagesOnDomReplace()
          : (typeof _isMessagePaneNearBottom==='function'&&_isMessagePaneNearBottom(1200)));
        const _settledStreamId=isActiveSession?(S.activeStreamId||(d&&d.stream_id)||''):'';
        if(isActiveSession){
          S.activeStreamId=null;
        }
        let lastAsst=null;
        if(isActiveSession){
          // Capture previous session totals BEFORE overwriting S.session with the new
          // cumulative values from the done event. prevIn/prevOut are the totals as of
          // the start of this turn; curIn/curOut are the full post-turn totals — the
          // delta is the per-turn usage for #1159.
          const _prevIn=(S.session&&S.session.input_tokens)||0;
          const _prevOut=(S.session&&S.session.output_tokens)||0;
          const _prevCost=(S.session&&S.session.estimated_cost)||0;
          const _prevCacheRead=(S.session&&S.session.cache_read_tokens)||0;
          const _prevCacheWrite=(S.session&&S.session.cache_write_tokens)||0;
          S.session=d.session;S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[]);if(typeof _messagesTruncated!=='undefined')_messagesTruncated=!!d.session._messages_truncated;
          // #4720: reset _oldestIdx (full-load symmetry; keeps the #4613 anchor aligned).
          if(typeof _oldestIdx!=='undefined')_oldestIdx=d.session._messages_offset||0;
          S.messages=_filterRecoveryControlMessages(S.messages || []);
          if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
          if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();
          if(S.session&&S.session.session_id){
            try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
            if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
          }
          const _markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages);
          if(
            window._compressionUi&&window._compressionUi.automatic&&
            window._compressionUi.sessionId===activeSid&&
            d.session&&d.session.session_id
          ){
            window._compressionUi={...window._compressionUi, sessionId:d.session.session_id};
          }
          // Find the last assistant message once for both reasoning persistence and timestamp
          lastAsst=[...S.messages].reverse().find(m=>m.role==='assistant');
          // Persist reasoning trace for Worklog Thinking Cards; normal transcript
          // rendering keeps provider reasoning out of the final answer.
          if(reasoningText&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=reasoningText;
          // Strip any inline <think> blocks still embedded in the server-side
          // content (M3 OpenAI-compat doesn't separate reasoning). Move them
          // to m.reasoning so the persisted session stays compact and the
          // thinking card has a proper source field on reload.
          if(lastAsst && typeof lastAsst.content === 'string' && lastAsst.content){
            const split=_splitThinkFromContent(lastAsst.content, lastAsst.reasoning);
            if(split.content!==lastAsst.content){
              lastAsst.content=split.content;
              if(split.reasoning) lastAsst.reasoning=split.reasoning;
            }
          }
          // Stamp _ts on the last assistant message if it has no timestamp
          if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp) lastAsst._ts=Date.now()/1000;
          if(d.usage){
            const _doneUsageFallback={...(S.lastUsage||{})};
            if(S.session){
              for(const _usageField of ['context_length','threshold_tokens','last_prompt_tokens','post_compression_context_tokens_estimate']){
                if(_doneUsageFallback[_usageField]==null&&S.session[_usageField]!=null){
                  _doneUsageFallback[_usageField]=S.session[_usageField];
                }
              }
            }
            S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
              ? _mergeUsageForCtxIndicator(d.usage,_doneUsageFallback)
              : {..._doneUsageFallback,...d.usage};
            _syncCtxIndicator(S.lastUsage);
            // #503 — compute per-turn cost delta and attach to last assistant message
            if(lastAsst){
              const prevIn=_prevIn;
              const prevOut=_prevOut;
              const prevCost=_prevCost;
              const curIn=d.usage.input_tokens||0;
              const curOut=d.usage.output_tokens||0;
              const curCost=d.usage.estimated_cost||0;
              const curCacheRead=d.usage.cache_read_tokens||0;
              const curCacheWrite=d.usage.cache_write_tokens||0;
              // Only set delta if values actually increased (skip no-op turns)
              if(curIn>prevIn||curOut>prevOut||curCacheRead>_prevCacheRead||curCacheWrite>_prevCacheWrite){
                lastAsst._turnUsage={
                  input_tokens:Math.max(0,curIn-prevIn),
                  output_tokens:Math.max(0,curOut-prevOut),
                  estimated_cost:Math.max(0,curCost-prevCost),
                  cache_read_tokens:Math.max(0,curCacheRead-_prevCacheRead),
                  cache_write_tokens:Math.max(0,curCacheWrite-_prevCacheWrite),
                  cache_hit_percent:d.usage.turn_cache_hit_percent,
                };
              }
              if(typeof d.usage.duration_seconds==='number'){
                lastAsst._turnDuration=d.usage.duration_seconds;
              }
              if(typeof d.usage.tps==='number'&&d.usage.tps>0){
                lastAsst._turnTps=d.usage.tps;
              }
              if(d.usage.gateway_routing){
                lastAsst._gatewayRouting=d.usage.gateway_routing;
                if(S.session)S.session.gateway_routing=d.usage.gateway_routing;
                if(S.session&&Array.isArray(S.session.gateway_routing_history))S.session.gateway_routing_history.push(d.usage.gateway_routing);
                else if(S.session)S.session.gateway_routing_history=[d.usage.gateway_routing];
              }
            }
          }
          _attachProjectedAnchorSceneToLastAssistant(S.messages);
          const hasMessageToolMetadata=S.messages.some(m=>{
            if(!m||m.role!=='assistant') return false;
            const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
            const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
            const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
            return hasTc||hasPartialTc||hasTu;
          });
          if(!hasMessageToolMetadata&&d.session.tool_calls&&d.session.tool_calls.length){
            S.toolCalls=d.session.tool_calls.map(tc=>tc);
            S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(d.session.tool_calls);
          } else {
            if(hasMessageToolMetadata) S._settledLiveToolMetadata=S.toolCalls.map(tc=>({...tc,done:true}));
            S.toolCalls=hasMessageToolMetadata?[]:S.toolCalls.map(tc=>({...tc,done:true}));
          }
          if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();
          if(uploaded.length){
            const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
            if(lastUser)lastUser.attachments=uploaded;
          }
          if(_latestGoalStatus&&_latestGoalStatus.message){
            S.messages.push({
              role:'assistant',
              content:String(_latestGoalStatus.message),
              _ts:Date.now()/1000,
              _goalStatus:true,
              _transient:true,
            });
          }
          clearLiveToolCards();
          S.busy=false;
          // No-reply guard (#373): if agent returned nothing, show inline error
          if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!assistantText){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
          if(_markerOnlyAssistantError&&typeof showToast==='function') showToast('No response received after context compression. Please retry.',5000,'error');
          if(isSessionViewed) _markSessionViewed(completedSid, completedMessageCount);
          // Cooldown: prevent refreshActiveSessionIfExternallyUpdated from
          // force-reloading immediately after "done" — the event already
          // delivered the final messages and tool calls.
          if(typeof window!=='undefined') window._streamJustFinished=true;
          setTimeout(()=>{ if(typeof window!=='undefined') window._streamJustFinished=false; }, 5000);
          // Expand render window to cover all messages so the done render
          // doesn't hide Activity behind a tiny window (winSize=50).
          if(typeof _messageRenderableMessageCount==='function'&&typeof _messageRenderWindowSize!=='undefined'){
            _messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize==='function'?_currentMessageRenderWindowSize():50, _messageRenderableMessageCount());
          }
          // #4650 review: the agent turn that just completed may have changed
          // server-side reasoning config (e.g. a `/reasoning <level>` slash
          // command writes agent.reasoning_effort) WITHOUT changing the model/
          // provider cache key. Invalidate the reasoning-chip cache once at the
          // turn boundary so the following syncTopbar() refetches the authoritative
          // effort exactly once (not per-token — the storm short-circuit is intact).
          if(typeof _lastReasoningFetchKey!=='undefined') _lastReasoningFetchKey=null;
          // Arm one-shot keep-open so the JUST-settled worklog stays open on the
          // settle render (height-stable swap, no shrink jump). Disarm, then run a
          // scroll-PRESERVING collapse pass for BOTH pin states so the worklog
          // returns to its copied live/user disclosure state (a pinned follower's
          // scrollToBottom() only settles scroll, it does NOT re-render, so without
          // this pass the forced-open DOM would persist for them). This unarmed
          // render also populates the cache with the correctly-collapsed DOM, and
          // the same-frame JS restore absorbs the collapse so there is no jump.
          // (#5260 gate-cert: keep-open must be transient + uncached for everyone.)
          if(typeof _armKeepSettledWorklogOpen==='function') _armKeepSettledWorklogOpen(_settledStreamId);
          syncTopbar();renderMessages({preserveScroll:true});
          if(typeof _disarmKeepSettledWorklogOpen==='function') _disarmKeepSettledWorklogOpen();
          if(typeof _renderMessagesWithScrollSnapshot==='function') _renderMessagesWithScrollSnapshot();
          else renderMessages({preserveScroll:true});
          if(shouldFollowOnDone&&typeof scrollToBottom==='function') scrollToBottom();
          if(typeof noteWorkspaceMutationsFromToolCalls==='function') noteWorkspaceMutationsFromToolCalls(S.toolCalls);
          loadDir('.', { preservePreview: true });
          // TTS auto-read: speak the last assistant response if enabled (#499)
          if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);
        }
        if(!lastAsst&&d.session&&Array.isArray(d.session.messages)){
          lastAsst=[...d.session.messages].reverse().find(m=>m&&m.role==='assistant')||null;
        }
        if(isActiveSession&&_pendingGoalContinuation&&typeof queueSessionMessage==='function'){
          const _goalNext=_pendingGoalContinuation;
          _pendingGoalContinuation=null;
          queueSessionMessage(_goalNext.sid,{
            text:_goalNext.text,
            files:[],
            model:_goalNext.model,
            model_provider:_goalNext.model_provider,
            profile:_goalNext.profile,
          });
          if(typeof updateQueueBadge==='function')updateQueueBadge(_goalNext.sid);
        }
        if(isActiveSession) _queueDrainSid=activeSid;
        renderSessionList();
        _setActivePaneIdleIfOwner();
        playNotificationSound();
        // #4416: notify if the tab was hidden at ANY point during this stream
        // (not just at done-receive time, which a throttled background-tab SSE
        // delivers late — after the user returns and document.hidden is false).
        // If the user watched the whole stream, _wasEverHidden stays false and
        // the notification is suppressed (matches Slack/Discord/Gmail/Claude).
        const _wasEverBackgrounded=_shouldForceCompletionNotification(activeSid, streamId);
        const _completionPreview=_completionNotificationPreviewText(lastAsst,{
          sessionId:completedSid,
          liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():assistantText,
        });
        sendBrowserNotification('Response complete',_completionPreview||'Task finished',{forceHidden:_wasEverBackgrounded,sid:activeSid});
      };
      if(_shouldUseLiveProseFade()&&assistantBody){
        _cancelAnimationFramePendingStreamRender();
        _drainStreamFadeBeforeDone(_finishDone);
        return;
      }
      _finishDone();
    });

    source.addEventListener('stream_end',async e=>{
      if(_streamFinalized){
        _closeSource(source);
        return;
      }
      _clearStreamEndRecovery();
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
      }catch(_){}
      if(S.activeStreamId===streamId && _liveStreamEndScenePresent()){
        _scheduleStreamEndRecovery(source);
        return;
      }
      // Some replay/journal paths can deliver stream_end without a preceding
      // done event. In that case closing the EventSource is not enough: the
      // live DOM/inflight state remains projected and can duplicate Thinking or
      // assistant content until a later session switch. Settle from the persisted
      // session before closing so the pane converges on canonical state.
      const status=await _restoreSettledSession(source,{status:true});
      if(status==='restored'){
        return;
      }
      if(status==='active'&&S.activeStreamId===streamId){
        _scheduleStreamEndRecovery(source,200);
        return;
      }
      _finalizeStreamEndFallback(source);
    });

    source.addEventListener('pending_steer_leftover',e=>{
      // The agent finished its turn with steer text still stashed (no
      // tool-result boundary fired). Match the CLI's leftover-delivery
      // behaviour: queue the leftover text as a next-turn user message
      // so the existing drain in setBusy(false) ships it.
      try{
        const d=JSON.parse(e.data||'{}');
        const sid=d.session_id||activeSid;
        const txt=String(d.text||'').trim();
        if(!txt||sid!==activeSid) return;
        _applyToAnchor('pending_steer_leftover',d,e);
        if(typeof queueSessionMessage==='function'){
          const _modelState=_chatPayloadModelState();
          queueSessionMessage(sid,{
            text:txt,files:[],
            model:_modelState.model,
            model_provider:_modelState.model_provider,
            profile:S.activeProfile||'default',
          });
          if(typeof updateQueueBadge==='function') updateQueueBadge(sid);
          showToast(t('steer_leftover_queued'),3000);
        }
      }catch(_){}
    });

    source.addEventListener('compressing',e=>{
      // Context auto-compression is starting. Surface the same calm running
      // compression card as manual /compress while the summarizer LLM call runs.
      if(!S.session||S.session.session_id!==activeSid) return;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      if(d.session_id&&d.session_id!==activeSid) return;
      _applyToAnchor('compressing',d,e);
      const state={
        sessionId:activeSid,
        phase:'running',
        automatic:true,
        message:'Compressing context',
        startedAt:Date.now()/1000,
      };
      if(typeof appendLiveCompressionCard==='function'&&appendLiveCompressionCard(state)){
        // Keep automatic compression inside the active Worklog. Calling
        // renderMessages() here rebuilds from the still-empty persisted
        // transcript during active streams and can erase already replayed tools.
        if(typeof clearCompressionUi==='function') clearCompressionUi();
        else window._compressionUi=null;
        snapshotLiveTurn();
        return;
      }
      if(typeof setCompressionUi==='function'){
        setCompressionUi(state);
      }
      snapshotLiveTurn();
    });

    source.addEventListener('compressed',e=>{
      // Context was auto-compressed during this turn. Keep the live timeline
      // honest by transitioning the running divider into a completed divider;
      // final settlement removes live-only compression rows from the Worklog.
      if(!S.session) return;
      const currentSid=S.session.session_id;
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      const eventSid=d.old_session_id||d.session_id||activeSid;
      const continuationSid=d.new_session_id||d.continuation_session_id||'';
      const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||d.new_session_id===currentSid||d.continuation_session_id===currentSid));
      if(!eventMatchesCurrent) return;
      _applyToAnchor('compressed',d,e);
      const displaySid=currentSid;
      if(d.usage&&typeof _syncCtxIndicator==='function'){
        S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
          ? _mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})
          : {...(S.lastUsage||{}),...d.usage};
        _syncCtxIndicator(S.lastUsage);
      }
      if(typeof appendLiveCompressionCard==='function'){
        appendLiveCompressionCard({
          sessionId:displaySid,
          phase:'done',
          automatic:true,
          message:'Context auto-compressed',
          continuationSessionId:continuationSid,
        });
      }
      if(typeof clearCompressionUi==='function') clearCompressionUi();
      else window._compressionUi=null;
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(!S.busy&&typeof renderMessages==='function') renderMessages();
    });

    source.addEventListener('metering',e=>{
      try{
        const d=JSON.parse(e.data||'{}');
        if((d.session_id||activeSid)!==activeSid) return;
        if(d.usage&&typeof _syncCtxIndicator==='function'){
          if(S.session&&S.session.session_id===activeSid){
            S.lastUsage=typeof _mergeUsageForCtxIndicator==='function'
              ? _mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})
              : {...(S.lastUsage||{}),...d.usage};
            _syncCtxIndicator(S.lastUsage);
          }
        }
        if(d.estimated===true||d.tps_available!==true||typeof d.tps!=='number'||d.tps<=0){
          if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(null);
          return;
        }
        if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(d.tps);
      }catch(_){}
    });

    source.addEventListener('apperror',e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      _clearStreamEndRecovery();
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      // Application-level error sent explicitly by the server (rate limit, crash, etc.)
      // This is distinct from the SSE network 'error' event below.
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      _clearOwnerInflightState();
      _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
      _clearStreamNotificationBackground(activeSid, streamId);
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      let d={};
      try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }
      const currentSid=S.session&&S.session.session_id;
      const eventSid=d.old_session_id||d.session_id||'';
      const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';
      const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||continuationSid===currentSid));
      if(eventMatchesCurrent){
        _flushReasoningToAnchor();
        _applyToAnchor('apperror',{
          type:d.type||'error',
          status:d.status||d.type||'error',
          message:d.message||'',
          hint:d.hint||'',
          details:d.details||'',
          session_id:d.session_id||eventSid||activeSid,
          old_session_id:d.old_session_id||null,
          new_session_id:d.new_session_id||d.continuation_session_id||null,
        },e);
      }
      if(S.session&&eventMatchesCurrent){
        S.activeStreamId=null;
        _scheduleAnchorRegistryCleanup();
        clearLiveToolCards();if(!assistantText)removeThinking();
        let isRecoveryControlMessage=false;
        try{
          const isRateLimit=d.type==='rate_limit';
          const isQuotaExhausted=d.type==='quota_exhausted';
          const isAuthMismatch=d.type==='auth_mismatch';
          const isGatewayAuthError=d.type==='gateway_auth_error';
          const isModelNotFound=d.type==='model_not_found';
          const isCancelled=d.type==='cancelled';
          const isInterrupted=d.type==='interrupted';
          const isCompressionExhausted=d.type==='compression_exhausted';
          const isToolLimitReached=d.type==='tool_limit_reached';
          isRecoveryControlMessage=isInterrupted && (d.recovery_control===true || _streamRecoveryControlMessageText(d.message));
          const isNoResponse=d.type==='no_response'||d.type==='silent_failure';
          const label=isCancelled?'Task cancelled':isInterrupted?'Response interrupted':isCompressionExhausted?'Context compression exhausted':isToolLimitReached?'Tool iteration limit reached':isQuotaExhausted?'Out of credits':isRateLimit?'Rate limit reached':isGatewayAuthError?(typeof t==='function'?t('gateway_auth_label'):'Gateway authentication failed'):isAuthMismatch?(typeof t==='function'?t('provider_mismatch_label'):'Provider mismatch'):isModelNotFound?(typeof t==='function'?t('model_not_found_label'):'Model not found'):isNoResponse?'No response from provider':'Error';
          const hint=d.hint?`\n\n*${d.hint}*`:'';
          const details=d.details?String(d.details).replace(/```/g,'`\u200b``'):'';
          const detailsLabel=isCancelled?'Cancellation details':isInterrupted?'Interruption details':isToolLimitReached?'Terminal state details':undefined;
          window._compressionUi=null;
          if(typeof clearCompressionUi==='function') clearCompressionUi();
          if(isRecoveryControlMessage){
            if(typeof showToast==='function') showToast('Stream recovery signal received. Restoring transcript...',3500,'error');
          } else if(d.session&&typeof d.session==='object'){
            S.session=d.session;
            const _nextMsgs3018=(d.session.messages||[]).filter(m=>m&&m.role);
            _attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);
            S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);
            if(S.session&&S.session.session_id){
              try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
              if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
            }
          } else {
            const recovery=(d.compression_recovery&&typeof d.compression_recovery==='object')?d.compression_recovery:null;
            S.messages.push({role:'assistant',content:`**${label}:** ${d.message}${hint}`,provider_details:details,provider_details_label:detailsLabel,_compressionRecovery:recovery||undefined});
            _attachProjectedAnchorSceneToLastAssistant(S.messages);
          }
        }catch(_){
          S.messages.push({role:'assistant',content:'**Error:** An error occurred. Check server logs.'});
          _attachProjectedAnchorSceneToLastAssistant(S.messages);
        }
        if(isRecoveryControlMessage){
          (async()=>{
            if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;
            if(S.session&&S.session.session_id===activeSid){
              S.messages=_filterRecoveryControlMessages(S.messages||[]);
              _markSessionViewed(activeSid, S.messages.length);
              renderMessages({preserveScroll:true});
            }
          })();
        } else {
          _markSessionViewed((S.session&&S.session.session_id)||activeSid, S.messages.length);
          renderMessages({preserveScroll:true});
        }
      }else if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,d.message||'Error');
      }
      _setActivePaneIdleIfOwner();
      renderSessionList(); // clear streaming indicator immediately on apperror
    });

    source.addEventListener('warning',e=>{
      // Non-fatal warning from server (e.g. fallback activated, retrying)
      if(!S.session||S.session.session_id!==activeSid) return;
      try{
        const d=JSON.parse(e.data);
        if(d.type==='approval_gateway_unsupported'){
          if(typeof showToast==='function') showToast(typeof t==='function'?t('approval_gateway_unsupported_label'):'Approvals not supported',4000,'warning');
          return;
        }
        if(d.type==='approval_gateway_offline'){
          if(typeof showToast==='function') showToast(d.message||'Gateway offline',4000,'warning');
          return;
        }
        // Show as a small inline notice, not a full error
        setComposerStatus(`${d.message||'Warning'}`);
        // If it's a fallback notice, show it briefly then clear
        if(d.type==='fallback') setTimeout(()=>setComposerStatus(''),4000);
      }catch(_){}
    });

    source.addEventListener('error',async e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source) && !_streamFinalized){
        return;
      }
      if(_terminalStateReached || _streamFinalized){
        _closeSource(source);
        return;
      }
      // #3885: if a stream_end recovery is in flight, don't start a competing
      // reconnect — recovery polls server state and owns the terminal decision
      // (else its exhaustion could mute a freshly reconnected stream). Opus stage-LK.
      if(_pendingStreamEndRecovery){
        _closeSource(source);
        return;
      }
      if(typeof recordClientSSEError==='function') recordClientSSEError('chat-response',{ready_state:source?source.readyState:null,session_id:activeSid,stream_id:streamId,reason:'chat EventSource.onerror'});
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _closeSource(source);
      // If the user has switched to a different session, don't attempt to
      // reconnect — the old stream's EventSource was closed intentionally
      // during session switch and reconnecting would leak a background stream.
      if(!_isSessionCurrentPane(activeSid)) return;
      if(_terminalStateReached || _streamFinalized){
        return;
      }
      // Attempt several reconnect/replay probes before declaring the turn lost.
      // A short-lived SSE error can arrive while the worker is still running or
      // while the run-journal replay file is just becoming visible. The old
      // single 1.5s probe could fall through to _handleStreamError(), clearing
      // S.activeStreamId/INFLIGHT and rendering a connection-interrupted marker
      // even though the backend was still producing tokens; the settled response
      // then reappeared later from sidecar/replay. Keep the live DOM/state intact
      // during this retry window and only surface an error after all probes fail.
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        const _retryDelays=[1500,3000,5000,8000,12000,20000];
        setComposerStatus(`Reconnecting… (1/${_retryDelays.length})`);
        const _probeReconnect=async(attempt=0)=>{
          if(_terminalStateReached || _streamFinalized) return;
          if(!_isSessionCurrentPane(activeSid)) return;
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st&&st.active){
              setComposerStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
            if(st&&st.replay_available){
              setComposerStatus('Restoring stream…');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
          }catch(_){
            if(_deferStreamErrorIfOffline()) return;
          }
          if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;
          if(_deferStreamErrorIfOffline()) return;
          if(_deferStreamErrorIfPageHidden(source)) return;
          const nextDelay=_retryDelays[attempt+1];
          if(nextDelay){
            setComposerStatus(`Reconnecting… (${attempt+2}/${_retryDelays.length})`);
            setTimeout(()=>{void _probeReconnect(attempt+1);}, nextDelay);
            return;
          }
          // Last-ditch: the stream may have finished while we were retrying.
          // _restoreSettledSession polls the full session API (not just stream
          // status) and can recover a completed response without an error banner.
          // This is especially important on iOS where Tailscale reconnects can
          // take longer than the retry window.
          setComposerStatus('Restoring session…');
          let _restoreTimedOut=false;
          const _restoreTimer=setTimeout(()=>{
            // If _restoreSettledSession hangs (flaky Tailscale), don't leave
            // the UI stuck on "Restoring session…" forever. Fall through to
            // _handleStreamError after 8s.
            _restoreTimedOut=true;
            if(!_terminalStateReached&&!_streamFinalized){
              if(_deferStreamErrorIfOffline()) return;
              if(_deferStreamErrorIfPageHidden(source)) return;
              _flushReasoningToAnchor();
              _scheduleAnchorRegistryCleanup(120000);
              _handleStreamError(source);
            }
          },8000);
          try{
            if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})){
              if(_restoreTimedOut) return; // timer already fired _handleStreamError
              clearTimeout(_restoreTimer);
              return;
            }
          }catch(_){
            // _restoreSettledSession threw. If the timer already fired,
            // _handleStreamError was called there; we return below.
            // Otherwise the code below cancels the timer and calls it directly.
          }
          if(_restoreTimedOut) return; // timer already fired _handleStreamError
          clearTimeout(_restoreTimer);
          if(_terminalStateReached||_streamFinalized) return;
          if(_deferStreamErrorIfOffline()) return;
          if(_deferStreamErrorIfPageHidden(source)) return;
          _flushReasoningToAnchor();
          _scheduleAnchorRegistryCleanup(120000);
          _handleStreamError(source);
        };
        setTimeout(()=>{void _probeReconnect(0);},_retryDelays[0]);
        return;
      }
      if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup(120000);
      _handleStreamError(source);
    });

    source.addEventListener('cancel',e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      _clearStreamEndRecovery();
      _terminalStateReached=true;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      try{if(source&&source.readyState!==2)source.close();}catch(_){ }
      _clearOwnerInflightState();
      _clearStreamHidden(activeSid, streamId);  // #4416: terminal path, drop hidden tracker
      _clearStreamNotificationBackground(activeSid, streamId);
      _clearApprovalForOwner();
      _clearClarifyForOwner('cancelled');
      let _cancelData={};
      try{ _cancelData=JSON.parse(e.data||'{}')||{}; }catch(_){ _cancelData={}; }
      _flushReasoningToAnchor();
      _applyToAnchor('cancel',{
        status:_cancelData.status||_cancelData.type||'cancelled',
        message:_cancelData.message||'',
        session_id:_cancelData.session_id||activeSid,
      },e);
      _scheduleAnchorRegistryCleanup();
      if(S.session&&S.session.session_id===activeSid){
        S.activeStreamId=null;
      }
      const _applyCancelSessionPayload=(sessionPayload)=>{
        if(!sessionPayload||typeof sessionPayload!=='object'||!S.session||S.session.session_id!==activeSid) return false;
        // Belt-and-suspenders: the embedded cancel snapshot must be for THIS session.
        // The GET path guarantees it via the URL; the embedded path via the stream→session
        // binding — but reject a mismatched id so a stray payload can't overwrite the view.
        if(sessionPayload.session_id&&sessionPayload.session_id!==activeSid) return false;
        // Capture follow-intent BEFORE replacing S.messages: a reader who was
        // following the live stream when it got cancelled/reconnected must land at
        // the bottom (where the cancellation notice renders), not be stranded at a
        // stale mid-stream scrollTop by preserveScroll's restore path. Same
        // jump-on-recovery class as the Connection-interrupted path below.
        const _wasFollowingAtCancel=((typeof _isMessagePaneNearBottom==='function')
            ? _isMessagePaneNearBottom(1200)
            : true)
          && !((typeof _isMessageReaderUnpinned==='function')
            ? _isMessageReaderUnpinned()
            : (typeof _messageUserUnpinned!=='undefined' && _messageUserUnpinned));
        S.session=sessionPayload;
        const _nextMsgs3018=(sessionPayload.messages||[]).filter(m=>m&&m.role);
        _attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);
        S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);
        if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
        clearLiveToolCards();if(!assistantText)removeThinking();
        _markSessionViewed(activeSid, sessionPayload.message_count ?? S.messages.length);
        renderMessages({preserveScroll:true});
        if(_wasFollowingAtCancel && typeof scrollToBottom==='function') scrollToBottom();
        return true;
      };
      // Prefer the canonical session snapshot embedded in the terminal cancel event.
      // It includes _partial reasoning/tool rows captured by cancel_stream(), avoiding
      // a second GET race where the visible cancelled work briefly collapses to only
      // the fallback "Task cancelled" marker (#4076).
      const _cancelSessionPayload=_cancelData&&typeof _cancelData.session==='object'?_cancelData.session:null;
      (async()=>{
        try{
          if(_applyCancelSessionPayload(_cancelSessionPayload)) return;
          // Fetch latest session from server to get accurate message list (includes cancel status)
          // This ensures messages stay in sync with server, fixing race condition where local
          // "*Task cancelled.*" message gets lost when done event overwrites S.messages
          const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
          if(data&&data.session) _applyCancelSessionPayload(data.session);
        }catch(_){
          // Fallback to local cancel message if API fails
          if(S.session&&S.session.session_id===activeSid){
            const _wasFollowingAtCancelFb=((typeof _isMessagePaneNearBottom==='function')
                ? _isMessagePaneNearBottom(1200)
                : true)
              && !((typeof _isMessageReaderUnpinned==='function')
                ? _isMessageReaderUnpinned()
                : (typeof _messageUserUnpinned!=='undefined' && _messageUserUnpinned));
            clearLiveToolCards();if(!assistantText)removeThinking();
            const cancelAgentName=(assistantDisplayName()+'').trim()||'Hermes';
            S.messages.push({role:'assistant',content:`**Task cancelled:** Task cancelled.\n\n*The run was cancelled by the user before ${cancelAgentName} finished. No provider failure occurred.*`,provider_details:'Task cancelled.',provider_details_label:'Cancellation details',_error:true});
            _attachProjectedAnchorSceneToLastAssistant(S.messages);
            renderMessages({preserveScroll:true});
            if(_wasFollowingAtCancelFb && typeof scrollToBottom==='function') scrollToBottom();
            _markSessionViewed(activeSid, S.messages.length);
          }
        }
      })();
      renderSessionList();
      _setActivePaneIdleIfOwner();
    });

    for(const _runJournalEventName of ['token','interim_assistant','reasoning','tool','tool_complete','todo_state','approval','clarify','state_saved','title','title_status','context_status','goal','goal_continue','done','stream_end','pending_steer_leftover','compressing','compressed','metering','apperror','warning','error','cancel']){
      source.addEventListener(_runJournalEventName,_rememberRunJournalCursor);
    }
  }

  // #3018: per-turn ephemeral fields are computed client-side in _finishDone
  // and attached to message objects (S.messages). When a server refresh
  // (loadSession, _restoreSettledSession, external active-session poll,
  // SSE error recovery) replaces S.messages with fresh server data, those
  // fields are dropped and the usage badge / duration / gateway routing
  // pill flashes-then-disappears. Carry them forward by matching messages
  // on (role, timestamp, content prefix) — the same identity the renderer
  // already uses for stable keys.
  function _messageIdentityKey(m){
    if(!m||!m.role) return '';
    const ts=m._ts||m.timestamp||'';
    let body='';
    if(typeof m.content==='string') body=m.content;
    else if(Array.isArray(m.content)){
      try{ body=m.content.map(p=>(p&&typeof p==='object')?(p.text||p.input_text||'')||'':String(p||'')).join('').slice(0,160); }catch(_){ body=''; }
    }
    return `${m.role}|${ts}|${body.slice(0,160)}`;
  }
  const _EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
  function _carryForwardEphemeralTurnFields(prevMessages, nextMessages){
    if(!Array.isArray(prevMessages)||!Array.isArray(nextMessages)) return nextMessages;
    if(!prevMessages.length||!nextMessages.length) return nextMessages;
    const prevIdx=new Map();
    for(const pm of prevMessages){
      const k=_messageIdentityKey(pm); if(!k) continue;
      // If duplicate keys, prefer the latest occurrence (it carries the
      // most-recently-attached ephemeral state).
      prevIdx.set(k,pm);
    }
    for(const nm of nextMessages){
      const k=_messageIdentityKey(nm); if(!k) continue;
      const pm=prevIdx.get(k); if(!pm) continue;
      for(const f of _EPHEMERAL_TURN_FIELDS){
        if(pm[f]!=null && nm[f]==null) nm[f]=pm[f];
      }
    }
    return nextMessages;
  }
  if(typeof window!=='undefined'){
    window._carryForwardEphemeralTurnFields=_carryForwardEphemeralTurnFields;
  }

  async function _restoreSettledSession(source, options=null){
    const returnStatus=!!(options&&options.status);
    const preserveVisibleOnShorterTerminalSnapshot=!!(options&&options.preserveVisibleOnShorterTerminalSnapshot);
    if(_isActiveSession() && S.activeStreamId!==streamId){
      _closeSource(source);
      return returnStatus?'stale':false;
    }
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);
      // Opus #2852 race-fix: if a late `done` event ran the finalize path while
      // we were awaiting the network roundtrip, bail out — done already settled.
      if(_streamFinalized) return returnStatus?'restored':true;
      const session=data&&data.session;
      if(!session) return returnStatus?'missing':false;
      if(session.active_stream_id||session.pending_user_message) return returnStatus?'active':false;
      if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _streamFinalized=true;
      _cancelAnimationFramePendingStreamRender();
      _streamFadeCleanupReduceMotionListener();
      _smdEndParser();
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      _clearOwnerInflightState();
      _flushReasoningToAnchor();
      _scheduleAnchorRegistryCleanup();
      _closeSource(source);
      _clearApprovalForOwner();
      _clearClarifyForOwner('terminal');
      const isSessionViewed=_isSessionActivelyViewed(activeSid);
      const completedSid=session.session_id||activeSid;
      if(!isSessionViewed && typeof _markSessionCompletionUnread==='function'){
        _markSessionCompletionUnread(completedSid, session.message_count);
      }
      const isActiveSession=_isSessionCurrentPane(activeSid);
      if(isActiveSession){
        S.activeStreamId=null;
        clearLiveToolCards();if(!assistantText)removeThinking();
        S.session=session;
        const _nextMsgs3018=(session.messages||[]).filter(m=>m&&m.role);
        const _currentMessages=Array.isArray(S.messages)?S.messages:[];
        const _currentVisibleMessages=_filterRecoveryControlMessages(_currentMessages || []);
        const _stagedMessages=_carryForwardEphemeralTurnFields(_currentMessages, _nextMsgs3018);
        const _currentVisibleEndsWithTerminalMarker=(
          _currentVisibleMessages.length>0 &&
          _isTerminalStreamErrorMarkerMessage(_currentVisibleMessages[_currentVisibleMessages.length-1])
        );
        const _stagedMatchesCurrentPrefix=(
          _stagedMessages.length>0 &&
          _stagedMessages.length<_currentVisibleMessages.length &&
          _currentVisibleEndsWithTerminalMarker &&
          _stagedMessages.every((message, idx)=>{
            const stagedKey=_messageIdentityKey(message);
            const currentKey=_messageIdentityKey(_currentVisibleMessages[idx]);
            return !!stagedKey && stagedKey===currentKey;
          })
        );
        const _preserveCurrentTranscript=preserveVisibleOnShorterTerminalSnapshot&&_stagedMatchesCurrentPrefix;
        const _resolvedMessages=_preserveCurrentTranscript
          ? [..._stagedMessages,..._currentVisibleMessages.slice(_stagedMessages.length)]
          : _stagedMessages;
        S.messages=_filterRecoveryControlMessages(_resolvedMessages || []);
        _attachProjectedAnchorSceneToLastAssistant(S.messages);
        if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
        if(S.session&&S.session.session_id){
          try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
          if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
        }
        const _markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages);
        if(_markerOnlyAssistantError&&typeof showToast==='function') showToast('No response received after context compression. Please retry.',5000,'error');
        const hasMessageToolMetadata=S.messages.some(m=>{
          if(!m||m.role!=='assistant') return false;
          // Recognize both the standard `tool_calls` (used by completed assistant
          // turns where the LLM emitted tool_call entries) and the WebUI-internal
          // `_partial_tool_calls` (used on Stop/Cancel partial messages — see
          // api/streaming.py cancel_stream).
          const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
          const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
          const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
          return hasTc||hasPartialTc||hasTu;
        });
        if(!hasMessageToolMetadata&&session.tool_calls&&session.tool_calls.length){
          S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(session.tool_calls||[]);
        }else{
          if(hasMessageToolMetadata) S._settledLiveToolMetadata=S.toolCalls.map(tc=>({...tc,done:true}));
          S.toolCalls=[];
        }
        if(isSessionViewed) _markSessionViewed(completedSid, session.message_count ?? S.messages.length);
        // Expand render window so the settled render doesn't hide Activity.
        if(typeof _messageRenderableMessageCount==='function'&&typeof _messageRenderWindowSize!=='undefined'){
          _messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize==='function'?_currentMessageRenderWindowSize():50, _messageRenderableMessageCount());
        }
        syncTopbar();renderMessages({preserveScroll:true});
      }
      if(_isActiveSession()) _queueDrainSid=activeSid;
      renderSessionList();
      _setActivePaneIdleIfOwner();
      return returnStatus?'restored':true;
    }catch(_){
      return returnStatus?'error':false;
    }
  }

  function _handleStreamError(source){
    if(_isActiveSession() && S.activeStreamId!==streamId){
      _closeSource(source);
      return;
    }
    _clearStreamEndRecovery();
    // Opus review Q1: mirror done/apperror/cancel finalization so any pending rAF
    // cannot fire after renderMessages() has settled the DOM with the error message.
    if(_persistTimer){clearTimeout(_persistTimer);_persistTimer=null;}
    _cancelThrottledSnapshotTimer();
    _clearAnchorProseIncrementalNode();
    _streamFinalized=true;
    _cancelAnimationFramePendingStreamRender();
    _streamFadeCleanupReduceMotionListener();
    if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
    _clearOwnerInflightState();
    _closeSource(source);
    _clearApprovalForOwner();
    _clearClarifyForOwner('terminal');
    if(S.session&&S.session.session_id===activeSid){
      S.activeStreamId=null;
      // Capture the reader's follow-intent BEFORE mutating S.messages. If the SSE
      // dropped while they were following the live stream (pinned / near the
      // bottom), the disconnect-recovery render must land them at the bottom where
      // the "Connection interrupted" notice appears — NOT restore a stale
      // mid-stream scrollTop. preserveScroll's restore path keys on the pre-render
      // snapshot's bottom-distance, which during a live stream can read large
      // (content was still growing under a followed viewport), so it would yank a
      // following reader up to a historical position on a process restart / SSE
      // drop. This is the "restart/disconnect jump-back" report: the jump is the
      // recovery render restoring an old position into a DOM whose height changed.
      // Follow-intent must be STICKY-aware: a reader who manually scrolled up
      // (sets _messageUserUnpinned, ui.js scroll listener) but stayed within
      // 1200px of the bottom would read _isMessagePaneNearBottom(1200)===true,
      // so a proximity-only check would re-follow them on recovery and clobber
      // their position (maintainer-reproduced bounce). Require near-bottom AND
      // not-unpinned. scrollToBottom() clears _messageUserUnpinned, so a genuine
      // follower stays pinned; only a manually-unpinned reader is spared.
      const _wasFollowingAtDisconnect=((typeof _isMessagePaneNearBottom==='function')
          ? _isMessagePaneNearBottom(1200)
          : true)
        && !((typeof _isMessageReaderUnpinned==='function')
          ? _isMessageReaderUnpinned()
          : (typeof _messageUserUnpinned!=='undefined' && _messageUserUnpinned));
      _flushReasoningToAnchor();
      _applyToAnchor('error',{
        status:'connection_lost',
        message:'The browser lost the live SSE connection before the response finished.',
        session_id:activeSid,
      },null);
      clearLiveToolCards();if(!assistantText)removeThinking();
      _ensureSingleTerminalStreamErrorMarker(S.messages);
      _attachProjectedAnchorSceneToLastAssistant(S.messages);
      renderMessages({preserveScroll:true});
      // If they were following the stream, force the viewport to the bottom after
      // the recovery render so they see the interruption notice in place instead
      // of being thrown back into the transcript. Readers who had scrolled up to
      // read history are left where they were (the near-bottom guard above is
      // false for them).
      if(_wasFollowingAtDisconnect && typeof scrollToBottom==='function') scrollToBottom();
      _markSessionViewed(activeSid, S.messages.length);
    }else{
      if(typeof trackBackgroundError==='function'){
        const _errTitle=(typeof _allSessions!=='undefined'&&_allSessions.find(s=>s.session_id===activeSid)||{}).title||null;
        trackBackgroundError(activeSid,_errTitle,'Connection interrupted');
      }
    }
    _setActivePaneIdleIfOwner();
  }

  (async()=>{
    // Reattach path can carry stale stream ids after server restart; preflight
    // status avoids opening a dead SSE URL that will 404 in the console.
    let replayOnly=false;
    if(reconnecting){
      try{
        const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
        if(!st.active&&st.replay_available){
          replayOnly=true;
        }else if(!st.active){
          _clearOwnerInflightState();
          _clearApprovalForOwner();
          _clearClarifyForOwner('terminal');
          if(S.session&&S.session.session_id===activeSid){
            // Follow-intent BEFORE removing live placeholders: a reader following
            // the (now-dead) stream should stay pinned to the bottom as the
            // thinking/tool placeholders are cleared, not be stranded mid-transcript
            // by preserveScroll restoring a stale scrollTop after the height shrinks.
            const _wasFollowingAtReconnectDead=((typeof _isMessagePaneNearBottom==='function')
                ? _isMessagePaneNearBottom(1200)
                : true)
              && !((typeof _isMessageReaderUnpinned==='function')
                ? _isMessageReaderUnpinned()
                : (typeof _messageUserUnpinned!=='undefined' && _messageUserUnpinned));
            S.activeStreamId=null;
            clearLiveToolCards();
            removeThinking();
            if(_isActiveSession()) _queueDrainSid=activeSid;
            _setActivePaneIdleIfOwner();
            renderMessages({preserveScroll:true});
            if(_wasFollowingAtReconnectDead && typeof scrollToBottom==='function') scrollToBottom();
            renderSessionList();
          }
          _scheduleAnchorRegistryCleanup(120000);
          return;
        }
      }catch(_){}
    }
    const replayParams=(reconnecting||replayOnly)?_runJournalReplayParams():'';
    _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${replayParams}`,document.baseURI||location.href).href,{withCredentials:true}));
  })();

}

Object.assign(HermesMessages, {
  attachLiveStream,
});
