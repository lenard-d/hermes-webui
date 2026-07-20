// Owns the two delayed jobs associated with one attached response stream:
// compact INFLIGHT persistence and live-turn DOM snapshots. Callers can request
// work repeatedly without creating one timer per token, and terminal paths have
// one cleanup operation that releases both timers.

export function createStreamProgressOwner(options={}){
  const sessionId=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const getInflight=typeof options.getInflight==='function'?options.getInflight:()=>null;
  const getUploaded=typeof options.getUploaded==='function'?options.getUploaded:()=>[];
  const getTodos=typeof options.getTodos==='function'?options.getTodos:()=>[];
  const getTodoStateMeta=typeof options.getTodoStateMeta==='function'?options.getTodoStateMeta:()=>null;
  const save=typeof options.save==='function'?options.save:()=>{};
  const snapshot=typeof options.snapshot==='function'?options.snapshot:()=>{};
  const schedule=typeof options.schedule==='function'?options.schedule:setTimeout;
  const cancel=typeof options.cancel==='function'?options.cancel:clearTimeout;
  let persistTimer=null;
  let snapshotTimer=null;

  function persistProgressState(){
    const inflight=getInflight();
    if(!inflight) return;
    save(sessionId,{
      streamId,
      messages:inflight.messages||[],
      uploaded:inflight.uploaded||getUploaded(),
      toolCalls:inflight.toolCalls||[],
      lastAssistantText:inflight.lastAssistantText||'',
      lastReasoningText:inflight.lastReasoningText||'',
      lastRunJournalSeq:inflight.lastRunJournalSeq||0,
      lastRunJournalEventId:inflight.lastRunJournalEventId||'',
      journalReplayFromStart:!!inflight.journalReplayFromStart,
      anchorActivityScene:inflight.anchorActivityScene||null,
      currentActivityBurstId:inflight.currentActivityBurstId||0,
      currentLiveSegmentSeq:inflight.currentLiveSegmentSeq||0,
      activityBurstAnchors:Array.isArray(inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[],
      todos:Array.isArray(inflight.todos)?inflight.todos:getTodos(),
      todoStateMeta:inflight.todoStateMeta||getTodoStateMeta()||null,
    });
  }

  function persistSoon(){
    if(persistTimer!==null) return;
    persistTimer=schedule(()=>{
      persistTimer=null;
      persistProgressState();
    },2000);
  }

  function captureProgressSnapshot(){
    snapshot(sessionId);
  }

  function snapshotSoon(){
    if(snapshotTimer!==null) return;
    snapshotTimer=schedule(()=>{
      snapshotTimer=null;
      captureProgressSnapshot();
    },700);
  }

  function cancelPersist(){
    if(persistTimer!==null){
      cancel(persistTimer);
      persistTimer=null;
    }
  }

  function cancelSnapshot(){
    if(snapshotTimer!==null){
      cancel(snapshotTimer);
      snapshotTimer=null;
    }
  }

  function cancelPending(){
    cancelPersist();
    cancelSnapshot();
  }

  return Object.freeze({
    cancelPending,
    cancelPersist,
    cancelSnapshot,
    persistNow:persistProgressState,
    persistSoon,
    snapshotNow:captureProgressSnapshot,
    snapshotSoon,
  });
}
