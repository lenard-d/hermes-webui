import { loadSession } from './existing-session-load.js';
import { _clearEmptyComposerModelOverride, _newSessionInFlight, _rememberEmptyComposerModelOverride, newSession } from './new-session.js';
import { registerSessionLifecycle } from './session-lifecycle-port.js';
import { _rearmActiveSessionStream, _restoreLoadedSession } from './session-load-recovery.js';

registerSessionLifecycle({
  create:newSession,
  isCreating:()=>Boolean(_newSessionInFlight),
  load:loadSession,
});

export const sessionLifecycle=Object.freeze({
  create:newSession,
  rearmStream:_rearmActiveSessionStream,
  restore:_restoreLoadedSession,
  load:loadSession,
});

export {
  _clearEmptyComposerModelOverride,
  _newSessionInFlight,
  _rememberEmptyComposerModelOverride,
  _rearmActiveSessionStream,
  _restoreLoadedSession,
  loadSession,
  newSession,
};
