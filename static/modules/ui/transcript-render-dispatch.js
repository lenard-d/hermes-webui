// One-way seam from navigation/editing policies into the transcript renderer.
// The renderer registers once; callers never import it back into its dependency graph.
let _transcriptRenderer=null;

function registerTranscriptRenderer(renderer){
  if(typeof renderer!=='function') throw new TypeError('transcript renderer must be a function');
  _transcriptRenderer=renderer;
}

function rerenderMessages(options){
  if(typeof _transcriptRenderer!=='function') return false;
  _transcriptRenderer(options);
  return true;
}

export { registerTranscriptRenderer, rerenderMessages };

const compatibilityBindings={};
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
