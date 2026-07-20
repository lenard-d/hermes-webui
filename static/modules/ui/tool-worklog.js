// Stable Tool Worklog interface. New code should import the semantic owner directly.
export * from './tool-identity.js';
export * from './tool-call-presentation.js';
export * from './tool-card-presentation.js';
export * from './worklog-tool-groups.js';
export * from './live-tool-worklog.js';

import { compatibilityBindings as identityBindings } from './tool-identity.js';
import { compatibilityBindings as callPresentationBindings } from './tool-call-presentation.js';
import { compatibilityBindings as cardPresentationBindings } from './tool-card-presentation.js';
import { compatibilityBindings as groupBindings } from './worklog-tool-groups.js';
import { compatibilityBindings as liveBindings } from './live-tool-worklog.js';

const compatibilityBindings = {};
for (const ownerBindings of [
  identityBindings,
  callPresentationBindings,
  cardPresentationBindings,
  groupBindings,
  liveBindings,
]) {
  Object.defineProperties(compatibilityBindings, Object.getOwnPropertyDescriptors(ownerBindings));
}
Object.freeze(compatibilityBindings);

const compatibilityFacade = true;
export { compatibilityBindings, compatibilityFacade };
