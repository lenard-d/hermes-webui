import {
  _isSessionActivelyViewed,
  _isSessionCurrentPane,
  _markSessionViewed,
} from './core.js';
import {
  _clearStreamHidden,
  _clearStreamNotificationBackground,
  _shouldForceCompletionNotification,
} from './stream-lifecycle.js';
import {
  _completionNotificationPreviewText,
  playNotificationSound,
  sendBrowserNotification,
} from './notifications.js';

// Terminal frames settle exactly one owned assistant turn. This owner keeps
// completion, application-error, cancel, and stream-end cleanup on the same
// terminal-state object so every exit observes the same finalization decision.
export function createStreamTerminalEventOwner(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const _uploaded=Array.isArray(options.uploaded)?options.uploaded:[];
  const _terminalState=options.terminalState&&typeof options.terminalState==='object'
    ? options.terminalState
    : {streamFinalized:false,terminalStateReached:false};
  const turn=options.turn&&typeof options.turn==='object'?options.turn:{};
  const lifecycle=options.lifecycle&&typeof options.lifecycle==='object'?options.lifecycle:{};
  const renderer=options.renderer&&typeof options.renderer==='object'?options.renderer:{};
  const anchor=options.anchor&&typeof options.anchor==='object'?options.anchor:{};
  const transcript=options.transcript&&typeof options.transcript==='object'?options.transcript:{};
  const tools=options.tools&&typeof options.tools==='object'?options.tools:{};
  const controlEvents=options.controlEvents&&typeof options.controlEvents==='object'?options.controlEvents:{};

  const _assistantBody=turn.assistantBody;
  const _assistantText=turn.assistantText;
  const _reasoningText=turn.reasoningText;
  const _splitThinkFromContent=turn.splitThinkFromContent;
  const _clearStreamEndRecovery=lifecycle.clearStreamEndRecovery;
  const _bailOutOfTerminalEventsFromStaleStream=lifecycle.bailOutOfStaleTerminal;
  const _cancelThrottledPersistTimer=lifecycle.cancelPersist;
  const _cancelThrottledSnapshotTimer=lifecycle.cancelSnapshot;
  const _clearOwnerInflightState=lifecycle.clearOwnerInflight;
  const _clearApprovalForOwner=lifecycle.clearApproval;
  const _clearClarifyForOwner=lifecycle.clearClarify;
  const _setActivePaneIdleIfOwner=lifecycle.setActivePaneIdle;
  const _closeSource=lifecycle.closeSource;
  const _liveStreamEndScenePresent=lifecycle.liveStreamEndScenePresent;
  const _scheduleStreamEndRecovery=lifecycle.scheduleStreamEndRecovery;
  const _restoreSettledSession=lifecycle.restoreSettledSession;
  const _finalizeStreamEndFallback=lifecycle.finalizeStreamEndFallback;
  const _cancelAnimationFramePendingStreamRender=renderer.cancelPendingRender;
  const _streamFadeCleanupReduceMotionListener=renderer.cleanupReduceMotion;
  const _smdEndParser=renderer.endParser;
  const _clearAnchorProseIncrementalNode=renderer.clearAnchorProseIncrementalNode;
  const _shouldUseLiveProseFade=renderer.shouldUseLiveProseFade;
  const _drainStreamFadeBeforeDone=renderer.drainFadeBeforeDone;
  const _streamDisplay=renderer.streamDisplay;
  const _flushReasoningToAnchor=anchor.flushReasoning;
  const _applyToAnchor=anchor.apply;
  const _scheduleAnchorRegistryCleanup=anchor.scheduleCleanup;
  const _attachProjectedAnchorSceneToLastAssistant=anchor.attachProjectedScene;
  const _carryForwardEphemeralTurnFields=transcript.carryForward;
  const _filterRecoveryControlMessages=transcript.filterRecoveryControls;
  const _replaceMarkerOnlyAssistantWithStreamError=transcript.replaceMarkerOnly;
  const _streamRecoveryControlMessageText=transcript.isRecoveryControlText;
  const _mergeSettledToolCallsWithLiveMetadata=tools.mergeSettledWithLive;
  const _controlEvents=controlEvents;

  function attach(source){
    source.addEventListener('done',e=>{
      if(_terminalState.streamFinalized) return;
      _clearStreamEndRecovery();
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      // Set _terminalState.streamFinalized IMMEDIATELY — before any fade delay. Without this,
      // a stream_end event arriving during the fade window sees
      // _terminalState.streamFinalized=false, calls _restoreSettledSession(), and overwrites
      // S.messages with stale server data (issue #3195).
      _terminalState.streamFinalized=true;
      _terminalState.terminalStateReached=true;
      _cancelThrottledPersistTimer();
      _cancelThrottledSnapshotTimer();
      const _doneData=JSON.parse(e.data);
      const _doneEvent=e;
      const _finishDone=()=>{
        // Bug A fix: cancel any pending rAF and mark stream finalized before
        // the DOM is settled by renderMessages, so no trailing token/reasoning rAF
        // can reintroduce a stale thinking card or duplicate content.
        _terminalState.streamFinalized=true;
        _cancelAnimationFramePendingStreamRender();
        _streamFadeCleanupReduceMotionListener();
        if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
        // Finalize smd parser — flushes any remaining buffered markdown state
        // and runs Prism + copy buttons on the live segment before the DOM is replaced
        if(_assistantBody()){
          const _finBody=_assistantBody();
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
          if(_reasoningText()&&lastAsst&&!lastAsst.reasoning) lastAsst.reasoning=_reasoningText();
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
          if(_uploaded.length){
            const lastUser=[...S.messages].reverse().find(m=>m.role==='user');
            if(lastUser)lastUser.attachments=_uploaded;
          }
          const _latestGoalStatus=_controlEvents.latestGoalStatus();
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
          if(!S.messages.some(m=>m.role==='assistant'&&String(m.content||'').trim())&&!_assistantText()){removeThinking();S.messages.push({role:'assistant',content:'**No response received.** Check your API key and model selection.'});}
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
        const _goalNext=isActiveSession?_controlEvents.takeGoalContinuation():null;
        if(_goalNext&&typeof queueSessionMessage==='function'){
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
          liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():_assistantText(),
        });
        sendBrowserNotification('Response complete',_completionPreview||'Task finished',{forceHidden:_wasEverBackgrounded,sid:activeSid});
      };
      if(_shouldUseLiveProseFade()&&_assistantBody()){
        _cancelAnimationFramePendingStreamRender();
        _drainStreamFadeBeforeDone(_finishDone);
        return;
      }
      _finishDone();
    });

    source.addEventListener('stream_end',async e=>{
      if(_terminalState.streamFinalized){
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
      _terminalState.terminalStateReached=true;
      _cancelThrottledPersistTimer();
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _terminalState.streamFinalized=true;
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
        clearLiveToolCards();if(!_assistantText())removeThinking();
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

    source.addEventListener('cancel',e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source)) return;
      _clearStreamEndRecovery();
      _terminalState.terminalStateReached=true;
      _cancelThrottledPersistTimer();
      _cancelThrottledSnapshotTimer();
      _clearAnchorProseIncrementalNode();
      _terminalState.streamFinalized=true;
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
        clearLiveToolCards();if(!_assistantText())removeThinking();
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
            clearLiveToolCards();if(!_assistantText())removeThinking();
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


  }

  return Object.freeze({attach});
}
