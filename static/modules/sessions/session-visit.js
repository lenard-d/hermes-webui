import { _syncSessionListSnapshotOnVisit } from './session-run-registry.js';
import { _forgetObservedStreamingSession, _setSessionViewedCount } from './session-unread.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

// A visit is a small transaction across persisted unread state, the polling
// snapshot, and the rendered sidebar. Keeping it here ensures all three layers
// acknowledge the same authoritative session metadata.
export function _acknowledgeSessionVisit(sid, messageCount = 0, lastMessageAt = 0){
  if(!sid) return false;
  _setSessionViewedCount(sid, messageCount);
  const isStreaming=_syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt);
  if(!isStreaming) _forgetObservedStreamingSession(sid);
  renderSessionListFromCache();
  return true;
}

export const sessionVisits=Object.freeze({acknowledge:_acknowledgeSessionVisit});
