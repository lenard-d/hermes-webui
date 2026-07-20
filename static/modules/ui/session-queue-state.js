const SESSION_QUEUES={};  // keyed by session_id for queued follow-up turns
// Tracks which session's queue to drain in setBusy(false).
// Set to activeSid just before setBusy(false) in done/error handlers so the
// queue drains the session that *finished*, not the one currently viewed.
// Single-shot: setBusy() reads and clears this on every call. Concurrent
// back-to-back stream completions would overwrite it, but HTTPServer is
// single-threaded so only one done event fires at a time in practice.
let _queueDrainSid=null;
function _getSessionQueue(sid, create=false){
  if(!sid) return [];
  if(!SESSION_QUEUES[sid]&&create) SESSION_QUEUES[sid]=[];
  return SESSION_QUEUES[sid]||[];
}
function _queueStorageKey(sid){
  return 'hermes-queue-'+sid;
}
function _clearPersistedSessionQueue(sid){
  if(!sid) return;
  const key=_queueStorageKey(sid);
  try{sessionStorage.removeItem(key);}catch(_){}
  try{localStorage.removeItem(key);}catch(_){}
}
function _persistSessionQueueStorage(sid, queue){
  if(!sid) return;
  const q=Array.isArray(queue)?queue:[];
  if(!q.length){_clearPersistedSessionQueue(sid);return;}
  const key=_queueStorageKey(sid);
  let payload='[]';
  try{payload=JSON.stringify(q);}catch(_){return;}
  try{sessionStorage.setItem(key,payload);}catch(_){}
  try{localStorage.setItem(key,payload);}catch(_){}
}
function _readPersistedSessionQueue(sid){
  if(!sid) return [];
  const key=_queueStorageKey(sid);
  const read=(store)=>{
    try{
      const raw=store&&store.getItem?store.getItem(key):null;
      if(!raw) return null;
      const parsed=JSON.parse(raw);
      return Array.isArray(parsed)?parsed:null;
    }catch(_){return null;}
  };
  const sessionValue=read(sessionStorage);
  if(sessionValue&&sessionValue.length) return sessionValue;
  const localValue=read(localStorage);
  if(localValue&&localValue.length){
    try{sessionStorage.setItem(key,JSON.stringify(localValue));}catch(_){}
    return localValue;
  }
  return [];
}
function queueSessionMessage(sid, payload){
  if(!sid||!payload) return 0;
  const q=_getSessionQueue(sid,true);
  // Stamp created_at so the restore path can detect stale entries (agent already responded)
  const entry={...payload, _queued_at: Date.now()};
  q.push(entry);
  _persistSessionQueueStorage(sid,q);
  return q.length;
}
function shiftQueuedSessionMessage(sid){
  const q=_getSessionQueue(sid,false);
  if(!q.length) return null;
  const next=q.shift();
  if(!q.length){
    delete SESSION_QUEUES[sid];
    _clearPersistedSessionQueue(sid);
  } else {
    _persistSessionQueueStorage(sid,q);
  }
  return next;
}
function getQueuedSessionCount(sid){
  return _getSessionQueue(sid,false).length;
}

export {
  _getSessionQueue,
  _queueStorageKey,
  _clearPersistedSessionQueue,
  _persistSessionQueueStorage,
  _readPersistedSessionQueue,
  queueSessionMessage,
  shiftQueuedSessionMessage,
  getQueuedSessionCount,
  SESSION_QUEUES,
  _queueDrainSid,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _getSessionQueue: { enumerable: true, get: () => _getSessionQueue, set: value => { _getSessionQueue = value; } },
  _queueStorageKey: { enumerable: true, get: () => _queueStorageKey, set: value => { _queueStorageKey = value; } },
  _clearPersistedSessionQueue: { enumerable: true, get: () => _clearPersistedSessionQueue, set: value => { _clearPersistedSessionQueue = value; } },
  _persistSessionQueueStorage: { enumerable: true, get: () => _persistSessionQueueStorage, set: value => { _persistSessionQueueStorage = value; } },
  _readPersistedSessionQueue: { enumerable: true, get: () => _readPersistedSessionQueue, set: value => { _readPersistedSessionQueue = value; } },
  queueSessionMessage: { enumerable: true, get: () => queueSessionMessage, set: value => { queueSessionMessage = value; } },
  shiftQueuedSessionMessage: { enumerable: true, get: () => shiftQueuedSessionMessage, set: value => { shiftQueuedSessionMessage = value; } },
  getQueuedSessionCount: { enumerable: true, get: () => getQueuedSessionCount, set: value => { getQueuedSessionCount = value; } },
  SESSION_QUEUES: { enumerable: true, get: () => SESSION_QUEUES },
  _queueDrainSid: { enumerable: true, get: () => _queueDrainSid, set: value => { _queueDrainSid = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
