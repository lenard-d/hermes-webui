import { sessionStateStoreBindings as sessionStateBindings } from './session-state-store.js';
import { sidebarStateBindings } from './sidebar-store.js';

// Invalidate every in-flight or queued list projection before a profile switch
// can replace the current sidebar. This is the single generation owner for
// fetch completions, deferred applies, and retry affordances.
function _invalidateSessionListRenders(){
  sidebarStateBindings._renderSessionListGen++;
  sessionStateBindings._pendingSessionListPayload = null;
  sidebarStateBindings._renderSessionListQueuedRequest = null;
  const loadError=sessionStateBindings._sessionListLoadError;
  if(loadError && (loadError.retrying || loadError._retryFailedFocus)){
    sessionStateBindings._sessionListLoadError = {...loadError};
    delete sessionStateBindings._sessionListLoadError.retrying;
    delete sessionStateBindings._sessionListLoadError._retryFailedFocus;
  }
}

if(typeof window!=='undefined') window._invalidateSessionListRenders = _invalidateSessionListRenders;

export { _invalidateSessionListRenders };
