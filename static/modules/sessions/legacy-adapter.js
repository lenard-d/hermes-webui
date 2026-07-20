// Temporary seam for classic scripts and extension globals.
// Session internals never read this object; they import their owners directly.
export function installLegacySessionGlobals(root,{sessions,bindings}){
  if(!root||!sessions||!bindings) throw new Error('session legacy adapter requires a root, sessions, and bindings');
  Object.defineProperty(root,'HermesSessions',{value:sessions,writable:true,configurable:true,enumerable:false});
  for(const [name,binding] of Object.entries(bindings)){
    const descriptor={configurable:true,enumerable:false,get:binding.get};
    if(typeof binding.set==='function') descriptor.set=binding.set;
    Object.defineProperty(root,name,descriptor);
  }
  return sessions;
}
