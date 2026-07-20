function _afterSessionFirstPaint(fn, delayMs=0){
  return new Promise((resolve)=>{
    const invoke=()=>{
      try{ resolve(typeof fn==='function' ? fn() : undefined); }
      catch(_){ resolve(undefined); }
    };
    const run=()=>{
      if(typeof requestIdleCallback==='function'){
        requestIdleCallback(invoke,{timeout:1500});
      }else{
        setTimeout(invoke, delayMs);
      }
    };
    if(typeof requestAnimationFrame==='function'){
      requestAnimationFrame(()=>requestAnimationFrame(run));
    }else{
      setTimeout(run, delayMs);
    }
  });
}

function _deferSessionSideEffect(sid, fn, delayMs=0){
  if(!sid||typeof fn!=='function') return Promise.resolve();
  return _afterSessionFirstPaint(()=>{
    if(!S.session||S.session.session_id!==sid) return undefined;
    return fn();
  },delayMs);
}

function _deferWorkspaceRefreshForSession(sid, opts={}){
  _deferSessionSideEffect(sid,()=>{
    const load=loadDir('.', opts);
    if(load&&typeof load.catch==='function') load.catch(()=>{});
  },150);
}

function _resolveSessionModelForDisplaySoon(sid){
  if(!sid) return;
  _deferSessionSideEffect(sid,async()=>{
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=1`);
      const model=data&&data.session&&data.session.model;
      const provider=data&&data.session&&data.session.model_provider;
      if(!model||!S.session||S.session.session_id!==sid) return;
      S.session.model=model;
      S.session.model_provider=provider||null;
      const resolvedContextLength=data.session.context_length||S.session.context_length||0;
      S.session.context_length=resolvedContextLength;
      S.session.threshold_tokens=data.session.threshold_tokens||0;
      S.session.last_prompt_tokens=data.session.last_prompt_tokens||0;
      S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;
      S.session._modelResolutionDeferred=false;
      syncTopbar();
      if(typeof _syncCtxIndicator==='function'){
        const u=S.lastUsage||{};
        const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
        _syncCtxIndicator({
          input_tokens:_pick(u.input_tokens,S.session.input_tokens),
          output_tokens:_pick(u.output_tokens,S.session.output_tokens),
          estimated_cost:_pick(u.estimated_cost,S.session.estimated_cost),
          cache_read_tokens:_pick(u.cache_read_tokens,S.session.cache_read_tokens),
          cache_write_tokens:_pick(u.cache_write_tokens,S.session.cache_write_tokens),
          cache_hit_percent:_pick(u.cache_hit_percent,S.session.cache_hit_percent,null),
          context_length:resolvedContextLength||u.context_length||0,
          last_prompt_tokens:_pick(u.last_prompt_tokens,S.session.last_prompt_tokens),
          post_compression_context_tokens_estimate:S.session.post_compression_context_tokens_estimate,
          threshold_tokens:data.session.threshold_tokens||0,
        });
      }
    }catch(_){
      // Keep session switching non-blocking; the next load can try again.
    }
  },0);
}

export const sessionPostLoad=Object.freeze({defer:_deferSessionSideEffect,refreshWorkspace:_deferWorkspaceRefreshForSession,resolveModel:_resolveSessionModelForDisplaySoon});

export { _deferWorkspaceRefreshForSession, _resolveSessionModelForDisplaySoon };
