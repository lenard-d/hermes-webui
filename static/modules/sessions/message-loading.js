import { _isCliSession, _isReadOnlySession, _openSidebarSession } from './sidebar-session-opening.js';
import { _ensureMessagesLoaded } from './transcript-loading.js';
import { transcriptWindowState } from './transcript-window-state.js';

export const sessionMessages=Object.freeze({open:_openSidebarSession,isReadOnly:_isReadOnlySession,isCli:_isCliSession,ensureLoaded:_ensureMessagesLoaded});

export * from './sidebar-session-opening.js';
export * from './handoff-lifecycle.js';
export * from './session-post-load.js';
export * from './transcript-loading.js';
export { _msgLimitMax } from './transcript-window-state.js';

export const messageLoadingBindings=Object.freeze({
  get _messagesTruncated(){ return transcriptWindowState.messagesTruncated; },
  set _messagesTruncated(value){ transcriptWindowState.messagesTruncated=value; },
});

export const messageLoadingState=transcriptWindowState;
