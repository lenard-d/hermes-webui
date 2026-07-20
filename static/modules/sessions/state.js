// Compatibility facade for session state. Durable draft, unread, runtime-list,
// and live-recovery behavior lives with its respective owner module.
import { _clearComposerDraft, _rememberNewChatDraftSession, _restoreComposerDraft, _restoreRememberedNewChatDraftSession, _saveComposerDraft, _saveComposerDraftNow } from './composer-drafts.js';
import {
  _inflightHasVisibleLiveState,
  SESSION_ARCHIVE_SWIPE_THRESHOLD_PX,
  SESSION_DELETE_SWIPE_THRESHOLD_PX,
  SESSION_LIST_FLIP_TIMEOUT_MS,
  SESSION_LIST_INTERACTION_IDLE_MS,
  SESSION_LONG_PRESS_DELAY_MS,
  SESSION_REFLOW_TIMEOUT_MS,
  SESSION_SWIPE_CANCEL_RATIO,
  SESSION_SWIPE_DURATION_MS,
  SESSION_SWIPE_REFLOW_LEAD_MS,
  _SESSION_LIST_BOOT_TIMEOUT_MS,
  _sessionStreamingById,
  _profileMatchesActiveProfile,
  _sessionEventProfilesMatch,
  sessionStateStoreBindings,
} from './session-state-store.js';
import {
  _isServerIdleSessionRow,
  _isSessionEffectivelyStreaming,
  _isSessionLocallyStreaming,
  _markPollingCompletionUnreadTransitions,
  _markSessionCompletedInList,
  _purgeStaleInflightEntries,
  _reconcileActiveSessionIdleStateFromList,
  _rememberRenderedSessionSnapshot,
  _rememberRenderedStreamingState,
  _rememberSessionListSource,
  _renderRuntimeJournalAnchorActivityScene,
  _selectLiveRecoveryInflight,
  _serverLiveSnapshotInflight,
} from './session-runtime.js';
import {
  _acknowledgeSessionVisit,
  _clearCronSessionCompletionUnreadForInactiveProfiles,
  _clearSessionCompletionUnread,
  _clearSessionViewedCount,
  _forgetObservedStreamingSession,
  _hasUnreadForSession,
  _isSessionActivelyViewedForList,
  _knownSessionProfileCount,
  _markSessionCompletionUnread,
  _markSessionCompletionUnreadIfBackground,
  _recordSessionProfileCount,
  _sessionVisitHasUnreadState,
  _setSessionViewedCount,
} from './session-unread.js';

const ICONS={
  stop:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>',
  pin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><polygon points="8,1.5 9.8,5.8 14.5,6.2 11,9.4 12,14 8,11.5 4,14 5,9.4 1.5,6.2 6.2,5.8"/></svg>',
  unpin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><polygon points="8,2 9.8,6.2 14.2,6.2 10.7,9.2 12,13.8 8,11 4,13.8 5.3,9.2 1.8,6.2 6.2,6.2"/></svg>',
  folder:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M2 4.5h4l1.5 1.5H14v7H2z"/></svg>',
  archive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><line x1="6" y1="8.5" x2="10" y2="8.5"/></svg>',
  unarchive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><polyline points="6.5,7 8,5.5 9.5,7"/></svg>',
  dup:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="4.5" y="4.5" width="8.5" height="8.5" rx="1.5"/><path d="M3 11.5V3h8.5"/></svg>',
  trash:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5v8.5h7v-8.5"/><line x1="7" y1="7" x2="7" y2="11"/><line x1="9" y1="7" x2="9" y2="11"/></svg>',
  more:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><circle cx="8" cy="3" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="8" cy="13" r="1.25"/></svg>',
  edit:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 2.5l2 2L5 13H3v-2z"/><path d="M10 4l2 2"/></svg>',
  spark:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8l1.1 3.1 3.1 1.1-3.1 1.1L8 10.2 6.9 7.1 3.8 6l3.1-1.1z"/><path d="M12.5 9.5l.5 1.5 1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5z"/></svg>',
  link:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6.7 9.3a3 3 0 0 1 0-4.2l1.7-1.7a3 3 0 0 1 4.2 4.2l-1 1"/><path d="M9.3 6.7a3 3 0 0 1 0 4.2l-1.7 1.7a3 3 0 0 1-4.2-4.2l1-1"/></svg>',
  download:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14 10.5v2.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-2.5"/><polyline points="4.5 7 8 10.5 11.5 7"/><line x1="8" y1="10.5" x2="8" y2="2"/></svg>',
};

function _manualTitleAuxConfigFromPayload(auxData){
  if(!auxData||typeof auxData!=='object'||Array.isArray(auxData)) return null;
  if(auxData.title_generation&&typeof auxData.title_generation==='object') return auxData;
  const tasks=Array.isArray(auxData.tasks)?auxData.tasks:null;
  if(!tasks) return null;
  const taskMap={};
  for(const task of tasks){
    if(task&&typeof task==='object'&&typeof task.task==='string'&&task.task){
      taskMap[task.task]=task;
    }
  }
  if(!Object.keys(taskMap).length) return null;
  return taskMap;
}

async function _loadManualTitleAuxConfig(){
  try{
    const auxData=await api('/api/model/auxiliary',{retries:0,timeoutToast:false});
    return _manualTitleAuxConfigFromPayload(auxData);
  }catch(_){
    return null;
  }
}

async function _manualTitleRegenerateTimeoutMs(){
  let cfg=null;
  try{
    const auxConfig=await _loadManualTitleAuxConfig();
    cfg=auxConfig&&auxConfig.title_generation;
  }catch(_){
    return null;
  }
  const timeoutSeconds=Number(cfg&&cfg.timeout);
  if(!Number.isFinite(timeoutSeconds)||timeoutSeconds<=0) return null;
  return Math.max(30000,Math.round((timeoutSeconds+5)*1000));
}

function _formatSessionModelWithGateway(s){
  if(!s||!s.model)return'';
  const routing=(typeof _latestGatewayRoutingForSession==='function')?_latestGatewayRoutingForSession(s):(s.gateway_routing||null);
  if(typeof _formatGatewayModelLabel==='function'){
    return _formatGatewayModelLabel(s.model,s.model,routing)||getModelLabel(s.model);
  }
  return s.model;
}





export const sessionState=Object.freeze({
  saveDraft:_saveComposerDraft,
  saveDraftNow:_saveComposerDraftNow,
  restoreDraft:_restoreComposerDraft,
  clearDraft:_clearComposerDraft,
  acknowledgeVisit:_acknowledgeSessionVisit,
  hasUnread:_hasUnreadForSession,
  selectLiveRecovery:_selectLiveRecoveryInflight,
  markCompleted:_markSessionCompletedInList,
});

export {
  ICONS,
  SESSION_ARCHIVE_SWIPE_THRESHOLD_PX,
  SESSION_DELETE_SWIPE_THRESHOLD_PX,
  SESSION_LIST_FLIP_TIMEOUT_MS,
  SESSION_LIST_INTERACTION_IDLE_MS,
  SESSION_LONG_PRESS_DELAY_MS,
  SESSION_REFLOW_TIMEOUT_MS,
  SESSION_SWIPE_CANCEL_RATIO,
  SESSION_SWIPE_DURATION_MS,
  SESSION_SWIPE_REFLOW_LEAD_MS,
  _SESSION_LIST_BOOT_TIMEOUT_MS,
  _acknowledgeSessionVisit,
  _clearComposerDraft,
  _clearCronSessionCompletionUnreadForInactiveProfiles,
  _clearSessionCompletionUnread,
  _clearSessionViewedCount,
  _forgetObservedStreamingSession,
  _formatSessionModelWithGateway,
  _hasUnreadForSession,
  _inflightHasVisibleLiveState,
  _isServerIdleSessionRow,
  _isSessionActivelyViewedForList,
  _isSessionEffectivelyStreaming,
  _isSessionLocallyStreaming,
  _knownSessionProfileCount,
  _manualTitleRegenerateTimeoutMs,
  _markPollingCompletionUnreadTransitions,
  _markSessionCompletedInList,
  _markSessionCompletionUnread,
  _markSessionCompletionUnreadIfBackground,
  _profileMatchesActiveProfile,
  _purgeStaleInflightEntries,
  _reconcileActiveSessionIdleStateFromList,
  _recordSessionProfileCount,
  _rememberNewChatDraftSession,
  _rememberRenderedSessionSnapshot,
  _rememberRenderedStreamingState,
  _rememberSessionListSource,
  _renderRuntimeJournalAnchorActivityScene,
  _restoreComposerDraft,
  _restoreRememberedNewChatDraftSession,
  _saveComposerDraft,
  _saveComposerDraftNow,
  _selectLiveRecoveryInflight,
  _serverLiveSnapshotInflight,
  _sessionEventProfilesMatch,
  _sessionStreamingById,
  _sessionVisitHasUnreadState,
  _setSessionViewedCount,
};

// Preserve the historical binding surface while delegating each mutation to
// the store that owns it.
export const sessionStateBindings=Object.freeze({
  get _loadSessionGeneration(){ return sessionStateStoreBindings._loadSessionGeneration; },
  set _loadSessionGeneration(value){ sessionStateStoreBindings._loadSessionGeneration=value; },
  get _loadingSessionId(){ return sessionStateStoreBindings._loadingSessionId; },
  set _loadingSessionId(value){ sessionStateStoreBindings._loadingSessionId=value; },
  get _pendingCarryForwardSnapshot(){ return sessionStateStoreBindings._pendingCarryForwardSnapshot; },
  set _pendingCarryForwardSnapshot(value){ sessionStateStoreBindings._pendingCarryForwardSnapshot=value; },
  get _pendingSessionListApplyTimer(){ return sessionStateStoreBindings._pendingSessionListApplyTimer; },
  set _pendingSessionListApplyTimer(value){ sessionStateStoreBindings._pendingSessionListApplyTimer=value; },
  get _pendingSessionListPayload(){ return sessionStateStoreBindings._pendingSessionListPayload; },
  set _pendingSessionListPayload(value){ sessionStateStoreBindings._pendingSessionListPayload=value; },
  get _sessionListHasLoadedOnce(){ return sessionStateStoreBindings._sessionListHasLoadedOnce; },
  set _sessionListHasLoadedOnce(value){ sessionStateStoreBindings._sessionListHasLoadedOnce=value; },
  get _sessionListLastScrollAt(){ return sessionStateStoreBindings._sessionListLastScrollAt; },
  set _sessionListLastScrollAt(value){ sessionStateStoreBindings._sessionListLastScrollAt=value; },
  get _sessionListLoadError(){ return sessionStateStoreBindings._sessionListLoadError; },
  set _sessionListLoadError(value){ sessionStateStoreBindings._sessionListLoadError=value; },
  get _sessionListPointerActive(){ return sessionStateStoreBindings._sessionListPointerActive; },
  set _sessionListPointerActive(value){ sessionStateStoreBindings._sessionListPointerActive=value; },
});
