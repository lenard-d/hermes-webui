// Mutable coordination state shared by the session-domain owners. This module
// contains storage only; behavior remains in drafts, unread, and runtime owners.
let _loadingSessionId = null;
let _loadSessionGeneration = 0;
let _pendingCarryForwardSnapshot = null;

const _sessionStreamingById = new Map();
const _sessionListSnapshotById = new Map();
const _sessionListSourceById = new Map();

let _sessionListPointerActive = false;
let _sessionListLastScrollAt = 0;
let _pendingSessionListPayload = null;
let _pendingSessionListApplyTimer = 0;
let _sessionListLoadError = null;
let _sessionListHasLoadedOnce = false;

export const _SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
export const SESSION_LIST_INTERACTION_IDLE_MS = 700;
export const SESSION_SWIPE_DURATION_MS = 500;
export const SESSION_SWIPE_REFLOW_LEAD_MS = 220;
export const SESSION_REFLOW_TIMEOUT_MS = 420;
export const SESSION_LIST_FLIP_TIMEOUT_MS = 460;
export const SESSION_LONG_PRESS_DELAY_MS = 400;
export const SESSION_ARCHIVE_SWIPE_THRESHOLD_PX = 128;
export const SESSION_DELETE_SWIPE_THRESHOLD_PX = 128;
export const SESSION_SWIPE_CANCEL_RATIO = 0.75;

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}

function _sessionEventProfilesMatch(eventProfile, activeProfile){
  if(!(typeof eventProfile === 'string' && eventProfile.trim())) return true;
  return _profileMatchesActiveProfile(eventProfile, activeProfile);
}

export { _profileMatchesActiveProfile, _sessionEventProfilesMatch, _sessionListSnapshotById, _sessionListSourceById, _sessionStreamingById };

export const sessionStateStoreBindings=Object.freeze({
  get _loadSessionGeneration(){ return _loadSessionGeneration; },
  set _loadSessionGeneration(value){ _loadSessionGeneration=value; },
  get _loadingSessionId(){ return _loadingSessionId; },
  set _loadingSessionId(value){ _loadingSessionId=value; },
  get _pendingCarryForwardSnapshot(){ return _pendingCarryForwardSnapshot; },
  set _pendingCarryForwardSnapshot(value){ _pendingCarryForwardSnapshot=value; },
  get _pendingSessionListApplyTimer(){ return _pendingSessionListApplyTimer; },
  set _pendingSessionListApplyTimer(value){ _pendingSessionListApplyTimer=value; },
  get _pendingSessionListPayload(){ return _pendingSessionListPayload; },
  set _pendingSessionListPayload(value){ _pendingSessionListPayload=value; },
  get _sessionListHasLoadedOnce(){ return _sessionListHasLoadedOnce; },
  set _sessionListHasLoadedOnce(value){ _sessionListHasLoadedOnce=value; },
  get _sessionListLastScrollAt(){ return _sessionListLastScrollAt; },
  set _sessionListLastScrollAt(value){ _sessionListLastScrollAt=value; },
  get _sessionListLoadError(){ return _sessionListLoadError; },
  set _sessionListLoadError(value){ _sessionListLoadError=value; },
  get _sessionListPointerActive(){ return _sessionListPointerActive; },
  set _sessionListPointerActive(value){ _sessionListPointerActive=value; },
});
