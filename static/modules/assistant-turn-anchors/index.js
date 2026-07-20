// Native Assistant Turn Anchor entrypoint. The model owns identity and recovery;
// the scene module receives only the immutable projection support it needs.
import { createAssistantTurnAnchorModel } from './model.js';
import { createAssistantTurnAnchorScene } from './activity-scene.js';
import { publishCompatibilityDomain } from '../compatibility.js';

const model=createAssistantTurnAnchorModel();
const scene=createAssistantTurnAnchorScene(model.sceneSupport);

export const HermesAssistantTurnAnchors=Object.freeze({
  version:'slice8-renderer-snapshot-adapter',
  ...model.publicApi,
  ...scene,
});

publishCompatibilityDomain('assistant-turn-anchors',{
  namespace:'HermesAssistantTurnAnchors',
  api:HermesAssistantTurnAnchors,
});
export default HermesAssistantTurnAnchors;
