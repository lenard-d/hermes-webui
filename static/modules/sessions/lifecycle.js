import { _rememberNewChatDraftSession, _restoreComposerDraft, _saveComposerDraftNow } from './composer-drafts.js';
import { _inflightHasVisibleLiveState, _renderRuntimeJournalAnchorActivityScene, _selectLiveRecoveryInflight, _serverLiveSnapshotInflight } from './session-live-recovery.js';
import { sessionLoadState } from './session-load-state.js';
import { _isSessionActivelyViewedForList, _sessionVisitHasUnreadState, _setSessionViewedCount } from './session-unread.js';
import { _acknowledgeSessionVisit } from './session-visit.js';
import { _captureSameSessionForceReloadHint, _checkAndShowHandoffHint, _clearSameSessionForceReloadHint, _deferWorkspaceRefreshForSession, _ensureMessagesLoaded, _hideHandoffHint, _isMessagingSession, _resolveSessionModelForDisplaySoon, messageLoadingBindings } from './message-loading.js';
import { _dropCurrentTurnAssistantMessages, _ensureInflightLiveAssistantMessage, _hasCurrentTailUserDuplicate, _mergeInflightTailMessages, _prepareRunningLiveTail, _projectInflightMessagesForActivityBursts, messageTimelineBindings } from './message-timeline.js';
import { NO_PROJECT_FILTER, sidebarStateBindings } from './sidebar-store.js';
import { _appRootPath, _setActiveSessionUrl } from './session-navigation.js';
import { _invalidateSessionListRenders } from './sidebar-render-state.js';
import { _setProfileSwitchListEmbargo, renderSessionList } from './session-list-loader.js';
import { _clearDeferredActiveSessionExternalRefresh, refreshSessionList } from './session-list-refresh.js';
import { sessionListViewBindings as sessionListBindings, showSessionListSkeleton } from './session-list-skeleton.js';
import { startGatewaySSE } from './sidebar-session-events.js';
import { _resolveSessionIdFromSidebarLineage } from './session-discovery.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

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

/**
 * Self-heal: clear the stuck session ID from localStorage and URL when a
 * loadSession() call failed during boot (no currentSid). This prevents the
 * browser from retrying the same dead session on every refresh.
 *
 * Called from loadSession() after 401 redirect (undefined data) or any
 * non-404 error (400, 403, 500, network). The 404 path has its own
 * inline self-heal; this helper consolidates the non-404 cases.
 *
 * Only clears when !currentSid — no session is active on screen, so
 * the stored ID is definitely stale. When currentSid is set (already
 * viewing a session), a non-404 failure could be a transient server error
 * and the session may still exist on the server; wiping localStorage in
 * that case is unnecessarily destructive (#4028 follow-up).
 *
 * A click into a *different* dead session (currentSid && currentSid!==sid)
 * must not run it: localStorage and the URL still point at the live session
 * (both are only updated on a successful load), so wiping them would log
 * the user out of a healthy session (#2782).
 */
function _clearStuckSessionOnBoot(sid, currentSid){
  if(!currentSid){
    try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
    try{ history.replaceState(null,'',_appRootPath()); }catch(_){ }
  }
}

// #2971 (Greptile P1 r3377162160): loadSession() tears down the live
// per-session SSE at the top via stopSessionStream() (line ~754), but only the
// success path re-arms it via startSessionStream() (line ~875). Every
// early-return exit (fetch error, auth-redirect undefined) — and the
// same-session no-op guard, which returns BEFORE the teardown — could leave
// the session the user actually remains on with a permanently null
// EventSource, silently dropping bg_task_complete delivery until a full page
// reload or a forced loadSession. This helper re-arms the stream for whatever
// session is currently on screen (S.session). startSessionStream() is
// idempotent — it no-ops when already live for that sid (top guard
// `_sessionStreamSessionId === sid && _sessionEventSource`) — so this never
// double-arms the success path, which arms the *newly assigned* S.session
// only after this point.
function _rearmActiveSessionStream(){
  if(typeof startSessionStream!=='function') return;
  const activeSid = S.session ? S.session.session_id : null;
  if(activeSid) startSessionStream(activeSid);
}

function _sessionProfileMismatchFromError(e){
  if(!e || e.status!==409 || !e.body) return null;
  try{
    const body=JSON.parse(e.body);
    if(body && body.code==='session_profile_mismatch' && body.profile){
      return {profile:String(body.profile), session_id:String(body.session_id||'')};
    }
  }catch(_){ }
  return null;
}

async function _switchProfileForSessionLoad(profile){
  const name=String(profile||'').trim();
  if(!name) throw new Error('missing profile');
  if(name===S.activeProfile) return;
  if(typeof _invalidateSessionListRenders==='function') _invalidateSessionListRenders();
  if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(true);
  if(typeof showSessionListSkeleton==='function') showSessionListSkeleton(name);
  try{
    const data=await api('/api/profile/switch',{method:'POST',body:JSON.stringify({name}),timeoutToast:false});
    S.activeProfile=data.active||name;
    S.activeProfileIsDefault=!!data.is_default;
    if(typeof _resetCronUnreadForProfileSwitch==='function'){
      _resetCronUnreadForProfileSwitch();
    }
    if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
    else localStorage.removeItem('hermes-webui-model');
    if(data.default_model) window._defaultModel=data.default_model;
    if(data.default_model_provider) window._activeProvider=data.default_model_provider;
    if(typeof refreshProfileTransitionReasoningChip==='function'){
      refreshProfileTransitionReasoningChip(data.default_model,data.default_model_provider);
    }
    if(typeof startGatewaySSE==='function') startGatewaySSE();
    if(typeof syncTopbar==='function') syncTopbar();
    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);
    if(typeof renderSessionList==='function') await renderSessionList();
  }catch(switchErr){
    // The switch POST failed, so we're still on the previous profile and its
    // caches are intact. Clear the up-front skeleton and re-render the real
    // list so the sidebar doesn't strand on the skeleton (the #4671 strand bug
    // — _sessionListSkeletonActive hard-gates renderSessionListFromCache + the
    // SSE/poll repaints until an unrelated full render fires). Mirror the
    // canonical switch's catch in panels.js, then rethrow so loadSession's
    // catch(switchErr) still routes into the generic error handler.
    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);
    sessionListBindings._sessionListSkeletonActive=false;
    if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
    throw switchErr;
  }
}

async function _restoreLoadedSession(ctx){
  const {sid,_keepStaleUntilLoaded,_loadGeneration,_isCurrentLoad,sameSessionForceReload}=ctx;
  let activeStreamId=ctx.activeStreamId;
  function _mergePendingSessionMessage(session,messages){
    if(!Array.isArray(messages)) return false;
    const pendingMsg=typeof getPendingSessionMessage==='function'?getPendingSessionMessage(session,messages):null;
    if(!pendingMsg) return false;
    const liveAssistantIdx=messages.findIndex(m=>m&&m.role==='assistant'&&m._live);
    const currentTurnMessages=liveAssistantIdx>=0?messages.slice(0,liveAssistantIdx):messages;
    if(_hasCurrentTailUserDuplicate(currentTurnMessages,pendingMsg)) return false;
    if(liveAssistantIdx>=0) messages.splice(liveAssistantIdx,0,pendingMsg);
    else messages.push(pendingMsg);
    return true;
  }

  // Phase 2a: If session is streaming, restore the persisted transcript first,
  // then merge the local INFLIGHT live tail. INFLIGHT is a recovery tail, not a
  // complete transcript; treating it as the full source makes long sessions look
  // like they lost history after switching away and back.
  if(!INFLIGHT[sid]&&activeStreamId&&typeof loadInflightState==='function'){
    const stored=loadInflightState(sid, activeStreamId);
    if(stored){
      INFLIGHT[sid]={
        streamId:String(stored.streamId||''),
        messages:Array.isArray(stored.messages)&&stored.messages.length?stored.messages:[],
        uploaded:Array.isArray(stored.uploaded)?stored.uploaded:[],
        toolCalls:Array.isArray(stored.toolCalls)?stored.toolCalls:[],
        // Phase 2: restore the live todo snapshot from persisted INFLIGHT
        // so the panel does not flicker to empty when a mid-stream
        // browser reload reattaches before the next `todo_state` event
        // fires.  Both fields are optional; missing values fall back to
        // cold-load via session.todo_state.
        todos:Array.isArray(stored.todos)?stored.todos:null,
        todoStateMeta:stored.todoStateMeta||null,
        reattach:true,
        lastAssistantText:String(stored.lastAssistantText||''),
        lastReasoningText:String(stored.lastReasoningText||''),
        lastRunJournalSeq:Number(stored.lastRunJournalSeq||0)||0,
        lastRunJournalEventId:String(stored.lastRunJournalEventId||''),
        journalReplayFromStart:!!stored.journalReplayFromStart,
        anchorActivityScene:(stored.anchorActivityScene&&stored.anchorActivityScene.version==='activity_scene_v1')?stored.anchorActivityScene:null,
        currentActivityBurstId:Number(stored.currentActivityBurstId||0)||0,
        currentLiveSegmentSeq:Number(stored.currentLiveSegmentSeq||0)||0,
        activityBurstAnchors:Array.isArray(stored.activityBurstAnchors)?stored.activityBurstAnchors:[],
      };
    }
  }

  if(INFLIGHT[sid]&&INFLIGHT[sid].journalReplayFromStart&&activeStreamId){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(activeStreamId&&INFLIGHT[sid]&&!_inflightHasVisibleLiveState(INFLIGHT[sid])){
    // A stale cursor-only INFLIGHT entry is worse than no cache: replay would
    // resume after lastRunJournalSeq while the pane has no prose/tool DOM to
    // preserve, making a session switch look like the live turn vanished.
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  const serverLiveSnapshot=activeStreamId
    ? _serverLiveSnapshotInflight(S.session.runtime_journal_snapshot, S.session.pending_attachments||[])
    : null;
  const hadLiveRecoveryInflight=!!INFLIGHT[sid];
  const liveRecoveryInflight=_selectLiveRecoveryInflight(INFLIGHT[sid], serverLiveSnapshot, activeStreamId);
  if(liveRecoveryInflight) INFLIGHT[sid]=liveRecoveryInflight;
  else if(hadLiveRecoveryInflight&&activeStreamId){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }

  if(INFLIGHT[sid]){
    _ensureInflightLiveAssistantMessage(INFLIGHT[sid]);
    const inflightMessages=_projectInflightMessagesForActivityBursts(INFLIGHT[sid]);
    S.toolCalls=[];
    // Switching between active sessions should rebuild the live worklog from
    // this session's INFLIGHT snapshot, not leave prior-session rows in place.
    if(typeof clearLiveToolCards==='function') clearLiveToolCards();
    try {
      await _ensureMessagesLoaded(sid, {force:_keepStaleUntilLoaded, loadGeneration:_loadGeneration});
    } catch(e) {
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      S.messages=inflightMessages;
    }
    if (!_isCurrentLoad()) {
      _rearmActiveSessionStream();
      return;
    }
    const liveTailPrepared=_prepareRunningLiveTail(S.messages,inflightMessages);
    if(liveTailPrepared){
      S.messages=_dropCurrentTurnAssistantMessages(S.messages);
    }
    S.messages=_mergeInflightTailMessages(S.messages,inflightMessages);
    S.toolCalls=(INFLIGHT[sid].toolCalls||[]);
    if(_mergePendingSessionMessage(S.session,S.messages)&&inflightMessages===(INFLIGHT[sid].messages||[])){
      INFLIGHT[sid].messages=S.messages;
    }
    // Refresh todos from cold-load or persisted INFLIGHT before painting.
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
    S.busy=!!activeStreamId;  // #4354: Only assert busy if server confirms active stream.
    // appendLiveToolCard() is guarded by S.activeStreamId; restore it before
    // replaying persisted live tools so the compact Activity count survives
    // switching away from and back to an active chat (#1715).
    S.activeStreamId=activeStreamId;
    const liveToolReplayId=(tc)=>String(tc&&(tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'')||'').trim();
    const replayPersistedLiveToolCards=(opts)=>{
      const liveToolCalls=Array.isArray(S.toolCalls)
        ? S.toolCalls
        : (Array.isArray(INFLIGHT[sid]&&INFLIGHT[sid].toolCalls)?INFLIGHT[sid].toolCalls:[]);
      const skipUnkeyedRestoredDuplicates=!!(opts&&opts.skipUnkeyedRestoredDuplicates);
      const restoredLiveTurn=skipUnkeyedRestoredDuplicates?document.getElementById('liveAssistantTurn'):null;
      const hasRestoredLiveToolRows=!!(restoredLiveTurn&&restoredLiveTurn.querySelector('.tool-card-row'));
      for(const tc of (liveToolCalls||[])){
        if(skipUnkeyedRestoredDuplicates&&hasRestoredLiveToolRows&&!liveToolReplayId(tc)) continue;
        if(tc&&tc.name) appendLiveToolCard(tc,{sessionId:sid,streamId:activeStreamId});
      }
    };
    let didReconnect=false;
    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function'){
      INFLIGHT[sid].reattach=false;
      if (!_isCurrentLoad()) return;
      didReconnect=true;
      attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
    }
    syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
    const restoredAnchorScene=activeStreamId&&typeof window!=='undefined'
      ? ((typeof window._renderLiveAnchorActivitySceneForStream==='function'&&window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid))||
        _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid))
      : false;
    if(typeof ensureRunActivityForCurrentTurn==='function') ensureRunActivityForCurrentTurn();
    const hasStructuredLiveState=!!(INFLIGHT[sid]&&(
      String(INFLIGHT[sid].lastAssistantText||'').trim()||
      String(INFLIGHT[sid].lastReasoningText||'').trim()||
      !!(INFLIGHT[sid].anchorActivityScene&&Array.isArray(INFLIGHT[sid].anchorActivityScene.activity_rows)&&INFLIGHT[sid].anchorActivityScene.activity_rows.length)||
      (Array.isArray(INFLIGHT[sid].activityBurstAnchors)&&INFLIGHT[sid].activityBurstAnchors.length)||
      (Array.isArray(INFLIGHT[sid].toolCalls)&&INFLIGHT[sid].toolCalls.length)
    ));
    let restoredLiveTurn=!!restoredAnchorScene;
    if(!restoredLiveTurn&&typeof restoreLiveTurnHtmlForSession==='function'){
      if(!hasStructuredLiveState){
        restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }else{
        const liveTurn=document.getElementById('liveAssistantTurn');
        const hasCurrentWorklogContent=!!(liveTurn&&liveTurn.querySelector(
          '.live-worklog[data-live-worklog-shell="1"] .tool-card-row,'+
          '.live-worklog[data-live-worklog-shell="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-worklog-group="1"] .wl-reason,'+
          '.tool-call-group[data-live-tool-call-group="1"] .tool-card-row,'+
          '.tool-call-group[data-live-tool-call-group="1"] .wl-reason'
        ));
        if(hasCurrentWorklogContent) restoredLiveTurn=true;
        else restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }
    }
    if(restoredLiveTurn&&didReconnect){
      replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});
    }
    if(!restoredLiveTurn){
      clearLiveToolCards();
      if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
      if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
      else appendThinking();
      replayPersistedLiveToolCards();
    }
    if(!restoredAnchorScene&&typeof ensureLiveWorklogShell==='function'){
      const liveTurn=document.getElementById('liveAssistantTurn');
      if(!liveTurn||!liveTurn.querySelector('.tool-call-group[data-tool-worklog-group="1"]')) ensureLiveWorklogShell();
    }
    _deferWorkspaceRefreshForSession(sid);
    setBusy(true);setComposerStatus('');
    startApprovalPolling(sid);
    if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
    if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
  }else{
    // Phase 2b: Idle session — load full messages lazily for rendering.
    // _ensureMessagesLoaded is idempotent; it skips if S.messages already populated.
    // #5177: when the caller asked us to keep stale messages until the new ones
    // arrive (visibility/focus recovery), force the fetch so the
    // "messages already populated" early-return inside _ensureMessagesLoaded
    // does NOT skip the swap to the new transcript.
    try {
      await _ensureMessagesLoaded(sid, {force:_keepStaleUntilLoaded, loadGeneration:_loadGeneration});
    } catch (e) {
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      // Network errors, server failures, or SSE drops (Chrome error codes 4/5)
      // can cause _ensureMessagesLoaded to throw. Without a try/catch here the
      // "Loading conversation..." div injected at the top of loadSession would
      // persist forever with no recovery path.
      const _msgInner = $('msgInner');
      if (_msgInner) {
        _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load messages. Try switching sessions or refreshing.</div>';
      }
      if (typeof showToast === 'function') showToast('Failed to load conversation messages', 3000, 'error');
      if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
      return;
    }
    // Stale? A newer loadSession() call has already started (#1060).
    if (!_isCurrentLoad()) return;

    // Restore any queued message that survived page refresh or tab restore.
    if(typeof queueSessionMessage==='function'){
      try{
        const _entries=typeof _readPersistedSessionQueue==='function'
          ? _readPersistedSessionQueue(sid)
          : [];
        if(Array.isArray(_entries)&&_entries.length){
          const _lastMsg=S.messages.slice().reverse()
            .find(m=>m&&m.role==='assistant');
          const _lastAsst=_lastMsg?(_lastMsg.timestamp||_lastMsg._ts||0)*1000:0;
          const _fresh=_entries.filter(e=>!e._queued_at||e._queued_at>_lastAsst);
          if(_fresh.length){
            const _first=_fresh[0];
            const _msg=$&&$('msg');
            if(_msg&&_first.text&&!_msg.value){
              _msg.value=_first.text||'';
              if(typeof autoResize==='function') autoResize();
              if(typeof showToast==='function') showToast((_fresh.length>1?`${_fresh.length} queued messages restored (showing first)`:'Queued message restored')+' — review and send when ready');
            }
          }
          if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);
        }
      }catch(_){if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);}
    }

    // Reconstruct tool calls from message metadata, or fall back to session-level summary.
    // (hasMessageToolMetadata already computed inside _ensureMessagesLoaded; S.toolCalls set there.)
    updateQueueBadge(sid);

    // Attach pending user message if one is queued.
    _mergePendingSessionMessage(S.session,S.messages);

    // Self-heal-vs-live-render race guard (maintainer/Codex-reproduced; verified
    // in an isolated instance). `activeStreamId` was snapshotted BEFORE the
    // awaited _ensureMessagesLoaded above. During a force reload (the
    // `session-updated` self-heal or any keepStaleUntilLoaded recovery), a
    // server-initiated turn can fire `server_turn_started` mid-await and set
    // S.activeStreamId for THIS sid. Without re-reading, the idle branch below
    // would clear S.activeStreamId/S.busy off the stale (null) snapshot and
    // silently kill the live turn's render. Fold a concurrently-attached
    // same-session stream into activeStreamId so the existing attach branch
    // (and all its `attachLiveStream(sid, activeStreamId, ...)` calls) keeps it.
    activeStreamId = activeStreamId || ((S.activeStreamId && S.session && S.session.session_id===sid) ? S.activeStreamId : null);

    if(activeStreamId){
      S.busy=true;
      S.activeStreamId=activeStreamId;
      if(typeof attachLiveStream==='function') attachLiveStream(sid, activeStreamId, S.session.pending_attachments||[], {reconnecting:true});
      else if(typeof watchInflightSession==='function') watchInflightSession(sid, activeStreamId);
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      const restoredAnchorScene=activeStreamId&&typeof window!=='undefined'
        ? ((typeof window._renderLiveAnchorActivitySceneForStream==='function'&&window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid))||
          _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid))
        : false;
      let restoredLiveTurn=!!restoredAnchorScene;
      if(!restoredLiveTurn&&typeof restoreLiveTurnHtmlForSession==='function'){
        restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);
      }
      if(!restoredLiveTurn){
        if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
        else appendThinking();
      }
      _deferWorkspaceRefreshForSession(sid);
      updateQueueBadge(sid);
      startApprovalPolling(sid);
      if(typeof startClarifyPolling==='function') startClarifyPolling(sid);
      if(typeof _fetchYoloState==='function') _fetchYoloState(sid);
    }else{
      S.busy=false;
      S.activeStreamId=null;
      updateSendBtn();
      setStatus('');
      setComposerStatus('');
      updateQueueBadge(sid);
      syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);
      if(typeof resumeManualCompressionForSession==='function') resumeManualCompressionForSession(sid);
      // Workspace refresh is guarded by session id inside loadDir(); keep it
      // after the transcript's first paint so chat switching is not competing
      // with file-tree / git badge IO.
      _deferWorkspaceRefreshForSession(sid);
    }
  }

  return true;
}

async function loadSession(sid){
  const opts = arguments[1] || {};
  // Resolve canonical lineage SID BEFORE both the direct and sidebar preload
  // notifications so extensions always see the canonical session id, not the
  // raw sidebar click id (which may differ after lineage folding).
  if(!opts.skipLineageResolve && typeof _resolveSessionIdFromSidebarLineage==='function'){
    const resolvedSid=_resolveSessionIdFromSidebarLineage(sid);
    if(resolvedSid&&resolvedSid!==sid) sid=resolvedSid;
  }
  // Extension pre-open hook — fires once per sidebar click, not on every call.
  // _openSidebarSession passes _preloadNotified:true so the hook isn't re-fired
  // when loadSession runs the actual navigation inside it.
  if(!opts.skipExtHooks && !opts._preloadNotified && typeof _hermesNotifySessionOpen==='function'){
    var _preResult=_hermesNotifySessionOpen(sid, null, {preload:true, opts:opts});
    if(_preResult&&_preResult.cancel===true){
      return;
    }
  }
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid===sid;
  // Clicking the already-open session in the sidebar is a no-op. Reloading it
  // tears down active pane state and can reset the long-session scroll window
  // to the top even though the user did not navigate anywhere. Explicit
  // refresh paths pass {force:true} when external state.db changes arrive.
  // Do not no-op a same-session click while another load is in flight: the
  // previous transcript may already have been cleared for the pending switch.
  // Static force-reload invariant: if(currentSid===sid && !forceReload) return;
  // #2971: idempotent re-arm before the no-op guard revives a stream a prior
  // failed loadSession killed; no-ops on real switches.
  _rearmActiveSessionStream();
  if(currentSid===sid && !forceReload && (!sessionLoadState.loadingSessionId || sessionLoadState.loadingSessionId===sid)){
    // Re-selecting the already-open session is a no-op for transcript/scroll, but
    // it is still a *visit*: clear a stale sidebar unread dot (e.g. one a
    // background completion left on the open, unfocused pane) before returning.
    if(_sessionVisitHasUnreadState(sid)){
      _acknowledgeSessionVisit(
        sid,
        Number(S.session.message_count || 0),
        Number(S.session.last_message_at || S.session.updated_at || 0)
      );
    }
    return;
  }
  // Mark this session as the in-flight load. Subsequent loadSession() calls
  // will overwrite this; stale awaits use the mismatch to bail out (#1060).
  const _loadGeneration=sessionLoadState.begin(sid);
  const _isCurrentLoad=()=>sessionLoadState.isCurrent(sid,_loadGeneration);
  if(currentSid!==sid&&typeof _uploadPendingFilesSyncProgressForSession==='function')_uploadPendingFilesSyncProgressForSession(sid);
  // Reset scroll state for fresh session navigation — the reader expects to
  // land at the bottom of the new transcript, not wherever a stale unpin flag
  // from a prior session or a stray touch event during loading would place them.
  if (currentSid !== sid && typeof _messageUserUnpinned !== 'undefined') {
    _messageUserUnpinned = false;
    _scrollPinned = true;
  }
  stopApprovalPolling();hideApprovalCard(forceReload);
  if(typeof stopSessionStream==='function') stopSessionStream();
  _yoloEnabled=false;_updateYoloPill();
  if(typeof stopClarifyPolling==='function') stopClarifyPolling();
  if(typeof hideClarifyCard==='function') hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed');
  // Show loading indicator immediately for responsiveness.
  // Cleared by renderMessages() once full session data arrives.
  // Persist the current composer draft before switching away so it can be
  // restored when the user switches back (#1060). Save to server now so the
  // draft survives page refresh and syncs across clients.
  if (currentSid && currentSid !== sid) {
    if(typeof window._clearPendingSelections==='function') window._clearPendingSelections();
    if(typeof _clearQueueCardDisplay==='function') _clearQueueCardDisplay(currentSid);
    await _saveComposerDraftNow(currentSid, ($('msg') || {}).value || '', S.pendingFiles ? [...S.pendingFiles] : []);
    // The awaited draft save above yields the event loop. If another
    // loadSession() started for a different session while we were waiting
    // (rapid switch B→C), _loadingSessionId now points at that newer load —
    // bail out before the destructive state-clearing block below so this stale
    // continuation can't wipe S.messages / write the loading placeholder /
    // close streams for the session the user actually landed on (#1060 guard,
    // extended to cover the new pre-switch await).
    if (!_isCurrentLoad()) return;
    // Snapshot the live turn before msgInner is replaced. Preserves the activity
    // timer, partial response, and tool cards so switching back does not rebuild
    // the stream UI from scratch.
    if(
      (S.busy||S.activeStreamId||(INFLIGHT&&INFLIGHT[currentSid]))&&
      typeof snapshotLiveTurnHtmlForSession==='function'
    ){
      if(!INFLIGHT[currentSid]){
        INFLIGHT[currentSid]={
          messages:Array.isArray(S.messages)?[...S.messages]:[],
          uploaded:[],
          toolCalls:Array.isArray(S.toolCalls)?[...S.toolCalls]:[],
        };
      }
      snapshotLiveTurnHtmlForSession(currentSid);
    }
  }
  const _keepStaleUntilLoaded = !!opts.keepStaleUntilLoaded && sameSessionForceReload;
  if (currentSid !== sid || forceReload) {
    // #3306: When force-reloading the currently-active session (e.g. external
    // poll triggering a refresh), snapshot the existing messages BEFORE we
    // clear them. _ensureMessagesLoaded() runs the ephemeral-field
    // carry-forward (_turnUsage, _turnDuration, _turnTps, _gatewayRouting,
    // _statusCard, _anchor_stream_id) against S.messages, but by the time the API fetch returns
    // S.messages has already been reset to [] here and the carry-forward is a
    // no-op. The visible symptom is the token-usage badge vanishing ~10s
    // after each assistant turn completes. Stash the snapshot so the
    // carry-forward call can consume it.
    sessionLoadState.pendingCarryForwardSnapshot = (currentSid === sid && forceReload)
      ? (S.messages || []).slice()
      : null;
    // #3239: also capture a reload-width hint BEFORE clearing so the
    // authoritative reload preserves the already-loaded transcript width
    // instead of collapsing a long session back to the default tail window.
    if (sameSessionForceReload) _captureSameSessionForceReloadHint(sid);
    else _clearSameSessionForceReloadHint();
    // #5177: keep-stale-until-loaded path — defer the destructive
    // S.messages/toolCalls clear so the user does NOT see a transcript-wide
    // blank gap during the metadata + messages round-trip. Only the
    // visibility / focus recovery callers in refreshActiveSessionIfExternallyUpdated
    // request this. The new transcript will be SWAPPED into S.messages by the
    // forced _ensureMessagesLoaded(...{force:true}) call below, producing a
    // single render frame with old DOM directly replaced by new DOM rather
    // than the old → empty → new sequence the default branch produces.
    //
    // The session-switch branch (currentSid !== sid) MUST continue to clear
    // synchronously — leaving a prior session's transcript on screen during a
    // navigation is the original bug this clear was written for. We gate
    // strictly on sameSessionForceReload (computed above as part of
    // _keepStaleUntilLoaded) so cross-session switches keep their existing
    // behaviour.
    if (!_keepStaleUntilLoaded) {
      S.messages = [];
      S.toolCalls = [];
      messageLoadingBindings._messagesTruncated = false;
      messageTimelineBindings._oldestIdx = 0;
    }
    // Close live SSE streams from the session we're leaving. The error
    // handler checks _isSessionActivelyViewed() and won't auto-reconnect
    // for a backgrounded session, preventing leaked connections that would
    // pump token events into an orphaned closure, freezing the main thread.
    if (currentSid && currentSid !== sid && typeof closeOtherLiveStreams === 'function') {
      closeOtherLiveStreams(sid);
    }
    messageTimelineBindings._loadingOlder = false;
    const _msgInner = $('msgInner');
    if (_msgInner && currentSid !== sid) _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Loading conversation...</div>';
  }
  // Phase 1: Load metadata only (~1KB) for fast session switching. Keep model
  // resolution out of the first-paint path; old provider-shaped model IDs are
  // repaired by the deferred resolver after S.session is assigned.
  // Guard against network/server failures to prevent a permanently stuck loading state.
  let data;
  try {
    data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  } catch(e) {
    const profileMismatch=_sessionProfileMismatchFromError(e);
    if(profileMismatch && profileMismatch.profile && !opts.skipProfileResolve){
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      try{
        if(typeof showToast==='function') showToast(`Switching to ${profileMismatch.profile} profile for this session…`,2200);
        await _switchProfileForSessionLoad(profileMismatch.profile);
        // Post-await stale-load guard (Codex): the profile switch above does a
        // network POST + session-list re-render, during which the user may have
        // navigated to a different session. If we no longer own the load, bail
        // before clearing _loadingSessionId or retrying so the stale
        // continuation can't hijack the UI back to the old target.
        if (!_isCurrentLoad()) {
          _rearmActiveSessionStream();
          return;
        }
        if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
        return loadSession(sid,{...opts,skipProfileResolve:true,force:true,_preloadNotified:true});
      }catch(switchErr){
        e=switchErr;
      }
    }
    const _msgInner = $('msgInner');
    // Stale-load guard (Codex): a newer loadSession() may have started while this
    // request was awaiting (e.g. the user clicked a healthy session during a
    // boot-time restore). currentSid was snapshotted before the await, so without
    // this guard a failed superseded load could self-heal (wipe localStorage/URL)
    // for the session the user actually navigated to. If we no longer own the
    // load, re-arm the active session's stream and bail before any DOM mutation
    // or self-heal.
    if (!_isCurrentLoad()) {
      _rearmActiveSessionStream();
      return;
    }
    if(_msgInner){
      if(e.status===404){
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Session not available in web UI.</div>';
        // Self-heal (clear saved id + strip /session/<id> URL) only when the
        // 404'd id is the one we are activating: a boot-time restore
        // (!currentSid, #2798) or a mid-session reload of the *current* session
        // whose sidecar was deleted server-side (#2782). A click into a
        // *different* dead session (currentSid && currentSid!==sid) must not run
        // it: localStorage and the URL still point at the live session (both are
        // only updated on a successful load), so wiping them would log the user
        // out of a healthy session. The URL strip is needed in the self-heal
        // case because _sessionIdFromLocation() re-injects the id on reload.
        // Only the rethrow stays gated on !currentSid: boot rethrows to fall
        // through to empty-state; mid-session there is no boot path to reach.
        if(!currentSid || currentSid===sid){
          try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
          try{ history.replaceState(null,'',_appRootPath()); }catch(_){ }
          if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
          if(!currentSid){
            throw e;
          }
        }
      } else {
        // Non-404, non-401 failure (400, 403, 500, network): 401 is handled
        // via the if(!data) guard below since api() returns undefined on 401
        // rather than throwing. Clear the stuck session ID only during boot
        // (!currentSid) so the next boot doesn't retry the same dead session.
        // When currentSid is set, a 500/network error may be transient — the
        // session might still exist on the server (#4028 follow-up).
        _clearStuckSessionOnBoot(sid, currentSid);
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load session. Try refreshing or switching sessions.</div>';
        if(typeof showToast==='function') showToast('Failed to load session',3000,'error');
      }
    }
    _clearSameSessionForceReloadHint(sid);
    // Capture whether this failure self-healed away the current session (a
    // 404 on the *current* session whose sidecar was deleted server-side).
    // In that case there is no live session left to stream for, so we must
    // NOT restart — doing so would spin the SSE reconnect loop against a dead
    // session_id.
    const _selfHealedCurrent = (e.status===404) && (currentSid===sid);
    if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
    // The session stream was stopped unconditionally at the top of this load
    // (mirroring stopApprovalPolling). On the happy path it's restarted ~120
    // lines below, but this failure exit never reaches that point — leaving
    // the session still on screen permanently silenced. bg_task_complete
    // events (the new feature's primary delivery path) would be dropped until
    // the user explicitly navigates to a session again. Restart the stream for
    // the session that remains on screen. Skip when a newer load is already in
    // flight (_loadingSessionId !== null after the reset above): that load owns
    // the stream and starts its own. Skip the self-healed-current case (no live
    // session to stream).
    // #2971: this fetch-error path keeps its bespoke guarded restart (rather
    // than the shared _rearmActiveSessionStream helper used on the other
    // early-returns) because only here can the current session have just
    // self-healed away — re-arming a 404'd/deleted session_id would spin the
    // SSE reconnect loop against a dead session.
    if (currentSid && !_selfHealedCurrent && sessionLoadState.loadingSessionId === null
        && typeof startSessionStream === 'function') {
      startSessionStream(currentSid);
    }
    return;
  }
  // Guard: api() may have redirected (401) and returned undefined; in that case
  // the browser is already navigating away, so abort the rest of this flow.
  // No self-heal: 401 is transient auth expiry — the session still exists
  // server-side. Clearing localStorage would wipe the saved session id and
  // send users to empty state after re-login (#4028 follow-up).
  if (!data) {
    _clearSameSessionForceReloadHint(sid);
    if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
    // #2971: re-arm the still-displayed session's stream (defensive — harmless
    // if the 401 redirect is already tearing the page down). Idempotent.
    _rearmActiveSessionStream();
    return;
  }
  // Stale response? A newer loadSession() call has already started (#1060).
  if (!_isCurrentLoad()) {
    // #2971: a newer in-flight load owns the final stream arming, but until it
    // assigns S.session and reaches startSessionStream() the currently-shown
    // session must not be left stream-dead by our top-of-function teardown.
    // Re-arm the genuinely-displayed S.session (idempotent — no-ops once the
    // newer load arms its own sid).
    _rearmActiveSessionStream();
    return;
  }
  // #2980: if this (current) load resolved a hidden pre-compression snapshot,
  // follow the backend's continuation hint to the visible continuation so a
  // mobile reload mid-compression doesn't strand the user on a hidden snapshot.
  // Do NOT write URL/localStorage here — let the re-entrant loadSession update
  // them only once the continuation actually loads, so a rejected/deleted/
  // cross-profile continuation can't poison restore state with an unusable id.
  const continuationSid=(data.session&&data.session.continuation_session_id)||'';
  if(continuationSid&&continuationSid!==sid&&!opts.skipContinuationResolve){
    sessionLoadState.loadingSessionId=null;
    return loadSession(continuationSid,{...opts,skipLineageResolve:true,skipContinuationResolve:true,force:true,_preloadNotified:true});
  }
  S.session=data.session;
  if(typeof _clearEmptyComposerModelOverride==='function') _clearEmptyComposerModelOverride();
  // Loading a real existing session abandons any pre-session toolset override
  // staged on the empty composer before any deferred refresh work runs.
  S._pendingSessionToolsets=null;
  if(typeof window!=='undefined'){
    if(!S._bootReady&&typeof window._startBootModelDropdown==='function'){
      Promise.resolve().then(()=>{
        if(!S.session||S.session.session_id!==sid) return undefined;
        return window._startBootModelDropdown();
      }).catch(()=>{});
    }else{
      // Session metadata already carries the active model, and syncTopbar()
      // injects a missing session-scoped option into the select. Mark the
      // provider catalog stale here, but defer its bounded freshness check
      // until the user actually opens the model picker.
      window._modelDropdownReady=null;
    }
  }
  if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
  S.session._modelResolutionDeferred=true;
  S.lastUsage={...(data.session.last_usage||{})};
  // Reset scroll-direction tracker only on real session switches so the new
  // chat's first scroll doesn't compare against the previous chat's scrollTop
  // and false-trigger an unpin (#1731 follow-up — Opus stage-302 SHOULD-FIX).
  // Same-session force refreshes reuse the current transcript viewport; clearing
  // the sticky-unpin state here makes preserveScroll treat a reader mid-answer
  // as pinned and snap them back to the bottom on the next render.
  if (currentSid !== sid) {
    _clearDeferredActiveSessionExternalRefresh();
  }
  if (currentSid !== sid && typeof window !== 'undefined' && typeof window._resetScrollDirectionTracker === 'function') {
    try { window._resetScrollDirectionTracker(); } catch (_) {}
  }
  if(typeof _applyPendingSessionModelForSession==='function') _applyPendingSessionModelForSession(sid);
  _resolveSessionModelForDisplaySoon(sid);
  // Sync workspace display immediately so the chip label reflects the new session's workspace
  // before any async message-loading begins (mirrors how model is handled).
  if(typeof syncTopbar==='function') syncTopbar();
  // Acknowledge the visit as soon as the session metadata is accepted for the
  // in-flight load: clears the viewed count + any stale completion-unread marker
  // `let` (not const): re-read below, after the awaited _ensureMessagesLoaded,
  // so a server_turn_started that attaches a live stream MID-RELOAD is honored
  // by the attach/idle decision instead of being clobbered by the stale snapshot.
  let activeStreamId=S.session.active_stream_id||null;
  // If the server says the session is idle, reset browser-side streaming flags
  // NOW — BEFORE _acknowledgeSessionVisit() below (whose sidebar repaint would
  // otherwise inherit the PREVIOUS session's busy/stream state) and before the
  // async _ensureMessagesLoaded gap. Without this, S.busy can remain true from a
  // still-running stream in the PREVIOUS session while S.session.session_id has
  // already advanced to the new one. _isSessionLocallyStreaming() checks
  // (isActive && S.busy), so the new session would appear locally-streaming
  // (sidebar spinner, Stop button, thinking state on an idle chat) and the visit
  // repaint would manufacture a phantom unread. Also clears stale INFLIGHT
  // entries left behind by a crashed/restarted stream. (#5917 gate: reset must
  // precede the acknowledge repaint.)
  if(!activeStreamId){
    S.activeStreamId=null;
    S.busy=false;
    if(INFLIGHT[sid]){
      delete INFLIGHT[sid];
      if(typeof clearInflightState==='function') clearInflightState(sid);
    }
  }

  // and syncs the polling snapshot so a deferred /api/sessions poll landing
  // during the async message-load gap below cannot re-flag a stale unread dot.
  _acknowledgeSessionVisit(
    S.session.session_id,
    Number(data.session.message_count || 0),
    Number(data.session.last_message_at || data.session.updated_at || 0)
  );
  try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
  _setActiveSessionUrl(S.session.session_id);
  if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);


  if(!await _restoreLoadedSession({
    sid,activeStreamId,_keepStaleUntilLoaded,_loadGeneration,_isCurrentLoad,sameSessionForceReload,
  })) return;

  // Sync context usage indicator from session data
  const _s=S.session;
  if(_s&&typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    const _pickPositive=(latest,stored,dflt=0)=>Number(latest)>0?latest:(Number(stored)>0?stored:dflt);
    _syncCtxIndicator({
      input_tokens:      _pick(u.input_tokens,      _s.input_tokens),
      output_tokens:     _pick(u.output_tokens,     _s.output_tokens),
      estimated_cost:    _pick(u.estimated_cost,    _s.estimated_cost),
      cache_read_tokens: _pick(u.cache_read_tokens, _s.cache_read_tokens),
      cache_write_tokens:_pick(u.cache_write_tokens,_s.cache_write_tokens),
      cache_hit_percent: _pick(u.cache_hit_percent, _s.cache_hit_percent, null),
      context_length:    _pickPositive(u.context_length, _s.context_length),
      last_prompt_tokens:_pick(u.last_prompt_tokens,_s.last_prompt_tokens),
      post_compression_context_tokens_estimate:_s.post_compression_context_tokens_estimate||null,
      threshold_tokens:  _pick(_s.threshold_tokens,  u.threshold_tokens),
    });
  }
  if(typeof _renderPendingPromptsForActiveSession==='function') _renderPendingPromptsForActiveSession();

  // Restore server-persisted composer draft (synced across clients + survives refresh).
  // Pass sid so _restoreComposerDraft can skip if this session is mid-load (guards
  // against stale writes from slow responses racing to restore the previous draft).
  const _draft = S.session && S.session.composer_draft;
  if (_draft && (typeof _restoreComposerDraft === 'function')) {
    _restoreComposerDraft(_draft, sid, {preserveActiveInput:!!opts.preserveActiveInput || (currentSid===sid&&forceReload)});
  }

  // Clear the in-flight session marker now that this load has completed (#1060).
  if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;

  // Re-acknowledge the visit after the async message-load gap. A deferred
  // sidebar /api/sessions poll can land while _ensureMessagesLoaded is in
  // flight and re-mark the open session unread; re-syncing here clears that
  // sticky dot once the transcript is settled (#4946).
  //
  // Gate the final ack on _isSessionActivelyViewedForList(sid): a completion
  // that lands while _ensureMessagesLoaded() is in flight AND the tab then goes
  // hidden is correctly marked unread — an UNCONDITIONAL ack here would wrongly
  // clear that hidden-tab-completion marker. Only clear when the session is
  // still actively viewed. (#5917 gate finding)
  if (
    S.session && S.session.session_id === sid &&
    (typeof _isSessionActivelyViewedForList !== 'function' || _isSessionActivelyViewedForList(sid))
  ) {
    _acknowledgeSessionVisit(
      sid,
      Number(S.session.message_count || 0),
      Number(S.session.last_message_at || S.session.updated_at || 0)
    );
  }

  if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();

  // ── Cross-channel handoff hint ──
  // After session fully loaded, check if this is a messaging session with
  // enough conversation rounds to warrant a handoff hint bar.
  if (S.session && _isMessagingSession(S.session)) {
    _checkAndShowHandoffHint(sid);
  } else {
    _hideHandoffHint();
  }
  // Extension post-load hook
  if(!opts.skipExtHooks && typeof _hermesNotifySessionOpen==='function'){
    try{ _hermesNotifySessionOpen(sid, S.session, {loaded:true, opts:opts}); }catch(_){}
  }
}

export const sessionLifecycle=Object.freeze({create:newSession,rearmStream:_rearmActiveSessionStream,restore:_restoreLoadedSession,load:loadSession});

export { _newSessionInFlight, _rememberEmptyComposerModelOverride, loadSession, newSession };
