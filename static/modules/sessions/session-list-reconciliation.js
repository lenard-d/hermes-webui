import { SESSION_LIST_INTERACTION_IDLE_MS, sessionListCoordination } from './session-list-coordination.js';
import { sessionRunRegistry } from './session-run-registry.js';

const _sessionStreamingById=sessionRunRegistry.streamingById;
import { _isServerIdleSessionRow, _isSessionEffectivelyStreaming, _isSessionLocallyStreaming, _markPollingCompletionUnreadTransitions, _purgeStaleInflightEntries, _reconcileActiveSessionIdleStateFromList, _rememberSessionListSource } from './session-run-state.js';
import { _forgetObservedStreamingSession, _recordSessionProfileCount } from './session-unread.js';
import { _requestedSessionSidebarSource, _sessionListExcludeHiddenEnabled } from './sidebar-session-opening.js';
import { _optimisticallyRemovedSessionIds, _sessionAttentionSoundState, sidebarStateBindings } from './sidebar-store.js';
import { _pruneLineageReportCacheToVisibleSessions } from './session-lineage-report.js';
import { sessionTimeBindings } from './session-time.js';
import { _activeSessionIdForSidebar } from './session-navigation.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _scheduleActiveSessionIdleReload, ensureActiveSessionExternalRefreshPoll, ensureSessionTimeRefreshPoll, startStreamingPoll, stopStreamingPoll } from './session-list-refresh.js';
import { animateNextSessionListRefresh, sessionListViewBindings } from './session-list-skeleton.js';
import { ensureSessionEventsSSE } from './sidebar-session-events.js';

function _isOptimisticFirstTurnSessionRow(s){
  if(!s||!s.session_id||s.archived) return false;
  const messageCount=Number(s.message_count||0);
  if(messageCount<=0&&!(s.pending_user_message||s.has_pending_user_message)) return false;
  return Boolean(
    s.is_streaming||
    s.active_stream_id||
    s.pending_user_message||
    s.has_pending_user_message||
    s.pending_started_at||
    _isSessionLocallyStreaming(s)||
    _sessionStreamingById.get(s.session_id)===true
  );
}

function _shouldKeepLocalOnlyOptimisticSessionRow(local){
  if(!_isOptimisticFirstTurnSessionRow(local)) return false;
  const sid=local.session_id;
  if(typeof _sendInProgress!=='undefined'&&_sendInProgress&&sid===_sendInProgressSid) return true;
  const activeSid=S&&S.session&&S.session.session_id;
  const isActive=Boolean(activeSid&&activeSid===sid);
  const hasRuntimeConfirmation=Boolean(local.active_stream_id||local.pending_user_message||local.has_pending_user_message||local.pending_started_at);
  if(isActive&&S.busy&&hasRuntimeConfirmation) return true;
  const localTs=Number(local.last_message_at||local.updated_at||0);
  const ageMs=localTs>0?Date.now()-(localTs*1000):Infinity;
  return Boolean(isActive&&S.busy&&ageMs>=0&&ageMs<5000);
}

function _dropStaleOptimisticSessionRow(sid){
  if(!sid) return;
  if(typeof _rememberSessionListSource==='function') _rememberSessionListSource(null, sid, false);
  if(INFLIGHT&&INFLIGHT[sid]){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }
  if(typeof _sessionStreamingById!=='undefined'&&_sessionStreamingById&&typeof _sessionStreamingById.set==='function'){
    _sessionStreamingById.set(sid,false);
  }
  if(typeof _forgetObservedStreamingSession==='function') _forgetObservedStreamingSession(sid);
}

function _mergeOptimisticFirstTurnSessions(fetchedSessions){
  const merged=Array.isArray(fetchedSessions)?[...fetchedSessions]:[];
  const bySid=new Map();
  merged.forEach((s,idx)=>{if(s&&s.session_id) bySid.set(s.session_id,idx);});
  for(const local of Array.isArray(sidebarStateBindings._allSessions)?sidebarStateBindings._allSessions:[]){
    if(!_isOptimisticFirstTurnSessionRow(local)) continue;
    const sid=local.session_id;
    const idx=bySid.has(sid)?bySid.get(sid):-1;
    if(idx>=0){
      const fetched=merged[idx]||{};
      const fetchedIsServerIdle=_isServerIdleSessionRow(fetched);
      const keepLocalOptimistic=fetchedIsServerIdle?false:_shouldKeepLocalOnlyOptimisticSessionRow(local);
      const localCount=Number(local.message_count||0);
      const fetchedCount=Number(fetched.message_count||0);
      const localTs=Number(local.last_message_at||local.updated_at||0);
      const fetchedTs=Number(fetched.last_message_at||fetched.updated_at||0);
      if(!keepLocalOptimistic&&typeof _dropStaleOptimisticSessionRow==='function') _dropStaleOptimisticSessionRow(sid);
      merged[idx]={
        ...local,
        ...fetched,
        title:keepLocalOptimistic?(local.title||fetched.title):fetched.title,
        message_count:keepLocalOptimistic?Math.max(localCount,fetchedCount):fetchedCount,
        last_message_at:keepLocalOptimistic?Math.max(localTs,fetchedTs):fetchedTs,
        updated_at:keepLocalOptimistic?Math.max(Number(local.updated_at||0),Number(fetched.updated_at||0),localTs,fetchedTs):Number(fetched.updated_at||fetchedTs||0),
        active_stream_id:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.active_stream_id||local.active_stream_id||null):null),
        pending_user_message:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.pending_user_message||local.pending_user_message||null):null),
        pending_started_at:fetchedIsServerIdle?null:(keepLocalOptimistic?(fetched.pending_started_at||local.pending_started_at||null):null),
        is_streaming:fetchedIsServerIdle?false:Boolean(fetched.is_streaming||(keepLocalOptimistic&&(local.is_streaming||_isSessionLocallyStreaming(local)))),
      };
    }else{
      if(_shouldKeepLocalOnlyOptimisticSessionRow(local)){
        merged.push({...local,is_streaming:true});
        bySid.set(sid,merged.length-1);
      }else{
        _dropStaleOptimisticSessionRow(sid);
      }
    }
  }
  return merged;
}

function _isSessionListUserInteracting(){
  const now=Date.now();
  const list=$('sessionList');
  const pointerOverList=Boolean(list&&(list.matches(':hover')||list.matches(':focus-within')));
  return Boolean(
    sessionListCoordination.pointerActive ||
    pointerOverList ||
    (sessionListCoordination.lastScrollAt && now-sessionListCoordination.lastScrollAt<SESSION_LIST_INTERACTION_IDLE_MS)
  );
}

function _schedulePendingSessionListApply(){
  if(sessionListCoordination.pendingApplyTimer) clearTimeout(sessionListCoordination.pendingApplyTimer);
  sessionListCoordination.pendingApplyTimer=setTimeout(()=>{
    sessionListCoordination.pendingApplyTimer=0;
    if(!sessionListCoordination.pendingPayload) return;
    if(_isSessionListUserInteracting()){
      _schedulePendingSessionListApply();
      return;
    }
    const payload=sessionListCoordination.pendingPayload;
    sessionListCoordination.pendingPayload=null;
    if(payload.gen!==sidebarStateBindings._renderSessionListGen) return;
    // Profile switch may have bumped unread gen after the list gen check
    // window; still drop completion-marking for the stale pre-switch payload.
    _applySessionListPayload(payload.sessData,payload.projData,{
      unreadGen:payload.unreadGen,
    });
  }, Math.max(120, SESSION_LIST_INTERACTION_IDLE_MS));
}


function _sessionAttentionSoundSignature(s){
  const attention=s&&s.attention&&typeof s.attention==='object'?s.attention:null;
  const count=Number(attention&&attention.count);
  if(!attention||!attention.kind||!Number.isFinite(count)||count<=0)return null;
  const kind=String(attention.kind)==='approval'?'approval':(String(attention.kind)==='clarify'?'clarify':'attention');
  return `${kind}:${Math.max(1,count||1)}`;
}

function _syncSessionAttentionSoundState(sessions){
  const next=new Map();
  for(const s of Array.isArray(sessions)?sessions:[]){
    if(!s||!s.session_id)continue;
    const sig=_sessionAttentionSoundSignature(s);
    if(sig) next.set(s.session_id,sig);
  }
  if(!sidebarStateBindings._sessionAttentionSoundPrimed){
    sidebarStateBindings._sessionAttentionSoundPrimed=true;
    _sessionAttentionSoundState.clear();
    next.forEach((sig,sid)=>_sessionAttentionSoundState.set(sid,sig));
    return;
  }
  next.forEach((sig,sid)=>{
    const prev=_sessionAttentionSoundState.get(sid);
    if(prev!==sig){
      const [kind,countRaw]=String(sig).split(':');
      const count=Number(countRaw)||1;
      const s=(Array.isArray(sessions)?sessions:[]).find(item=>item&&item.session_id===sid)||{session_id:sid};
      const playKey=typeof _attentionSoundKey==='function'?_attentionSoundKey(s.session_id,kind,count):`${s.session_id}:${sig}`;
      if(playKey&&typeof playAttentionSound==='function') playAttentionSound(playKey);
    }
  });
  _sessionAttentionSoundState.clear();
  next.forEach((sig,sid)=>_sessionAttentionSoundState.set(sid,sig));
}

// Signature of everything the sidebar render reads. Used to skip the full DOM
// rebuild when a poll returns data identical to what is already on screen (the
// common idle case). We serialize the FULL applied row objects (not a curated
// field subset) plus the reference/nesting rows and the coarse display state, so
// ANY server- or client-visible field the render helpers read (streaming/pending
// state, attention dots, source/read-only/worktree/lineage/child/model/profile
// meta, etc.) is covered — a narrow allowlist silently false-skips the moment a
// new rendered field is added (Codex #5467 gate: it omitted pending/running,
// attention, and the source/lineage cluster). A streaming/pending row's fields
// advance each poll so its signature changes and it still renders. Serialization
// failure returns null → never skip (fail-open). (#5455 WS2.4)
let _lastSessionListRenderSig = null;
function _sessionListRenderSignature(){
  try{
    const search=($('sessionSearch')&&$('sessionSearch').value)||'';
    return JSON.stringify([
      sidebarStateBindings._allSessions,
      sidebarStateBindings._sidebarReferenceSessions,
      sidebarStateBindings._allProjects,
      _activeSessionIdForSidebar(),
      search,
      sidebarStateBindings._sessionSourceFilter,
      !!sidebarStateBindings._sessionSelectMode,
      (window._sidebarDensity==='detailed'?'d':'c'),
      !!sidebarStateBindings._showAllProfiles,
      sidebarStateBindings._otherProfileCount,sidebarStateBindings._archivedWebuiCount,sidebarStateBindings._archivedCliCount,
      sidebarStateBindings._serverWebuiSessionCount,sidebarStateBindings._serverCliSessionCount,
    ]);
  }catch(_){ return null; }
}
function _applySessionListPayload(sessData, projData, opts){
  // Server's other_profile_count tells us how many sessions exist outside the
  // active profile so the "Show N from other profiles" toggle can render
  // without a second round-trip. Stashed on the module for renderSessionListFromCache.
  const applyOpts = (opts && typeof opts === 'object') ? opts : {};
  sidebarStateBindings._otherProfileCount = sessData.other_profile_count || 0;
  sidebarStateBindings._archivedWebuiCount = Number(sessData.archived_webui_count ?? sessData.archived_count ?? 0);
  sidebarStateBindings._archivedCliCount = Number(sessData.archived_cli_count ?? 0);
  sidebarStateBindings._serverWebuiSessionCount = Object.prototype.hasOwnProperty.call(sessData, 'webui_session_count')
    ? Number(sessData.webui_session_count)
    : null;
  sidebarStateBindings._serverCliSessionCount = Object.prototype.hasOwnProperty.call(sessData, 'cli_session_count')
    ? Number(sessData.cli_session_count)
    : null;
  if (!Number.isFinite(sidebarStateBindings._serverWebuiSessionCount)) sidebarStateBindings._serverWebuiSessionCount = null;
  if (!Number.isFinite(sidebarStateBindings._serverCliSessionCount)) sidebarStateBindings._serverCliSessionCount = null;
  // Capture server clock for clock-skew compensation (issue #1144).
  // server_time is epoch seconds from the server's time.time().
  // _serverTimeDelta = client - server, so (Date.now() - _serverTimeDelta)
  // gives an approximation of the current server time.
  if (typeof sessData.server_time === 'number' && sessData.server_time > 0) {
    sessionTimeBindings._serverTimeDelta = Date.now() - (sessData.server_time * 1000);
  }
  if (typeof sessData.server_tz === 'string') {
    sessionTimeBindings._serverTz = sessData.server_tz;
  }
  const serverSessions=_optimisticallyRemovedSessionIds.size
    ? (sessData.sessions||[]).filter(s=>s&&!_optimisticallyRemovedSessionIds.has(s.session_id))
    : (sessData.sessions||[]);
  sidebarStateBindings._sidebarReferenceSessions = Array.isArray(sessData.sidebar_reference_sessions)
    ? sessData.sidebar_reference_sessions
    : [];
  const reconciledActiveSid=(S&&S.session&&S.session.session_id)||'';
  if(_reconcileActiveSessionIdleStateFromList(serverSessions)===true){
    _scheduleActiveSessionIdleReload(reconciledActiveSid);
  }
  sidebarStateBindings._allSessions = _mergeOptimisticFirstTurnSessions(serverSessions);
  // Tag the cache with the scope it was loaded under (active profile +
  // all-profiles flag). If a later /api/sessions fails right after a profile
  // switch, the catch path checks this so it won't re-render the PRIOR
  // profile's rows as if they were current (#4167 review item 3).
  sidebarStateBindings._allSessionsScope = {
    profile: (typeof sessData.active_profile === 'string' && sessData.active_profile)
      ? sessData.active_profile
      : (S.activeProfile || 'default'),
    allProfiles: !!sidebarStateBindings._showAllProfiles,
    sidebarSource: _requestedSessionSidebarSource(),
    excludeHidden: _sessionListExcludeHiddenEnabled(),
  };
  // Record this profile's session count so the NEXT switch into it can pick an
  // honest skeleton (empty-state vs content) before its fetch resolves (#4717).
  // Only record an UNFILTERED total: skip all-profiles (conflates profiles), and
  // skip while a project or CLI-source filter is active (those record a filtered
  // subset that could cache a misleading 0 for a profile that has sessions under
  // a different filter). This mirrors the read-side `filterActive` gate in
  // showSessionListSkeleton so the write and read agree on what the count means.
  const _recordFilterActive = (typeof sidebarStateBindings._activeProject !== 'undefined' && sidebarStateBindings._activeProject)
    || (typeof sidebarStateBindings._sessionSourceFilter !== 'undefined' && sidebarStateBindings._sessionSourceFilter === 'cli');
  if (!sidebarStateBindings._showAllProfiles && !_recordFilterActive) {
    _recordSessionProfileCount(sidebarStateBindings._allSessionsScope.profile, sidebarStateBindings._allSessions.length);
  }
  _syncSessionAttentionSoundState(sidebarStateBindings._allSessions);
  _pruneLineageReportCacheToVisibleSessions(sidebarStateBindings._allSessions);
  sidebarStateBindings._allProjects = projData.projects||[];
  // Capture the recovering-from-error state BEFORE clearing it: the error banner
  // DOM was rendered outside the signature path, so if this payload heals with
  // rows identical to the last render, the identical-signature skip below would
  // leave the stale "Could not load conversations" banner on screen. (Codex #5467)
  const _hadSessionListLoadError = !!sessionListCoordination.loadError;
  sessionListCoordination.loadError = null;
  sessionListCoordination.hasLoadedOnce = true;
  // Greptile #5975 P1: a /api/sessions request started under profile A can
  // finish after a switch to B already cleared A's cron markers. The list gen
  // check can already have passed (TOCTOU) or a deferred apply can land later.
  // Re-validate the profile-switch unread generation (shared with cron poll
  // reset via _cronPollGeneration) immediately before marking completions.
  const expectedUnreadGen = applyOpts.unreadGen;
  const currentUnreadGen = (typeof _cronPollGeneration === 'number') ? _cronPollGeneration : 0;
  if (typeof expectedUnreadGen !== 'number' || expectedUnreadGen === currentUnreadGen) {
    _markPollingCompletionUnreadTransitions(sidebarStateBindings._allSessions);
  }
  const isStreaming = sidebarStateBindings._allSessions.some(s => _isSessionEffectivelyStreaming(s));
  if (isStreaming) {
    startStreamingPoll();
  } else {
    stopStreamingPoll();
  }
  ensureSessionTimeRefreshPoll();
  ensureActiveSessionExternalRefreshPoll();
  if(!sidebarStateBindings._sessionListFirstRenderAnimated&&Array.isArray(sidebarStateBindings._allSessions)&&sidebarStateBindings._allSessions.length){
    animateNextSessionListRefresh({enterAll:true});
    sidebarStateBindings._sessionListFirstRenderAnimated=true;
  }
  ensureSessionEventsSSE();
  // #4671: this payload is the freshly-resolved /api/sessions response (and a superseded
  // response was already discarded by the generation guard upstream), so _allSessions now
  // holds the CURRENT profile's rows. Clear the skeleton flag right before painting so this
  // authoritative render replaces the profile-switch skeleton — while unrelated renders that
  // fire before this point stay blocked by the guard in renderSessionListFromCache().
  const _hadSessionListSkeleton = sessionListViewBindings._sessionListSkeletonActive;
  sessionListViewBindings._sessionListSkeletonActive = false;
  // No-op fast path: if this payload renders identically to what is already on
  // screen (the common case for idle polls) and no entrance animation is
  // pending, skip the full DOM rebuild. Only applies here in the fetch/apply
  // path; the 60s relative-time refresh and every other render trigger call
  // renderSessionListFromCache directly and are unaffected. Guarded by the same
  // conditions renderSessionListFromCache bails on, so a bailed render never
  // caches a signature that would suppress the next real repaint. (#5455 WS2.4)
  // NEVER skip when recovering from a skeleton or error-banner DOM state: those
  // are rendered outside the signature path, so an identical-signature match
  // would leave the skeleton/error on screen instead of the real list. (Codex #5467)
  const _canRenderNow = !sidebarStateBindings._renamingSid && !sidebarStateBindings._sessionActionMenu;
  const _mustForceRender = _hadSessionListSkeleton || _hadSessionListLoadError;
  const _renderSig = _sessionListRenderSignature();
  if(_canRenderNow && !_mustForceRender && !sidebarStateBindings._sessionListRefreshAnimationPending && _renderSig && _renderSig===_lastSessionListRenderSig){
    // Preserve the per-refresh INFLIGHT cleanup that renderSessionListFromCache
    // would otherwise perform, then skip only the DOM rebuild.
    if(typeof _purgeStaleInflightEntries==='function') _purgeStaleInflightEntries();
    return;
  }
  if(_canRenderNow) _lastSessionListRenderSig = _renderSig;
  renderSessionListFromCache();  // no-ops if rename is in progress
}


export { _applySessionListPayload, _dropStaleOptimisticSessionRow, _isSessionListUserInteracting, _schedulePendingSessionListApply };
