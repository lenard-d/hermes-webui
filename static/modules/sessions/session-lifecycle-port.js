let lifecycleOwner=null;

function registerSessionLifecycle(owner){
  if(!owner||typeof owner.load!=='function'||typeof owner.create!=='function'){
    throw new TypeError('session lifecycle owner must provide load and create');
  }
  lifecycleOwner=owner;
}

function _requireLifecycleOwner(){
  if(!lifecycleOwner) throw new Error('session lifecycle owner is not registered');
  return lifecycleOwner;
}

function loadSession(...args){
  return _requireLifecycleOwner().load(...args);
}

function newSession(...args){
  return _requireLifecycleOwner().create(...args);
}

function isNewSessionInFlight(){
  const owner=_requireLifecycleOwner();
  return typeof owner.isCreating==='function'&&owner.isCreating();
}

export { isNewSessionInFlight, loadSession, newSession, registerSessionLifecycle };
