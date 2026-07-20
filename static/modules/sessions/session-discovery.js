// Compatibility interface for browser globals and callers outside the sessions
// domain. Session modules import the focused owners directly.
import { clearSessionSearch, filterSessions, sessionSearchBindings } from './session-search.js';
import { _collapseSessionLineageForSidebar, _resolveSessionIdFromSidebarLineage } from './session-lineage.js';
import { sessionTimeBindings } from './session-time.js';

export * from './session-child-attachment.js';
export * from './session-lineage.js';
export * from './session-lineage-report.js';
export * from './session-row-labels.js';
export * from './session-search.js';
export * from './session-time.js';

export const sessionDiscovery=Object.freeze({
  filter:filterSessions,
  clear:clearSessionSearch,
  resolveLineage:_resolveSessionIdFromSidebarLineage,
  collapseLineage:_collapseSessionLineageForSidebar,
});

export const sessionDiscoveryBindings=Object.freeze({
  get _contentSearchResults(){ return sessionSearchBindings._contentSearchResults; },
  set _contentSearchResults(value){ sessionSearchBindings._contentSearchResults=value; },
  get _hideSearchPreviewsAfterSelect(){ return sessionSearchBindings._hideSearchPreviewsAfterSelect; },
  set _hideSearchPreviewsAfterSelect(value){ sessionSearchBindings._hideSearchPreviewsAfterSelect=value; },
  get _serverTimeDelta(){ return sessionTimeBindings._serverTimeDelta; },
  set _serverTimeDelta(value){ sessionTimeBindings._serverTimeDelta=value; },
  get _serverTz(){ return sessionTimeBindings._serverTz; },
  set _serverTz(value){ sessionTimeBindings._serverTz=value; },
});
