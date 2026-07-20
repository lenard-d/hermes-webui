function _looksLikeVersionedModel(query){
  return /\d$/.test(String(query||''));
}

function _buildModelCandidates(sel,groups){
  const options=[];
  const providerMap={};
  if(Array.isArray(groups)&&groups.length){
    for(const g of groups){
      const providerId=(g&&g.provider_id)||'';
      const push=m=>{
        if(!m||!m.id)return;
        options.push({value:m.id,textContent:m.label||m.id});
        if(providerId&&!(m.id in providerMap))providerMap[m.id]=providerId;
      };
      for(const m of (Array.isArray(g.models)?g.models:[]))push(m);
      for(const m of (Array.isArray(g.extra_models)?g.extra_models:[]))push(m);
    }
  }
  if(!options.length&&sel){
    for(const option of Array.from(sel.options||[])){
      options.push({value:option.value,textContent:option.textContent});
    }
  }
  return {options,providerMap};
}

function _bestModelMatch(options,query){
  let best=null;
  const versioned=_looksLikeVersionedModel(query);
  for(const option of options){
    const value=option.value.toLowerCase();
    const text=option.textContent.toLowerCase();
    if(value===query||text===query)return option.value;
    if(!value.includes(query)&&!text.includes(query))continue;
    if(versioned){
      const idx=value.indexOf(query);
      const after=idx>=0?value.charAt(idx+query.length):'';
      if(after&&after!=='.'&&!/\d/.test(after))continue;
    }
    if(best===null||option.value.length<best.length)best=option.value;
  }
  return best;
}

function _nearestModelSuggestion(options,query){
  let suggestion='';
  for(const option of options){
    if(!option.value.toLowerCase().includes(query))continue;
    if(!suggestion||option.value.length<suggestion.length)suggestion=option.value;
  }
  return suggestion;
}

async function cmdModel(args){
  if(!args){showToast(t('model_usage'));return;}
  const sel=$('modelSelect');
  if(!sel)return;
  let q=args.toLowerCase();
  let modelsData=null;
  try{
    const response=await fetch(new URL('api/models',document.baseURI||location.href).href);
    if(response.ok){
      modelsData=await response.json();
      for(const [alias,modelId] of Object.entries(modelsData.aliases||{})){
        if(alias.toLowerCase()===q){
          q=modelId.toLowerCase();
          break;
        }
      }
    }
  }catch(_){/* Catalog enrichment is optional; the live picker remains usable. */}

  const {options:candidates,providerMap}=_buildModelCandidates(sel,modelsData&&modelsData.groups);
  const preferred=(S&&S.session&&S.session.model_provider)||window._activeProvider||null;
  let match=(typeof _findModelInDropdown==='function')
    ?_findModelInDropdown(q,sel,preferred)
    :null;
  if(!match)match=_bestModelMatch(candidates,q);

  if(!match&&q.includes('/')){
    const bare=q.slice(q.lastIndexOf('/')+1);
    match=_bestModelMatch(candidates,bare);
    const nearSuggestion=_nearestModelSuggestion(candidates,q)||_nearestModelSuggestion(candidates,bare);
    const versionedNoSnap=_looksLikeVersionedModel(bare)&&nearSuggestion;
    // Cross-provider fallback: update a genuinely off-catalog provider directly,
    // but never snap a complete versioned id to a longer paid/tier variant.
    if(!match&&!versionedNoSnap&&S&&S.session&&S.session.session_id){
      const provider=q.slice(0,q.indexOf('/'));
      try{
        const response=await fetch(new URL('api/session/update',document.baseURI||location.href).href,{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            session_id:S.session.session_id,
            model:q,
            model_provider:provider,
          }),
        });
        if(response.ok){
          S.session.model=q;
          S.session.model_provider=provider;
          if(typeof syncTopbar==='function')syncTopbar();
          showToast(t('switched_to')+q);
          return;
        }
      }catch(_){/* The normal no-match response below remains authoritative. */}
    }
  }

  if(!match){
    let message=t('no_model_match')+`${args}"`;
    const suggestion=_nearestModelSuggestion(candidates,q);
    if(suggestion)message+=t('model_did_you_mean', suggestion);
    showToast(message);
    return;
  }

  const hasOption=Array.from(sel.options||[]).some(option=>option.value===match);
  if(!hasOption&&typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(match,sel,providerMap[match]||null);
  }else{
    sel.value=match;
  }
  await sel.onchange();
  showToast(t('switched_to')+match);
}

export {
  _bestModelMatch,
  _buildModelCandidates,
  _looksLikeVersionedModel,
  _nearestModelSuggestion,
  cmdModel,
};
