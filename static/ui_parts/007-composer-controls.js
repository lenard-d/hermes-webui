// ── Session toolsets chip (#493) ───────────────────────────────────────────
let _currentSessionToolsets = null; // null = active profile defaults, array = custom list
let _toolsetsCatalog = null;

function _applyToolsetsChip(toolsets) {
  _currentSessionToolsets = toolsets;
  const wrap = $('composerToolsetsWrap');
  const label = $('composerToolsetsLabel');
  const chip = $('composerToolsetsChip');
  if (!wrap || !label) return;
  // Visibility is controlled entirely by responsive CSS — the chip shows only
  // at wide composer-footer widths (>= 1100px container query). At narrower
  // widths the layout is too cramped (model + reasoning + profile + workspace
  // + context-ring + send) to add another chip. Cleared inline style so the
  // CSS @container query is the single source of truth. State is still
  // tracked so /api/session/toolsets continues to work for cron/scripted
  // callers regardless of UI visibility. (#1431)
  wrap.style.display = '';
  const hasCustom = Array.isArray(toolsets) && toolsets.length > 0;
  const isStaged = hasCustom
    && typeof S !== 'undefined'
    && S
    && !S.session
    && Array.isArray(S._pendingSessionToolsets);
  if (hasCustom) {
    const stagedSuffix = isStaged ? ' (staged)' : '';
    label.textContent = toolsets.join(', ') + stagedSuffix;
    chip.classList.add('has-custom');
    chip.title = t('session_toolsets') + ': ' + toolsets.join(', ') + stagedSuffix;
  } else {
    label.textContent = t('session_toolsets_profile_defaults');
    chip.classList.remove('has-custom');
    chip.title = t('session_toolsets') + ': ' + t('session_toolsets_profile_defaults');
  }
}

function _syncToolsetsChip() {
  if (typeof S === 'undefined' || !S || !S.session) {
    const stagedToolsets = (typeof S !== 'undefined' && S && Array.isArray(S._pendingSessionToolsets))
      ? S._pendingSessionToolsets
      : null;
    _applyToolsetsChip(stagedToolsets);
    return;
  }
  _applyToolsetsChip(S.session.enabled_toolsets || null);
}

function syncToolsetsChip() {
  _syncToolsetsChip();
}

function _normalizeToolsetsCatalog(payload) {
  const servers = payload && Array.isArray(payload.servers) ? payload.servers : [];
  const seen = new Set();
  const names = [];
  servers.forEach(function(server) {
    const name = String((server && server.name) || '').trim();
    if (!name || seen.has(name)) return;
    seen.add(name);
    names.push(name);
  });
  return names;
}

function _loadToolsetsCatalog() {
  if (Array.isArray(_toolsetsCatalog)) return Promise.resolve(_toolsetsCatalog);
  return api('/api/mcp/servers')
    .then(function(payload) {
      _toolsetsCatalog = _normalizeToolsetsCatalog(payload);
      return _toolsetsCatalog;
    })
    .catch(function() {
      _toolsetsCatalog = false;
      return [];
    });
}

function invalidateToolsetsCatalog(payload) {
  _toolsetsCatalog = payload && Array.isArray(payload.servers) ? _normalizeToolsetsCatalog(payload) : null;
}
if (typeof window !== 'undefined') window.invalidateToolsetsCatalog = invalidateToolsetsCatalog;

function _toolsetsInputList(input) {
  if (!input) return [];
  return input.value.split(',').map(s => s.trim()).filter(Boolean);
}

function _ensureToolsetsPresetSection() {
  const dd = $('composerToolsetsDropdown');
  if (!dd) return null;
  let section = $('toolsetsPresetSections');
  if (section) return section;
  section = document.createElement('div');
  section.id = 'toolsetsPresetSections';
  section.className = 'toolsets-preset-sections';
  const inputRow = dd.querySelector('.toolsets-dropdown-input-row');
  if (inputRow) dd.insertBefore(section, inputRow);
  else dd.appendChild(section);
  return section;
}

function _appendToolsetsLabel(section, text) {
  const label = document.createElement('div');
  label.className = 'toolsets-dropdown-desc';
  label.textContent = text;
  section.appendChild(label);
}

function _renderToolsetsPresetSections(opts) {
  const state = opts && opts.state;
  const input = opts && opts.input;
  const section = _ensureToolsetsPresetSection();
  if (!section || !state || !input) return;
  const selected = _toolsetsInputList(input);
  const selectedSet = new Set(selected);
  const hasCustom = selected.length > 0;
  state.textContent = hasCustom
    ? '🔧 ' + selected.join(', ')
    : '👤 ' + t('session_toolsets_profile_defaults');

  section.innerHTML = '';
  const defaultsBtn = document.createElement('button');
  defaultsBtn.type = 'button';
  defaultsBtn.id = 'toolsetsProfileDefaultsBtn';
  defaultsBtn.className = 'toolsets-action-btn toolsets-clear-btn';
  defaultsBtn.textContent = t('session_toolsets_use_profile_defaults');
  section.appendChild(defaultsBtn);

  _appendToolsetsLabel(section, t('session_toolsets_configured_servers'));
  if (_toolsetsCatalog === null) {
    _appendToolsetsLabel(section, t('session_toolsets_loading_servers'));
    return;
  }
  if (_toolsetsCatalog === false) {
    _appendToolsetsLabel(section, t('mcp_load_failed'));
    return;
  }
  if (!Array.isArray(_toolsetsCatalog) || !_toolsetsCatalog.length) {
    _appendToolsetsLabel(section, t('session_toolsets_no_configured_servers'));
    return;
  }
  _toolsetsCatalog.forEach(function(name) {
    const row = document.createElement('label');
    row.className = 'toolsets-server-option';
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.gap = '6px';
    row.style.margin = '4px 0';
    row.style.fontSize = '12px';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'toolsets-server-checkbox';
    checkbox.value = name;
    checkbox.checked = selectedSet.has(name);
    row.appendChild(checkbox);
    row.appendChild(document.createTextNode(name));
    section.appendChild(row);
  });
}

function _populateToolsetsDropdown() {
  const desc = $('toolsetsDropdownDesc');
  const state = $('toolsetsDropdownState');
  const input = $('toolsetsInput');
  const applyBtn = $('toolsetsApplyBtn');
  const clearBtn = $('toolsetsClearBtn');
  if (!desc || !state || !input) return;
  desc.textContent = t('session_toolsets_desc');
  if (applyBtn) applyBtn.textContent = t('session_toolsets_apply');
  if (clearBtn) clearBtn.textContent = t('session_toolsets_clear');
  input.placeholder = t('session_toolsets_placeholder');
  // Escape key handler for toolsets input
  input.onkeydown = function(e) { if(e.key === 'Escape') closeToolsetsDropdown(); };
  input.oninput = function() { _renderToolsetsPresetSections({ state, input }); };
  const hasCustom = Array.isArray(_currentSessionToolsets) && _currentSessionToolsets.length > 0;
  if (hasCustom) {
    input.value = _currentSessionToolsets.join(', ');
  } else {
    input.value = '';
  }
  _renderToolsetsPresetSections({ state, input });
}

function _positionToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  const footer = document.querySelector('.composer-footer');
  if (!dd || !chip || !footer) return;
  // Defense: if the chip has been hidden by responsive CSS (e.g. resize across
  // 1100px container threshold while dropdown was open), don't try to anchor
  // to a zero-rect element — close the dropdown instead. (#1431)
  if (chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  const chipRect = chip.getBoundingClientRect();
  const footerRect = footer.getBoundingClientRect();
  let left = chipRect.left - footerRect.left;
  const maxLeft = Math.max(0, footer.clientWidth - dd.offsetWidth);
  left = Math.max(0, Math.min(left, maxLeft));
  dd.style.left = left + 'px';
}

function toggleToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (!dd || !chip) return;
  // Don't open when the chip itself is hidden by responsive CSS (#1431).
  // offsetParent === null catches display:none on the element or any ancestor.
  if (chip.offsetParent === null) return;
  const open = dd.classList.contains('open');
  if (open) { closeToolsetsDropdown(); return; }
  if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
  if (typeof closeWsDropdown === 'function') closeWsDropdown();
  closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  _syncToolsetsChip();
  _populateToolsetsDropdown();
  _loadToolsetsCatalog().then(function() {
    const stillOpen = dd && dd.classList.contains('open');
    if (stillOpen) {
      const state = $('toolsetsDropdownState');
      const input = $('toolsetsInput');
      _renderToolsetsPresetSections({ state, input });
    }
  });
  dd.classList.add('open');
  _positionToolsetsDropdown();
  chip.classList.add('active');
  // Focus the input after a tick so the layout has settled
  setTimeout(() => { const inp = $('toolsetsInput'); if (inp) inp.focus(); }, 50);
}

function closeToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (dd) dd.classList.remove('open');
  if (chip) chip.classList.remove('active');
}

function _applySessionToolsets(toolsets) {
  if (typeof S === 'undefined' || !S) return;
  if (!S.session) {
    S._pendingSessionToolsets = toolsets;
    _applyToolsetsChip(toolsets);
    if (Array.isArray(toolsets) && toolsets.length) {
      showToast('🔧 ' + t('session_toolsets_applied') + ': ' + toolsets.join(', '));
    } else {
      showToast('🌍 ' + t('session_toolsets_cleared'));
    }
    return;
  }
  const sid = S.session.session_id;
  api('/api/session/toolsets', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, toolsets: toolsets })
  })
    .then(function(r) {
      if (r && r.ok) {
        S.session.enabled_toolsets = r.enabled_toolsets || null;
        _applyToolsetsChip(r.enabled_toolsets || null);
        if (r.enabled_toolsets && r.enabled_toolsets.length) {
          showToast('🔧 ' + t('session_toolsets_applied') + ': ' + r.enabled_toolsets.join(', '));
        } else {
          showToast('🌍 ' + t('session_toolsets_cleared'));
        }
      } else {
        showToast(t('session_toolsets_failed') + (r && r.error ? r.error : 'Unknown error'), 3000, 'error');
      }
    })
    .catch(function(err) {
      showToast(t('session_toolsets_failed') + (err.message || err), 3000, 'error');
    });
}

// Click-outside handler for toolsets dropdown
document.addEventListener('click', function(e) {
  if (
    !e.target.closest('#composerToolsetsChip') &&
    !e.target.closest('#composerToolsetsDropdown')
  ) closeToolsetsDropdown();
  // Active profile defaults button
  if (e.target.closest('#toolsetsProfileDefaultsBtn')) {
    _applySessionToolsets(null);
    closeToolsetsDropdown();
    return;
  }
  // Apply button
  if (e.target.closest('#toolsetsApplyBtn')) {
    const input = $('toolsetsInput');
    if (!input) return;
    const raw = input.value.trim();
    if (!raw) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    const toolsets = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (toolsets.length === 0) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    _applySessionToolsets(toolsets);
    closeToolsetsDropdown();
  }
  // Clear button
  if (e.target.closest('#toolsetsClearBtn')) {
    _applySessionToolsets(null);
    closeToolsetsDropdown();
  }
});

document.addEventListener('change', function(e) {
  if (!e.target.closest('#toolsetsPresetSections')) return;
  if (!e.target.classList.contains('toolsets-server-checkbox')) return;
  const input = $('toolsetsInput');
  const state = $('toolsetsDropdownState');
  if (!input) return;
  const checked = Array.from(document.querySelectorAll('#toolsetsPresetSections .toolsets-server-checkbox:checked'))
    .map(el => String(el.value || '').trim())
    .filter(Boolean);
  const catalogSet = new Set(Array.isArray(_toolsetsCatalog) ? _toolsetsCatalog : []);
  const manual = _toolsetsInputList(input).filter(name => !catalogSet.has(name));
  input.value = checked.concat(manual).join(', ');
  _renderToolsetsPresetSections({ state, input });
});

// Position toolsets dropdown on resize, OR close it if the chip is no longer
// visible (e.g. resize crossed the 1100px container threshold while dropdown
// was open — the wrap is hidden by CSS but the dropdown sibling stays open
// without an anchor). (#1431)
window.addEventListener('resize', () => {
  const dd = $('composerToolsetsDropdown');
  if (!dd || !dd.classList.contains('open')) return;
  const chip = $('composerToolsetsChip');
  if (!chip || chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  _positionToolsetsDropdown();
});

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

// ── Scroll pinning ──────────────────────────────────────────────────────────
// When streaming, auto-scroll only while the user is following the live tail.
// Any manual scroll up sets a sticky unpinned flag until the user scrolls back
// to the bottom (near-bottom hysteresis on downward motion) or clicks ↓.
// Programmatic scrolls are ignored via _programmaticScroll. Fixes #1469 / #1360 / #1731.
let _scrollPinned=true;
let _programmaticScroll=false;
let _programmaticScrollSetAt=0;
let _programmaticScrollResetTimer=0;
function _deferClearProgrammaticScroll(ms){clearTimeout(_programmaticScrollResetTimer);_programmaticScrollResetTimer=setTimeout(()=>{_programmaticScroll=false;},ms||80);}
let _nearBottomCount=0;
let _lastScrollTop=null;
let _lastMessageClientHeight=null;   // #4702: track scroller height to ignore iOS portrait toolbar-settle reflows (a clientHeight increase fires a scroll event with decreased scrollTop that is NOT a user scroll)
// Sticky-unpin model (#3343 supersedes #3330's proximity re-pin): once the user
// scrolls up, streaming stops auto-following until they return to the bottom or
// click ↓. The upward-intent TIMEOUT mechanism (_lastMessageUpwardIntentMs /
// MESSAGE_UPWARD_INTENT_MS) is removed — sticky-unpin makes it unnecessary.
// Keep the non-message intent timestamp at -Infinity so load-time isn't read as
// intent (the #3330 follow-up fix); 0 would mark the first NON_MESSAGE_SCROLL_INTENT
// window after load as suppressed.
let _lastNonMessageScrollIntentMs=-Infinity;
let _messageUserUnpinned=false;
let _bottomSettleToken=0;
let _settleRAF=0;
let _settleRO=null;
let _settleTimer=0;
let _settleFinalTimer=0;
const NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS=350;
let _touchStartY=null;
let _messageTouchScrollActive=false;
let _lastMessageTouchScrollIntentMs=-Infinity;
let _deferredOlderMessagesTimer=0;
const MESSAGE_TOUCH_SCROLL_SUPPRESS_MS=1200;
// #4970 review: track recent LOW-DELTA upward message-pane wheel intent separately from
// the decisive deltaY<-30 sticky-unpin threshold. A gentle trackpad wheel
// (deltaY:-5) is real user intent but never crosses -30, so without this the
// post-render artifact suppression would swallow it for the whole window.
const MESSAGE_WHEEL_INTENT_SUPPRESS_MS=1200;
let _lastMessageWheelIntentMs=-Infinity;
let _lastMessageScrollIntentMs=-Infinity;
// #4970 review (greptile P1): keyboard scrolling of the message pane (PageUp/Down,
// arrows, Space, Home/End) fires a native `scroll` event with NO wheel/touch/
// scrollbar/non-message intent. Without recording it, a keyboard scroll-up inside
// the post-render artifact window is swallowed and live-follow snaps the reader
// back to the bottom. Stamp a generic scroll-key intent so the suppression skips it.
const MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS=1200;
let _lastMessageKeyScrollIntentMs=-Infinity;
let _newMessageCueVisible=false;
let _lastMessageRenderAt=-Infinity;
function _recentMessageRenderArtifactWindow(ms){
  return performance.now()-_lastMessageRenderAt<(ms||1400);
}
function _cancelBottomSettle(){ _bottomSettleToken++; if(_settleRO){ _settleRO.disconnect(); _settleRO=null; } clearTimeout(_settleTimer); clearTimeout(_settleFinalTimer); cancelAnimationFrame(_settleRAF); }
function _markMessageTouchScrollIntent(active=true){
  _messageTouchScrollActive=!!active;
  _lastMessageTouchScrollIntentMs=performance.now();
}
function _recentMessageTouchScrollIntent(){
  return _messageTouchScrollActive || performance.now()-_lastMessageTouchScrollIntentMs<MESSAGE_TOUCH_SCROLL_SUPPRESS_MS;
}
// #4970: true when the reader recently made ANY upward message-pane wheel
// motion, including gentle low-delta trackpad wheels below the -30 sticky-unpin
// threshold. The post-render artifact suppression must NOT fire when this is
// true, otherwise a real gentle scroll-up right after a render gets swallowed.
function _recentMessageWheelIntent(){
  return performance.now()-_lastMessageWheelIntentMs<MESSAGE_WHEEL_INTENT_SUPPRESS_MS;
}
function _recentMessageScrollIntent(){
  // This manual-reader snapshot signal intentionally excludes the raw
  // touch/key recency helpers: those also record near-tail events for render
  // artifact suppression. Only this timestamp is guarded by bottom distance.
  return performance.now()-_lastMessageScrollIntentMs<MESSAGE_WHEEL_INTENT_SUPPRESS_MS
    || (typeof _scrollbarDragActive!=='undefined'&&!!_scrollbarDragActive);
}
// #4970 review (greptile P1): true when the reader recently used the keyboard to
// scroll the message pane. Keyboard scrolls fire a native scroll event with no
// wheel/touch intent, so the post-render artifact suppression must skip them.
function _recentMessageKeyScrollIntent(){
  return performance.now()-_lastMessageKeyScrollIntentMs<MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS;
}
function _isMessageReaderUnpinned(){
  return !!_messageUserUnpinned;
}
function _olderMessagesPrefetchReady(){
  const el=document.getElementById('messages');
  if(!el) return false;
  const olderPrefetchPx=Math.max(600,el.clientHeight*1.5);
  return _isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function';
}
function _scheduleDeferredOlderMessagesLoad(){
  clearTimeout(_deferredOlderMessagesTimer);
  _deferredOlderMessagesTimer=setTimeout(()=>{
    _deferredOlderMessagesTimer=0;
    if(_recentMessageTouchScrollIntent()){
      _scheduleDeferredOlderMessagesLoad();
      return;
    }
    if(_olderMessagesPrefetchReady()) _loadOlderMessages();
  },MESSAGE_TOUCH_SCROLL_SUPPRESS_MS+50);
}
function _recordNonMessageScrollIntent(e){
  const el=document.getElementById('messages');
  const target=e&&e.target;
  if(!el||!target) return;
  if(!el.contains(target)){ _lastNonMessageScrollIntentMs=performance.now(); return; }
  // #4970: record ANY upward message-pane wheel motion as recent wheel intent,
  // including gentle low-delta trackpad wheels (e.g. deltaY:-5) that never reach
  // the decisive -30 sticky-unpin threshold below. The post-render artifact
  // suppression consults _recentMessageWheelIntent() so it cannot swallow a real
  // gentle scroll-up. This does NOT unpin on its own — only the <-30 branch and
  // the scroll listener's movedUp branch flip _messageUserUnpinned.
  if(e.type==='touchmove'||(typeof e.deltaY==='number'&&e.deltaY!==0)){
    const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
    if(bottomDistance>120) _lastMessageScrollIntentMs=performance.now();
  }
  if(typeof e.deltaY==='number'&&e.deltaY<0) _lastMessageWheelIntentMs=performance.now();
  if(e.type==='touchmove'||(typeof e.deltaY==='number'&&e.deltaY< -30)){
    _cancelBottomSettle();
    if(e.type==='touchmove') _markMessageTouchScrollIntent(true);
    if(typeof e.deltaY==='number'&&e.deltaY< -30){
      _messageUserUnpinned=true;
      _nearBottomCount=0;
      _scrollPinned=false;
    } else if(e.type==='touchmove'&&_touchStartY!==null&&e.touches&&e.touches[0]){
      // Detect upward-scroll intent on touch: dragging the finger DOWN the
      // screen scrolls the content up into earlier history (scrollTop
      // decreases) — the same "user scrolled away" signal the wheel deltaY<0
      // branch and the scroll listener's movedUp branch use. dy>0 = finger
      // moved down = reveal earlier content = unpin.
      const dy=e.touches[0].clientY-_touchStartY;
      if(dy>8){
        _messageUserUnpinned=true;
        _nearBottomCount=0;
        _scrollPinned=false;
      }
    }
  }
}
function _recentNonMessageScrollIntent(){
  return performance.now()-_lastNonMessageScrollIntentMs<NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS;
}
function _setScrollToBottomCueText(btn, textKey, labelKey){
  if(!btn) return;
  const label=btn.querySelector('.session-jump-btn__text');
  if(label){
    label.setAttribute('data-i18n',textKey);
    label.textContent=(typeof t==='function')?t(textKey):label.textContent;
  }
  btn.setAttribute('data-i18n-aria-label',labelKey);
  btn.setAttribute('data-i18n-title',labelKey);
  const accessible=(typeof t==='function')?t(labelKey):btn.getAttribute('aria-label')||'';
  if(accessible){
    btn.setAttribute('aria-label',accessible);
    btn.setAttribute('title',accessible);
  }
}
function _syncScrollToBottomCue(show, opts){
  const btn=$('scrollToBottomBtn');
  if(!btn) return;
  const newMessage=!!(opts&&opts.newMessage);
  btn.classList.toggle('scroll-to-bottom-btn--new-message',newMessage);
  if(newMessage) _setScrollToBottomCueText(btn,'session_new_message','session_new_message_label');
  else _setScrollToBottomCueText(btn,'session_jump_end','session_jump_end_label');
  btn.style.display=show?'flex':'none';
}
function _showNewMessageScrollCue(){
  _newMessageCueVisible=true;
  _syncScrollToBottomCue(true,{newMessage:true});
}
function _clearNewMessageScrollCue(){
  _newMessageCueVisible=false;
  _syncScrollToBottomCue(false,{newMessage:false});
}
function _maybeShowNewMessageScrollCue(scrollSnapshot){
  const el=document.getElementById('messages');
  if(!el||!scrollSnapshot) return;
  const previousHeight=Number(scrollSnapshot.scrollHeight)||0;
  const distance=el.scrollHeight-el.scrollTop-el.clientHeight;
  if(el.scrollHeight>previousHeight+24 && distance>80) _showNewMessageScrollCue();
  else _syncScrollToBottomCue(distance>80,{newMessage:_newMessageCueVisible});
}
if(typeof document!=='undefined'){
  document.addEventListener('wheel',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchmove',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchstart',function(e){
    const el=document.getElementById('messages');
    if(e.touches&&e.touches[0]) _touchStartY=e.touches[0].clientY;
    if(el&&e.target&&el.contains(e.target)) _markMessageTouchScrollIntent(true);
  },{capture:true,passive:true});
  document.addEventListener('touchend',function(){ _touchStartY=null; if(_messageTouchScrollActive) _markMessageTouchScrollIntent(false); },{capture:true,passive:true});
  document.addEventListener('touchcancel',function(){ _touchStartY=null; if(_messageTouchScrollActive) _markMessageTouchScrollIntent(false); },{capture:true,passive:true});
}
// Reset hook for session-switch — called from sessions.js loadSession() to
// prevent the new chat's first scroll comparing against the previous chat's
// scrollTop (Opus stage-302 SHOULD-FIX, #1731 follow-up).
function _resetScrollDirectionTracker(){
  _clearNewMessageScrollCue();
  _lastScrollTop=null;
  _lastMessageClientHeight=null;
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _touchStartY=null;
  _messageTouchScrollActive=false;
  _lastMessageTouchScrollIntentMs=-Infinity;
  // #4970 review: also clear low-delta wheel intent on session switch, else a
  // gentle wheel in the previous chat leaves _recentMessageWheelIntent() true
  // into the new chat's first post-render window — the artifact then isn't
  // suppressed, falls into movedUp, and falsely unpins live-follow.
  _lastMessageWheelIntentMs=-Infinity;
  _lastMessageScrollIntentMs=-Infinity;
  // #4970 review (greptile P1): same hygiene for keyboard scroll intent.
  _lastMessageKeyScrollIntentMs=-Infinity;
  clearTimeout(_deferredOlderMessagesTimer);
  _deferredOlderMessagesTimer=0;
}
function _resetStreamScrollFollow(){
  _clearNewMessageScrollCue();
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _lastScrollTop=null;
  // #4970 review: clear low-delta wheel intent on fresh stream start too, else a
  // gentle upward wheel within the prior 1200ms can under-suppress a genuine
  // no-intent render artifact and silently disable live follow for the new stream.
  _lastMessageWheelIntentMs=-Infinity;
  _lastMessageScrollIntentMs=-Infinity;
  // #4970 review (greptile P1): same hygiene for keyboard scroll intent.
  _lastMessageKeyScrollIntentMs=-Infinity;
  _cancelBottomSettle();
}
if(typeof window!=='undefined'){
  window._resetScrollDirectionTracker=_resetScrollDirectionTracker;
  window._resetStreamScrollFollow=_resetStreamScrollFollow;
}
/* ── Pull-to-refresh for PWA standalone (Android) ── */
(function(){
  if(typeof document==='undefined') return;
  const isStandalone=window.navigator?.standalone||matchMedia('(display-mode:standalone),(display-mode:fullscreen)').matches;
  if(!isStandalone) return;
  const el=document.getElementById('messages');
  if(!el) return;
  let _ptrState=0; // 0=idle, 1=pulling, 2=ready
  let _ptrStartY=0;
  let _ptrCurrentY=0;
  const THRESHOLD=80;
  let _indicator=null;
  function _ptrCreateIndicator(){
    if(_indicator) return;
    _indicator=document.createElement('div');
    _indicator.className='pull-to-refresh-indicator';
    _indicator.innerHTML='<span class="ptr-icon">↓</span> <span class="ptr-text">Pull to refresh</span>';
    el.parentNode.insertBefore(_indicator,el);
  }
  function _ptrUpdate(progress){
    _ptrCreateIndicator();
    const pulling=progress<1;
    _indicator.classList.toggle('active',progress>0);
    const icon=_indicator.querySelector('.ptr-icon');
    const text=_indicator.querySelector('.ptr-text');
    if(icon) icon.classList.toggle('ready',!pulling);
    if(text) text.textContent=pulling?'Pull to refresh':'Release to refresh';
  }
  function _ptrReset(){
    _ptrState=0;
    _ptrStartY=0;
    _ptrCurrentY=0;
    if(_indicator) _indicator.classList.remove('active');
  }
  el.addEventListener('touchstart',function(e){
    if(el.scrollTop>0||_ptrState!==0) return;
    _ptrStartY=e.touches[0].clientY;
    _ptrState=1;
  },{passive:true});
  el.addEventListener('touchmove',function(e){
    if(_ptrState!==1) return;
    _ptrCurrentY=e.touches[0].clientY;
    const pull=_ptrCurrentY-_ptrStartY;
    if(pull<0){ _ptrReset(); return; }
    /* If not at the top, smooth-scroll to top first.
       Next pull gesture will trigger the refresh. */
    if(el.scrollTop>0){
      el.scrollTo({top:0,behavior:'smooth'});
      _ptrReset();
      return;
    }
    const progress=Math.min(pull/THRESHOLD,1);
    _ptrUpdate(progress);
    _ptrState=progress>=1?2:1;
    if(progress>0.3) e.preventDefault();
  },{passive:false});
  el.addEventListener('touchend',function(){
    if(_ptrState===2){
      if(typeof window.refreshSessionList==='function'){
        Promise.resolve(window.refreshSessionList('pull', {force:true, refreshActive:true})).catch(()=>{}).finally(_ptrReset);
      }else{
        window.location.reload();
      }
      return;
    }
    _ptrReset();
  },{passive:true});
  el.addEventListener('touchcancel',_ptrReset,{passive:true});
})();
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  el.addEventListener('pointerdown',(e)=>{
    if(e.target===el&&e.offsetX>=el.clientWidth) _scrollbarDragActive=true;
  },{passive:true});
  window.addEventListener('pointerup',()=>{
    if(!_scrollbarDragActive) return;
    _scrollbarDragActive=false;
    _scheduleMessageVirtualizedRender(true);
  },{passive:true});
  window.addEventListener('pointercancel',()=>{
    if(!_scrollbarDragActive) return;
    _scrollbarDragActive=false;
    _scheduleMessageVirtualizedRender(true);
  },{passive:true});
  window.addEventListener('blur',()=>{ _scrollbarDragActive=false; },{passive:true});
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='hidden') _scrollbarDragActive=false;
  },{passive:true});
  // #4970 review (greptile P1): record keyboard-driven message-pane scrolling as
  // user intent. PageUp/PageDown, Arrow keys, Space/Shift+Space, Home/End scroll
  // the pane and fire a native scroll event with no wheel/touch intent — without
  // this stamp a keyboard scroll-up inside the post-render artifact window is
  // swallowed and live-follow snaps the reader back to the bottom. Only count it
  // when the scroll container (or a descendant) is the active/scrolling target,
  // not when typing in the composer or activating an in-transcript control.
  const _MESSAGE_SCROLL_KEYS=new Set([
    'PageUp','PageDown','ArrowUp','ArrowDown','Home','End','Spacebar',' ',
  ]);
  const _isMessageInteractiveKeyTarget=(node)=>{
    if(!node||!el.contains(node)) return false;
    if(node.tagName==='INPUT'||node.tagName==='TEXTAREA'||node.isContentEditable) return true;
    return !!(node.closest&&node.closest('button,a[href],select,summary,[role="button"],[role="tab"],[role="menuitem"],[contenteditable="true"]'));
  };
  document.addEventListener('keydown',(e)=>{
    if(!e||!_MESSAGE_SCROLL_KEYS.has(e.key)) return;
    const a=document.activeElement;
    const t=e.target;
    // Ignore keys aimed at editable fields (composer, inputs, contenteditable).
    if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.isContentEditable)) return;
    // Space/Spacebar activates focused transcript controls (buttons, role=button,
    // links, tabs) rather than scrolling. The listener is capture-phase, so target
    // handlers have not yet preventDefault()/stopPropagation()'d; inspect the
    // active/target control path directly.
    if((e.key===' '||e.key==='Spacebar')&&(_isMessageInteractiveKeyTarget(t)||_isMessageInteractiveKeyTarget(a))) return;
    // Count only when the message pane itself is the scroll target: it is focused,
    // contains the focus, or the pointer is over it (keyboard scroll w/o focus).
    if(a===el||el.contains(a)||el.matches(':hover')){
      const now=performance.now();
      _lastMessageKeyScrollIntentMs=now;
      const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
      if(bottomDistance>120) _lastMessageScrollIntentMs=now;
    }
  },{capture:true,passive:true});
  let _scrollRaf=0;
  el.addEventListener('scroll',()=>{
    _scheduleMessageVirtualizedRender();
    if(_programmaticScroll&&(performance.now()-_programmaticScrollSetAt)>150) _programmaticScroll=false;
    if(_programmaticScroll) return;
    _markMessageVirtualScrollActive();
    cancelAnimationFrame(_scrollRaf);
    _scrollRaf=requestAnimationFrame(()=>{
      const top=el.scrollTop;
      const bottomDistance=el.scrollHeight-top-el.clientHeight;
      const nearBottom=bottomDistance<250;
      // #4702: iOS Safari (esp. portrait) resolves its dynamic toolbar height
      // AFTER first paint. When the toolbar collapses the scroller GROWS
      // (clientHeight increases), which fires a scroll event with a DECREASED
      // scrollTop even though the user never scrolled. Without this guard that
      // reflow is misread as an upward scroll and falsely unpins a freshly-opened
      // session, stranding portrait readers at the top (sibling: #4701). On
      // desktop/landscape the scroller height is stable, so `grew` is always
      // false and behavior is byte-identical.
      const grew=_lastMessageClientHeight!==null&&el.clientHeight>_lastMessageClientHeight+1;
      _lastMessageClientHeight=el.clientHeight;
      const movedUp=!grew&&_lastScrollTop!==null&&top<_lastScrollTop-2;
      const movedDown=_lastScrollTop!==null&&top>_lastScrollTop+2;
      // Suppress the post-render scroll artifact: right after renderMessages()
      // rebuilds #msgInner, the browser can emit a non-user upward scroll event.
      // The typeof guards keep this branch inert in unit harnesses that inject
      // the listener body without these helpers (and short-circuit before any
      // call), while production evaluates the real intent/recency helpers.
      // #4970: also require no recent low-delta message-pane wheel intent, so a
      // gentle trackpad scroll-up (deltaY>-30) right after a render still unpins
      // instead of being swallowed for the artifact window.
      // #4970 review: and never suppress while a scrollbar drag is active — a
      // manual scrollbar-drag upward scroll inside the window is real intent.
      // typeof guard keeps the #4295 node harness (no _scrollbarDragActive
      // injected) inert via short-circuit.
      // #4970 review (greptile P1): likewise skip suppression when the reader
      // recently scrolled the pane with the keyboard — a keyboard scroll-up is
      // real intent that produces a native scroll event with no wheel/touch.
      if(movedUp
        && typeof _recentMessageRenderArtifactWindow==='function'
        && typeof _recentMessageTouchScrollIntent==='function'
        && typeof _recentNonMessageScrollIntent==='function'
        && typeof _recentMessageWheelIntent==='function'
        && typeof _recentMessageKeyScrollIntent==='function'
        && (typeof _scrollbarDragActive==='undefined' || !_scrollbarDragActive)
        && _recentMessageRenderArtifactWindow(1400)
        && !_recentMessageTouchScrollIntent()
        && !_recentNonMessageScrollIntent()
        && !_recentMessageWheelIntent()
        && !_recentMessageKeyScrollIntent()){
        _lastScrollTop=top;
        return;
      }
      _lastScrollTop=top;
      if(movedUp){
        _cancelBottomSettle();
        _nearBottomCount=0;
        _scrollPinned=false;
        _messageUserUnpinned=true;
      }else if(movedDown&&nearBottom){
        _nearBottomCount=_nearBottomCount+1;
        if(_nearBottomCount>=2){
          // Only re-pin when the reader has genuinely reached the true bottom
          // tail (<=80px). nearBottom spans a ~250px band, so proximity alone
          // must NOT clear the sticky unpin flag (#4295) — a reader scanning the
          // last lines mid-stream would otherwise get yanked back to the bottom.
          if(!_messageUserUnpinned||bottomDistance<=80){
            _messageUserUnpinned=false;
            _scrollPinned=true;
          }
          _nearBottomCount=0;
        }
      }else if(!_messageUserUnpinned){
        if(nearBottom){
          _nearBottomCount=_nearBottomCount+1;
          if(_nearBottomCount>=2){_scrollPinned=true;_nearBottomCount=0;}
        }else if(!movedUp && _autoScrollFollow && _scrollPinned){
          // Content-grew-beneath-a-pinned-viewport case (NOT a user scroll-away).
          // During streaming on a tall transcript (esp. mobile, where chunks land
          // fast), new content increases scrollHeight under a stationary viewport,
          // so bottomDistance crosses the nearBottom threshold even though the
          // reader never scrolled (top did NOT move up, _messageUserUnpinned is
          // false). Previously this fell through to `_scrollPinned=false`, killing
          // auto-follow mid-stream: the follow writer and this listener then fought
          // frame-by-frame, the viewport stalled while content kept growing, and it
          // was progressively stranded mid-transcript (the "jump back" report).
          // Keep the pin and re-snap to the true bottom instead of unpinning.
          _nearBottomCount=0;
          if(typeof _setMessageScrollToBottom==='function') _setMessageScrollToBottom();
        }else{
          _nearBottomCount=0;
          _scrollPinned=false;
        }
      }else if(!nearBottom){
        _nearBottomCount=0;
        _scrollPinned=false;
      }
      if(nearBottom) _clearNewMessageScrollCue();
      const showBottomButton=!_scrollPinned && el.scrollHeight-top-el.clientHeight>80;
      _syncScrollToBottomCue(showBottomButton,{newMessage:_newMessageCueVisible});
      if(typeof _updateSessionStartJumpButton==='function') _updateSessionStartJumpButton();
      // Prefetch older messages before the reader hits the hard top. Prepending
      // then preserving scrollTop is seamless only if there is runway left for
      // the user's continued upward wheel/touch movement.
      const olderPrefetchPx=Math.max(600,el.clientHeight*1.5);
      if(_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function'){
        if(_recentMessageTouchScrollIntent()) _scheduleDeferredOlderMessagesLoad();
        else _loadOlderMessages();
      }
    });
  });
})();
function _fmtTokens(n){if(!n||n<0)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return String(n);}
function _formatTurnDuration(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.round(n));
  if(total<60)return`${total}s`;
  const h=Math.floor(total/3600);
  const m=Math.floor((total%3600)/60);
  const s=total%60;
  if(h)return`${h}h ${m}m`;
  return`${m}m ${s}s`;
}
function _formatFirstToken(ms){
  const n=Number(ms);
  if(!Number.isFinite(n)||n<0)return'';
  if(n<1000)return`${Math.round(n)}ms`;
  return`${(n/1000).toFixed(2)}s`;
}
function _formatActiveElapsedTimer(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.floor(n));
  const m=Math.floor(total/60);
  const s=total%60;
  return`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function _processedElapsedLabel(seconds){
  const text=_formatTurnDuration(seconds);
  return text?t('processed_elapsed',text):'';
}
const _COMPRESSION_ELAPSED_MAX_SECONDS=5*60;
let _compressionElapsedTimer=null;
function _compressionElapsedStartedAt(state){const n=Number(state&&state.startedAt);return Number.isFinite(n)&&n>0?n:null;}
function _compressionElapsedLabel(state){
  const started=_compressionElapsedStartedAt(state);
  if(!started)return'';
  const elapsed=Math.max(0,(Date.now()/1000)-started);
  if(elapsed>=_COMPRESSION_ELAPSED_MAX_SECONDS)return '5+ min';
  return _formatActiveElapsedTimer(elapsed);
}
function _compressionElapsedExpired(state){const started=_compressionElapsedStartedAt(state);return !!(started&&((Date.now()/1000)-started)>=_COMPRESSION_ELAPSED_MAX_SECONDS);}
function _compressionLiveCardNode(){return document.querySelector('[data-live-compression-card="1"][data-compression-started-at]');}
function _compressionLiveCardState(){
  const node=_compressionLiveCardNode();
  const started=Number(node&&node.getAttribute('data-compression-started-at'));
  if(!node||!S.session||!Number.isFinite(started)||started<=0)return null;
  return {sessionId:S.session.session_id,phase:'running',automatic:true,message:node.getAttribute('data-compression-message')||'Auto-compressing context...',startedAt:started};
}
function _updateCompressionElapsedCards(state){
  if(!state)return false;
  return false;
}
function _updateCompressionElapsedTimer(){
  const state=_compressionStateForCurrentSession()||_compressionLiveCardState();
  if(state&&state.automatic&&state.phase==='running'){
    _updateCompressionElapsedCards(state);
    if(_compressionElapsedExpired(state)) _clearCompressionElapsedTimer();
  }else _clearCompressionElapsedTimer();
}
function _startCompressionElapsedTimer(){if(!_compressionElapsedTimer)_compressionElapsedTimer=setInterval(_updateCompressionElapsedTimer,1000);}
function _clearCompressionElapsedTimer(){if(_compressionElapsedTimer){clearInterval(_compressionElapsedTimer);_compressionElapsedTimer=null;}}
let _activityElapsedTimer=null;
let _activityElapsedTimerGroup=null;
function _activityNowSeconds(){return Date.now()/1000;}
function _isActivityTimerGroup(group){
  return !!(group&&group.getAttribute('data-run-activity-group')==='1');
}
function _activityElapsedStartedAt(group){
  if(!group)return null;
  const raw=(group.dataset&&group.dataset.turnStartedAt!==undefined&&group.dataset.turnStartedAt!=='')
    ?group.dataset.turnStartedAt
    :(S.session&&S.session.pending_started_at);
  const started=Number(raw);
  return Number.isFinite(started)&&started>0?started:null;
}
function _activityElapsedLabel(group){
  const started=_activityElapsedStartedAt(group);
  if(!started)return'';
  return _formatActiveElapsedTimer(_activityNowSeconds()-started);
}
function _activityProcessedElapsedLabel(group){
  const started=_activityElapsedStartedAt(group);
  if(!started)return'';
  return _processedElapsedLabel(_activityNowSeconds()-started);
}
function _activitySettledProcessedLabel(group){
  let durationText=_formatTurnDuration(group&&group.dataset&&group.dataset.turnDuration);
  if(!durationText&&group){
    const durationEl=group.querySelector&&group.querySelector('.tool-call-group-duration');
    const legacy=String(durationEl&&durationEl.textContent||'').replace(/^\s*Done in\s+/i,'').trim();
    if(legacy) durationText=legacy;
  }
  return durationText?t('processed_elapsed',durationText):'';
}
function _activityMarkObserved(group, ts){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const stamp=Number(ts||_activityNowSeconds());
  if(Number.isFinite(stamp)&&stamp>0) group.setAttribute('data-last-activity-at',String(stamp));
}
function _activityLastObservedAge(group){
  const stamp=Number(group&&group.getAttribute('data-last-activity-at'));
  if(!Number.isFinite(stamp)||stamp<=0)return null;
  return Math.max(0,_activityNowSeconds()-stamp);
}
function _activityClockLabel(ts){
  const stamp=Number(ts||_activityNowSeconds());
  if(!Number.isFinite(stamp)||stamp<=0)return'';
  try{return new Date(stamp*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});}catch(_){return'';}
}
// Full date+time label for the worklog event-time tooltip (title attr). Guards the
// same valid-Date range as _timestampSeconds so a bad epoch never yields "Invalid
// Date" in the tooltip. (#5739)
function _activityFullClockLabel(ts){
  const stamp=Number(ts);
  if(!Number.isFinite(stamp)||stamp<=0||stamp>8.64e12)return'';
  try{
    const d=new Date(stamp*1000);
    if(isNaN(d.getTime()))return'';
    return d.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }catch(_){return'';}
}
function _timestampSeconds(value){
  if(value===undefined||value===null||value==='') return null;
  if(value instanceof Date){
    const stamp=value.getTime()/1000;
    return (Number.isFinite(stamp)&&stamp>0&&Math.abs(stamp)<=8.64e12)?stamp:null;
  }
  const numeric=Number(value);
  if(Number.isFinite(numeric)&&numeric>0){
    const stamp=numeric>1e12?numeric/1000:numeric;
    // Reject epochs outside JavaScript's valid Date range (±8.64e15 ms = ±8.64e12 s);
    // otherwise new Date(stamp*1000) yields "Invalid Date" and renders literally
    // (e.g. a garbage numeric timestamp like 1e20 passes finite/>0). (#5739 gate.)
    return (Number.isFinite(stamp)&&stamp>0&&stamp<=8.64e12)?stamp:null;
  }
  if(typeof value==='string'){
    const text=value.trim();
    if(!text||/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(text)) return null;
    const parsed=Date.parse(text);
    if(Number.isFinite(parsed)&&parsed>0){
      const stamp=parsed/1000;
      return stamp<=8.64e12?stamp:null;
    }
  }
  return null;
}
function _firstValidTimestampSeconds(...values){
  for(const value of values){
    const stamp=_timestampSeconds(value);
    if(stamp) return stamp;
  }
  return null;
}
function _transparentEventTimestampSeconds(row, opts){
  opts=opts||{};
  for(const key of ['ts','timestamp','created_at']){
    const stamp=_timestampSeconds(opts[key]);
    if(stamp) return stamp;
  }
  const toolCall=opts.toolCall||row&&row._tcData||null;
  if(toolCall&&typeof toolCall==='object'){
    for(const key of ['ts','timestamp','created_at','started_at','completed_at']){
      const stamp=_timestampSeconds(toolCall[key]);
      if(stamp) return stamp;
    }
  }
  if(row&&typeof row.getAttribute==='function'){
    for(const key of ['data-event-at','data-activity-at']){
      const stamp=_timestampSeconds(row.getAttribute(key));
      if(stamp) return stamp;
    }
  }
  if(opts.live===true) return _activityNowSeconds();
  return null;
}
function _syncTransparentEventTimestamp(row, header, opts){
  if(!row||!header) return null;
  opts=opts||{};
  const showEventTimestamp=!(typeof window!=='undefined'&&window._transparentEventTimestamps===false);
  const live=opts.live===true||row.getAttribute&&(
    row.getAttribute('data-live-tid')==='1'||
    row.getAttribute('data-live-thinking')==='1'||
    row.getAttribute('data-live-assistant')==='1'||
    row.getAttribute('data-live-stream-owned')==='1'
  );
  const explicitTs=_firstValidTimestampSeconds(opts.ts, opts.timestamp, opts.created_at);
  const toolCall=opts.toolCall||row&&row._tcData||null;
  const toolTs=toolCall&&typeof toolCall==='object'
    ? _firstValidTimestampSeconds(
      toolCall.ts,
      toolCall.timestamp,
      toolCall.created_at,
      toolCall.started_at,
      toolCall.completed_at
    )
    : null;
  const attrTs=row&&typeof row.getAttribute==='function'
    ? _firstValidTimestampSeconds(
      row.getAttribute('data-event-at'),
      row.getAttribute('data-activity-at')
    )
    : null;
  const ts=explicitTs||toolTs||attrTs||(live?_activityNowSeconds():null);
  const label=ts?_activityClockLabel(ts):'';
  let timeEl=header.querySelector('.transparent-event-time');
  if(!label){
    if(timeEl) timeEl.remove();
    row.removeAttribute('data-event-at');
    row.removeAttribute('data-event-at-source');
    return null;
  }
  const source=explicitTs||toolTs||attrTs?'event':'live';
  row.setAttribute('data-event-at',String(ts));
  row.setAttribute('data-event-at-source',source);
  if(!showEventTimestamp){
    if(timeEl) timeEl.remove();
    return null;
  }
  if(!timeEl){
    timeEl=document.createElement('span');
    timeEl.className='transparent-event-time';
  }
  timeEl.textContent=label;
  // Full date+time tooltip: the bare clock label is date-ambiguous when a settled
  // session is reviewed days later (or a run crosses midnight), and timing is the
  // whole point of this label. (#5739 Fable UX fix.)
  const fullLabel=_activityFullClockLabel(ts);
  if(fullLabel) timeEl.setAttribute('title',fullLabel); else timeEl.removeAttribute('title');
  timeEl.setAttribute('data-event-at',String(ts));
  timeEl.setAttribute('data-event-at-source',source);
  const anchor=header.querySelector('.transparent-event-status,.thinking-card-btn-row,.tool-card-toggle,.thinking-card-toggle');
  if(timeEl.parentNode!==header){
    if(anchor&&anchor.parentNode===header) header.insertBefore(timeEl,anchor);
    else header.appendChild(timeEl);
  }else if(anchor&&timeEl.nextSibling!==anchor){
    header.insertBefore(timeEl,anchor);
  }
  return timeEl;
}

window.HermesUI.register('composerControls', {
  syncToolsetsChip,
  toggleToolsetsDropdown,
  openMobileComposerConfig,
  openComposerContextMenu,
});
