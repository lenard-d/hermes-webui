import {
  _bgTaskCompleteRingBufferAdd,
  _chatPayloadModelState,
  _deferStreamErrorIfOffline,
  _desktopBackgroundedForNotifications,
  _extractInlineThinkingFromContent,
  _isSessionActivelyViewed,
  _isSessionCurrentPane,
  _markSessionViewed,
} from './core.js';
import {
  LIVE_STREAMS,
  _STREAM_NOTIFICATION_BACKGROUND,
  _STREAM_WAS_HIDDEN,
  _bindStreamHiddenTracker,
  _clearStreamHidden,
  _clearStreamNotificationBackground,
  _shouldForceCompletionNotification,
  closeLiveStream,
  closeOtherLiveStreams,
} from './stream-lifecycle.js';
import {
  _approvalSessionId,
  _clearApprovalPendingForSession,
  hideApprovalCard,
  showApprovalForSession,
  stopApprovalPolling,
  transcript,
} from './approvals.js';
import {
  _clarifySessionId,
  _clearClarifyPendingForSession,
  hideClarifyCard,
  showClarifyForSession,
  stopClarifyPolling,
} from './clarify.js';
import {
  _attentionSoundKey,
  _completionNotificationPreviewText,
  playAttentionSound,
  playNotificationSound,
  sendBrowserNotification,
} from './notifications.js';
import {
  _maybeNotifyPersistentStateSaved,
  _showPersistentStateToast,
} from './composer-context.js';
import {
  _handleBgTaskCompleteEvent,
  _resumeSessionStreamAfterLiveChat,
  _suspendSessionStreamForLiveChat,
} from './session-events.js';
import { applySessionTitleUpdate, send } from './send.js';
import { createStreamAnchorSceneSettlement } from './anchor-scene.js';
import { createStreamLiveToolTracker } from './live-tools.js';
import { createStreamRenderer } from './rendering.js';
import { createStreamRunJournalCursor } from './run-journal.js';

export function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  // Resolve the complete runtime module seam before touching hidden-state,
  // INFLIGHT, live transports, session suspension, or visible run status. A
  // partially loaded asset set must fail as one unit; otherwise send() has
  // already started the backend run while this client silently remains busy
  // without an SSE owner.
  const _requiredStreamFactories={
    runJournal:createStreamRunJournalCursor,
    anchorScene:createStreamAnchorSceneSettlement,
    liveTools:createStreamLiveToolTracker,
    renderer:createStreamRenderer,
  };
  const _missingStreamFactories=Object.entries(_requiredStreamFactories)
    .filter(([,factory])=>typeof factory!=='function')
    .map(([owner])=>owner);
  if(_missingStreamFactories.length){
    throw new Error(`required stream modules must load before attachLiveStream: ${_missingStreamFactories.join(', ')}`);
  }
  const _createRunJournalCursor=_requiredStreamFactories.runJournal;
  const _createAnchorSceneSettlement=_requiredStreamFactories.anchorScene;
  const _createLiveToolTracker=_requiredStreamFactories.liveTools;
  const _createStreamRenderer=_requiredStreamFactories.renderer;
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
  let _currentActivityBurstId=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentActivityBurstId)||0)||0;
  let _currentLiveSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  let _assistantSegmentSeq=Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].currentLiveSegmentSeq)||0)||0;
  const _runJournalCursor=_createRunJournalCursor({
    initialSeq:reconnecting?Number((INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastRunJournalSeq)||0):0,
    initialEventId:reconnecting?String((INFLIGHT[activeSid]&&INFLIGHT[activeSid].lastRunJournalEventId)||''):'',
    getInflight:()=>INFLIGHT[activeSid],
    persist:()=>_throttledPersist(),
  });
  const _rememberRunJournalCursor=_runJournalCursor.remember;
  const _runJournalReplayAfterSeq=_runJournalCursor.replayAfterSeq;
  const _runJournalReplayParams=_runJournalCursor.replayParams;
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
  const _anchorSceneSettlement=_createAnchorSceneSettlement({
    sessionId:activeSid,
    streamId,
    state:S,
    anchorRegistry:_anchorRegistry,
    projectLiveScene:_projectLiveAnchorActivityScene,
    activeMode:_anchorSceneActiveMode,
    rowDisplayHintForMode:_anchorSceneRowDisplayHintForMode,
    request:api,
    getOldestMessageIndex:()=>typeof _oldestIdx!=='undefined'?_oldestIdx:0,
  });
  function _attachProjectedAnchorSceneToLastAssistant(messages){
    return _anchorSceneSettlement.attachProjectedSceneToLastAssistant(messages);
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

  const _liveToolTracker=_createLiveToolTracker({
    sessionId:activeSid,
    state:S,
    inflightStore:INFLIGHT,
    uploaded,
    persist:persistInflightState,
    getAssistantRow:()=>assistantRow,
    getAssistantSegmentSeq:()=>_assistantSegmentSeq,
    getCurrentLiveSegmentSeq:()=>_currentLiveSegmentSeq,
    getCurrentActivityBurstId:()=>_currentActivityBurstId,
  });
  const upsertLiveToolCall=_liveToolTracker.upsert;

  const _streamRenderer=_createStreamRenderer({
    reconnecting,
    readState:()=>({
      assistantText,
      reasoningText,
      liveReasoningText,
      assistantRow,
      assistantBody,
      segmentStart,
      streamFinalized:_streamFinalized,
    }),
    updateLiveThinking:_updateLiveThinkingCard,
    upsertAnchorProse:_upsertAnchorProcessProse,
    syncWorklogReasons:(row,text)=>{
      if(typeof _syncLiveWorklogReasonsForAnchor==='function') _syncLiveWorklogReasonsForAnchor(row,text);
    },
    scrollPinned:()=>scrollIfPinned(),
    snapshotLiveTurn:_throttledSnapshotLiveTurn,
    resetSegmentState:()=>{
      assistantRow=null;
      assistantBody=null;
      segmentStart=assistantText.length;
      _freshSegment=true;
    },
  });
  const _stripXmlToolCalls=_streamRenderer.stripXmlToolCalls;
  const _streamDisplay=_streamRenderer.streamDisplay;
  const _parseStreamState=_streamRenderer.parseStreamState;
  const _renderLiveThinking=_streamRenderer.renderLiveThinking;
  const _smdEndParser=_streamRenderer.endParser;
  const _resetStreamFadeState=_streamRenderer.resetFadeState;
  const _cancelAnimationFramePendingStreamRender=_streamRenderer.cancelPendingRender;
  const _shouldUseLiveProseFade=_streamRenderer.shouldUseLiveProseFade;
  const _streamFadeCleanupReduceMotionListener=_streamRenderer.cleanupReduceMotion;
  const _drainStreamFadeBeforeDone=_streamRenderer.drainFadeBeforeDone;
  const _flushPendingSegmentRender=_streamRenderer.flushPendingSegment;
  const _resetAssistantSegment=_streamRenderer.resetAssistantSegment;
  const _scheduleRender=_streamRenderer.scheduleRender;
  const _clearAnchorProseIncrementalNode=_streamRenderer.clearAnchorProseIncrementalNode;

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
