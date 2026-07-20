window.HermesBoot.begin('shellNavigation');
// ── Mobile navigation ──────────────────────────────────────────────────────
// URL prefill boot helpers.
function _prefillHasDraftText(prefillIntent){
  return !!(prefillIntent&&prefillIntent.hasText);
}
function _rootPrefillNeedsFreshComposer(urlSession, savedLocal, prefillIntent){
  return !urlSession&&!!savedLocal&&_prefillHasDraftText(prefillIntent);
}
function _profileQueryBlocksSavedLocalRestore(profileIntent, urlSession){
  return !!(profileIntent&&profileIntent.hasParam&&profileIntent.valid&&!urlSession);
}
async function _applyComposerPrefillOnBoot(prefillIntent){
  if(!prefillIntent||!prefillIntent.hasText) return;
  const msg=(typeof $==='function')?$('msg'):document.getElementById('msg');
  if(!msg) return;
  const text=String(prefillIntent.text||'');
  msg.value=text;
  if(typeof autoResize==='function') autoResize();
  else if(typeof updateSendBtn==='function') updateSendBtn();
  if(typeof msg.focus==='function') msg.focus();
}
async function _finalizeComposerPrefillOnBoot(prefillIntent){
  if(prefillIntent&&prefillIntent.hasParams&&typeof _consumeComposerPrefillParamsFromLocation==='function'){
    _consumeComposerPrefillParamsFromLocation();
  }
  await _applyComposerPrefillOnBoot(prefillIntent);
}

// Mobile navigation.
let _workspacePanelMode='closed'; // 'closed' | 'browse' | 'preview'

function _isCompactWorkspaceViewport(){
  return window.matchMedia('(max-width: 900px)').matches;
}

function _isPhoneWidthViewport(){
  return window.matchMedia('(max-width: 640px)').matches;
}

function _isTouchKeyboardViewport(){
  try{return matchMedia('(hover:none) and (pointer:coarse)').matches&&!_hasFinePointerCoexisting();}catch(_){return false;}
}

function _syncKeyboardBottomInset(){
  const root=document.documentElement;
  if(!root) return;
  if(!window.visualViewport||!_isTouchKeyboardViewport()){
    root.style.removeProperty('--keyboard-bottom-inset');
    return;
  }
  const vv=window.visualViewport;
  // A pinch-zoomed viewport (vv.scale != 1) makes innerHeight - vv.height
  // reflect the zoom, not the keyboard — on Chromium touch devices with
  // accessibility "force enable zoom" that yields a large spurious inset that
  // jitters on pan. Treat only the unzoomed state as keyboard occlusion.
  if(Math.abs((vv.scale||1)-1)>0.05){
    root.style.removeProperty('--keyboard-bottom-inset');
    return;
  }
  const inset=Math.max(0,Math.ceil(window.innerHeight-(vv.height+vv.offsetTop)));
  if(inset>0){
    root.style.setProperty('--keyboard-bottom-inset',`${inset}px`);
  }else{
    root.style.removeProperty('--keyboard-bottom-inset');
  }
}

// Mobile PWA viewport reflow guard. When the on-screen keyboard / browser
// chrome shows or hides, visualViewport (or a plain resize on browsers without
// it) changes height without a layout invalidation, leaving the phone layout
// painted against stale geometry. Toggling a one-frame `viewport-reflow` class
// (which applies a cheap GPU-promotion transform under the @media(max-width:640px)
// rule) forces a repaint, then we resync the workspace panel + sidebar aria.
function _forceMobileViewportReflow(){
  _syncKeyboardBottomInset();
  if(!_isPhoneWidthViewport()) return;
  const layout=document.querySelector('.layout');
  if(!layout) return;
  document.documentElement.classList.add('viewport-reflow');
  void layout.offsetWidth;
  requestAnimationFrame(()=>{
    document.documentElement.classList.remove('viewport-reflow');
    try{ syncWorkspacePanelState(); }catch(_){ }
    try{ if(typeof _syncSidebarAria==='function') _syncSidebarAria(); }catch(_){ }
  });
}

function _syncWorkspacePanelInlineWidth(){
  const {panel}= _workspacePanelEls();
  if(!panel) return;

  const isCompact = _isCompactWorkspaceViewport();
  if(isCompact){
    if(panel.style.width) panel.style.removeProperty('width');
    return;
  }

  const saved = localStorage.getItem('hermes-panel-w');
  if(!saved) return;
  const parsed = parseInt(saved, 10);
  if(Number.isNaN(parsed) || parsed <= 0) return;
  panel.style.width = `${parsed}px`;
}

function _workspacePanelEls(){
  return {
    layout: document.querySelector('.layout'),
    panel: document.querySelector('.rightpanel'),
    toggleBtn: $('btnWorkspacePanelToggle'),
    edgeToggleBtn: $('btnWorkspacePanelEdgeToggle'),
    collapseBtn: $('btnCollapseWorkspacePanel'),
  };
}

function _hasWorkspacePreviewVisible(){
  const preview=$('previewArea');
  return !!(preview&&preview.classList.contains('visible'));
}

function _setWorkspacePanelMode(mode){
  const {layout,panel}= _workspacePanelEls();
  if(!layout||!panel)return;
  _workspacePanelMode=(mode==='browse'||mode==='preview')?mode:'closed';
  const open=_workspacePanelMode!=='closed';
  document.documentElement.dataset.workspacePanel=open?'open':'closed';
  // Persist open/closed across refreshes (browse/preview → open; closed → closed)
  // Do NOT overwrite the user's "keep open" preference — only track runtime state
  // so that toggleWorkspacePanel(false) from the toolbar doesn't clear the setting.
  try{localStorage.setItem('hermes-webui-workspace-panel', open ? 'open' : 'closed');}catch(_){}
  layout.classList.toggle('workspace-panel-collapsed',!open);
  if(_isCompactWorkspaceViewport()){
    panel.classList.toggle('mobile-open',open);
  }else{
    panel.classList.remove('mobile-open');
  }
  syncWorkspacePanelUI();
}

function syncWorkspacePanelState(){
  const hasPreview=_hasWorkspacePreviewVisible();
  if(hasPreview){
    if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
    else syncWorkspacePanelUI();
    return;
  }
  if(!S.session){
    // No active session — if the panel was explicitly opened (browse mode), keep it
    // open so the workspace pane doesn't vanish on a fresh-page or empty-session boot.
    // The file tree will show the "no workspace" placeholder naturally via renderFileTree().
    // Only force-close if the mode is 'preview' (file preview without a session is invalid).
    if(_workspacePanelMode==='preview') _setWorkspacePanelMode('closed');
    else syncWorkspacePanelUI();
    return;
  }
  _setWorkspacePanelMode(_workspacePanelMode==='preview'?'closed':_workspacePanelMode);
}

function openWorkspacePanel(mode='browse'){
  if(mode==='browse'&&!S.session&&!_hasWorkspacePreviewVisible()&&!S._profileDefaultWorkspace)return;
  if(mode==='preview'&&_workspacePanelMode==='browse'){
    syncWorkspacePanelUI();
    return;
  }
  _setWorkspacePanelMode(mode);
}

function closeWorkspacePanel(){
  _setWorkspacePanelMode('closed');
}

function ensureWorkspacePreviewVisible(){
  if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
  else syncWorkspacePanelUI();
}

function handleWorkspaceClose(){
  if(_hasWorkspacePreviewVisible()){
    clearPreview();
    return;
  }
  closeWorkspacePanel();
}

async function _maybeBindFreshDefaultWorkspaceSession(prefillIntent=null){
  if(_prefillHasDraftText(prefillIntent)) return false;
  if(S.session) return false;
  if(_workspacePanelMode!=='browse') return false;
  if(!S._profileDefaultWorkspace) return false;
  try{
    // worktree:false is explicit and load-bearing — this auto-bind runs on
    // page load, and a config-level worktree default must never leak a fresh
    // worktree + branch from simply opening the UI (#6022).
    await newSession(false, {awaitWorkspaceLoad: true, worktree: false});
    return true;
  }catch(e){
    console.warn('[hermes] failed to bind fresh default workspace session', e);
    return false;
  }
}

/**
 * Set a tooltip on a button, preferring the custom CSS tooltip (`data-tooltip`)
 * when the element opts in via the `has-tooltip` class. Falls back to the
 * native `title` attribute for elements that haven't opted in.
 *
 * Critical: when the element DOES have data-tooltip, this MUST also clear any
 * existing native `title` attribute, otherwise the slow ~1.5s native browser
 * tooltip co-fires alongside the fast custom CSS tooltip — exactly the bug
 * #1775 reports. Always pair `data-tooltip` with `removeAttribute('title')`.
 */
function _setButtonTooltip(btn, text){
  if(!btn) return;
  if(btn.hasAttribute('data-tooltip')){
    btn.setAttribute('data-tooltip', text);
    if(btn.hasAttribute('title')) btn.removeAttribute('title');
  } else {
    btn.title = text;
  }
}

function _uiText(key, fallback){
  if(typeof t==='function'){
    const val=t(key);
    if(val&&val!==key) return val;
  }
  return fallback;
}

function syncWorkspacePanelUI(){
  const {layout,panel,toggleBtn,edgeToggleBtn,collapseBtn}= _workspacePanelEls();
  if(!layout||!panel)return;
  const desktopOpen=_workspacePanelMode!=='closed';
  const mobileOpen=panel.classList.contains('mobile-open');
  const isCompact=_isCompactWorkspaceViewport();
  const isOpen=isCompact?mobileOpen:desktopOpen;
  const canBrowse=!!S.session||_hasWorkspacePreviewVisible()||!!(S._profileDefaultWorkspace);
  const hasPreview=_hasWorkspacePreviewVisible();
  if(toggleBtn){
    toggleBtn.classList.toggle('active',isOpen);
    toggleBtn.setAttribute('aria-pressed',isOpen?'true':'false');
    const label=_uiText(isOpen?'workspace_panel_hide':'workspace_panel_show', isOpen?'Hide workspace panel':'Show workspace panel');
    _setButtonTooltip(toggleBtn, label);
    toggleBtn.setAttribute('aria-label', label);
    toggleBtn.disabled=!canBrowse;
  }
  if(edgeToggleBtn){
    edgeToggleBtn.classList.toggle('active',isOpen);
    edgeToggleBtn.setAttribute('aria-expanded',isOpen?'true':'false');
    const label=_uiText(isOpen?'workspace_panel_hide':'workspace_panel_show', isOpen?'Hide workspace panel':'Show workspace panel');
    _setButtonTooltip(edgeToggleBtn, label);
    edgeToggleBtn.setAttribute('aria-label', label);
    edgeToggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    _setButtonTooltip(collapseBtn, isCompact?_uiText('workspace_panel_close','Close workspace panel'):_uiText('workspace_panel_hide','Hide workspace panel'));
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    const label=hasPreview?_uiText('workspace_close_preview','Close preview'):_uiText('terminal_close','Close');
    _setButtonTooltip(clearBtn, label);
    clearBtn.setAttribute('aria-label', label);
    if(!isCompact) clearBtn.style.display='';
  }
}

function toggleMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  if(!sidebar)return;
  const isOpen=sidebar.classList.contains('mobile-open');
  if(isOpen){closeMobileSidebar();}
  else{
    try{if(typeof _syncMobileSidebarPanelFromMainView==='function')_syncMobileSidebarPanelFromMainView();}catch(_){}
    sidebar.classList.remove('mobile-session-page');sidebar.classList.add('mobile-panel-drawer','mobile-open');
  }
}
function closeMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(sidebar)sidebar.classList.remove('mobile-open','mobile-session-page','mobile-panel-drawer');
  if(overlay)overlay.classList.remove('visible');
}

const _PWA_SIDEBAR_SWIPE_EDGE=80;
const _PWA_SIDEBAR_SWIPE_CLAIM=10;
const _PWA_SIDEBAR_SWIPE_TRIGGER=64;
const _PWA_SIDEBAR_SWIPE_MAX_VERTICAL=56;
let _pwaSidebarSwipe=null;

function _isPwaStandalone(){
  try{
    return document.documentElement.classList.contains('pwa-standalone')
      || window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone===true;
  }catch(_){return false;}
}

function _isInteractiveSwipeTarget(target){
  try{return !!(target&&target.closest&&target.closest('input,textarea,select,button,a,[contenteditable="true"],.topbar-chips,.composer-left,.sidebar,.rightpanel'));}
  catch(_){return false;}
}

function _pwaSidebarSwipePoint(e){
  const touch=e&&e.touches&&e.touches[0]||e&&e.changedTouches&&e.changedTouches[0];
  const src=touch||e;
  if(!src)return null;
  return {clientX:Number(src.clientX)||0,clientY:Number(src.clientY)||0};
}

function _isTouchPointerEvent(e){
  return !!(e&&e.pointerType==='touch');
}

function _openMobileSidebarFromGesture(){
  if(_isDesktopWidth())return;
  const sidebar=document.querySelector('.sidebar');
  if(!sidebar)return;
  try{if(typeof _syncMobileSidebarPanelFromMainView==='function')_syncMobileSidebarPanelFromMainView();}catch(_){}
  const layout=document.querySelector('.layout');
  if(layout)layout.classList.remove('sidebar-collapsed');
  sidebar.classList.remove('sidebar-collapsed');
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  sidebar.classList.remove('mobile-session-page');
  sidebar.classList.add('mobile-panel-drawer');
  sidebar.classList.add('mobile-open');
}

function _onPwaSidebarSwipeStart(e){
  if(_isDesktopWidth())return;
  if(_isTouchPointerEvent(e))return;
  if(e.pointerType==='mouse'||(e.pointerType&&e.pointerType!=='touch'&&e.pointerType!=='pen'))return;
  if(document.querySelector('.sidebar')?.classList.contains('mobile-open'))return;
  const point=_pwaSidebarSwipePoint(e);
  if(!point)return;
  if(point.clientX>_PWA_SIDEBAR_SWIPE_EDGE)return;
  if(_isInteractiveSwipeTarget(e.target))return;
  _pwaSidebarSwipe={startX:point.clientX,startY:point.clientY,active:true,opened:false};
}

function _onPwaSidebarSwipeMove(e){
  if(_isTouchPointerEvent(e))return;
  const swipe=_pwaSidebarSwipe;
  if(!swipe||!swipe.active||swipe.opened)return;
  const point=_pwaSidebarSwipePoint(e);
  if(!point)return;
  const dx=point.clientX-swipe.startX;
  const dy=point.clientY-swipe.startY;
  if(dx<0||Math.abs(dy)>_PWA_SIDEBAR_SWIPE_MAX_VERTICAL*1.5){_pwaSidebarSwipe=null;return;}
  if(dx>=_PWA_SIDEBAR_SWIPE_CLAIM&&dx>Math.abs(dy)*1.2){
    if(e.cancelable)e.preventDefault();
  }
  if(dx>=_PWA_SIDEBAR_SWIPE_TRIGGER&&Math.abs(dy)<=_PWA_SIDEBAR_SWIPE_MAX_VERTICAL&&dx>Math.abs(dy)*1.5){
    if(e.cancelable)e.preventDefault();
    swipe.opened=true;
    _openMobileSidebarFromGesture();
  }
}

function _onPwaSidebarSwipeEnd(e){if(_isTouchPointerEvent(e))return;_pwaSidebarSwipe=null;}
function _onPwaSidebarSwipeCancel(e){if(_isTouchPointerEvent(e))return;_pwaSidebarSwipe=null;}

function _installPwaSidebarSwipeGesture(){
  // #4660 review (Codex CORE): the #pwaSidebarEdgeGuard element is now
  // pointer-events:none (CSS), so it can no longer intercept hit-testing for
  // taps / vertical scrolls that merely start in the left edge strip — those
  // pass through to the underlying .messages scroller. The edge-swipe-to-open
  // gesture is handled entirely by the window-level CAPTURE touch/pointer
  // listeners below (which see the event regardless of the guard), so no
  // dedicated guard-element listener is needed.
  window.addEventListener('touchstart', _onPwaSidebarSwipeStart, {capture:true,passive:true});
  window.addEventListener('touchmove', _onPwaSidebarSwipeMove, {capture:true,passive:false});
  window.addEventListener('touchend', _onPwaSidebarSwipeEnd, {capture:true,passive:true});
  window.addEventListener('touchcancel', _onPwaSidebarSwipeCancel, {capture:true,passive:true});
  window.addEventListener('pointerdown', _onPwaSidebarSwipeStart, {passive:true});
  window.addEventListener('pointermove', _onPwaSidebarSwipeMove, {passive:false});
  window.addEventListener('pointerup', _onPwaSidebarSwipeEnd, {passive:true});
  window.addEventListener('pointercancel', _onPwaSidebarSwipeCancel, {passive:true});
}
_installPwaSidebarSwipeGesture();

// ── Desktop sidebar collapse toggle ────────────────────────────────────────
// Two discoverability paths into the same state:
//   (1) Click the already-active rail icon → collapse / expand the sidebar.
//   (2) Cmd/Ctrl+B keyboard shortcut (VS Code convention).
// Mobile is unaffected: the sidebar is an overlay there, and every collapse
// code path is gated on `_isDesktopWidth()` (min-width:641px).
// State is persisted via localStorage and survives reloads + bfcache.
const _SIDEBAR_COLLAPSED_KEY='hermes-webui-sidebar-collapsed';

function _isDesktopWidth(){
  try{return window.matchMedia('(min-width:641px)').matches;}catch(_){return true;}
}

function _isSidebarCollapsed(){
  return document.querySelector('.layout')?.classList.contains('sidebar-collapsed')||false;
}

function _syncSidebarAria(){
  // Mirror the open/collapsed state on the active rail button via aria-expanded
  // so screen readers announce the toggle. Open=true, collapsed=false.
  const active=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]');
  if(active)active.setAttribute('aria-expanded',!_isSidebarCollapsed());
}

function toggleSidebar(forceState){
  if(!_isDesktopWidth())return; // mobile uses an overlay; never collapse there
  const layout=document.querySelector('.layout');
  if(!layout)return;
  const next=typeof forceState==='boolean'?forceState:!_isSidebarCollapsed();
  layout.classList.toggle('sidebar-collapsed',next);
  // Clear the flash-prevention root-level marker once JS owns the state.
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  try{localStorage.setItem(_SIDEBAR_COLLAPSED_KEY,next?'1':'0');}catch(_){}
  _syncSidebarAria();
}

function expandSidebar(){
  if(_isSidebarCollapsed())toggleSidebar(false);
}

// Boot-time restore. The inline flash-prevention script in index.html already
// set data-sidebar-collapsed='1' on <html> before the stylesheet so the page
// renders collapsed without paint flash. This IIFE promotes that pre-paint
// state into the .layout class system where both JS and CSS can read it.
(function _restoreSidebarState(){
  try{document.documentElement.removeAttribute('data-sidebar-collapsed');}catch(_){}
  if(!_isDesktopWidth())return;
  try{
    if(localStorage.getItem(_SIDEBAR_COLLAPSED_KEY)==='1'){
      const layout=document.querySelector('.layout');
      if(layout)layout.classList.add('sidebar-collapsed');
    }
  }catch(_){}
  _syncSidebarAria();
})();
// ── Boot-time tab visibility ────────────────────────────────────────────────
// Apply hidden tabs from localStorage. The primary flash-prevention is an
// inline <script> in index.html (after sidebar-nav) that runs synchronously
// before first paint. This IIFE is a secondary fallback: it ensures consistency
// after panels.js is loaded and handles the active-tab switch. No-op if
// panels.js hasn't loaded yet (typeof guard).
(function _restoreTabVisibility(){
  try{
    if(typeof _applyTabOrder==='function'&&typeof _getTabOrder==='function'){
      _applyTabOrder(_getTabOrder());
    }
    if(typeof _applyTabVisibility==='function'&&typeof _getHiddenTabs==='function'){
      _applyTabVisibility(_getHiddenTabs());
    }
    var active=document.querySelector('.rail .rail-btn.nav-tab.active[data-panel]')
               ||document.querySelector('.sidebar-nav .nav-tab.active[data-panel]');
    if(active&&active.classList.contains('nav-tab-hidden')){
      var chatBtn=document.querySelector('.rail .rail-btn.nav-tab[data-panel="chat"]');
      if(chatBtn)chatBtn.classList.add('active');
      if(active)active.classList.remove('active');
    }
  }catch(_){}
})();
function toggleMobileFiles(){
  toggleWorkspacePanel();
}
function closeMobileWorkspacePanelFromChat(e){
  if(!_isCompactWorkspaceViewport()||_workspacePanelMode==='closed') return;
  const panel=document.querySelector('.rightpanel');
  if(panel&&panel.contains(e.target)) return;
  closeWorkspacePanel();
}
function toggleWorkspacePanel(force){
  const {panel}= _workspacePanelEls();
  if(!panel)return;
  const currentlyOpen=_workspacePanelMode!=='closed';
  const nextOpen=typeof force==='boolean'?force:!currentlyOpen;
  if(!nextOpen){
    closeWorkspacePanel();
    return;
  }
  const nextMode=_hasWorkspacePreviewVisible()?'preview':'browse';
  openWorkspacePanel(nextMode);
}
function mobileSwitchPanel(name){
  switchPanel(name);
  if(name==='chat'){
    closeMobileSidebar();
  } else {
    const sidebar=document.querySelector('.sidebar');
    if(sidebar){
      sidebar.classList.remove('mobile-session-page');
      sidebar.classList.add('mobile-panel-drawer','mobile-open');
    }
  }
}

$('btnSend').onclick=()=>{
  if(typeof handleComposerPrimaryAction==='function') return handleComposerPrimaryAction();
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  // Turn-based voice mode: let the voice mode system handle the send flow
  if(typeof window._voiceModeActive==='function'&&window._voiceModeActive()){
    // Immediately send whatever is in the textarea
    if(typeof window._voiceModeImmediateSend==='function') window._voiceModeImmediateSend();
    return;
  }
  send();
};
$('mainChat')?.addEventListener('pointerdown', closeMobileWorkspacePanelFromChat);
$('btnAttach').onclick=e=>{if(e&&e.preventDefault)e.preventDefault();$('fileInput').value='';$('fileInput').click();};

window.HermesBoot.publish('shellNavigation',{syncWorkspacePanelState,openWorkspacePanel,closeWorkspacePanel,toggleMobileSidebar,closeMobileSidebar,toggleSidebar,mobileSwitchPanel});
