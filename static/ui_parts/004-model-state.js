// ── Smart model resolver ────────────────────────────────────────────────────
// Finds the best matching option value in a <select> for a given model ID.
// Handles mismatches like 'claude-sonnet-4-6' vs 'anthropic/claude-sonnet-4.6'.
// When a preferred provider is supplied, duplicate normalized IDs prefer that
// provider's option so Settings/profile rehydration doesn't snap back to the
// first colliding entry.
function _getOptionProviderId(opt){
  if(!opt) return '';
  if(opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  const group=opt.parentElement;
  if(group && group.tagName==='OPTGROUP' && group.dataset && group.dataset.provider){
    return group.dataset.provider;
  }
  const value=String(opt.value||'');
  if(value.startsWith('@') && value.includes(':')) return value.slice(1,value.lastIndexOf(':'));
  return '';
}
function _providerFromModelValue(modelId){
  const value=String(modelId||'').trim();
  if(value.startsWith('@')&&value.includes(':')) return value.slice(1,value.lastIndexOf(':'));
  return '';
}
function _modelPickerOptionIdentity(modelId, providerId){
  let value=String(modelId||'');
  const provider=String(providerId||'').trim();
  if(value.startsWith('@')&&value.includes(':')){
    const exactPrefix=provider ? `@${provider}:` : '';
    if(exactPrefix && value.toLowerCase().startsWith(exactPrefix.toLowerCase())){
      value=value.substring(exactPrefix.length);
    }else if(value.startsWith('@custom:')){
      const namedProvider=value.substring('@custom:'.length);
      const splitAt=namedProvider.indexOf(':');
      value=splitAt>=0 ? namedProvider.substring(splitAt+1) : namedProvider;
    }else{
      value=value.substring(value.indexOf(':')+1);
    }
  }
  return value.replace(/-/g,'.').toLowerCase();
}
function _deduplicateModelPickerOptions(sel,selectedValue){
  if(!sel||!sel.querySelectorAll) return 0;
  let removed=0;
  for(const group of sel.querySelectorAll('optgroup')){
    const options=Array.from(group.children||[]).filter(opt=>opt&&opt.tagName==='OPTION');
    const byIdentity=new Map();
    for(const opt of options){
      const identity=_modelPickerOptionIdentity(opt.value,_getOptionProviderId(opt));
      if(!identity) continue;
      if(!byIdentity.has(identity)) byIdentity.set(identity,[]);
      byIdentity.get(identity).push(opt);
    }
    for(const candidates of byIdentity.values()){
      if(candidates.length<2) continue;
      const selected=candidates.find(opt=>opt.value===selectedValue);
      const routable=candidates.find(opt=>String(opt.value||'').startsWith('@'));
      const survivor=selected||routable||candidates[0];
      for(const opt of candidates){
        if(opt===survivor) continue;
        group.removeChild(opt);
        removed++;
      }
    }
  }
  return removed;
}
function _providerSkipsModelMismatchWarning(providerId){
  const p=String(providerId||'').toLowerCase();
  return !p||p==='custom'||p.startsWith('custom:')||p==='openrouter';
}
function _providerDefersMissingModelFallback(providerId){
  const p=String(providerId||'').toLowerCase();
  // Named custom providers and OpenRouter can legitimately route vendor-prefixed
  // model IDs that are not present in the current static catalog. Do not
  // silently rewrite those sessions to the default just because the option has
  // not been hydrated yet (#2405).
  return p.startsWith('custom:')||p==='openrouter';
}
function _modelStateForSelect(sel, modelId){
  const value=String(modelId||'').trim();
  if(!value) return {model:'',model_provider:null};
  const explicitProvider=_providerFromModelValue(value);
  if(explicitProvider){
    const selected=sel&&sel.options
      ?Array.from(sel.options).find(o=>String(o.value||'')===value)
      :null;
    const routedModel=selected&&selected.dataset&&selected.dataset.model;
    // Read the provider from the matched option's authoritative data-provider
    // rather than re-parsing the value at its LAST colon: a colon-bearing model
    // id (e.g. model-a:free) synthesized as @custom:backup:model-a:free would
    // otherwise mis-parse to provider "custom:backup:model-a" (#6221 re-gate).
    const routedProvider=selected?String(_getOptionProviderId(selected)||'').trim():'';
    return {model:routedModel||value,model_provider:routedProvider||explicitProvider};
  }
  // Resolve the provider from the option whose VALUE matches the requested
  // model — never blindly from sel.selectedOptions[0] (#5567). During a profile
  // /tab switch or a model-list rebuild the dropdown transiently still has the
  // PREVIOUS profile's default option selected (e.g. an ollama model), so reading
  // selectedOptions[0] would stamp that foreign provider onto a model it doesn't
  // own — which is then persisted into the session's model_provider and re-sent
  // on every turn, bricking it with a "Provider 'X'…no API key" error for a
  // provider the session never used.
  let opt=null;
  const selected=sel&&sel.selectedOptions&&sel.selectedOptions[0];
  // Prefer the currently-selected option ONLY when it actually is the requested
  // model — this preserves the user's exact pick in the same-value/different-
  // provider collision case (two providers offering the same model id).
  if(selected&&String(selected.value||'')===value){
    opt=selected;
  }else if(sel&&sel.options){
    opt=Array.from(sel.options).find(o=>String(o.value||'')===value)||null;
  }
  const provider=String(_getOptionProviderId(opt)||'').trim();
  return {model:value,model_provider:(provider&&provider!=='default')?provider:null};
}
function _captureModelDropdownSelection(sel){
  if(!sel||!sel.value) return null;
  try{
    const state=_modelStateForSelect(sel,sel.value);
    if(state&&state.model) return state;
  }catch(_){}
  return {model:String(sel.value||''),model_provider:null};
}
function _modelProviderForSend(modelId){
  const sessionProvider=(S&&S.session&&S.session.model_provider)||null;
  if(sessionProvider) return sessionProvider;
  const model=String(modelId||'').trim();
  if(!model) return null;
  const explicitProvider=typeof _providerFromModelValue==='function'
    ? _providerFromModelValue(model)
    : '';
  if(explicitProvider) return explicitProvider;
  const sel=typeof $==='function' ? $('modelSelect') : null;
  if(sel&&String(sel.value||'').trim()===model&&typeof _modelStateForSelect==='function'){
    try{
      const dropdownState=_modelStateForSelect(sel,sel.value);
      if(dropdownState&&String(dropdownState.model||'').trim()===model){
        return dropdownState.model_provider||null;
      }
    }catch(_){}
  }
  if(typeof _readPersistedModelState==='function'){
    try{
      const persisted=_readPersistedModelState();
      if(persisted&&String(persisted.model||'').trim()===model){
        return persisted.model_provider||null;
      }
    }catch(_){}
  }
  return null;
}
function _reconcileModelDropdownSelection(sel,data,previousState,opts){
  if(!sel) return null;
  const activeSession=(typeof S!=='undefined'&&S&&S.session)?S.session:null;
  // Fresh boot is the only path where the profile/server default intentionally
  // beats a browser-persisted or static fallback value. Every other model-list
  // rebuild should preserve the loaded session model or the user's current
  // in-page selection when it still exists in the refreshed catalog.
  const shouldApplyBootDefault=!!(opts&&opts.preferProfileDefaultOnFreshBoot);

  // Helper: apply the requested model, but if it is missing from the current
  // catalog (cross-provider selection after a partial/timed-out rebuild), inject
  // it as a custom option instead of returning null and letting the browser
  // silently snap to the first <option>. _ensureModelOptionInDropdown already
  // tries _applyModelToDropdown first, so delegate to it (single scan) and keep
  // the plain-apply fallback for the unlikely case it is unavailable.
  const _applyOrEnsure = function(modelId, providerId) {
    if (typeof _ensureModelOptionInDropdown === 'function') {
      return _ensureModelOptionInDropdown(modelId, sel, providerId);
    }
    return _applyModelToDropdown(modelId, sel, providerId);
  };

  if(shouldApplyBootDefault && data&&data.default_model && !(activeSession&&activeSession.model)){
    return _applyOrEnsure(data.default_model, data.active_provider||null);
  }
  if(activeSession&&activeSession.model){
    return _applyOrEnsure(activeSession.model, activeSession.model_provider||null);
  }
  if(previousState&&previousState.model){
    return _applyOrEnsure(previousState.model, previousState.model_provider||null);
  }
  return null;
}
function _providerQualifiedModelValueForSelect(sel, modelId){
  return _modelStateForSelect(sel,modelId).model;
}
function _readPersistedModelState(){
  try{
    const raw=localStorage.getItem(MODEL_STATE_KEY);
    if(raw){
      const parsed=JSON.parse(raw);
      if(parsed&&parsed.model){
        return {
          model:String(parsed.model||''),
          model_provider:parsed.model_provider?String(parsed.model_provider):(_providerFromModelValue(parsed.model)||null),
        };
      }
    }
  }catch(_){}
  const legacy=localStorage.getItem('hermes-webui-model');
  if(!legacy) return null;
  return {model:legacy,model_provider:_providerFromModelValue(legacy)||null};
}
function _writePersistedModelState(model, modelProvider){
  const value=String(model||'').trim();
  const provider=modelProvider?String(modelProvider).trim():(_providerFromModelValue(value)||null);
  if(!value){
    localStorage.removeItem('hermes-webui-model');
    localStorage.removeItem(MODEL_STATE_KEY);
    return;
  }
  localStorage.setItem('hermes-webui-model', value);
  try{
    localStorage.setItem(MODEL_STATE_KEY, JSON.stringify({model:value,model_provider:provider||null}));
  }catch(_){}
}
function _clearPersistedModelState(){
  localStorage.removeItem('hermes-webui-model');
  localStorage.removeItem(MODEL_STATE_KEY);
}
function _pendingSessionModelKey(sessionId){
  return PENDING_SESSION_MODEL_PREFIX+String(sessionId||'');
}
function _rememberPendingSessionModel(sessionId, model, modelProvider){
  const sid=String(sessionId||'').trim();
  const value=String(model||'').trim();
  if(!sid||!value) return;
  const provider=modelProvider?String(modelProvider).trim():(_providerFromModelValue(value)||null);
  try{
    sessionStorage.setItem(_pendingSessionModelKey(sid), JSON.stringify({
      model:value,
      model_provider:provider||null,
      saved_at:Date.now(),
    }));
  }catch(_){}
}
function _readPendingSessionModel(sessionId){
  const sid=String(sessionId||'').trim();
  if(!sid) return null;
  try{
    const raw=sessionStorage.getItem(_pendingSessionModelKey(sid));
    if(!raw) return null;
    const parsed=JSON.parse(raw);
    const model=String(parsed&&parsed.model||'').trim();
    if(!model){
      sessionStorage.removeItem(_pendingSessionModelKey(sid));
      return null;
    }
    const savedAt=Number(parsed.saved_at||0);
    if(savedAt&&Date.now()-savedAt>PENDING_SESSION_MODEL_MAX_AGE_MS){
      sessionStorage.removeItem(_pendingSessionModelKey(sid));
      return null;
    }
    return {
      model,
      model_provider:parsed&&parsed.model_provider?String(parsed.model_provider):(_providerFromModelValue(model)||null),
    };
  }catch(_){
    try{sessionStorage.removeItem(_pendingSessionModelKey(sid));}catch(__){}
    return null;
  }
}
function _clearPendingSessionModel(sessionId){
  const sid=String(sessionId||'').trim();
  if(!sid) return;
  try{sessionStorage.removeItem(_pendingSessionModelKey(sid));}catch(_){}
}
// #5924: the recovery-send deliberate-pick signal. Returns {model, model_provider}
// ONLY when the active session's own model is a genuine non-default pick vs the
// profile default — the same signal send()'s persistent-pick path (_isCrossProviderPick)
// uses, generalized to same-provider non-default picks too. Used by the recovery
// paths (cmdRetry / submitEdit) to decide whether to re-arm the single-shot
// explicit-pick marker: the marker is consumed by the failed send before we reach
// recovery, so we can't read it back, and comparing _chatPayloadModel() to itself
// either false-negatives (an already-applied pick looks unchanged) or false-positives
// (provider inference manufactures a "change"). A non-default session model is the
// durable, inference-free evidence of a real pick. Returns null (no re-arm → the
// server's compatible-model resolution runs) when the session is on the default.
function _deliberateSessionModelPick(sessionId){
  if(!S.session||S.session.session_id!==sessionId) return null;
  const model=String(S.session.model||'').trim();
  if(!model) return null;
  // Require SESSION-OWNED provider evidence — a stored model_provider on the
  // session itself. Do NOT infer a provider from the model string: an
  // unreachable/renamed model like "@removed:mistral-large" with no stored
  // provider must NOT count as a deliberate pick (round-2/3 false-positive).
  const provider=S.session.model_provider?String(S.session.model_provider).trim():'';
  if(!provider) return null;
  // Require a KNOWN profile default to compare against. If we don't know the
  // default (empty window._defaultModel), we can't prove this is a non-default
  // pick, so fail closed → no re-arm (server compatible-model resolution runs).
  const defaultModel=(typeof window!=='undefined'&&window._defaultModel)?String(window._defaultModel):'';
  const activeProvider=(typeof window!=='undefined'&&window._activeProvider)?String(window._activeProvider):'';
  if(!defaultModel||!activeProvider) return null;
  // Non-default = a different model OR a different provider than the profile
  // default. A session sitting exactly on the profile default is NOT a pick.
  const isDefault=(model===defaultModel)&&(provider===activeProvider);
  if(isDefault) return null;
  return {model, model_provider:provider};
}
// #5924: re-arm the single-shot explicit-pick marker from a recovery pick, but
// ONLY if it's still safe at fire time. Guards the SILENT same-session race where
// the user changes the model DURING the recovery's awaits: (1) the session must
// still be the captured one; (2) the session's CURRENT model/provider must still
// equal the captured pick (a mid-flight change means the pick is stale — skip);
// (3) never clobber a NEWER pending marker (an onchange during the await already
// wrote the authoritative one). Returns true if it re-armed.
function _reArmRecoveryPick(sessionId, pick){
  if(!pick||!pick.model) return false;
  if(!S.session||S.session.session_id!==sessionId) return false;
  // Current session state must still match the captured pick (no mid-flight change).
  if(String(S.session.model||'')!==String(pick.model||'')
     ||String(S.session.model_provider||'')!==String(pick.model_provider||'')) return false;
  // Do not overwrite a newer marker written by an onchange during the await.
  if(typeof _readPendingSessionModel==='function'){
    const existing=_readPendingSessionModel(sessionId);
    if(existing&&existing.model
       &&(String(existing.model)!==String(pick.model)
          ||String(existing.model_provider||'')!==String(pick.model_provider||''))) return false;
  }
  if(typeof _rememberPendingSessionModel==='function'){
    _rememberPendingSessionModel(sessionId, pick.model, pick.model_provider);
    return true;
  }
  return false;
}
function _applyPendingSessionModelForSession(sessionId){
  if(!S.session||S.session.session_id!==sessionId) return false;
  const pending=_readPendingSessionModel(sessionId);
  if(!pending) return false;
  const sameModel=String(S.session.model||'')===pending.model;
  const sameProvider=String(S.session.model_provider||'')===String(pending.model_provider||'');
  if(sameModel&&sameProvider){
    _clearPendingSessionModel(sessionId);
    return false;
  }
  S.session.model=pending.model;
  S.session.model_provider=pending.model_provider||null;
  const retry=_persistSessionModelCorrection(pending.model,pending.model_provider||null,{propagateErrors:true});
  if(retry&&typeof retry.then==='function'){
    retry.then(()=>_clearPendingSessionModel(sessionId)).catch(()=>{});
  }
  return true;
}
function _findModelInDropdown(modelId, sel, preferredProviderId){
  if(!modelId||!sel) return null;
  const options=Array.from(sel.options);
  const opts=options.map(o=>o.value);
  // 0. Exact match — highest priority when it doesn't conflict with a
  // cross-provider preference (#3360, guarded for #1228/#1313).
  // When all models share the same provider (e.g. a custom proxy),
  // normalization can collapse distinct multi-slash IDs to the same key
  // and options.find() returns whichever appears first in the DOM instead
  // of the exact value.  But when the exact option belongs to a *different*
  // provider than the preferred one, we must fall through to the provider-
  // aware match so rehydration doesn't snap to the wrong provider row.
  if(opts.includes(modelId)){
    const exactOpt=options.find(o=>o.value===modelId);
    const exactProv=exactOpt?_getOptionProviderId(exactOpt).toLowerCase():'';
    const pref=String(preferredProviderId||'').toLowerCase();
    if(!pref || !exactProv || exactProv===pref) return modelId;
  }
  // 1. Restore lookup keeps the older hierarchy-preserving matcher instead of
  // the picker-dedup identity, so missing qualified models do not substitute a
  // different suffix-sharing sibling.
  const norm=s=>String(s||'')
    .toLowerCase()
    .replace(/^@([^:]+:)+/,'')
    .replace(/^[^/]+\//,'')
    .replace(/-/g,'.');
  const target=norm(modelId);
  let explicitProvider='';
  const rawModel=String(modelId||'');
  if(rawModel.startsWith('@')&&rawModel.includes(':')){
    explicitProvider=rawModel.slice(1,rawModel.lastIndexOf(':'));
  }
  const preferred=String(preferredProviderId||explicitProvider||'').toLowerCase();
  if(preferred){
    const providerMatch=options.find(o=>norm(o.value)===target && _getOptionProviderId(o).toLowerCase()===preferred);
    if(providerMatch) return providerMatch.value;
  }
  // 2. Normalized match — but ONLY when unambiguous. If the bare id
  // matches across multiple provider groups AND no provider hint is
  // available, return null instead of snapping to the first group's
  // option. This prevents a deliberate non-default pick from reverting
  // to the default provider on re-render (#6195).
  const exact=opts.find(o=>norm(o)===target);
  if(exact){
    const normMatches=options.filter(o=>norm(o.value)===target);
    if(normMatches.length>1 && !preferred && !explicitProvider && !rawModel.includes('/')){
      return null;  // ambiguous bare id — caller must inject the correct option
    }
    return exact;
  }
  // If the request is provider-qualified (either explicit @provider:model or
  // a slash-qualified vendor/model id), do NOT fuzzy-match a sibling model
  // once exact/provider-aware lookup failed. Returning null lets the caller
  // preserve the raw typed value instead of snapping to the closest catalog
  // entry. This keeps uncatalogued models routable instead of silently turning
  // them into a nearby curated sibling.
  if(rawModel.startsWith('@')||rawModel.includes('/')) return null;
  // 3. Prefix/substring: require the candidate to start with the FULL normalized target
  // (not a truncated base). This avoids false matches like gpt.5.5 → gpt.5.4.mini (#1188).
  // Only fall back to the shorter base form if target itself is very short (a bare root
  // like "gpt" or "claude") where stripping would be a no-op anyway.
  const base=target.replace(/\.\d+$/,'');  // strip trailing version number
  const useBase=base.length<=4||base===target; // bare root — stripping changed nothing meaningful
  const prefixTarget=useBase?base:target;
  // When the typed target is a COMPLETE versioned name (ends in a digit, e.g.
  // "mimo-v2.5" → norm "mimo.v2.5"), a prefix hit on a longer option is only
  // legitimate if the extra text continues the VERSION ("." + digit, e.g.
  // mimo.v2 → mimo.v2.5...). If the extra text is a variant/tier suffix
  // ("." + non-digit, e.g. mimo.v2.5.pro from "mimo-v2.5-pro"), the user asked
  // for the base model that simply isn't in the catalog — do NOT silently snap
  // them to the -pro/-flash tier (and a different price tier). Let resolution
  // fall through to null so the caller reports no-match instead. (#3368)
  const targetEndsInVersion=/\d$/.test(target);
  const partial=opts.find(o=>{
    const no=norm(o);
    if(!no.startsWith(prefixTarget)) return false;
    if(targetEndsInVersion && no!==target){
      const rest=no.slice(target.length);
      // reject "." + non-digit (variant/tier suffix); allow "" or "." + digit (version continuation)
      if(rest && !/^\.\d/.test(rest)) return false;
    }
    return true;
  });
  return partial||null;
}

// Set the model picker to the best match for modelId.
// Returns the resolved value that was actually set, or null if nothing matched.
function _refreshOpenModelDropdown(){
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList&&dd.classList.contains('open')&&typeof renderModelDropdown==='function'){
    renderModelDropdown();
    if(typeof _positionModelDropdown==='function') _positionModelDropdown();
  }
  const sdd=$('settingsModelDropdown');
  if(sdd&&sdd.classList&&sdd.classList.contains('open')&&typeof renderModelDropdown==='function'){
    // Re-rendering the OPEN settings picker (e.g. when a late live-model fetch
    // resolves) must not re-grab search focus on touch — same coarse-pointer rule
    // as openSettingsModelDropdown, or the mobile keyboard pops after opening.
    const _coarsePointer=(typeof window.matchMedia==='function')&&window.matchMedia('(pointer: coarse)').matches;
    renderModelDropdown({
      dropdownId:'settingsModelDropdown',
      selectId:'settingsModel',
      forceOpenKey:'settingsModel',
      closeDropdown:closeSettingsModelDropdown,
      selectModel:selectSettingsModelFromDropdown,
      scopeNoteText:t('settings_desc_model')||'Used for new conversations. Existing conversations keep their selected model.',
      autoFocusSearch:!_coarsePointer,
    });
  }
}
function _applyModelToDropdown(modelId, sel, preferredProviderId, opts){
  if(!modelId||!sel) return null;
  const isRichPickerSelect=sel.id==='modelSelect'||sel.id==='settingsModel';
  const currentState=(isRichPickerSelect&&typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel, sel.value)
    : null;
  const resolved=_findModelInDropdown(modelId,sel,preferredProviderId);
  if(resolved){
    sel.value=resolved;
    const preferredProvider=String(preferredProviderId||'').trim().toLowerCase();
    if(preferredProvider&&sel.options){
      // Assigning select.value picks the first duplicate value. Restore the
      // provider-specific option that the caller matched (#6131).
      const preferredOption=Array.from(sel.options).find(o=>
        String(o.value||'')===String(resolved)
        && String(_getOptionProviderId(o)||'').trim().toLowerCase()===preferredProvider
      );
      if(preferredOption) preferredOption.selected=true;
    }
    if(isRichPickerSelect){
      const resolvedState=typeof _modelStateForSelect==='function'
        ? _modelStateForSelect(sel, resolved)
        : {model:resolved,model_provider:preferredProviderId||null};
      const pickerChanged= !!(opts&&opts.forceRefresh) || !currentState
        || String(currentState.model||'')!==String(resolvedState.model||'')
        || String(currentState.model_provider||'')!==String(resolvedState.model_provider||'');
      if(sel.id==='modelSelect'&&typeof syncModelChip==='function') syncModelChip();
      if(sel.id==='settingsModel'&&typeof syncSettingsModelChip==='function') syncSettingsModelChip();
      if(pickerChanged) _refreshOpenModelDropdown();
    }
    return resolved;
  }
  return null;
}
function _ensureModelOptionInDropdown(modelId, sel, preferredProviderId){
  if(!modelId||!sel) return null;
  if(typeof _deduplicateModelPickerOptions==='function') _deduplicateModelPickerOptions(sel,sel.value);
  const requestedProvider=String(preferredProviderId||_providerFromModelValue(modelId)||'').trim();
  const applied=_applyModelToDropdown(modelId,sel,requestedProvider||null);
  if(applied){
    const appliedState=typeof _modelStateForSelect==='function'
      ?_modelStateForSelect(sel,applied)
      :{model:applied,model_provider:null};
    if(!requestedProvider||String(appliedState&&appliedState.model_provider||'').toLowerCase()===requestedProvider.toLowerCase()) return applied;
  }
  const explicitPrefix=requestedProvider?`@${requestedProvider}:`:'';
  const rawModel=String(modelId||'');
  const bareModel=explicitPrefix&&rawModel.toLowerCase().startsWith(explicitPrefix.toLowerCase())
    ?rawModel.slice(explicitPrefix.length)
    :rawModel;
  const value=requestedProvider?`${explicitPrefix}${bareModel}`:rawModel;
  const opt=document.createElement('option');
  opt.value=value;
  opt.textContent=typeof getModelLabel==='function'?getModelLabel(modelId):modelId;
  opt.dataset.custom='1';
  const badge=(window._configuredModelBadges||{})[value];
  const rawBadge=(window._configuredModelBadges||{})[rawModel];
  if(badge&&badge.provider) opt.dataset.provider=badge.provider;
  if(rawBadge&&rawBadge.provider) opt.dataset.provider=rawBadge.provider;
  if(requestedProvider) opt.dataset.model=bareModel;
  const provider=requestedProvider||(badge&&badge.provider)||(rawBadge&&rawBadge.provider)||_providerFromModelValue(value)||'';
  if(provider) opt.dataset.provider=provider;
  sel.appendChild(opt);
  sel.value=value;
  if(sel.id==='modelSelect'){
    if(typeof syncModelChip==='function') syncModelChip();
    _refreshOpenModelDropdown();
  }
  if(sel.id==='settingsModel'){
    if(typeof syncSettingsModelChip==='function') syncSettingsModelChip();
    _refreshOpenModelDropdown();
  }
  return value;
}
function _modelStateFromAppliedDropdown(sel, modelValue){
  const state=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel,modelValue)
    : {model:modelValue,model_provider:null};
  return {model:state.model||modelValue,model_provider:state.model_provider||null};
}
function _persistSessionModelCorrection(model, provider, opts){
  if(!S.session) return;
  const request=fetch(new URL('api/session/update',document.baseURI||location.href).href,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:S.session.id||S.session.session_id,model:model,model_provider:provider||null})
  });
  return opts&&opts.propagateErrors ? request : request.catch(()=>{});
}
let _modelDropdownRequestSeq=0;
let _modelCatalogFallbackRetried=false;

function _applySessionModelFallback(sel){
  if(!sel) return null;
  const configuredDefault=String(window._defaultModel||'').trim();
  if(configuredDefault){
    const appliedDefault=_applyModelToDropdown(configuredDefault,sel,window._activeProvider||null);
    if(appliedDefault) return _modelStateFromAppliedDropdown(sel,appliedDefault);
  }
  const first=sel.querySelector('optgroup > option, option');
  if(first){
    sel.value=first.value;
    if(sel.id==='modelSelect'){
      if(typeof syncModelChip==='function') syncModelChip();
      _refreshOpenModelDropdown();
    }
    return _modelStateFromAppliedDropdown(sel,first.value);
  }
  return null;
}


window.HermesUI.register('modelState', {
  _captureModelDropdownSelection,
  _applyPendingSessionModelForSession,
  _applySessionModelFallback,
});
