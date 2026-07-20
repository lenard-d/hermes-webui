let _renderSessionList = null;

function registerSessionListRenderer(renderer){
  if(typeof renderer!=='function') throw new TypeError('session-list renderer must be a function');
  _renderSessionList=renderer;
}

const renderSessionList=(...args)=>{
  if(!_renderSessionList) return Promise.resolve();
  return _renderSessionList(...args);
};

export { registerSessionListRenderer, renderSessionList };
