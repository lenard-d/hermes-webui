import { state } from "./state.js";
import { switchPanel } from "./core.js";
import { _scheduleAppearanceAutosave } from "./settings-navigation.js";

// Panels domain: composer drop handling and settings state

// Drag and drop
const wrap=$('composerWrap');let dragCounter=0;
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('dragenter',e=>{e.preventDefault();
  const isWsPath=e.dataTransfer.types.includes('application/ws-path');
  const isFiles=e.dataTransfer.types.includes('Files');
  if(isFiles||isWsPath){
    dragCounter++;
    // Context-aware hint: a workspace-file drag inserts an @path reference;
    // an OS-file drag attaches the file to the message.
    const hint=$('dropHintText');
    if(hint) hint.textContent=isWsPath?'Drop to insert workspace reference':'Drop files to attach';
    wrap.classList.add('drag-over');
  }
});
document.addEventListener('dragleave',e=>{dragCounter--;if(dragCounter<=0){dragCounter=0;wrap.classList.remove('drag-over');}});
document.addEventListener('drop',e=>{
  e.preventDefault();dragCounter=0;wrap.classList.remove('drag-over');
  // Workspace file/folder drag → insert @path reference into composer
  const wsPath=e.dataTransfer.getData('application/ws-path');
  if(wsPath){
    const msgEl=$('msg');
    if(msgEl){
      const start=msgEl.selectionStart;const end=msgEl.selectionEnd;
      const val=msgEl.value;
      const prefix=start>0&&!val[start-1].match(/\s/)?' ':'';
      const insert=prefix+'@'+wsPath+' ';
      msgEl.value=val.slice(0,start)+insert+val.slice(end);
      msgEl.selectionStart=msgEl.selectionEnd=start+insert.length;
      msgEl.focus();
    }
    return;
  }
  // OS file drag → attach files
  const files=Array.from(e.dataTransfer.files);
  if(files.length){addFiles(files);$('msg').focus();}
});

// ── Settings panel ───────────────────────────────────────────────────────────

// ── Sidebar tab visibility/order ────────────────────────────────────────────
const _ALWAYS_VISIBLE_TABS = new Set(['chat','settings']);
const _HIDDEN_TABS_LS_KEY = 'hermes-webui-hidden-tabs';
const _TAB_ORDER_LS_KEY = 'hermes-webui-tab-order';
const _COMPOSER_CONTROL_ORDER_LS_KEY = 'hermes-webui-composer-control-order';

export function _sanitizeTabPanelList(panels){
  if(!Array.isArray(panels)) return [];
  var out=[];
  panels.forEach(function(panel){
    if(typeof panel!=='string') return;
    panel=panel.trim();
    if(!panel||_ALWAYS_VISIBLE_TABS.has(panel)) return;
    if(out.indexOf(panel)===-1) out.push(panel);
  });
  return out;
}

export function _getHiddenTabs(){
  try{var h=localStorage.getItem(_HIDDEN_TABS_LS_KEY);if(h)return _sanitizeTabPanelList(JSON.parse(h));}catch(e){}
  return[];
}

export function _setHiddenTabs(panels){
  try{localStorage.setItem(_HIDDEN_TABS_LS_KEY,JSON.stringify(_sanitizeTabPanelList(panels)));}catch(e){}
}

export function _getTabOrder(){
  try{var h=localStorage.getItem(_TAB_ORDER_LS_KEY);if(h)return _sanitizeTabPanelList(JSON.parse(h));}catch(e){}
  return[];
}

export function _setTabOrder(panels){
  try{localStorage.setItem(_TAB_ORDER_LS_KEY,JSON.stringify(_sanitizeTabPanelList(panels)));}catch(e){}
}

export function _availableSidebarPanels(){
  var out=[];
  var tabs=document.querySelectorAll('.rail .rail-btn.nav-tab[data-panel], .sidebar-nav .nav-tab[data-panel]');
  tabs.forEach(function(tab){
    var panel=tab.dataset.panel;
    if(!panel||_ALWAYS_VISIBLE_TABS.has(panel)) return;
    if(tab.classList.contains('dashboard-link')||tab.hasAttribute('data-dashboard-link')) return;
    if(out.indexOf(panel)===-1) out.push(panel);
  });
  return out;
}

export function _orderedSidebarPanels(order){
  var available=_availableSidebarPanels();
  var requested=_sanitizeTabPanelList(Array.isArray(order)?order:_getTabOrder());
  var out=[];
  requested.forEach(function(panel){ if(available.indexOf(panel)!==-1&&out.indexOf(panel)===-1) out.push(panel); });
  available.forEach(function(panel){ if(out.indexOf(panel)===-1) out.push(panel); });
  return out;
}

export function _dashboardPanelMode(){
  var modeEl=$('settingsDashboardMode');
  var mode=modeEl&&modeEl.value;
  return mode==='never'||mode==='always'||mode==='auto'?mode:'auto';
}

export function _isDashboardChipOn(){
  return _dashboardPanelMode()!=='never';
}

export function _renderDashboardVisibilityChip(container){
  if(!container)return null;
  var chip=document.createElement('button');
  chip.type='button';
  chip.className='tab-visibility-chip';
  chip.setAttribute('data-tab-panel','__hermes_dashboard__');
  chip.setAttribute('role','switch');
  var isOn=_isDashboardChipOn();
  chip.setAttribute('aria-checked',isOn?'true':'false');
  if(!isOn) chip.classList.add('chip-off');
  chip.textContent=typeof t==='function'?t('tab_dashboard'):'Dashboard';
  chip.onclick=function(){
    if(Date.now()<state._tabVisibilityDragSuppressUntil)return;
    _toggleDashboardVisibilityChip();
  };
  return chip;
}

export function _applyTabOrder(order){
  var ordered=_orderedSidebarPanels(order);
  ['.rail','.sidebar-nav'].forEach(function(selector){
    var container=document.querySelector(selector);
    if(!container) return;
    var anchor=Array.prototype.find.call(container.children,function(child){
      if(child.classList&&child.classList.contains('rail-spacer')) return true;
      if(child.classList&&child.classList.contains('dashboard-link')) return true;
      if(child.hasAttribute&&child.hasAttribute('data-dashboard-link')) return true;
      return child.dataset&&child.dataset.panel==='settings';
    });
    ordered.forEach(function(panel){
      var node=container.querySelector('.nav-tab[data-panel="'+panel+'"]');
      if(node) container.insertBefore(node,anchor||null);
    });
  });
}

export function _applyTabVisibility(hidden){
  hidden=_sanitizeTabPanelList(hidden);
  _applyTabOrder(_getTabOrder());
  // Hide/unhide all [data-panel] elements (sidebar-nav buttons + rail buttons)
  document.querySelectorAll('[data-panel]').forEach(function(el){
    var panel=el.dataset.panel;
    if(!panel)return;
    var shouldHide=hidden.indexOf(panel)!==-1;
    // Never hide always-visible panels (chat, settings) even if present in hidden_tabs
    if(_ALWAYS_VISIBLE_TABS.has(panel)) shouldHide=false;
    el.classList.toggle('nav-tab-hidden',shouldHide);
  });
  // If the currently active tab is hidden, switch to chat
  var activeRail=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]');
  var activeSidebar=document.querySelector('.sidebar-nav .nav-tab.active[data-panel]');
  var activeEl=activeRail||activeSidebar;
  if(activeEl&&activeEl.classList.contains('nav-tab-hidden')){
    if(typeof switchPanel==='function') switchPanel('chat');
  }
}

export function _renderTabVisibilityChips(){
  var container=$('tabVisibilityChips');
  if(!container)return;
  var hidden=_getHiddenTabs();
  var panels=_orderedSidebarPanels();
  container.innerHTML='';
  panels.forEach(function(panel){
    var tab=document.querySelector('.rail .rail-btn.nav-tab[data-panel="'+panel+'"]')
      ||document.querySelector('.sidebar-nav .nav-tab[data-panel="'+panel+'"]');
    var label=(tab&&(tab.dataset.tooltip||tab.dataset.label))||panel;
    // Capitalize first letter
    label=label.charAt(0).toUpperCase()+label.slice(1);
    var chip=document.createElement('button');
    chip.type='button';
    chip.className='tab-visibility-chip';
    var isOff=hidden.indexOf(panel)!==-1;
    if(isOff)chip.classList.add('chip-off');
    chip.textContent=label;
    chip.setAttribute('data-tab-panel',panel);
    chip.setAttribute('draggable','true');
    // Use role="switch" + aria-checked instead of aria-pressed so screen
    // readers narrate "Tasks switch on/off" (matches user mental model) rather
    // than "Tasks toggle button pressed/not-pressed" (where the polarity is
    // confusing because chip-off looks like the "off" state).
    chip.setAttribute('role','switch');
    chip.setAttribute('aria-checked',isOff?'false':'true');
    chip.onclick=function(){
      if(Date.now()<state._tabVisibilityDragSuppressUntil)return;
      _toggleTabVisibilityChip(panel);
    };
    _wireTabChipDrag(chip,panel);
    container.appendChild(chip);
  });
  var dashboardChip=_renderDashboardVisibilityChip(container);
  if(dashboardChip) container.appendChild(dashboardChip);
}

export function _wireTabChipDrag(chip,panel){
  if(!chip)return;
  chip.addEventListener('dragstart',function(e){
    chip.classList.add('dragging');
    if(e.dataTransfer){
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain',panel);
    }
  });
  chip.addEventListener('dragend',function(){chip.classList.remove('dragging');});
  chip.addEventListener('dragover',function(e){e.preventDefault();chip.classList.add('drag-over');if(e.dataTransfer)e.dataTransfer.dropEffect='move';});
  chip.addEventListener('dragleave',function(){chip.classList.remove('drag-over');});
  chip.addEventListener('drop',function(e){_handleTabVisibilityChipDrop(e,panel);});
}

export function _moveTabOrderPanel(sourcePanel,targetPanel){
  if(!sourcePanel||!targetPanel||sourcePanel===targetPanel) return false;
  var order=_orderedSidebarPanels();
  var from=order.indexOf(sourcePanel);
  var to=order.indexOf(targetPanel);
  if(from===-1||to===-1) return false;
  order.splice(from,1);
  order.splice(to,0,sourcePanel);
  _setTabOrder(order);
  _applyTabOrder(order);
  _renderTabVisibilityChips();
  _scheduleAppearanceAutosave();
  return true;
}

export function _handleTabVisibilityChipDrop(e,targetPanel){
  if(e){e.preventDefault();e.stopPropagation();}
  document.querySelectorAll('.tab-visibility-chip.drag-over').forEach(function(el){el.classList.remove('drag-over');});
  var sourcePanel=e&&e.dataTransfer?e.dataTransfer.getData('text/plain'):'';
  if(_moveTabOrderPanel(sourcePanel,targetPanel)) state._tabVisibilityDragSuppressUntil=Date.now()+250;
}

export function _toggleTabVisibilityChip(panel){
  if(_ALWAYS_VISIBLE_TABS.has(panel))return;
  var hidden=_getHiddenTabs();
  var idx=hidden.indexOf(panel);
  if(idx!==-1){
    hidden.splice(idx,1);
  }else{
    hidden.push(panel);
  }
  _setHiddenTabs(hidden);
  _applyTabVisibility(hidden);
  _renderTabVisibilityChips();
  _scheduleAppearanceAutosave();
}

export function _toggleDashboardVisibilityChip(){
  var modeEl=$('settingsDashboardMode');
  if(!modeEl||typeof saveDashboardSettings!=='function') return;
  var currentMode=_dashboardPanelMode();
  var nextMode=currentMode==='never'
    ? (typeof _getDashboardChipRestoreMode==='function' ? _getDashboardChipRestoreMode() : 'auto')
    : 'never';
  var previousMode=currentMode;
  modeEl.value=nextMode;
  Promise.resolve(saveDashboardSettings({raiseOnError:true})).catch(function(){
    modeEl.value=previousMode;
    if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();
  });
}

export function _ensureComposerControlVisibilityState(settings){
  const fromSettings=(typeof _composerControlVisibilityFromSettings==='function')
    ? _composerControlVisibilityFromSettings(settings||{})
    : {};
  if(!window._composerControlVisibility) window._composerControlVisibility={};
  Object.assign(window._composerControlVisibility, fromSettings);
}

export function _composerControlDefsForSettings(){
  const baseDefs=Array.isArray(window._COMPOSER_CONTROL_TOGGLE_DEFS)?window._COMPOSER_CONTROL_TOGGLE_DEFS:[];
  const situationalDefs=Array.isArray(window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS)?window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS:[];
  return baseDefs.concat(situationalDefs);
}

export function _getComposerControlOrder(){
  if(Array.isArray(window._composerControlOrder)){
    return typeof window._sanitizeComposerControlOrder==='function'
      ? window._sanitizeComposerControlOrder(window._composerControlOrder)
      : window._composerControlOrder.slice();
  }
  try{
    const raw=localStorage.getItem(_COMPOSER_CONTROL_ORDER_LS_KEY);
    if(raw){
      const parsed=JSON.parse(raw);
      if(typeof window._sanitizeComposerControlOrder==='function') return window._sanitizeComposerControlOrder(parsed);
      if(Array.isArray(parsed)) return parsed.filter(key=>typeof key==='string');
    }
  }catch(e){}
  return [];
}

export function _setComposerControlOrder(order){
  const sanitized=typeof window._sanitizeComposerControlOrder==='function'
    ? window._sanitizeComposerControlOrder(order)
    : (Array.isArray(order)?order.filter(key=>typeof key==='string') : []);
  window._composerControlOrder=sanitized;
  try{localStorage.setItem(_COMPOSER_CONTROL_ORDER_LS_KEY,JSON.stringify(sanitized));}catch(e){}
  return sanitized;
}

export function _orderedComposerControlDefsForSettings(defs){
  defs=Array.isArray(defs)?defs:[];
  const byKey=new Map(defs.map(def=>[def.key,def]));
  const out=[];
  _getComposerControlOrder().forEach(function(key){
    if(byKey.has(key)) out.push(byKey.get(key));
  });
  defs.forEach(function(def){if(out.indexOf(def)===-1) out.push(def);});
  return out;
}

export function _composerControlOrderGroupKey(key){
  const def=_composerControlDefsForSettings().find(item=>item&&item.key===key);
  return def&&def.orderGroup?def.orderGroup:'';
}

export function _composerControlDropAllowed(sourceKey,targetKey){
  if(!sourceKey||!targetKey||sourceKey===targetKey) return false;
  const sourceGroup=_composerControlOrderGroupKey(sourceKey);
  const targetGroup=_composerControlOrderGroupKey(targetKey);
  return !!sourceGroup&&sourceGroup===targetGroup;
}

export function _clearComposerControlDragOver(){
  document.querySelectorAll('[data-composer-control-key].drag-over').forEach(function(el){el.classList.remove('drag-over');});
}

export function _moveComposerControlOrderKey(sourceKey,targetKey){
  if(!_composerControlDropAllowed(sourceKey,targetKey)) return false;
  const order=_orderedComposerControlDefsForSettings(_composerControlDefsForSettings()).map(def=>def.key);
  const from=order.indexOf(sourceKey);
  const to=order.indexOf(targetKey);
  if(from===-1||to===-1) return false;
  order.splice(from,1);
  order.splice(to,0,sourceKey);
  const next=_setComposerControlOrder(order);
  if(typeof window._applyComposerControlOrder==='function') window._applyComposerControlOrder(next);
  _renderComposerControlChips();
  _renderComposerSituationalControlChips();
  _scheduleAppearanceAutosave();
  return true;
}

export function _handleComposerControlChipDrop(e,targetKey){
  if(e){e.preventDefault();e.stopPropagation();}
  _clearComposerControlDragOver();
  const sourceKey=e&&e.dataTransfer?e.dataTransfer.getData('text/plain'):state._composerControlDraggingKey;
  if(_moveComposerControlOrderKey(sourceKey,targetKey)) state._composerControlDragSuppressUntil=Date.now()+250;
  state._composerControlDraggingKey='';
}

export function _wireComposerControlChipDrag(chip,key){
  if(!chip)return;
  chip.setAttribute('data-composer-control-key',key);
  chip.setAttribute('draggable','true');
  chip.addEventListener('dragstart',function(e){
    state._composerControlDraggingKey=key;
    chip.classList.add('dragging');
    if(e.dataTransfer){
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain',key);
    }
  });
  chip.addEventListener('dragend',function(){
    chip.classList.remove('dragging');
    _clearComposerControlDragOver();
    state._composerControlDraggingKey='';
  });
  chip.addEventListener('dragover',function(e){
    const sourceKey=state._composerControlDraggingKey;
    if(!_composerControlDropAllowed(sourceKey,key)){
      if(e.dataTransfer)e.dataTransfer.dropEffect='none';
      return;
    }
    e.preventDefault();
    chip.classList.add('drag-over');
    if(e.dataTransfer)e.dataTransfer.dropEffect='move';
  });
  chip.addEventListener('dragleave',function(){chip.classList.remove('drag-over');});
  chip.addEventListener('drop',function(e){_handleComposerControlChipDrop(e,key);});
}

export function _composerControlVisibilityPayload(){
  const payload={};
  const defs=_composerControlDefsForSettings();
  const state=window._composerControlVisibility||{};
  defs.forEach(function(def){payload[def.key]=!!state[def.key];});
  return payload;
}

export function _toggleComposerControlChip(key){
  if(!window._composerControlVisibility) window._composerControlVisibility={};
  window._composerControlVisibility[key]=!window._composerControlVisibility[key];
  if(typeof _renderComposerControlChips==='function') _renderComposerControlChips();
  if(typeof _renderComposerSituationalControlChips==='function') _renderComposerSituationalControlChips();
  if(typeof _applyComposerFooterVisibilitySettings==='function') _applyComposerFooterVisibilitySettings();
  _scheduleAppearanceAutosave();
}

export function _composerControlChipLabel(def){
  if(!def) return '';
  if(def.labelKey&&typeof t==='function'){
    const localized=t(def.labelKey);
    if(typeof localized==='string'&&localized&&localized!==def.labelKey) return localized;
  }
  return def.label||'';
}

export function _renderComposerControlChips(){
  const container=$('composerControlsChips');
  if(!container) return;
  const defs=Array.isArray(window._COMPOSER_CONTROL_TOGGLE_DEFS)?window._COMPOSER_CONTROL_TOGGLE_DEFS:[];
  const state=window._composerControlVisibility||{};
  container.innerHTML='';
  _orderedComposerControlDefsForSettings(defs).forEach(function(def){
    const chip=document.createElement('button');
    chip.type='button';
    chip.className='tab-visibility-chip';
    const hidden=!!state[def.key];
    if(hidden) chip.classList.add('chip-off');
    chip.textContent=_composerControlChipLabel(def);
    chip.setAttribute('role','switch');
    chip.setAttribute('aria-checked',hidden?'false':'true');
    chip.onclick=function(){if(Date.now()<state._composerControlDragSuppressUntil)return;_toggleComposerControlChip(def.key);};
    _wireComposerControlChipDrag(chip,def.key);
    container.appendChild(chip);
  });
}

export function _renderComposerSituationalControlChips(){
  const container=$('composerSituationalControlsChips');
  if(!container) return;
  const defs=Array.isArray(window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS)?window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS:[];
  const state=window._composerControlVisibility||{};
  container.innerHTML='';
  _orderedComposerControlDefsForSettings(defs).forEach(function(def){
    const chip=document.createElement('button');
    chip.type='button';
    chip.className='tab-visibility-chip';
    const hidden=!!state[def.key];
    if(hidden) chip.classList.add('chip-off');
    chip.textContent=_composerControlChipLabel(def);
    chip.setAttribute('role','switch');
    chip.setAttribute('aria-checked',hidden?'false':'true');
    chip.onclick=function(){if(Date.now()<state._composerControlDragSuppressUntil)return;_toggleComposerControlChip(def.key);};
    _wireComposerControlChipDrag(chip,def.key);
    container.appendChild(chip);
  });
}
