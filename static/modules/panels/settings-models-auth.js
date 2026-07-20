import { state } from "./state.js";

import { _applyStructuredCodeViewSettings,_markSettingsDirty,_syncChatActivityDisplayModeControl,_syncTransparentEventTimestampsControl } from "./settings-navigation.js";
import { _applyWorkspaceTodosTabVisibility,_syncSettingsMaxTokensPlaceholder } from "./settings-preferences.js";
import { _ensureComposerControlVisibilityState,_renderComposerControlChips,_renderComposerSituationalControlChips,_setComposerControlOrder } from "./settings-state.js";

// Panels domain: authentication, updates, and auxiliary models

export function _setSettingsAuthButtonsVisible(active){
  const signOutBtn=$('btnSignOut');
  if(signOutBtn) signOutBtn.style.display=active?'':'none';
  const disableBtn=$('btnDisableAuth');
  if(disableBtn) disableBtn.style.display=active?'':'none';
  const passkeyBtn=$('btnRegisterPasskey');
  if(passkeyBtn) passkeyBtn.disabled=!active||!window.PublicKeyCredential||!navigator.credentials;
}
export function _syncPasswordlessButton(authStatus){
  const btn=$('btnGoPasswordless');
  if(!btn) return;
  const can=!!(authStatus&&authStatus.auth_enabled&&authStatus.password_auth_enabled&&authStatus.passkeys_count>0&&!state._settingsPasswordEnvLocked);
  btn.style.display=can?'':'none';
  btn.disabled=!can;
}

export function _renderSettingsAuthStatus(authStatus){
  const el=$('settingsAuthStatus');
  if(!el) return;
  if(!authStatus) { el.style.display='none'; return; }
  el.style.display='block';
  let label='',cls='detail-badge ok';
  if(authStatus.auth_enabled && authStatus.password_auth_enabled){
    label=t('auth_status_password'); cls='detail-badge ok';
  }else if(authStatus.auth_enabled && !authStatus.password_auth_enabled){
    label=t('auth_status_passkey_only'); cls='detail-badge warn';
  }else{
    label=t('auth_status_unauthenticated'); cls='detail-badge err';
  }
  el.innerHTML='<span class="'+cls+'" style="font-size:11px">'+label+'</span>';
}

export function _updateCurrentPasswordVisibility(){
  const block=$('settingsCurrentPasswordBlock');
  if(!block) return;
  block.style.display=state._settingsPasswordAuthEnabled?'block':'none';
}

export function _updateAuthWarningBadge(authStatus){
  const badges=['authWarningBadgeDesktop','authWarningBadgeMobile'];
  const authDisabled=!authStatus||!authStatus.auth_enabled;
  const acknowledged=!!(authStatus&&authStatus.auth_disabled_acknowledged);
  badges.forEach(function(id){
    const el=$(id);
    if(!el) return;
    if(!authDisabled){ el.style.display='none'; return; }
    el.style.display='block';
    el.style.background=acknowledged?'#e8a030':'#e05';
  });
}

export function _updateAuthDisabledWarning(authStatus){
  const el=$('settingsAuthDisabledWarning');
  if(!el) return;
  const authDisabled=!authStatus||!authStatus.auth_enabled;
  if(!authDisabled){ el.style.display='none'; return; }
  el.style.display='block';
  const cb=$('settingsAuthDisabledAck');
  if(cb) cb.checked=!!(authStatus&&authStatus.auth_disabled_acknowledged);
}

export async function _setAuthDisabledAck(checked){
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({_auth_disabled_acknowledged:!!checked})});
    try{
      const authStatus=await api('/api/auth/status');
      _updateAuthWarningBadge(authStatus);
    }catch(e){}
  }catch(e){
    showToast(t('auth_ack_save_failed')+e.message);
  }
}

export function _b64uToBytes(s){
  s=String(s||'').replace(/-/g,'+').replace(/_/g,'/');
  while(s.length%4) s+='=';
  const bin=atob(s), out=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) out[i]=bin.charCodeAt(i);
  return out;
}
export function _bytesToB64u(buf){
  const bytes=new Uint8Array(buf);let bin='';
  for(let i=0;i<bytes.length;i++) bin+=String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/g,'');
}

export async function loadPasskeys(){
  const list=$('passkeyList');
  const block=$('passkeysSettingsBlock');
  if(!list) return;
  // Stage-batch14: respect the HERMES_WEBUI_PASSKEY feature flag — hide the
  // whole block when passkey support is disabled at the server level so users
  // don't see a non-functional "Add passkey" button (clicking it would 404).
  try{
    const status=await api('/api/auth/status');
    if(status && status.passkey_feature_flag === false){
      if(block) block.style.display='none';
      return;
    }
    if(block) block.style.display='';
  }catch(_e){
    // If /api/auth/status fails, keep the block hidden to avoid showing a
    // broken affordance.
    if(block) block.style.display='none';
    return;
  }
  if(!window.PublicKeyCredential||!navigator.credentials){
    list.textContent='Passkeys are not supported by this browser/context.';
    const btn=$('btnRegisterPasskey'); if(btn) btn.disabled=true;
    return;
  }
  try{
    const data=await api('/api/auth/passkeys',{method:'POST',body:'{}'});
    if(data && data.disabled){
      if(block) block.style.display='none';
      return;
    }
    const creds=(data&&data.credentials)||[];
    if(!creds.length){list.textContent='No passkeys registered.';return;}
    list.innerHTML=creds.map(c=>`<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:6px"><span>${esc(c.label||'Passkey')}</span><button class="btn-tiny" onclick="deletePasskey('${esc(c.id)}')">Remove</button></div>`).join('');
  }catch(e){list.textContent='Failed to load passkeys: '+e.message;}
}

export async function registerPasskey(){
  if(!window.PublicKeyCredential||!navigator.credentials){showToast('Passkeys require a supported browser and secure context.');return;}
  const label='This device';
  try{
    const optData=await api('/api/auth/passkey/register/options',{method:'POST',body:'{}'});
    const pk=optData.publicKey;
    pk.challenge=_b64uToBytes(pk.challenge);
    pk.user=Object.assign({},pk.user,{id:_b64uToBytes(pk.user.id)});
    if(Array.isArray(pk.excludeCredentials)) pk.excludeCredentials=pk.excludeCredentials.map(c=>Object.assign({},c,{id:_b64uToBytes(c.id)}));
    const cred=await navigator.credentials.create({publicKey:pk});
    if(!cred) throw new Error('Passkey registration cancelled');
    await api('/api/auth/passkey/register',{method:'POST',body:JSON.stringify({
      id:cred.id,rawId:_bytesToB64u(cred.rawId),type:cred.type,label,
      response:{clientDataJSON:_bytesToB64u(cred.response.clientDataJSON),attestationObject:_bytesToB64u(cred.response.attestationObject)}
    })});
    showToast('Passkey registered');
    loadPasskeys();
    try{_syncPasswordlessButton(await api('/api/auth/status'));}catch(_e){}
  }catch(e){showToast('Passkey registration failed: '+e.message);}
}

export async function deletePasskey(id){
  const ok=await showConfirmDialog({title:'Remove passkey?',message:'This browser/device will no longer be able to sign in with that passkey.',confirmLabel:'Remove',danger:true,focusCancel:true});
  if(!ok) return;
  try{await api('/api/auth/passkey/delete',{method:'POST',body:JSON.stringify({id})});showToast('Passkey removed');loadPasskeys();try{_syncPasswordlessButton(await api('/api/auth/status'));}catch(_e){}}
  catch(e){showToast('Failed to remove passkey: '+e.message);}
}

export function _applySavedSettingsUi(saved, body, opts){
  const {sendKey,showTokenUsage,showQuotaChip,showConversationOutline,showBusyPlaceholderHint,showTps,fadeTextEffect,showCliSessions,theme,skin,language,sidebarDensity,fontSize}=opts;
  window._sendKey=sendKey||'enter';
  window._showTokenUsage=showTokenUsage;
  window._showQuotaChip=showQuotaChip===true;
  window._showConversationOutline=showConversationOutline===true;
  document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
  if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
  window._showBusyPlaceholderHint=showBusyPlaceholderHint===true;
  window._showTps=showTps;
  window._fadeTextEffect=!!fadeTextEffect;
  window._showCliSessions=showCliSessions;
  window._showPreviousMessagingSessions=!!body.show_previous_messaging_sessions;
  window._soundEnabled=body.sound_enabled;
  window._notificationsEnabled=body.notifications_enabled;
  window._whatsNewSummaryEnabled=!!body.whats_new_summary_enabled;
  window._showThinking=body.show_thinking!==false;
  window._simplifiedToolCalling=true;
  _syncChatActivityDisplayModeControl(body.chat_activity_display_mode);
  _syncTransparentEventTimestampsControl(body.transparent_stream_event_timestamps, body.chat_activity_display_mode);
  window._terminalAutoExpandOnOutput=!!body.terminal_auto_expand_on_output;
  window._workspaceTodosTab=!!body.workspace_todos_tab;
  if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
  window._sessionJumpButtonsEnabled=!!body.session_jump_buttons;
  if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
  window._sidebarDensity=sidebarDensity==='detailed'?'detailed':'compact';
  // #5170 mirror write in _applySavedSettingsUi, under the #5145 rename:
  // persist so a reload/offline first-send honors the resolved mode.
  window._defaultMessageMode=(typeof _persistDefaultMessageMode==='function')
    ? _persistDefaultMessageMode(body.default_message_mode||body.busy_input_mode)
    : (body.default_message_mode||body.busy_input_mode||'steer');
  window._sessionEndlessScrollEnabled=!!body.session_endless_scroll;
  window._autoScrollFollow=body.auto_scroll_follow!==false;
  window._largeTextPasteAsAttachment=body.large_text_paste_as_attachment!==false;
  window._projectQuickCreate=!!body.project_quick_create_buttons;
  if(Object.prototype.hasOwnProperty.call(body,'structured_code_default_view')){
    _applyStructuredCodeViewSettings(body.structured_code_default_view,body.structured_code_auto_tree_lines,false);
  }
  window._botName=body.bot_name||'Hermes';
  if(typeof applyBotName==='function') applyBotName();
  else if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
  if(typeof setLocale==='function') setLocale(language);
  if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
  _ensureComposerControlVisibilityState(saved||body||{});
  const composerOrderSource=(saved&&Array.isArray(saved.composer_control_order))
    ? saved.composer_control_order
    : (Array.isArray(body.composer_control_order)?body.composer_control_order:null);
  if(composerOrderSource){
    const composerOrder=_setComposerControlOrder(composerOrderSource);
    if(typeof window._applyComposerControlOrder==='function') window._applyComposerControlOrder(composerOrder);
  }
  _renderComposerControlChips();
  _renderComposerSituationalControlChips();
  if(typeof _applyComposerFooterVisibilitySettings==='function') _applyComposerFooterVisibilitySettings();
  const maxTokensField=$('settingsMaxTokens');
  if(maxTokensField){
    const savedRawMaxTokens=saved&&saved.max_tokens;
    const parsedSavedMaxTokens=parseInt(savedRawMaxTokens,10);
    maxTokensField.value=(Number.isFinite(parsedSavedMaxTokens)&&parsedSavedMaxTokens>0)
      ? String(parsedSavedMaxTokens)
      : '';
    _syncSettingsMaxTokensPlaceholder(maxTokensField,saved&&saved.max_tokens_fallback);
    maxTokensField.dataset.initialValue=maxTokensField.value;
  }
  if(typeof startGatewaySSE==='function'){
    if(showCliSessions) startGatewaySSE();
    else if(typeof stopGatewaySSE==='function') stopGatewaySSE();
  }
  _setSettingsAuthButtonsVisible(!!saved.auth_enabled);
  state._settingsDirty=false;
  state._settingsThemeOnOpen=theme;
  state._settingsSkinOnOpen=skin||'default';
  state._settingsFontSizeOnOpen=fontSize||localStorage.getItem('hermes-font-size')||'default';
  const bar=$('settingsUnsavedBar');
  if(bar) bar.style.display='none';
  state._settingsHermesDefaultModelOnOpen=body.default_model||state._settingsHermesDefaultModelOnOpen||'';
  if(Object.prototype.hasOwnProperty.call(body,'default_model_provider')) state._settingsHermesDefaultModelProviderOnOpen=body.default_model_provider||null;
  // Sync window._defaultModel so newSession() uses the just-saved default without a reload (#908).
  if(body.default_model) window._defaultModel=body.default_model;
  if(Object.prototype.hasOwnProperty.call(body,'default_model_provider')) window._activeProvider=body.default_model_provider||null;
  if(typeof clearMessageRenderCache==='function') clearMessageRenderCache();
  renderMessages();
  if(typeof syncTopbar==='function') syncTopbar();
  if(typeof renderSessionList==='function') renderSessionList();
}

// Instant client-side badge feedback when the update channel is toggled, before
// the server round-trip that authoritatively re-renders the badge from
// update_channel_version. Keeps the "· Experimental" suffix in sync immediately.
export function _syncUpdateChannelBadge(channel){
  try{
    const badge=$('settings-webui-version-badge');
    if(!badge) return;
    let base=badge.textContent||'';
    // Strip any existing " · Experimental" suffix, then re-append if needed.
    base=base.replace(/\s·\sExperimental\s*$/,'');
    badge.textContent = channel==='experimental' ? (base+' · Experimental') : base;
  }catch(e){}
}

export async function checkUpdatesNow(channelOverride){
  const btn=$('btnCheckUpdatesNow');
  const label=$('checkUpdatesLabel');
  const spinner=$('checkUpdatesSpinner');
  const status=$('checkUpdatesStatus');
  if(!btn||!label) return;
  // Disable button, show spinner
  btn.disabled=true;
  if(spinner) spinner.style.display='';
  if(label) label.textContent=t('settings_checking');
  if(status) status.textContent='';

  try {
    // Pass the channel explicitly when the caller has one (e.g. the dropdown
    // just switched) so the check cannot race the debounced settings autosave
    // and answer for the previous channel. Omit otherwise → server uses the
    // saved setting. (Fable UX gate.)
    const _checkBody={force:true};
    if(channelOverride==='stable'||channelOverride==='experimental') _checkBody.channel=channelOverride;
    const data=await api('/api/updates/check',{method:'POST',body:JSON.stringify(_checkBody),timeoutMs:60000});
    if(data.disabled){
      if(status){status.textContent=t('settings_updates_disabled');status.style.color='var(--muted)';}
    } else {
      const errorParts=[];
      const formatUpdateError=(typeof _formatUpdateCheckError==='function')
        ? _formatUpdateCheckError
        : ((label,info)=>info&&info.error?label:null);
      const webuiError=formatUpdateError('WebUI',data.webui);
      const agentError=formatUpdateError('Agent',data.agent);
      if(webuiError) errorParts.push(webuiError);
      if(agentError) errorParts.push(agentError);
      const parts=[];
      const formatUpdatePart=(typeof _formatUpdateTargetStatus==='function')
        ? _formatUpdateTargetStatus
        : ((label,info)=>info&&info.behind>0?label+': '+info.behind:null);
      const webuiPart=formatUpdatePart('WebUI',data.webui);
      const agentPart=formatUpdatePart('Agent',data.agent);
      if(webuiPart) parts.push(webuiPart);
      if(agentPart) parts.push(agentPart);
      const manualInstruction=(typeof _formatManualUpdateInstruction==='function')
        ? _formatManualUpdateInstruction(data.webui)
        : null;
      // Track non-git targets separately so a mixed deployment (one git
      // checkout + one no-git install) never hides the "can't check" state
      // behind an up-to-date summary (#4356).
      const noGitParts=[];
      if(data.webui&&data.webui.no_git&&!data.webui.manual_update) noGitParts.push('WebUI');
      if(data.agent&&data.agent.no_git&&!data.agent.ignored) noGitParts.push('Agent');
      if(parts.length){
        let txt=t('settings_updates_available').replace('{count}',parts.join(', '));
        if(manualInstruction) txt+=' · '+manualInstruction;
        if(noGitParts.length) txt+=' · '+t('settings_update_no_git');
        if(status){status.textContent=txt;status.style.color='var(--accent)';}
        // Also trigger the update banner
        if(typeof _showUpdateBanner==='function') _showUpdateBanner(data);
      } else if(errorParts.length){
        if(status){status.textContent=t('settings_update_check_failed')+': '+errorParts.join(', ');status.style.color='var(--error)';}
      } else if(noGitParts.length){
        if(status){status.textContent=t('settings_update_no_git');status.style.color='var(--muted)';}
      } else {
        if(status){status.textContent=t('settings_up_to_date');status.style.color='var(--success)';}
        if(typeof _showUpdateBanner==='function') _showUpdateBanner(data);
      }
    }
  } catch(e){
    // Never expose raw e.message in UI — log to console for debugging only
    console.warn('[checkUpdatesNow]', e);
    // Show a generic user-facing error; if the API returned a message body use it
    let userMsg=t('settings_update_check_failed');
    if(e&&e.response){
      try{
        const body=JSON.parse(e.response);
        if(body.error) userMsg=String(body.error).substring(0,120);
      }catch(_){}
    }
    if(status){status.textContent=userMsg;status.style.color='var(--error)';}
  } finally {
    btn.disabled=false;
    if(spinner) spinner.style.display='none';
    if(label) label.textContent=t('settings_check_now');
  }
}
// ── Auxiliary Models ──────────────────────────────────────────────────────────


export function _auxSelectStyle(){
 return 'width:100%;padding:6px 8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px;font-size:12px;box-sizing:border-box';
}

export function _auxTaskLabelFromMeta(taskKey, taskCfg){
  const nameKey='settings_aux_task_'+taskKey;
  const descKey=nameKey+'_desc';
  const tName=t(nameKey);
  const tDesc=t(descKey);
  const name=(tName&&tName!==nameKey)?String(tName).trim():'';
  const description=(tDesc&&tDesc!==descKey)?String(tDesc).trim():'';
  const fallbackName=(taskCfg&&typeof taskCfg.label==='string'&&taskCfg.label.trim())?String(taskCfg.label).trim():taskKey;
  const fallbackDesc=(taskCfg&&typeof taskCfg.description==='string'&&taskCfg.description.trim())?String(taskCfg.description).trim():'';
  return {
    task: taskKey,
    label: name||fallbackName,
    description: description||fallbackDesc,
  };
}

export function _normalizeAuxiliaryTasks(rawTasks){
  const tasks=Array.isArray(rawTasks)?rawTasks:[];
  const out=[];
  const seen=new Set();
  for(const rawTask of tasks){
    if(!rawTask||typeof rawTask!=='object') continue;
    const task=(typeof rawTask.task==='string'?String(rawTask.task).trim():'');
    if(!task||seen.has(task)) continue;
    seen.add(task);
    const meta=_auxTaskLabelFromMeta(task,rawTask);
    const entry={
      task,
      provider:String(rawTask.provider||'auto').trim()||'auto',
      model:String(rawTask.model||'').trim(),
      base_url:String(rawTask.base_url||'').trim(),
      timeout:rawTask.timeout,
      download_timeout:rawTask.download_timeout,
      max_concurrency:rawTask.max_concurrency,
      extra_body:rawTask.extra_body&&typeof rawTask.extra_body==='object'?rawTask.extra_body:{},
      api_key_set:!!rawTask.api_key_set,
      label:meta.label,
      description:meta.description,
    };
    out.push(entry);
  }
  return out;
}

export function _buildAuxProviderOptions(sel,providers,currentProvider){
 sel.innerHTML='';
 // "auto" = use main model
 const autoOpt=document.createElement('option');
 autoOpt.value='auto';autoOpt.textContent='auto ('+t('settings_aux_provider_auto')+')';
 if(currentProvider==='auto'||!currentProvider) autoOpt.selected=true;
 sel.appendChild(autoOpt);
 for(const p of providers){
  const opt=document.createElement('option');
  opt.value=p.slug;opt.textContent=p.name;
  if(p.slug===currentProvider) opt.selected=true;
  sel.appendChild(opt);
 }
}

export function _buildAuxModelOptions(sel,provider,providers,currentModel){
 sel.innerHTML='';
 const emptyOpt=document.createElement('option');
 emptyOpt.value='';emptyOpt.textContent=t('settings_aux_model_auto')||'auto (use provider default)';
 sel.appendChild(emptyOpt);
 if(!provider||provider==='auto'){
  sel.value=currentModel||'';
  return;
 }
 // Find matching provider in cached list
 const pData=providers.find(p=>p.slug===provider);
 if(pData&&pData.models){
  for(const mId of pData.models){
   const opt=document.createElement('option');
   opt.value=mId;opt.textContent=mId;
   if(mId===currentModel) opt.selected=true;
   sel.appendChild(opt);
  }
 }
 // Always allow custom model — add a text input option hint
 const customOpt=document.createElement('option');
 customOpt.value='__custom__';customOpt.textContent=t('settings_aux_model_custom')||'Custom model…';
 sel.appendChild(customOpt);
 // If currentModel not in list and not empty, add it as a custom option
 if(currentModel&&!pData?.models?.includes(currentModel)){
  const existingOpt=document.createElement('option');
  existingOpt.value=currentModel;existingOpt.textContent=currentModel+' (configured)';
  existingOpt.selected=true;
  sel.insertBefore(existingOpt,customOpt);
 }
}

export function _onAuxProviderChange(taskKey,providers){
 const provSel=$('aux-prov-'+taskKey);
 const modelSel=$('aux-model-'+taskKey);
 if(!provSel||!modelSel) return;
 const provider=provSel.value;
 _buildAuxModelOptions(modelSel,provider,providers,'');
 _markAuxDirty();
}

export async function _onAuxModelChange(taskKey){
 const modelSel=$('aux-model-'+taskKey);
 if(!modelSel) return;
 if(modelSel.value==='__custom__'){
  const customModel=await showPromptDialog({title:t('settings_aux_model_custom')||'Custom model',message:t('settings_aux_model_custom_prompt')||'Enter model ID:',placeholder:'model/provider:model-id',confirmLabel:t('settings_btn_apply_aux_models')||'Apply'});
  if(customModel&&customModel.trim()){
   // Insert custom model option before the __custom__ option
   const opt=document.createElement('option');
   opt.value=customModel.trim();opt.textContent=customModel.trim();
   // Remove __custom__ selection
   const customIdx=[...modelSel.options].findIndex(o=>o.value==='__custom__');
   if(customIdx>=0) modelSel.insertBefore(opt,modelSel.options[customIdx]);
   modelSel.value=customModel.trim();
  }else{
   modelSel.value='';
  }
 }
 _markAuxDirty();
}

export function _markAuxDirty(){
 const applyBtn=$('btnApplyAuxModels');
 if(applyBtn) applyBtn.style.display='';
 _markSettingsDirty();
}

export function _auxAdvancedValue(cfg,key){
 const v=cfg&&Object.prototype.hasOwnProperty.call(cfg,key)?cfg[key]:'';
 return v===null||v===undefined?'':String(v);
}

export function _ensureAuxAdvancedModal(){
 let overlay=$('auxAdvancedOverlay');
 if(overlay) return overlay;
 overlay=document.createElement('div');
 overlay.id='auxAdvancedOverlay';
 overlay.style.cssText='position:fixed;inset:0;z-index:9999;background:rgba(5,7,15,.68);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;padding:20px';
 const neutralBtn='font-size:12px;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-weight:600';
 const primaryBtn='font-size:12px;padding:7px 12px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#1a1a1a;cursor:pointer;font-weight:700';
 overlay.innerHTML=`<div role="dialog" aria-modal="true" aria-labelledby="auxAdvancedTitle" style="width:min(620px,calc(100vw - 32px));max-height:calc(100vh - 48px);overflow:auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,.45);padding:16px">
  <style>#auxAdvancedOverlay input:-webkit-autofill,#auxAdvancedOverlay textarea:-webkit-autofill{box-shadow:0 0 0 1000px var(--code-bg) inset!important;-webkit-box-shadow:0 0 0 1000px var(--code-bg) inset!important;-webkit-text-fill-color:var(--text)!important;caret-color:var(--text)!important}</style>
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px">
   <div><div id="auxAdvancedTitle" style="font-weight:700;font-size:16px"></div><div id="auxAdvancedSubtitle" style="font-size:11px;color:var(--muted);margin-top:2px"></div></div>
   <button type="button" id="auxAdvancedClose" aria-label="${esc(t('terminal_close')||'Close')}" style="width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;border-radius:8px;border:1px solid var(--border);background:var(--input-bg);color:var(--text);cursor:pointer;font-size:18px;line-height:1">×</button>
  </div>
  <div id="auxAdvancedBody" style="display:grid;gap:10px"></div>
  <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
   <button type="button" id="auxAdvancedCancel" style="${neutralBtn}">${esc(t('cancel')||'Cancel')}</button>
   <button type="button" id="auxAdvancedSave" style="${primaryBtn}">${esc(t('settings_aux_advanced_save')||'Save options')}</button>
  </div>
 </div>`;
 document.body.appendChild(overlay);
 const close=()=>{overlay.style.display='none';overlay.dataset.task='';};
 $('auxAdvancedClose')?.addEventListener('click',close);
 $('auxAdvancedCancel')?.addEventListener('click',close);
 overlay.addEventListener('click',ev=>{if(ev.target===overlay) close();});
 return overlay;
}

export function _auxAdvancedInputHtml(id,label,value,desc,type='text',extraAttrs='',extraStyle=''){
 const fieldName=id==='auxAdvancedApiKey'?'aux-manual-override-value':('aux-field-'+id.replace(/^auxAdvanced/,'').toLowerCase());
 const autocompleteAttr=/\bautocomplete=/.test(extraAttrs)?'':'autocomplete="off"';
 const inputAttrs=`id="${id}" name="${fieldName}" type="${type}" value="${esc(value)}" ${autocompleteAttr} autocapitalize="off" autocorrect="off" spellcheck="false" data-lpignore="true" data-1p-ignore="true" ${extraAttrs}`;
 return `<label style="display:grid;gap:4px;font-size:12px;color:var(--text)"><span style="font-weight:600">${esc(label)}</span><input ${inputAttrs} style="width:100%;box-sizing:border-box;padding:7px 8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px;font-size:12px${extraStyle}"><span style="font-size:10px;color:var(--muted);line-height:1.35">${esc(desc)}</span></label>`;
}

export function _mainModelSupportsServiceTier(cfg){
 const selected=$('settingsModel');
 const selectedOpt=selected&&selected.selectedIndex>=0?selected.options[selected.selectedIndex]:null;
 const optgroup=selectedOpt&&selectedOpt.parentElement&&selectedOpt.parentElement.tagName==='OPTGROUP'?selectedOpt.parentElement:null;
 const provider=((selectedOpt&&selectedOpt.dataset&&selectedOpt.dataset.provider)||(optgroup&&optgroup.dataset&&optgroup.dataset.provider)||(cfg&&cfg.provider)||'').trim().toLowerCase();
 if(provider!=='openai'&&provider!=='openai-api'&&provider!=='openai-codex') return false;
 const fastSupport = selectedOpt&&selectedOpt.dataset?selectedOpt.dataset.fast:'';
 if(fastSupport) return fastSupport==='1'||fastSupport==='true';
 return cfg&&cfg.supports_fast_tier===true;
}

export function _openAuxAdvancedOptions(taskCfg,cfg){
 const isMain=taskCfg==='__main__';
 const taskKey=isMain?'__main__':(taskCfg&&typeof taskCfg==='object'&&typeof taskCfg.task==='string'?taskCfg.task:typeof taskCfg==='string'?taskCfg:'');
 const slot=isMain?{task:taskKey,label:(t('settings_label_model')||'Default model')}:_auxTaskLabelFromMeta(taskKey,taskCfg);
 const overlay=_ensureAuxAdvancedModal();
 overlay.dataset.task=taskKey;
 const title=$('auxAdvancedTitle'),sub=$('auxAdvancedSubtitle'),body=$('auxAdvancedBody');
 const slotName=isMain?(t('settings_label_model')||'Default model'):(slot&&slot.label)||taskKey;
 if(title) title.textContent=isMain?(t('settings_main_advanced_title')||'Main model options'):((t('settings_aux_advanced_title')||'{task} options').replace('{task}',slotName));
 if(sub) sub.textContent=isMain?(t('settings_main_advanced_subtitle')||'Advanced config for the default chat model.'):(t('settings_aux_advanced_subtitle')||'Advanced config for auxiliary.');
 const extraBody=cfg&&cfg.extra_body&&typeof cfg.extra_body==='object'&&Object.keys(cfg.extra_body).length?JSON.stringify(cfg.extra_body,null,2):'';
 const apiKeyHint=cfg&&cfg.api_key_set?(t('settings_aux_advanced_api_key_set_hint')||'API key is set. Leave blank to keep it, or use clear to remove it.'):(t('settings_aux_advanced_api_key_empty_hint')||'Leave blank to use provider/default credentials.');
 if(body){
  const selectedServiceTier=((cfg&&cfg.service_tier)||'').trim().toLowerCase()==='priority'?'priority':'';
  const serviceTierField=isMain&&_mainModelSupportsServiceTier(cfg)
   ? `<label style="display:grid;gap:4px;font-size:12px;color:var(--text)"><span style="font-weight:600">${esc(t('settings_main_advanced_service_tier')||'Service tier')}</span><select id="auxAdvancedServiceTier" style="width:100%;box-sizing:border-box;padding:7px 8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px;font-size:12px"><option value=""${selectedServiceTier?'':' selected'}>${esc(t('settings_main_advanced_service_tier_default')||'Default / off')}</option><option value="priority"${selectedServiceTier==='priority'?' selected':''}>${esc(t('settings_main_advanced_service_tier_priority')||'Priority (fast)')}</option></select><span style="font-size:10px;color:var(--muted);line-height:1.35">${esc(t('settings_main_advanced_service_tier_desc')||'Optional request setting for OpenAI-family providers.')}</span></label>`
   : '';
  const timingFields=isMain?'':(
   _auxAdvancedInputHtml('auxAdvancedTimeout',t('settings_aux_advanced_timeout')||'Timeout seconds',_auxAdvancedValue(cfg,'timeout'),t('settings_aux_advanced_timeout_desc')||'Request timeout for this auxiliary task. Blank uses Hermes default.','number','inputmode="numeric" min="1" step="1"')+
   _auxAdvancedInputHtml('auxAdvancedDownloadTimeout',t('settings_aux_advanced_download_timeout')||'Download timeout seconds',_auxAdvancedValue(cfg,'download_timeout'),t('settings_aux_advanced_download_timeout_desc')||'Only relevant for tasks that download media/content, e.g. vision. Blank uses default.','number','inputmode="numeric" min="1" step="1"')+
   _auxAdvancedInputHtml('auxAdvancedMaxConcurrency',t('settings_aux_advanced_max_concurrency')||'Max concurrency',_auxAdvancedValue(cfg,'max_concurrency'),t('settings_aux_advanced_max_concurrency_desc')||'Optional per-task concurrency limit. Blank uses default.','number','inputmode="numeric" min="1" step="1"'));
  body.innerHTML=
   _auxAdvancedInputHtml('auxAdvancedBaseUrl',t('settings_aux_advanced_base_url')||'Base URL',_auxAdvancedValue(cfg,'base_url'),t('settings_aux_advanced_base_url_desc')||'Optional provider endpoint override.','text','inputmode="url"')+
   serviceTierField+
   timingFields+
   `<label style="display:grid;gap:4px;font-size:12px;color:var(--text)"><span style="font-weight:600">${esc(t('settings_aux_advanced_extra_body')||'Extra body JSON')}</span><textarea id="auxAdvancedExtraBody" rows="6" style="width:100%;box-sizing:border-box;padding:7px 8px;background:var(--code-bg);color:var(--text);border:1px solid var(--border2);border-radius:6px;font-size:12px;font-family:var(--mono,monospace)">${esc(extraBody)}</textarea><span style="font-size:10px;color:var(--muted);line-height:1.35">${esc(t('settings_aux_advanced_extra_body_desc')||'Optional JSON object merged into the model request body.')}</span></label>`+
   _auxAdvancedInputHtml('auxAdvancedApiKey',t('settings_aux_advanced_api_key')||'API key override','',apiKeyHint,'text','autocomplete="one-time-code" inputmode="text" readonly onfocus="this.removeAttribute(&quot;readonly&quot;)"',';-webkit-text-security:disc')+
   `<label style="display:${cfg&&cfg.api_key_set?'flex':'none'};align-items:center;gap:8px;font-size:12px;color:var(--text)"><input id="auxAdvancedApiKeyClear" type="checkbox" style="width:15px;height:15px;accent-color:var(--accent)"><span>${esc(t('settings_aux_advanced_api_key_clear')||'Clear existing API key override')}</span></label>`;
 }
 const save=$('auxAdvancedSave');
 if(save){
  save.onclick=async()=>{
   let extra={};
   const extraText=($('auxAdvancedExtraBody')?.value||'').trim();
   if(extraText){
    try{extra=JSON.parse(extraText);}catch(e){if(typeof showToast==='function') showToast(t('settings_aux_advanced_extra_body_invalid_json')||'Extra body must be valid JSON');return;}
    if(!extra||Array.isArray(extra)||typeof extra!=='object'){if(typeof showToast==='function') showToast(t('settings_aux_advanced_extra_body_object_required')||'Extra body must be a JSON object');return;}
   }
   const provSel=isMain?null:$('aux-prov-'+taskKey),modelSel=isMain?$('settingsModel'):$('aux-model-'+taskKey);
   const provider=isMain?((cfg&&cfg.provider)||''):(provSel?provSel.value:((cfg&&cfg.provider)||'auto'));
   const model=modelSel&&modelSel.value!=='__custom__'?(modelSel.value||''):((cfg&&cfg.model)||'');
   const advanced={
    base_url:$('auxAdvancedBaseUrl')?.value||'',
    extra_body:extra,
    api_key:$('auxAdvancedApiKey')?.value||'',
    api_key_clear:!!($('auxAdvancedApiKeyClear')&&$('auxAdvancedApiKeyClear').checked),
   };
   if(isMain&&$('auxAdvancedServiceTier')){
    advanced.service_tier=$('auxAdvancedServiceTier')?.value||'';
   }
   if(!isMain){
    advanced.timeout=$('auxAdvancedTimeout')?.value||'';
    advanced.download_timeout=$('auxAdvancedDownloadTimeout')?.value||'';
    advanced.max_concurrency=$('auxAdvancedMaxConcurrency')?.value||'';
   }
   try{
    await api('/api/model/set',{method:'POST',body:JSON.stringify({scope:isMain?'main':'auxiliary',task:isMain?'':taskKey,provider,model,advanced})});
    if(typeof showToast==='function') showToast(isMain?(t('settings_main_advanced_saved')||'Main model options saved'):(t('settings_aux_advanced_saved')||'Auxiliary options saved'));
    overlay.style.display='none';
    _loadAuxiliaryModels();
    // #4650 review: a main-model advanced save can change base_url, which
    // /api/reasoning's answer depends on for some providers (e.g. LM Studio),
    // WITHOUT changing the model/provider cache key. Invalidate the reasoning
    // cache and refresh so the chip reflects the new config (one refetch).
    if(isMain){
      if(typeof _lastReasoningFetchKey!=='undefined') _lastReasoningFetchKey=null;
      if(typeof fetchReasoningChip==='function') fetchReasoningChip();
    }
   }catch(e){
    if(typeof showToast==='function') showToast(isMain?(t('settings_main_advanced_save_failed')||'Failed to save main model options'):(t('settings_aux_advanced_save_failed')||'Failed to save auxiliary options'));
   }
  };
 }
 overlay.style.display='flex';
 setTimeout(()=>$('auxAdvancedBaseUrl')?.focus(),0);
}

export function _bindMainAdvancedOptionsButton(){
 const modelSel=$('settingsModel');
 let btn=$('mainAdvancedBtn');
 if(modelSel){
  const parent=modelSel.parentElement;
  let row=parent&&parent.classList&&parent.classList.contains('model-advanced-row')?parent:null;
  if(!row){
   row=document.createElement('div');
   row.className='model-advanced-row';
   parent.insertBefore(row,modelSel);
   row.appendChild(modelSel);
  }
  if(!btn){
   btn=document.createElement('button');
   btn.type='button';
   btn.id='mainAdvancedBtn';
  }
  if(btn.parentElement!==row) row.appendChild(btn);
  row.style.cssText='display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:8px;align-items:center';
  modelSel.style.width='100%';
  modelSel.style.minWidth='0';
  modelSel.style.boxSizing='border-box';
 }
 if(!btn) return;
 btn.classList.add('model-advanced-btn');
 if(!btn.querySelector('svg')&&typeof li==='function') btn.innerHTML=li('settings',15);
 btn.style.position='';
 btn.style.right='';
 btn.style.top='';
 btn.style.transform='';
 btn.style.width='32px';
 btn.style.height='32px';
 btn.style.display='flex';
 btn.style.alignItems='center';
 btn.style.justifyContent='center';
 btn.style.flex='0 0 32px';
 btn.style.boxSizing='border-box';
 const title=t('settings_aux_advanced_button_title')||'Advanced options';
 btn.title=title;
 btn.setAttribute('aria-label',t('settings_main_advanced_button_aria')||'Advanced options for main model');
 btn.disabled=state._mainAdvancedConfig===null;
 btn.style.opacity='';
 btn.style.cursor='';
 if(btn._bound) return;
 btn._bound=true;
 btn.addEventListener('click',()=>{if(state._mainAdvancedConfig!==null)_openAuxAdvancedOptions('__main__',state._mainAdvancedConfig||{});});
}

export async function _loadAuxiliaryModels(){
 const container=$('auxModelsContainer');
 if(!container) return;
 container.innerHTML='<div style="color:var(--muted);font-size:12px">'+(t('settings_aux_loading')||'Loading…')+'</div>';

 try{
  // Fetch auxiliary config AND the WebUI's own /api/models for provider/model lists
  const [auxData,modelsData]=await Promise.all([
   api('/api/model/auxiliary').catch(()=>null),
   api('/api/models').catch(()=>null),
  ]);
  // Build provider list from /api/models groups
  // /api/models returns: { groups: [{ provider: str, provider_id: str, models: [{id,label}] }] }
  const groups=(modelsData&&modelsData.groups)||[];
  state._auxProviders=groups.filter(g=>g.provider&&((g.models&&g.models.length>0)||(g.extra_models&&g.extra_models.length>0))).map(g=>({
   slug:g.provider_id||g.provider,
   name:g.provider,
   models:[...(g.models||[]),...(g.extra_models||[])].map(m=>m.id),
  }));
  if(auxData&&Object.prototype.hasOwnProperty.call(auxData,'main')){
   state._mainAdvancedConfig=auxData.main||{};
  }else{
   state._mainAdvancedConfig=null;
  }
  _bindMainAdvancedOptionsButton();
  state._auxTasks=_normalizeAuxiliaryTasks((auxData&&auxData.tasks)||[]);
  // Build a quick lookup: taskKey → config
  const taskMap={};
  for(const task of state._auxTasks) taskMap[task.task]=task;
  state._auxOriginalConfig=JSON.parse(JSON.stringify(taskMap));

  container.innerHTML='';
  for(const task of state._auxTasks){
   const cfg=taskMap[task.task]||{provider:'auto',model:''};
   const row=document.createElement('div');
   row.style.cssText='display:grid;grid-template-columns:120px 1fr 1fr 34px;gap:8px;align-items:center;margin-bottom:8px';

   // Task name + description
   const label=document.createElement('div');
   label.style.cssText='font-size:12px;font-weight:500;color:var(--text);line-height:1.3';
   label.innerHTML=esc(task.label||task.task)+'<div style="font-size:10px;color:var(--muted);font-weight:400">'+esc(task.description||'')+'</div>';
   row.appendChild(label);

   // Provider select
   const provSel=document.createElement('select');
   provSel.id='aux-prov-'+task.task;
   provSel.style.cssText=_auxSelectStyle();
   _buildAuxProviderOptions(provSel,state._auxProviders,cfg.provider);
   provSel.addEventListener('change',()=>_onAuxProviderChange(task.task,state._auxProviders));
   row.appendChild(provSel);

   // Model select
   const modelSel=document.createElement('select');
   modelSel.id='aux-model-'+task.task;
   modelSel.style.cssText=_auxSelectStyle();
   _buildAuxModelOptions(modelSel,cfg.provider,state._auxProviders,cfg.model);
   modelSel.addEventListener('change',()=>_onAuxModelChange(task.task));
   row.appendChild(modelSel);

   const advancedBtn=document.createElement('button');
   advancedBtn.type='button';
   advancedBtn.className='aux-advanced-btn model-advanced-btn';
   const advTitle=t('settings_aux_advanced_button_title')||'Advanced options';
   const taskName=task.label||task.task;
   advancedBtn.title=advTitle;
   advancedBtn.setAttribute('aria-label',(t('settings_aux_advanced_button_aria')||'Advanced options for {task}').replace('{task}',taskName));
   advancedBtn.innerHTML=typeof li==='function'?li('settings',15):'⚙';
   advancedBtn.addEventListener('click',()=>_openAuxAdvancedOptions(task,cfg));
   row.appendChild(advancedBtn);

   container.appendChild(row);
  }
  // Hide apply button (no changes yet)
  const applyBtn=$('btnApplyAuxModels');
  if(applyBtn) applyBtn.style.display='none';

  // Reset button
  const resetBtn=$('btnResetAuxModels');
  if(resetBtn&&!resetBtn._bound){
   resetBtn._bound=true;
   resetBtn.addEventListener('click',async()=>{
    if(!(await showConfirmDialog({title:t('settings_aux_reset_confirm_title')||'Reset auxiliary models?',message:t('settings_aux_reset_confirm_msg')||'This will set all auxiliary tasks to auto (use main model).',confirmLabel:t('settings_btn_reset_aux_models')||'Reset',danger:true}))) return;
    try{
     await api('/api/model/set',{method:'POST',body:JSON.stringify({scope:'auxiliary',task:'__reset__',provider:'auto',model:''})});
     if(typeof showToast==='function') showToast(t('settings_aux_reset_done')||'Auxiliary models reset to auto');
     _loadAuxiliaryModels();
    }catch(e){
     if(typeof showToast==='function') showToast(t('settings_aux_save_failed')||'Failed to reset auxiliary models');
    }
   });
  }

  // Apply button
  if(applyBtn&&!applyBtn._bound){
   applyBtn._bound=true;
   applyBtn.addEventListener('click',_applyAuxModels);
  }
 }catch(e){
  console.warn('[settings] auxiliary models load failed',e);
  container.innerHTML='<div style="color:var(--muted);font-size:12px">'+(t('settings_aux_load_failed')||'Could not load auxiliary model settings. Make sure the agent API is available.')+'</div>';
 }
}

export async function _applyAuxModels(){
 let saved=0;
 for(const task of state._auxTasks){
  const provSel=$('aux-prov-'+task.task);
  const modelSel=$('aux-model-'+task.task);
  if(!provSel) continue;
  const provider=provSel.value;
  const model=(modelSel&&modelSel.value!=='__custom__')?(modelSel.value||''):'';
  const orig=state._auxOriginalConfig?.[task.task]||{provider:'auto',model:''};
  // Only save if changed
  if(provider!==orig.provider||model!==orig.model){
   try{
    await api('/api/model/set',{method:'POST',body:JSON.stringify({scope:'auxiliary',task:task.task,provider,model})});
    saved++;
   }catch(e){
    console.warn('[settings] failed to save aux task',task.task,e);
    if(typeof showToast==='function') showToast(t('settings_aux_save_failed')||'Failed to save auxiliary model');
    return;
   }
  }
 }
 if(typeof showToast==='function') showToast(saved?(t('settings_aux_saved')||'Auxiliary models updated'):(t('settings_aux_no_changes')||'No changes to apply'));
 // Reload to refresh state
 _loadAuxiliaryModels();
}
