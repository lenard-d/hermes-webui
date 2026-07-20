import { sessionListCoordination } from './session-list-coordination.js';
import { sidebarStateBindings } from './sidebar-store.js';

// Invalidate every in-flight or queued list projection before a profile switch
// can replace the current sidebar. This is the single generation owner for
// fetch completions, deferred applies, and retry affordances.
function _invalidateSessionListRenders(){
  sidebarStateBindings._renderSessionListGen++;
  sessionListCoordination.pendingPayload = null;
  sidebarStateBindings._renderSessionListQueuedRequest = null;
  const loadError=sessionListCoordination.loadError;
  if(loadError && (loadError.retrying || loadError._retryFailedFocus)){
    sessionListCoordination.loadError = {...loadError};
    delete sessionListCoordination.loadError.retrying;
    delete sessionListCoordination.loadError._retryFailedFocus;
  }
}

if(typeof window!=='undefined') window._invalidateSessionListRenders = _invalidateSessionListRenders;

export { _invalidateSessionListRenders };
