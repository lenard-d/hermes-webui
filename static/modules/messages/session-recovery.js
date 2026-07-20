import {
  _isSessionActivelyViewed,
  _isSessionCurrentPane,
  _markSessionViewed,
} from './core.js';

// Recovery converges a disconnected live turn on one canonical session
// snapshot. It owns transcript preservation, stale-owner rejection, and the
// explicit connection-lost projection used when no settled snapshot exists.
export function createStreamSessionRecovery(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const api=options.request;
  const _terminalState=options.terminalState&&typeof options.terminalState==='object'
    ? options.terminalState
    : {streamFinalized:false};
  const turn=options.turn&&typeof options.turn==='object'?options.turn:{};
  const lifecycle=options.lifecycle&&typeof options.lifecycle==='object'?options.lifecycle:{};
  const renderer=options.renderer&&typeof options.renderer==='object'?options.renderer:{};
  const anchor=options.anchor&&typeof options.anchor==='object'?options.anchor:{};
  const transcript=options.transcript&&typeof options.transcript==='object'?options.transcript:{};
  const tools=options.tools&&typeof options.tools==='object'?options.tools:{};

  const _assistantText=turn.assistantText;
  const _isActiveSession=turn.isActiveSession;
  const _cancelThrottledPersistTimer=lifecycle.cancelPersist;
  const _cancelThrottledSnapshotTimer=lifecycle.cancelSnapshot;
  const _clearOwnerInflightState=lifecycle.clearOwnerInflight;
  const _closeSource=lifecycle.closeSource;
  const _clearApprovalForOwner=lifecycle.clearApproval;
  const _clearClarifyForOwner=lifecycle.clearClarify;
  const _setActivePaneIdleIfOwner=lifecycle.setActivePaneIdle;
  const _cancelAnimationFramePendingStreamRender=renderer.cancelPendingRender;
  const _streamFadeCleanupReduceMotionListener=renderer.cleanupReduceMotion;
  const _smdEndParser=renderer.endParser;
  const _clearAnchorProseIncrementalNode=renderer.clearAnchorProseIncrementalNode;
  const _flushReasoningToAnchor=anchor.flushReasoning;
  const _scheduleAnchorRegistryCleanup=anchor.scheduleCleanup;
  const _applyToAnchor=anchor.apply;
  const _attachProjectedAnchorSceneToLastAssistant=anchor.attachProjectedScene;
  const _carryForwardEphemeralTurnFields=transcript.carryForward;
  const _filterRecoveryControlMessages=transcript.filterRecoveryControls;
  const _isTerminalStreamErrorMarkerMessage=transcript.isTerminalStreamErrorMarker;
  const _ensureSingleTerminalStreamErrorMarker=transcript.ensureSingleTerminalStreamErrorMarker;
  const _replaceMarkerOnlyAssistantWithStreamError=transcript.replaceMarkerOnly;
  const _mergeSettledToolCallsWithLiveMetadata=tools.mergeSettledWithLive;

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
      if(_terminalState.streamFinalized) return returnStatus?'restored':true;
      const session=data&&data.session;
      if(!session) return returnStatus?'missing':false;
      if(session.active_stream_id||session.pending_user_message) return returnStatus?'active':false;
      _cancelThrottledPersistTimer();
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _terminalState.streamFinalized=true;
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
        clearLiveToolCards();if(!_assistantText())removeThinking();
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
    _cancelThrottledPersistTimer();
    _cancelThrottledSnapshotTimer();
    _clearAnchorProseIncrementalNode();
    _terminalState.streamFinalized=true;
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
      clearLiveToolCards();if(!_assistantText())removeThinking();
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
  
  
  return Object.freeze({
    handleConnectionLost:_handleStreamError,
    restoreSettledSession:_restoreSettledSession,
  });
}
