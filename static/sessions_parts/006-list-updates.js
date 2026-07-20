window.HermesSessions=window.HermesSessions||{};
window.HermesSessions.parts=window.HermesSessions.parts||{};
let _profileSwitchListEmbargo = false;
function _setProfileSwitchListEmbargo(on){ _profileSwitchListEmbargo = !!on; }
if(typeof window!=='undefined') window._setProfileSwitchListEmbargo = _setProfileSwitchListEmbargo;

function animateNextSessionListRefresh(options={}){
  _sessionListRefreshAnimationPending = true;
  if(options&&options.enterAll) _sessionListEnterAllAnimationPending = true;
}

// ── Loading skeletons (#4662 Phase 1) ───────────────────────────────────────
// Tracks whether the session list is currently showing a skeleton so a
// resolving render knows to replace it (and so we don't stack skeletons).
let _sessionListSkeletonActive = false;

// Skeleton structure mirrors a real sidebar: a couple of group headers
// (Pinned / Today / Last week) with single-line rows under each. Title widths
// vary so it reads as real conversations. `stamp:false` omits the timestamp bar
// on the occasional row (a real list mixes rows with/without a visible time).
const _SESSION_SKELETON_GROUPS = [
  {rows: [{title: 70}]},
  {rows: [{title: 84}, {title: 58}, {title: 76}]},
  {rows: [{title: 64}, {title: 90}, {title: 52}, {title: 72}]},
];

// Render a skeleton placeholder into #sessionList that mirrors the real row
// anatomy (group labels + single-line title bars with a short timestamp bar).
// Called the instant a profile switch begins so the user never sees the
// previous profile's conversations.
function showSessionListSkeleton(targetProfile){
  const list = $('sessionList');
  if(!list) return;
  // Tear down any active virtual-scroll state up front so a pending scroll-driven
  // render can't repaint the previous profile's cached rows over the skeleton
  // (#4662 Codex gate). Cancel the queued RAF and drop the data-session-virtual-*
  // window markers; the real render rebuilds them from the new payload. Done once
  // here so it applies to BOTH the content and empty-state skeleton branches.
  if(typeof _sessionVirtualScrollRaf!=='undefined'&&_sessionVirtualScrollRaf){
    cancelAnimationFrame(_sessionVirtualScrollRaf);
    _sessionVirtualScrollRaf=0;
  }
  delete list.dataset.sessionVirtualTotal;
  delete list.dataset.sessionVirtualStart;
  delete list.dataset.sessionVirtualEnd;
  delete list.dataset.sessionVirtualFilter;
  delete list.dataset.sessionVirtualActiveAnchor;
  // #4717: if we already know (from a prior render) the profile we're switching
  // INTO has zero conversations, a full content skeleton (group labels + 8 rows)
  // is misleading — it implies data that will never arrive, then resolves to an
  // empty list. Render a quiet empty-state placeholder instead. Only when the
  // count is KNOWN to be 0; an unknown profile (null) keeps the content skeleton
  // (safe default — never hide a skeleton for a profile that may have sessions).
  // Skip the empty branch while a project/source filter is active, since the
  // per-profile count is an unfiltered total and could be non-zero overall yet
  // empty under the filter (or vice-versa) — the content skeleton is the safe
  // choice there. typeof guards keep this safe if the helper isn't in scope.
  const knownCount = (typeof targetProfile === 'string' && targetProfile
      && typeof _knownSessionProfileCount === 'function')
    ? _knownSessionProfileCount(targetProfile) : null;
  const filterActive = (typeof _activeProject !== 'undefined' && _activeProject)
    || (typeof _sessionSourceFilter !== 'undefined' && _sessionSourceFilter === 'cli');
  const wrap = document.createElement('div');
  wrap.setAttribute('aria-hidden', 'true');
  if(knownCount === 0 && !filterActive){
    // A single faint placeholder bar rather than a "no conversations" text — the
    // real empty-state note paints the instant the (fast, empty) fetch resolves,
    // so we just hold a calm, content-free space in the meantime (no flash of a
    // fake list, no premature wording).
    wrap.className = 'skeleton-list skeleton-list-empty';
    const bar = document.createElement('div');
    bar.className = 'skeleton-empty-hint';
    wrap.appendChild(bar);
  } else {
    wrap.className = 'skeleton-list';
    let rowIndex = 0;
    for(const group of _SESSION_SKELETON_GROUPS){
      const label = document.createElement('div');
      label.className = 'skeleton-group-label';
      wrap.appendChild(label);
      for(const spec of group.rows){
        const row = document.createElement('div');
        row.className = 'skeleton-row';
        // Stagger the fade-in per row. Set inline (not via CSS :nth-child) because
        // group-label siblings are interleaved with rows, so a :nth-child stagger
        // would skip most rows. Cap so the longest list doesn't feel laggy.
        row.style.animationDelay = Math.min(rowIndex * 0.025, 0.2) + 's';
        rowIndex++;
        const title = document.createElement('div');
        title.className = 'skeleton-bar skeleton-title';
        title.style.width = spec.title + '%';
        const stamp = document.createElement('div');
        stamp.className = 'skeleton-bar skeleton-stamp';
        row.appendChild(title);
        row.appendChild(stamp);
        wrap.appendChild(row);
      }
    }
  }
  list.innerHTML = '';
  list.appendChild(wrap);
  list.scrollTop = 0;
  _sessionListSkeletonActive = true;
}

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
  for(const local of Array.isArray(_allSessions)?_allSessions:[]){
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
    _sessionListPointerActive ||
    pointerOverList ||
    (_sessionListLastScrollAt && now-_sessionListLastScrollAt<SESSION_LIST_INTERACTION_IDLE_MS)
  );
}

function _schedulePendingSessionListApply(){
  if(_pendingSessionListApplyTimer) clearTimeout(_pendingSessionListApplyTimer);
  _pendingSessionListApplyTimer=setTimeout(()=>{
    _pendingSessionListApplyTimer=0;
    if(!_pendingSessionListPayload) return;
    if(_isSessionListUserInteracting()){
      _schedulePendingSessionListApply();
      return;
    }
    const payload=_pendingSessionListPayload;
    _pendingSessionListPayload=null;
    if(payload.gen!==_renderSessionListGen) return;
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
  if(!_sessionAttentionSoundPrimed){
    _sessionAttentionSoundPrimed=true;
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
      _allSessions,
      _sidebarReferenceSessions,
      _allProjects,
      _activeSessionIdForSidebar(),
      search,
      _sessionSourceFilter,
      !!_sessionSelectMode,
      (window._sidebarDensity==='detailed'?'d':'c'),
      !!_showAllProfiles,
      _otherProfileCount,_archivedWebuiCount,_archivedCliCount,
      _serverWebuiSessionCount,_serverCliSessionCount,
    ]);
  }catch(_){ return null; }
}
function _applySessionListPayload(sessData, projData, opts){
  // Server's other_profile_count tells us how many sessions exist outside the
  // active profile so the "Show N from other profiles" toggle can render
  // without a second round-trip. Stashed on the module for renderSessionListFromCache.
  const applyOpts = (opts && typeof opts === 'object') ? opts : {};
  _otherProfileCount = sessData.other_profile_count || 0;
  _archivedWebuiCount = Number(sessData.archived_webui_count ?? sessData.archived_count ?? 0);
  _archivedCliCount = Number(sessData.archived_cli_count ?? 0);
  _serverWebuiSessionCount = Object.prototype.hasOwnProperty.call(sessData, 'webui_session_count')
    ? Number(sessData.webui_session_count)
    : null;
  _serverCliSessionCount = Object.prototype.hasOwnProperty.call(sessData, 'cli_session_count')
    ? Number(sessData.cli_session_count)
    : null;
  if (!Number.isFinite(_serverWebuiSessionCount)) _serverWebuiSessionCount = null;
  if (!Number.isFinite(_serverCliSessionCount)) _serverCliSessionCount = null;
  // Capture server clock for clock-skew compensation (issue #1144).
  // server_time is epoch seconds from the server's time.time().
  // _serverTimeDelta = client - server, so (Date.now() - _serverTimeDelta)
  // gives an approximation of the current server time.
  if (typeof sessData.server_time === 'number' && sessData.server_time > 0) {
    _serverTimeDelta = Date.now() - (sessData.server_time * 1000);
  }
  if (typeof sessData.server_tz === 'string') {
    _serverTz = sessData.server_tz;
  }
  const serverSessions=_optimisticallyRemovedSessionIds.size
    ? (sessData.sessions||[]).filter(s=>s&&!_optimisticallyRemovedSessionIds.has(s.session_id))
    : (sessData.sessions||[]);
  _sidebarReferenceSessions = Array.isArray(sessData.sidebar_reference_sessions)
    ? sessData.sidebar_reference_sessions
    : [];
  _reconcileActiveSessionIdleStateFromList(serverSessions);
  _allSessions = _mergeOptimisticFirstTurnSessions(serverSessions);
  // Tag the cache with the scope it was loaded under (active profile +
  // all-profiles flag). If a later /api/sessions fails right after a profile
  // switch, the catch path checks this so it won't re-render the PRIOR
  // profile's rows as if they were current (#4167 review item 3).
  _allSessionsScope = {
    profile: (typeof sessData.active_profile === 'string' && sessData.active_profile)
      ? sessData.active_profile
      : (S.activeProfile || 'default'),
    allProfiles: !!_showAllProfiles,
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
  const _recordFilterActive = (typeof _activeProject !== 'undefined' && _activeProject)
    || (typeof _sessionSourceFilter !== 'undefined' && _sessionSourceFilter === 'cli');
  if (!_showAllProfiles && !_recordFilterActive) {
    _recordSessionProfileCount(_allSessionsScope.profile, _allSessions.length);
  }
  _syncSessionAttentionSoundState(_allSessions);
  _pruneLineageReportCacheToVisibleSessions(_allSessions);
  _allProjects = projData.projects||[];
  // Capture the recovering-from-error state BEFORE clearing it: the error banner
  // DOM was rendered outside the signature path, so if this payload heals with
  // rows identical to the last render, the identical-signature skip below would
  // leave the stale "Could not load conversations" banner on screen. (Codex #5467)
  const _hadSessionListLoadError = !!_sessionListLoadError;
  _sessionListLoadError = null;
  _sessionListHasLoadedOnce = true;
  // Greptile #5975 P1: a /api/sessions request started under profile A can
  // finish after a switch to B already cleared A's cron markers. The list gen
  // check can already have passed (TOCTOU) or a deferred apply can land later.
  // Re-validate the profile-switch unread generation (shared with cron poll
  // reset via _cronPollGeneration) immediately before marking completions.
  const expectedUnreadGen = applyOpts.unreadGen;
  const currentUnreadGen = (typeof _cronPollGeneration === 'number') ? _cronPollGeneration : 0;
  if (typeof expectedUnreadGen !== 'number' || expectedUnreadGen === currentUnreadGen) {
    _markPollingCompletionUnreadTransitions(_allSessions);
  }
  const isStreaming = _allSessions.some(s => _isSessionEffectivelyStreaming(s));
  if (isStreaming) {
    startStreamingPoll();
  } else {
    stopStreamingPoll();
  }
  ensureSessionTimeRefreshPoll();
  ensureActiveSessionExternalRefreshPoll();
  if(!_sessionListFirstRenderAnimated&&Array.isArray(_allSessions)&&_allSessions.length){
    animateNextSessionListRefresh({enterAll:true});
    _sessionListFirstRenderAnimated=true;
  }
  ensureSessionEventsSSE();
  // #4671: this payload is the freshly-resolved /api/sessions response (and a superseded
  // response was already discarded by the generation guard upstream), so _allSessions now
  // holds the CURRENT profile's rows. Clear the skeleton flag right before painting so this
  // authoritative render replaces the profile-switch skeleton — while unrelated renders that
  // fire before this point stay blocked by the guard in renderSessionListFromCache().
  const _hadSessionListSkeleton = _sessionListSkeletonActive;
  _sessionListSkeletonActive = false;
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
  const _canRenderNow = !_renamingSid && !_sessionActionMenu;
  const _mustForceRender = _hadSessionListSkeleton || _hadSessionListLoadError;
  const _renderSig = _sessionListRenderSignature();
  if(_canRenderNow && !_mustForceRender && !_sessionListRefreshAnimationPending && _renderSig && _renderSig===_lastSessionListRenderSig){
    // Preserve the per-refresh INFLIGHT cleanup that renderSessionListFromCache
    // would otherwise perform, then skip only the DOM rebuild.
    if(typeof _purgeStaleInflightEntries==='function') _purgeStaleInflightEntries();
    return;
  }
  if(_canRenderNow) _lastSessionListRenderSig = _renderSig;
  renderSessionListFromCache();  // no-ops if rename is in progress
}

function _mergeRenderSessionListOptions(prev, next){
  const merged={...(prev||{}),...(next||{})};
  // Immediate refreshes must not be downgraded by a later passive polling tick.
  if((prev&&prev.deferWhileInteracting===false)||(next&&next.deferWhileInteracting===false)){
    merged.deferWhileInteracting=false;
  }
  return merged;
}

function _showSessionListLoadError(error){
  console.warn('renderSessionList',error);
  const isTimeout=Boolean(error&&(error.timeout===true||error.name==='TimeoutError'));
  // If this error is landing while a retry was in flight, flag the fresh Retry
  // button (rebuilt by the repaint) to reclaim keyboard focus so keyboard users
  // aren't dropped to <body> on a failed retry.
  const wasRetrying=Boolean(_sessionListLoadError&&_sessionListLoadError.retrying);
  _sessionListLoadError={
    message:isTimeout
      ? 'Session list is taking longer than expected.'
      : 'Could not load conversations.',
    detail:isTimeout
      ? 'The backend may still be scanning a very large session history.'
      : String(error&&error.message?error.message:''),
    _retryFailedFocus:wasRetrying,
  };
}

function _renderSessionListLoadErrorNote(){
  if(!_sessionListLoadError) return null;
  const note=document.createElement('div');
  note.className='session-list-error session-empty-note';
  // a11y: announce load-error / retry-failure transitions to screen readers
  // (the note is re-rendered on both the pending click and the failure repaint).
  note.setAttribute('role','status');
  note.setAttribute('aria-live','polite');
  const title=document.createElement('div');
  title.textContent=_sessionListLoadError.message||'Could not load conversations.';
  note.appendChild(title);
  if(_sessionListLoadError.detail){
    const detail=document.createElement('div');
    detail.className='session-list-error-detail';
    detail.textContent=_sessionListLoadError.detail;
    note.appendChild(detail);
  }
  const retry=document.createElement('button');
  retry.type='button';
  retry.className='session-list-error-retry';
  const retrying=Boolean(_sessionListLoadError.retrying);
  // Use aria-disabled (not the disabled property) for the pending state so the
  // button can keep keyboard focus across the sidebar rebuild; the click/keydown
  // guards below make it inert while busy.
  const setPending=()=>{
    retry.textContent='Retrying…';
    retry.setAttribute('aria-disabled','true');
    retry.setAttribute('aria-busy','true');
    retry.onclick=null;
  };
  const bindRetry=()=>{
    retry.onclick=(e)=>{
      e.stopPropagation();
      if(!_sessionListLoadError||_sessionListLoadError.retrying) return;
      if(retry.getAttribute('aria-disabled')==='true') return;
      setPending();
      _sessionListLoadError={..._sessionListLoadError,retrying:true};
      renderSessionListFromCache();
      void renderSessionList({deferWhileInteracting:false}).finally(()=>{
        if(!retry.parentNode||(_sessionListLoadError&&_sessionListLoadError.retrying)) return;
        retry.textContent='Retry';
        retry.removeAttribute('aria-disabled');
        retry.removeAttribute('aria-busy');
        bindRetry();
      });
    };
  };
  if(retrying){
    setPending();
  }else{
    retry.textContent='Retry';
    retry.removeAttribute('aria-disabled');
    bindRetry();
    // On a failure repaint that replaces a pending button, restore keyboard
    // focus to the fresh Retry button so keyboard users aren't dropped to body.
    if(_sessionListLoadError._retryFailedFocus){
      delete _sessionListLoadError._retryFailedFocus;
      const _refocus=()=>{ try{ if(typeof retry.focus==='function') retry.focus(); }catch(_e){} };
      if(typeof requestAnimationFrame==='function') requestAnimationFrame(_refocus); else _refocus();
    }
  }
  note.appendChild(retry);
  return note;
}

async function _runRenderSessionListRefresh(opts, _gen){
  const deferWhileInteracting=Boolean(opts&&opts.deferWhileInteracting);
  if(!deferWhileInteracting) _pendingSessionListPayload=null;
  // Capture profile-switch unread generation BEFORE the await so a switch
  // mid-flight (which increments _cronPollGeneration) invalidates completion
  // marking for this response even if list gen checks already passed.
  const unreadGen = (typeof _cronPollGeneration === 'number') ? _cronPollGeneration : 0;
  try{
    if(!($('sessionSearch').value||'').trim()) _contentSearchResults = [];
    const sessionListQS = _sessionListQueryString();
    // #5394: the sidebar session-list GET is idempotent, so 502/503/504 retry
    // must be unconditional. Previously retries/retryStatuses were boot-gated, so
    // a transient 502 during an nginx->backend restart on a warm refresh (profile
    // switch, focus/visible/reconnect) failed on the first attempt and left the
    // sidebar stale until a hard reload. Boot still keeps the larger timeout +
    // timeout retry; every refresh now retries the transient upstream statuses.
    const sessionRequestOpts={
      timeoutToast:false,
      retries:1,
      retryStatuses:[502,503,504],
    };
    if(!_sessionListHasLoadedOnce){
      sessionRequestOpts.timeoutMs=_SESSION_LIST_BOOT_TIMEOUT_MS;
      sessionRequestOpts.retryTimeouts=true;
    }
    const {sessData, projData}=await _loadSidebarSessionListPayload(sessionListQS, sessionRequestOpts);
    // Discard stale response — a newer renderSessionList() call superseded us.
    if (_gen !== _renderSessionListGen) return;
    // #4671: while a profile switch is mid-flight, drop ANY payload — even one whose
    // generation still matches — because a render that STARTED after the skeleton showed
    // but before the switch response set the new-profile cookie fetched the OLD profile's
    // rows. The switch clears the embargo immediately before its own (authoritative)
    // renderSessionList(), so that render's payload is the first allowed to paint.
    if (_profileSwitchListEmbargo) return;
    if(deferWhileInteracting&&_isSessionListUserInteracting()){
      _pendingSessionListPayload={gen:_gen,sessData,projData,unreadGen};
      _schedulePendingSessionListApply();
      return;
    }
    _applySessionListPayload(sessData,projData,{unreadGen});
  }catch(e){
    if (_gen !== _renderSessionListGen) return;
    // #4671: same embargo guard as the success path — a mid-switch /api/sessions that
    // FAILS must not clear the skeleton flag or render the old-profile cache either. The
    // switch-owned render (after the embargo lifts) is the only one allowed to resolve the
    // skeleton; if the switch itself fails, its catch clears the skeleton + embargo.
    if (_profileSwitchListEmbargo) return;
    _showSessionListLoadError(e);
    // Only fall back to the cached rows if they were loaded under the SAME
    // scope we're requesting now. After a profile switch the cache holds the
    // PRIOR profile's sessions; re-rendering them would falsely show another
    // profile's conversations, so render the error state with no rows instead
    // (#4167 review item 3).
    const _curScope = {
      profile: S.activeProfile || 'default',
      allProfiles: !!_showAllProfiles,
      sidebarSource: _requestedSessionSidebarSource(),
      excludeHidden: _sessionListExcludeHiddenEnabled(),
    };
    const _scopeMatches = _allSessionsScope
      && _allSessionsScope.profile === _curScope.profile
      && _allSessionsScope.allProfiles === _curScope.allProfiles
      && _allSessionsScope.sidebarSource === _curScope.sidebarSource
      && _allSessionsScope.excludeHidden === _curScope.excludeHidden;
    // #4671: the /api/sessions fetch failed — clear the skeleton flag so this error
    // render (matched cache, or empty rows for a mismatched scope) replaces the
    // up-front profile-switch skeleton instead of stranding it.
    _sessionListSkeletonActive = false;
    if (_scopeMatches) {
      renderSessionListFromCache();
    } else {
      _allSessions = [];
      _sidebarReferenceSessions = [];
      _allSessionsScope = _curScope;
      _clearSessionSourceTabCounts();
      renderSessionListFromCache();
    }
  }
}

async function _loadSidebarSessionListPayload(sessionListQS, sessionRequestOpts){
  const projectPromise = (async() => {
    try{
      const projectQS = _showAllProfiles ? '?all_profiles=1' : '';
      return await api('/api/projects' + projectQS,{timeoutToast:false});
    }catch(projectError){
      console.warn('renderProjectsList',projectError);
      return {projects:_allProjects||[]};
    }
  })();

  const sessData = await api('/api/sessions' + sessionListQS,sessionRequestOpts);
  const projData = await projectPromise;

  return {sessData,projData};
}

async function _drainRenderSessionListQueue(initialRequest){
  let request=initialRequest;
  try{
    while(request){
      await _runRenderSessionListRefresh(request.opts, request.gen);
      request=_renderSessionListQueuedRequest;
      _renderSessionListQueuedRequest=null;
    }
  }finally{
    _renderSessionListInFlight=null;
    if(_renderSessionListQueuedRequest){
      const next=_renderSessionListQueuedRequest;
      _renderSessionListQueuedRequest=null;
      _renderSessionListInFlight=_drainRenderSessionListQueue(next);
    }
  }
}

async function renderSessionList(opts={}){
  const request={opts:opts||{},gen:++_renderSessionListGen};
  if(_renderSessionListInFlight){
    _renderSessionListQueuedRequest={
      opts:_mergeRenderSessionListOptions(_renderSessionListQueuedRequest&&_renderSessionListQueuedRequest.opts, request.opts),
      gen:request.gen,
    };
    return _renderSessionListInFlight;
  }
  _renderSessionListInFlight=_drainRenderSessionListQueue(request);
  return _renderSessionListInFlight;
}

// ── Gateway session SSE (real-time sync for agent sessions) ──
let _gatewaySSE = null;
let _gatewayPollTimer = null;
let _gatewayProbeInFlight = false;
let _gatewaySSEWarningShown = false;
const _gatewayFallbackPollMs = 30000;
const _streamingPollMs = 30000;
const _sessionTimeRefreshMs = 60000;
// #3107: the active-session "is it externally updated?" poll used to fire
// every 5 s. On long sessions this caused visible scroll jitter and a
// noticeable network/CPU floor because the SSE session-events stream
// already pushes invalidations in real time; this poll exists only as a
// fallback for the case where SSE is broken/unavailable. Bump to 30 s
// to keep the safety net without turning it into a primary refresh path.
const _activeSessionExternalRefreshMs = 30000;
let _streamingPollTimer = null;
let _sessionTimeRefreshTimer = null;
let _streamingPollVisibilityHandler = null;
let _sessionTimeRefreshVisibilityHandler = null;
let _activeSessionExternalRefreshTimer = null;
let _activeSessionExternalRefreshInFlight = false;
let _deferredActiveSessionExternalRefreshReason = '';
let _sessionEventsSSE = null;
let _sessionEventsRefreshTimer = 0;
let _sessionEventsRefreshPendingRequest = null;
let _sessionEventsReconnectTimer = 0;
let _sessionEventsNeedsRefreshOnOpen = false;
let _sessionEventsReconnectAttempt = 0;
const _sessionEventsReconnectBaseMs = 5000;
const _sessionEventsReconnectMaxMs = 30000;

function _sessionEventsReconnectDelayMs(){
  const attempt = Math.max(0, Number(_sessionEventsReconnectAttempt || 0));
  const base = Math.min(_sessionEventsReconnectMaxMs, _sessionEventsReconnectBaseMs * Math.pow(2, attempt));
  const jitter = Math.floor(Math.random() * Math.max(1, Math.floor(base * 0.35)));
  return Math.min(_sessionEventsReconnectMaxMs, Math.floor(base * 0.75) + jitter);
}
let _sessionListRefreshInFlight = false;
let _sessionListRefreshPendingRequest = null;

function _mergeSessionListRefreshOptions(prev, next){
  const merged = {...(prev||{}), ...(next||{})};
  if((prev&&prev.force===true)||(next&&next.force===true)) merged.force = true;
  if((prev&&prev.refreshActive===true)||(next&&next.refreshActive===true)) merged.refreshActive = true;
  return merged;
}

function _refreshSessionListAfterSidebarResume(reason){
  // A direct resume refresh satisfies any pending onopen catch-up from the same close.
  _sessionEventsNeedsRefreshOnOpen = false;
  void refreshSessionList(reason, {force:true});
}

function startStreamingPoll(){
  if(_streamingPollTimer) return;
  _streamingPollTimer = setInterval(() => {
    // Skip while the tab is hidden: this poll fetches /api/sessions and rebuilds
    // the sidebar, work the user cannot see. The visibilitychange handler below
    // brings the list current the moment the tab is shown again, so no update is
    // lost — the background tab just stops burning network + DOM churn.
    if(typeof document !== 'undefined' && document.hidden) return;
    void renderSessionList({deferWhileInteracting:true});
  }, _streamingPollMs);
  if(typeof document !== 'undefined' && !_streamingPollVisibilityHandler){
    _streamingPollVisibilityHandler = () => {
      if(!document.hidden) void renderSessionList({deferWhileInteracting:true});
    };
    document.addEventListener('visibilitychange', _streamingPollVisibilityHandler);
  }
}

function stopStreamingPoll(){
  if(_streamingPollVisibilityHandler && typeof document !== 'undefined'){
    document.removeEventListener('visibilitychange', _streamingPollVisibilityHandler);
    _streamingPollVisibilityHandler = null;
  }
  if(!_streamingPollTimer) return;
  clearInterval(_streamingPollTimer);
  _streamingPollTimer = null;
}

function ensureSessionTimeRefreshPoll(){
  if(_sessionTimeRefreshTimer) return;
  _sessionTimeRefreshTimer = setInterval(() => {
    // Relative-time labels only matter when visible; the visibilitychange
    // handler below refreshes timestamps immediately when the tab is shown.
    if(typeof document !== 'undefined' && document.hidden) return;
    renderSessionListFromCache();
  }, _sessionTimeRefreshMs);
  if(typeof document !== 'undefined' && !_sessionTimeRefreshVisibilityHandler){
    _sessionTimeRefreshVisibilityHandler = () => {
      if(!document.hidden) renderSessionListFromCache();
    };
    document.addEventListener('visibilitychange', _sessionTimeRefreshVisibilityHandler);
  }
}

function _deferActiveSessionExternalRefresh(reason){
  const nextReason = reason || 'poll';
  if(_deferredActiveSessionExternalRefreshReason==='idle-reconcile'&&nextReason==='poll') return;
  _deferredActiveSessionExternalRefreshReason = nextReason;
}

function _clearDeferredActiveSessionExternalRefresh(){
  _deferredActiveSessionExternalRefreshReason = '';
}

function _flushDeferredActiveSessionExternalRefresh(){
  const reason = _deferredActiveSessionExternalRefreshReason;
  if(!reason) return;
  _deferredActiveSessionExternalRefreshReason = '';
  void refreshActiveSessionIfExternallyUpdated(reason);
}

// Reconcile the active session against server-side metadata. Returns a status
// string so callers (notably the post-stream idle reconcile) can decide how to
// react:
//   'skipped'   — a guard short-circuited before any network probe ran
//   'unchanged' — server metadata matched local (or only a non-transcript bump)
//   'reloaded'  — the transcript was force-reloaded to re-sync
//   'failed'    — the probe request threw (transient); caller may fall back
//
// opts.ignoreStreamJustFinished — bypass the post-stream cooldown. Only the
//   idle-reconcile path sets this: it runs once for the just-finished active
//   turn and probes server metadata FIRST (reloading only on an actual count
//   change), so it is safe to look even right after the "done" event without
//   the unconditional force reload that produced the mobile-PWA end-of-turn
//   flash (#3976). This intentionally COEXISTS with the #3916/#4195 poll-only
//   external gate below, which is untouched.
async function refreshActiveSessionIfExternallyUpdated(reason){
  // opts read via arguments[1] (same pattern as loadSession) so the public
  // signature stays (reason) — callers like the poll/focus/visibility hooks and
  // refreshSessionList keep passing a single reason. Only the post-stream idle
  // reconcile passes opts (see _scheduleActiveSessionIdleReload).
  const opts = arguments[1] || {};
  if(_activeSessionExternalRefreshInFlight) return 'skipped';
  if(!S.session || !S.session.session_id) return 'skipped';
  if(S.busy || S.activeStreamId) return 'skipped';
  if(typeof _isMessageReaderUnpinned==='function'&&_isMessageReaderUnpinned()){
    _deferActiveSessionExternalRefresh(reason||'poll');
    return 'skipped';
  }
  // #3916/#4195: the 30s timer is only a fallback for imported/external sessions.
  // WebUI-native sessions should not keep probing forever when the sidebar SSE
  // is healthy, but they still must reconcile when an actual sessions_changed
  // event, focus, or visibility recovery says another client/process mutated
  // the active transcript (#4205 follow-up shape). The idle-reconcile path uses
  // a non-'poll' reason, so it already sails through this gate untouched.
  if((reason||'poll')==='poll' && !_isExternalSession(S.session)) return 'skipped';
  // Cooldown: don't force-reload immediately after streaming ends — the
  // "done" event already delivered the final messages. Reloading here would
  // clear S.toolCalls and lose Activity. The idle-reconcile path may bypass
  // this guard (opts.ignoreStreamJustFinished) because it probes server
  // metadata first and only reloads when the count actually changed (#3976).
  if(!opts.ignoreStreamJustFinished && typeof window !== 'undefined' && window._streamJustFinished) return 'skipped';
  if(typeof document !== 'undefined' && document.hidden) return 'skipped';
  const sid = S.session.session_id;
  const localCount = Number(S.session.message_count || (Array.isArray(S.messages)?S.messages.length:0) || 0);
  const localLast = Number(S.session.last_message_at || S.session.updated_at || 0);
  _activeSessionExternalRefreshInFlight = true;
  try{
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`,{timeoutToast:false});
    if(!data || !data.session) return 'unchanged';
    if(!S.session || S.session.session_id !== sid) return 'skipped';
    if(S.busy || S.activeStreamId) return 'skipped';
    const remoteCount = Number(data.session.message_count || 0);
    const remoteLast = Number(data.session.last_message_at || data.session.updated_at || 0);
    // Force-reload the whole transcript whenever the visible conversation's
    // message count CHANGED in either direction. A higher count means new
    // messages; a LOWER count means another tab/client truncated, undid,
    // retried, or regenerated the transcript (/api/session/truncate, /retry,
    // /undo all shrink s.messages and write a lower message_count) — both must
    // re-sync or this tab silently keeps a stale transcript.
    //
    // A bump in last_message_at WITHOUT a count change means a non-transcript
    // write touched the session — most commonly the post-turn background
    // skill/memory review, which rewrites memory/skills and advances updated_at
    // but adds no chat messages. Reloading on that bump tears down and re-fetches
    // the transcript: loadSession(force) clears S.messages and awaits a
    // round-trip before re-rendering, so the whole conversation visibly
    // disappears and "reappears a moment later" with no new content. Skip the
    // destructive reload in that case and just refresh the lightweight sidebar
    // list metadata, advancing the local last-seen marker so the same metadata
    // bump doesn't re-trigger on every subsequent poll.
    if(remoteCount !== localCount){
      // Hidden-tab return / visibility / focus recovery commonly trips
      // remoteCount !== localCount when the post-turn bg-review thread or a
      // sibling tab persisted messages while the tab was hidden. The default
      // loadSession(force) path clears S.messages synchronously and waits for
      // the full transcript round-trip before re-rendering, producing the
      // user-visible "everything disappears, then reappears after a moment"
      // gap that #5061 (metadata-only) and #5122 (SSE 4-probe) DO NOT cover
      // (#5177). Pass keepStaleUntilLoaded so the destructive clear is
      // deferred to swap-in-place when the new transcript actually arrives.
      // Restrict to the recovery reasons that produced the field repro; the
      // post-stream idle reconcile and external/imported-session polls keep
      // the original behaviour (no DOM is on-screen long enough for the gap
      // to matter, and any change there would have to re-verify their own
      // tradeoffs).
      const _recoveryReasons = {visible:true, focus:true};
      const _keepStaleUntilLoaded = !!_recoveryReasons[String(reason||'')];
      // #5409: skip force-reload while a different session's loadSession()
      // is in flight — avoids overwriting _loadingSessionId and silently
      // cancelling an in-progress session switch. All four call paths
      // (idle-reconcile, poll, visibility, focus) funnel through here.
      if(typeof _loadingSessionId !== 'undefined' && _loadingSessionId && _loadingSessionId !== sid) return 'skipped';
      await loadSession(sid, {force:true, externalRefreshReason:reason||'poll', keepStaleUntilLoaded:_keepStaleUntilLoaded});
      if(typeof renderSessionList==='function') void renderSessionList();
      return 'reloaded';
    }else if(remoteLast > localLast){
      if(S.session && S.session.session_id === sid){
        S.session.last_message_at = remoteLast;
        if(data.session.updated_at) S.session.updated_at = data.session.updated_at;
      }
      if(typeof renderSessionList==='function') void renderSessionList();
    }
    return 'unchanged';
  }catch(e){
    // Ignore transient refresh failures; the next poll/focus event will retry.
    return 'failed';
  }finally{
    _activeSessionExternalRefreshInFlight = false;
  }
}

function ensureActiveSessionExternalRefreshPoll(){
  if(_activeSessionExternalRefreshTimer) return;
  _activeSessionExternalRefreshTimer = setInterval(() => {
    void refreshActiveSessionIfExternallyUpdated('poll');
  }, _activeSessionExternalRefreshMs);
  if(typeof document !== 'undefined' && !document._hermesExternalRefreshVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(!document.hidden) void refreshActiveSessionIfExternallyUpdated('visible');
    });
    document._hermesExternalRefreshVisibilityHook = true;
  }
  if(typeof window !== 'undefined' && !window._hermesExternalRefreshFocusHook){
    window.addEventListener('focus', () => { void refreshActiveSessionIfExternallyUpdated('focus'); });
    window._hermesExternalRefreshFocusHook = true;
  }
}

async function refreshSessionList(reason='manual', opts={}){
  const force = !!(opts && opts.force);
  const refreshActive = !!(opts && opts.refreshActive);
  if(!force && typeof document !== 'undefined' && document.hidden) return;
  if(_sessionListRefreshInFlight){
    _sessionListRefreshPendingRequest = {
      reason: reason || 'session-list',
      opts:_mergeSessionListRefreshOptions(_sessionListRefreshPendingRequest && _sessionListRefreshPendingRequest.opts, opts),
    };
    return;
  }
  _sessionListRefreshInFlight = true;
  try{
    await renderSessionList({deferWhileInteracting:!force});
    if(refreshActive) await refreshActiveSessionIfExternallyUpdated(reason||'session-list');
  }finally{
    _sessionListRefreshInFlight = false;
    const pendingRequest = _sessionListRefreshPendingRequest;
    _sessionListRefreshPendingRequest = null;
    if(pendingRequest) _scheduleSessionEventsRefresh(pendingRequest.reason, pendingRequest.opts);
  }
}

function _scheduleSessionEventsRefresh(reason, opts={}){
  _sessionEventsRefreshPendingRequest = {
    reason: reason || (_sessionEventsRefreshPendingRequest && _sessionEventsRefreshPendingRequest.reason) || 'event',
    opts:_mergeSessionListRefreshOptions(_sessionEventsRefreshPendingRequest && _sessionEventsRefreshPendingRequest.opts, opts),
  };
  if(_sessionEventsRefreshTimer) return;
  _sessionEventsRefreshTimer = setTimeout(() => {
    _sessionEventsRefreshTimer = 0;
    const request = _sessionEventsRefreshPendingRequest || {reason:'event', opts:{}};
    _sessionEventsRefreshPendingRequest = null;
    void refreshSessionList(request.reason||'event', request.opts);
  }, 300);
}

function _sessionEventTargetsActiveSession(payload){
  const eventSessionId = payload && typeof payload.session_id === 'string' ? payload.session_id : '';
  if(!eventSessionId) return false;
  return !!(S.session && S.session.session_id && S.session.session_id === eventSessionId);
}

// ── #4151: focus-aware close for the two GLOBAL sidebar SSE streams ──────────
// Each WebUI window holds up to three persistent SSE connections (session-events
// + gateway + the per-session stream). #3992/#3996 close them on the Page
// Visibility API (`visibilitychange` / `document.hidden`) so a hidden tab frees
// HTTP/1.1 pool slots. But a PWA *standalone* window does NOT reliably fire
// `visibilitychange` when it merely loses focus to another window of the same
// app — `document.hidden` only flips on minimize. So two side-by-side PWA windows
// both stay `visibilityState==='visible'`, each keeps its sidebar streams open,
// and 2x3 = 6 = the per-origin HTTP/1.1 connection limit; every later fetch()
// (the 30s polls) queues behind the saturated pool and times out (#4151).
// `document.hasFocus()` is the signal `visibilitychange` misses — only one window
// holds focus at a time.
//
// Scope: ONLY the two global sidebar streams (session-events + gateway). The
// per-session live stream (messages.js `startSessionStream`) deliberately stays
// visibility-only — it carries live `bg_task_complete` toasts and
// `server_turn_started` live-view that an unfocused-but-VISIBLE window must still
// receive (the OS-notification path is gated on `document.hidden`, so the in-app
// toast is the only completion signal a visible-unfocused window gets). Closing
// it on blur would regress the multi-window live-view UX.
function _sidebarSseBackgrounded(){
  if(typeof document === 'undefined') return false;
  if(document.hidden) return true;
  if(typeof document.hasFocus === 'function' && !document.hasFocus()) return true;
  return false;
}

let _sidebarSseBlurCloseTimer = 0;
// Debounce the blur-close so a transient blur (native dialog, quick alt-tab and
// back) doesn't thrash the streams; a sustained blur frees the pool slots.
const _SIDEBAR_SSE_BLUR_CLOSE_MS = 1000;

function _installSidebarSseFocusHook(){
  if(typeof window === 'undefined' || typeof document === 'undefined') return;
  if(document._hermesSidebarSseFocusHook) return;
  document._hermesSidebarSseFocusHook = true;
  window.addEventListener('blur', () => {
    if(_sidebarSseBlurCloseTimer) return;
    _sidebarSseBlurCloseTimer = setTimeout(() => {
      _sidebarSseBlurCloseTimer = 0;
      // Re-check at fire time — focus may have returned during the debounce.
      if(_sidebarSseBackgrounded()){
        _closeSessionEventsSSE();
        stopGatewaySSE();
      }
    }, _SIDEBAR_SSE_BLUR_CLOSE_MS);
  });
  window.addEventListener('focus', () => {
    if(_sidebarSseBlurCloseTimer){ clearTimeout(_sidebarSseBlurCloseTimer); _sidebarSseBlurCloseTimer = 0; }
    // Reopen and catch up on anything missed while blurred. ensureSessionEventsSSE()
    // is idempotent (`if(_sessionEventsSSE) return`), but startGatewaySSE() is NOT — it
    // begins with an unconditional stopGatewaySSE(). So only reopen the gateway when it
    // was actually closed; otherwise a transient blur shorter than the debounce (where
    // the blur-close timer was cleared and the stream was never torn down) would
    // drop+reconnect the live gateway on every window switch, cancelling its poll
    // fallback and resetting probe/warning state — the exact thrash the debounce exists
    // to prevent, in the multi-window scenario this fix targets (#4151).
    ensureSessionEventsSSE();
    if(!_gatewaySSE) startGatewaySSE();
    void _refreshSessionListAfterSidebarResume('focus');
  });
}

function _closeSessionEventsSSE(){
  if(_sessionEventsSSE){
    try{if(_sessionEventsSSE.readyState!==2)_sessionEventsSSE.close();}catch(_){ }
    _sessionEventsSSE = null;
    _sessionEventsNeedsRefreshOnOpen = true;
  }
}

function ensureSessionEventsSSE(){
  if(typeof document !== 'undefined' && !document._hermesSessionEventsVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(document.hidden){
        _closeSessionEventsSSE();
      }else{
        ensureSessionEventsSSE();
        void _refreshSessionListAfterSidebarResume('visible');
      }
    });
    document._hermesSessionEventsVisibilityHook = true;
  }
  _installSidebarSseFocusHook();
  if(typeof EventSource==='undefined') return;
  if(_sidebarSseBackgrounded()) return;
  if(_sessionEventsSSE) return;
  try{
    // Same-origin relative URL preserves subpath mounts and normal WebUI cookies.
    _sessionEventsSSE = new EventSource('api/sessions/events');
    _sessionEventsSSE.onopen = () => {
      _sessionEventsReconnectAttempt = 0;
      if(!_sessionEventsNeedsRefreshOnOpen) return;
      _sessionEventsNeedsRefreshOnOpen = false;
      void _refreshSessionListAfterSidebarResume('reconnect');
    };
    _sessionEventsSSE.addEventListener('sessions_changed', (ev) => {
      const activeProfile = S.activeProfile || 'default';
      let eventTargetsActiveSession = false;
      try {
        const payload = typeof ev?.data === 'string' ? JSON.parse(ev.data) : {};
        const eventProfile = payload && typeof payload.profile === 'string' ? payload.profile : '';
        if (!_sessionEventProfilesMatch(eventProfile, activeProfile)) {
          return;
        }
        eventTargetsActiveSession = _sessionEventTargetsActiveSession(payload);
      } catch (_err) {
        // Non-JSON payload (or transient malformed event). Keep legacy behavior:
        // refresh once event was seen.
      }
      _scheduleSessionEventsRefresh(eventTargetsActiveSession?'event-active-session':'event', {force:true, refreshActive:true});
    });
    _sessionEventsSSE.onerror = () => {
      _sessionEventsNeedsRefreshOnOpen = true;
      _closeSessionEventsSSE();
      if(_sessionEventsReconnectTimer) return;
      const delayMs = _sessionEventsReconnectDelayMs();
      _sessionEventsReconnectAttempt = Math.min(_sessionEventsReconnectAttempt + 1, 6);
      _sessionEventsReconnectTimer = setTimeout(() => {
        _sessionEventsReconnectTimer = 0;
        ensureSessionEventsSSE();
      }, delayMs);
    };
  }catch(e){
    _closeSessionEventsSSE();
  }
}

if(typeof window!=='undefined') window.refreshSessionList = refreshSessionList;

let _gatewayPollVisibilityHandler = null; // saved so stopGatewayPollFallback can remove it

function startGatewayPollFallback(ms){
  const intervalMs = Math.max(5000, Number(ms) || _gatewayFallbackPollMs);
  if(_gatewayPollTimer) clearInterval(_gatewayPollTimer);
  _gatewayPollTimer = setInterval(() => {
    // Skip poll when tab is hidden or a stream is active — saves CPU
    // and avoids redundant DOM renders during active streaming (#4704).
    if(typeof document !== 'undefined' && document.hidden) return;
    if(typeof S !== 'undefined' && (S.busy || S.activeStreamId)) return;
    renderSessionList({deferWhileInteracting:true});
  }, intervalMs);
  // Visibility catch-up: refresh immediately when tab re-gains focus,
  // so no gateway updates are dropped during hidden-skip periods.
  // Save the handler so stopGatewayPollFallback can removeEventListener it (#4730 review).
  if(typeof document !== 'undefined' && !_gatewayPollVisibilityHandler){
    _gatewayPollVisibilityHandler = () => {
      if(!document.hidden && typeof renderSessionList === 'function'){
        void renderSessionList({deferWhileInteracting:false});
      }
    };
    document.addEventListener('visibilitychange', _gatewayPollVisibilityHandler);
  }
}

function stopGatewayPollFallback(){
  if(_gatewayPollTimer){
    clearInterval(_gatewayPollTimer);
    _gatewayPollTimer = null;
  }
  if(_gatewayPollVisibilityHandler && typeof document !== 'undefined'){
    document.removeEventListener('visibilitychange', _gatewayPollVisibilityHandler);
    _gatewayPollVisibilityHandler = null;
  }
}

function _gatewaySessionSnapshotKey(sessions){
  return (Array.isArray(sessions)?sessions:[])
    .filter(s=>s&&s.session_id)
    .map(s=>`${s.session_id}:${s.updated_at||0}:${s.message_count||0}`)
    .sort()
    .join('|');
}

function _isGatewaySessionForSnapshot(session){
  if(!session) return false;
  if(typeof _isCliSession==='function'&&_isCliSession(session)) return true;
  if(typeof _isMessagingSession==='function'&&_isMessagingSession(session)) return true;
  const source=String(session.session_source||session.raw_source||session.source_tag||session.source||'').toLowerCase();
  return !!source&&source!=='webui';
}

function _isDuplicateGatewaySessionSnapshot(sessions){
  const incoming=(Array.isArray(sessions)?sessions:[]).filter(_isGatewaySessionForSnapshot);
  const currentGatewaySessions=(Array.isArray(_allSessions)?_allSessions:[]).filter(_isGatewaySessionForSnapshot);
  if(!incoming.length&&!currentGatewaySessions.length) return true;
  return _gatewaySessionSnapshotKey(incoming)===_gatewaySessionSnapshotKey(currentGatewaySessions);
}

async function probeGatewaySSEStatus(){
  if(_gatewayProbeInFlight || !window._showCliSessions) return;
  _gatewayProbeInFlight = true;
  try{
    const resp = await fetch(new URL('api/sessions/gateway/stream?probe=1', document.baseURI || location.href).href, { credentials:'same-origin' });
    const data = await resp.json().catch(() => ({}));
    if(resp.ok && data.watcher_running){
      stopGatewayPollFallback();
      _gatewaySSEWarningShown = false;
      if(!_gatewaySSE && typeof EventSource!=='undefined' && !(document&&document.hidden)) startGatewaySSE();
      return;
    }
    if(resp.status === 503 || data.watcher_running === false){
      startGatewayPollFallback(data.fallback_poll_ms || _gatewayFallbackPollMs);
      renderSessionList({deferWhileInteracting:true});
      if(!_gatewaySSEWarningShown && typeof showToast === 'function'){
        showToast('Gateway sync unavailable — falling back to periodic refresh.', 5000);
        _gatewaySSEWarningShown = true;
      }
    }
  }catch(e){
    // Network error during probe — server may be unreachable.
    // Start fallback polling as a safe default; it will self-cancel
    // when the SSE connection recovers and sessions_changed fires.
    startGatewayPollFallback(_gatewayFallbackPollMs);
    renderSessionList({deferWhileInteracting:true});
  }finally{
    _gatewayProbeInFlight = false;
  }
}

function startGatewaySSE(){
  stopGatewaySSE();
  if(!window._showCliSessions) return;
  // Visibility hook (install once) — mirror ensureSessionEventsSSE() pattern
  if(typeof document !== 'undefined' && !document._hermesGatewaySSEVisibilityHook){
    document.addEventListener('visibilitychange', () => {
      if(document.hidden){
        stopGatewaySSE();
      }else{
        void startGatewaySSE();
      }
    });
    document._hermesGatewaySSEVisibilityHook = true;
  }
  _installSidebarSseFocusHook();
  // Don't open when tab is hidden OR the window has lost focus (PWA blur) —
  // saves connection pool slots (#4151).
  if(_sidebarSseBackgrounded()) return;
  try{
    _gatewaySSE = new EventSource('api/sessions/gateway/stream');
    _gatewaySSE.addEventListener('sessions_changed', (ev) => {
      try{
        const data = JSON.parse(ev.data);
        if(data.sessions){
          stopGatewayPollFallback();
          _gatewaySSEWarningShown = false;
          if(!_isDuplicateGatewaySessionSnapshot(data.sessions)){
            renderSessionList({deferWhileInteracting:true}); // re-fetch and re-render
          }
          // If the active session received new gateway messages, refresh the conversation view.
          // S.busy check prevents stomping on an in-progress WebUI response.
          // _isExternalSession covers CLI-originated and messaging-source sessions
          // that need a server-side import before WebUI can read them.
          if(S.session && !S.busy && _isExternalSession(S.session)){
            const changedIds = new Set((data.sessions||[]).map(s=>s.session_id));
            if(changedIds.has(S.session.session_id)){
              // Capture active session ID before async fetch — race guard.
              // If the user switches sessions while the fetch is in-flight, discard the result.
              const activeSid = S.session.session_id;
              api('/api/session/import_cli',{method:'POST',body:JSON.stringify(_externalImportPayload(S.session))})
                .then(res=>{
                  if(!S.session || S.session.session_id !== activeSid) return;
                  if(res && res.session && Array.isArray(res.session.messages)){
                    const prev = S.messages.length;
                    const next = res.session.messages.filter(m => m && m.role);
                    if (next.length < prev) return;
                    if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;
                    // Carry forward ephemeral turn fields (_turnUsage/
                    // _turnDuration/_turnTps/_gatewayRouting/_statusCard/
                    // _anchor_stream_id) so
                    // gateway-driven CLI refreshes do not drop the badge.
                    let _nextToAssign = next;
                    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
                      _nextToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], next);
                    }
                    S.messages = _nextToAssign;
                    if(S.session && S.session.session_id === activeSid){
                      S.session.message_count = next.length;
                      const newest = next.length ? next[next.length - 1] : null;
                      const newestTs = Number((newest && (newest.timestamp || newest._ts)) || 0);
                      if(newestTs){
                        S.session.last_message_at = newestTs;
                        S.session.updated_at = newestTs;
                      }
                    }
                    if(S.messages.length !== prev){
                      renderMessages({preserveScroll:true});
                      if(typeof highlightCode==='function') highlightCode();
                    }
                  }
                })
                .catch(()=>{ /* ignore — next poll will retry */ });
            }
          }
        }
      }catch(e){ /* ignore parse errors */ }
    });
    _gatewaySSE.onerror = () => {
      if(typeof recordClientSSEError==='function') recordClientSSEError('gateway-sessions',{ready_state:_gatewaySSE?_gatewaySSE.readyState:null,reason:'gateway EventSource.onerror'});
      if(_gatewaySSE){
        try{if(_gatewaySSE.readyState!==2)_gatewaySSE.close();}catch(_){ }
        _gatewaySSE = null;
      }
      void probeGatewaySSEStatus();
    };
  }catch(e){
    void probeGatewaySSEStatus();
  }
}

function stopGatewaySSE(){
  if(_gatewaySSE){
    try{if(_gatewaySSE.readyState!==2)_gatewaySSE.close();}catch(_){ }
    _gatewaySSE = null;
  }
  stopGatewayPollFallback();
  _gatewayProbeInFlight = false;
  _gatewaySSEWarningShown = false;
}

window.HermesSessions.parts.listUpdates=Object.freeze({showSkeleton:showSessionListSkeleton,applyPayload:_applySessionListPayload,render:renderSessionList,refresh:refreshSessionList,startPolling:startStreamingPoll,stopPolling:stopStreamingPoll,startGateway:startGatewaySSE,stopGateway:stopGatewaySSE});
