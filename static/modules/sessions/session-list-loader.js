import { _SESSION_LIST_BOOT_TIMEOUT_MS, sessionStateStoreBindings as sessionStateBindings } from './session-state-store.js';
import { _clearSessionSourceTabCounts, _requestedSessionSidebarSource, _sessionListExcludeHiddenEnabled, _sessionListQueryString } from './message-loading.js';
import { sessionDiscoveryBindings } from './session-discovery.js';
import { _applySessionListPayload, _isSessionListUserInteracting, _schedulePendingSessionListApply } from './session-list-reconciliation.js';
import { sessionListViewBindings } from './session-list-skeleton.js';
import { sidebarStateBindings } from './sidebar-store.js';
import { registerSessionListRenderer } from './session-list-render-port.js';

let _profileSwitchListEmbargo = false;
function _setProfileSwitchListEmbargo(on){ _profileSwitchListEmbargo = !!on; }
if(typeof window!=='undefined') window._setProfileSwitchListEmbargo = _setProfileSwitchListEmbargo;

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
  const wasRetrying=Boolean(sessionStateBindings._sessionListLoadError&&sessionStateBindings._sessionListLoadError.retrying);
  sessionStateBindings._sessionListLoadError={
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
  if(!sessionStateBindings._sessionListLoadError) return null;
  const note=document.createElement('div');
  note.className='session-list-error session-empty-note';
  // a11y: announce load-error / retry-failure transitions to screen readers
  // (the note is re-rendered on both the pending click and the failure repaint).
  note.setAttribute('role','status');
  note.setAttribute('aria-live','polite');
  const title=document.createElement('div');
  title.textContent=sessionStateBindings._sessionListLoadError.message||'Could not load conversations.';
  note.appendChild(title);
  if(sessionStateBindings._sessionListLoadError.detail){
    const detail=document.createElement('div');
    detail.className='session-list-error-detail';
    detail.textContent=sessionStateBindings._sessionListLoadError.detail;
    note.appendChild(detail);
  }
  const retry=document.createElement('button');
  retry.type='button';
  retry.className='session-list-error-retry';
  const retrying=Boolean(sessionStateBindings._sessionListLoadError.retrying);
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
      if(!sessionStateBindings._sessionListLoadError||sessionStateBindings._sessionListLoadError.retrying) return;
      if(retry.getAttribute('aria-disabled')==='true') return;
      setPending();
      sessionStateBindings._sessionListLoadError={...sessionStateBindings._sessionListLoadError,retrying:true};
      renderSessionListFromCache();
      void renderSessionList({deferWhileInteracting:false}).finally(()=>{
        if(!retry.parentNode||(sessionStateBindings._sessionListLoadError&&sessionStateBindings._sessionListLoadError.retrying)) return;
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
    if(sessionStateBindings._sessionListLoadError._retryFailedFocus){
      delete sessionStateBindings._sessionListLoadError._retryFailedFocus;
      const _refocus=()=>{ try{ if(typeof retry.focus==='function') retry.focus(); }catch(_e){} };
      if(typeof requestAnimationFrame==='function') requestAnimationFrame(_refocus); else _refocus();
    }
  }
  note.appendChild(retry);
  return note;
}

async function _runRenderSessionListRefresh(opts, _gen){
  const deferWhileInteracting=Boolean(opts&&opts.deferWhileInteracting);
  if(!deferWhileInteracting) sessionStateBindings._pendingSessionListPayload=null;
  // Capture profile-switch unread generation BEFORE the await so a switch
  // mid-flight (which increments _cronPollGeneration) invalidates completion
  // marking for this response even if list gen checks already passed.
  const unreadGen = (typeof _cronPollGeneration === 'number') ? _cronPollGeneration : 0;
  try{
    if(!($('sessionSearch').value||'').trim()) sessionDiscoveryBindings._contentSearchResults = [];
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
    if(!sessionStateBindings._sessionListHasLoadedOnce){
      sessionRequestOpts.timeoutMs=_SESSION_LIST_BOOT_TIMEOUT_MS;
      sessionRequestOpts.retryTimeouts=true;
    }
    const {sessData, projData}=await _loadSidebarSessionListPayload(sessionListQS, sessionRequestOpts);
    // Discard stale response — a newer renderSessionList() call superseded us.
    if (_gen !== sidebarStateBindings._renderSessionListGen) return;
    // #4671: while a profile switch is mid-flight, drop ANY payload — even one whose
    // generation still matches — because a render that STARTED after the skeleton showed
    // but before the switch response set the new-profile cookie fetched the OLD profile's
    // rows. The switch clears the embargo immediately before its own (authoritative)
    // renderSessionList(), so that render's payload is the first allowed to paint.
    if (_profileSwitchListEmbargo) return;
    if(deferWhileInteracting&&_isSessionListUserInteracting()){
      sessionStateBindings._pendingSessionListPayload={gen:_gen,sessData,projData,unreadGen};
      _schedulePendingSessionListApply();
      return;
    }
    _applySessionListPayload(sessData,projData,{unreadGen});
  }catch(e){
    if (_gen !== sidebarStateBindings._renderSessionListGen) return;
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
      allProfiles: !!sidebarStateBindings._showAllProfiles,
      sidebarSource: _requestedSessionSidebarSource(),
      excludeHidden: _sessionListExcludeHiddenEnabled(),
    };
    const _scopeMatches = sidebarStateBindings._allSessionsScope
      && sidebarStateBindings._allSessionsScope.profile === _curScope.profile
      && sidebarStateBindings._allSessionsScope.allProfiles === _curScope.allProfiles
      && sidebarStateBindings._allSessionsScope.sidebarSource === _curScope.sidebarSource
      && sidebarStateBindings._allSessionsScope.excludeHidden === _curScope.excludeHidden;
    // #4671: the /api/sessions fetch failed — clear the skeleton flag so this error
    // render (matched cache, or empty rows for a mismatched scope) replaces the
    // up-front profile-switch skeleton instead of stranding it.
    sessionListViewBindings._sessionListSkeletonActive = false;
    if (_scopeMatches) {
      renderSessionListFromCache();
    } else {
      sidebarStateBindings._allSessions = [];
      sidebarStateBindings._sidebarReferenceSessions = [];
      sidebarStateBindings._allSessionsScope = _curScope;
      _clearSessionSourceTabCounts();
      renderSessionListFromCache();
    }
  }
}

async function _loadSidebarSessionListPayload(sessionListQS, sessionRequestOpts){
  const projectPromise = (async() => {
    try{
      const projectQS = sidebarStateBindings._showAllProfiles ? '?all_profiles=1' : '';
      return await api('/api/projects' + projectQS,{timeoutToast:false});
    }catch(projectError){
      console.warn('renderProjectsList',projectError);
      return {projects:sidebarStateBindings._allProjects||[]};
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
      request=sidebarStateBindings._renderSessionListQueuedRequest;
      sidebarStateBindings._renderSessionListQueuedRequest=null;
    }
  }finally{
    sidebarStateBindings._renderSessionListInFlight=null;
    if(sidebarStateBindings._renderSessionListQueuedRequest){
      const next=sidebarStateBindings._renderSessionListQueuedRequest;
      sidebarStateBindings._renderSessionListQueuedRequest=null;
      sidebarStateBindings._renderSessionListInFlight=_drainRenderSessionListQueue(next);
    }
  }
}

async function renderSessionList(opts={}){
  const request={opts:opts||{},gen:++sidebarStateBindings._renderSessionListGen};
  if(sidebarStateBindings._renderSessionListInFlight){
    sidebarStateBindings._renderSessionListQueuedRequest={
      opts:_mergeRenderSessionListOptions(sidebarStateBindings._renderSessionListQueuedRequest&&sidebarStateBindings._renderSessionListQueuedRequest.opts, request.opts),
      gen:request.gen,
    };
    return sidebarStateBindings._renderSessionListInFlight;
  }
  sidebarStateBindings._renderSessionListInFlight=_drainRenderSessionListQueue(request);
  return sidebarStateBindings._renderSessionListInFlight;
}

registerSessionListRenderer(renderSessionList);


export { _renderSessionListLoadErrorNote, _setProfileSwitchListEmbargo, renderSessionList };
