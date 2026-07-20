window.HermesBoot.begin('bootstrapCoordinator');
(async()=>{
  // Load send key preference
  let _bootSettings={};
  const prefillIntent=(typeof _composerPrefillIntentFromLocation==='function')?_composerPrefillIntentFromLocation():null;
  try{
    const s=await api('/api/settings');
    _bootSettings=s;
    if(typeof checkWebUIVersionSkew==='function'){try{checkWebUIVersionSkew(s);}catch(_){}}
    window._sendKey=s.send_key||'enter';
    // Persist default workspace so the blank new-chat page can show it
    // and workspace actions (New file/folder) work before the first session (#804).
    if(s.default_workspace) S._profileDefaultWorkspace=s.default_workspace;
    window._showTokenUsage=!!s.show_token_usage;
    window._showQuotaChip=s.show_quota_chip===true;
    window._showConversationOutline=s.show_conversation_outline===true;
    document.documentElement.dataset.conversationOutline=window._showConversationOutline?'enabled':'disabled';
    if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    window._hideEmptyStateSuggestions=s.hide_empty_state_suggestions===true;
    applyEmptyStateSuggestionPref();
    // #4343: transcript virtualization is EXPERIMENTAL/opt-IN (default OFF).
    // It caused scroll-up flicker on long sessions, so it's off for everyone
    // unless explicitly opted in; long transcripts render in full by default.
    window._virtualizeTranscript=s.virtualize_transcript===true;
    window._showTps=!!s.show_tps;
    window._fadeTextEffect=!!s.fade_text_effect;
    window._showCliSessions=s.show_cli_sessions!==false;
    window._showPreviousMessagingSessions=!!s.show_previous_messaging_sessions;
    window._soundEnabled=!!s.sound_enabled;
    window._notificationsEnabled=!!s.notifications_enabled;
    window._whatsNewSummaryEnabled=!!s.whats_new_summary_enabled;
    window._showThinking=s.show_thinking!==false;
    window._simplifiedToolCalling=true;
    window._chatActivityDisplayMode=s.chat_activity_display_mode==='transparent_stream'||s.chat_activity_display_mode==='hide_all_activity'
      ? s.chat_activity_display_mode
      : 'compact_worklog';
    window._transparentStream=window._chatActivityDisplayMode==='transparent_stream';
    window._transparentEventTimestamps=s.transparent_stream_event_timestamps!==false;
    window._terminalAutoExpandOnOutput=!!s.terminal_auto_expand_on_output;
    window._worklogDetailsExpandedByDefault=!!(
      Object.prototype.hasOwnProperty.call(s,'worklog_details_expanded_default')
        ? s.worklog_details_expanded_default
        : s.activity_feed_expanded_default
    );
    window._workspaceTodosTab=!!s.workspace_todos_tab;
    if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
    window._sidebarDensity=(s.sidebar_density==='detailed'?'detailed':'compact');
    window._pinnedSessionsLimit=parseInt(s.pinned_sessions_limit||3,10)||3;
    window._inflightStateLimits={
      maxSessions:parseInt(s.inflight_state_max_sessions||8,10)||8,
      messages:parseInt(s.inflight_state_max_messages||24,10)||24,
      toolCalls:parseInt(s.inflight_state_max_tool_calls||48,10)||48,
      stringChars:parseInt(s.inflight_state_max_string_chars||60000,10)||60000,
      jsonChars:parseInt(s.inflight_state_max_json_chars||1500000,10)||1500000,
    };
    // #5162 rename + steer default, layered on the #5170 localStorage mirror:
    // resolve the mode (new key, legacy busy_input_mode fallback, else 'steer'
    // via _normalizeDefaultMessageMode) and persist it so the very first send
    // after a reload honors the saved choice.
    window._defaultMessageMode=_persistDefaultMessageMode(s.default_message_mode||s.busy_input_mode);
    window._showBusyPlaceholderHint=!!s.show_busy_placeholder_hint;
    window._newChatOnWorkspaceSwitch=!!s.new_chat_on_workspace_switch;  // #5473 opt-in
    window._sessionEndlessScrollEnabled=!!s.session_endless_scroll;
    window._autoScrollFollow=s.auto_scroll_follow!==false;
    window._largeTextPasteAsAttachment=s.large_text_paste_as_attachment!==false;
    window._projectQuickCreate=!!s.project_quick_create_buttons;
    window._composerControlVisibility=_composerControlVisibilityFromSettings(s);
    window._composerControlOrder=_sanitizeComposerControlOrder(s.composer_control_order);
    _applyComposerControlOrder(window._composerControlOrder);
    window._showTitlebarProfile=!!s.show_titlebar_profile;
    _applyTitlebarProfileVisibility();
    window._botName=s.bot_name||'Hermes';
    if(s.default_model_provider) window._activeProvider=s.default_model_provider;
    if(s.default_model){
      window._defaultModel=s.default_model;
      const sel=$('modelSelect');
      if(sel&&typeof _applyModelToDropdown==='function'){
        // Fresh page boot must prefer the profile/server default over stale
        // browser-persisted model state. A restored session can still apply its
        // own persisted model later through loadSession(). Preserve the browser
        // keys for legacy/no-default fallback paths instead of deleting them.
        const existingDefaultOpt=Array.from(sel.options).find(o=>o.value===s.default_model);
        if(existingDefaultOpt&&window._activeProvider&&!existingDefaultOpt.dataset.provider){
          existingDefaultOpt.dataset.provider=window._activeProvider;
        }
        if(!existingDefaultOpt){
          const opt=document.createElement('option');
          opt.value=s.default_model;
          opt.textContent=typeof getModelLabel==='function'?getModelLabel(s.default_model):s.default_model;
          opt.dataset.custom='1';
          opt.dataset.provider=window._activeProvider||'';
          sel.querySelectorAll('option[data-custom]').forEach(o=>o.remove());
          sel.appendChild(opt);
        }
        _applyModelToDropdown(s.default_model,sel,window._activeProvider||null);
      }
    }
    window._sessionJumpButtonsEnabled=!!s.session_jump_buttons;
    window._renderUserMarkdown=!!s.render_user_markdown;
    // JSON/YAML structured code-block default view (#484): auto | on | off,
    // plus the 'auto'-mode line threshold (sanitized int 1..1000, fallback 10).
    window._structuredCodeDefaultView=['on','off','auto'].includes(s.structured_code_default_view)?s.structured_code_default_view:'auto';
    const _sctLines=parseInt(s.structured_code_auto_tree_lines,10);
    window._structuredCodeAutoTreeLines=(Number.isFinite(_sctLines)&&_sctLines>=1&&_sctLines<=1000)?_sctLines:10;
    // Reconcile appearance: prefer localStorage (what the user last saw) over
    // the server.  If they diverge (e.g. a previous autosave POST failed),
    // push the localStorage values back to the server so settings.json stays
    // in sync without ever clobbering the user's chosen theme/skin.
    //
    // Caveat: the pre-paint inline script in index.html normalises empty
    // localStorage into 'dark'/'default' BEFORE this code runs, so a truly
    // empty (new-browser) state is indistinguishable from a user who chose
    // the defaults.  To avoid blocking server→client sync on first visit we
    // only let localStorage override the server when it carries an explicit
    // user-selectable theme value or a NON-DEFAULT skin.  That keeps the
    // server in charge for empty first-visit state while preserving explicit
    // light/dark/system choices after a failed autosave.
    const srvAppearance=_normalizeAppearance(s.theme,s.skin);
    const lsTheme=(localStorage.getItem('hermes-theme')||'').trim().toLowerCase();
    const lsSkin=(localStorage.getItem('hermes-skin')||'').trim().toLowerCase();
    const lsAppearance=_normalizeAppearance(lsTheme||null,lsSkin||null);
    // An unknown non-default persisted skin is most likely an extension-provided
    // skin (registerHermesSkin) whose extension script hasn't registered it yet
    // at this point in boot. Preserve it verbatim instead of normalizing it away
    // to 'default' — the extension's registerHermesSkin() will inject the CSS and
    // re-apply it once it loads. Without this, the boot sync would clobber the
    // saved choice before the extension runs.
    const lsSkinIsPendingExt=!!lsSkin&&lsSkin!=='default'&&!_VALID_SKINS.has(lsSkin)&&!_LEGACY_THEME_MAP[lsSkin];
    const lsHasExplicitSkin=lsSkin&&lsSkin!=='default';
    const lsHasExplicitTheme=lsTheme&&['system','light','dark'].includes(lsTheme);
    const theme=lsHasExplicitTheme?lsAppearance.theme:srvAppearance.theme;
    const skin=lsHasExplicitSkin?(lsSkinIsPendingExt?lsSkin:lsAppearance.skin):srvAppearance.skin;
    localStorage.setItem('hermes-theme',theme);
    _applyTheme(theme);
    localStorage.setItem('hermes-skin',skin);
    _applySkin(skin);
    // Reconcile: if localStorage and server disagree, push localStorage
    // values to the server so the next refresh won't revert. Skip the push for a
    // still-pending extension skin (don't persist it server-side until it's a
    // confirmed-registered skin — avoids writing a skin the server can't validate).
    if((lsHasExplicitTheme||lsHasExplicitSkin)&&!lsSkinIsPendingExt&&(theme!==srvAppearance.theme||skin!==srvAppearance.skin)){
      try{
        api('/api/settings',{method:'POST',body:JSON.stringify({theme,skin})});
      }catch(_){}
    }
    const fontSize=(s.font_size||localStorage.getItem('hermes-font-size')||'default');
    localStorage.setItem('hermes-font-size',fontSize);
    _applyFontSize(fontSize);
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(s.language, localStorage.getItem('hermes-lang'))
        : (s.language || localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    _mirrorSpeechSettingsFromServer(s);
    // Apply voice-mode visibility BEFORE computing the divider so the
    // .composer-divider (#5451) sees #btnVoiceMode final display even
    // when a server/localStorage sync path flipped the pref between
    // module init and settings-load completion (round-2 SILENT race).
    // Note: must use window._applyVoiceModePref — the bare name is
    // closure-local to the voice-mode IIFE and not visible here.
    if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
    _applyComposerFooterVisibilitySettings();
    // TTS: apply enabled state on boot so buttons show/hide correctly (#499)
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }catch(e){
    window._sendKey='enter';
    window._showTokenUsage=false;
    window._showQuotaChip=false;
    window._showConversationOutline=false;
    document.documentElement.dataset.conversationOutline='disabled';
    if(typeof applyConversationOutlinePreference==='function') applyConversationOutlinePreference();
    window._hideEmptyStateSuggestions=false;
    applyEmptyStateSuggestionPref();
    window._virtualizeTranscript=false;  // settings-load failed: default-OFF (experimental/opt-in) (#4343)
    window._showTps=false;
    window._fadeTextEffect=false;
    window._showCliSessions=true;  // settings-load failed: mirror the True config default (#3988)
    window._soundEnabled=false;
    window._notificationsEnabled=false;
    window._whatsNewSummaryEnabled=false;
    window._showThinking=true;
    window._simplifiedToolCalling=true;
    window._chatActivityDisplayMode='compact_worklog';
    window._transparentStream=false;
    window._transparentEventTimestamps=true;
    window._terminalAutoExpandOnOutput=false;
    window._workspaceTodosTab=false;
    if(typeof _applyWorkspaceTodosTabVisibility==='function') _applyWorkspaceTodosTabVisibility();
    window._sessionJumpButtonsEnabled=false;
    window._structuredCodeDefaultView='auto';
    window._structuredCodeAutoTreeLines=10;
    window._sidebarDensity='compact';
    window._pinnedSessionsLimit=3;
    // Settings load failed: keep the persisted default-message-mode preference
    // (the eager default already read it from the localStorage mirror) instead
    // of clobbering it, so a saved 'steer'/'interrupt'/'queue' still applies
    // when the server is unreachable (#5167). The placeholder-hint has no
    // persisted mirror, so it defaults off on failure.
    window._defaultMessageMode=_readPersistedDefaultMessageMode();
    window._showBusyPlaceholderHint=false;
    window._sessionEndlessScrollEnabled=false;
    window._autoScrollFollow=true;
    window._composerControlVisibility=_composerControlVisibilityFromSettings(null);
    window._composerControlOrder=[];
    _applyComposerControlOrder(window._composerControlOrder);
    window._botName='Hermes';
    _bootSettings={check_for_updates:false};
    if(typeof setLocale==='function'){
      const _lang=typeof resolvePreferredLocale==='function'
        ? resolvePreferredLocale(null, localStorage.getItem('hermes-lang'))
        : (localStorage.getItem('hermes-lang') || 'en');
      setLocale(_lang);
      if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();
    }
    // Apply voice-mode visibility BEFORE computing the divider so the
    // .composer-divider (#5451) sees #btnVoiceMode final display even when
    // a server/localStorage sync path flipped the pref between module init
    // and settings-load completion (round-2 SILENT race fix; safe no-op on
    // the failure-fallback path because _applyVoiceModePref is idempotent).
    // Note: must use window._applyVoiceModePref — the bare name is
    // closure-local to the voice-mode IIFE and not visible here.
    if(typeof window._applyVoiceModePref==='function') window._applyVoiceModePref();
    _applyComposerFooterVisibilitySettings();
    if(typeof _applyTtsEnabled==='function') _applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true');
  }
  // Non-blocking update check (fire-and-forget, once per tab session)
  // ?test_updates=1 in URL forces banner display for testing (bypasses sessionStorage guards)
  const _testUpdates=new URLSearchParams(location.search).get('test_updates')==='1';
  if(_testUpdates||(_bootSettings.check_for_updates!==false&&!sessionStorage.getItem('hermes-update-checked')&&!sessionStorage.getItem('hermes-update-dismissed'))){
    const _checkUrl='api/updates/check'+(_testUpdates?'?simulate=1':'');
    api(_checkUrl,{method:_testUpdates?'GET':'POST',body:_testUpdates?undefined:JSON.stringify({force:false})}).then(d=>{if(!_testUpdates)sessionStorage.setItem('hermes-update-checked','1');if((d.webui&&d.webui.behind>0)||(d.agent&&d.agent.behind>0))_showUpdateBanner(d);}).catch(()=>{});
  }
  const _bootActiveProfileUnauthRedirectBudget=(()=>{
    const markerKey='hermes-webui-active-profile-bootstrap-401';
    let consumed=false;
    const readAttempted=(storage=sessionStorage)=>{
      try{
        const attempted=storage&&storage.getItem?storage.getItem(markerKey)==='1':false;
        if(attempted) consumed=true;
        return attempted;
      }catch(_){
        return false;
      }
    };
    const markAttempted=(storage=sessionStorage)=>{
      consumed=true;
      try{
        if(storage&&storage.setItem) storage.setItem(markerKey,'1');
      }catch(_){}
    };
    const clearAttempted=(storage=sessionStorage)=>{
      try{
        if(storage&&storage.removeItem) storage.removeItem(markerKey);
      }catch(_){}
    };
    const spendOnFallback=(storage=sessionStorage)=>{
      consumed=true;
      clearAttempted(storage);
    };
    const spendOnRedirect=(storage=sessionStorage)=>{
      if(consumed) return false;
      markAttempted(storage);
      return true;
    };
    const redirectToLogin=(nextUrl)=>{
      // #5578: never nest the login URL into its own next= — if already on a
      // login-shaped page, reload 'login' bare (the page keeps its inner next).
      const _p=(window.location.pathname||'').replace(/\/+$/,'');
      if(/(?:^|\/)login$/.test(_p)){window.location.href='login';return;}
      window.location.href='login?next='+encodeURIComponent(nextUrl);
    };
    return {
      readAttempted,
      clearAttempted,
      spendOnFallback,
      spendOnRedirect,
      redirectToLogin,
      isConsumed:()=>consumed,
    };
  })();
  async function _resolveActiveProfileBootstrapState({
    loadActiveProfile = () => api('/api/profile/active', {redirect401: false}),
    getNextUrl = () => window.location.pathname + window.location.search,
    redirectToLogin = (nextUrl) => {
      _bootActiveProfileUnauthRedirectBudget.redirectToLogin(nextUrl);
    },
    markerStorage = sessionStorage,
  } = {}) {
    const alreadyAttempted = _bootActiveProfileUnauthRedirectBudget.readAttempted(markerStorage);
    try {
      const p = await loadActiveProfile();
      if (p && typeof p === 'object' && typeof p.name === 'string') {
        _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
        if (p.default_workspace) S._profileDefaultWorkspace = p.default_workspace;
        return {status: 'resolved', profile: p.name || 'default', isDefault: !!p.is_default};
      }
      if (p === undefined && !alreadyAttempted) {
        if (_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(markerStorage)) {
          redirectToLogin(getNextUrl());
        }
        return {status: 'recovery-redirect'};
      }
      if (p === undefined) _bootActiveProfileUnauthRedirectBudget.spendOnFallback(markerStorage);
      else _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
      return {status: 'fallback', profile: 'default', isDefault: true};
    } catch (e) {
      _bootActiveProfileUnauthRedirectBudget.clearAttempted(markerStorage);
      if (!alreadyAttempted && e && e.status === 401) {
        if (_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(markerStorage)) {
          redirectToLogin(getNextUrl());
        }
        return {status: 'recovery-redirect'};
      }
      if (e && e.status === 401) _bootActiveProfileUnauthRedirectBudget.spendOnFallback(markerStorage);
      return {status: 'fallback', profile: 'default', isDefault: true};
    }
  }

  // Fetch active profile
  const activeProfileState = await _resolveActiveProfileBootstrapState();
  if (activeProfileState.status === 'recovery-redirect') return;
  S.activeProfile = activeProfileState.profile;
  S.activeProfileIsDefault = activeProfileState.isDefault;
  applyBotName();
  // Update profile chip label immediately
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
  const titleLabel=$('titlebarProfileLabel');
  if(titleLabel) titleLabel.textContent=S.activeProfile||'default';
  const profileIntent=(typeof _profileQueryIntentFromLocation==='function')?_profileQueryIntentFromLocation():null;
  const _savedLocalBeforeProfileSwitch=localStorage.getItem('hermes-webui-session');
  const _profileSwitchProfileBefore=S.activeProfile||'default';
  const _profileSwitchIsDefaultBefore=!!S.activeProfileIsDefault;
  let _profileSwitchCompleted=false;
  let _profileSwitchChangedProfile=false;
  if(profileIntent&&profileIntent.hasParam){
    try{
      if(profileIntent.valid){
        if(typeof switchToProfile==='function'){
          _profileSwitchCompleted=await switchToProfile(profileIntent.name)===true;
          if(_profileSwitchCompleted){
            _profileSwitchChangedProfile=(S.activeProfile||'default')!==_profileSwitchProfileBefore||!!S.activeProfileIsDefault!==_profileSwitchIsDefaultBefore;
            if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();
          }
        }
      }else{
        console.warn('[boot] ignored invalid profile query', profileIntent.name);
        if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();
      }
    }catch(e){
      console.warn('[boot] profile query switch failed', e);
    }
  }
  if(typeof fetchReasoningChip==='function'&&(!_profileSwitchCompleted||!_profileSwitchChangedProfile)) fetchReasoningChip();
  // Fetch available models without blocking session restore. The static HTML
  // options enough for first paint; the dynamic provider list can settle
  // after the saved session is visible.
  const _redirectBootModelDropdownIfUnauth=(res)=>{
    if(!res||res.status!==401) return false;
    window._modelDropdownReady=null;
    if(_bootActiveProfileUnauthRedirectBudget.isConsumed()) return true;
    if(_bootActiveProfileUnauthRedirectBudget.spendOnRedirect(sessionStorage)){
      _bootActiveProfileUnauthRedirectBudget.redirectToLogin(window.location.pathname+window.location.search);
    }
    return true;
  };
  const _hydrateModelDropdown=({redirectIfUnauth=null,freshness=null}={})=>populateModelDropdown({
    preferProfileDefaultOnFreshBoot:true,
    ...(redirectIfUnauth?{redirectIfUnauth}:{}),
    ...(freshness?{freshness}:{}),
  }).then(()=>{
    const sessionModelState=S.session&&S.session.model
      ? {model:S.session.model,model_provider:S.session.model_provider||null}
      : null;
    const savedState=(typeof _readPersistedModelState==='function')
      ? _readPersistedModelState()
      : (localStorage.getItem('hermes-webui-model')?{model:localStorage.getItem('hermes-webui-model'),model_provider:null}:null);
    // Active sessions are authoritative. On fresh boot without a restored
    // session, keep the profile/server default ahead of stale browser model
    // state when a default exists.
    const stateToApply=sessionModelState||(!window._defaultModel?savedState:null);
    const savedModel=stateToApply&&stateToApply.model;
    if(savedModel && $('modelSelect')){
      const applied=(typeof _applyModelToDropdown==='function')
        ? (sessionModelState
          ? _applyModelToDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null)
          : _applyModelToDropdown(savedState.model,$('modelSelect'),savedState.model_provider||null))
        : null;
      if(!applied) $('modelSelect').value=stateToApply.model;
      // If the value didn't take (model not in list), clear the bad pref only
      // for persisted browser preferences. Active sessions remain authoritative.
      if(!applied&&sessionModelState&&typeof _ensureModelOptionInDropdown==='function'){
        _ensureModelOptionInDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null);
      }
      else if(!applied&&!sessionModelState&&$('modelSelect').value!==stateToApply.model){
        if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
        else {
          localStorage.removeItem('hermes-webui-model');
          localStorage.removeItem('hermes-webui-model-state');
        }
      }
      else if(typeof syncModelChip==='function') syncModelChip();
    }
    if(S.session) syncTopbar();
    else if(typeof syncReasoningChip==='function') syncReasoningChip();
  });
  let _modelDropdownReadyFreshness=null;
  const _trackModelDropdownReady=(promise,freshness=null)=>{
    const tracked=Promise.resolve(promise).catch(e=>{
      if(window._modelDropdownReady===tracked){
        window._modelDropdownReady=null;
        _modelDropdownReadyFreshness=null;
      }
      throw e;
    });
    window._modelDropdownReady=tracked;
    _modelDropdownReadyFreshness=freshness||null;
    return tracked;
  };
  const _startModelDropdown=(opts={})=>{
    const requestedFreshness=opts&&opts.freshness?opts.freshness:null;
    const ready=window._modelDropdownReady;
    if(ready&&typeof ready.then==='function'){
      if(!requestedFreshness||_modelDropdownReadyFreshness===requestedFreshness) return ready;
      const queued=Promise.resolve(ready).catch(()=>{}).then(()=>_hydrateModelDropdown(opts));
      return _trackModelDropdownReady(queued,requestedFreshness);
    }
    return _trackModelDropdownReady(_hydrateModelDropdown(opts),requestedFreshness);
  };
  const _startBootModelDropdown=()=>{
    const ready=window._modelDropdownReady;
    if(ready&&typeof ready.then==='function') return ready;
    return _trackModelDropdownReady(
      _hydrateModelDropdown({redirectIfUnauth:_redirectBootModelDropdownIfUnauth}),
      null,
    );
  };
  window._modelDropdownReady=null;
  window._startBootModelDropdown=_startBootModelDropdown;
  window._ensureModelDropdownReady=_startModelDropdown;
  setTimeout(()=>{
    try{Promise.resolve(_startBootModelDropdown()).catch(()=>{});}catch(_){}
  },0);
  // Start independent boot fetches without holding the conversation list behind
  // them. The sidebar can render from /api/sessions while workspace/onboarding
  // metadata settles in parallel.
  const _workspaceListReady=loadWorkspaceList();
  const _onboardingReady=_bootSettings.onboarding_completed?Promise.resolve(false):loadOnboardingWizard();
  // Render the session list before restoring the saved conversation so a stale
  // saved-session/client-side boot error cannot leave the sidebar empty forever.
  await renderSessionList();
  await _workspaceListReady;
  await _onboardingReady;
  _initResizePanels();
  // Workspace panel restore happens AFTER loadSession so we know if
  // the session has a workspace — prevents the snap-open-then-closed flash (#576).
  // Fix #822: clear any browser-restored value before first render. This
  // covers fresh page loads and reloads. The bfcache restore case is handled
  // separately below by a `pageshow` listener — the async IIFE here does NOT
  // re-run when the browser restores the page from bfcache.
  const _srch = document.getElementById('sessionSearch'); if (_srch) _srch.value = '';
  if (typeof syncSessionSearchClear === 'function') syncSessionSearchClear();
  if(typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();
  const urlSession=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;
  const pwaLaunchAction=(window.HermesPWA&&typeof window.HermesPWA.launchAction==='function')
    ? window.HermesPWA.launchAction()
    : null;
  if(pwaLaunchAction==='new-chat'){
    try{
      await newSession(true);
      // New-chat PWA launches need the empty conversation visible immediately.
      // Boot model hydration can take several seconds when /api/models falls
      // into a cold provider-catalog rebuild; it is already safe to finish in
      // the background because newSession() posted the configured default and
      // rendered the session's authoritative model/provider.
      if(S.session){
        try{Promise.resolve(_startBootModelDropdown()).catch(()=>{});}catch(_){}
      }
      S._bootReady=true;
      syncTopbar();syncWorkspacePanelState();await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();return;
    }catch(e){console.warn('[pwa] new-chat launch action failed', e);}
  }
  const _profileQueryBlocksSavedLocal=_profileQueryBlocksSavedLocalRestore(profileIntent, urlSession);
  if(_profileQueryBlocksSavedLocal&&_profileSwitchCompleted&&_profileSwitchChangedProfile){
    try{
      if(localStorage.getItem('hermes-webui-session')===_savedLocalBeforeProfileSwitch) localStorage.removeItem('hermes-webui-session');
    }catch(_){}
  }
  const savedLocal=localStorage.getItem('hermes-webui-session');
  const saved=urlSession||savedLocal;
  if(saved){
    try{
      const savedSidebarOnlyState=(!urlSession&&savedLocal)
        ? await _savedSessionSidebarOnlyState(savedLocal)
        : null;
      if(savedSidebarOnlyState&&savedSidebarOnlyState.sidebarOnly){
        if(savedSidebarOnlyState.archived){
          try{localStorage.removeItem('hermes-webui-session');}catch(_){}
        }
        S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false;
        S._bootReady=true;
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      if(_rootPrefillNeedsFreshComposer(urlSession, savedLocal, prefillIntent)){
        S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false;
        S._bootReady=true;
        const _ephPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
          || localStorage.getItem('hermes-webui-workspace-panel')==='open';
        if(_ephPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
        await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      await loadSession(saved, {preserveActiveInput:true});
      // Hard refresh starts from the static HTML model list. Hydrate the live
      // catalog after the saved session is known, then re-apply that session's
      // model before S._bootReady lets syncModelChip reveal the composer label.
      // Otherwise the chip can display the static default (e.g. GPT-5.4 Mini)
      // even though S.session already points at the Codex/current model.
      if(S.session) await _startBootModelDropdown();
      // If the restored session has no messages it is an ephemeral scratch pad —
      // treat the page as a fresh start rather than resuming a blank conversation.
      // loadSession() already ran, so loadDir() has populated the workspace file tree.
      // Do NOT remove the session ID from localStorage — keeping it means every
      // subsequent refresh will also run loadSession() → loadDir() → files stay visible.
      // Removing it here caused the file tree to go blank on the second refresh
      // because the "no saved session" path never calls loadDir (#workspace-files).
      const _restoredInFlight = S.session && (
        S.session.active_stream_id ||
        S.session.pending_user_message
      );
      const _restoredDraft = (S.session && S.session.composer_draft) || {};
      const _restoredDraftText = String(_restoredDraft.text||'').trim();
      const _restoredDraftFiles = Array.isArray(_restoredDraft.files)
        ? _restoredDraft.files.filter(Boolean)
        : [];
      const _restoredHasDraft = !!(_restoredDraftText || _restoredDraftFiles.length);
      if(S.session && (S.session.message_count||0) === 0 && !_restoredInFlight && !_restoredHasDraft){
        S.session=null; S.messages=[];
        S._bootReady=true;
        // Restore panel pref before syncing so the workspace panel stays visible
        // even though there is no active session (#workspace-persist).
        const _ephPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
          || localStorage.getItem('hermes-webui-workspace-panel')==='open';
        if(_ephPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
        await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
        syncTopbar();syncWorkspacePanelState();
        $('emptyState').style.display='';
        await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);if(typeof startGatewaySSE==='function')startGatewaySSE();
        return;
      }
      // Restore the panel from localStorage when the session has a workspace.
      // Preference key takes priority over runtime state so that closing
      // the panel via toolbar X doesn't suppress the "keep open" setting.
      const panelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
        || localStorage.getItem('hermes-webui-workspace-panel')==='open';
      if(S.session&&S.session.workspace&&panelPref&&!_isCompactWorkspaceViewport()){
        _workspacePanelMode='browse';
      }
      S._bootReady=true;
      syncTopbar();syncWorkspacePanelState();await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();await checkInflightOnBoot(saved);await _finalizeComposerPrefillOnBoot(prefillIntent);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  S._bootReady=true;
  syncTopbar();
  // Restore panel pref so the workspace panel stays visible on a fresh load if the
  // user had it open during their last session (#workspace-persist).
  const _freshPanelPref=localStorage.getItem('hermes-webui-workspace-panel-pref')==='open'
    || localStorage.getItem('hermes-webui-workspace-panel')==='open';
  if(_freshPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';
  await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);
  syncWorkspacePanelState();
  $('emptyState').style.display='';
  await renderSessionList();await _finalizeComposerPrefillOnBoot(prefillIntent);
  // Start real-time gateway session sync if setting is enabled
  if(typeof startGatewaySSE==='function') startGatewaySSE();
})().catch(e=>{
  console.error('[hermes] boot failed', e);
  try{S._bootReady=true;}catch(_){}
  try{syncTopbar();}catch(_){}
  try{syncWorkspacePanelState();}catch(_){}
  try{$('emptyState').style.display='';}catch(_){}
  try{if(typeof renderSessionList==='function') void renderSessionList();}catch(_){}
});

// Fix #822 (bfcache path): when the browser restores the page from the
// back-forward cache, the async boot IIFE above does NOT re-run, but the
// DOM — including any stale value in #sessionSearch — IS restored.  A
// prior search string would silently hide all sessions via the filter in
// renderSessionListFromCache().  Clear the field and re-run the full layout
// sync whenever the page is restored from cache (`event.persisted === true`).
// Fix #1045: also re-run topbar/workspace/panel state so the rail and layout
// chrome aren't left in the stale bfcache snapshot.
window.addEventListener('pageshow', async (event) => {
  if (!event.persisted) return;  // fresh loads are handled by the IIFE above
  _syncKeyboardBottomInset();
  const _srch = document.getElementById('sessionSearch');
  if (_srch) _srch.value = '';
  if (typeof syncSessionSearchClear === 'function') syncSessionSearchClear();
  // Close any dropdowns/popovers that were open when the user navigated away.
  // bfcache freezes DOM state, so a dropdown left open remains open on restore.
  if (typeof closeModelDropdown === 'function') try { closeModelDropdown(); } catch (_) {}
  if (typeof closeReasoningDropdown === 'function') try { closeReasoningDropdown(); } catch (_) {}
  if (typeof closeWsDropdown === 'function') try { closeWsDropdown(); } catch (_) {}
  if (typeof closeProfileDropdown === 'function') try { closeProfileDropdown(); } catch (_) {}
  // BFCache restores the frozen DOM without rerunning boot. Refresh the active
  // session through the normal load path so in-flight sessions with
  // active_stream_id / pending_user_message can reattach like a reload restore.
  if (S.session && S.session.session_id && typeof loadSession === 'function') {
    try {
      await loadSession(S.session.session_id);
      if (S.session && S.session.session_id && typeof checkInflightOnBoot === 'function') {
        try { await checkInflightOnBoot(S.session.session_id); } catch (_) {}
      }
    } catch (_) {}
  }
  // Re-synchronise layout chrome that the boot IIFE sets up but bfcache
  // doesn't re-run. Each call is guarded so missing helpers degrade silently.
  if (typeof syncTopbar === 'function') try { syncTopbar(); } catch (_) {}
  if (typeof syncWorkspacePanelState === 'function') try { syncWorkspacePanelState(); } catch (_) {}
  if (typeof renderSessionListFromCache === 'function') {
    try { renderSessionListFromCache(); } catch (_) {}
  }
  // Restart the gateway SSE watcher — the persisted connection is dead after bfcache
  if (typeof startGatewaySSE === 'function') try { startGatewaySSE(); } catch (_) {}
  // Re-sync sidebar collapse state from localStorage. bfcache restored the
  // frozen DOM but another tab may have toggled the sidebar in the meantime.
  if (typeof _isSidebarCollapsed === 'function' && typeof toggleSidebar === 'function') {
    try {
      const _want = localStorage.getItem('hermes-webui-sidebar-collapsed') === '1';
      const _have = _isSidebarCollapsed();
      if (_want !== _have) toggleSidebar(_want);
      if (typeof _syncSidebarAria === 'function') _syncSidebarAria();
    } catch (_) {}
  }
});

async function shutdownServer() {
  const ok = await showConfirmDialog({
    title: (typeof t === 'function' ? t('settings_shutdown_confirm_title') : 'Stop Hermes WebUI'),
    message: (typeof t === 'function' ? t('settings_shutdown_confirm_message') : 'Stop the Hermes WebUI server?'),
    confirmLabel: (typeof t === 'function' ? t('settings_shutdown_confirm_btn') : 'Stop'),
    danger: true,
  });
  if (!ok) return;
  localStorage.setItem('hermes-webui-server-stopped', '1');
  try { var bc = new BroadcastChannel('hermes-webui-shutdown'); bc.postMessage('stop'); bc.close(); } catch(_) {}
  _showServerStopped();
  try { await api('/api/shutdown', { method: 'POST' }); } catch (_) {}
}

function _showServerStopped() {
  var stoppedMsg = (typeof t === 'function' ? t('settings_shutdown_stopped_message') : 'Server stopped. You can close this tab.');
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-family:system-ui,ui-sans-serif;font-size:14px"><p>' + stoppedMsg + '</p></div>';
}
window.HermesBoot.publish('bootstrapCoordinator',{shutdownServer,showServerStopped:_showServerStopped});
window.HermesBoot.assertComplete();
