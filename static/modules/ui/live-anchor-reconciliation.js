import { _sanitizeThinkingDisplayText, scrollIfPinned } from './activity-and-scroll.js';
import { _firstValidTimestampSeconds, _messageUserUnpinned, _nearBottomCount, _scrollPinned, compatibilityBindings as composerControlsBindings } from './composer-controls.js';
import { _decorateTransparentEventRow, _syncTransparentEventControls } from './activity-presentation.js';
import { _assistantTurnBlocks } from './assistant-turn-presentation.js';
import { _renderThinkingInto } from './thinking-lifecycle.js';
import { $, S } from './state.js';
import { _syncToolCallGroupSummary } from './worklog-tool-groups.js';
import { _renderWorklogReasonInto } from './worklog-reasoning.js';

function _liveProcessedWorklogAnchorScore(group, index){
  if(!group) return -1;
  const hasRows=!!group.querySelector('.tool-card-row,.wl-reason,.agent-activity-thinking,[data-anchor-scene-row="1"]');
  const label=group.querySelector('.tool-worklog-label,.tool-call-group-label');
  const text=String(label&&label.textContent||'').trim();
  const hasElapsed=!!(group.getAttribute('data-active-turn-elapsed')||/\d/.test(text));
  let score=index;
  if(hasElapsed) score+=400;
  if(hasRows) score+=200;
  if(group.getAttribute('data-live-activity-current')==='1') score+=80;
  if(group.getAttribute('data-anchor-scene-owner')==='1') score+=40;
  if(group.getAttribute('data-live-tool-call-group')==='1') score+=20;
  return score;
}
function _dedupeLiveProcessedWorklogAnchors(turn){
  const blocks=_assistantTurnBlocks(turn||$('liveAssistantTurn'));
  if(!blocks) return null;
  const groups=Array.from(blocks.querySelectorAll(
    '.tool-worklog-group[data-tool-worklog-group="1"],'+
    '.tool-call-group[data-tool-worklog-group="1"],'+
    '.live-worklog[data-live-worklog-shell="1"]'
  )).filter(group=>group&&group.isConnected!==false);
  if(groups.length<=1) return groups[0]||null;
  let keep=groups[0];
  let keepScore=_liveProcessedWorklogAnchorScore(keep,0);
  groups.forEach((group,index)=>{
    const score=_liveProcessedWorklogAnchorScore(group,index);
    if(score>=keepScore){
      keep=group;
      keepScore=score;
    }
  });
  groups.forEach(group=>{
    if(group!==keep) group.remove();
  });
  if(keep&&typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(keep);
  return keep;
}
function isLiveAnchorActivitySceneOwner(streamId){
  const turn=$('liveAssistantTurn');
  if(!turn) return false;
  const owner=turn.getAttribute('data-anchor-scene-live-owner')==='1'||
    !!turn.querySelector('[data-live-anchor-scene-owner="1"],[data-anchor-scene-row="1"]');
  if(!owner) return false;
  const current=turn.getAttribute('data-anchor-stream-id')||'';
  return !streamId||!current||String(streamId)===current;
}
function _projectLiveAnchorActivitySceneForStream(streamId, mode){
  const api=(typeof window!=='undefined')?window.HermesAssistantTurnAnchors:null;
  const map=(typeof window!=='undefined')?window._liveAnchorRegistries:null;
  const registry=map&&streamId?map.get(streamId):null;
  if(!api||!registry||typeof api.projectAssistantTurnAnchorActivityScene!=='function') return null;
  try{
    return api.projectAssistantTurnAnchorActivityScene(registry,{mode:mode||'compact_worklog'});
  }catch(err){
    if(typeof console!=='undefined'&&console.warn) console.warn('assistant turn anchor scene projection failed',err);
    return null;
  }
}
function _prepareLiveAnchorScrollRebuildGuard(scrollSnapshot){
  const messagesEl=$('messages');
  if(!messagesEl||!scrollSnapshot) return {readerAwayFromBottom:false,release:null};
  const beforeBottomDistance=Math.max(0,messagesEl.scrollHeight-messagesEl.scrollTop-messagesEl.clientHeight);
  // Only treat the reader as away if they were ALREADY in a non-follow state.
  // A pinned follower can transiently have bottomDistance>250 mid-render (the
  // assistant body grows before the anchor scene re-renders), so keying on a
  // raw scrollTop>0 here would mis-classify a pinned reader as unpinned and kill
  // auto-follow. Require an explicit unpin/non-pinned signal instead.
  const readerAwayFromBottom=beforeBottomDistance>250&&(_messageUserUnpinned||_scrollPinned===false);
  if(!readerAwayFromBottom) return {readerAwayFromBottom:false,release:null};
  scrollSnapshot.pinned=false;
  scrollSnapshot.userUnpinned=true;
  scrollSnapshot.bottom=beforeBottomDistance;
  composerControlsBindings._messageUserUnpinned=true;
  composerControlsBindings._scrollPinned=false;
  composerControlsBindings._nearBottomCount=0;
  const msgInner=$('msgInner');
  if(!msgInner||!msgInner.style) return {readerAwayFromBottom:true,release:null};
  const guardPreviousKey='liveAnchorScrollGuardPreviousMinHeight';
  let previousMinHeight=msgInner.style.minHeight||'';
  if(msgInner.dataset&&Object.prototype.hasOwnProperty.call(msgInner.dataset,guardPreviousKey)){
    previousMinHeight=msgInner.dataset[guardPreviousKey]||'';
  }else if(msgInner.dataset){
    msgInner.dataset[guardPreviousKey]=previousMinHeight;
  }
  const guardHeight=Math.max(messagesEl.scrollHeight,Number(scrollSnapshot.scrollHeight)||0);
  if(guardHeight>0) msgInner.style.minHeight=`${guardHeight}px`;
  return {
    readerAwayFromBottom:true,
    release:()=>{
      msgInner.style.minHeight=previousMinHeight;
      if(msgInner.dataset&&msgInner.dataset[guardPreviousKey]===previousMinHeight){
        delete msgInner.dataset[guardPreviousKey];
      }
    },
  };
}
function _resetMismatchedLiveAssistantTurnForSession(turn, sessionId){
  const sid=String(sessionId||'');
  if(!turn||!sid||!turn.dataset) return false;
  const existingSid=String(turn.dataset.sessionId||'');
  if(!existingSid||existingSid===sid) return false;
  const blocks=typeof _assistantTurnBlocks==='function' ? _assistantTurnBlocks(turn) : turn;
  if(blocks){
    try{
      blocks.innerHTML='';
    }catch(_){
      while(blocks.firstChild) blocks.removeChild(blocks.firstChild);
    }
  }
  turn.dataset.sessionId=sid;
  return true;
}
function _liveAnchorReasoningRowForFallback(turn, opts){
  opts=opts||{};
  const blocks=typeof _assistantTurnBlocks==='function' ? _assistantTurnBlocks(turn) : turn;
  if(!turn||!blocks||!blocks.querySelectorAll) return null;
  const streamId=String(opts.streamId||S.activeStreamId||'');
  const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');
  const localId=String(opts.anchorReasoningLocalId||opts.localId||'').trim();
  if(!localId) return null;
  const rows=blocks.querySelectorAll(
    '[data-anchor-scene-row="1"][data-anchor-local-id]'
  );
  for(const row of Array.from(rows)){
    const anchorLocalId=String(row.getAttribute&&row.getAttribute('data-anchor-local-id')||'');
    if(anchorLocalId!==localId) continue;
    const rowRole=String(row.getAttribute&&row.getAttribute('data-anchor-row-role')||'');
    const rowSource=String(row.getAttribute&&row.getAttribute('data-anchor-source-event-type')||'');
    if(rowRole!=='thinking'&&rowSource!=='reasoning') continue;
    const rowStreamId=String(row.getAttribute&&row.getAttribute('data-anchor-stream-id')||'');
    if(rowStreamId&&streamId&&rowStreamId!==streamId) continue;
    const rowSessionId=String(row.getAttribute&&row.getAttribute('data-session-id')||'');
    if(rowSessionId&&sessionId&&rowSessionId!==sessionId) continue;
    return row;
  }
  return null;
}
function _updateLiveAnchorReasoningRowForFallback(turn, text, opts){
  const clean=_sanitizeThinkingDisplayText(text);
  if(!clean||window._showThinking===false) return false;
  const row=_liveAnchorReasoningRowForFallback(turn, opts);
  if(!row) return false;
  if(row.classList&&row.classList.contains('wl-reason')){
    if(typeof _renderWorklogReasonInto==='function') _renderWorklogReasonInto(row, clean);
    else row.textContent=clean;
    const group=row.closest&&row.closest('.tool-worklog-group,.tool-call-group,.live-worklog');
    if(group&&typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(group);
  }else if(row.classList&&row.classList.contains('transparent-event-row')){
    _renderThinkingInto(row, clean);
    const eventAt=row.getAttribute&&row.getAttribute('data-event-at');
    const nextTs=typeof _firstValidTimestampSeconds==='function'
      ? _firstValidTimestampSeconds(opts&&opts.ts, opts&&opts.timestamp, opts&&opts.created_at, eventAt)
      : null;
    if(typeof _decorateTransparentEventRow==='function'){
      _decorateTransparentEventRow(row,{
        type:'thinking',
        text:clean,
        preview:clean,
        ts:nextTs||undefined,
        live:true,
        segmentSeq:opts&&opts.segmentSeq,
        burstId:opts&&opts.burstId,
      });
    }
  }else{
    _renderThinkingInto(row, clean);
  }
  if(turn&&typeof _syncTransparentEventControls==='function') _syncTransparentEventControls(turn);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
  return true;
}

export {
  _liveProcessedWorklogAnchorScore,
  _dedupeLiveProcessedWorklogAnchors,
  isLiveAnchorActivitySceneOwner,
  _projectLiveAnchorActivitySceneForStream,
  _prepareLiveAnchorScrollRebuildGuard,
  _resetMismatchedLiveAssistantTurnForSession,
  _liveAnchorReasoningRowForFallback,
  _updateLiveAnchorReasoningRowForFallback,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _liveProcessedWorklogAnchorScore: { enumerable: true, get: () => _liveProcessedWorklogAnchorScore, set: (value) => { _liveProcessedWorklogAnchorScore = value; } },
  _dedupeLiveProcessedWorklogAnchors: { enumerable: true, get: () => _dedupeLiveProcessedWorklogAnchors, set: (value) => { _dedupeLiveProcessedWorklogAnchors = value; } },
  isLiveAnchorActivitySceneOwner: { enumerable: true, get: () => isLiveAnchorActivitySceneOwner, set: (value) => { isLiveAnchorActivitySceneOwner = value; } },
  _projectLiveAnchorActivitySceneForStream: { enumerable: true, get: () => _projectLiveAnchorActivitySceneForStream, set: (value) => { _projectLiveAnchorActivitySceneForStream = value; } },
  _prepareLiveAnchorScrollRebuildGuard: { enumerable: true, get: () => _prepareLiveAnchorScrollRebuildGuard, set: (value) => { _prepareLiveAnchorScrollRebuildGuard = value; } },
  _resetMismatchedLiveAssistantTurnForSession: { enumerable: true, get: () => _resetMismatchedLiveAssistantTurnForSession, set: (value) => { _resetMismatchedLiveAssistantTurnForSession = value; } },
  _liveAnchorReasoningRowForFallback: { enumerable: true, get: () => _liveAnchorReasoningRowForFallback, set: (value) => { _liveAnchorReasoningRowForFallback = value; } },
  _updateLiveAnchorReasoningRowForFallback: { enumerable: true, get: () => _updateLiveAnchorReasoningRowForFallback, set: (value) => { _updateLiveAnchorReasoningRowForFallback = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
