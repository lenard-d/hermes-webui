// Stable compatibility seam for the live transcript modules. New code should
// import the semantic owner directly; the aggregate keeps the historical
// HermesUI/global interface intact for extensions and extracted-function tests.
export const compatibilityFacade = true;

export * from './live-run-status.js';
export * from './compression-ui.js';
export * from './handoff-ui.js';
export * from './message-render-cache.js';
export * from './cli-tool-presentation.js';
export * from './message-scroll-snapshot.js';

import { compatibilityBindings as liveRunStatusBindings } from './live-run-status.js';
import { compatibilityBindings as compressionUiBindings } from './compression-ui.js';
import { compatibilityBindings as handoffUiBindings } from './handoff-ui.js';
import { compatibilityBindings as messageRenderCacheBindings } from './message-render-cache.js';
import { compatibilityBindings as cliToolPresentationBindings } from './cli-tool-presentation.js';
import { compatibilityBindings as messageScrollSnapshotBindings } from './message-scroll-snapshot.js';

const compatibilityBindings = {};
for (const ownerBindings of [
  liveRunStatusBindings,
  compressionUiBindings,
  handoffUiBindings,
  messageRenderCacheBindings,
  cliToolPresentationBindings,
  messageScrollSnapshotBindings,
]) {
  Object.defineProperties(compatibilityBindings, Object.getOwnPropertyDescriptors(ownerBindings));
}
Object.freeze(compatibilityBindings);

export { compatibilityBindings };
