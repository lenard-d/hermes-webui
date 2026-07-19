// Bounded LRU cache for rendered session transcripts.
//
// This module owns its accounting and eviction policy. Callers only provide a
// session id and a render entry; they do not mutate the backing Map directly.

function positiveLimit(value, fallback){
  const parsed=Number(value);
  return Number.isFinite(parsed)&&parsed>0?Math.floor(parsed):fallback;
}

export function createSessionRenderCache(options={}){
  const maxEntries=positiveLimit(options.maxEntries,8);
  const maxEntryBytes=positiveLimit(options.maxEntryBytes,2*1024*1024);
  const maxTotalBytes=positiveLimit(options.maxTotalBytes,8*1024*1024);
  const entries=new Map();
  let retainedBytes=0;

  function entryBytes(entry){
    // JavaScript strings are UTF-16 in memory. Budget retained browser heap,
    // not the compressed or UTF-8 wire representation.
    return String(entry&&entry.html||'').length*2;
  }

  function remove(sessionId){
    const cached=entries.get(sessionId);
    if(!cached) return false;
    retainedBytes=Math.max(0,retainedBytes-Number(cached.bytes||0));
    return entries.delete(sessionId);
  }

  function get(sessionId){
    const cached=entries.get(sessionId);
    if(!cached) return null;
    // Map insertion order is the LRU order; a successful read becomes newest.
    entries.delete(sessionId);
    entries.set(sessionId,cached);
    return cached;
  }

  function set(sessionId,entry){
    if(!sessionId||!entry||typeof entry!=='object') return false;
    const bytes=entryBytes(entry);
    remove(sessionId);
    if(bytes>maxEntryBytes||bytes>maxTotalBytes) return false;

    entries.set(sessionId,{...entry,bytes});
    retainedBytes+=bytes;
    while(entries.size>maxEntries||retainedBytes>maxTotalBytes){
      const oldestSessionId=entries.keys().next().value;
      if(oldestSessionId===undefined) break;
      remove(oldestSessionId);
    }
    return entries.has(sessionId);
  }

  function clear(){
    entries.clear();
    retainedBytes=0;
  }

  return Object.freeze({
    get,
    set,
    delete:remove,
    clear,
    has:(sessionId)=>entries.has(sessionId),
    get size(){ return entries.size; },
    get bytes(){ return retainedBytes; },
  });
}
