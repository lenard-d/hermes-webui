// Public sidebar facade. Mutable state, navigation, selection, motion, cached
// projections, and row actions each have a dedicated owner module.
import { _archiveSession, _copySessionLink, _openSessionActionMenu, closeSessionActionMenu } from './sidebar-actions.js';
import { _setActiveSessionUrl } from './session-navigation.js';
import { toggleSessionSelectMode } from './sidebar-selection.js';

export const sidebarControls=Object.freeze({
  setUrl:_setActiveSessionUrl,
  toggleSelect:toggleSessionSelectMode,
  closeActionMenu:closeSessionActionMenu,
  copyLink:_copySessionLink,
  openActionMenu:_openSessionActionMenu,
  archive:_archiveSession,
});

export * from './sidebar-actions.js';
export * from './sidebar-cache.js';
export * from './sidebar-motion.js';
export * from './sidebar-render-state.js';
export * from './sidebar-selection.js';
export * from './sidebar-store.js';
export * from './session-navigation.js';
