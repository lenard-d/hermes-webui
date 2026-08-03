import { state } from "./state.js";
import { checkWebUIVersionSkew } from "./kanban-board.js";
import { loadExtensionsPanel } from "./settings-extensions.js";
import { loadPluginsPanel } from "./settings-plugins.js";
import { _bindMainAdvancedOptionsButton,_loadAuxiliaryModels,_renderSettingsAuthStatus,_setSettingsAuthButtonsVisible,_syncPasswordlessButton,_syncUpdateChannelBadge,_updateAuthDisabledWarning,_updateAuthWarningBadge,_updateCurrentPasswordVisibility,checkUpdatesNow,loadPasskeys } from "./settings-models-auth.js";
import { _applyStructuredCodeViewSettings,_applyTtsEnabled,_captureSpeechPreferenceOwnership,_markSettingsDirty,_markSpeechPreferenceChanged,_pickChatActivityDisplayMode,_pickTransparentEventTimestamps,_preferencesPayloadFromUi,_scheduleAppearanceAutosave,_setOwnedSpeechPayload,_structuredCodeViewFromUi,_syncChatActivityDisplayModeControl,_syncHermesPanelSessionActions,_syncSpeechPreferenceCache,_syncStructuredCodeLinesEnabled,_syncTransparentEventTimestampsControl,switchSettingsSection } from "./settings-navigation.js";
import { loadProvidersPanel } from "./settings-providers.js";
import { _applyTabOrder,_applyTabVisibility,_ensureComposerControlVisibilityState,_getHiddenTabs,_getTabOrder,_renderComposerControlChips,_renderComposerSituationalControlChips,_renderTabVisibilityChips,_setComposerControlOrder,_setHiddenTabs,_setTabOrder } from "./settings-state.js";

// Panels domain: settings preferences and hydration

export function _speechPreferencesPayloadFromUi(){
  const payload={};
  const ttsEnabledCb=$('settingsTtsEnabled');
  if(ttsEnabledCb) _setOwnedSpeechPayload(payload,'tts_enabled',ttsEnabledCb.checked);
  const ttsAutoReadCb=$('settingsTtsAutoRead');
  if(ttsAutoReadCb) _setOwnedSpeechPayload(payload,'tts_auto_read',ttsAutoReadCb.checked);
  const ttsEngineSel=$('settingsTtsEngine');
  if(ttsEngineSel) _setOwnedSpeechPayload(payload,'tts_engine',ttsEngineSel.value||'browser');
  const ttsVoiceSel=$('settingsTtsVoice');
  if(ttsVoiceSel) _setOwnedSpeechPayload(payload,'tts_voice',ttsVoiceSel.value||'');
  const ttsRateSlider=$('settingsTtsRate');
  if(ttsRateSlider) _setOwnedSpeechPayload(payload,'tts_rate',parseFloat(ttsRateSlider.value));
  const ttsPitchSlider=$('settingsTtsPitch');
  if(ttsPitchSlider) _setOwnedSpeechPayload(payload,'tts_pitch',parseFloat(ttsPitchSlider.value));
  const voiceModeCb=$('settingsVoiceModeEnabled');
  if(voiceModeCb) _setOwnedSpeechPayload(payload,'voice_mode_button',voiceModeCb.checked);
  const rawAudioCb=$('settingsRawAudio');
  _setOwnedSpeechPayload(payload,'raw_audio_mode',rawAudioCb?rawAudioCb.checked:localStorage.getItem('hermes-raw-audio-mode')==='true');
  _setOwnedSpeechPayload(payload,'voice_continuous',localStorage.getItem('hermes-voice-continuous')==='true');
  const voiceSilence=parseInt(localStorage.getItem('hermes-voice-silence-ms'),10);
  _setOwnedSpeechPayload(payload,'voice_silence_ms',(Number.isFinite(voiceSilence)&&voiceSilence>=200)?voiceSilence:1800);
  return payload;
}

export function _setPreferencesAutosaveStatus(state){
  const el=$('settingsPreferencesAutosaveStatus');
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
    el.innerHTML=`<span>${esc(t('settings_autosave_failed'))}</span> <button type=\"button\" onclick=\"_retryPreferencesAutosave()\">${esc(t('settings_autosave_retry'))}</button>`;
  }
}

export function _rememberPreferencesSaved(payload){
  if(!payload) return;
  if(payload.send_key!==undefined) localStorage.setItem('hermes-pref-send_key',payload.send_key);
  if(payload.language!==undefined) localStorage.setItem('hermes-pref-language',payload.language);
}

export function _applyWorkspaceTodosTabVisibility(){
  const tab=$('workspaceTodosTab');
  if(tab) tab.hidden=!window._workspaceTodosTab;
  const rp=document.querySelector('.rightpanel');
  if(!window._workspaceTodosTab && rp && rp.dataset.activeTab==='todos'){
    if(typeof switchWorkspacePanelTab==='function') switchWorkspacePanelTab('files');
  }
}

export function _schedulePreferencesAutosave(){
  const payload=_preferencesPayloadFromUi();
  _rememberPreferencesSaved(payload);
  state._settingsPreferencesAutosaveRetryPayload=payload;
  _setPreferencesAutosaveStatus('saving');
  if(state._settingsPreferencesAutosaveTimer) clearTimeout(state._settingsPreferencesAutosaveTimer);
  state._settingsPreferencesAutosaveTimer=setTimeout(()=>_autosavePreferencesSettings(payload),350);
}

export async function _autosavePreferencesSettings(payload){
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    if(payload&&payload.terminal_auto_expand_on_output!==undefined){
      window._terminalAutoExpandOnOutput=!!(saved&&saved.terminal_auto_expand_on_output);
    }
    if(payload&&payload.workspace_todos_tab!==undefined){
      window._workspaceTodosTab=!!(saved&&saved.workspace_todos_tab);
      if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
    }
    if(payload&&Object.prototype.hasOwnProperty.call(payload,'fade_text_effect')) window._fadeTextEffect=!!payload.fade_text_effect;
    if(saved&&Object.prototype.hasOwnProperty.call(saved,'pinned_sessions_limit')) window._pinnedSessionsLimit=parseInt(saved.pinned_sessions_limit,10)||3;
    if(payload&&payload.show_tps!==undefined){
      window._showTps=!!(saved&&saved.show_tps);
      if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
      if(typeof renderMessages==='function') renderMessages();
    }
    if(payload&&payload.hide_empty_state_suggestions!==undefined){
      window._hideEmptyStateSuggestions=!!(saved&&saved.hide_empty_state_suggestions);
      if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
    }
    if(payload&&payload.show_conversation_outline!==undefined){
      window._showConversationOutline=!!(saved&&saved.show_conversation_outline);
      document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
      if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    }
    if(payload&&payload.default_message_mode!==undefined){
      // #5170 mirror write on autosave, under the #5145 rename: persist the
      // saved mode so a reload/offline first-send honors it (legacy fallback).
      const _dmm=(saved&&saved.default_message_mode)||(saved&&saved.busy_input_mode);
      window._defaultMessageMode=(typeof _persistDefaultMessageMode==='function')?_persistDefaultMessageMode(_dmm):(_dmm||'steer');
      if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
    }
    if(payload&&payload.show_busy_placeholder_hint!==undefined){
      window._showBusyPlaceholderHint=!!(saved&&saved.show_busy_placeholder_hint);
      if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
    }
    if(payload&&payload.new_chat_on_workspace_switch!==undefined){
      window._newChatOnWorkspaceSwitch=!!(saved&&saved.new_chat_on_workspace_switch);  // #5473
    }
    state._settingsPreferencesAutosaveRetryPayload=null;
    _setPreferencesAutosaveStatus('saved');
    // Only clear the global dirty flag and hide the unsaved-changes bar when
    // there is no pending edit on a manually-saved field. Password and model
    // are still committed via the explicit "Save Settings" button (password
    // for security; model goes through /api/default-model). Without this
    // guard, autosaving a checkbox right after a user typed in the password
    // field would silently dismiss the password edit. (Opus pre-release
    // review of v0.50.250, SHOULD-FIX Q1.)
    const pwField=$('settingsPassword');
    const pwDirty=!!(pwField&&pwField.value);
    const modelSel=$('settingsModel');
    const modelState=(typeof _captureModelDropdownSelection==='function'&&modelSel)
      ? (_captureModelDropdownSelection(modelSel)||{model:String((modelSel&&modelSel.value)||''),model_provider:null})
      : {model:String((modelSel&&modelSel.value)||''),model_provider:null};
    const modelDirty=!!(
      modelSel&&(
        (modelState.model||'')!==(state._settingsHermesDefaultModelOnOpen||'')||
        ((modelState.model_provider||null)!==(state._settingsHermesDefaultModelProviderOnOpen||null))
      )
    );
    if(!pwDirty&&!modelDirty){
      const maxTokensField=$('settingsMaxTokens');
      const maxTokensDirty=!!(
        maxTokensField&&
        String(maxTokensField.value||'')!==String(maxTokensField.dataset.initialValue||'')
      );
      if(!maxTokensDirty){
        state._settingsDirty=false;
        const bar=$('settingsUnsavedBar');
        if(bar) bar.style.display='none';
      }
    }
  }catch(e){
    console.warn('[settings] preferences autosave failed', e);
    _setPreferencesAutosaveStatus('failed');
  }
}

export function _retryPreferencesAutosave(){
  const payload=state._settingsPreferencesAutosaveRetryPayload||_preferencesPayloadFromUi();
  _setPreferencesAutosaveStatus('saving');
  _autosavePreferencesSettings(payload);
}

export function _syncSettingsMaxTokensPlaceholder(field, fallbackValue){
  if(!field) return;
  const parsedFallback=parseInt(fallbackValue,10);
  if(Number.isFinite(parsedFallback)&&parsedFallback>0&&typeof t==='function'){
    field.placeholder=t('settings_placeholder_max_tokens_fallback', parsedFallback);
    return;
  }
  field.placeholder=(typeof t==='function')
    ? t('settings_placeholder_max_tokens_none')
    : 'No override';
}
export function _loadSettingsAppearance(settings){
  checkWebUIVersionSkew(settings);
  // Populate the version badges from the server — keeps them in sync with git
  // tags automatically without any manual release step.
  //
  // The DISPLAY badge uses update_channel_version (a channel-scoped
  // `git describe --match`), which is SEPARATE from settings.webui_version.
  // webui_version is load-bearing for asset cache-busting / SW cache / stale-
  // client skew detection and must stay channel-neutral — never render it as
  // the channel badge. See api/updates.channel_version_badge().
  const webuiBadge = $('settings-webui-version-badge');
  if(webuiBadge){
    const chanVer = settings.update_channel_version || settings.webui_version || 'not detected';
    const chan = settings.update_channel==='experimental' ? 'experimental' : 'stable';
    // Only annotate the channel when on experimental — stable is the implicit
    // default and needs no extra chrome.
    webuiBadge.textContent = chan==='experimental'
      ? `WebUI: ${chanVer} · Experimental`
      : `WebUI: ${chanVer}`;
  }
  const agentBadge = $('settings-agent-version-badge');
  if(agentBadge){
    const agentVersion = (settings.agent_version || 'not detected').toString().trim() || 'not detected';
    agentBadge.textContent = `Agent: ${agentVersion}`;
  }
  // Hydrate appearance controls first so a slow /api/models request
  // cannot overwrite an in-progress theme/skin selection.
  const themeSel=$('settingsTheme');
  const themeVal=settings.theme||'dark';
  if(themeSel) themeSel.value=themeVal;
  if(typeof _syncThemePicker==='function') _syncThemePicker(themeVal);
  const skinVal=(localStorage.getItem('hermes-skin')||settings.skin||'default').toLowerCase();
  const skinSel=$('settingsSkin');
  if(skinSel) skinSel.value=skinVal;
  if(typeof _buildSkinPicker==='function') _buildSkinPicker(skinVal);
  const fontSizeVal=settings.font_size||localStorage.getItem('hermes-font-size')||'default';
  localStorage.setItem('hermes-font-size',fontSizeVal);
  if(typeof _applyFontSize==='function') _applyFontSize(fontSizeVal);
  const fontSizeSel=$('settingsFontSize');
  if(fontSizeSel) fontSizeSel.value=fontSizeVal;
  if(typeof _syncFontSizePicker==='function') _syncFontSizePicker(fontSizeVal);
  const jumpButtonsCb=$('settingsSessionJumpButtons');
  if(jumpButtonsCb){
    jumpButtonsCb.checked=!!settings.session_jump_buttons;
    window._sessionJumpButtonsEnabled=jumpButtonsCb.checked;
    jumpButtonsCb.onchange=function(){
      window._sessionJumpButtonsEnabled=this.checked;
      if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
      _scheduleAppearanceAutosave();
    };
  }
  if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
  // Workspace panel default-open toggle (localStorage-backed)
  // Uses a separate key (hermes-webui-workspace-panel-pref) so that
  // closing the panel via toolbar X does not clear the user's preference.
  const wsPanelCb=$('settingsWorkspacePanelOpen');
  if(wsPanelCb){
    wsPanelCb.checked=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open';
    wsPanelCb.onchange=function(){
      const open=this.checked;
      localStorage.setItem('hermes-webui-workspace-panel-pref',open?'open':'closed');
      // Also sync the runtime key so the current session reflects the change
      localStorage.setItem('hermes-webui-workspace-panel',open?'open':'closed');
      document.documentElement.dataset.workspacePanel=open?'open':'closed';
      if(open&&_workspacePanelMode==='closed') openWorkspacePanel('browse');
      else if(!open&&_workspacePanelMode!=='closed') toggleWorkspacePanel(false);
    };
  }
  const endlessScrollCb=$('settingsSessionEndlessScroll');
  if(endlessScrollCb){
    endlessScrollCb.checked=!!settings.session_endless_scroll;
    window._sessionEndlessScrollEnabled=endlessScrollCb.checked;
    endlessScrollCb.onchange=function(){
      window._sessionEndlessScrollEnabled=this.checked;
      _scheduleAppearanceAutosave();
    };
  }
  const autoScrollFollowCb=$('settingsAutoScrollFollow');
  if(autoScrollFollowCb){
    autoScrollFollowCb.checked=settings.auto_scroll_follow!==false;
    window._autoScrollFollow=autoScrollFollowCb.checked;
    autoScrollFollowCb.onchange=function(){
      window._autoScrollFollow=this.checked;
      _scheduleAppearanceAutosave();
    };
  }
  const worklogDetailsExpandedCb=$('settingsWorklogDetailsExpandedDefault');
  const chatActivityModeSel=$('settingsChatActivityDisplayMode');
  const transparentEventTimestampsCb=$('settingsTransparentEventTimestamps');
  if(chatActivityModeSel){
    _syncChatActivityDisplayModeControl(settings.chat_activity_display_mode);
    _syncTransparentEventTimestampsControl(settings.transparent_stream_event_timestamps, settings.chat_activity_display_mode);
    chatActivityModeSel.addEventListener('change',()=>{
      _pickChatActivityDisplayMode(chatActivityModeSel.value);
    },{once:false});
  }
  if(transparentEventTimestampsCb){
    transparentEventTimestampsCb.addEventListener('change',()=>{
      _pickTransparentEventTimestamps(transparentEventTimestampsCb.checked);
    },{once:false});
  }
  if(worklogDetailsExpandedCb){
    const worklogDetailsExpanded=Object.prototype.hasOwnProperty.call(settings,'worklog_details_expanded_default')
      ? settings.worklog_details_expanded_default
      : settings.activity_feed_expanded_default;
    worklogDetailsExpandedCb.checked=!!worklogDetailsExpanded;
    window._worklogDetailsExpandedByDefault=worklogDetailsExpandedCb.checked;
    worklogDetailsExpandedCb.onchange=function(){
      window._worklogDetailsExpandedByDefault=this.checked;
      if(typeof _applyWorklogDetailsExpandedDefault==='function') _applyWorklogDetailsExpandedDefault();
      _scheduleAppearanceAutosave();
    };
  }
  const renderUserMarkdownCb=$('settingsRenderUserMarkdown');
  if(renderUserMarkdownCb){
    renderUserMarkdownCb.checked=!!settings.render_user_markdown;
    window._renderUserMarkdown=renderUserMarkdownCb.checked;
    renderUserMarkdownCb.onchange=function(){
      window._renderUserMarkdown=this.checked;
      if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
      if(typeof renderMessages==='function') renderMessages();
      _scheduleAppearanceAutosave();
    };
  }
  const largeTextPasteCb=$('settingsLargeTextPasteAsAttachment');
  if(largeTextPasteCb){
    largeTextPasteCb.checked=settings.large_text_paste_as_attachment!==false;
    window._largeTextPasteAsAttachment=largeTextPasteCb.checked;
    largeTextPasteCb.onchange=function(){
      window._largeTextPasteAsAttachment=this.checked;
      _scheduleAppearanceAutosave();
    };
  }
  const pqcCb=$('settingsProjectQuickCreate');
  if(pqcCb){
    pqcCb.checked=!!(settings.project_quick_create_buttons);
    window._projectQuickCreate=pqcCb.checked;
    pqcCb.onchange=function(){
      window._projectQuickCreate=this.checked;
      // Rebuild the sidebar so the per-project + buttons appear/disappear
      // immediately, rather than only on the next render.
      try{ if(typeof renderSessionListFromCache==='function') renderSessionListFromCache(); }catch(_){}
      _scheduleAppearanceAutosave();
    };
  }
  const structuredCodeModeSel=$('settingsStructuredCodeMode');
  const structuredCodeLinesField=$('settingsStructuredCodeAutoLines');
  if(structuredCodeModeSel){
    const mode=['auto','on','off'].includes(settings.structured_code_default_view)?settings.structured_code_default_view:'auto';
    structuredCodeModeSel.value=mode;
    const lines=parseInt(settings.structured_code_auto_tree_lines,10);
    const safeLines=(Number.isFinite(lines)&&lines>=1&&lines<=1000)?lines:10;
    if(structuredCodeLinesField) structuredCodeLinesField.value=safeLines;
    _applyStructuredCodeViewSettings(mode,safeLines,false);
    _syncStructuredCodeLinesEnabled();
    structuredCodeModeSel.onchange=function(){
      const cfg=_structuredCodeViewFromUi();
      _applyStructuredCodeViewSettings(cfg.structured_code_default_view,cfg.structured_code_auto_tree_lines,true);
      _syncStructuredCodeLinesEnabled();
      _scheduleAppearanceAutosave();
    };
    if(structuredCodeLinesField){
      // Commit on change (blur / Enter / spinner) rather than every keystroke,
      // so a long transcript is not rebuilt per digit typed. Only re-render in
      // auto mode, where the threshold actually affects the default view.
      structuredCodeLinesField.addEventListener('change',function(){
        const cfg=_structuredCodeViewFromUi();
        structuredCodeLinesField.value=cfg.structured_code_auto_tree_lines;
        _applyStructuredCodeViewSettings(cfg.structured_code_default_view,cfg.structured_code_auto_tree_lines,cfg.structured_code_default_view==='auto');
        _scheduleAppearanceAutosave();
      },{once:false});
    }
  }
  const showTitlebarProfileCb=$('settingsShowTitlebarProfile');
  if(showTitlebarProfileCb){
    showTitlebarProfileCb.checked=!!settings.show_titlebar_profile;
    showTitlebarProfileCb.onchange=function(){
      window._showTitlebarProfile=this.checked;
      if(typeof _applyTitlebarProfileVisibility==='function') _applyTitlebarProfileVisibility();
      _scheduleAppearanceAutosave();
    };
  }
  _ensureComposerControlVisibilityState(settings);
  if(Array.isArray(settings.composer_control_order)){
    const composerOrder=_setComposerControlOrder(settings.composer_control_order);
    if(typeof window._applyComposerControlOrder==='function') window._applyComposerControlOrder(composerOrder);
  }
  _renderComposerControlChips();
  _renderComposerSituationalControlChips();
  if(typeof _applyComposerFooterVisibilitySettings==='function') _applyComposerFooterVisibilitySettings();
  // Tab visibility/order chips (dynamically populated from DOM)
  var hiddenTabs=[];
  if(Array.isArray(settings.hidden_tabs)){
    // Server value takes priority — even an empty array means "no tabs hidden"
    hiddenTabs=settings.hidden_tabs.filter(function(s){return typeof s==='string'&&s.trim();});
  }else{
    // Server has no hidden_tabs key — fall back to localStorage
    hiddenTabs=_getHiddenTabs();
  }
  var tabOrder=[];
  if(Array.isArray(settings.tab_order)){
    tabOrder=settings.tab_order.filter(function(s){return typeof s==='string'&&s.trim();});
  }else{
    tabOrder=_getTabOrder();
  }
  _setTabOrder(tabOrder);
  _applyTabOrder(tabOrder);
  _setHiddenTabs(hiddenTabs);
  _applyTabVisibility(hiddenTabs);
  _renderTabVisibilityChips();
  const resolvedLanguage=(typeof resolvePreferredLocale==='function')
    ? resolvePreferredLocale(settings.language, localStorage.getItem('hermes-lang'))
    : (settings.language || localStorage.getItem('hermes-lang') || 'en');
  // Keep settings modal and current page strings in sync with the resolved locale.
  if(typeof setLocale==='function'){
    setLocale(resolvedLanguage);
    if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
  }
    return resolvedLanguage;
}
export async function _loadSettingsModelControls(settings){
  // Populate model dropdown from /api/models + live model fetch (#872)
  const modelSel=$('settingsModel');
  if(modelSel){
    modelSel.innerHTML='';
    let models=null;
    try{
      models=await api('/api/models');
      for(const g of ((models||{}).groups||[])){
        const og=document.createElement('optgroup');
        og.label=g.provider;
        if(g.provider_id) og.dataset.provider=g.provider_id;
        for(const m of [...(g.models||[]),...(g.extra_models||[])]){
          const opt=document.createElement('option');
          opt.value=m.id;opt.textContent=m.label;
          if(m && (m.supports_fast_tier === true || String(m.supports_fast_tier).toLowerCase()==='true')){
            opt.dataset.fast='1';
          }else if(m && (m.supports_fast_tier === false || String(m.supports_fast_tier).toLowerCase()==='false')){
            opt.dataset.fast='0';
          }
          og.appendChild(opt);
        }
        modelSel.appendChild(og);
      }
      // Append live-fetched models for the active provider, same as the
      // chat-header dropdown does via _fetchLiveModels() (#872).
      if(models.active_provider && typeof _fetchLiveModels==='function'){
        _fetchLiveModels(models.active_provider, modelSel);
      }
    }catch(e){}
    state._settingsHermesDefaultModelOnOpen=(models&&models.default_model)||'';
    state._settingsHermesDefaultModelProviderOnOpen=(models&&models.active_provider)||null;
    // Use the smart matcher so a saved bare form like "anthropic/claude-opus-4.6"
    // (what the CLI's `hermes model` command writes) still selects the matching
    // `@nous:anthropic/claude-opus-4.6` option on a Nous setup. Without this, the
    // picker renders blank for any user whose default was persisted without the
    // @-prefix — CLI-first users, legacy installs, etc.
    if(typeof _applyModelToDropdown==='function'){
      _applyModelToDropdown(state._settingsHermesDefaultModelOnOpen, modelSel, (models&&models.active_provider)||window._activeProvider||null);
    }else{
      modelSel.value=state._settingsHermesDefaultModelOnOpen;
    }
    if(typeof closeSettingsModelDropdown==='function') closeSettingsModelDropdown();
    if(typeof mountSettingsModelPicker==='function') mountSettingsModelPicker();
    modelSel.addEventListener('change',_markSettingsDirty,{once:false});
    if(!modelSel._settingsChipSyncBound){
      modelSel._settingsChipSyncBound=true;
      modelSel.addEventListener('change',()=>{if(typeof syncSettingsModelChip==='function') syncSettingsModelChip();},{once:false});
    }
  }
  // Auxiliary models — load task assignments and provider/model options
  _bindMainAdvancedOptionsButton();
  _loadAuxiliaryModels();
}
export function _loadSettingsPreferences(settings, resolvedLanguage){
  // Send key preference
  const sendKeySel=$('settingsSendKey');
  if(sendKeySel){sendKeySel.value=settings.send_key||'enter';sendKeySel.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  // Language preference — list every supported locale without eagerly loading
  // every translation bundle onto the initial page path.
  const langSel=$('settingsLanguage');
  if(langSel){
    langSel.innerHTML='';
    if(typeof SUPPORTED_LOCALES!=='undefined'){
      for(const {code,label} of SUPPORTED_LOCALES){
        const opt=document.createElement('option');
        opt.value=code;opt.textContent=label||code;
        langSel.appendChild(opt);
      }
    }
    langSel.value=resolvedLanguage;
    langSel.addEventListener('change',function(){
      if(typeof setLocale==='function'){setLocale(this.value);if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();}
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const showUsageCb=$('settingsShowTokenUsage');
  if(showUsageCb){showUsageCb.checked=!!settings.show_token_usage;showUsageCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const maxTokensField=$('settingsMaxTokens');
  if(maxTokensField){
    const rawMaxTokens=settings.max_tokens;
    const parsedMaxTokens=parseInt(rawMaxTokens,10);
    const hasRootOverride=Number.isFinite(parsedMaxTokens)&&parsedMaxTokens>0;
    maxTokensField.value=hasRootOverride
      ? String(parsedMaxTokens)
      : '';
    _syncSettingsMaxTokensPlaceholder(maxTokensField,settings.max_tokens_fallback);
    maxTokensField.dataset.initialValue=maxTokensField.value;
    maxTokensField.addEventListener('input',_markSettingsDirty,{once:false});
  }
  // Ambient provider quota chip toggle — default off; only shows at ≥1400px viewport
  // when enabled (see style.css @media (max-width:1399.98px) rule).
  const showQuotaChipCb=$('settingsShowQuotaChip');
  if(showQuotaChipCb){
    showQuotaChipCb.checked=settings.show_quota_chip===true;
    window._showQuotaChip=showQuotaChipCb.checked;
    showQuotaChipCb.addEventListener('change',()=>{
      window._showQuotaChip=showQuotaChipCb.checked;
      if(typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const hideSuggestionsCb=$('settingsHideSuggestions');
  if(hideSuggestionsCb){
    hideSuggestionsCb.checked=settings.hide_empty_state_suggestions===true;
    window._hideEmptyStateSuggestions=hideSuggestionsCb.checked;
    if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
    hideSuggestionsCb.addEventListener('change',()=>{
      window._hideEmptyStateSuggestions=hideSuggestionsCb.checked;
      if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const virtualizeTranscriptCb=$('settingsVirtualizeTranscript');
  if(virtualizeTranscriptCb){
    // #4343: EXPERIMENTAL/opt-IN, default OFF. Honor a stored true only when
    // it came from an explicit post-flip opt-in (===true); a pre-flip true is
    // already reset to false server-side by the load_settings migration.
    virtualizeTranscriptCb.checked=settings.virtualize_transcript===true;
    window._virtualizeTranscript=virtualizeTranscriptCb.checked;
    virtualizeTranscriptCb.addEventListener('change',()=>{
      window._virtualizeTranscript=virtualizeTranscriptCb.checked;
      // Re-render the open transcript so the change takes effect immediately
      // (full render when off, windowed when on).
      if(typeof renderMessages==='function'){ try{ renderMessages({preserveScroll:true}); }catch(e){ console.warn('[virtualize_transcript] renderMessages failed on toggle:',e); } }
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const showConversationOutlineCb=$('settingsShowConversationOutline');
  if(showConversationOutlineCb){
    showConversationOutlineCb.checked=settings.show_conversation_outline===true;
    window._showConversationOutline=showConversationOutlineCb.checked;
    document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
    if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    showConversationOutlineCb.addEventListener('change',()=>{
      _schedulePreferencesAutosave();
      window._showConversationOutline=showConversationOutlineCb.checked;
      document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
      if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    },{once:false});
  }
  const showTpsCb=$('settingsShowTps');
  if(showTpsCb){showTpsCb.checked=!!settings.show_tps;showTpsCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const pinnedLimitField=$('settingsPinnedSessionsLimit');
  if(pinnedLimitField){
    pinnedLimitField.value=parseInt(settings.pinned_sessions_limit||3,10)||3;
    window._pinnedSessionsLimit=parseInt(pinnedLimitField.value,10)||3;
    pinnedLimitField.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    pinnedLimitField.addEventListener('input',()=>{window._pinnedSessionsLimit=parseInt(pinnedLimitField.value,10)||3;_schedulePreferencesAutosave();},{once:false});
  }
  const fadeTextCb=$('settingsFadeTextEffect');
  if(fadeTextCb){
    fadeTextCb.checked=!!settings.fade_text_effect;
    window._fadeTextEffect=fadeTextCb.checked;
    fadeTextCb.addEventListener('change',()=>{
      window._fadeTextEffect=fadeTextCb.checked;
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const terminalAutoExpandCb=$('settingsTerminalAutoExpand');
  if(terminalAutoExpandCb){terminalAutoExpandCb.checked=!!settings.terminal_auto_expand_on_output;window._terminalAutoExpandOnOutput=terminalAutoExpandCb.checked;terminalAutoExpandCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const workspaceTodosTabCb=$('settingsWorkspaceTodosTab');
  if(workspaceTodosTabCb){
    workspaceTodosTabCb.checked=!!settings.workspace_todos_tab;
    window._workspaceTodosTab=workspaceTodosTabCb.checked;
    _applyWorkspaceTodosTabVisibility();
    workspaceTodosTabCb.addEventListener('change',()=>{
      window._workspaceTodosTab=workspaceTodosTabCb.checked;
      _applyWorkspaceTodosTabVisibility();
      _schedulePreferencesAutosave();
    },{once:false});
  }
  const apiRedactCb=$('settingsApiRedact');
  if(apiRedactCb){apiRedactCb.checked=settings.api_redact_enabled!==false;apiRedactCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const showCliCb=$('settingsShowCliSessions');
  if(showCliCb){showCliCb.checked=settings.show_cli_sessions!==false;showCliCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const showClaudeCodeCb=$('settingsShowClaudeCodeSessions');
  if(showClaudeCodeCb){
    showClaudeCodeCb.checked=!!settings.show_claude_code_sessions;
    showClaudeCodeCb.disabled=showCliCb?!showCliCb.checked:true;
    showClaudeCodeCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  if(showCliCb){showCliCb.addEventListener('change',function(){
    const enabled=!!showCliCb.checked;
    if(showCronCb) showCronCb.disabled=!enabled;
    if(showClaudeCodeCb) showClaudeCodeCb.disabled=!enabled;
    _schedulePreferencesAutosave();
  },{once:false});}
  const showCronCb=$('settingsShowCronSessions');
  if(showCronCb){
    showCronCb.checked=!!settings.show_cron_sessions;
    showCronCb.disabled=showCliCb?!showCliCb.checked:true;
    showCronCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  const showWebhookCb=$('settingsShowWebhookSessions');
  if(showWebhookCb){
    showWebhookCb.checked=!!settings.show_webhook_sessions;
    showWebhookCb.disabled=showCliCb?!showCliCb.checked:true;
    showWebhookCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});
    if(showCliCb){showCliCb.addEventListener('change',function(){showWebhookCb.disabled=!showCliCb.checked;},{once:false});}
  }
  const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');
  if(showPreviousMessagingCb){showPreviousMessagingCb.checked=!!settings.show_previous_messaging_sessions;showPreviousMessagingCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const syncCb=$('settingsSyncInsights');
  if(syncCb){syncCb.checked=!!settings.sync_to_insights;syncCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const updateCb=$('settingsCheckUpdates');
  if(updateCb){updateCb.checked=settings.check_for_updates!==false;updateCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const updateChannelSel=$('settingsUpdateChannel');
  if(updateChannelSel){
    updateChannelSel.value=settings.update_channel==='experimental'?'experimental':'stable';
    updateChannelSel.addEventListener('change',function(){
      // Persist the channel, then invalidate the cached update check and
      // re-check so the banner reflects the newly-selected channel. Changing
      // the channel changes WHAT is offered, never WHAT is installed — the
      // update banner still gates the actual apply behind "Update Now".
      _schedulePreferencesAutosave();
      if(typeof checkUpdatesNow==='function'){
        // Pass the just-selected channel EXPLICITLY so the re-check cannot race
        // the debounced autosave PUT and answer for the previous channel.
        const _picked=updateChannelSel.value;
        setTimeout(function(){try{checkUpdatesNow(_picked);}catch(e){}},400);
      }
      if(typeof _syncUpdateChannelBadge==='function') _syncUpdateChannelBadge(updateChannelSel.value);
    },{once:false});
  }
  const ignoreAgentUpdatesCb=$('settingsIgnoreAgentUpdates');
  if(ignoreAgentUpdatesCb){ignoreAgentUpdatesCb.checked=!!settings.ignore_agent_updates;ignoreAgentUpdatesCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const whatsNewSummaryCb=$('settingsWhatsNewSummary');
  if(whatsNewSummaryCb){whatsNewSummaryCb.checked=!!settings.whats_new_summary_enabled;whatsNewSummaryCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  const soundCb=$('settingsSoundEnabled');
  if(soundCb){soundCb.checked=!!settings.sound_enabled;soundCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
}
export function _loadSettingsSpeechAndRuntime(settings){
  // Right-to-left chat layout (#1721 salvage) — Settings-only, no composer button.
  const rtlCb=$('settingsRtl');
  if(rtlCb){
    const saved=!!settings.rtl || localStorage.getItem('hermes-rtl')==='true';
    rtlCb.checked=saved;
    try{localStorage.setItem('hermes-rtl',saved?'true':'false');}catch(_){}
    document.documentElement.classList.toggle('chat-content-rtl',saved);
    rtlCb.addEventListener('change',()=>{
      const on=rtlCb.checked;
      try{localStorage.setItem('hermes-rtl',on?'true':'false');}catch(_){}
      document.documentElement.classList.toggle('chat-content-rtl',on);
      _schedulePreferencesAutosave();
    },{once:false});
  }
  if(typeof window._mirrorSpeechSettingsFromServer==='function') window._mirrorSpeechSettingsFromServer(settings);
  const persistedSpeechKeys = new Set(
    Array.isArray(settings && settings.persisted_speech_keys)
      ? settings.persisted_speech_keys
      : []
  );
  _captureSpeechPreferenceOwnership(settings);
  const _speechSetting=function(key,storageKey,fallback,kind){
    const stored=localStorage.getItem(storageKey);
    if(settings&&persistedSpeechKeys.has(key)) return settings[key];
    return stored===null?fallback:stored;
  };
  const _speechBool=function(key,storageKey,fallback){
    const value=_speechSetting(key,storageKey,fallback,'bool');
    return value===true||value==='true';
  };
  const rawAudioCb=$('settingsRawAudio');
  if(rawAudioCb){
    rawAudioCb.checked=_speechBool('raw_audio_mode','hermes-raw-audio-mode',false);
    rawAudioCb.onchange=function(){
      _markSpeechPreferenceChanged('raw_audio_mode');
      if(typeof window._applyRawAudioModePreference==='function') window._applyRawAudioModePreference(this.checked);
      else localStorage.setItem('hermes-raw-audio-mode',this.checked?'true':'false');
      _schedulePreferencesAutosave();
    };
  }
  const voiceContinuous=_speechBool('voice_continuous','hermes-voice-continuous',false);
  _syncSpeechPreferenceCache('voice_continuous',voiceContinuous?'true':'false');
  const voiceSilence=parseInt(_speechSetting('voice_silence_ms','hermes-voice-silence-ms',1800),10);
  _syncSpeechPreferenceCache('voice_silence_ms',Number.isFinite(voiceSilence)&&voiceSilence>=200?String(voiceSilence):'1800');
  // TTS settings use /api/settings as the durable source and localStorage as the runtime cache.
  const ttsEnabledCb=$('settingsTtsEnabled');
  if(ttsEnabledCb){ttsEnabledCb.checked=_speechBool('tts_enabled','hermes-tts-enabled',false);ttsEnabledCb.onchange=function(){_markSpeechPreferenceChanged('tts_enabled');localStorage.setItem('hermes-tts-enabled',this.checked?'true':'false');_applyTtsEnabled(this.checked);_schedulePreferencesAutosave();};}
  const ttsAutoReadCb=$('settingsTtsAutoRead');
  if(ttsAutoReadCb){ttsAutoReadCb.checked=_speechBool('tts_auto_read','hermes-tts-auto-read',false);ttsAutoReadCb.onchange=function(){_markSpeechPreferenceChanged('tts_auto_read');localStorage.setItem('hermes-tts-auto-read',this.checked?'true':'false');_schedulePreferencesAutosave();};}
  // Voice-mode button visibility (#1488).
  // Toggling re-applies immediately via the boot.js helper so the user sees
  // the audio-waveform button appear/disappear without a reload.
  // Also recomputes composer footer visibility so the .composer-divider
  // (which tracks whether all left-group buttons are hidden, see #5451)
  // stays in sync when #btnVoiceMode appears or disappears here.
  const voiceModeCb=$('settingsVoiceModeEnabled');
  if(voiceModeCb){
    voiceModeCb.checked=_speechBool('voice_mode_button','hermes-voice-mode-button',false);
    voiceModeCb.onchange=function(){
      _markSpeechPreferenceChanged('voice_mode_button');
      localStorage.setItem('hermes-voice-mode-button',this.checked?'true':'false');
      if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
      if(typeof window._applyComposerFooterVisibilitySettings==='function') window._applyComposerFooterVisibilitySettings();
      _schedulePreferencesAutosave();
    };
  }
  // TTS engine selector
  const ttsEngineSel=$('settingsTtsEngine');
  if(ttsEngineSel){
    // Re-add any extension-registered TTS engines (window.registerHermesTtsEngine)
    // as options — the <select> markup only hardcodes the built-ins, and this
    // settings panel can render after an extension registered its engine.
    if(typeof window._hermesTtsEngineOptions==='function'){
      window._hermesTtsEngineOptions().forEach(function(e){
        if(!ttsEngineSel.querySelector('option[value="'+e.id+'"]')){
          var opt=document.createElement('option');
          opt.value=e.id; opt.textContent=e.label;
          ttsEngineSel.appendChild(opt);
        }
      });
    }
    const saved=String(_speechSetting('tts_engine','hermes-tts-engine','browser')||'browser');
    if(!ttsEngineSel.querySelector('option[value="'+saved+'"]')){
      var savedOpt=document.createElement('option');
      savedOpt.value=saved; savedOpt.textContent=saved;
      ttsEngineSel.appendChild(savedOpt);
    }
    ttsEngineSel.value=saved;
    _syncSpeechPreferenceCache('tts_engine',saved);
    ttsEngineSel.onchange=function(){
      _markSpeechPreferenceChanged('tts_engine');
      localStorage.setItem('hermes-tts-engine',this.value);
      populateTtsVoices();
      _schedulePreferencesAutosave();
    };
  }
  // Populate voice selector based on engine
  const ttsVoiceSel=$('settingsTtsVoice');
  const populateTtsVoices=()=>{
    if(!ttsVoiceSel) return;
    const engine=localStorage.getItem('hermes-tts-engine')||'browser';
    const current=String(_speechSetting('tts_voice','hermes-tts-voice','')||'');
    _syncSpeechPreferenceCache('tts_voice',current);
    if(engine==='elevenlabs'){
      ttsVoiceSel.innerHTML='<option value="">Hermy — ElevenLabs (server-configured)</option>';
    } else if(engine==='openai'){
      ttsVoiceSel.innerHTML='<option value="">OpenAI voice (server-configured)</option>';
    } else if(engine==='edge'){
      const edgeVoices=[
        {value:'zh-CN-XiaoxiaoNeural',label:'Xiaoxiao (Chinese, Female)'},
        {value:'zh-CN-XiaoyiNeural',label:'Xiaoyi (Chinese, Female)'},
        {value:'zh-CN-YunxiNeural',label:'Yunxi (Chinese, Male)'},
        {value:'zh-CN-YunjianNeural',label:'Yunjian (Chinese, Male)'},
        {value:'zh-CN-YunyangNeural',label:'Yunyang (Chinese, Male)'},
        {value:'en-US-AriaNeural',label:'Aria (English, Female)'},
        {value:'en-US-GuyNeural',label:'Guy (English, Male)'},
        {value:'id-ID-GadisNeural',label:'Gadis (Indonesian, Female)'},
      ];
      ttsVoiceSel.innerHTML='<option value="">Default (Xiaoxiao)</option>';
      edgeVoices.forEach(v=>{
        const opt=document.createElement('option');
        opt.value=v.value;opt.textContent=v.label;
        if(v.value===current) opt.selected=true;
        ttsVoiceSel.appendChild(opt);
      });
    } else {
      if(!('speechSynthesis' in window)){
        ttsVoiceSel.innerHTML='<option value="">Speech synthesis not available</option>';
        return;
      }
      const voices=speechSynthesis.getVoices();
      ttsVoiceSel.innerHTML='<option value="">Default system voice</option>';
      voices.forEach(v=>{
        const opt=document.createElement('option');
        opt.value=v.name;opt.textContent=v.name+(v.lang?' ('+v.lang+')':'');
        if(v.name===current) opt.selected=true;
        ttsVoiceSel.appendChild(opt);
      });
    }
  };
  if(ttsVoiceSel&&'speechSynthesis' in window){
    populateTtsVoices();
    speechSynthesis.addEventListener('voiceschanged',function(){
      const engine=localStorage.getItem('hermes-tts-engine')||'browser';
      if(engine==='browser') populateTtsVoices();
    },{once:false});
    ttsVoiceSel.onchange=function(){_markSpeechPreferenceChanged('tts_voice');localStorage.setItem('hermes-tts-voice',this.value);_schedulePreferencesAutosave();};
  }
  // TTS rate/pitch sliders
  const ttsRateSlider=$('settingsTtsRate');
  const ttsRateValue=$('settingsTtsRateValue');
  if(ttsRateSlider){
    const savedRate=_speechSetting('tts_rate','hermes-tts-rate',1);
    ttsRateSlider.value=(savedRate===null||savedRate===undefined)?'1':String(savedRate);
    if(ttsRateValue) ttsRateValue.textContent=parseFloat(ttsRateSlider.value).toFixed(1)+'x';
    _syncSpeechPreferenceCache('tts_rate',ttsRateSlider.value);
    ttsRateSlider.oninput=function(){_markSpeechPreferenceChanged('tts_rate');if(ttsRateValue)ttsRateValue.textContent=parseFloat(this.value).toFixed(1)+'x';localStorage.setItem('hermes-tts-rate',this.value);_schedulePreferencesAutosave();};
  }
  const ttsPitchSlider=$('settingsTtsPitch');
  const ttsPitchValue=$('settingsTtsPitchValue');
  if(ttsPitchSlider){
    const savedPitch=_speechSetting('tts_pitch','hermes-tts-pitch',1);
    ttsPitchSlider.value=(savedPitch===null||savedPitch===undefined)?'1':String(savedPitch);
    if(ttsPitchValue) ttsPitchValue.textContent=parseFloat(ttsPitchSlider.value).toFixed(1);
    _syncSpeechPreferenceCache('tts_pitch',ttsPitchSlider.value);
    ttsPitchSlider.oninput=function(){_markSpeechPreferenceChanged('tts_pitch');if(ttsPitchValue)ttsPitchValue.textContent=parseFloat(this.value).toFixed(1);localStorage.setItem('hermes-tts-pitch',this.value);_schedulePreferencesAutosave();};
  }
  const notifCb=$('settingsNotificationsEnabled');
  if(notifCb){notifCb.checked=!!settings.notifications_enabled;notifCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});}
  // show_thinking has no settings panel checkbox — controlled via /reasoning show|hide
  const sidebarDensitySel=$('settingsSidebarDensity');
  if(sidebarDensitySel){
    sidebarDensitySel.value=settings.sidebar_density==='detailed'?'detailed':'compact';
    sidebarDensitySel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  const autoTitleRefreshSel=$('settingsAutoTitleRefresh');
  if(autoTitleRefreshSel){
    const val=String(settings.auto_title_refresh_every||'0');
    autoTitleRefreshSel.value=['0','5','10','20'].includes(val)?val:'0';
    autoTitleRefreshSel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  // Default message mode
  const defaultMessageModeSel=$('settingsDefaultMessageMode');
  if(defaultMessageModeSel){
    const val=String(settings.default_message_mode||settings.busy_input_mode||'steer');
    defaultMessageModeSel.value=['queue','interrupt','steer'].includes(val)?val:'steer';
    // #5170 mirror write on panel load, under the #5145 rename.
    window._defaultMessageMode=(typeof _persistDefaultMessageMode==='function')?_persistDefaultMessageMode(defaultMessageModeSel.value):defaultMessageModeSel.value;
    defaultMessageModeSel.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  const showBusyPlaceholderHintCb=$('settingsShowBusyPlaceholderHint');
  if(showBusyPlaceholderHintCb){
    showBusyPlaceholderHintCb.checked=!!settings.show_busy_placeholder_hint;
    window._showBusyPlaceholderHint=showBusyPlaceholderHintCb.checked;
    showBusyPlaceholderHintCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
  const newChatOnWorkspaceSwitchCb=$('settingsNewChatOnWorkspaceSwitch');
  if(newChatOnWorkspaceSwitchCb){
    newChatOnWorkspaceSwitchCb.checked=!!settings.new_chat_on_workspace_switch;
    window._newChatOnWorkspaceSwitch=newChatOnWorkspaceSwitchCb.checked;
    newChatOnWorkspaceSwitchCb.addEventListener('change',_schedulePreferencesAutosave,{once:false});
  }
  // Bot name — debounced autosave (text input)
  const botNameField=$('settingsBotName');
  if(botNameField){
    botNameField.value=settings.bot_name||'Hermes';
    let botNameTimer=null;
    botNameField.addEventListener('input',()=>{
      if(botNameTimer) clearTimeout(botNameTimer);
      botNameTimer=setTimeout(_schedulePreferencesAutosave,500);
    },{once:false});
  }
}
export async function _loadSettingsAuthentication(settings){
  // Password field: always blank (we don't send hash back)
  const pwField=$('settingsPassword');
  if(pwField){pwField.value='';pwField.addEventListener('input',_markSettingsDirty,{once:false});}
  // #1560: when HERMES_WEBUI_PASSWORD env var is set, the settings password
  // field silently no-ops. Disable it + reveal the lock banner so the UI
  // tells the truth before a user tries (and the backend now also returns
  // 409 as defense-in-depth).
  const pwEnvLocked=!!settings.password_env_var;
  state._settingsPasswordEnvLocked=pwEnvLocked;
  const pwLockBanner=$('settingsPasswordEnvLock');
  if(pwField){
    pwField.disabled=pwEnvLocked;
    if(pwEnvLocked){
      pwField.value='';
      pwField.placeholder=t('password_env_var_locked_placeholder')||pwField.placeholder;
    }
  }
  if(pwLockBanner) pwLockBanner.style.display=pwEnvLocked?'block':'none';
  // Show auth buttons only when auth is active
  try{
    const authStatus=await api('/api/auth/status');
    state._settingsPasswordAuthEnabled=!!authStatus.password_auth_enabled;
    _setSettingsAuthButtonsVisible(!!authStatus.auth_enabled);
    _syncPasswordlessButton(authStatus);
    _renderSettingsAuthStatus(authStatus);
    _updateCurrentPasswordVisibility();
    _updateAuthWarningBadge(authStatus);
    _updateAuthDisabledWarning(authStatus);
  }catch(e){}
  loadPasskeys();
  // #1560: env-var-locked password also disables the Disable Auth button —
  // clearing settings.password_hash is silent no-op when the env var is set,
  // and the backend now returns 409 anyway, so don't offer the action.
  // Sign Out remains available since it only clears the session cookie.
  if(pwEnvLocked){
    const disableBtn=$('btnDisableAuth');
    if(disableBtn) disableBtn.style.display='none';
  }
  _syncHermesPanelSessionActions();
  if(typeof loadDashboardSettings==='function') loadDashboardSettings();
  loadProvidersPanel(); // load provider cards in background
  loadPluginsPanel(); // load plugin/hook visibility in background
  loadExtensionsPanel(); // load extension diagnostics in background
  switchSettingsSection(state._settingsSection);
}

export async function loadSettingsPanel(){
  try{
    const settings=await api('/api/settings');
    const resolvedLanguage=_loadSettingsAppearance(settings);
    await _loadSettingsModelControls(settings);
    _loadSettingsPreferences(settings,resolvedLanguage);
    _loadSettingsSpeechAndRuntime(settings);
    await _loadSettingsAuthentication(settings);
  }catch(e){
    showToast(t('settings_load_failed')+e.message);
  }
}
