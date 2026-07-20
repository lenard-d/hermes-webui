// Stable Worklog/Transparent Stream interface. New code should import the semantic owner directly.
export * from './transparent-turn-presentation.js';
export * from './worklog-disclosure.js';
export * from './worklog-reasoning.js';
export * from './worklog-step-presentation.js';
export * from './anchor-scene-presentation.js';
export * from './live-anchor-reconciliation.js';
export { _toolIdentity, _toolDisclosureIdentity } from './tool-identity.js';

import { compatibilityBindings as turnPresentationBindings } from './transparent-turn-presentation.js';
import { compatibilityBindings as disclosureBindings } from './worklog-disclosure.js';
import { compatibilityBindings as reasoningBindings } from './worklog-reasoning.js';
import { compatibilityBindings as stepBindings } from './worklog-step-presentation.js';
import { compatibilityBindings as anchorSceneBindings } from './anchor-scene-presentation.js';
import { compatibilityBindings as liveAnchorBindings } from './live-anchor-reconciliation.js';

const compatibilityBindings = {};
for (const ownerBindings of [
  turnPresentationBindings,
  disclosureBindings,
  reasoningBindings,
  stepBindings,
  anchorSceneBindings,
  liveAnchorBindings,
]) {
  Object.defineProperties(compatibilityBindings, Object.getOwnPropertyDescriptors(ownerBindings));
}
Object.freeze(compatibilityBindings);

const compatibilityFacade = true;
export { compatibilityBindings, compatibilityFacade };
