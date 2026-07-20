import { getModelLabel } from './activity-and-scroll.js';
import { closeOtherComposerMenus, registerComposerMenu } from './composer-menu-registry.js';
import { _positionModelDropdown, _restoreModelDropdownHome, syncModelChip } from './model-catalog.js';
import { renderModelPicker } from './model-picker-rendering.js';
import { MODEL_PICKER_REFRESH_EVENT, _ensureModelOptionInDropdown, _modelStateForSelect } from './model-state.js';
import { $ } from './state.js';

function renderModelDropdown(){
  const opts=arguments[0]||{};
  return renderModelPicker(Object.assign({},opts,{
    selectModel:typeof opts.selectModel==='function'
      ?opts.selectModel
      :(value,provider)=>selectModelFromDropdown(value,provider),
    closeDropdown:typeof opts.closeDropdown==='function'
      ?opts.closeDropdown
      :closeModelDropdown,
  }));
}

async function selectModelFromDropdown(value){
  const preferredProviderId=arguments[1];
  const sel=$('modelSelect');
  if(!sel) { closeModelDropdown(); return; }
  const provider=String(preferredProviderId||'').trim()||null;
  const currentState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel, sel.value)
    : {model:sel.value,model_provider:null};
  const sameModel=String(currentState.model||'')===String(value||'');
  const sameProvider=String(currentState.model_provider||'')===String(provider||'');
  if(sameModel&&sameProvider){ closeModelDropdown(); return; }
  // Resolve the provider-specific option so duplicate bare IDs (e.g. gpt-5.5
  // under OpenAI Codex vs OpenRouter) update session model_provider correctly.
  if(typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(value, sel, provider);
  }else{
    sel.value=value;
  }
  syncModelChip();
  closeModelDropdown();
  if(typeof sel.onchange==='function') await sel.onchange();
}

async function toggleModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const sel=$('modelSelect');
  if(!dd||!chip||!sel) return;
  const open=dd.classList.contains('open');
  if(open){closeModelDropdown(); return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeOtherComposerMenus('model');
  if(typeof window._ensureModelDropdownReady==='function'){
    const ready=window._ensureModelDropdownReady({freshness:'session_visit'});
    if(ready&&typeof ready.catch==='function') ready.catch(()=>{});
  }
  if(dd.classList.contains('open')) return;
  renderModelDropdown();
  dd.classList.add('open');
  _positionModelDropdown();
  const activeRow=dd.querySelector('.model-opt.active');
  if(activeRow&&typeof activeRow.scrollIntoView==='function') activeRow.scrollIntoView({block:'nearest'});
  chip.classList.add('active');
  const mobileAction=$('composerMobileModelAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function closeModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const mobileAction=$('composerMobileModelAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
  // If the phone path reparented the menu onto <body>, put it back in the
  // footer and clear the fixed-position inline styles so the DOM returns to its
  // baseline shape and the next desktop open anchors correctly (#6080).
  if(typeof _restoreModelDropdownHome==='function') _restoreModelDropdownHome();
}
registerComposerMenu('model',closeModelDropdown);

function closeSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  const chip=$('settingsModelChip');
  if(dd) dd.classList.remove('open');
  if(chip){
    chip.classList.remove('active');
    chip.setAttribute('aria-expanded','false');
  }
}

function syncSettingsModelChip(){
  const sel=$('settingsModel');
  const chip=$('settingsModelChip');
  if(!sel||!chip) return;
  const opt=sel.selectedOptions&&sel.selectedOptions[0];
  const text=(opt&&opt.textContent)||getModelLabel(sel.value||'')||t('settings_label_model')||'Default Model';
  chip.textContent=text;
  chip.title=sel.value||text;
}

function selectSettingsModelFromDropdown(value,preferredProviderId){
  const sel=$('settingsModel');
  if(!sel){closeSettingsModelDropdown();return;}
  const provider=String(preferredProviderId||'').trim()||null;
  if(typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(value,sel,provider);
  }else{
    sel.value=value;
    if(typeof syncSettingsModelChip==='function') syncSettingsModelChip();
  }
  closeSettingsModelDropdown();
  try{
    if(typeof Event==='function') sel.dispatchEvent(new Event('change',{bubbles:true}));
    else if(typeof sel.onchange==='function') sel.onchange();
  }catch(_){}
}

function openSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  const sel=$('settingsModel');
  const chip=$('settingsModelChip');
  if(!dd||!sel) return;
  // Auto-focus the search on desktop only. On touch (coarse pointer) grabbing focus
  // pops the on-screen keyboard the instant the chip is tapped — the composer picker
  // doesn't do it either, so match that behavior on touch. Computed before render so
  // renderModelDropdown's own initial focus is suppressed too (not just the outer one).
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
  dd.classList.add('open');
  if(chip){
    chip.classList.add('active');
    chip.setAttribute('aria-expanded','true');
  }
  if(!_coarsePointer){
    setTimeout(()=>{
      const input=dd.querySelector('.model-search-input');
      if(input) input.focus();
    },0);
  }
}

function toggleSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  if(dd&&dd.classList.contains('open')){closeSettingsModelDropdown();return;}
  openSettingsModelDropdown();
}

function mountSettingsModelPicker(){
  const chip=$('settingsModelChip');
  const sel=$('settingsModel');
  if(!chip||!sel) return;
  syncSettingsModelChip();
  if(!chip._settingsModelPickerBound){
    chip._settingsModelPickerBound=true;
    chip.addEventListener('click',e=>{
      e.preventDefault();
      e.stopPropagation();
      toggleSettingsModelDropdown();
    });
    chip.addEventListener('keydown',e=>{
      if(e.key==='Enter'||e.key===' '||e.key==='ArrowDown'){
        e.preventDefault();
        toggleSettingsModelDropdown();
      }
    });
  }
}

function refreshOpenModelPickers(){
  syncModelChip();
  syncSettingsModelChip();
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList.contains('open')){
    renderModelDropdown();
    _positionModelDropdown();
  }
  const settingsDropdown=$('settingsModelDropdown');
  if(settingsDropdown&&settingsDropdown.classList.contains('open')){
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

document.addEventListener(MODEL_PICKER_REFRESH_EVENT,refreshOpenModelPickers);

document.addEventListener('click',e=>{
  if(
    !e.target.closest('#composerModelChip') &&
    !e.target.closest('#composerMobileModelAction') &&
    !e.target.closest('#composerModelDropdown')
  ) closeModelDropdown();
  if(
    !e.target.closest('#settingsModelChip') &&
    !e.target.closest('#settingsModel') &&
    !e.target.closest('#settingsModelDropdown')
  ) closeSettingsModelDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList.contains('open')) _positionModelDropdown();
});

// visualViewport resize/scroll fire on mobile when the on-screen keyboard opens
// or the URL bar collapses/expands — the phone dropdown is fixed to the visual
// viewport, so it must be re-measured against the new offsets. Coalesce with rAF
// so a burst of scroll/resize events triggers at most one reposition per frame.
let _modelDropdownRepositionScheduled=false;
function _repositionOpenModelDropdown(){
  const dd=$('composerModelDropdown');
  if(!(dd&&dd.classList.contains('open'))||_modelDropdownRepositionScheduled) return;
  _modelDropdownRepositionScheduled=true;
  requestAnimationFrame(()=>{
    _modelDropdownRepositionScheduled=false;
    const openDd=$('composerModelDropdown');
    if(openDd&&openDd.classList.contains('open')) _positionModelDropdown();
  });
}
if(window.visualViewport){
  window.visualViewport.addEventListener('resize',_repositionOpenModelDropdown);
  window.visualViewport.addEventListener('scroll',_repositionOpenModelDropdown);
}

export {
  renderModelDropdown,
  closeModelDropdown,
  closeSettingsModelDropdown,
  syncSettingsModelChip,
  selectSettingsModelFromDropdown,
  openSettingsModelDropdown,
  toggleSettingsModelDropdown,
  mountSettingsModelPicker,
  refreshOpenModelPickers,
  _repositionOpenModelDropdown,
  selectModelFromDropdown,
  toggleModelDropdown,
  _modelDropdownRepositionScheduled,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderModelDropdown: { enumerable: true, get: () => renderModelDropdown, set: (value) => { renderModelDropdown = value; } },
  closeModelDropdown: { enumerable: true, get: () => closeModelDropdown, set: (value) => { closeModelDropdown = value; } },
  closeSettingsModelDropdown: { enumerable: true, get: () => closeSettingsModelDropdown, set: (value) => { closeSettingsModelDropdown = value; } },
  syncSettingsModelChip: { enumerable: true, get: () => syncSettingsModelChip, set: (value) => { syncSettingsModelChip = value; } },
  selectSettingsModelFromDropdown: { enumerable: true, get: () => selectSettingsModelFromDropdown, set: (value) => { selectSettingsModelFromDropdown = value; } },
  openSettingsModelDropdown: { enumerable: true, get: () => openSettingsModelDropdown, set: (value) => { openSettingsModelDropdown = value; } },
  toggleSettingsModelDropdown: { enumerable: true, get: () => toggleSettingsModelDropdown, set: (value) => { toggleSettingsModelDropdown = value; } },
  mountSettingsModelPicker: { enumerable: true, get: () => mountSettingsModelPicker, set: (value) => { mountSettingsModelPicker = value; } },
  refreshOpenModelPickers: { enumerable: true, get: () => refreshOpenModelPickers, set: (value) => { refreshOpenModelPickers = value; } },
  _repositionOpenModelDropdown: { enumerable: true, get: () => _repositionOpenModelDropdown, set: (value) => { _repositionOpenModelDropdown = value; } },
  selectModelFromDropdown: { enumerable: true, get: () => selectModelFromDropdown, set: (value) => { selectModelFromDropdown = value; } },
  toggleModelDropdown: { enumerable: true, get: () => toggleModelDropdown, set: (value) => { toggleModelDropdown = value; } },
  _modelDropdownRepositionScheduled: { enumerable: true, get: () => _modelDropdownRepositionScheduled, set: (value) => { _modelDropdownRepositionScheduled = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
