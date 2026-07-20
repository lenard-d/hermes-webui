import {
  _deferStreamErrorIfOffline,
  _isSessionCurrentPane,
} from './core.js';

// EventSource ownership includes reconnect probes, hidden-tab deferral, replay
// URLs, and dead-stream preflight. Keeping them together prevents competing
// recovery paths from settling or reopening the same transport.
export function createStreamTransportOwner(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const api=options.request;
  const _terminalState=options.terminalState&&typeof options.terminalState==='object'
    ? options.terminalState
    : {streamFinalized:false,terminalStateReached:false};
  const lifecycle=options.lifecycle&&typeof options.lifecycle==='object'?options.lifecycle:{};
  const anchor=options.anchor&&typeof options.anchor==='object'?options.anchor:{};
  const journal=options.journal&&typeof options.journal==='object'?options.journal:{};
  const _wireSource=options.wireSource;
  const _restoreSettledSession=options.restoreSettledSession;
  const _handleStreamError=options.handleConnectionLost;
  const _closeSource=lifecycle.closeSource;
  const _clearOwnerInflightState=lifecycle.clearOwnerInflight;
  const _clearApprovalForOwner=lifecycle.clearApproval;
  const _clearClarifyForOwner=lifecycle.clearClarify;
  const _setActivePaneIdleIfOwner=lifecycle.setActivePaneIdle;
  const _isActiveSession=lifecycle.isActiveSession;
  const _isStreamEndRecoveryPending=lifecycle.isStreamEndRecoveryPending;
  const _flushReasoningToAnchor=anchor.flushReasoning;
  const _scheduleAnchorRegistryCleanup=anchor.scheduleCleanup;
  const _runJournalReplayParams=journal.replayParams;
  let _reconnectAttempted=false;
  let _deferredStreamRecoveryBound=false;

  function _pageHiddenForStreamError(){
    return (typeof document!=='undefined'&&document.visibilityState==='hidden')||
      (typeof document!=='undefined'&&document.wasDiscarded===true);
  }
  
  function _reattachOrRestoreAfterDeferredStreamError(source){
    if(_terminalState.terminalStateReached||_terminalState.streamFinalized) return;
    if((S.session&&S.session.session_id)!==activeSid) return;
    (async()=>{
      try{
        if(streamId){
          const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
          if(st.active){
            setComposerStatus('Reconnected');
            _wireSource(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
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
  
  
  function attach(source){
    source.addEventListener('error',async e=>{
      if(_bailOutOfTerminalEventsFromStaleStream(source) && !_terminalState.streamFinalized){
        return;
      }
      if(_terminalState.terminalStateReached || _terminalState.streamFinalized){
        _closeSource(source);
        return;
      }
      // #3885: if a stream_end recovery is in flight, don't start a competing
      // reconnect — recovery polls server state and owns the terminal decision
      // (else its exhaustion could mute a freshly reconnected stream). Opus stage-LK.
      if(_isStreamEndRecoveryPending()){
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
      if(_terminalState.terminalStateReached || _terminalState.streamFinalized){
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
          if(_terminalState.terminalStateReached || _terminalState.streamFinalized) return;
          if(!_isSessionCurrentPane(activeSid)) return;
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st&&st.active){
              setComposerStatus('Reconnected');
              _wireSource(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
            if(st&&st.replay_available){
              setComposerStatus('Restoring stream…');
              _wireSource(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
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
            if(!_terminalState.terminalStateReached&&!_terminalState.streamFinalized){
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
          if(_terminalState.terminalStateReached||_terminalState.streamFinalized) return;
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
    
    
  }

  async function start(reconnecting=false){
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
    _wireSource(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${replayParams}`,document.baseURI||location.href).href,{withCredentials:true}));
    
  }

  return Object.freeze({attach,start});
}
