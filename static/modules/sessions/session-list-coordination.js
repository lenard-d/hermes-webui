// Session-list interaction, deferred-apply, and render timing coordination.
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

export const sessionListCoordination=Object.freeze({
  get pendingApplyTimer(){ return _pendingSessionListApplyTimer; },
  set pendingApplyTimer(value){ _pendingSessionListApplyTimer=value; },
  get pendingPayload(){ return _pendingSessionListPayload; },
  set pendingPayload(value){ _pendingSessionListPayload=value; },
  get hasLoadedOnce(){ return _sessionListHasLoadedOnce; },
  set hasLoadedOnce(value){ _sessionListHasLoadedOnce=value; },
  get lastScrollAt(){ return _sessionListLastScrollAt; },
  set lastScrollAt(value){ _sessionListLastScrollAt=value; },
  get loadError(){ return _sessionListLoadError; },
  set loadError(value){ _sessionListLoadError=value; },
  get pointerActive(){ return _sessionListPointerActive; },
  set pointerActive(value){ _sessionListPointerActive=value; },
});
