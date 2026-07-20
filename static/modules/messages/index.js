import {
  _chatPayloadModel,
  _chatPayloadModelState,
  _extractInlineThinkingFromContent,
  _isSessionActivelyViewed,
} from './core.js';
import {
  _clearPendingSelections,
  insertSavedPromptIntoComposer,
  toggleSavedPromptsPopup,
} from './composer-context.js';
import { applySessionTitleUpdate, send } from './send.js';
import {
  closeLiveStream,
  closeOtherLiveStreams,
} from './stream-lifecycle.js';
import { createStreamAnchorSceneSettlement } from './anchor-scene.js';
import { createStreamRunJournalCursor } from './run-journal.js';
import { createStreamLiveToolTracker } from './live-tools.js';
import { createStreamRenderer } from './rendering.js';
import { attachLiveStream } from './stream.js';
import {
  _fetchYoloState,
  _updateYoloPill,
  activeSessionHasPendingPromptAttention,
  autoResize,
  dismissApprovalCard,
  hideApprovalCard,
  respondApproval,
  scheduleComposerAutoResize,
  startApprovalPolling,
  stopApprovalPolling,
  toggleApprovalCardCollapsed,
  toggleYoloFromApproval,
  transcript,
} from './approvals.js';
import {
  _renderPendingPromptsForActiveSession,
  hideClarifyCard,
  respondClarify,
  startClarifyPolling,
  stopClarifyPolling,
  toggleClarifyCardCollapsed,
} from './clarify.js';
import { startSessionStream, stopSessionStream } from './session-events.js';
import {
  _attentionSoundKey,
  attachBtwStream,
  hideBackgroundBadge,
  playAttentionSound,
  playNotificationSound,
  requestNotificationPermission,
  sendBrowserNotification,
  showBackgroundBadge,
  startBackgroundPolling,
} from './notifications.js';
import { enhanceMarkdownTables } from './markdown-tables.js';
import { publishCompatibilityDomain } from '../compatibility.js';

const messagesApi = {
  send,
  applySessionTitleUpdate,
  attachLiveStream,
  closeLiveStream,
  closeOtherLiveStreams,
  createStreamAnchorSceneSettlement,
  createStreamRunJournalCursor,
  createStreamLiveToolTracker,
  createStreamRenderer,
  startApprovalPolling,
  stopApprovalPolling,
  respondApproval,
  startClarifyPolling,
  stopClarifyPolling,
  respondClarify,
  startSessionStream,
  stopSessionStream,
  attachBtwStream,
  requestNotificationPermission,
  sendBrowserNotification,
  playNotificationSound,
  transcript,
  autoResize,
  scheduleComposerAutoResize,
  insertSavedPromptIntoComposer,
  toggleSavedPromptsPopup,
  enhanceMarkdownTables,
  extractInlineThinkingFromContent: _extractInlineThinkingFromContent,
};

const legacyBindings = {
  ...messagesApi,
  _attentionSoundKey,
  _chatPayloadModel,
  _chatPayloadModelState,
  _clearPendingSelections,
  _fetchYoloState,
  _isSessionActivelyViewed,
  _renderPendingPromptsForActiveSession,
  _updateYoloPill,
  activeSessionHasPendingPromptAttention,
  dismissApprovalCard,
  hideApprovalCard,
  hideBackgroundBadge,
  hideClarifyCard,
  playAttentionSound,
  showBackgroundBadge,
  startBackgroundPolling,
  toggleApprovalCardCollapsed,
  toggleClarifyCardCollapsed,
  toggleYoloFromApproval,
};

publishCompatibilityDomain('messages', {
  namespace: 'HermesMessages',
  api: Object.freeze({...messagesApi}),
  bindings: legacyBindings,
});

export {
  applySessionTitleUpdate,
  attachBtwStream,
  attachLiveStream,
  autoResize,
  closeLiveStream,
  closeOtherLiveStreams,
  createStreamAnchorSceneSettlement,
  createStreamLiveToolTracker,
  createStreamRenderer,
  createStreamRunJournalCursor,
  enhanceMarkdownTables,
  insertSavedPromptIntoComposer,
  playNotificationSound,
  requestNotificationPermission,
  respondApproval,
  respondClarify,
  scheduleComposerAutoResize,
  send,
  sendBrowserNotification,
  startApprovalPolling,
  startClarifyPolling,
  startSessionStream,
  stopApprovalPolling,
  stopClarifyPolling,
  stopSessionStream,
  toggleSavedPromptsPopup,
  transcript,
};
