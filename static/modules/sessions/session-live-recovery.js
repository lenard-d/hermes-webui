import { _messageComparableText } from './message-timeline.js';

export function _inflightHasVisibleLiveState(inflight){
  if(!inflight||typeof inflight!=='object') return false;
  if(String(inflight.lastAssistantText||'').trim()) return true;
  if(String(inflight.lastReasoningText||'').trim()) return true;
  if(String(inflight.liveTurnHtml||'').trim()) return true;
  if(Array.isArray(inflight.toolCalls)&&inflight.toolCalls.length) return true;
  if(Array.isArray(inflight.activityBurstAnchors)&&inflight.activityBurstAnchors.length) return true;
  if(Array.isArray(inflight.messages)){
    return inflight.messages.some((msg)=>{
      if(!msg) return false;
      if(msg.role === 'user') return Boolean(_messageComparableText(msg));
      if(msg.role !== 'assistant') return false;
      const content=msg.content;
      if(typeof content==='string') return content.trim();
      if(Array.isArray(content)) return content.length>0;
      return Boolean(content);
    });
  }
  return false;
}

function _serverLiveSnapshotToolId(toolCall){
  return String(toolCall&&(toolCall.tid||toolCall.id||toolCall.tool_call_id||toolCall.tool_use_id||toolCall.call_id||'')||'').trim();
}

export function _serverLiveSnapshotInflight(snapshot, uploaded){
  if(!snapshot||typeof snapshot!=='object') return null;
  const rawMessages=Array.isArray(snapshot.messages)?snapshot.messages:[];
  const messages=rawMessages
    .filter((message)=>message&&message.role)
    .map((message)=>({...message,_live:message._live!==false,_journal_snapshot:true}));
  const rawToolCalls=Array.isArray(snapshot.tool_calls)?snapshot.tool_calls:[];
  const toolCalls=rawToolCalls
    .filter((toolCall)=>toolCall&&toolCall.name)
    .map((toolCall)=>{
      const next={...toolCall,_live:true,_journal_snapshot:true};
      const tid=_serverLiveSnapshotToolId(next);
      if(tid&&!next.tid) next.tid=tid;
      return next;
    });
  let lastAssistantText=String(snapshot.last_assistant_text||snapshot.lastAssistantText||'');
  let lastReasoningText=String(snapshot.last_reasoning_text||snapshot.lastReasoningText||'');
  const lastLiveAssistant=[...messages].reverse().find((message)=>message&&message.role==='assistant'&&message._live);
  if(lastLiveAssistant){
    if(!lastAssistantText&&typeof lastLiveAssistant.content==='string') lastAssistantText=lastLiveAssistant.content;
    if(!lastReasoningText&&typeof lastLiveAssistant.reasoning==='string') lastReasoningText=lastLiveAssistant.reasoning;
  }
  if((lastAssistantText||lastReasoningText)&&!lastLiveAssistant){
    messages.push({
      role:'assistant',
      content:lastAssistantText,
      reasoning:lastReasoningText||undefined,
      _live:true,
      _journal_snapshot:true,
    });
  }
  const replayAfterSeq=Number(snapshot.last_seq||0);
  const activityBurstAnchors=Array.isArray(snapshot.activity_burst_anchors)
    ?snapshot.activity_burst_anchors
    :(Array.isArray(snapshot.activityBurstAnchors)?snapshot.activityBurstAnchors:[]);
  const anchorActivityScene=(snapshot.anchor_activity_scene&&snapshot.anchor_activity_scene.version==='activity_scene_v1')
    ?snapshot.anchor_activity_scene
    :((snapshot.anchorActivityScene&&snapshot.anchorActivityScene.version==='activity_scene_v1')?snapshot.anchorActivityScene:null);
  const hasAnchorActivityScene=!!(anchorActivityScene&&Array.isArray(anchorActivityScene.activity_rows)&&anchorActivityScene.activity_rows.length);
  if(!messages.length&&!toolCalls.length&&!lastAssistantText&&!lastReasoningText&&!hasAnchorActivityScene) return null;
  return {
    streamId:String(snapshot.stream_id||snapshot.streamId||''),
    messages,
    uploaded:Array.isArray(uploaded)?[...uploaded]:[],
    toolCalls,
    todos:null,
    todoStateMeta:null,
    reattach:true,
    journalSnapshot:true,
    lastAssistantText,
    lastReasoningText,
    lastRunJournalSeq:Number.isFinite(replayAfterSeq)?Math.max(0,replayAfterSeq):0,
    lastRunJournalEventId:String(snapshot.last_event_id||snapshot.lastEventId||''),
    anchorActivityScene,
    currentActivityBurstId:Number(snapshot.current_activity_burst_id||snapshot.currentActivityBurstId||0)||0,
    currentLiveSegmentSeq:Number(snapshot.current_live_segment_seq||snapshot.currentLiveSegmentSeq||0)||0,
    activityBurstAnchors,
  };
}

export function _selectLiveRecoveryInflight(localInflight, serverLiveSnapshot, activeStreamId){
  if(!serverLiveSnapshot) return localInflight||null;
  if(!localInflight||!_inflightHasVisibleLiveState(localInflight)) return serverLiveSnapshot;

  const requestedActiveId=String(activeStreamId||'').trim();
  const localId=String(localInflight.streamId||'').trim();
  const serverId=String(serverLiveSnapshot.streamId||'').trim();
  const activeId=requestedActiveId||serverId;
  const selectDurableSnapshot=()=>{
    if(activeId&&localId===activeId&&Array.isArray(localInflight.todos)&&localInflight.todoStateMeta){
      return {...serverLiveSnapshot,todos:localInflight.todos,todoStateMeta:localInflight.todoStateMeta};
    }
    return serverLiveSnapshot;
  };
  if(requestedActiveId&&serverId&&serverId!==requestedActiveId){
    return localId===requestedActiveId?localInflight:null;
  }
  if(activeId&&localId!==activeId) return selectDurableSnapshot();

  const localSeq=Math.max(0,Number(localInflight.lastRunJournalSeq)||0);
  const serverSeq=Math.max(0,Number(serverLiveSnapshot.lastRunJournalSeq)||0);
  return serverSeq>=localSeq?selectDurableSnapshot():localInflight;
}

function _anchorActivitySceneStreamId(scene){
  if(!scene||typeof scene!=='object') return '';
  const identity=scene.identity&&typeof scene.identity==='object'?scene.identity:null;
  return String(scene.stream_id||scene.streamId||(identity&&(identity.stream_id||identity.streamId))||'').trim();
}

function _anchorActivitySceneMatchesStream(scene, activeStreamId){
  const activeId=String(activeStreamId||'').trim();
  if(!activeId) return true;
  const sceneId=_anchorActivitySceneStreamId(scene);
  return !sceneId||sceneId===activeId;
}

function _runtimeJournalAnchorActivitySceneForSession(sid, activeStreamId){
  const inflight=INFLIGHT&&sid?INFLIGHT[sid]:null;
  if(inflight&&inflight.anchorActivityScene&&inflight.anchorActivityScene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(inflight.anchorActivityScene,activeStreamId)){
    return inflight.anchorActivityScene;
  }
  const snapshot=S.session&&S.session.runtime_journal_snapshot;
  const scene=snapshot&&(snapshot.anchor_activity_scene||snapshot.anchorActivityScene);
  return scene&&scene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(scene,activeStreamId)?scene:null;
}

export function _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid){
  if(!activeStreamId||typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneSnapshotForStream!=='function') return false;
  const scene=_runtimeJournalAnchorActivitySceneForSession(sid,activeStreamId);
  if(!scene) return false;
  return !!window._renderLiveAnchorActivitySceneSnapshotForStream(activeStreamId,scene,sid);
}

export const sessionLiveRecovery=Object.freeze({
  hasVisibleState:_inflightHasVisibleLiveState,
  fromServerSnapshot:_serverLiveSnapshotInflight,
  select:_selectLiveRecoveryInflight,
  renderAnchorScene:_renderRuntimeJournalAnchorActivityScene,
});
