// Temporary adapter for classic scripts and inline handlers that have not yet
// migrated to native modules. Domain modules never read through this seam.
export function installMessagesCompatibility(api, legacyGlobals) {
  if (!api || typeof api !== 'object') {
    throw new Error('messages module interface is required');
  }
  const frozenApi = Object.freeze({...api});
  globalThis.HermesMessages = frozenApi;
  Object.assign(globalThis, legacyGlobals || {});
  return frozenApi;
}
