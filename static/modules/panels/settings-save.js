import { state } from "./state.js";

import { _applySavedSettingsUi,_renderSettingsAuthStatus,_setSettingsAuthButtonsVisible,_syncPasswordlessButton,_updateAuthDisabledWarning,_updateAuthWarningBadge,_updateCurrentPasswordVisibility,loadPasskeys } from "./settings-models-auth.js";
import { _hideSettingsPanel,_resetSettingsPanelState,_structuredCodeViewFromUi } from "./settings-navigation.js";
import { _speechPreferencesPayloadFromUi } from "./settings-preferences.js";
import { _composerControlVisibilityPayload,_getComposerControlOrder } from "./settings-state.js";

// Panels domain: settings persistence and cron alerts

export async function saveSettings(andClose){
  const model=($('settingsModel')||{}).value;
  const modelState=(typeof _captureModelDropdownSelection==='function'&&$('settingsModel'))
    ? (_captureModelDropdownSelection($('settingsModel'))||{model:String(model||''),model_provider:null})
    : {model:String(model||''),model_provider:null};
  const modelChanged=(model||'')!==(state._settingsHermesDefaultModelOnOpen||'')||((modelState.model_provider||null)!==(state._settingsHermesDefaultModelProviderOnOpen||null));
  const sendKey=($('settingsSendKey')||{}).value;
  const showTokenUsage=!!($('settingsShowTokenUsage')||{}).checked;
  const showQuotaChip=!!($('settingsShowQuotaChip')||{}).checked;
  const showConversationOutline=!!($('settingsShowConversationOutline')||{}).checked;
  const showTps=!!($('settingsShowTps')||{}).checked;
  const fadeTextEffect=!!($('settingsFadeTextEffect')||{}).checked;
  const showCliSessions=!!($('settingsShowCliSessions')||{}).checked;
  const showClaudeCodeSessions=!!($('settingsShowClaudeCodeSessions')||{}).checked;
  const showCronSessions=!!($('settingsShowCronSessions')||{}).checked;
  const showWebhookSessions=!!($('settingsShowWebhookSessions')||{}).checked;
  const showPreviousMessagingSessions=!!($('settingsShowPreviousMessagingSessions')||{}).checked;
  const pinnedSessionsLimit=parseInt(($('settingsPinnedSessionsLimit')||{}).value,10)||3;
  const pw=($('settingsPassword')||{}).value;
  const theme=($('settingsTheme')||{}).value||'dark';
  const skin=($('settingsSkin')||{}).value||'default';
  const fontSize=($('settingsFontSize')||{}).value||localStorage.getItem('hermes-font-size')||'default';
  const language=($('settingsLanguage')||{}).value||'en';
  const sidebarDensity=($('settingsSidebarDensity')||{}).value==='detailed'?'detailed':'compact';
  const defaultMessageMode=($('settingsDefaultMessageMode')||{}).value||'steer';
  const showBusyPlaceholderHint=!!($('settingsShowBusyPlaceholderHint')||{}).checked;
  const body={};
  Object.assign(body,_speechPreferencesPayloadFromUi());

  if(sendKey) body.send_key=sendKey;
  body.theme=theme;
  body.skin=skin;
  body.font_size=fontSize;
  body.session_jump_buttons=!!($('settingsSessionJumpButtons')||{}).checked;
  body.session_endless_scroll=!!($('settingsSessionEndlessScroll')||{}).checked;
  body.chat_activity_display_mode=((($('settingsChatActivityDisplayMode')||{}).value==='transparent_stream')
    ||(($('settingsChatActivityDisplayMode')||{}).value==='hide_all_activity'))
    ? ($('settingsChatActivityDisplayMode')||{}).value
    : 'compact_worklog';
  body.transparent_stream_event_timestamps=(($('settingsTransparentEventTimestamps')||{}).checked)!==false;
  body.auto_scroll_follow=!!($('settingsAutoScrollFollow')||{}).checked;
  body.render_user_markdown=!!($('settingsRenderUserMarkdown')||{}).checked;
  body.large_text_paste_as_attachment=!!($('settingsLargeTextPasteAsAttachment')||{}).checked;
  body.project_quick_create_buttons=!!($('settingsProjectQuickCreate')||{}).checked;
  Object.assign(body,_structuredCodeViewFromUi());
  Object.assign(body,_composerControlVisibilityPayload());
  body.composer_control_order=_getComposerControlOrder();
  body.language=language;
  body.show_token_usage=showTokenUsage;
  const maxTokensField=$('settingsMaxTokens');
  if(maxTokensField){
    const maxTokensRaw=String(maxTokensField.value||'').trim();
    const initialMaxTokens=String(maxTokensField.dataset.initialValue||'').trim();
    if(maxTokensRaw!==initialMaxTokens){
      body.max_tokens=maxTokensRaw===''?null:maxTokensRaw;
    }
  }
  body.show_quota_chip=showQuotaChip===true;
  body.show_conversation_outline=showConversationOutline===true;
  body.show_busy_placeholder_hint=showBusyPlaceholderHint===true;
  body.show_tps=showTps;
  body.fade_text_effect=fadeTextEffect;
  body.terminal_auto_expand_on_output=!!($('settingsTerminalAutoExpand')||{}).checked;
  body.workspace_todos_tab=!!window._workspaceTodosTab;
  body.api_redact_enabled=!!($('settingsApiRedact')||{}).checked;
  body.show_cli_sessions=showCliSessions;
  // Persist the opt-out child independently; the read path applies the parent gate.
  body.show_claude_code_sessions=showClaudeCodeSessions;
  // Cron and webhook sessions are gated on CLI sessions (server short-circuits otherwise);
  // mirror the autosave path so the explicit Save Settings button persists them too. (#3514)
  body.show_cron_sessions=showCliSessions&&showCronSessions;
  body.show_webhook_sessions=showCliSessions&&showWebhookSessions;
  body.show_previous_messaging_sessions=showPreviousMessagingSessions;
  body.pinned_sessions_limit=pinnedSessionsLimit;
  body.sync_to_insights=!!($('settingsSyncInsights')||{}).checked;
  body.check_for_updates=!!($('settingsCheckUpdates')||{}).checked;
  body.update_channel=($('settingsUpdateChannel')||{}).value==='experimental'?'experimental':'stable';
  body.ignore_agent_updates=!!($('settingsIgnoreAgentUpdates')||{}).checked;
  body.whats_new_summary_enabled=!!($('settingsWhatsNewSummary')||{}).checked;
  body.sound_enabled=!!($('settingsSoundEnabled')||{}).checked;
  body.rtl=!!($('settingsRtl')||{}).checked;
  body.notifications_enabled=!!($('settingsNotificationsEnabled')||{}).checked;
  body.show_thinking=window._showThinking!==false;
  body.sidebar_density=sidebarDensity;
  body.default_message_mode=defaultMessageMode;
  body.auto_title_refresh_every=(($('settingsAutoTitleRefresh')||{}).value||'0');
  const botName=(($('settingsBotName')||{}).value||'').trim();
  body.bot_name=botName||'Hermes';
  // Password: only act if the field has content; blank = leave auth unchanged
  if(pw && pw.trim()){
    const currentPwField=$('settingsCurrentPassword');
    const currentPw=(currentPwField||{}).value||'';
    if(state._settingsPasswordAuthEnabled && !currentPw.trim()){
      if(currentPwField) currentPwField.focus();
      showToast(t('current_password_required'));
      return;
    }
    const payload={...body,_set_password:pw.trim()};
    if(state._settingsPasswordAuthEnabled) payload._current_password=currentPw;
    try{
      const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
      if(modelChanged && model){
        try{
          await api('/api/default-model',{method:'POST',body:JSON.stringify({model,provider:modelState.model_provider||null})});
          body.default_model=model;
          body.default_model_provider=(modelState&&modelState.model===model)?(modelState.model_provider||null):null;
        }catch(_modelErr){
          if(typeof showToast==='function') showToast('Failed to update default model — settings saved');
        }
      }
      _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showQuotaChip,showConversationOutline,showBusyPlaceholderHint,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize});
      showToast(t(saved.auth_just_enabled?'settings_saved_pw':'settings_saved_pw_updated'));
      const cpField=$('settingsCurrentPassword'); if(cpField) cpField.value='';
      const pwField=$('settingsPassword'); if(pwField) pwField.value='';
      state._settingsPasswordAuthEnabled=!!saved.password_auth_enabled;
      _updateCurrentPasswordVisibility();
      try{
        const authStatus=await api('/api/auth/status');
        _renderSettingsAuthStatus(authStatus);
        _updateAuthWarningBadge(authStatus);
        _updateAuthDisabledWarning(authStatus);
      }catch(e){}
      state._settingsDirty=false;
      _resetSettingsPanelState();
      if(!andClose) state._pendingSettingsTargetPanel = null;
      if(andClose) _hideSettingsPanel();
      return;
    }catch(e){showToast(t('settings_save_failed')+e.message);return;}
  }
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
    if(modelChanged && model){
      try{
        await api('/api/default-model',{method:'POST',body:JSON.stringify({model,provider:modelState.model_provider||null})});
        body.default_model=model;
        body.default_model_provider=(modelState&&modelState.model===model)?(modelState.model_provider||null):null;
      }catch(_modelErr){
        if(typeof showToast==='function') showToast('Failed to update default model — settings saved');
      }
    }
    _applySavedSettingsUi(saved, body, {sendKey,showTokenUsage,showQuotaChip,showConversationOutline,showBusyPlaceholderHint,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize});
    showToast(t('settings_saved'));
    state._settingsDirty=false;
    _resetSettingsPanelState();
    if(!andClose) state._pendingSettingsTargetPanel = null;
    if(andClose) _hideSettingsPanel();
  }catch(e){
    showToast(t('settings_save_failed')+e.message);
  }
}

export async function signOut(){
  try{
    const response=await api('/api/auth/logout',{method:'POST',body:'{}'});
    window.location.href=response.trusted_logout_url||'login';
  }catch(e){
    showToast(t('sign_out_failed')+e.message);
  }
}

export async function goPasswordless(){
  const ok=await showConfirmDialog({title:'Go passwordless?',message:'This removes the password and keeps passkey sign-in enabled. Keep at least one passkey registered or you could lose access.',confirmLabel:'Go passwordless',danger:false,focusCancel:true});
  if(!ok) return;
  const currentPw=($('settingsCurrentPassword')||{}).value;
  const payload={_passwordless:true};
  if(state._settingsPasswordAuthEnabled && currentPw) payload._current_password=currentPw;
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    showToast('Password removed. Passkey sign-in remains enabled.');
    _setSettingsAuthButtonsVisible(!!saved.auth_enabled);
    _syncPasswordlessButton({auth_enabled:saved.auth_enabled,password_auth_enabled:false,passkeys_count:1});
    const pwField=$('settingsPassword'); if(pwField) pwField.value='';
    const cpField=$('settingsCurrentPassword'); if(cpField) cpField.value='';
    state._settingsPasswordAuthEnabled=false;
    _updateCurrentPasswordVisibility();
    try{
      const authStatus=await api('/api/auth/status');
      _renderSettingsAuthStatus(authStatus);
      _updateAuthWarningBadge(authStatus);
    }catch(e){}
  }catch(e){showToast('Failed to go passwordless: '+e.message);}
}

export async function disableAuth(){
  const currentPwField=$('settingsCurrentPassword');
  const currentPw=(currentPwField||{}).value||'';
  if(state._settingsPasswordAuthEnabled && !currentPw.trim()){
    if(currentPwField) currentPwField.focus();
    showToast(t('current_password_required'));
    return;
  }
  const confirmText='DISABLE AUTH';
  const userInput=await showPromptDialog({title:t('disable_auth_confirm_title'),message:t('disable_auth_confirm_message')+' '+t('disable_auth_typed_confirm'),placeholder:confirmText,confirmLabel:t('disable_auth'),danger:true});
  if(!userInput || userInput.trim()!==confirmText) return;
  const payload={_clear_password:true};
  if(state._settingsPasswordAuthEnabled) payload._current_password=currentPw;
  try{
    const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(payload)});
    showToast(t('auth_disabled'));
    const disableBtn=$('btnDisableAuth');
    if(disableBtn) disableBtn.style.display='none';
    const signOutBtn=$('btnSignOut');
    if(signOutBtn) signOutBtn.style.display='none';
    _syncPasswordlessButton({auth_enabled:false,password_auth_enabled:false,passkeys_count:0});
    state._settingsPasswordAuthEnabled=false;
    _updateCurrentPasswordVisibility();
    const cpField=$('settingsCurrentPassword'); if(cpField) cpField.value='';
    loadPasskeys();
    try{
      const authStatus=await api('/api/auth/status');
      _renderSettingsAuthStatus(authStatus);
      _updateAuthWarningBadge(authStatus);
      _updateAuthDisabledWarning(authStatus);
    }catch(e){}
  }catch(e){
    showToast(t('disable_auth_failed')+e.message);
  }
}
