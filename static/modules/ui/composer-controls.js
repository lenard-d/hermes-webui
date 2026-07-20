// Stable compatibility facade. New code should import the semantic owner directly.
export {
  _applyToolsetsChip,
  _syncToolsetsChip,
  syncToolsetsChip,
  _normalizeToolsetsCatalog,
  _loadToolsetsCatalog,
  invalidateToolsetsCatalog,
  _toolsetsInputList,
  _ensureToolsetsPresetSection,
  _appendToolsetsLabel,
  _renderToolsetsPresetSections,
  _populateToolsetsDropdown,
  _positionToolsetsDropdown,
  toggleToolsetsDropdown,
  closeToolsetsDropdown,
  _applySessionToolsets,
  _currentSessionToolsets,
  _toolsetsCatalog,
} from './toolsets-controls.js';

export {
  _syncMobileComposerConfigButton,
  closeMobileComposerConfig,
  openMobileComposerConfig,
  toggleMobileComposerConfig,
  openComposerContextMenu,
} from './mobile-composer-config.js';

export {
  _deferClearProgrammaticScroll,
  _recentMessageRenderArtifactWindow,
  _cancelBottomSettle,
  _markMessageTouchScrollIntent,
  _recentMessageTouchScrollIntent,
  _recentMessageWheelIntent,
  _recentMessageScrollIntent,
  _recentMessageKeyScrollIntent,
  _isMessageReaderUnpinned,
  _olderMessagesPrefetchReady,
  _scheduleDeferredOlderMessagesLoad,
  _recordNonMessageScrollIntent,
  _recentNonMessageScrollIntent,
  _setScrollToBottomCueText,
  _syncScrollToBottomCue,
  _showNewMessageScrollCue,
  _clearNewMessageScrollCue,
  _maybeShowNewMessageScrollCue,
  _resetScrollDirectionTracker,
  _resetStreamScrollFollow,
  NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS,
  MESSAGE_TOUCH_SCROLL_SUPPRESS_MS,
  MESSAGE_WHEEL_INTENT_SUPPRESS_MS,
  MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS,
  _scrollPinned,
  _programmaticScroll,
  _programmaticScrollSetAt,
  _programmaticScrollResetTimer,
  _nearBottomCount,
  _lastScrollTop,
  _lastMessageClientHeight,
  _lastNonMessageScrollIntentMs,
  _messageUserUnpinned,
  _bottomSettleToken,
  _settleRAF,
  _settleRO,
  _settleTimer,
  _settleFinalTimer,
  _touchStartY,
  _messageTouchScrollActive,
  _lastMessageTouchScrollIntentMs,
  _deferredOlderMessagesTimer,
  _lastMessageWheelIntentMs,
  _lastMessageScrollIntentMs,
  _lastMessageKeyScrollIntentMs,
  _newMessageCueVisible,
  _lastMessageRenderAt,
} from './message-scroll-follow.js';

export {
  _fmtTokens,
  _formatTurnDuration,
  _formatFirstToken,
  _formatActiveElapsedTimer,
  _processedElapsedLabel,
  _compressionElapsedStartedAt,
  _compressionElapsedLabel,
  _compressionElapsedExpired,
  _compressionLiveCardNode,
  _compressionLiveCardState,
  _updateCompressionElapsedCards,
  _updateCompressionElapsedTimer,
  _startCompressionElapsedTimer,
  _clearCompressionElapsedTimer,
  _activityNowSeconds,
  _isActivityTimerGroup,
  _activityElapsedStartedAt,
  _activityElapsedLabel,
  _activityProcessedElapsedLabel,
  _activitySettledProcessedLabel,
  _activityMarkObserved,
  _activityLastObservedAge,
  _activityClockLabel,
  _activityFullClockLabel,
  _timestampSeconds,
  _firstValidTimestampSeconds,
  _transparentEventTimestampSeconds,
  _syncTransparentEventTimestamp,
  _COMPRESSION_ELAPSED_MAX_SECONDS,
  _compressionElapsedTimer,
  _activityElapsedTimer,
  _activityElapsedTimerGroup,
} from './activity-timing.js';

import { compatibilityBindings as toolsetsBindings } from './toolsets-controls.js';
import { compatibilityBindings as mobileConfigBindings } from './mobile-composer-config.js';
import { compatibilityBindings as messageScrollBindings } from './message-scroll-follow.js';
import { compatibilityBindings as activityTimingBindings } from './activity-timing.js';

const compatibilityBindings = {};
for (const ownerBindings of [toolsetsBindings, mobileConfigBindings, messageScrollBindings, activityTimingBindings]) {
  Object.defineProperties(compatibilityBindings, Object.getOwnPropertyDescriptors(ownerBindings));
}
Object.freeze(compatibilityBindings);

const compatibilityFacade = true;
export { compatibilityBindings, compatibilityFacade };
