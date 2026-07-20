let _renderFromCache = null;

function registerSidebarRenderer(renderer){
  if(typeof renderer!=='function') throw new TypeError('sidebar renderer must be a function');
  _renderFromCache=renderer;
}

const renderSessionListFromCache=(...args)=>{
  if(!_renderFromCache) return undefined;
  return _renderFromCache(...args);
};

export { registerSidebarRenderer, renderSessionListFromCache };
