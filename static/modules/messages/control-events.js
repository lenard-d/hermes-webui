import { _chatPayloadModelState } from './core.js';

// Owns non-content SSE application for one attached response stream. These
// events update session metadata, queued control intent, and goal continuation
// state; token/tool/terminal projection remains with the live turn owner.
export function createStreamControlEventOwner(options={}){
  const sessionId=String(options.sessionId||'');
  const state=options.state&&typeof options.state==='object'?options.state:{};
  const applyToAnchor=typeof options.applyToAnchor==='function'?options.applyToAnchor:()=>{};
  const modelState=typeof options.modelState==='function'?options.modelState:_chatPayloadModelState;
  const showPersistentStateToast=typeof options.showPersistentStateToast==='function'?options.showPersistentStateToast:()=>{};
  const applySessionTitle=typeof options.applySessionTitle==='function'?options.applySessionTitle:()=>{};
  const handleBackgroundTaskComplete=typeof options.handleBackgroundTaskComplete==='function'?options.handleBackgroundTaskComplete:()=>{};
  const ui=options.ui&&typeof options.ui==='object'?options.ui:{};
  const translate=typeof ui.translate==='function'?ui.translate:(key)=>String(key||'');
  const setComposerStatus=typeof ui.setComposerStatus==='function'?ui.setComposerStatus:()=>{};
  const showToast=typeof ui.showToast==='function'?ui.showToast:()=>{};
  const queueSessionMessage=typeof ui.queueSessionMessage==='function'?ui.queueSessionMessage:null;
  const updateQueueBadge=typeof ui.updateQueueBadge==='function'?ui.updateQueueBadge:()=>{};
  let latestGoalStatus=null;
  let pendingGoalContinuation=null;

  function parsePayload(event){
    try{
      const payload=JSON.parse(event&&event.data||'{}');
      return payload&&typeof payload==='object'?payload:{};
    }catch(_){
      return null;
    }
  }

  function belongsToOwner(payload){
    return !!payload&&String(payload.session_id||sessionId)===sessionId;
  }

  function resolveGoalMessage(payload){
    const key=String(payload&&payload.message_key||'').trim();
    const args=Array.isArray(payload&&payload.message_args)?payload.message_args:[];
    const raw=String(payload&&payload.message||'').trim();
    if(key){
      try{
        const translated=String(translate(key,...args));
        if(translated&&translated!==key) return translated;
      }catch(_){ }
    }
    return raw;
  }

  function attach(source){
    if(!source||typeof source.addEventListener!=='function'){
      throw new TypeError('stream control events require an EventSource-like listener interface');
    }

    source.addEventListener('state_saved',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      if(!state.session||state.session.session_id!==sessionId) return;
      showPersistentStateToast(payload.kind,payload.name||'',{
        created:String(payload.action||'').toLowerCase()==='created',
      });
    });

    source.addEventListener('title',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      applySessionTitle(sessionId,payload.title);
    });

    source.addEventListener('title_status',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      try{
        console.info('[title]',{
          status:String(payload.status||''),
          reason:String(payload.reason||''),
          title:String(payload.title||''),
          raw_preview:String(payload.raw_preview||''),
          session_id:String(payload.session_id||sessionId),
        });
      }catch(_){ }
    });

    source.addEventListener('context_status',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      const prefill=payload.prefill||{};
      const status=String(prefill.status||'not_configured');
      const label=String(prefill.label||'session recall');
      if(status==='loaded'){
        setComposerStatus(`Context loaded: ${label}`);
      }else if(status==='error'){
        setComposerStatus(`Context unavailable: ${label}`);
        showToast(`Context unavailable: ${String(prefill.error||label)}`,3600,'warning');
      }
    });

    source.addEventListener('goal',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      const goalState=String(payload.state||'').trim();
      const goalEvaluatingMessage=translate('goal_evaluating_progress');
      if(goalState==='evaluating'){
        setComposerStatus(goalEvaluatingMessage);
        return;
      }
      const message=resolveGoalMessage(payload);
      if(!message) return;
      latestGoalStatus={
        message,
        decision:payload.decision||null,
        state:goalState||null,
      };
      setComposerStatus(message);
      showToast(message.split('\n')[0],2600);
    });

    source.addEventListener('goal_continue',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      const continuationPrompt=String(payload.continuation_prompt||payload.text||'').trim();
      if(!continuationPrompt) return;
      applyToAnchor('goal_continue',payload,event);
      const resolvedModelState=modelState()||{};
      pendingGoalContinuation={
        sid:sessionId,
        text:continuationPrompt,
        model:resolvedModelState.model,
        model_provider:resolvedModelState.model_provider,
        profile:state.activeProfile||'default',
      };
      const toast=translate('goal_continuing_toast');
      const message=resolveGoalMessage(payload);
      showToast((toast&&message&&message!==toast)?message.split('\n')[0]:toast,2200);
    });

    source.addEventListener('bg_task_complete',event=>{
      handleBackgroundTaskComplete(event,sessionId,{source:'stream'});
    });

    source.addEventListener('pending_steer_leftover',event=>{
      const payload=parsePayload(event);
      if(!belongsToOwner(payload)) return;
      const text=String(payload.text||'').trim();
      if(!text) return;
      applyToAnchor('pending_steer_leftover',payload,event);
      if(!queueSessionMessage) return;
      const resolvedModelState=modelState()||{};
      queueSessionMessage(sessionId,{
        text,
        files:[],
        model:resolvedModelState.model,
        model_provider:resolvedModelState.model_provider,
        profile:state.activeProfile||'default',
      });
      updateQueueBadge(sessionId);
      showToast(translate('steer_leftover_queued'),3000);
    });

    source.addEventListener('warning',event=>{
      if(!state.session||state.session.session_id!==sessionId) return;
      const payload=parsePayload(event);
      if(!payload) return;
      if(payload.type==='approval_gateway_unsupported'){
        showToast(translate('approval_gateway_unsupported_label')||'Approvals not supported',4000,'warning');
        return;
      }
      if(payload.type==='approval_gateway_offline'){
        showToast(payload.message||'Gateway offline',4000,'warning');
        return;
      }
      setComposerStatus(`${payload.message||'Warning'}`);
      if(payload.type==='fallback') setTimeout(()=>setComposerStatus(''),4000);
    });

    return source;
  }

  function latestGoalStatusSnapshot(){
    return latestGoalStatus?{...latestGoalStatus}:null;
  }

  function takeGoalContinuation(){
    const continuation=pendingGoalContinuation;
    pendingGoalContinuation=null;
    return continuation?{...continuation}:null;
  }

  return Object.freeze({
    attach,
    latestGoalStatus:latestGoalStatusSnapshot,
    takeGoalContinuation,
  });
}
