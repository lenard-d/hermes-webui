// Stable composer interface. New code should import the semantic owner directly.
export { renderMd } from './markdown-renderer.js';

export {
  setComposerStatus,
  lockComposerForClarify,
  unlockComposerForClarify,
  _composerHasContent,
  _getExplicitBusyCommandAction,
  getComposerPrimaryAction,
  _applyBusyComposerPlaceholder,
  _setComposerPrimaryButtonIcon,
  updateSendBtn,
  setBusy,
  handleComposerPrimaryAction,
} from './composer-primary-control.js';

export {
  _clearQueueCardDisplay,
  _renderQueueChips,
  _updateQueuePill,
  updateQueueBadge,
  _queueRenderKeys,
  _queueCollapsed,
  _queueRenderEpoch,
} from './composer-queue.js';

export { _composerLockState, _compressionPlaceholderSaved } from './composer-state.js';

export {
  setStatus,
  clearToastDismissTimer,
  setToastDismissTimer,
  dismissToast,
  copyToastText,
  showToast,
  TOAST_DEFAULT_MS,
  TOAST_ERROR_DEFAULT_MS,
} from './toast-notifications.js';

import { compatibilityBindings as primaryControlBindings } from './composer-primary-control.js';
import { compatibilityBindings as queueBindings } from './composer-queue.js';
import { compatibilityBindings as composerStateBindings } from './composer-state.js';
import { compatibilityBindings as markdownBindings } from './markdown-renderer.js';
import { compatibilityBindings as toastBindings } from './toast-notifications.js';

const compatibilityBindings = {};
for (const ownerBindings of [
  markdownBindings,
  primaryControlBindings,
  queueBindings,
  composerStateBindings,
  toastBindings,
]) {
  Object.defineProperties(compatibilityBindings, Object.getOwnPropertyDescriptors(ownerBindings));
}
Object.freeze(compatibilityBindings);

const compatibilityFacade = true;
export { compatibilityBindings, compatibilityFacade };
