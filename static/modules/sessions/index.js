// Native session-domain entrypoint. Internal modules import their owners
// directly; only this entrypoint publishes compatibility globals.
import { composerDrafts, _clearComposerDraft, _rememberNewChatDraftSession, _restoreComposerDraft, _restoreRememberedNewChatDraftSession, _saveComposerDraft, _saveComposerDraftNow } from './composer-drafts.js';
import { sessionLiveRecovery, _inflightHasVisibleLiveState, _renderRuntimeJournalAnchorActivityScene, _selectLiveRecoveryInflight, _serverLiveSnapshotInflight } from './session-live-recovery.js';
import { SESSION_LONG_PRESS_DELAY_MS, sessionListCoordination } from './session-list-coordination.js';
import { sessionLoadState } from './session-load-state.js';
import { _profileMatchesActiveProfile } from './session-profile-scope.js';
import { sessionRunRegistry } from './session-run-registry.js';
import { sessionRunState, _markSessionCompletedInList } from './session-run-state.js';
import { sessionUnread, _clearCronSessionCompletionUnreadForInactiveProfiles, _clearSessionCompletionUnread, _hasUnreadForSession, _markSessionCompletionUnread, _markSessionCompletionUnreadIfBackground, _setSessionViewedCount } from './session-unread.js';
import { sessionVisits, _acknowledgeSessionVisit } from './session-visit.js';
import { sessionLifecycle } from './lifecycle.js';
import { sessionMessages } from './message-loading.js';
import { messageTimeline } from './message-timeline.js';
import { sidebarControls } from './sidebar-state.js';
import { listUpdates } from './session-list.js';
import { sessionDiscovery } from './session-discovery.js';
import { sidebarRowBehavior } from './sidebar-interactions.js';
import { sidebarRendering, renderSessionListFromCache } from './sidebar-renderer.js';
import { sessionManagement } from './management.js';
import * as lifecycleModule from './lifecycle.js';
import * as messageLoadingModule from './message-loading.js';
import * as messageTimelineModule from './message-timeline.js';
import * as sidebarStateModule from './sidebar-state.js';
import * as sessionListModule from './session-list.js';
import * as sessionDiscoveryModule from './session-discovery.js';
import * as sidebarInteractionsModule from './sidebar-interactions.js';
import * as sidebarRendererModule from './sidebar-renderer.js';
import * as managementModule from './management.js';
import { publishCompatibilityDomain } from '../compatibility.js';

function _markSessionCompletedAndRender(session, previousSid=null){
  const changed=_markSessionCompletedInList(session,previousSid);
  if(changed) renderSessionListFromCache();
  return changed;
}

function _markSessionCompletionUnreadIfBackgroundAndRender(sid, messageCount=null, meta=null){
  const changed=_markSessionCompletionUnreadIfBackground(sid,messageCount,meta);
  renderSessionListFromCache();
  return changed;
}

function _clearCronSessionCompletionUnreadForInactiveProfilesAndRender(activeProfile){
  const changed=_clearCronSessionCompletionUnreadForInactiveProfiles(activeProfile);
  if(changed) renderSessionListFromCache();
  return changed;
}

function _restoreRememberedNewChatDraft(){
  return _restoreRememberedNewChatDraftSession(sessionLifecycle.load);
}

export const sessionRuntime=Object.freeze({
  ...sessionRunState,
  ...sessionLiveRecovery,
  markCompleted:_markSessionCompletedAndRender,
});

export const sessionState=Object.freeze({
  saveDraft:_saveComposerDraft,
  saveDraftNow:_saveComposerDraftNow,
  restoreDraft:_restoreComposerDraft,
  clearDraft:_clearComposerDraft,
  acknowledgeVisit:_acknowledgeSessionVisit,
  hasUnread:_hasUnreadForSession,
  selectLiveRecovery:_selectLiveRecoveryInflight,
  markCompleted:_markSessionCompletedAndRender,
});

export const parts=Object.freeze({
  sessionState,
  sessionLoadState,
  sessionListCoordination,
  sessionRunRegistry,
  composerDrafts,
  sessionRuntime,
  sessionUnread,
  sessionVisits,
  sessionLifecycle,
  sessionMessages,
  messageTimeline,
  sidebarControls,
  listUpdates,
  sessionDiscovery,
  sidebarRowBehavior,
  sidebarRendering,
  sessionManagement,
});

export const api=Object.freeze({
  newSession:sessionLifecycle.create,
  loadSession:sessionLifecycle.load,
  renderSessionList:listUpdates.render,
  renderSessionListFromCache:sidebarRendering.renderList,
  showSessionListSkeleton:listUpdates.showSkeleton,
  refreshSessionList:listUpdates.refresh,
  filterSessions:sessionDiscovery.filter,
  clearSessionSearch:sessionDiscovery.clear,
  deleteSession:sessionManagement.deleteSession,
  removeWorktree:sessionManagement.removeWorktree,
  navigateSession:sessionManagement.navigate,
});

const legacyBindings=Object.freeze({
  "SESSION_LONG_PRESS_DELAY_MS":Object.freeze({get:()=>SESSION_LONG_PRESS_DELAY_MS}),
  "_allSessions":Object.freeze({get:()=>sidebarStateModule.sidebarStateBindings._allSessions,set:(value)=>{sidebarStateModule.sidebarStateBindings._allSessions=value;}}),
  "_appRootPath":Object.freeze({get:()=>sidebarStateModule._appRootPath}),
  "_clearComposerDraft":Object.freeze({get:()=>_clearComposerDraft}),
  "_clearCronSessionCompletionUnreadForInactiveProfiles":Object.freeze({get:()=>_clearCronSessionCompletionUnreadForInactiveProfilesAndRender}),
  "_clearSessionCompletionUnread":Object.freeze({get:()=>_clearSessionCompletionUnread}),
  "_composerPrefillIntentFromLocation":Object.freeze({get:()=>sidebarStateModule._composerPrefillIntentFromLocation}),
  "_consumeComposerPrefillParamsFromLocation":Object.freeze({get:()=>sidebarStateModule._consumeComposerPrefillParamsFromLocation}),
  "_consumeProfileQueryParamFromLocation":Object.freeze({get:()=>sidebarStateModule._consumeProfileQueryParamFromLocation}),
  "_dismissHandoffHint":Object.freeze({get:()=>messageLoadingModule._dismissHandoffHint}),
  "_ensureAllMessagesLoaded":Object.freeze({get:()=>messageTimelineModule._ensureAllMessagesLoaded}),
  "_flushDeferredActiveSessionExternalRefresh":Object.freeze({get:()=>sessionListModule._flushDeferredActiveSessionExternalRefresh}),
  "_formatInServerTz":Object.freeze({get:()=>sessionDiscoveryModule._formatInServerTz}),
  "_invalidateSessionListRenders":Object.freeze({get:()=>sidebarStateModule._invalidateSessionListRenders}),
  "_isBranchableReadOnlySession":Object.freeze({get:()=>messageLoadingModule._isBranchableReadOnlySession}),
  "_isReadOnlySession":Object.freeze({get:()=>messageLoadingModule._isReadOnlySession}),
  "_loadOlderMessages":Object.freeze({get:()=>messageTimelineModule._loadOlderMessages}),
  "_loadingSessionId":Object.freeze({get:()=>sessionLoadState.loadingSessionId,set:(value)=>{sessionLoadState.loadingSessionId=value;}}),
  "_markSessionCompletedInList":Object.freeze({get:()=>_markSessionCompletedAndRender}),
  "_markSessionCompletionUnread":Object.freeze({get:()=>_markSessionCompletionUnread}),
  "_markSessionCompletionUnreadIfBackground":Object.freeze({get:()=>_markSessionCompletionUnreadIfBackgroundAndRender}),
  "_messagesTruncated":Object.freeze({get:()=>messageLoadingModule.messageLoadingBindings._messagesTruncated,set:(value)=>{messageLoadingModule.messageLoadingBindings._messagesTruncated=value;}}),
  "_oldestIdx":Object.freeze({get:()=>messageTimelineModule.messageTimelineBindings._oldestIdx,set:(value)=>{messageTimelineModule.messageTimelineBindings._oldestIdx=value;}}),
  "_openSessionActionMenu":Object.freeze({get:()=>sidebarStateModule._openSessionActionMenu}),
  "_profileMatchesActiveProfile":Object.freeze({get:()=>_profileMatchesActiveProfile}),
  "_profileQueryIntentFromLocation":Object.freeze({get:()=>sidebarStateModule._profileQueryIntentFromLocation}),
  "_profileSwitchOpeningExistingSession":Object.freeze({get:()=>sidebarStateModule.sidebarStateBindings._profileSwitchOpeningExistingSession,set:(value)=>{sidebarStateModule.sidebarStateBindings._profileSwitchOpeningExistingSession=value;}}),
  "_rememberEmptyComposerModelOverride":Object.freeze({get:()=>lifecycleModule._rememberEmptyComposerModelOverride}),
  "_renamingSid":Object.freeze({get:()=>sidebarStateModule.sidebarStateBindings._renamingSid,set:(value)=>{sidebarStateModule.sidebarStateBindings._renamingSid=value;}}),
  "_restoreRememberedNewChatDraftSession":Object.freeze({get:()=>_restoreRememberedNewChatDraft}),
  "_sameTranscriptMessage":Object.freeze({get:()=>messageTimelineModule._sameTranscriptMessage}),
  "_saveComposerDraft":Object.freeze({get:()=>_saveComposerDraft}),
  "_saveComposerDraftNow":Object.freeze({get:()=>_saveComposerDraftNow}),
  "_sessionActionMenu":Object.freeze({get:()=>sidebarStateModule.sidebarStateBindings._sessionActionMenu}),
  "_sessionIdFromLocation":Object.freeze({get:()=>sidebarStateModule._sessionIdFromLocation}),
  "_sessionListSkeletonActive":Object.freeze({get:()=>sessionListModule.sessionListBindings._sessionListSkeletonActive,set:(value)=>{sessionListModule.sessionListBindings._sessionListSkeletonActive=value;}}),
  "_sessionUrlForSid":Object.freeze({get:()=>sidebarStateModule._sessionUrlForSid}),
  "_setActiveSessionUrl":Object.freeze({get:()=>sidebarStateModule._setActiveSessionUrl}),
  "_setProfileSwitchListEmbargo":Object.freeze({get:()=>sessionListModule._setProfileSwitchListEmbargo}),
  "_setSessionViewedCount":Object.freeze({get:()=>_setSessionViewedCount}),
  "_showAllProfiles":Object.freeze({get:()=>sidebarStateModule.sidebarStateBindings._showAllProfiles,set:(value)=>{sidebarStateModule.sidebarStateBindings._showAllProfiles=value;}}),
  "animateNextSessionListRefresh":Object.freeze({get:()=>sessionListModule.animateNextSessionListRefresh}),
  "clearOptimisticSessionStreaming":Object.freeze({get:()=>sidebarInteractionsModule.clearOptimisticSessionStreaming}),
  "clearSessionSearch":Object.freeze({get:()=>sessionDiscoveryModule.clearSessionSearch}),
  "closeSessionActionMenu":Object.freeze({get:()=>sidebarStateModule.closeSessionActionMenu}),
  "filterSessions":Object.freeze({get:()=>sessionDiscoveryModule.filterSessions}),
  "loadSession":Object.freeze({get:()=>lifecycleModule.loadSession}),
  "newSession":Object.freeze({get:()=>lifecycleModule.newSession}),
  "renderSessionList":Object.freeze({get:()=>sessionListModule.renderSessionList}),
  "renderSessionListFromCache":Object.freeze({get:()=>sidebarRendererModule.renderSessionListFromCache}),
  "showSessionListSkeleton":Object.freeze({get:()=>sessionListModule.showSessionListSkeleton}),
  "startGatewaySSE":Object.freeze({get:()=>sessionListModule.startGatewaySSE}),
  "stopGatewaySSE":Object.freeze({get:()=>sessionListModule.stopGatewaySSE}),
  "syncSessionSearchClear":Object.freeze({get:()=>sessionDiscoveryModule.syncSessionSearchClear}),
  "upsertActiveSessionForLocalTurn":Object.freeze({get:()=>sidebarInteractionsModule.upsertActiveSessionForLocalTurn}),
});

export const HermesSessions=Object.freeze({version:'native-es-modules-v1',parts,api});
publishCompatibilityDomain('sessions',{
  namespace:'HermesSessions',
  api:HermesSessions,
  bindings:legacyBindings,
});
export default HermesSessions;
