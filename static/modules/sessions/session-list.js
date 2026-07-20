// Public session-list facade. Fetch/render coordination, payload reconciliation,
// background refresh, skeleton UI, and global sidebar event streams have
// separate lifecycle owners.
import { renderSessionList } from './session-list-loader.js';
import { _applySessionListPayload } from './session-list-reconciliation.js';
import { refreshSessionList, startStreamingPoll, stopStreamingPoll } from './session-list-refresh.js';
import { sessionListViewBindings, showSessionListSkeleton } from './session-list-skeleton.js';
import { startGatewaySSE, stopGatewaySSE } from './sidebar-session-events.js';

export const listUpdates=Object.freeze({
  showSkeleton:showSessionListSkeleton,
  applyPayload:_applySessionListPayload,
  render:renderSessionList,
  refresh:refreshSessionList,
  startPolling:startStreamingPoll,
  stopPolling:stopStreamingPoll,
  startGateway:startGatewaySSE,
  stopGateway:stopGatewaySSE,
});

export * from './session-list-loader.js';
export * from './session-list-reconciliation.js';
export * from './session-list-refresh.js';
export * from './session-list-skeleton.js';
export * from './sidebar-session-events.js';
