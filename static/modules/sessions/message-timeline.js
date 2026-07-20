import { _mergeInflightTailMessages, _prepareRunningLiveTail } from './current-turn-transcript.js';
import { _ensureAllMessagesLoaded, _loadOlderMessages } from './older-message-pagination.js';
import { transcriptWindowState } from './transcript-window-state.js';

export const messageTimeline=Object.freeze({mergeInflight:_mergeInflightTailMessages,prepareRunningTail:_prepareRunningLiveTail,loadOlder:_loadOlderMessages,ensureAllLoaded:_ensureAllMessagesLoaded});

export * from './current-turn-transcript.js';
export * from './older-message-pagination.js';

export const messageTimelineBindings=Object.freeze({
  get _loadingOlder(){ return transcriptWindowState.loadingOlder; },
  set _loadingOlder(value){ transcriptWindowState.loadingOlder=value; },
  get _oldestIdx(){ return transcriptWindowState.oldestIdx; },
  set _oldestIdx(value){ transcriptWindowState.oldestIdx=value; },
});
