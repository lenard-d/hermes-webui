/**
 * Temporary classic-script compatibility seam.
 *
 * Native modules import each other directly. Domain entrypoints register only
 * the globals still required by classic workspace/extension code or inline
 * handlers. Delete a binding when its final legacy caller becomes a module.
 *
 * This is the only production module allowed to write migrated-domain symbols
 * onto globalThis.
 */
const publishedDomains=new Map();

/**
 * Publish one migrated domain at the temporary classic-script boundary.
 * Binding values may be plain values or property descriptors with a getter.
 */
export function publishCompatibilityDomain(domain,{namespace=null,api=null,bindings={}}={}){
  if(!domain)throw new Error('compatibility domain name is required');
  const previous=publishedDomains.get(domain);
  if(previous){
    if(previous!==api)throw new Error(`compatibility domain already published: ${domain}`);
    return previous;
  }
  if(namespace){
    Object.defineProperty(globalThis,namespace,{
      configurable:true,
      enumerable:false,
      writable:true,
      value:api,
    });
  }
  for(const [name,binding] of Object.entries(bindings||{})){
    if(typeof binding==='undefined')continue;
    const descriptor=(binding&&typeof binding==='object'&&typeof binding.get==='function')
      ? {configurable:true,enumerable:false,get:binding.get,...(typeof binding.set==='function'?{set:binding.set}:{})}
      : {configurable:true,enumerable:true,writable:true,value:binding};
    Object.defineProperty(globalThis,name,descriptor);
  }
  publishedDomains.set(domain,api);
  return api;
}
