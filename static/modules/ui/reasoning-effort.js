import { showToast } from './toast-notifications.js';
import { closeOtherComposerMenus, registerComposerMenu } from './composer-menu-registry.js';
import { _modelStateForSelect } from './model-state.js';
import { $, S } from './state.js';

// ── Reasoning effort chip ────────────────────────────────────────────────────
let _currentReasoningEffort=null;
let _currentReasoningEffortsSupported=null;
// Whether the model accepts the thinking on/off toggle when supported_efforts
// is empty (GLM-4.5–5.1 on native zai). Undefined = unknown, treated as true
// so the chip stays visible by default (prior behavior).
let _currentReasoningToggleSupported=undefined;
let _profileTransitionReasoningContext=null;

function _normalizeReasoningEffort(eff){
  return String(eff||'').trim().toLowerCase();
}

function _formatReasoningEffortLabel(effort){
  if(effort==='none') return 'None';
  if(!effort) return 'Default';
  if(effort==='minimal') return 'Minimal';
  if(effort==='low') return 'Low';
  if(effort==='medium') return 'Medium';
  if(effort==='high') return 'High';
  if(effort==='xhigh') return 'XHigh';
  if(effort==='max') return 'Max';
  return effort.charAt(0).toUpperCase()+effort.slice(1);
}

function _reasoningEffortContext(){
  const transition=_profileTransitionReasoningContext;
  const session=S&&S.session;
  if(transition&&(!session||session.profile!==transition.profile)){
    const ctx={};
    if(transition.model) ctx.model=transition.model;
    if(transition.provider) ctx.provider=transition.provider;
    return ctx;
  }
  const sel=$('modelSelect');
  const model=(S&&S.session&&S.session.model)||(sel&&sel.value)||'';
  let provider=(S&&S.session&&S.session.model_provider)||'';
  if(!provider&&sel&&model&&typeof _modelStateForSelect==='function'){
    provider=_modelStateForSelect(sel, model).model_provider||'';
  }
  const ctx={};
  if(model) ctx.model=model;
  if(provider) ctx.provider=provider;
  return ctx;
}

function _reasoningEffortQuery(){
  const params=new URLSearchParams(_reasoningEffortContext());
  const qs=params.toString();
  return qs?('?'+qs):'';
}

function _applyReasoningOptions(supportedEfforts){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  const supported=new Set(Array.isArray(supportedEfforts)?supportedEfforts:[]);
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    const effort=opt.dataset.effort;
    // 'none' (turn thinking off) and '' (Default = clear override, provider
    // default = thinking on) are meta-options outside the effort ladder. They
    // are always shown so a thinking-toggle-only model (GLM-4.5–5.1 on native
    // zai, where the ladder is empty) still has an operable two-state control:
    // Default (on) + None (off). Without the Default option the toggle is
    // one-way off-only — the user can disable thinking but cannot re-enable it.
    // (#6219 round-3)
    if(effort==='none'||effort===''){
      opt.style.display='';
      return;
    }
    if(!supported.size){
      opt.style.display='none';
      return;
    }
    opt.style.display=supported.has(effort)?'':'none';
  });
}

function _applyReasoningChip(eff){
  const meta=arguments[1]||null;
  const effort=_normalizeReasoningEffort(eff);
  _currentReasoningEffort=effort;
  if(meta&&Array.isArray(meta.supported_efforts)){
    _currentReasoningEffortsSupported=meta.supported_efforts;
  }
  // supports_thinking_toggle: the model accepts the thinking on/off toggle even
  // when the effort ladder is empty (GLM-4.5–5.1 on native zai accept
  // `thinking: {"type": ...}` but NOT `reasoning_effort`). Without honoring this
  // flag, returning an empty supported_efforts hides the entire chip and
  // silently regresses the working thinking on/off control for those models.
  // Default true preserves prior behavior when the field is absent.
  if(meta&&typeof meta.supports_thinking_toggle==='boolean'){
    _currentReasoningToggleSupported=meta.supports_thinking_toggle;
  }
  const wrap=$('composerReasoningWrap');
  const label=$('composerReasoningLabel');
  const chip=$('composerReasoningChip');
  const mobileLabel=$('composerMobileReasoningLabel');
  const mobileAction=$('composerMobileReasoningAction');
  if(!wrap||!label) return;
  const supportedEfforts=(typeof _currentReasoningEffortsSupported==='undefined')
    ?null
    :_currentReasoningEffortsSupported;
  const toggleSupported=(typeof _currentReasoningToggleSupported==='undefined')
    ?true
    :_currentReasoningToggleSupported;
  const hasEffortLadder=Array.isArray(supportedEfforts)
    ?supportedEfforts.length>0
    :true;
  // Show the chip if there is an effort ladder OR a thinking toggle is still
  // available. Only hide when the model supports neither.
  const supports=hasEffortLadder||toggleSupported;
  if(!supports){
    wrap.style.display='none';
    if(mobileAction) mobileAction.style.display='none';
    return;
  }
  wrap.style.display='';
  if(mobileAction) mobileAction.style.display='';
  if(typeof _applyReasoningOptions==='function') _applyReasoningOptions(supportedEfforts);
  const text=_formatReasoningEffortLabel(effort);
  label.textContent=text;
  if(mobileLabel) mobileLabel.textContent=text;
  if(chip){
    const inactive=!effort||effort==='none';
    chip.classList.toggle('inactive',inactive);
    const labelText='Reasoning effort: '+text;
    chip.title=labelText;
    chip.setAttribute('aria-label',labelText);
  }
  if(mobileAction) mobileAction.classList.toggle('inactive',!effort||effort==='none');
  _highlightReasoningOption(effort);
}

// Tracks the model/provider identity of the last reasoning fetch so routine
// topbar syncs can serve the cached chip state instead of re-hitting the
// network. null = never fetched.
let _lastReasoningFetchKey=null;
// Monotonic dispatch counter. Each fetchReasoningChip() increments it and the
// async handlers capture their own value; a response (success OR failure) only
// applies if it is still the most recent dispatch. This defeats out-of-order
// resolution even when two fetches share the same model/provider key (e.g. a
// profile switch that resets the cache and refetches the same default model but
// a different agent.reasoning_effort) — #4650 review.
let _reasoningFetchSeq=0;

function fetchReasoningChip(keyOverride){
  // Set the cache key OPTIMISTICALLY before the request so rapid routine syncs
  // while this GET is in flight short-circuit instead of re-dispatching (that
  // in-flight window is exactly where the #4650 storm lived).
  const key=keyOverride===undefined?_reasoningEffortQuery():keyOverride;
  const seq=++_reasoningFetchSeq;
  _lastReasoningFetchKey=key;
  api('/api/reasoning'+key).then(function(st){
    // Ignore a stale/superseded response: only the most recent dispatch may
    // apply, so an older in-flight GET can't poison the current chip (#4650).
    if(seq!==_reasoningFetchSeq) return;
    _applyReasoningChip((st&&st.reasoning_effort)||'', st||{});
  }).catch(function(){
    // Same staleness guard on failure: a stale error must neither hide the chip
    // nor clear a newer fetch's key. Only the latest dispatch clears the key so
    // routine syncs retry after a genuine transient failure.
    if(seq!==_reasoningFetchSeq) return;
    _lastReasoningFetchKey=null;
    _applyReasoningChip('', {supported_efforts:[], supports_thinking_toggle:false});
  });
}

function refreshProfileTransitionReasoningChip(model, provider){
  _profileTransitionReasoningContext={profile:(S&&S.activeProfile)||'default',model,provider};
  _currentReasoningEffort=null;
  _currentReasoningEffortsSupported=null;
  _currentReasoningToggleSupported=undefined;
  _lastReasoningFetchKey=null;
  ++_reasoningFetchSeq;
  _applyReasoningChip('', {supported_efforts:[], supports_thinking_toggle:false});
  const params=new URLSearchParams();
  if(model) params.set('model',model);
  if(provider) params.set('provider',provider);
  fetchReasoningChip(params.size?'?'+params.toString():undefined);
}

function clearProfileTransitionReasoningContext(){
  _profileTransitionReasoningContext=null;
}

function syncReasoningChip(){
  // #4650: syncTopbar() calls this on every routine UI refresh, and during
  // streaming those fire at high frequency. Before a9ce2889 this served the
  // cached _currentReasoningEffort after the first load; that commit made it
  // refetch unconditionally to refresh supported-efforts after a model switch,
  // which turned ordinary syncs into a GET /api/reasoning storm (one per token).
  // Restore the cache short-circuit but keep a9ce2889's intent: only hit the
  // network when nothing is cached yet OR the model/provider identity changed
  // since the last fetch (the only inputs that change /api/reasoning's answer).
  // The user-pick and model-switch paths still update the cache directly.
  const key=_reasoningEffortQuery();
  // Short-circuit on the KEY alone: if a fetch for this exact model/provider has
  // already been dispatched (in-flight) or completed, do not dispatch another —
  // this is what stops the #4650 storm, including the COLD-cache window where
  // _currentReasoningEffort is still null between the first dispatch and its
  // response (10 syncs before the first GET resolves must produce ONE request,
  // not ten). Apply the cached chip only once we actually have an effort value.
  if(_lastReasoningFetchKey===key){
    if(_currentReasoningEffort!==null) _applyReasoningChip(_currentReasoningEffort);
    return;
  }
  fetchReasoningChip();
}

function _highlightReasoningOption(effort){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    opt.classList.toggle('selected',opt.dataset.effort===effort);
  });
}

function toggleReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  if(!dd||!chip) return;
  const open=dd.classList.contains('open');
  if(open){closeReasoningDropdown();return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeOtherComposerMenus('reasoning');
  _highlightReasoningOption(_currentReasoningEffort);
  dd.classList.add('open');
  _positionReasoningDropdown();
  chip.classList.add('active');
  const mobileAction=$('composerMobileReasoningAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function _positionReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer) return;
  const panel=$('composerMobileConfigPanel');
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:chip;
  const chipRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0,footer.clientWidth-dd.offsetWidth);
  left=Math.max(0,Math.min(left,maxLeft));
  dd.style.left=`${left}px`;
}

function closeReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
}
registerComposerMenu('reasoning',closeReasoningDropdown);

window.addEventListener('resize',()=>{
  const dropdown=$('composerReasoningDropdown');
  if(dropdown&&dropdown.classList.contains('open')) _positionReasoningDropdown();
});

document.addEventListener('click',function(e){
  if(
    !e.target.closest('#composerReasoningChip') &&
    !e.target.closest('#composerMobileReasoningAction') &&
    !e.target.closest('#composerReasoningDropdown')
  ) closeReasoningDropdown();
  if(e.target.closest('.reasoning-option')){
    const opt=e.target.closest('.reasoning-option');
    const effort=opt&&opt.dataset.effort;
    // NOTE: effort may be the empty string for the "Default" option (clears
    // the override). Check option presence, not truthiness — `if(effort)` would
    // silently ignore the Default click and leave the toggle one-way off-only.
    // (#6219 round-3)
    if(opt){
      const payload=Object.assign({effort:effort},_reasoningEffortContext());
      api('/api/reasoning',{method:'POST',body:JSON.stringify(payload)})
        .then(function(st){
          // For Default (effort=''), the returned reasoning_effort is '' (clear)
          // — display 'Default' rather than an empty toast.
          const display=(st&&st.reasoning_effort)||effort||'Default';
          _applyReasoningChip((st&&st.reasoning_effort)||effort, st||{});
          showToast('🧠 Reasoning effort set to '+display);
        })
        .catch(function(){showToast('🧠 Failed to set effort');});
      closeReasoningDropdown();
    }
  }
});

export {
  _normalizeReasoningEffort,
  _formatReasoningEffortLabel,
  _reasoningEffortContext,
  _reasoningEffortQuery,
  _applyReasoningOptions,
  _applyReasoningChip,
  fetchReasoningChip,
  refreshProfileTransitionReasoningChip,
  clearProfileTransitionReasoningContext,
  syncReasoningChip,
  _highlightReasoningOption,
  toggleReasoningDropdown,
  _positionReasoningDropdown,
  closeReasoningDropdown,
  _currentReasoningEffort,
  _currentReasoningEffortsSupported,
  _currentReasoningToggleSupported,
  _profileTransitionReasoningContext,
  _lastReasoningFetchKey,
  _reasoningFetchSeq,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _normalizeReasoningEffort: { enumerable: true, get: () => _normalizeReasoningEffort, set: (value) => { _normalizeReasoningEffort = value; } },
  _formatReasoningEffortLabel: { enumerable: true, get: () => _formatReasoningEffortLabel, set: (value) => { _formatReasoningEffortLabel = value; } },
  _reasoningEffortContext: { enumerable: true, get: () => _reasoningEffortContext, set: (value) => { _reasoningEffortContext = value; } },
  _reasoningEffortQuery: { enumerable: true, get: () => _reasoningEffortQuery, set: (value) => { _reasoningEffortQuery = value; } },
  _applyReasoningOptions: { enumerable: true, get: () => _applyReasoningOptions, set: (value) => { _applyReasoningOptions = value; } },
  _applyReasoningChip: { enumerable: true, get: () => _applyReasoningChip, set: (value) => { _applyReasoningChip = value; } },
  fetchReasoningChip: { enumerable: true, get: () => fetchReasoningChip, set: (value) => { fetchReasoningChip = value; } },
  refreshProfileTransitionReasoningChip: { enumerable: true, get: () => refreshProfileTransitionReasoningChip, set: (value) => { refreshProfileTransitionReasoningChip = value; } },
  clearProfileTransitionReasoningContext: { enumerable: true, get: () => clearProfileTransitionReasoningContext, set: (value) => { clearProfileTransitionReasoningContext = value; } },
  syncReasoningChip: { enumerable: true, get: () => syncReasoningChip, set: (value) => { syncReasoningChip = value; } },
  _highlightReasoningOption: { enumerable: true, get: () => _highlightReasoningOption, set: (value) => { _highlightReasoningOption = value; } },
  toggleReasoningDropdown: { enumerable: true, get: () => toggleReasoningDropdown, set: (value) => { toggleReasoningDropdown = value; } },
  _positionReasoningDropdown: { enumerable: true, get: () => _positionReasoningDropdown, set: (value) => { _positionReasoningDropdown = value; } },
  closeReasoningDropdown: { enumerable: true, get: () => closeReasoningDropdown, set: (value) => { closeReasoningDropdown = value; } },
  _currentReasoningEffort: { enumerable: true, get: () => _currentReasoningEffort, set: (value) => { _currentReasoningEffort = value; } },
  _currentReasoningEffortsSupported: { enumerable: true, get: () => _currentReasoningEffortsSupported, set: (value) => { _currentReasoningEffortsSupported = value; } },
  _currentReasoningToggleSupported: { enumerable: true, get: () => _currentReasoningToggleSupported, set: (value) => { _currentReasoningToggleSupported = value; } },
  _profileTransitionReasoningContext: { enumerable: true, get: () => _profileTransitionReasoningContext, set: (value) => { _profileTransitionReasoningContext = value; } },
  _lastReasoningFetchKey: { enumerable: true, get: () => _lastReasoningFetchKey, set: (value) => { _lastReasoningFetchKey = value; } },
  _reasoningFetchSeq: { enumerable: true, get: () => _reasoningFetchSeq, set: (value) => { _reasoningFetchSeq = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
