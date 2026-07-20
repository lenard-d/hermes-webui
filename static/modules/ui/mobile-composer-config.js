import { closeModelDropdown, closeReasoningDropdown } from './model-selection.js';
import { closeToolsetsDropdown } from './toolsets-controls.js';
import { $ } from './state.js';

function _syncMobileComposerConfigButton(open){
  const btn=$('composerMobileConfigBtn');
  if(!btn) return;
  btn.classList.toggle('active',!!open);
  btn.setAttribute('aria-expanded',open?'true':'false');
}

function closeMobileComposerConfig(){
  const panel=$('composerMobileConfigPanel');
  if(panel) panel.classList.remove('open');
  _syncMobileComposerConfigButton(false);
  if(typeof closeWsDropdown==='function') closeWsDropdown();
}

function openMobileComposerConfig(){
  const panel=$('composerMobileConfigPanel');
  if(!panel) return;
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  closeReasoningDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  panel.classList.add('open');
  _syncMobileComposerConfigButton(true);
}

function toggleMobileComposerConfig(){
  const panel=$('composerMobileConfigPanel');
  if(!panel) return;
  const open=panel.classList.contains('open');
  if(open){
    closeMobileComposerConfig();
    closeModelDropdown();
    closeReasoningDropdown();
    if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
    return;
  }
  openMobileComposerConfig();
}

function openComposerContextMenu(e){
  if(e){
    e.preventDefault();
    e.stopPropagation();
  }
  const tooltip=$('ctxTooltip');
  if(tooltip){
    tooltip.classList.remove('ctx-tooltip-active');
    tooltip.setAttribute('aria-hidden','true');
  }
  openMobileComposerConfig();
}
window.openComposerContextMenu=openComposerContextMenu;

document.addEventListener('click',function(e){
  if(
    e.target.closest('#composerMobileConfigBtn') ||
    e.target.closest('#composerMobileConfigPanel') ||
    e.target.closest('#composerWsDropdown') ||
    e.target.closest('#composerModelDropdown') ||
    e.target.closest('#composerReasoningDropdown')
  ) return;
  closeMobileComposerConfig();
});

document.addEventListener('keydown',function(e){
  if(e.key!=='Escape') return;
  const panel=$('composerMobileConfigPanel');
  if(!panel||!panel.classList.contains('open')) return;
  e.preventDefault();
  closeMobileComposerConfig();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  closeReasoningDropdown();
});

window.addEventListener('resize',function(){
  if(window.matchMedia && !window.matchMedia('(max-width: 640px)').matches){
    closeMobileComposerConfig();
    closeModelDropdown();
    closeReasoningDropdown();
    if(typeof closeWsDropdown==='function') closeWsDropdown();
  }
});


export {
  _syncMobileComposerConfigButton,
  closeMobileComposerConfig,
  openMobileComposerConfig,
  toggleMobileComposerConfig,
  openComposerContextMenu,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _syncMobileComposerConfigButton: { enumerable: true, get: () => _syncMobileComposerConfigButton, set: (value) => { _syncMobileComposerConfigButton = value; } },
  closeMobileComposerConfig: { enumerable: true, get: () => closeMobileComposerConfig, set: (value) => { closeMobileComposerConfig = value; } },
  openMobileComposerConfig: { enumerable: true, get: () => openMobileComposerConfig, set: (value) => { openMobileComposerConfig = value; } },
  toggleMobileComposerConfig: { enumerable: true, get: () => toggleMobileComposerConfig, set: (value) => { toggleMobileComposerConfig = value; } },
  openComposerContextMenu: { enumerable: true, get: () => openComposerContextMenu, set: (value) => { openComposerContextMenu = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
