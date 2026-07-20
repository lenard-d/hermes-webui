import { state } from "./state.js";
import { _closeMobileSidebarAfterPanelSelection,_consumeSettingsTargetPanel,switchPanel } from "./core.js";
import { loadExtensionsPanel,loadPluginsPanel } from "./settings-extensions.js";
import { _speechPreferencesPayloadFromUi } from "./settings-preferences.js";
import { loadProvidersPanel } from "./settings-providers.js";
import { saveSettings } from "./settings-save.js";
import { _composerControlVisibilityPayload,_ensureComposerControlVisibilityState,_getComposerControlOrder,_getHiddenTabs,_getTabOrder,_renderComposerControlChips,_renderComposerSituationalControlChips,_setComposerControlOrder } from "./settings-state.js";

// Panels domain: settings navigation and autosave

const settingsSectionListeners = new Set();

export function onSettingsSectionChange(listener) {
  settingsSectionListeners.add(listener);
  return () => settingsSectionListeners.delete(listener);
}

export function switchSettingsSection(name,opts){
  // If the main content is not showing settings, just remember the section
  // without force-switching the panel. The section will be applied when the
  // user next opens settings via switchPanel(). (#appearance-auto-reopen)
  if (state._currentPanel !== 'settings') {
    state._currentSettingsSection = name;
    state._settingsSection = name;
    return;
  }
  let section=(name==='appearance'||name==='preferences'||name==='providers'||name==='plugins'||name==='extensions'||name==='system'||name==='help')?name:'conversation';
  // Deep-linking to the Plugins pane when the tab is hidden (no plugins
  // installed, #3457) falls back to Conversation. Resolve this BEFORE toggling
  // panes/sidebar/dropdown below so every downstream selection uses the
  // corrected section — otherwise the plugins pane would still render active
  // but empty. (#3457)
  if(section==='plugins'){
    const pluginsTabBtn=document.querySelector('[data-settings-section="plugins"]');
    if(pluginsTabBtn && pluginsTabBtn.style.display==='none') section='conversation';
  }
  state._settingsSection=section;
  state._currentSettingsSection=section;
  const map={conversation:'Conversation',appearance:'Appearance',preferences:'Preferences',providers:'Providers',plugins:'Plugins',extensions:'Extensions',system:'System',help:'Help'};
  // Sidebar menu items
  document.querySelectorAll('#settingsMenu .side-menu-item').forEach(it=>{
    it.classList.toggle('active', it.dataset.settingsSection===section);
  });
  // Panes in main
  ['conversation','appearance','preferences','providers','plugins','extensions','system','help'].forEach(key=>{
    const pane=$('settingsPane'+map[key]);
    if(pane) pane.classList.toggle('active', key===section);
  });
  // Sync mobile dropdown
  const dd=$('settingsSectionDropdown');
  if(dd && dd.value!==section) dd.value=section;
  // Lazy-load integration panels when their tabs are opened. Search
  // navigation passes skipLazyLoad: the loaders rebuild the pane DOM from a
  // fresh fetch, which would detach the field it is about to scroll to.
  if(!(opts&&opts.skipLazyLoad)){
    if(section==='providers') loadProvidersPanel();
    if(section==='plugins') loadPluginsPanel();
    if(section==='extensions') loadExtensionsPanel();
  }
  settingsSectionListeners.forEach(listener => listener(section));
  if(opts&&opts.fromSidebarItem)_closeMobileSidebarAfterPanelSelection();
}

export function _normalizeSettingsSearchText(value) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

export function _extractSettingsDescriptionText(field, labelEl) {
  const chunks = [];
  const settingsSearch = (field.dataset && field.dataset.settingsSearch) || '';
  if (settingsSearch) chunks.push(settingsSearch);
  field.querySelectorAll('[data-i18n]').forEach(node => {
    if (labelEl && (node === labelEl || labelEl.contains(node))) return;
    const key = node.dataset ? node.dataset.i18n : null;
    if (key) chunks.push(t(key));
  });
  return chunks.join(' ');
}

export function _extractSettingsValueText(field) {
  const chunks = [];
  const controls = [...field.querySelectorAll('select, input, textarea')];
  controls.forEach(control => {
    const tagName = (control.tagName || '').toLowerCase();
    if (tagName === 'select') {
      control.querySelectorAll('option').forEach(option => {
        if (option.dataset && option.dataset.i18n) {
          chunks.push(t(option.dataset.i18n));
        } else {
          chunks.push(option.textContent);
        }
      });
      return;
    }
    const type = (control.getAttribute && control.getAttribute('type')) || control.type || '';
    if (tagName === 'input' && ['checkbox', 'radio', 'file', 'submit', 'reset', 'button'].includes(type)) return;
    if (control.value) chunks.push(control.value);
  });
  return chunks.join(' ');
}

export async function _buildSettingsIndex() {
  if (state._settingsIndex) return;
  // Memoize the in-flight build so concurrent searches share one pass; the
  // lazy pane loaders are not guaranteed re-entrant.
  if (state._settingsIndexPromise) return state._settingsIndexPromise;
  const promise = (async () => {
    // Ensure lazy-loaded panes are populated before reading the DOM
    await Promise.all([loadProvidersPanel(), loadPluginsPanel(), loadExtensionsPanel()]);
    const index = [];
    const add = (entry) => {
      index.push({ ...entry, _settingsSearchIndex: index.length });
    };
    const sectionMap = {
      settingsPaneConversation: 'conversation',
      settingsPaneAppearance: 'appearance',
      settingsPanePreferences: 'preferences',
      settingsPaneProviders: 'providers',
      settingsPanePlugins: 'plugins',
      settingsPaneExtensions: 'extensions',
      settingsPaneSystem: 'system',
      settingsPaneHelp: 'help',
    };
    for (const [paneId, sectionKey] of Object.entries(sectionMap)) {
      const pane = $(paneId);
      if (!pane) continue;
      pane.querySelectorAll('.settings-field').forEach(field => {
        // The i18n key may live on the <label> itself (label[data-i18n]) OR on
        // a child of the label — the common toggle shape is
        // <label><input><span data-i18n="..."></span></label>. Match both, plus
        // a plain <label> with no i18n key, so every field is searchable
        // (previously only label[data-i18n] indexed, silently dropping most
        // checkbox settings). #4340 review fix.
        const labelEl = field.querySelector('label[data-i18n], label [data-i18n], label');
        if (!labelEl) return;
        const i18nKey = labelEl.dataset ? labelEl.dataset.i18n : undefined;
        const titleText = (i18nKey && t(i18nKey)) || labelEl.textContent.trim();
        if (!titleText) return;
        const valueText = _normalizeSettingsSearchText(_extractSettingsValueText(field));
        const descriptionText = _normalizeSettingsSearchText(_extractSettingsDescriptionText(field, labelEl));
        const searchBlob = [titleText, valueText, descriptionText, field.textContent]
          .filter(Boolean)
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim();
        add({
          label: titleText,
          titleText,
          valueText,
          descriptionText,
          searchBlob,
          sectionKey,
          i18nKey,
          el: field,
        });
      });
      if (sectionKey === 'providers') {
        pane.querySelectorAll('.provider-card').forEach(card => {
          const cardName = ((card.querySelector('.provider-card-name') || {}).textContent || '').trim();
          if (cardName) {
            const titleText = cardName;
            const valueText = _normalizeSettingsSearchText(_extractSettingsValueText(card));
            const descriptionText = _normalizeSettingsSearchText(_extractSettingsDescriptionText(card));
            const searchBlob = [cardName, valueText, descriptionText, card.textContent]
              .filter(Boolean)
              .join(' ')
              .replace(/\s+/g, ' ')
              .trim();
            add({
              label: cardName,
              titleText,
              valueText,
              descriptionText,
              searchBlob,
              sectionKey,
              el: card,
              cardName,
            });
          }
          card.querySelectorAll('.provider-card-field').forEach(field => {
            const fieldLabel = ((field.querySelector('.provider-card-label') || {}).textContent || '').trim();
            const label = [cardName, fieldLabel].filter(Boolean).join(' ');
            if (!label) return;
            const valueText = _normalizeSettingsSearchText(_extractSettingsValueText(field));
            const descriptionText = _normalizeSettingsSearchText(_extractSettingsDescriptionText(field));
            const searchBlob = [label, valueText, descriptionText, field.textContent]
              .filter(Boolean)
              .join(' ')
              .replace(/\s+/g, ' ')
              .trim();
            add({
              label,
              titleText: label,
              valueText,
              descriptionText,
              searchBlob,
              sectionKey,
              el: field,
              cardName,
              fieldLabel,
            });
          });
        });
      }
      if (sectionKey === 'plugins') {
        pane.querySelectorAll('.plugin-card').forEach(card => {
          const cardName = ((card.querySelector('.provider-card-name') || {}).textContent || '').trim();
          if (!cardName) return;
          const titleText = cardName;
          const valueText = _normalizeSettingsSearchText(_extractSettingsValueText(card));
          const descriptionText = _normalizeSettingsSearchText(_extractSettingsDescriptionText(card));
          const searchBlob = [cardName, valueText, descriptionText, card.textContent]
            .filter(Boolean)
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();
          add({
            label: cardName,
            titleText,
            valueText,
            descriptionText,
            searchBlob,
            sectionKey,
            el: card,
            cardName,
          });
        });
      }
    }
    // A panel-session reset while building clears the memo; drop this result
    // instead of resurrecting a stale index for the new session.
    if (state._settingsIndexPromise === promise) state._settingsIndex = index;
  })().catch(e => { if (state._settingsIndexPromise === promise) state._settingsIndexPromise = null; throw e; });
  state._settingsIndexPromise = promise;
  return promise;
}

export async function filterSettings(query) {
  const resultsEl = $('settingsSearchResults');
  if (!resultsEl) return;
  const q = (query || '').trim().toLowerCase();
  if (!q) { ++state._settingsSearchSeq; resultsEl.style.display = 'none'; resultsEl.innerHTML = ''; return; }
  const seq = ++state._settingsSearchSeq;
  await _buildSettingsIndex();
  // A newer keystroke superseded this query while the index was building.
  if (seq !== state._settingsSearchSeq) return;
  const sectionLabels = {
    conversation: t('settings_tab_conversation') || 'Conversation',
    appearance: t('settings_tab_appearance') || 'Appearance',
    preferences: t('settings_tab_preferences') || 'Preferences',
    providers: t('providers_tab_title') || 'Providers',
    plugins: t('settings_tab_plugins') || 'Plugins',
    extensions: t('settings_tab_extensions') || 'Extensions',
    system: t('settings_tab_system') || 'System',
    help: t('settings_tab_help') || 'Help',
  };
  const matches = (state._settingsIndex || []).map((entry) => {
    const score = _scoreSettingsSearchMatch(entry, q);
    return score ? { entry, score, index: entry._settingsSearchIndex } : null;
  }).filter(Boolean);
  if (!matches.length) {
    resultsEl.innerHTML = `<div class="settings-search-empty">${esc(t('settings_search_no_results') || 'No settings found.')}</div>`;
    resultsEl.style.display = '';
    return;
  }
  resultsEl.innerHTML = '';
  matches.sort((left, right) => {
    if (left.score.bucketIndex !== right.score.bucketIndex) {
      return left.score.bucketIndex - right.score.bucketIndex;
    }
    if (left.score.matchIndex !== right.score.matchIndex) {
      return left.score.matchIndex - right.score.matchIndex;
    }
    return left.index - right.index;
  });
  for (const { entry: m } of matches.slice(0, 12)) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'settings-search-result';
    item.innerHTML = `<span class="settings-search-section">${esc(sectionLabels[m.sectionKey] || m.sectionKey)}</span>` +
      `<span class="settings-search-arrow">›</span>` +
      `<span class="settings-search-label">${esc(m.label)}</span>`;
    item.addEventListener('click', () => {
      _navigateToSettingsField(m);
      resultsEl.style.display = 'none';
      resultsEl.innerHTML = '';
      const input = $('settingsSearch');
      if (input) input.value = '';
    });
    resultsEl.appendChild(item);
  }
  resultsEl.style.display = '';
}

export function _scoreSettingsSearchMatch(entry, q) {
  const query = (q || '').toLowerCase().trim();
  if (!query) return null;
  const buckets = [
    ['titleText', 0],
    ['valueText', 1],
    ['descriptionText', 2],
    ['searchBlob', 3],
  ];
  for (const [bucketName, bucketIndex] of buckets) {
    const hay = _normalizeSettingsSearchText(entry[bucketName]);
    if (!hay) continue;
    const matchIndex = hay.indexOf(query);
    if (matchIndex < 0) continue;
    return {
      bucketIndex,
      matchType: matchIndex === 0 ? 'prefix' : 'contains',
      matchIndex,
    };
  }
  return null;
}

export function _navigateToSettingsField(entry) {
  // The panes were populated when the index was built, so skip the tab-switch
  // lazy reload: loadProvidersPanel()/loadPluginsPanel() rebuild the pane DOM
  // from a fresh fetch and would detach the node mid-scroll.
  switchSettingsSection(entry.sectionKey, { skipLazyLoad: true });
  requestAnimationFrame(() => {
    const el = _resolveSettingsField(entry);
    if (!el) return;
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    _highlightSettingsField(el);
  });
}

export function _resolveSettingsField(entry) {
  // Re-resolve in the live DOM: any pane re-render since indexing (e.g. the
  // user visited the tab) replaces the node the index captured.
  const paneIds = {
    conversation: 'settingsPaneConversation',
    appearance: 'settingsPaneAppearance',
    preferences: 'settingsPanePreferences',
    providers: 'settingsPaneProviders',
    plugins: 'settingsPanePlugins',
    extensions: 'settingsPaneExtensions',
    system: 'settingsPaneSystem',
    help: 'settingsPaneHelp',
  };
  const pane = $(paneIds[entry.sectionKey]);
  if (pane && entry.cardName && (entry.sectionKey === 'providers' || entry.sectionKey === 'plugins')) {
    const cards = entry.sectionKey === 'providers'
      ? pane.querySelectorAll('.provider-card')
      : pane.querySelectorAll('.plugin-card');
    for (const card of cards) {
      const name = ((card.querySelector('.provider-card-name') || {}).textContent || '').trim();
      if (name !== entry.cardName) continue;
      if (entry.fieldLabel && entry.sectionKey === 'providers') {
        for (const field of card.querySelectorAll('.provider-card-field')) {
          const label = ((field.querySelector('.provider-card-label') || {}).textContent || '').trim();
          if (label === entry.fieldLabel) return field;
        }
      }
      return card;
    }
  }
  // The i18n key may sit on the label or on a child of it (span inside a
  // toggle label), so resolve via any [data-i18n] node, then climb to the
  // enclosing .settings-field. #4340 review fix.
  const labelEl = pane && entry.i18nKey
    ? pane.querySelector(`[data-i18n="${CSS.escape(entry.i18nKey)}"]`)
    : null;
  const live = labelEl && labelEl.closest('.settings-field');
  if (live) return live;
  return entry.el && entry.el.isConnected ? entry.el : null;
}

export function _highlightSettingsField(el) {
  if (!el) return;
  el.classList.remove('settings-field-highlight');
  void el.offsetWidth;
  el.classList.add('settings-field-highlight');
  setTimeout(() => el.classList.remove('settings-field-highlight'), 1800);
}

export function _syncHermesPanelSessionActions(){
  const hasSession=!!S.session;
  const visibleMessages=hasSession?(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length:0;
  const title=hasSession?(S.session.title||t('untitled')):t('active_conversation_none');
  const meta=$('hermesSessionMeta');
  const hasShare=!!(hasSession&&S.session&&S.session.share_token);
  if(meta){
    if(!hasSession){
      meta.textContent=t('active_conversation_none');
    }else{
      const base=t('active_conversation_meta', title, visibleMessages);
      meta.textContent=hasShare
        ? `${base} · ${t('share_session_status_active')}`
        : base;
    }
  }
  const setDisabled=(id,disabled)=>{
    const el=$(id);
    if(!el)return;
    el.disabled=!!disabled;
    el.classList.toggle('disabled',!!disabled);
  };
  setDisabled('btnDownload',!hasSession||visibleMessages===0);
  setDisabled('btnExportJSON',!hasSession);
  setDisabled('btnShareSession',!hasSession||visibleMessages===0);
  setDisabled('btnStopSharingSession',!hasShare);
  setDisabled('btnClearConvModal',!hasSession||visibleMessages===0);
}

// Thin wrapper: settings now live in the main content area. External callers
// (keyboard shortcuts, commands) keep working through this name.
export function toggleSettings(){
  if(state._currentPanel==='settings'){
    _closeSettingsPanel();
  } else {
    switchPanel('settings');
  }
}

export function _resetSettingsPanelState(){
  const bar=$('settingsUnsavedBar');
  if(bar) bar.style.display='none';
  _setAppearanceAutosaveStatus('');
}

export function _hideSettingsPanel(){
  _resetSettingsPanelState();
  const target = _consumeSettingsTargetPanel('chat');
  if(state._currentPanel==='settings') switchPanel(target, {bypassSettingsGuard:true});
}

// Close with unsaved-changes check. If dirty, show a confirm dialog.
export function _closeSettingsPanel(){
  if(!state._settingsDirty){
    _revertSettingsPreview();
    _hideSettingsPanel();
    return;
  }
  state._pendingSettingsTargetPanel = state._pendingSettingsTargetPanel || 'chat';
  _showSettingsUnsavedBar();
}

// Revert live DOM/localStorage to what they were when the panel opened
export function _revertSettingsPreview(){
  // Appearance controls autosave immediately. Closing/discarding the settings
  // panel must not roll back theme, skin, or font-size after the user sees the
  // inline saved state.
}

// Show the "Unsaved changes" bar inside the settings panel
export function _showSettingsUnsavedBar(){
  let bar = $('settingsUnsavedBar');
  if(bar){ bar.style.display=''; return; }
  // Create it
  bar = document.createElement('div');
  bar.id = 'settingsUnsavedBar';
  bar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;background:rgba(233,69,96,.12);border:1px solid rgba(233,69,96,.3);border-radius:8px;padding:10px 14px;margin:0 0 12px;font-size:13px;';
  bar.innerHTML = `<span style="color:var(--text)">${esc(t('settings_unsaved_changes'))}</span>`
    + '<span style="display:flex;gap:8px">'
    + `<button onclick="_discardSettings()" style="padding:5px 12px;border-radius:6px;border:1px solid var(--border2);background:rgba(255,255,255,.06);color:var(--muted);cursor:pointer;font-size:12px;font-weight:600">${esc(t('discard'))}</button>`
    + `<button onclick="saveSettings(true)" style="padding:5px 12px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-size:12px;font-weight:600">${esc(t('save'))}</button>`
    + '</span>';
  const body = document.querySelector('#mainSettings .settings-main') || document.querySelector('.settings-main');
  if(body) body.prepend(bar);
}

export function _discardSettings(){
  _revertSettingsPreview();
  state._settingsDirty = false;
  _hideSettingsPanel();
}

// Mark settings as dirty whenever anything changes
export function _markSettingsDirty(){
  state._settingsDirty = true;
}

// Apply TTS enabled state: toggles a body class so the CSS rule
// `body.tts-enabled .msg-tts-btn` shows/hides the speaker icon. We toggle the
// body class instead of writing inline `style.display` because the parent
// `.msg-action-btn` has no display rule, so clearing the inline style let the
// `.msg-tts-btn{display:none;}` cascade re-hide the button (#1409).
export function _applyTtsEnabled(enabled){
  document.body.classList.toggle('tts-enabled', !!enabled);
}

// Read + sanitize the JSON/YAML structured code-block default-view controls
// (#484). mode is one of auto|on|off; lines is clamped to an int 1..1000 with a
// fallback of 10 (the original hardcoded threshold).
export function _structuredCodeViewFromUi(){
  const modeSel=$('settingsStructuredCodeMode');
  const mode=modeSel&&['auto','on','off'].includes(modeSel.value)?modeSel.value:'auto';
  const linesField=$('settingsStructuredCodeAutoLines');
  const n=parseInt((linesField||{}).value,10);
  const lines=(Number.isFinite(n)&&n>=1&&n<=1000)?n:10;
  return {structured_code_default_view:mode,structured_code_auto_tree_lines:lines};
}

// Apply the structured code-block settings to runtime globals and re-render the
// transcript so already-rendered JSON/YAML blocks pick up the new default. The
// per-block Raw/Tree toggle is unaffected.
export function _applyStructuredCodeViewSettings(mode,lines,rerender){
  window._structuredCodeDefaultView=['auto','on','off'].includes(mode)?mode:'auto';
  const n=parseInt(lines,10);
  window._structuredCodeAutoTreeLines=(Number.isFinite(n)&&n>=1&&n<=1000)?n:10;
  if(rerender){
    if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
    if(typeof renderMessages==='function') renderMessages({preserveScroll:true});
  }
}

// The Auto-threshold input is only meaningful in 'auto' mode; disable it
// otherwise so the control reads as inactive without hiding it.
export function _syncStructuredCodeLinesEnabled(){
  const modeSel=$('settingsStructuredCodeMode');
  const linesField=$('settingsStructuredCodeAutoLines');
  // Both controls live in the same settings-field and are present together;
  // if either is missing there's nothing to sync.
  if(!modeSel||!linesField) return;
  const isAuto=modeSel.value==='auto';
  linesField.disabled=!isAuto;
  linesField.style.opacity=isAuto?'':'0.5';
}

export function _appearancePayloadFromUi(){
  const worklogDetailsExpanded=!!($('settingsWorklogDetailsExpandedDefault')||{}).checked;
  const chatActivityModeSel=$('settingsChatActivityDisplayMode');
  const transparentEventTimestamps=$('settingsTransparentEventTimestamps');
  return {
    theme: ($('settingsTheme')||{}).value || localStorage.getItem('hermes-theme') || 'dark',
    skin: ($('settingsSkin')||{}).value || localStorage.getItem('hermes-skin') || 'default',
    font_size: ($('settingsFontSize')||{}).value || localStorage.getItem('hermes-font-size') || 'default',
    chat_activity_display_mode: chatActivityModeSel&&(chatActivityModeSel.value==='transparent_stream'||chatActivityModeSel.value==='hide_all_activity')
      ? chatActivityModeSel.value
      : 'compact_worklog',
    transparent_stream_event_timestamps: transparentEventTimestamps ? transparentEventTimestamps.checked : true,
    session_jump_buttons: !!($('settingsSessionJumpButtons')||{}).checked,
    session_endless_scroll: !!($('settingsSessionEndlessScroll')||{}).checked,
    auto_scroll_follow: !!($('settingsAutoScrollFollow')||{}).checked,
    render_user_markdown: !!($('settingsRenderUserMarkdown')||{}).checked,
    large_text_paste_as_attachment: !!($('settingsLargeTextPasteAsAttachment')||{}).checked,
    project_quick_create_buttons: !!($('settingsProjectQuickCreate')||{}).checked,
    ..._structuredCodeViewFromUi(),
    show_titlebar_profile: !!($('settingsShowTitlebarProfile')||{}).checked,
    worklog_details_expanded_default: worklogDetailsExpanded,
    activity_feed_expanded_default: worklogDetailsExpanded,
    ..._composerControlVisibilityPayload(),
    composer_control_order: _getComposerControlOrder(),
    hidden_tabs: _getHiddenTabs(),
    tab_order: _getTabOrder(),
  };
}

export function _syncChatActivityDisplayModeControl(mode){
  const next=mode==='transparent_stream'||mode==='hide_all_activity' ? mode : 'compact_worklog';
  const select=$('settingsChatActivityDisplayMode');
  if(select) select.value=next;
  document.querySelectorAll('[data-chat-activity-mode]').forEach(btn=>{
    const active=btn.getAttribute('data-chat-activity-mode')===next;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  window._chatActivityDisplayMode=next;
  window._transparentStream=next==='transparent_stream';
  if(typeof _syncTransparentEventTimestampsControl==='function') _syncTransparentEventTimestampsControl(window._transparentEventTimestamps,next);
  if(next==='hide_all_activity'&&typeof window._hideLiveActivityForFinalAnswerOnly==='function') window._hideLiveActivityForFinalAnswerOnly();
}

export function _syncTransparentEventTimestampsControl(enabled, mode){
  const next=enabled!==false;
  const activeMode=mode==='transparent_stream'||mode==='hide_all_activity' ? mode : (window._chatActivityDisplayMode||'compact_worklog');
  const checkbox=$('settingsTransparentEventTimestamps');
  if(checkbox){
    checkbox.checked=next;
    checkbox.disabled=activeMode!=='transparent_stream';
    checkbox.style.opacity=activeMode==='transparent_stream'?'':'0.5';
  }
  window._transparentEventTimestamps=next;
}

export function _pickChatActivityDisplayMode(mode){
  _syncChatActivityDisplayModeControl(mode);
  if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
  if(typeof renderMessages==='function') renderMessages({preserveScroll:true});
  _scheduleAppearanceAutosave();
}

export function _pickTransparentEventTimestamps(enabled){
  _syncTransparentEventTimestampsControl(enabled,window._chatActivityDisplayMode);
  if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
  if(typeof renderMessages==='function') renderMessages({preserveScroll:true});
  _scheduleAppearanceAutosave();
}

export function _setAppearanceAutosaveStatus(state){
  const el=$('settingsAppearanceAutosaveStatus');
  if(!el) return;
  el.className='settings-autosave-status';
  if(!state){
    el.textContent='';
    return;
  }
  el.classList.add('is-'+state);
  if(state==='saving'){
    el.textContent=t('settings_autosave_saving');
  }else if(state==='saved'){
    el.textContent=t('settings_autosave_saved');
  }else if(state==='failed'){
    el.innerHTML=`<span>${esc(t('settings_autosave_failed'))}</span> <button type="button" onclick="_retryAppearanceAutosave()">${esc(t('settings_autosave_retry'))}</button>`;
  }
}

export function _rememberAppearanceSaved(payload){
  if(!payload) return;
  state._settingsThemeOnOpen=payload.theme||localStorage.getItem('hermes-theme')||'dark';
  state._settingsSkinOnOpen=payload.skin||localStorage.getItem('hermes-skin')||'default';
  state._settingsFontSizeOnOpen=payload.font_size||localStorage.getItem('hermes-font-size')||'default';
}

export function _scheduleAppearanceAutosave(){
  const payload=_appearancePayloadFromUi();
  // Keep discard/close behavior aligned with the new mental model: appearance
  // changes are committed immediately instead of treated as preview-only edits.
  _rememberAppearanceSaved(payload);
  state._settingsAppearanceAutosaveRetryPayload=payload;
  _setAppearanceAutosaveStatus('saving');
  if(state._settingsAppearanceAutosaveTimer) clearTimeout(state._settingsAppearanceAutosaveTimer);
  state._settingsAppearanceAutosaveTimer=setTimeout(()=>_autosaveAppearanceSettings(payload),350);
}

export async function _autosaveAppearanceSettings(payload){
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    state._settingsAppearanceAutosaveRetryPayload=null;
    _rememberAppearanceSaved(payload);
    if(saved&&saved.font_size){
      localStorage.setItem('hermes-font-size',saved.font_size);
    }
    if(saved){
      window._sessionJumpButtonsEnabled=!!saved.session_jump_buttons;
      if(Object.prototype.hasOwnProperty.call(saved,'chat_activity_display_mode')){
        const beforeMode=window._chatActivityDisplayMode;
        const beforeTimestamps=window._transparentEventTimestamps!==false;
        _syncChatActivityDisplayModeControl(saved.chat_activity_display_mode);
        _syncTransparentEventTimestampsControl(saved.transparent_stream_event_timestamps, saved.chat_activity_display_mode);
        if(window._chatActivityDisplayMode!==beforeMode||((window._transparentEventTimestamps!==false)!==beforeTimestamps)){
          if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
          if(typeof renderMessages==='function') renderMessages({preserveScroll:true});
        }
      }
      if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
    }
    window._sessionEndlessScrollEnabled=!!(saved&&saved.session_endless_scroll);
    window._autoScrollFollow=!saved||saved.auto_scroll_follow!==false;
    window._largeTextPasteAsAttachment=!saved||saved.large_text_paste_as_attachment!==false;
    window._projectQuickCreate=!!(saved&&saved.project_quick_create_buttons);
    if(saved&&Object.prototype.hasOwnProperty.call(saved,'structured_code_default_view')){
      // Re-sync from the server-validated/clamped values so the UI and runtime
      // globals match exactly what was persisted.
      _applyStructuredCodeViewSettings(saved.structured_code_default_view,saved.structured_code_auto_tree_lines,false);
      const modeSel=$('settingsStructuredCodeMode');
      if(modeSel) modeSel.value=window._structuredCodeDefaultView;
      const linesField=$('settingsStructuredCodeAutoLines');
      if(linesField) linesField.value=window._structuredCodeAutoTreeLines;
      _syncStructuredCodeLinesEnabled();
    }
    if(saved&&payload&&Object.prototype.hasOwnProperty.call(payload,'worklog_details_expanded_default')&&(
      Object.prototype.hasOwnProperty.call(saved,'worklog_details_expanded_default') ||
      Object.prototype.hasOwnProperty.call(saved,'activity_feed_expanded_default')
    )){
      window._worklogDetailsExpandedByDefault=!!(
        Object.prototype.hasOwnProperty.call(saved,'worklog_details_expanded_default')
          ? saved.worklog_details_expanded_default
          : saved.activity_feed_expanded_default
      );
    }
    if(saved){
      _ensureComposerControlVisibilityState(saved);
      if(Array.isArray(saved.composer_control_order)){
        const nextOrder=_setComposerControlOrder(saved.composer_control_order);
        if(typeof window._applyComposerControlOrder==='function') window._applyComposerControlOrder(nextOrder);
      }
      _renderComposerControlChips();
      _renderComposerSituationalControlChips();
      if(typeof _applyComposerFooterVisibilitySettings==='function') _applyComposerFooterVisibilitySettings();
    }
    _setAppearanceAutosaveStatus('saved');
  }catch(e){
    console.warn('[settings] appearance autosave failed', e);
    _setAppearanceAutosaveStatus('failed');
  }
}

export function _retryAppearanceAutosave(){
  const payload=state._settingsAppearanceAutosaveRetryPayload||_appearancePayloadFromUi();
  _setAppearanceAutosaveStatus('saving');
  _autosaveAppearanceSettings(payload);
}

// ── Phase 2: Preferences autosave (Issue #1003) ───────────────────────

const _SETTINGS_SPEECH_STORAGE_KEYS={
  tts_enabled:'hermes-tts-enabled',
  tts_auto_read:'hermes-tts-auto-read',
  tts_engine:'hermes-tts-engine',
  tts_voice:'hermes-tts-voice',
  tts_rate:'hermes-tts-rate',
  tts_pitch:'hermes-tts-pitch',
  voice_mode_button:'hermes-voice-mode-button',
  voice_continuous:'hermes-voice-continuous',
  voice_silence_ms:'hermes-voice-silence-ms',
  raw_audio_mode:'hermes-raw-audio-mode',
};

export function _captureSpeechPreferenceOwnership(settings){
  state._settingsSpeechPersistedKeys=new Set(Array.isArray(settings&&settings.persisted_speech_keys)?settings.persisted_speech_keys:[]);
  state._settingsSpeechLocalStorageKeys=new Set();
  state._settingsSpeechChangedKeys=new Set();
  Object.entries(_SETTINGS_SPEECH_STORAGE_KEYS).forEach(([settingKey,storageKey])=>{
    try{if(localStorage.getItem(storageKey)!==null) state._settingsSpeechLocalStorageKeys.add(settingKey);}catch(_){}
  });
}

export function _speechPreferenceIsOwned(settingKey){
  return state._settingsSpeechPersistedKeys.has(settingKey)||state._settingsSpeechLocalStorageKeys.has(settingKey)||state._settingsSpeechChangedKeys.has(settingKey);
}

export function _markSpeechPreferenceChanged(settingKey){
  state._settingsSpeechChangedKeys.add(settingKey);
}

export function _syncSpeechPreferenceCache(settingKey,value){
  if(!_speechPreferenceIsOwned(settingKey)) return;
  const storageKey=_SETTINGS_SPEECH_STORAGE_KEYS[settingKey];
  if(storageKey) localStorage.setItem(storageKey,String(value));
}

export function _setOwnedSpeechPayload(payload,settingKey,value){
  if(_speechPreferenceIsOwned(settingKey)) payload[settingKey]=value;
}

export function _preferencesPayloadFromUi(){
  const payload={};
  const sendKeySel=$('settingsSendKey');
  if(sendKeySel) payload.send_key=sendKeySel.value;
  const langSel=$('settingsLanguage');
  if(langSel) payload.language=langSel.value;
  const showUsageCb=$('settingsShowTokenUsage');
  if(showUsageCb) payload.show_token_usage=showUsageCb.checked;
  const showQuotaChipCb=$('settingsShowQuotaChip');
  if(showQuotaChipCb) payload.show_quota_chip=showQuotaChipCb.checked;
  const showConversationOutlineCb=$('settingsShowConversationOutline');
  if(showConversationOutlineCb) payload.show_conversation_outline=showConversationOutlineCb.checked;
  const hideSuggestionsCb=$('settingsHideSuggestions');
  if(hideSuggestionsCb) payload.hide_empty_state_suggestions=hideSuggestionsCb.checked;
  const virtualizeTranscriptCb=$('settingsVirtualizeTranscript');
  if(virtualizeTranscriptCb){
    payload.virtualize_transcript=virtualizeTranscriptCb.checked;
    // #4343: persist the opt-in marker alongside. Enabling the experimental
    // feature records an explicit post-flip opt-in so load_settings honors it
    // (a stored true WITHOUT this marker is treated as a stale pre-flip value
    // and reset to off). Unchecking clears the marker.
    payload.virtualize_transcript_optin=virtualizeTranscriptCb.checked;
  }
  const showTpsCb=$('settingsShowTps');
  if(showTpsCb) payload.show_tps=showTpsCb.checked;
  const fadeTextCb=$('settingsFadeTextEffect');
  if(fadeTextCb) payload.fade_text_effect=fadeTextCb.checked;
  const terminalAutoExpandCb=$('settingsTerminalAutoExpand');
  if(terminalAutoExpandCb) payload.terminal_auto_expand_on_output=terminalAutoExpandCb.checked;
  const workspaceTodosTabCb=$('settingsWorkspaceTodosTab');
  if(workspaceTodosTabCb) payload.workspace_todos_tab=workspaceTodosTabCb.checked;
  const apiRedactCb=$('settingsApiRedact');
  if(apiRedactCb) payload.api_redact_enabled=apiRedactCb.checked;
  const showCliCb=$('settingsShowCliSessions');
  if(showCliCb) payload.show_cli_sessions=showCliCb.checked;
  const showClaudeCodeCb=$('settingsShowClaudeCodeSessions');
  if(showClaudeCodeCb) payload.show_claude_code_sessions=showClaudeCodeCb.checked;
  const showCronCb=$('settingsShowCronSessions');
  // Gate cron sessions on CLI sessions (the server short-circuits otherwise),
  // identically to the explicit saveSettings() path, so neither save route can
  // persist show_cron_sessions=true while show_cli_sessions=false. (#3514)
  if(showCronCb) payload.show_cron_sessions=!!(showCliCb&&showCliCb.checked&&showCronCb.checked);
  const showWebhookCb=$('settingsShowWebhookSessions');
  if(showWebhookCb) payload.show_webhook_sessions=!!(showCliCb&&showCliCb.checked&&showWebhookCb.checked);
  const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');
  if(showPreviousMessagingCb) payload.show_previous_messaging_sessions=showPreviousMessagingCb.checked;
  const syncCb=$('settingsSyncInsights');
  if(syncCb) payload.sync_to_insights=syncCb.checked;
  const updateCb=$('settingsCheckUpdates');
  if(updateCb) payload.check_for_updates=updateCb.checked;
  const updateChannelSel=$('settingsUpdateChannel');
  if(updateChannelSel) payload.update_channel=updateChannelSel.value;
  const ignoreAgentUpdatesCb=$('settingsIgnoreAgentUpdates');
  if(ignoreAgentUpdatesCb) payload.ignore_agent_updates=ignoreAgentUpdatesCb.checked;
  const whatsNewSummaryCb=$('settingsWhatsNewSummary');
  if(whatsNewSummaryCb) payload.whats_new_summary_enabled=whatsNewSummaryCb.checked;
  const soundCb=$('settingsSoundEnabled');
  if(soundCb) payload.sound_enabled=soundCb.checked;
  const rtlCb=$('settingsRtl');
  if(rtlCb) payload.rtl=rtlCb.checked;
  const notifCb=$('settingsNotificationsEnabled');
  if(notifCb) payload.notifications_enabled=notifCb.checked;
  const sidebarDensitySel=$('settingsSidebarDensity');
  if(sidebarDensitySel) payload.sidebar_density=sidebarDensitySel.value;
  const pinnedLimitField=$('settingsPinnedSessionsLimit');
  if(pinnedLimitField) payload.pinned_sessions_limit=parseInt(pinnedLimitField.value,10);
  const autoTitleRefreshSel=$('settingsAutoTitleRefresh');
  if(autoTitleRefreshSel) payload.auto_title_refresh_every=parseInt(autoTitleRefreshSel.value,10);
  const defaultMessageModeSel=$('settingsDefaultMessageMode');
  if(defaultMessageModeSel) payload.default_message_mode=defaultMessageModeSel.value;
  const showBusyPlaceholderHintCb=$('settingsShowBusyPlaceholderHint');
  if(showBusyPlaceholderHintCb) payload.show_busy_placeholder_hint=showBusyPlaceholderHintCb.checked;
  const newChatOnWorkspaceSwitchCb=$('settingsNewChatOnWorkspaceSwitch');
  if(newChatOnWorkspaceSwitchCb) payload.new_chat_on_workspace_switch=newChatOnWorkspaceSwitchCb.checked;
  const botNameField=$('settingsBotName');
  if(botNameField) payload.bot_name=botNameField.value;
  Object.assign(payload,_speechPreferencesPayloadFromUi());
  return payload;
}
