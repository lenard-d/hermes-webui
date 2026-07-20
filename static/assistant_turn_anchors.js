// Stable Assistant Turn Anchors public compatibility facade (#3926 / #3400).
// The browser directly loads model.js and activity_scene.js before this file.
(function assembleAssistantTurnAnchors(root){
  const parts=root.HermesAssistantTurnAnchorParts;
  if(!parts||typeof parts.createModel!=='function'||typeof parts.createScene!=='function'){
    throw new Error('assistant turn anchor parts must load before the facade');
  }
  const model=parts.createModel();
  const scene=parts.createScene(model.sceneSupport);
  root.HermesAssistantTurnAnchors=Object.freeze({
    version:'slice8-renderer-snapshot-adapter',
    ...model.publicApi,
    ...scene,
  });
  delete root.HermesAssistantTurnAnchorParts;
})(typeof window!=='undefined'?window:globalThis);
