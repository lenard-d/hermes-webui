// ── Durable browser recovery state (B4/B5: reload resilience) ──
const INFLIGHT_KEY = 'hermes-webui-inflight'; // localStorage key for in-flight session tracking
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state'; // localStorage snapshots for mid-stream reload recovery
const INFLIGHT_STATE_DEFAULT_LIMITS = {
  maxSessions:8,
  messages:24,
  toolCalls:48,
  stringChars:60000,
  jsonChars:1500000,
};

function _boundedInflightInt(value, fallback, min, max){
  const n=parseInt(value,10);
  if(!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}
function _getInflightStateLimits(){
  const configured=(typeof window!=='undefined'&&window._inflightStateLimits&&typeof window._inflightStateLimits==='object')?window._inflightStateLimits:{};
  return {
    maxSessions:_boundedInflightInt(configured.maxSessions, INFLIGHT_STATE_DEFAULT_LIMITS.maxSessions, 1, 25),
    messages:_boundedInflightInt(configured.messages, INFLIGHT_STATE_DEFAULT_LIMITS.messages, 1, 100),
    toolCalls:_boundedInflightInt(configured.toolCalls, INFLIGHT_STATE_DEFAULT_LIMITS.toolCalls, 1, 200),
    stringChars:_boundedInflightInt(configured.stringChars, INFLIGHT_STATE_DEFAULT_LIMITS.stringChars, 1000, 500000),
    jsonChars:_boundedInflightInt(configured.jsonChars, INFLIGHT_STATE_DEFAULT_LIMITS.jsonChars, 100000, 4000000),
  };
}

function _readInflightStateMap(){
  try{
    const raw=localStorage.getItem(INFLIGHT_STATE_KEY);
    const parsed=raw?JSON.parse(raw):{};
    return parsed&&typeof parsed==='object'?parsed:{};
  }catch(_){
    return {};
  }
}
function _isStorageQuotaError(err){
  return !!err && (
    err.name==='QuotaExceededError' ||
    err.name==='NS_ERROR_DOM_QUOTA_REACHED' ||
    err.code===22 ||
    err.code===1014
  );
}
function _truncateInflightValue(value, maxChars){
  const limits=_getInflightStateLimits();
  const stringLimit=_boundedInflightInt(maxChars, limits.stringChars, 1000, 500000);
  if(typeof value==='string'){
    if(value.length<=stringLimit) return value;
    return value.slice(0,stringLimit)+'\n\n[truncated for browser recovery storage]';
  }
  if(Array.isArray(value)) return value.map(v=>_truncateInflightValue(v, Math.max(2000, Math.floor(stringLimit/2))));
  if(value&&typeof value==='object'){
    const out={};
    for(const [k,v] of Object.entries(value)) out[k]=_truncateInflightValue(v, stringLimit);
    return out;
  }
  return value;
}
function _compactInflightState(state){
  const limits=_getInflightStateLimits();
  const messages=Array.isArray(state.messages)?state.messages.slice(-limits.messages):[];
  const toolCalls=Array.isArray(state.toolCalls)?state.toolCalls.slice(-limits.toolCalls):[];
  // Phase 2: persist the live todo snapshot so reload / SSE reattach
  // restores the panel without waiting for the next live `todo` write.
  // The list is bounded by the agent (typically <20 items) and each
  // item is small, so no per-list cap is needed beyond the existing
  // stringChars truncation in _truncateInflightValue.
  const todos=Array.isArray(state.todos)?state.todos:null;
  const todoStateMeta=(state.todoStateMeta&&typeof state.todoStateMeta==='object')?state.todoStateMeta:null;
  return _truncateInflightValue({
    streamId:state.streamId||null,
    messages,
    uploaded:Array.isArray(state.uploaded)?state.uploaded.slice(-20):[],
    toolCalls,
    lastAssistantText:state.lastAssistantText||'',
    lastReasoningText:state.lastReasoningText||'',
    lastRunJournalSeq:state.lastRunJournalSeq||0,
    lastRunJournalEventId:state.lastRunJournalEventId||'',
    journalReplayFromStart:!!state.journalReplayFromStart,
    currentActivityBurstId:state.currentActivityBurstId||0,
    currentLiveSegmentSeq:state.currentLiveSegmentSeq||0,
    activityBurstAnchors:Array.isArray(state.activityBurstAnchors)?state.activityBurstAnchors.slice(-50):[],
    todos,
    todoStateMeta,
  }, limits.stringChars);
}
function _writeInflightStateMap(all){
  const limits=_getInflightStateLimits();
  const entries=Object.entries(all||{})
    .sort((a,b)=>Number(b[1]&&b[1].updated_at||0)-Number(a[1]&&a[1].updated_at||0))
    .slice(0,limits.maxSessions);
  const compact={};
  for(const [sid,entry] of entries) compact[sid]=entry;
  let json=JSON.stringify(compact);
  if(json.length>limits.jsonChars){
    const current=entries[0];
    json=JSON.stringify(current?{[current[0]]:current[1]}:{});
  }
  if(json.length>limits.jsonChars){
    localStorage.removeItem(INFLIGHT_STATE_KEY);
    return false;
  }
  localStorage.setItem(INFLIGHT_STATE_KEY,json);
  return true;
}
function saveInflightState(sid, state){
  if(!sid||!state) return;
  const entry={..._compactInflightState(state),updated_at:Date.now()};
  try{
    const all=_readInflightStateMap();
    all[sid]=entry;
    _writeInflightStateMap(all);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      _writeInflightStateMap({[sid]:entry});
    }catch(_){
      try{localStorage.removeItem(INFLIGHT_STATE_KEY);}catch(__){}
    }
  }
}
function loadInflightState(sid, streamId){
  if(!sid) return null;
  const all=_readInflightStateMap();
  const entry=all[sid];
  if(!entry) return null;
  if(streamId&&entry.streamId&&entry.streamId!==streamId) return null;
  if(entry.updated_at&&Date.now()-entry.updated_at>10*60*1000){
    clearInflightState(sid);
    return null;
  }
  return entry;
}
function clearInflightState(sid){
  if(!sid) return;
  try{
    const all=_readInflightStateMap();
    if(!(sid in all)) return;
    delete all[sid];
    if(Object.keys(all).length) localStorage.setItem(INFLIGHT_STATE_KEY, JSON.stringify(all));
    else localStorage.removeItem(INFLIGHT_STATE_KEY);
  }catch(_){ }
}

export {
  INFLIGHT_KEY,
  INFLIGHT_STATE_KEY,
  INFLIGHT_STATE_DEFAULT_LIMITS,
  _boundedInflightInt,
  _getInflightStateLimits,
  _readInflightStateMap,
  _isStorageQuotaError,
  _truncateInflightValue,
  _compactInflightState,
  _writeInflightStateMap,
  saveInflightState,
  loadInflightState,
  clearInflightState,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  INFLIGHT_KEY: { enumerable: true, get: () => INFLIGHT_KEY },
  INFLIGHT_STATE_KEY: { enumerable: true, get: () => INFLIGHT_STATE_KEY },
  INFLIGHT_STATE_DEFAULT_LIMITS: { enumerable: true, get: () => INFLIGHT_STATE_DEFAULT_LIMITS },
  _boundedInflightInt: { enumerable: true, get: () => _boundedInflightInt, set: value => { _boundedInflightInt = value; } },
  _getInflightStateLimits: { enumerable: true, get: () => _getInflightStateLimits, set: value => { _getInflightStateLimits = value; } },
  _readInflightStateMap: { enumerable: true, get: () => _readInflightStateMap, set: value => { _readInflightStateMap = value; } },
  _isStorageQuotaError: { enumerable: true, get: () => _isStorageQuotaError, set: value => { _isStorageQuotaError = value; } },
  _truncateInflightValue: { enumerable: true, get: () => _truncateInflightValue, set: value => { _truncateInflightValue = value; } },
  _compactInflightState: { enumerable: true, get: () => _compactInflightState, set: value => { _compactInflightState = value; } },
  _writeInflightStateMap: { enumerable: true, get: () => _writeInflightStateMap, set: value => { _writeInflightStateMap = value; } },
  saveInflightState: { enumerable: true, get: () => saveInflightState, set: value => { saveInflightState = value; } },
  loadInflightState: { enumerable: true, get: () => loadInflightState, set: value => { loadInflightState = value; } },
  clearInflightState: { enumerable: true, get: () => clearInflightState, set: value => { clearInflightState = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
