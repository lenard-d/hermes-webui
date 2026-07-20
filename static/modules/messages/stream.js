import {
  _desktopBackgroundedForNotifications,
  _extractInlineThinkingFromContent,
  _isSessionActivelyViewed,
  _markSessionViewed,
} from './core.js';
import {
  LIVE_STREAMS,
  _STREAM_NOTIFICATION_BACKGROUND,
  _STREAM_WAS_HIDDEN,
  _bindStreamHiddenTracker,
  _clearStreamHidden,
  _clearStreamNotificationBackground,
  closeLiveStream,
  closeOtherLiveStreams,
} from './stream-lifecycle.js';
import {
  _approvalSessionId,
  _clearApprovalPendingForSession,
  hideApprovalCard,
  stopApprovalPolling,
  transcript,
} from './approvals.js';
import {
  _clarifySessionId,
  _clearClarifyPendingForSession,
  hideClarifyCard,
  stopClarifyPolling,
} from './clarify.js';
import {
  _maybeNotifyPersistentStateSaved,
  _showPersistentStateToast,
} from './composer-context.js';
import {
  _handleBgTaskCompleteEvent,
  _resumeSessionStreamAfterLiveChat,
  _suspendSessionStreamForLiveChat,
} from './session-events.js';
import { applySessionTitleUpdate } from './send.js';
import { createStreamAnchorLiveRuntime } from './anchor-live.js';
import { createStreamCompressionEventOwner } from './compression-events.js';
import { createStreamContentEventOwner } from './content-events.js';
import { createStreamControlEventOwner } from './control-events.js';
import { createStreamLiveToolTracker } from './live-tools.js';
import { createStreamRenderer } from './rendering.js';
import { createStreamProgressOwner } from './stream-progress.js';
import { createStreamRunJournalCursor } from './run-journal.js';
import { createStreamSessionRecovery } from './session-recovery.js';
import { createStreamTranscriptProjection } from './stream-transcript.js';
import { createStreamTerminalEventOwner } from './terminal-events.js';
import { createStreamTransportOwner } from './stream-transport.js';

export function attachLiveStream(activeSid, streamId, uploaded=[], options={}){
  if(!activeSid||!streamId) return;
  // Resolve the complete runtime module seam before touching hidden-state,
  // INFLIGHT, live transports, session suspension, or visible run status. A
  // partially loaded asset set must fail as one unit; otherwise send() has
  // already started the backend run while this client silently remains busy
  // without an SSE owner.
  const _requiredStreamFactories={
    runJournal:createStreamRunJournalCursor,
    anchorLive:createStreamAnchorLiveRuntime,
    compressionEvents:createStreamCompressionEventOwner,
    contentEvents:createStreamContentEventOwner,
    controlEvents:createStreamControlEventOwner,
    liveTools:createStreamLiveToolTracker,
    progress:createStreamProgressOwner,
    renderer:createStreamRenderer,
    sessionRecovery:createStreamSessionRecovery,
    terminalEvents:createStreamTerminalEventOwner,
    transcript:createStreamTranscriptProjection,
    transport:createStreamTransportOwner,
  };
  const _missingStreamFactories=Object.entries(_requiredStreamFactories)
    .filter(([,factory])=>typeof factory!=='function')
    .map(([owner])=>owner);
  if(_missingStreamFactories.length){
    throw new Error(`required stream modules must load before attachLiveStream: ${_missingStreamFactories.join(', ')}`);
  }
  const _createRunJournalCursor=_requiredStreamFactories.runJournal;
  const _createAnchorLiveRuntime=_requiredStreamFactories.anchorLive;
  const _createCompressionEventOwner=_requiredStreamFactories.compressionEvents;
  const _createControlEventOwner=_requiredStreamFactories.controlEvents;
  const _createLiveToolTracker=_requiredStreamFactories.liveTools;
  const _createStreamProgressOwner=_requiredStreamFactories.progress;
  const _createStreamRenderer=_requiredStreamFactories.renderer;
  const _createSessionRecovery=_requiredStreamFactories.sessionRecovery;
  const _createContentEventOwner=_requiredStreamFactories.contentEvents;
  const _createTerminalEventOwner=_requiredStreamFactories.terminalEvents;
  const _createTranscriptProjection=_requiredStreamFactories.transcript;
  const _createTransportOwner=_requiredStreamFactories.transport;
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
  const _streamTranscript=_createTranscriptProjection({
    messageText:message=>String(typeof msgContent==='function'?msgContent(message):(message&&message.content||'')),
    markerOnlyText:text=>typeof _isPreservedCompressionTaskListMarkerOnlyText==='function'
      && _isPreservedCompressionTaskListMarkerOnlyText(text),
  });
  const _carryForwardEphemeralTurnFields=_streamTranscript.carryForwardEphemeralTurnFields;
  const _ensureSingleTerminalStreamErrorMarker=_streamTranscript.ensureSingleTerminalStreamErrorMarker;
  const _filterRecoveryControlMessages=_streamTranscript.filterRecoveryControlMessages;
  const _isTerminalStreamErrorMarkerMessage=_streamTranscript.isTerminalStreamErrorMarkerMessage;
  const _replaceMarkerOnlyAssistantWithStreamError=_streamTranscript.replaceMarkerOnlyAssistantWithStreamError;
  const _streamRecoveryControlMessageText=_streamTranscript.isRecoveryControlText;
  function _setActivePaneIdleIfOwner(){
    if(_isActiveSession()||!S.session||!INFLIGHT[S.session.session_id]){
      setBusy(false);
      setComposerStatus('');
      if(typeof setStatus==='function') setStatus('');
    }
  }
  const _streamProgress=_createStreamProgressOwner({
    sessionId:activeSid,
    streamId,
    getInflight:()=>INFLIGHT[activeSid],
    getUploaded:()=>[...uploaded],
    getTodos:()=>S.todos,
    getTodoStateMeta:()=>S.todoStateMeta,
    save:(sid,payload)=>{
      if(typeof saveInflightState==='function') saveInflightState(sid,payload);
    },
    snapshot:(sid)=>{
      if(typeof snapshotLiveTurnHtmlForSession==='function') snapshotLiveTurnHtmlForSession(sid);
    },
  });
  function persistInflightState(){
    _streamProgress.persistNow();
  }
  function snapshotLiveTurn(){
    _streamProgress.snapshotNow();
  }
  // Throttled per-frame variant. snapshotLiveTurnHtmlForSession serializes the
  // whole (growing) live turn via turn.outerHTML — O(n)/frame -> O(n^2) over a
  // long answer, and a real GC-pressure source. The snapshot only backs
  // mid-stream session-switch restore, and the switch path (sessions.js) plus
  // the stream event boundaries (tool/done) already capture synchronously, so a
  // coarse trailing snapshot during streaming is sufficient. (#5455 WS2.2)
  function _throttledSnapshotLiveTurn(){
    _streamProgress.snapshotSoon();
  }
  function _cancelThrottledSnapshotTimer(){
    _streamProgress.cancelSnapshot();
  }
  // Throttled variant for token-by-token updates. persistInflightState()
  // calls saveInflightState() which does JSON.parse + JSON.stringify + write
  // on the entire inflight map every call. On a fast model at 60 tok/s with
  // a 10KB messages array this is ~36MB of JSON churn per second — a major
  // GC pressure source that causes the renderer to crash under load.
  // State transitions (tool events, done, error) still call persistInflightState()
  // directly so no more than 2s of progress is lost on a crash.
  function _throttledPersist(){
    _streamProgress.persistSoon();
  }
  function _cancelThrottledPersistTimer(){
    _streamProgress.cancelPersist();
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
    _cancelThrottledPersistTimer();
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
  let _terminalStateReached=false;
  let _pendingStreamEndRecovery=false;
  let _streamEndRecoveryTimer=null;
  let _streamEndRecoveryAttempts=0;

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
  const _anchorLiveRuntime=_createAnchorLiveRuntime({
    sessionId:activeSid,
    streamId,
    state:S,
    request:api,
    runJournalReplayAfterSeq:_runJournalReplayAfterSeq,
    isActiveSession:_isActiveSession,
    liveThinkingPlacement:_liveThinkingPlacement,
    syncInflightAssistantMessage,
    readPlacement:()=>({
      assistantSegmentSeq:_assistantSegmentSeq,
      currentLiveSegmentSeq:_currentLiveSegmentSeq,
      currentActivityBurstId:_currentActivityBurstId,
    }),
    readReasoning:()=>({reasoningText,liveReasoningText}),
    writeReasoning:(next)=>{
      if(next&&Object.prototype.hasOwnProperty.call(next,'reasoningText')) reasoningText=next.reasoningText;
      if(next&&Object.prototype.hasOwnProperty.call(next,'liveReasoningText')) liveReasoningText=next.liveReasoningText;
    },
    getInflight:()=>INFLIGHT[activeSid],
    getOldestMessageIndex:()=>typeof _oldestIdx!=='undefined'?_oldestIdx:0,
  });
  const _anchorRegistry=_anchorLiveRuntime.registry;
  const _applyToAnchor=_anchorLiveRuntime.apply;
  const _ensureAnchorCompressionCompletedOnLiveProgress=_anchorLiveRuntime.ensureCompressionCompletedOnLiveProgress;
  const _attachProjectedAnchorSceneToLastAssistant=_anchorLiveRuntime.attachProjectedSceneToLastAssistant;
  const _upsertAnchorProcessProse=_anchorLiveRuntime.upsertProcessProse;
  const _upsertAnchorReasoning=_anchorLiveRuntime.upsertReasoning;
  const _stripLiveReasoningEcho=_anchorLiveRuntime.stripReasoningEcho;
  const _flushReasoningToAnchor=_anchorLiveRuntime.flushReasoning;
  const _scheduleAnchorRegistryCleanup=_anchorLiveRuntime.scheduleCleanup;

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
    streamId,
    state:S,
    inflightStore:INFLIGHT,
    uploaded,
    persist:persistInflightState,
    getAssistantRow:()=>assistantRow,
    getAssistantSegmentSeq:()=>_assistantSegmentSeq,
    getCurrentLiveSegmentSeq:()=>_currentLiveSegmentSeq,
    getCurrentActivityBurstId:()=>_currentActivityBurstId,
    eventScene:{
      isTerminal:()=>_terminalStateReached||_streamFinalized,
      completeAutomaticCompression:(sid)=>_completeAutomaticCompressionOnLiveProgress(sid),
      pendingDisplayText:()=>segmentStart===0
        ? (_parseStreamState().displayText||'')
        : _stripXmlToolCalls(assistantText.slice(segmentStart)),
      sealPendingProse:(text,options)=>_upsertAnchorProcessProse(text,options),
      applyToAnchor:_applyToAnchor,
      scheduleArtifacts:()=>{
        if(S.session&&S.session.session_id===activeSid&&typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();
      },
      finalizeThinking:()=>{
        if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      },
      clearReasoning:()=>{ liveReasoningText=''; },
      removeRunningRow:()=>{ const row=$('toolRunningRow');if(row)row.remove(); },
      hasAssistantOutput:()=>!!(assistantRow&&assistantBody),
      ensureAssistantRow,
      flushPendingSegment:(options)=>_flushPendingSegmentRender(options),
      appendToolCard:(toolCall,identity)=>appendLiveToolCard(toolCall,identity),
      snapshot:snapshotLiveTurn,
      startFreshSegment:()=>{ _freshSegment=true; },
      endParser:()=>_smdEndParser(),
      resetAssistantSegment:()=>_resetAssistantSegment(),
      scrollPinned:()=>scrollIfPinned(),
      noteWorkspaceMutation:(toolCall)=>{
        if(typeof noteWorkspaceMutationsFromToolCall==='function') noteWorkspaceMutationsFromToolCall(toolCall);
      },
      notifyPersistentStateSaved:(toolCall)=>_maybeNotifyPersistentStateSaved(toolCall),
      refreshOpenPreview:()=>{
        if(typeof refreshOpenPreviewIfMutated==='function') refreshOpenPreviewIfMutated();
      },
    },
  });

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

  const _compressionEvents=_createCompressionEventOwner({
    sessionId:activeSid,
    state:S,
    applyToAnchor:_applyToAnchor,
    completeAnchorOnLiveProgress:_ensureAnchorCompressionCompletedOnLiveProgress,
    snapshot:snapshotLiveTurn,
    mergeUsage:(usage,fallback)=>typeof _mergeUsageForCtxIndicator==='function'
      ? _mergeUsageForCtxIndicator(usage,fallback)
      : {...fallback,...usage},
    syncUsage:(usage)=>{
      if(typeof _syncCtxIndicator==='function') _syncCtxIndicator(usage);
    },
    view:{
      hasRunningCard:()=>!!document.querySelector('[data-live-compression-card="1"][data-compression-started-at]'),
      getCompressionState:()=>window._compressionUi||null,
      appendLiveCard:(compressionState)=>typeof appendLiveCompressionCard==='function'
        ? appendLiveCompressionCard(compressionState)
        : false,
      clearCompressionUi:()=>{
        if(typeof clearCompressionUi==='function') clearCompressionUi();
        else window._compressionUi=null;
      },
      setCompressionUi:(compressionState)=>{
        if(typeof setCompressionUi==='function') setCompressionUi(compressionState);
      },
      setCompressionSessionLock:(value)=>{
        if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(value);
      },
      renderMessages:()=>{
        if(typeof renderMessages==='function') renderMessages();
      },
    },
  });
  const _completeAutomaticCompressionOnLiveProgress=_compressionEvents.completeOnLiveProgress;

  const _controlEvents=_createControlEventOwner({
    sessionId:activeSid,
    state:S,
    applyToAnchor:_applyToAnchor,
    showPersistentStateToast:(...args)=>_showPersistentStateToast(...args),
    applySessionTitle:(...args)=>applySessionTitleUpdate(...args),
    handleBackgroundTaskComplete:(...args)=>_handleBgTaskCompleteEvent(...args),
    ui:{
      translate:(key,...args)=>typeof t==='function'?t(key,...args):String(key||''),
      setComposerStatus:(value)=>{
        if(typeof setComposerStatus==='function') setComposerStatus(value);
      },
      showToast:(...args)=>{
        if(typeof showToast==='function') showToast(...args);
      },
      queueSessionMessage:typeof queueSessionMessage==='function'
        ? (sid,payload)=>queueSessionMessage(sid,payload)
        : null,
      updateQueueBadge:(sid)=>{
        if(typeof updateQueueBadge==='function') updateQueueBadge(sid);
      },
    },
  });

  const _contentEvents=_createContentEventOwner({
    sessionId:activeSid,
    streamId,
    state:S,
    inflightStore:INFLIGHT,
    completeAutomaticCompression:_completeAutomaticCompressionOnLiveProgress,
    persistInflight:persistInflightState,
    turn:{
      isTerminal:()=>_terminalStateReached||_streamFinalized,
      appendAssistantText:text=>{ assistantText+=String(text||''); },
      appendInterimAssistantText:text=>{ assistantText+=assistantText?`\n\n${text}`:text; },
      appendReasoning:text=>{ reasoningText+=text;liveReasoningText+=text; },
      assistantRow:()=>assistantRow,
      interimSnippetCount:()=>visibleInterimSnippets.length,
      isFreshSegment:()=>_freshSegment,
      liveThinkingText:_liveThinkingText,
      ownsActiveStreamOrBackground:_ownsActiveStreamOrBackground,
      pushInterimSnippet:text=>{ visibleInterimSnippets.push(text); },
      recordActivityBoundary,
      setLiveReasoningText:text=>{ liveReasoningText=text; },
      syncInflight:syncInflightAssistantMessage,
    },
    renderer:{
      ensureAssistantRow,
      flushPendingSegment:_flushPendingSegmentRender,
      liveThinkingPlacement:_liveThinkingPlacement,
      parseStreamState:_parseStreamState,
      resetAssistantSegment:_resetAssistantSegment,
      scheduleRender:_scheduleRender,
      updateLiveThinking:_updateLiveThinkingCard,
    },
    anchor:{
      apply:_applyToAnchor,
      stripReasoningEcho:_stripLiveReasoningEcho,
      upsertReasoning:_upsertAnchorReasoning,
    },
  });

  const _terminalState={
    get streamFinalized(){ return _streamFinalized; },
    set streamFinalized(value){ _streamFinalized=!!value; },
    get terminalStateReached(){ return _terminalStateReached; },
    set terminalStateReached(value){ _terminalStateReached=!!value; },
  };
  const _sessionRecovery=_createSessionRecovery({
    sessionId:activeSid,
    streamId,
    state:S,
    request:api,
    terminalState:_terminalState,
    turn:{
      assistantText:()=>assistantText,
      isActiveSession:_isActiveSession,
    },
    lifecycle:{
      cancelPersist:_cancelThrottledPersistTimer,
      cancelSnapshot:_cancelThrottledSnapshotTimer,
      clearApproval:_clearApprovalForOwner,
      clearClarify:_clearClarifyForOwner,
      clearOwnerInflight:_clearOwnerInflightState,
      closeSource:_closeSource,
      setActivePaneIdle:_setActivePaneIdleIfOwner,
    },
    renderer:{
      cancelPendingRender:_cancelAnimationFramePendingStreamRender,
      cleanupReduceMotion:_streamFadeCleanupReduceMotionListener,
      clearAnchorProseIncrementalNode:_clearAnchorProseIncrementalNode,
      endParser:_smdEndParser,
    },
    anchor:{
      apply:_applyToAnchor,
      attachProjectedScene:_attachProjectedAnchorSceneToLastAssistant,
      flushReasoning:_flushReasoningToAnchor,
      scheduleCleanup:_scheduleAnchorRegistryCleanup,
    },
    transcript:{
      carryForward:_carryForwardEphemeralTurnFields,
      ensureSingleTerminalStreamErrorMarker:_ensureSingleTerminalStreamErrorMarker,
      filterRecoveryControls:_filterRecoveryControlMessages,
      isTerminalStreamErrorMarker:_isTerminalStreamErrorMarkerMessage,
      replaceMarkerOnly:_replaceMarkerOnlyAssistantWithStreamError,
    },
    tools:{
      mergeSettledWithLive:_mergeSettledToolCallsWithLiveMetadata,
    },
  });
  const _restoreSettledSession=_sessionRecovery.restoreSettledSession;
  const _handleStreamError=_sessionRecovery.handleConnectionLost;
  const _terminalEvents=_createTerminalEventOwner({
    sessionId:activeSid,
    streamId,
    state:S,
    uploaded,
    terminalState:_terminalState,
    turn:{
      assistantBody:()=>assistantBody,
      assistantText:()=>assistantText,
      reasoningText:()=>reasoningText,
      splitThinkFromContent:_splitThinkFromContent,
    },
    lifecycle:{
      bailOutOfStaleTerminal:_bailOutOfTerminalEventsFromStaleStream,
      cancelPersist:_cancelThrottledPersistTimer,
      cancelSnapshot:_cancelThrottledSnapshotTimer,
      clearApproval:_clearApprovalForOwner,
      clearClarify:_clearClarifyForOwner,
      clearOwnerInflight:_clearOwnerInflightState,
      clearStreamEndRecovery:_clearStreamEndRecovery,
      closeSource:_closeSource,
      finalizeStreamEndFallback:_finalizeStreamEndFallback,
      liveStreamEndScenePresent:_liveStreamEndScenePresent,
      restoreSettledSession:_restoreSettledSession,
      scheduleStreamEndRecovery:_scheduleStreamEndRecovery,
      setActivePaneIdle:_setActivePaneIdleIfOwner,
    },
    renderer:{
      cancelPendingRender:_cancelAnimationFramePendingStreamRender,
      cleanupReduceMotion:_streamFadeCleanupReduceMotionListener,
      clearAnchorProseIncrementalNode:_clearAnchorProseIncrementalNode,
      drainFadeBeforeDone:_drainStreamFadeBeforeDone,
      endParser:_smdEndParser,
      shouldUseLiveProseFade:_shouldUseLiveProseFade,
      streamDisplay:_streamDisplay,
    },
    anchor:{
      apply:_applyToAnchor,
      attachProjectedScene:_attachProjectedAnchorSceneToLastAssistant,
      flushReasoning:_flushReasoningToAnchor,
      scheduleCleanup:_scheduleAnchorRegistryCleanup,
    },
    transcript:{
      carryForward:_carryForwardEphemeralTurnFields,
      filterRecoveryControls:_filterRecoveryControlMessages,
      isRecoveryControlText:_streamRecoveryControlMessageText,
      replaceMarkerOnly:_replaceMarkerOnlyAssistantWithStreamError,
    },
    tools:{
      mergeSettledWithLive:_mergeSettledToolCallsWithLiveMetadata,
    },
    controlEvents:_controlEvents,
  });
  const _transport=_createTransportOwner({
    sessionId:activeSid,
    streamId,
    state:S,
    request:api,
    terminalState:_terminalState,
    wireSource:_wireSSE,
    restoreSettledSession:_restoreSettledSession,
    handleConnectionLost:_handleStreamError,
    lifecycle:{
      clearApproval:_clearApprovalForOwner,
      clearClarify:_clearClarifyForOwner,
      clearOwnerInflight:_clearOwnerInflightState,
      closeSource:_closeSource,
      isActiveSession:_isActiveSession,
      isStreamEndRecoveryPending:()=>_pendingStreamEndRecovery,
      setActivePaneIdle:_setActivePaneIdleIfOwner,
    },
    anchor:{
      flushReasoning:_flushReasoningToAnchor,
      scheduleCleanup:_scheduleAnchorRegistryCleanup,
    },
    journal:{
      replayParams:_runJournalReplayParams,
    },
  });

  function _wireSSE(source){
    const existingLive=LIVE_STREAMS[activeSid];
    if(existingLive&&existingLive.source&&existingLive.source!==source){
      try{if(existingLive.source.readyState!==2)existingLive.source.close();}catch(_){ }
    }
    LIVE_STREAMS[activeSid]={streamId,source};
    _controlEvents.attach(source);

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

    _contentEvents.attach(source);
    _liveToolTracker.attach(source);

    _compressionEvents.attach(source);
    _terminalEvents.attach(source);
    _transport.attach(source);

    for(const _runJournalEventName of ['token','interim_assistant','reasoning','tool','tool_complete','todo_state','approval','clarify','state_saved','title','title_status','context_status','goal','goal_continue','done','stream_end','pending_steer_leftover','compressing','compressed','metering','apperror','warning','error','cancel']){
      source.addEventListener(_runJournalEventName,_rememberRunJournalCursor);
    }
  }

  // Session loads outside this controller use the same projection owner.
  if(typeof window!=='undefined'){
    window._carryForwardEphemeralTurnFields=_carryForwardEphemeralTurnFields;
  }
  void _transport.start(reconnecting);
}
