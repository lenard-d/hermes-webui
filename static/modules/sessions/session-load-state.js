// The load lifecycle owns its generation token, active target, and the one
// carry-forward snapshot consumed while replacing a session transcript.
let _loadingSessionId = null;
let _loadSessionGeneration = 0;
let _pendingCarryForwardSnapshot = null;

export const sessionLoadState=Object.freeze({
  get loadingSessionId(){ return _loadingSessionId; },
  set loadingSessionId(value){ _loadingSessionId=value; },
  get generation(){ return _loadSessionGeneration; },
  set generation(value){ _loadSessionGeneration=value; },
  get pendingCarryForwardSnapshot(){ return _pendingCarryForwardSnapshot; },
  set pendingCarryForwardSnapshot(value){ _pendingCarryForwardSnapshot=value; },
  begin(sid){
    _loadingSessionId=sid;
    _loadSessionGeneration+=1;
    return _loadSessionGeneration;
  },
  isCurrent(sid, generation){
    return _loadingSessionId===sid&&_loadSessionGeneration===generation;
  },
  finish(sid, generation=null){
    if(_loadingSessionId!==sid) return false;
    if(generation!==null&&_loadSessionGeneration!==generation) return false;
    _loadingSessionId=null;
    return true;
  },
});
