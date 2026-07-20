import { _rememberNewChatDraftSession } from './composer-drafts.js';
import { _setSessionViewedCount } from './session-unread.js';
import { _deferWorkspaceRefreshForSession } from './session-post-load.js';
import { NO_PROJECT_FILTER, sidebarStateBindings } from './sidebar-store.js';
import { _setActiveSessionUrl } from './session-navigation.js';

let _newSessionInFlight=null;
const _newSessionPendingText=()=>t('new_session_creating')||'Creating new conversation…';
const _emptyComposerModelOverrideHost=typeof window!=='undefined'?window:globalThis;

function _rememberEmptyComposerModelOverride(model, modelProvider){
  const resolvedModel=String(model||'').trim();
  if(!resolvedModel) return;
  _emptyComposerModelOverrideHost._emptyComposerModelOverride={
    model:resolvedModel,
    model_provider:modelProvider||null,
    saved_at:Date.now(),
  };
}

function _readEmptyComposerModelOverride(){
  const state=_emptyComposerModelOverrideHost._emptyComposerModelOverride;
  if(!state||!state.model) return null;
  return {
    model:String(state.model||''),
    model_provider:state.model_provider||null,
    saved_at:Number(state.saved_at||0)||0,
  };
}

function _clearEmptyComposerModelOverride(){
  _emptyComposerModelOverrideHost._emptyComposerModelOverride=null;
}

let _newSessionWorkspaceAnnouncementClearTimer=null;

function _setNewSessionWorkspaceCue(message){
  const announcer=$('a11yAnnouncer');
  const composerCue=$('composerWorkspaceContext');
  const msg=$('msg');
  const cueId='composerWorkspaceContext';
  if(_newSessionWorkspaceAnnouncementClearTimer&&typeof clearTimeout==='function'){
    clearTimeout(_newSessionWorkspaceAnnouncementClearTimer);
    _newSessionWorkspaceAnnouncementClearTimer=null;
  }
  const removeComposerCue=()=>{
    if(composerCue&&composerCue.textContent===message) composerCue.textContent='';
    if(msg){
      const ids=(msg.getAttribute('aria-describedby')||'')
        .split(/\s+/)
        .filter(Boolean)
        .filter(id=>id!==cueId);
      if(ids.length) msg.setAttribute('aria-describedby',ids.join(' '));
      else msg.removeAttribute('aria-describedby');
    }
  };
  const clear=()=>{
    if(announcer&&announcer.textContent===message) announcer.textContent='';
    removeComposerCue();
    _newSessionWorkspaceAnnouncementClearTimer=null;
  };
  const announce=()=>{
    if(announcer) announcer.textContent=message;
    if(composerCue&&msg){
      composerCue.textContent=message;
      const ids=(msg.getAttribute('aria-describedby')||'')
        .split(/\s+/)
        .filter(Boolean)
        .filter(id=>id!==cueId);
      ids.push(cueId);
      msg.setAttribute('aria-describedby',ids.join(' '));
    }
    if(typeof setTimeout==='function'){
      _newSessionWorkspaceAnnouncementClearTimer=setTimeout(clear,5000);
    }
  };
  if(announcer) announcer.textContent='';
  removeComposerCue();
  if(typeof requestAnimationFrame==='function') requestAnimationFrame(announce);
  else announce();
}

function _announceNewSessionWorkspace(session){
  if(!session||!session.workspace) return;
  const name=(typeof getWorkspaceFriendlyName==='function')
    ? getWorkspaceFriendlyName(session.workspace)
    : String(session.workspace).split('/').filter(Boolean).pop()||session.workspace;
  _setNewSessionWorkspaceCue(t('new_session_workspace_announce',name));
}

function _setNewSessionPending(pending){
  const ids=['btnNewChat','btnTitlebarNewChat'];
  for (let i=0;i<ids.length;i++){
    const btn=$(ids[i]);
    if(!btn) continue;
    btn.disabled=!!pending;
    btn.setAttribute('aria-busy',pending?'true':'false');
  }
  const statusEl=$('composerStatus');
  const pendingText=_newSessionPendingText();
  if(pending){
    setComposerStatus(pendingText);
  }else if(statusEl&&statusEl.textContent===pendingText){
    setComposerStatus('');
  }
}

async function newSession(flash, options={}){
  if(_newSessionInFlight){
    if(typeof showToast==='function') showToast(_newSessionPendingText(),1500);
    return _newSessionInFlight;
  }
  _setNewSessionPending(true);
  _newSessionInFlight=(async()=>{
    // Starting a brand-new chat must not carry named context blocks selected in
    // the previous conversation (#2543). loadSession() clears these on a sidebar
    // switch, but the New Chat path replaces S.session here without going through
    // loadSession(), so clear them explicitly before the session is replaced.
    if(typeof window._clearPendingSelections==='function') window._clearPendingSelections();
    updateQueueBadge();
    S.toolCalls=[];
    messageLoadingBindings._messagesTruncated=false;
    messageTimelineBindings._oldestIdx=0;
    clearLiveToolCards();
    // One-shot profile-switch workspace wins first; otherwise prefer the profile default.
    const switchWs=S._profileSwitchWorkspace;
    S._profileSwitchWorkspace=null;
    const inheritWs=switchWs||(S._profileDefaultWorkspace||null)||(S.session?S.session.workspace:null);
    const reqBody={
      workspace:inheritWs,
      profile:S.activeProfile||'default',
    };
    if(S.session&&S.session.session_id) reqBody.prev_session_id=S.session.session_id;
    // Three-value worktree contract (#6022): explicit true/false is forwarded
    // verbatim; an ABSENT key lets the server apply the agent's config-level
    // `worktree:` default. Auto-bind paths pass worktree:false explicitly so a
    // config default can never mint a worktree (+ branch) on mere page load.
    if(options&&Object.prototype.hasOwnProperty.call(options,'worktree')) reqBody.worktree=!!options.worktree;
    if(Object.prototype.hasOwnProperty.call(options,'project_id')){
      reqBody.project_id=options.project_id;
    } else if(sidebarStateBindings._activeProject&&sidebarStateBindings._activeProject!==NO_PROJECT_FILTER){
      reqBody.project_id=sidebarStateBindings._activeProject;
    }
    // Forward a pre-session toolset override only from the empty composer (#4490).
    if(!S.session && Array.isArray(S._pendingSessionToolsets)) reqBody.enabled_toolsets=S._pendingSessionToolsets;
    const modelSelForNew=$('modelSelect');
    const explicitModelOverride=(typeof _readEmptyComposerModelOverride==='function')
      ? _readEmptyComposerModelOverride()
      : null;
    const hasLoadedSession=!!(S.session&&S.session.session_id);
    let newModelState=null;
    let consumedExplicitModelOverride=false;
    let usingConfiguredDefault=false;
    if(!hasLoadedSession&&explicitModelOverride&&explicitModelOverride.model){
      newModelState=explicitModelOverride;
      consumedExplicitModelOverride=true;
    }else if(window._defaultModel){
      // Configured default wins over stale picker/persisted state even with no
      // loaded session (deleting the last session left S.session null + stale picker) (#4728).
      newModelState={model:window._defaultModel,model_provider:null};
      usingConfiguredDefault=true;
    }else if(modelSelForNew&&modelSelForNew.value&&typeof _modelStateForSelect==='function'){
      newModelState=_modelStateForSelect(modelSelForNew,modelSelForNew.value);
    }else if(typeof _readPersistedModelState==='function'){
      newModelState=_readPersistedModelState();
    }
    if(newModelState&&newModelState.model){
      reqBody.model=newModelState.model;
      // Cold-start / picker-without-provider fallback: when the dropdown option's
      // data-provider is empty/'default' or the persisted state predates provider
      // tracking, newModelState.model_provider is null. POST /api/session/new's
      // fast path in _resolve_compatible_session_model_state requires both model
      // and a truthy model_provider; without it, the request falls into
      // get_available_models() and a 3-4s cold catalog rebuild. window._activeProvider
      // is hydrated at boot (ui.js) and on config refresh (panels.js), so it's a
      // safe default that matches the user's configured route. S.session.model_provider
      // is the previous-session fallback when the dropdown is unhydrated.
      //
      // Guard: a slash-qualified model (e.g. "gemini/gemini-2.5") or an
      // @provider:model string already carries a foreign provider namespace from
      // a previous session that was served by a different backend. Attaching
      // the current _activeProvider to such a slug would let the server's fast
      // path pass it through without consulting the catalog, silently
      // re-pointing the new session at the wrong backend (the very case the
      // slow-path normalization in _resolve_compatible_session_model_state is
      // designed to fix — see routes.py docstring around line 1891-1894). For
      // those models we leave the wire shape with model_provider=null so the
      // slow path's cross-provider repair still runs. Closes the open
      // follow-up from #2518.
      const _bareModel=!/[/]/.test(newModelState.model)&&!newModelState.model.startsWith('@');
      // Second guard (#3410-followup): even a bare model can carry a known
      // family prefix (gpt→openai, claude→anthropic, gemini→google). If that
      // family maps to a DIFFERENT provider than the fallback we'd attach, the
      // server fast path passes the pair through verbatim (no validation) and
      // silently routes to the wrong backend — so leave model_provider=null and
      // let the slow-path family repair run (mirrors routes.py _normalize_provider_id).
      const _fallbackProvider=_bareModel
        ? ((usingConfiguredDefault?window._activeProvider:(window._activeProvider||(S.session&&S.session.model_provider)))||'')
        : '';
      const _familyProvider=(m=>{const s=String(m||'').toLowerCase();
        if(s.startsWith('gpt'))return 'openai';if(s.startsWith('claude'))return 'anthropic';
        if(s.startsWith('gemini'))return 'google';return '';})(newModelState.model);
      const _normProv=p=>{const s=String(p||'').toLowerCase();
        if(s.startsWith('openai'))return 'openai';if(s.startsWith('anthropic')||s.startsWith('claude'))return 'anthropic';
        if(s.startsWith('google')||s.startsWith('gemini'))return 'google';return s;};
      const _familyMismatch=_familyProvider&&_fallbackProvider&&_normProv(_fallbackProvider)!==_familyProvider;
      const _fallbackIsNamedCustom=String(_fallbackProvider||'').toLowerCase().startsWith('custom:');
      reqBody.model_provider=newModelState.model_provider
        ||((_bareModel&&!_familyMismatch&&!_fallbackIsNamedCustom)?(_fallbackProvider||null):null)
        ||null;
    }
    const data=await api('/api/session/new',{method:'POST',body:JSON.stringify(reqBody)});
    if(consumedExplicitModelOverride&&typeof _clearEmptyComposerModelOverride==='function'){
      _clearEmptyComposerModelOverride();
    }
    S.session=data.session;S.messages=data.session.messages||[];
    S._pendingSessionToolsets=null;
    if(sidebarStateBindings._sessionSourceFilter==='cli') sidebarStateBindings._sessionSourceFilter='webui';
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
    S.lastUsage={...(data.session.last_usage||{})};
    if(!(options&&options.worktree)) _rememberNewChatDraftSession(S.session);
    if(flash)S.session._flash=true;
    try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
    _setActiveSessionUrl(S.session.session_id);
    if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);
    _setSessionViewedCount(S.session.session_id, S.session.message_count || 0);
    // Sync chat-header dropdown to the session's model/provider so the UI reflects
    // the default route the server actually used (#872). Compare provider state too:
    // duplicate model ids can exist under several providers, and a stale persisted
    // picker selection with the same model id should not mask the new session's
    // configured default provider.
    const modelSel=$('modelSelect');
    if(S.session.model && modelSel && typeof _applyModelToDropdown==='function'){
      const currentModelState=(typeof _modelStateForSelect==='function')
        ? _modelStateForSelect(modelSel,modelSel.value)
        : {model:modelSel.value,model_provider:null};
      const sessionProvider=S.session.model_provider||null;
      const currentProvider=currentModelState.model_provider||null;
      if(S.session.model!==modelSel.value || sessionProvider !== currentProvider){
        let sessionModelApplied=_applyModelToDropdown(S.session.model,modelSel,sessionProvider);
        if(!sessionModelApplied){
          const opt=document.createElement('option');
          opt.value=S.session.model;
          opt.textContent=typeof getModelLabel==='function'?getModelLabel(S.session.model):S.session.model;
          opt.dataset.custom='1';
          opt.dataset.provider=sessionProvider||'';
          modelSel.appendChild(opt);
          sessionModelApplied=_applyModelToDropdown(S.session.model,modelSel,sessionProvider);
        }
        if(sessionModelApplied&&typeof syncModelChip==='function') syncModelChip();
      }
    }
    // Reset per-session visual state: a fresh chat is idle even if another
    // conversation is still streaming in the background.
    S.busy=false;
    S.activeStreamId=null;
    updateSendBtn();
    setStatus('');
    setComposerStatus('');
    if(typeof _setLiveAssistantTps==='function') _setLiveAssistantTps(null);
    if(typeof _syncCtxIndicator==='function'){
      _syncCtxIndicator({
        input_tokens:data.session.input_tokens||0,
        output_tokens:data.session.output_tokens||0,
        estimated_cost:data.session.estimated_cost||0,
        cache_read_tokens:data.session.cache_read_tokens||0,
        cache_write_tokens:data.session.cache_write_tokens||0,
        cache_hit_percent:data.session.cache_hit_percent,
        context_length:data.session.context_length||0,
        last_prompt_tokens:data.session.last_prompt_tokens||0,
        post_compression_context_tokens_estimate:data.session.post_compression_context_tokens_estimate||null,
        threshold_tokens:data.session.threshold_tokens||0,
      });
    }
    updateQueueBadge(S.session.session_id);
    syncTopbar();renderMessages();
    if(typeof _announceNewSessionWorkspace==='function') _announceNewSessionWorkspace(S.session);
    // Keep new-chat first paint instant. The workspace tree / git badge can
    // refresh right after paint unless this caller explicitly needs it loaded
    // before continuing (profile/default-workspace binding path).
    if(options&&options.awaitWorkspaceLoad){
      await loadDir('.');
    }else if(typeof _deferWorkspaceRefreshForSession==='function'){
      _deferWorkspaceRefreshForSession(S.session.session_id);
    }else{
      const _dirP=loadDir('.');
      if(_dirP&&typeof _dirP.catch==='function') _dirP.catch(()=>{});
    }
    // Refresh sidebar to include the newly created session (#3874).
    if(typeof refreshSessionList==='function'){Promise.resolve(refreshSessionList('new-session')).catch(()=>{})}
  })();
  try{
    return await _newSessionInFlight;
  }finally{
    _newSessionInFlight=null;
    _setNewSessionPending(false);
  }
}

export { _clearEmptyComposerModelOverride, _newSessionInFlight, _rememberEmptyComposerModelOverride, newSession };
