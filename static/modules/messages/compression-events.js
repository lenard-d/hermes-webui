// Owns the automatic context-compression lifecycle for one response stream.
// The owner projects compression events into the active Worklog and usage
// state through explicit ports; it does not depend on browser load order.

export function createStreamCompressionEventOwner(options={}){
  const sessionId=String(options.sessionId||'');
  const state=options.state&&typeof options.state==='object'?options.state:{};
  const applyToAnchor=typeof options.applyToAnchor==='function'?options.applyToAnchor:()=>{};
  const completeAnchorOnLiveProgress=typeof options.completeAnchorOnLiveProgress==='function'
    ? options.completeAnchorOnLiveProgress
    : ()=>{};
  const snapshot=typeof options.snapshot==='function'?options.snapshot:()=>{};
  const mergeUsage=typeof options.mergeUsage==='function'
    ? options.mergeUsage
    : (next,previous)=>({...previous,...next});
  const syncUsage=typeof options.syncUsage==='function'?options.syncUsage:()=>{};
  const now=typeof options.now==='function'?options.now:()=>Date.now()/1000;
  const view=options.view&&typeof options.view==='object'?options.view:{};
  const hasRunningCard=typeof view.hasRunningCard==='function'?view.hasRunningCard:()=>false;
  const getCompressionState=typeof view.getCompressionState==='function'?view.getCompressionState:()=>null;
  const appendLiveCard=typeof view.appendLiveCard==='function'?view.appendLiveCard:()=>false;
  const clearCompressionUi=typeof view.clearCompressionUi==='function'?view.clearCompressionUi:()=>{};
  const setCompressionUi=typeof view.setCompressionUi==='function'?view.setCompressionUi:()=>{};
  const setCompressionSessionLock=typeof view.setCompressionSessionLock==='function'?view.setCompressionSessionLock:()=>{};
  const renderMessages=typeof view.renderMessages==='function'?view.renderMessages:()=>{};

  function parsePayload(event){
    try{
      const payload=JSON.parse(event&&event.data||'{}');
      return payload&&typeof payload==='object'?payload:{};
    }catch(_){
      return null;
    }
  }

  function completeOnLiveProgress(candidateSessionId=sessionId){
    const sid=String(candidateSessionId||'');
    const compressionState=getCompressionState();
    const runningState=!!(
      compressionState&&
      compressionState.automatic&&
      compressionState.phase==='running'&&
      (!sid||!compressionState.sessionId||String(compressionState.sessionId)===sid)
    );
    if(!hasRunningCard()&&!runningState) return false;
    completeAnchorOnLiveProgress(sid);
    appendLiveCard({
      sessionId:sid,
      phase:'done',
      automatic:true,
      message:'Context auto-compressed',
    });
    return true;
  }

  function attach(source){
    if(!source||typeof source.addEventListener!=='function'){
      throw new TypeError('compression events require an EventSource-like listener interface');
    }

    source.addEventListener('compressing',event=>{
      if(!state.session||state.session.session_id!==sessionId) return;
      const payload=parsePayload(event);
      if(!payload) return;
      if(payload.session_id&&payload.session_id!==sessionId) return;
      applyToAnchor('compressing',payload,event);
      const compressionState={
        sessionId,
        phase:'running',
        automatic:true,
        message:'Compressing context',
        startedAt:now(),
      };
      if(appendLiveCard(compressionState)){
        clearCompressionUi();
        snapshot();
        return;
      }
      setCompressionUi(compressionState);
      snapshot();
    });

    source.addEventListener('compressed',event=>{
      if(!state.session) return;
      const currentSid=String(state.session.session_id||'');
      const payload=parsePayload(event);
      if(!payload) return;
      const eventSid=String(payload.old_session_id||payload.session_id||sessionId);
      const continuationSid=String(payload.new_session_id||payload.continuation_session_id||'');
      const eventMatchesCurrent=!!(
        currentSid&&(
          eventSid===currentSid||
          String(payload.new_session_id||'')===currentSid||
          String(payload.continuation_session_id||'')===currentSid
        )
      );
      if(!eventMatchesCurrent) return;
      applyToAnchor('compressed',payload,event);
      if(payload.usage){
        state.lastUsage=mergeUsage(payload.usage,state.lastUsage||{});
        syncUsage(state.lastUsage);
      }
      appendLiveCard({
        sessionId:currentSid,
        phase:'done',
        automatic:true,
        message:'Context auto-compressed',
        continuationSessionId:continuationSid,
      });
      clearCompressionUi();
      setCompressionSessionLock(null);
      if(!state.busy) renderMessages();
    });

    return source;
  }

  return Object.freeze({
    attach,
    completeOnLiveProgress,
  });
}
