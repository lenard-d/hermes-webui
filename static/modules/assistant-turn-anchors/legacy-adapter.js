// Temporary seam for classic stream and renderer callers.
// Anchor internals communicate only through module imports and frozen interfaces.
export function installLegacyAssistantTurnAnchors(root,anchors){
  if(!root||!anchors) throw new Error('assistant turn anchor legacy adapter requires a root and anchors');
  Object.defineProperty(root,'HermesAssistantTurnAnchors',{
    value:anchors,
    writable:true,
    configurable:true,
    enumerable:false,
  });
  return anchors;
}
