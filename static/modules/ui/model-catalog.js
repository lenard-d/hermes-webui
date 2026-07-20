import { _formatGatewayModelLabel, _gatewayRoutingLabel, _latestGatewayRoutingForSession, getModelLabel } from './activity-and-scroll.js';
import { _dynamicModelLabels } from './media-and-quota.js';
import { _applyModelToDropdown, _captureModelDropdownSelection, _deduplicateModelPickerOptions, _getOptionProviderId, _modelPickerOptionIdentity, _modelStateForSelect, _providerSkipsModelMismatchWarning, _reconcileModelDropdownSelection, _refreshOpenModelDropdown } from './model-state.js';
import { $, S, _redirectIfUnauth, esc } from './state.js';

let _modelDropdownRequestSeq=0;
let _modelCatalogFallbackRetried=false;

async function populateModelDropdown(opts={}){
  const sel=$('modelSelect');
  if(!sel) return;
  // `_activeProvider` is refreshed from the /api/models response below.
  if(typeof _modelDropdownRequestSeq!=='number') _modelDropdownRequestSeq=0;
  if(typeof _modelCatalogFallbackRetried!=='boolean') _modelCatalogFallbackRetried=false;
  const requestSeq=++_modelDropdownRequestSeq;
  try{
    const modelsUrl=new URL('api/models',document.baseURI||location.href);
    const requestedFreshness=opts&&opts.freshness?String(opts.freshness):'';
    if(opts&&opts.freshness) modelsUrl.searchParams.set('freshness',opts.freshness);
    const _modelsRes=await fetch(modelsUrl.href,{credentials:'include'});
    if(requestSeq!==_modelDropdownRequestSeq) return;
    const customRedirectIfUnauth=opts&&typeof opts.redirectIfUnauth==='function'?opts.redirectIfUnauth:null;
    if(customRedirectIfUnauth){
      if(customRedirectIfUnauth(_modelsRes)) return;
    }else if(_redirectIfUnauth(_modelsRes)) return;
    // `_activeProvider` is populated from the /api/models payload below.
    const data=await _modelsRes.json();
    if(requestSeq!==_modelDropdownRequestSeq) return;
    window._activeProvider=data.active_provider||null;
    window._defaultModel=data.default_model||null;
    window._configuredModelBadges=data.configured_model_badges||{};
    window._modelEndpointErrors={};
    // Keep g.extra_models label hydration in this function for /model and tail selections.

    const _synthGroupsFromConfigured=()=>{
      const badgeMap=window._configuredModelBadges||{};
      const grouped=new Map();
      const addModel=(providerId,modelId)=>{
        const pid=String(providerId||'configured').trim()||'configured';
        const mid=String(modelId||'').trim();
        if(!mid) return;
        if(!grouped.has(pid)) grouped.set(pid,[]);
        const arr=grouped.get(pid);
        if(arr.some(m=>m.id===mid)) return;
        arr.push({id:mid,label:getModelLabel(mid)});
      };

      for(const [modelId,badge] of Object.entries(badgeMap)){
        const mid=String(modelId||'').trim();
        // Prefer canonical IDs only; skip derived aliases such as
        // @provider:model and provider/model to avoid noisy duplicates.
        if(!mid||mid.startsWith('@')||mid.includes('/')) continue;
        const provider=(badge&&badge.provider)||'configured';
        addModel(provider,mid);
      }

      if(grouped.size===0&&data&&data.default_model){
        addModel(data.active_provider||'configured',data.default_model);
      }

      const groups=[];
      for(const [providerId,models] of grouped.entries()){
        const display=(String(providerId).startsWith('custom:')
          ? String(providerId).slice('custom:'.length)
          : String(providerId))||'Configured';
        groups.push({provider:display,provider_id:providerId,models});
      }
      return groups;
    };

    const usedConfiguredFallback=!(Array.isArray(data.groups)&&data.groups.length);
    const groups=usedConfiguredFallback
      ? _synthGroupsFromConfigured()
      : data.groups;
    const willRetry=usedConfiguredFallback && requestedFreshness!=='session_visit' && !_modelCatalogFallbackRetried;

    if(!groups.length){
      if(willRetry){
        _modelCatalogFallbackRetried=true;
        populateModelDropdown({...opts,freshness:'session_visit'}).catch(()=>{});
      }
      return; // no server groups and no configured fallback
    }
    const previousSelection=_captureModelDropdownSelection(sel);
    // Clear existing options
    sel.innerHTML='';
    for(const key of Object.keys(_dynamicModelLabels)) delete _dynamicModelLabels[key];
    for(const g of groups){
      const og=document.createElement('optgroup');
      og.label=g.provider;
      if(g.provider_id) og.dataset.provider=g.provider_id;
      if(g.models_endpoint_error){
        const errorKey=g.provider_id||g.provider||'';
        og.dataset.modelsEndpointError=JSON.stringify(g.models_endpoint_error);
        if(errorKey) window._modelEndpointErrors[errorKey]=g.models_endpoint_error;
      }
      for(const m of (Array.isArray(g.models)?g.models:[])){
        const opt=document.createElement('option');
        opt.value=m.id;
        opt.textContent=m.label;
        if(m && (m.supports_fast_tier === true || String(m.supports_fast_tier).toLowerCase()==='true')){
          opt.dataset.fast='1';
        }else if(m && (m.supports_fast_tier === false || String(m.supports_fast_tier).toLowerCase()==='false')){
          opt.dataset.fast='0';
        }
        og.appendChild(opt);
        _dynamicModelLabels[m.id]=m.label||m.id;
      }
      // Hydrate the label map from extra_models too (the catalog tail that
      // doesn't render as <option> entries when the picker is capped — see
      // _build_nous_featured_set in api/config.py for the rationale). This
      // keeps a model selected from the slash-command autocomplete or a
      // persisted-localStorage value renderable with its proper label
      // instead of falling back to the bare ID. #1567.
      if(Array.isArray(g.extra_models)){
        try{ og.dataset.extraModels=JSON.stringify(g.extra_models); }catch(_e){ og.dataset.extraModels='[]'; }
        for(const m of g.extra_models){
          if(m && m.id) _dynamicModelLabels[m.id]=m.label||m.id;
        }
      }
      sel.appendChild(og);
    }
    if(typeof _deduplicateModelPickerOptions==='function'){
      _deduplicateModelPickerOptions(sel,previousSelection&&previousSelection.model||'');
    }
    _reconcileModelDropdownSelection(sel,data,previousSelection,opts);
    if(typeof syncModelChip==='function') syncModelChip();
    const dd=$('composerModelDropdown');
    if(dd&&dd.classList.contains('open')) _refreshOpenModelDropdown();
    // Kick off a background live-model fetch for the active provider.
    // This runs after the static list is already shown (no blocking flicker).
    if(data.active_provider && !willRetry) _fetchLiveModels(data.active_provider, sel, requestSeq);
    if(willRetry){
      _modelCatalogFallbackRetried=true;
      populateModelDropdown({...opts,freshness:'session_visit'}).catch(()=>{});
    }
  }catch(e){
    if(requestSeq!==_modelDropdownRequestSeq) return;
    // API unavailable -- keep the hardcoded HTML options as fallback
    console.warn('Failed to load models from server:',e.message);
    if(typeof syncModelChip==='function') syncModelChip();
  }
}

// Cache so we don't re-fetch on every page load
const _liveModelCache={};
// Tracks providers for which a live-model fetch is in flight.
// Used by syncTopbar() to defer model corrections until the fetch completes,
// preventing premature fallback to the first static model (#1169).
const _liveModelFetchPending=new Set();

function _addLiveModelsToSelect(provider, models, sel){
  if(!provider||!models||!models.length||!sel) return 0;
  const currentVal=sel.value;
  let providerGroup=null;
  for(const og of sel.querySelectorAll('optgroup')){
    if(og.dataset.provider&&og.dataset.provider===provider){
      providerGroup=og; break;
    }
    if(og.label&&og.label.toLowerCase().includes(provider.toLowerCase())){
      providerGroup=og; break;
    }
  }
  if(!providerGroup){
    providerGroup=document.createElement('optgroup');
    providerGroup.label=provider.charAt(0).toUpperCase()+provider.slice(1)+' (live)';
    providerGroup.dataset.provider=provider;
    sel.appendChild(providerGroup);
  }else if(!providerGroup.dataset.provider){
    providerGroup.dataset.provider=provider;
  }
  const existingIds=new Set([...sel.options].map(o=>o.value));
  const _ap=(window._activeProvider||'').toLowerCase();
  const _providerLower=String(provider||'').toLowerCase();
  const _isNamedCustomActiveProvider=_ap.startsWith('custom:');
  const _isPortalFetch=_ap && _ap!=='openrouter' && _ap!=='custom' && _ap!=='openai-codex' && (_providerLower===_ap||_isNamedCustomActiveProvider&&_providerLower===_ap);
  // Keep existingNorm.has( within the #907 source slice.
  const optionIdentity=typeof _modelPickerOptionIdentity==='function'
    ? (modelId,providerId)=>_modelPickerOptionIdentity(modelId,providerId)
    : (modelId,providerId)=>{
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
        return value.split('/').pop().replace(/-/g,'.').toLowerCase();
      };
  const existingNorm=new Set([...sel.options].map(o=>optionIdentity(o.value,_getOptionProviderId(o))));
  let added=0;
  for(const m of models){
    let mid=m.id;
    if(_isPortalFetch && !mid.startsWith('@')){
      mid=`@${provider}:${mid}`;
    }
    if(existingIds.has(mid)) continue;
    const identity=optionIdentity(mid,provider);
    if(existingNorm.has(identity)){
      const sameGroup=Array.from(providerGroup.children||[]).find(o=>optionIdentity(o.value,_getOptionProviderId(o))===identity);
      if(sameGroup){
        const incomingRoutable=String(mid).startsWith('@');
        const existingRoutable=String(sameGroup.value||'').startsWith('@');
        if(!(!existingRoutable&&incomingRoutable)) continue; // let proxy replace catalog twin
      }
    }
    const opt=document.createElement('option');
    opt.value=mid;
    opt.textContent=m.label||m.id;
    opt.title='Live model — fetched from provider';
    opt.dataset.provider=provider;
    if(m && (m.supports_fast_tier === true || String(m.supports_fast_tier).toLowerCase()==='true')){
      opt.dataset.fast='1';
    }else if(m && (m.supports_fast_tier === false || String(m.supports_fast_tier).toLowerCase()==='false')){
      opt.dataset.fast='0';
    }
    providerGroup.appendChild(opt);
    existingIds.add(mid);
    existingNorm.add(identity);
    _dynamicModelLabels[mid]=m.label||m.id;
    added++;
  }
  if(typeof _deduplicateModelPickerOptions==='function') _deduplicateModelPickerOptions(sel,currentVal);
  const currentState=(currentVal&&typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel, currentVal)
    : {model:currentVal||'', model_provider:(S.session&&S.session.model_provider)||null};
  const currentProvider=currentState&&currentState.model_provider||null;
  if(added>0 && currentVal) _applyModelToDropdown(currentVal, sel, currentProvider, {forceRefresh:true});
  // After live models are added, re-apply the session's model in case it was
  // absent from the static list and syncTopbar() fired before the live fetch
  // completed (#1169). This ensures the session model wins over any premature
  // fallback that may have set sel.value to the first available option.
  if(S.session && S.session.model && sel.id==='modelSelect'){
    const sessionProvider=S.session.model_provider||null;
    const sessionAlreadyRefreshed=added>0 && currentVal
      && String((currentState&&currentState.model)||'')===String(S.session.model||'')
      && String((currentState&&currentState.model_provider)||'')===String(sessionProvider||'');
    const reapplied=_applyModelToDropdown(S.session.model, sel, sessionProvider, {forceRefresh:added>0&&!sessionAlreadyRefreshed});
    if(reapplied && typeof syncModelChip==='function') syncModelChip();
  }
  return added;
}

async function _fetchLiveModels(provider, sel, requestSeq=null){
  if(!provider||!sel) return;
  if(requestSeq!==null&&requestSeq!==_modelDropdownRequestSeq) return;
  // Already fetched — apply cached models to this select element (#872)
  if(_liveModelCache[provider]){
    if(requestSeq!==null&&requestSeq!==_modelDropdownRequestSeq) return;
    const added=_addLiveModelsToSelect(provider,_liveModelCache[provider],sel);
    if(added>0 && typeof syncModelChip==='function') syncModelChip();
    return;
  }
  _liveModelFetchPending.add(provider);
  try{
    const url=new URL('api/models/live',document.baseURI||location.href);
    url.searchParams.set('provider',provider);
    const _liveRes=await fetch(url.href,{credentials:'include'});
    if(requestSeq!==null&&requestSeq!==_modelDropdownRequestSeq) return;
    if(_redirectIfUnauth(_liveRes)) return;
    const data=await _liveRes.json();
    if(requestSeq!==null&&requestSeq!==_modelDropdownRequestSeq) return;
    if(!data.models||!data.models.length) return;
    _liveModelCache[provider]=data.models;
    if(requestSeq!==null&&requestSeq!==_modelDropdownRequestSeq) return;
    const added=_addLiveModelsToSelect(provider,data.models,sel);
    if(added>0){
      if(typeof syncModelChip==='function') syncModelChip();
      console.debug('[hermes] Live models loaded for',provider+':',added,'new models added');
    }
  }catch(e){
    console.debug('[hermes] Live model fetch failed for',provider,e.message);
  }finally{
    _liveModelFetchPending.delete(provider);
  }
}

/**
 * Check if the given model ID belongs to a different provider than the one
 * currently configured in Hermes. Returns a warning string if mismatched,
 * or null if the selection looks compatible.
 *
 * Provider detection is intentionally loose — we compare the model's slash
 * prefix (e.g. "openai/" from "openai/gpt-4o") against the active provider
 * name. Custom/local endpoints report active_provider='custom', a named
 * custom provider such as 'custom:zenmux', or the base_url hostname; skip the
 * check for those values to avoid false positives.
 */
function _checkProviderMismatch(modelId){
  const ap=(window._activeProvider||'').toLowerCase();
  if(_providerSkipsModelMismatchWarning(ap)) return null; // can't reliably check
  // @provider: prefixed IDs came from that provider's live model list — no mismatch possible
  if(modelId.startsWith('@')) return null;
  const slash=modelId.indexOf('/');
  if(slash<0) return null; // bare model name, no provider prefix
  const modelProvider=modelId.substring(0,slash).toLowerCase();
  // Normalise common aliases
  const aliases={'claude':'anthropic','gpt':'openai','gemini':'google'};
  const norm=p=>aliases[p]||p;
  if(norm(modelProvider)!==norm(ap)){
    return (window.t?window.t('provider_mismatch_warning',modelId,ap):
      `"${modelId}" may not work with your configured provider (${ap}). Send anyway or run \`hermes model\` to switch.`);
  }
  return null;
}

function _selectedModelOption(){
  const sel=$('modelSelect');
  if(!sel) return null;
  return sel.options[sel.selectedIndex]||null;
}

function _normalizeConfiguredModelKey(modelId){
  let s=String(modelId||'').trim().toLowerCase();
  let strippedAtProvider=false;
  // Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> jingdong:GLM-5).
  // Defensive: trailing-colon / trailing-slash falls back to the original key
  // so malformed configs don't collapse distinct ids to '' (matches backend _norm_model_id).
  if(s.startsWith('@')&&s.includes(':')){const ci=s.indexOf(':',1);const cand=s.slice(ci+1);strippedAtProvider=!!cand;s=cand||s;}
  // Skip slash-based stripping for URI-scheme IDs (e.g. gpt://folder/model)
  // whose slashes are path separators, not provider delimiters (#3429).
  const _hasScheme=/^[a-z][a-z0-9+.-]*:\/\//i.test(s);
  if(!_hasScheme){
    // Strip provider-qualified prefixes that contain colons before the first
    // slash (e.g. 'custom:llm-proxy/model' → 'model').  Without this, badge-
    // key variants like 'custom:llm-proxy/opencode_go/deepseek-v4-pro' and the
    // bare 'opencode_go/deepseek-v4-pro' produce different normalized keys and
    // aren't deduped in the configured section (#3360).
    if(!strippedAtProvider&&s.includes('/')&&s.indexOf(':')!==-1&&s.indexOf(':')<s.indexOf('/')){
      s=s.slice(s.indexOf('/')+1)||s;
    }
    // Strip only the first slash-segment (provider prefix), preserving any
    // remaining vendor hierarchy. Using split('/').pop() here previously
    // discarded ALL segments except the last, collapsing distinct multi-slash
    // IDs like 'vendor_a/deepseek-v4-pro' and 'vendor_b/deepseek/deepseek-v4-pro'
    // to the same key, causing badge misattribution and configured-entry
    // suppression (#3360).
    if(s.includes('/')) s=s.replace(/^[^/]+\//, '')||s;
  }
  return s.replace(/-/g,'.');
}

function _isEquivalentConfiguredModelEntry(modelId,badge,entries){
  const normalized=_normalizeConfiguredModelKey(modelId);
  const provider=String(badge&&badge.provider||'').toLowerCase();
  const matchingEntries=(entries||[]).filter(existing=>
    _normalizeConfiguredModelKey(existing.value)===normalized
  );
  if(matchingEntries.some(existing=>{
    const entryProvider=String(existing.providerId||'').toLowerCase();
    return !provider||!entryProvider||entryProvider===provider;
  })) return true;
  // @provider:model is an equivalent routing spelling only when an existing
  // picker row belongs to that same provider. This supports named custom
  // providers (@custom:name:model) without collapsing matching model IDs from
  // different providers.
  const rawId=String(modelId||'');
  const prefix=provider?`@${provider}:`:'';
  if(!prefix||!rawId.toLowerCase().startsWith(prefix)) return false;
  const routedId=rawId.slice(prefix.length);
  return (entries||[]).some(entry=>
    String(entry.providerId||'').toLowerCase()===provider
    &&_normalizeConfiguredModelKey(entry.value)===_normalizeConfiguredModelKey(routedId)
  );
}

function _getConfiguredModelBadge(modelId,badgeMap,providerId){
  const map=badgeMap||window._configuredModelBadges||{};
  if(!modelId||!map) return null;
  const provider=String(providerId||'').toLowerCase();
  const exact=map[modelId];
  if(exact && (!provider || !exact.provider || String(exact.provider).toLowerCase()===provider)) return exact;
  const targetNorm=_normalizeConfiguredModelKey(modelId);
  const matches=[];
  for(const [candidate,badge] of Object.entries(map)){
    if(_normalizeConfiguredModelKey(candidate)===targetNorm) matches.push(badge);
  }
  if(!matches.length) return null;
  if(provider){
    const providerMatch=matches.find(badge=>String(badge&&badge.provider||'').toLowerCase()===provider);
    if(providerMatch) return providerMatch;
    return matches.length===1 ? matches[0] : null;
  }
  return matches[0];
}

function _compactComposerModelChipLabel(modelId,labelText){
  const id=String(modelId||'').trim();
  const raw=String(labelText||'').trim();
  if(!raw) return getModelLabel(id);
  const idLower=id.toLowerCase();
  const rawLower=raw.toLowerCase();
  const slash=id.indexOf('/');
  if(slash>0){
    const provider=id.slice(0,slash).toLowerCase();
    if(rawLower.startsWith(provider+'/')){
      return raw.slice(provider.length+1).trim();
    }
  }
  if(id&&rawLower===idLower&&raw.includes('/')){
    return raw.slice(raw.indexOf('/')+1).trim();
  }
  if(raw.includes('/') && !/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)){
    const parts=raw.split('/').map(s=>s.trim()).filter(Boolean);
    if(parts.length>=2){
      const tail=parts[parts.length-1];
      const tailLower=tail.toLowerCase();
      if(idLower && (tailLower===idLower || idLower.endsWith('/'+tailLower))) return tail;
      if(parts.length===2){
        const leadLower=parts[0].toLowerCase();
        if(tailLower.startsWith(leadLower+'-')) return tail;
      }
    }
  }
  return raw;
}

function syncModelChip(){
  const sel=$('modelSelect');
  const chip=$('composerModelChip');
  const label=$('composerModelLabel');
  const mobileLabel=$('composerMobileModelLabel');
  const mobileAction=$('composerMobileModelAction');
  const dd=$('composerModelDropdown');
  if(!sel||!chip||!label) return;
  // Don't show a model label until boot has finished loading to prevent flash of wrong default
  if(!S._bootReady){
    label.textContent='';
    if(mobileLabel) mobileLabel.textContent='';
    chip.title='Conversation model';
    return;
  }
  const opt=_selectedModelOption();
  const text=opt?opt.textContent:getModelLabel(sel.value||'');
  const compactText=_compactComposerModelChipLabel(sel.value||'', text);
  const gatewayRouting=_latestGatewayRoutingForSession(S.session);
  const displayText=_formatGatewayModelLabel(sel.value||'',compactText,gatewayRouting)||compactText;
  label.textContent=displayText;
  if(mobileLabel) mobileLabel.textContent=displayText;
  chip.title=gatewayRouting?`${sel.value||'Conversation model'} ${_gatewayRoutingLabel(gatewayRouting)}`:(sel.value||'Conversation model');
  chip.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
  if(mobileAction) mobileAction.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
}

// Remembers where #composerModelDropdown lives in the composer-footer so the
// phone path can move it to <body> and put it back exactly. Captured lazily on
// the first reparent (see _positionModelDropdown phone branch).
let _modelDropdownHome=null;

// Return the model dropdown into its original .composer-footer slot and clear
// every inline style the phone path wrote, so the desktop CSS (position:absolute
// anchored on the relatively-positioned .composer-footer) fully governs again.
// Safe to call when the element never moved — it just no-ops the reinsert.
function _restoreModelDropdownHome(){
  const dd=document.getElementById('composerModelDropdown');
  if(!dd) return;
  dd.classList.remove('model-dropdown--floating');
  dd.style.left='';
  dd.style.top='';
  dd.style.bottom='';
  dd.style.width='';
  dd.style.maxWidth='';
  dd.style.maxHeight='';
  if(_modelDropdownHome&&_modelDropdownHome.parent&&dd.parentNode!==_modelDropdownHome.parent){
    const ref=_modelDropdownHome.nextSibling;
    if(ref&&ref.parentNode===_modelDropdownHome.parent){
      _modelDropdownHome.parent.insertBefore(dd,ref);
    }else{
      _modelDropdownHome.parent.appendChild(dd);
    }
  }
}

function _positionModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const mobileAction=$('composerMobileModelAction');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!footer) return;
  const panel=$('composerMobileConfigPanel');
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:(chip&&chip.offsetParent?chip:mobileAction);
  if(!anchor) return;
  const isPhone=typeof window.matchMedia==='function'&&window.matchMedia('(max-width:640px)').matches;
  if(isPhone){
    // #6080: .composer-footer sets container-type:inline-size (and a
    // backdrop-filter under the Geist Contrast skin) — both establish a fixed
    // containing block, so a position:fixed dropdown left inside the footer
    // resolves against the FOOTER (bottom of screen) instead of the viewport
    // and lands below the fold. Reparent to <body> — exactly the working
    // #profileDropdown idiom — so position:fixed is viewport-relative on ALL
    // skins, then compute coordinates against the visual viewport.
    if(!_modelDropdownHome){
      _modelDropdownHome={parent:dd.parentNode,nextSibling:dd.nextSibling};
    }
    if(dd.parentNode!==document.body) document.body.appendChild(dd);
    dd.classList.add('model-dropdown--floating');
    const anchorRect=anchor.getBoundingClientRect();
    const visualViewport=window.visualViewport;
    const viewportWidth=Math.max(1,Number(visualViewport&&visualViewport.width)||window.innerWidth||1);
    const viewportHeight=Math.max(1,Number(visualViewport&&visualViewport.height)||window.innerHeight||1);
    const viewportTop=Math.max(0,Number(visualViewport&&visualViewport.offsetTop)||0);
    const viewportBottom=viewportTop+viewportHeight;
    const margin=8;
    const gap=6;
    const viewportLeft=Math.max(0,Number(visualViewport&&visualViewport.offsetLeft)||0);
    const viewportRight=viewportLeft+viewportWidth;
    const titlebar=document.querySelector('.app-titlebar');
    const titlebarBottom=titlebar&&typeof titlebar.getBoundingClientRect==='function'
      ? Number(titlebar.getBoundingClientRect().bottom)||0
      : 0;
    const contentTop=Math.max(viewportTop+margin,titlebarBottom+margin);
    const menuWidth=Math.max(1,viewportWidth-margin*2);
    const left=Math.max(viewportLeft+margin,Math.min(anchorRect.left,viewportRight-menuWidth-margin));
    dd.style.left=`${left}px`;
    dd.style.width=`${menuWidth}px`;
    dd.style.maxWidth=`${menuWidth}px`;
    dd.style.bottom='auto';
    const menuHeight=Math.max(dd.scrollHeight,dd.offsetHeight);
    const aboveSpace=Math.max(0,anchorRect.top-contentTop-gap-margin);
    const belowSpace=Math.max(0,viewportBottom-anchorRect.bottom-gap-margin);
    const openAbove=aboveSpace>=Math.min(menuHeight,belowSpace)||aboveSpace>=belowSpace;
    const availableHeight=Math.max(1,openAbove?aboveSpace:belowSpace);
    dd.style.maxHeight=`${availableHeight}px`;
    const visibleHeight=Math.min(menuHeight||availableHeight,availableHeight);
    const top=openAbove
      ? anchorRect.top-gap-visibleHeight
      : anchorRect.bottom+gap;
    dd.style.top=`${Math.max(contentTop,Math.min(top,viewportBottom-margin-visibleHeight))}px`;
    return;
  }
  // Desktop (>640px): keep the current master behaviour — an absolutely
  // positioned .composer-footer child. Restore the element into the footer (in
  // case a prior phone open moved it to <body>) and clear the phone inline
  // styles so the desktop CSS anchor is byte-for-byte identical to master.
  _restoreModelDropdownHome();
  const anchorRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=anchorRect.left-footerRect.left;
  const maxLeft=Math.max(0, footer.clientWidth-dd.offsetWidth);
  left=Math.max(0, Math.min(left, maxLeft));
  dd.style.left=`${left}px`;
}

function _readModelOverflowData(group){
  if(!group||!group.dataset||!group.dataset.extraModels) return [];
  try{
    const parsed=JSON.parse(group.dataset.extraModels);
    return Array.isArray(parsed)?parsed.filter(m=>m&&m.id):[];
  }catch(_e){
    return [];
  }
}

function _appendOverflowOptionsToGroup(group, extraModels){
  if(!group||!Array.isArray(extraModels)||!extraModels.length) return 0;
  // The selected model may already have been injected into the <select> (e.g. a
  // hidden overflow model picked from search via _ensureModelOptionInDropdown).
  // Appending it again here would create a duplicate row once the group expands,
  // so reuse/move any existing option with the same value instead of re-creating it. (#3691)
  const parentSelect=(group.parentNode&&group.parentNode.tagName==='SELECT')?group.parentNode:null;
  const existingByValue=new Map();
  if(parentSelect){
    for(const opt of Array.from(parentSelect.querySelectorAll('option'))){
      if(opt&&typeof opt.value==='string') existingByValue.set(opt.value,opt);
    }
  }
  let appended=0;
  for(const m of extraModels){
    if(!m||!m.id) continue;
    const existing=existingByValue.get(m.id);
    if(existing){
      // Move the already-present option into this group rather than duplicating it.
      if(existing.parentNode!==group) group.appendChild(existing);
      continue;
    }
    const opt=document.createElement('option');
    opt.value=m.id;
    opt.textContent=m.label||m.id;
    group.appendChild(opt);
    appended++;
  }
  if(group.dataset){
    group.dataset.extraModels='[]';
    group.dataset.overflowExpanded='1';
  }
  return appended;
}

function _mountSearchableModelSelect(opts={}){
  const root=opts.root;
  if(!root) return null;
  const choices=Array.isArray(opts.choices)
    ? opts.choices
      .map(choice=>choice&&choice.id?{id:String(choice.id),label:String(choice.label||choice.id)}:null)
      .filter(Boolean)
    : [];
  const selectedValue=String(opts.selectedValue||'');
  const onModelChange=typeof opts.onModelChange==='function' ? opts.onModelChange : ()=>{};
  const selectId=opts.selectId||'';
  const customInputId=opts.customInputId||'';
  const listedChoiceIds=new Set(choices.map(choice=>choice.id));
  const listedSelection=listedChoiceIds.has(selectedValue) ? selectedValue : '';
  const customSelection=listedSelection ? '' : selectedValue;
  let lastListedValue=listedSelection||(choices[0]?choices[0].id:'');
  root.innerHTML=
    `<div class="model-search-row">`+
      `<input class="model-search-input" type="text" placeholder="${esc(t('model_search_placeholder')||'Search models…')}" spellcheck="false" autocomplete="off">`+
      `<button class="model-search-clear" title="Clear search">${li('x',10)}</button>`+
    `</div>`+
    `<select ${selectId?`id="${esc(selectId)}"`:''}></select>`+
    `<div class="model-group model-custom-sep">${esc(t('model_custom_label')||'Custom model ID')}</div>`+
    `<div class="model-custom-row">`+
      `<input ${customInputId?`id="${esc(customInputId)}"`:''} class="model-custom-input" type="text" placeholder="${esc(t('model_custom_placeholder')||'e.g. openai/gpt-5.4')}" spellcheck="false" autocomplete="off">`+
      `<button class="model-custom-btn" title="Use this model">${li('plus',12)}</button>`+
    `</div>`;
  const searchInput=root.querySelector('.model-search-input');
  const clearButton=root.querySelector('.model-search-clear');
  const selectEl=selectId ? root.querySelector(`#${selectId}`) : root.querySelector('select');
  const customInput=customInputId ? root.querySelector(`#${customInputId}`) : root.querySelector('.model-custom-input');
  const customButton=root.querySelector('.model-custom-btn');
  if(!searchInput||!clearButton||!selectEl||!customInput||!customButton) return null;

  const noMatchesOption=document.createElement('option');
  noMatchesOption.value='';
  noMatchesOption.textContent='No matching models';
  noMatchesOption.disabled=true;
  noMatchesOption.hidden=true;
  selectEl.appendChild(noMatchesOption);

  for(const choice of choices){
    const option=document.createElement('option');
    option.value=choice.id;
    option.textContent=choice.label;
    selectEl.appendChild(option);
  }
  if(listedSelection){
    selectEl.value=listedSelection;
  }else if(customSelection){
    selectEl.selectedIndex=-1;
  }else if(choices.length){
    selectEl.value=choices[0].id;
    onModelChange(lastListedValue);
  }
  customInput.value=customSelection;

  const applyFilter=()=>{
    const needle=(searchInput.value||'').trim().toLowerCase();
    let visibleCount=0;
    for(const option of Array.from(selectEl.options)){
      if(option===noMatchesOption) continue;
      const haystack=`${option.textContent||''} ${option.value||''}`.toLowerCase();
      const visible=!needle||haystack.includes(needle);
      option.hidden=!visible;
      if(visible) visibleCount++;
    }
    noMatchesOption.hidden=visibleCount!==0;
  };

  const applyCustomSelection=()=>{
    onModelChange((customInput.value||'').trim());
  };

  searchInput.addEventListener('input', applyFilter);
  clearButton.addEventListener('click', ()=>{
    searchInput.value='';
    applyFilter();
    searchInput.focus();
  });
  selectEl.addEventListener('change', ()=>{
    customInput.value='';
    lastListedValue=selectEl.value||lastListedValue;
    onModelChange(lastListedValue);
  });
  customInput.addEventListener('input', ()=>{
    const value=(customInput.value||'').trim();
    if(value){
      selectEl.selectedIndex=-1;
      onModelChange(value);
      return;
    }
    customInput.value='';
    if(lastListedValue){
      selectEl.value=lastListedValue;
      onModelChange(lastListedValue);
      return;
    }
    onModelChange('');
  });
  customInput.addEventListener('keydown', (event)=>{
    if(event.key!=='Enter') return;
    event.preventDefault();
    applyCustomSelection();
  });
  customButton.addEventListener('click', (event)=>{
    event.preventDefault();
    applyCustomSelection();
  });
  applyFilter();
  return {searchInput,selectEl,customInput,customButton};
}



export {
  _modelDropdownRequestSeq,
  _modelCatalogFallbackRetried,
  _addLiveModelsToSelect,
  _checkProviderMismatch,
  _selectedModelOption,
  _normalizeConfiguredModelKey,
  _isEquivalentConfiguredModelEntry,
  _getConfiguredModelBadge,
  _compactComposerModelChipLabel,
  syncModelChip,
  _restoreModelDropdownHome,
  _positionModelDropdown,
  _readModelOverflowData,
  _appendOverflowOptionsToGroup,
  _mountSearchableModelSelect,
  populateModelDropdown,
  _fetchLiveModels,
  _liveModelCache,
  _liveModelFetchPending,
  _modelDropdownHome,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _modelDropdownRequestSeq: { enumerable: true, get: () => _modelDropdownRequestSeq, set: (value) => { _modelDropdownRequestSeq = value; } },
  _modelCatalogFallbackRetried: { enumerable: true, get: () => _modelCatalogFallbackRetried, set: (value) => { _modelCatalogFallbackRetried = value; } },
  _addLiveModelsToSelect: { enumerable: true, get: () => _addLiveModelsToSelect, set: (value) => { _addLiveModelsToSelect = value; } },
  _checkProviderMismatch: { enumerable: true, get: () => _checkProviderMismatch, set: (value) => { _checkProviderMismatch = value; } },
  _selectedModelOption: { enumerable: true, get: () => _selectedModelOption, set: (value) => { _selectedModelOption = value; } },
  _normalizeConfiguredModelKey: { enumerable: true, get: () => _normalizeConfiguredModelKey, set: (value) => { _normalizeConfiguredModelKey = value; } },
  _isEquivalentConfiguredModelEntry: { enumerable: true, get: () => _isEquivalentConfiguredModelEntry, set: (value) => { _isEquivalentConfiguredModelEntry = value; } },
  _getConfiguredModelBadge: { enumerable: true, get: () => _getConfiguredModelBadge, set: (value) => { _getConfiguredModelBadge = value; } },
  _compactComposerModelChipLabel: { enumerable: true, get: () => _compactComposerModelChipLabel, set: (value) => { _compactComposerModelChipLabel = value; } },
  syncModelChip: { enumerable: true, get: () => syncModelChip, set: (value) => { syncModelChip = value; } },
  _restoreModelDropdownHome: { enumerable: true, get: () => _restoreModelDropdownHome, set: (value) => { _restoreModelDropdownHome = value; } },
  _positionModelDropdown: { enumerable: true, get: () => _positionModelDropdown, set: (value) => { _positionModelDropdown = value; } },
  _readModelOverflowData: { enumerable: true, get: () => _readModelOverflowData, set: (value) => { _readModelOverflowData = value; } },
  _appendOverflowOptionsToGroup: { enumerable: true, get: () => _appendOverflowOptionsToGroup, set: (value) => { _appendOverflowOptionsToGroup = value; } },
  _mountSearchableModelSelect: { enumerable: true, get: () => _mountSearchableModelSelect, set: (value) => { _mountSearchableModelSelect = value; } },
  populateModelDropdown: { enumerable: true, get: () => populateModelDropdown, set: (value) => { populateModelDropdown = value; } },
  _fetchLiveModels: { enumerable: true, get: () => _fetchLiveModels, set: (value) => { _fetchLiveModels = value; } },
  _liveModelCache: { enumerable: true, get: () => _liveModelCache },
  _liveModelFetchPending: { enumerable: true, get: () => _liveModelFetchPending },
  _modelDropdownHome: { enumerable: true, get: () => _modelDropdownHome, set: (value) => { _modelDropdownHome = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
