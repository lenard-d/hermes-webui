const _composerMenuClosers=new Map();

function registerComposerMenu(name,close){
  const key=String(name||'').trim();
  if(!key||typeof close!=='function') throw new TypeError('composer menu registration requires a name and close function');
  _composerMenuClosers.set(key,close);
  return ()=>{if(_composerMenuClosers.get(key)===close) _composerMenuClosers.delete(key);};
}

function closeComposerMenu(name){
  const close=_composerMenuClosers.get(String(name||'').trim());
  if(typeof close==='function') close();
}

function closeOtherComposerMenus(activeName){
  const active=String(activeName||'').trim();
  for(const [name,close] of _composerMenuClosers){
    if(name!==active) close();
  }
}

export {
  registerComposerMenu,
  closeComposerMenu,
  closeOtherComposerMenus,
  _composerMenuClosers,
};

const compatibilityBindings = {};
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
