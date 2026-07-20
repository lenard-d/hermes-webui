import { _clearActivityElapsedTimer, _sanitizeThinkingDisplayText, scrollIfPinned } from './activity-and-scroll.js';
import { _renderLiveAnchorActivitySceneForStream } from './anchor-scenes.js';
import { _firstValidTimestampSeconds, _scrollPinned } from './composer-controls.js';
import { _decorateTransparentEventRow, _syncTransparentEventControls, _thinkingActivityNode, _worklogDetailsExpandedDefault, isFinalAnswerOnlyMode, isSimplifiedToolCalling, isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks, _createAssistantTurn } from './assistant-turn-presentation.js';
import { $, S, esc } from './state.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './worklog-tool-groups.js';
import { _resetMismatchedLiveAssistantTurnForSession, _updateLiveAnchorReasoningRowForFallback, isLiveAnchorActivitySceneOwner } from './live-anchor-reconciliation.js';
import { ensureLiveWorklogContainer } from './worklog-reasoning.js';

function _thinkingMarkup(text=''){
  const clean=_sanitizeThinkingDisplayText(text);
  const openClass=_worklogDetailsExpandedDefault()?' open':'';
  return (clean&&String(clean).trim())
    ? `<div class="thinking-card${openClass}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(String(clean).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
}

function _renderThinkingInto(row,text=''){
  if(!row) return;
  const clean=_sanitizeThinkingDisplayText(text);
  if(!clean){
    row.innerHTML=_thinkingMarkup(text);
    return;
  }
  const pre=row.querySelector('.thinking-card-body pre');
  if(pre){
    pre.textContent=clean;
    return;
  }
  row.innerHTML=_thinkingMarkup(text);
}

function finalizeThinkingCard(){
  const _guardTurn = $('liveAssistantTurn');
  if(_guardTurn && S.session && _guardTurn.dataset.sessionId !== S.session.session_id) return;
  if(isTransparentStream()){
    const row=$('thinkingRow');
    if(row){
      row.removeAttribute('id');
      row.removeAttribute('data-thinking-active');
      row.removeAttribute('data-live-thinking');
    }
    return;
  }
  if(!isSimplifiedToolCalling()){
    const row=$('thinkingRow');
    if(!row) return;
    const hasContent=!!row.querySelector('.thinking-card');
    if(!hasContent && row.getAttribute('data-thinking-active')==='1'){
      row.remove();
      return;
    }
    if(_scrollPinned){
      const body=row&&row.querySelector('.thinking-card-body');
      if(body) body.scrollTop=0;
    }
    row.removeAttribute('id');
    row.removeAttribute('data-thinking-active');
    return;
  }
  const turn=$('liveAssistantTurn');
  const group=turn&&turn.querySelector('.live-worklog[data-live-tool-call-group="1"],.tool-worklog-group[data-live-tool-call-group="1"],.tool-call-group[data-live-tool-call-group="1"]');
  if(group){
    const activeReason=turn.querySelector('.wl-reason[data-worklog-reason-active="1"]');
    if(activeReason) activeReason.removeAttribute('data-worklog-reason-active');
    turn.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(active=>{
      active.removeAttribute('data-thinking-active');
      active.removeAttribute('data-live-thinking');
    });
    _syncToolCallGroupSummary(group);
  }
}

function appendThinking(text='', options){
  options=options||{};
  const allowPendingPlaceholder=!!(options&&options.pending===true);
  const anchorRenderFallback=!!(options&&options.anchorRenderFallback===true);
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return;
  if(!S.session||(!S.activeStreamId&&!allowPendingPlaceholder)) return;
  if(options.sessionId&&String(options.sessionId)!==String(S.session.session_id||'')) return;
  if(options.streamId&&String(options.streamId)!==String(S.activeStreamId||'')) return;
  const existingLiveTurn=$('liveAssistantTurn');
  if(anchorRenderFallback&&existingLiveTurn&&existingLiveTurn.dataset&&
      existingLiveTurn.dataset.sessionId&&
      String(existingLiveTurn.dataset.sessionId)!==String(S.session.session_id||'')){
    if(!_resetMismatchedLiveAssistantTurnForSession(existingLiveTurn, S.session.session_id)) return;
  }
  if(anchorRenderFallback&&existingLiveTurn&&_updateLiveAnchorReasoningRowForFallback(existingLiveTurn,text,options)) return;
  if(!allowPendingPlaceholder&&!anchorRenderFallback&&isLiveAnchorActivitySceneOwner(S.activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(S.activeStreamId, S.session.session_id);
    return;
  }
  const empty=$('emptyState');
  if(empty) empty.style.display='none';
  if(!isSimplifiedToolCalling()){
    let row=$('thinkingRow');
    if(!row){
      row=document.createElement('div');
      row.id='thinkingRow';
      row.className='thinking-card-row';
      const inner=$('msgInner');
      if(inner) inner.appendChild(row);
    }
    row.setAttribute('data-thinking-active','1');
    _renderThinkingInto(row,text);
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return;
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    const inner=$('msgInner');
    if(inner) inner.appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const clean=_sanitizeThinkingDisplayText(text);
  if(clean&&window._showThinking!==false){
    const segmentSeq=options.segmentSeq!==undefined&&options.segmentSeq!==null?String(options.segmentSeq):'';
    const burstId=options.burstId!==undefined&&options.burstId!==null?String(options.burstId):'';
    const thinkingKey=String(options.thinkingKey||(
      segmentSeq?`segment:${segmentSeq}`:
      burstId?`burst:${burstId}`:
      'turn'
    ));
    if(isTransparentStream()){
      let row=blocks.querySelector(`.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="${CSS.escape(thinkingKey)}"]`);
      if(!row){
        row=_thinkingActivityNode(clean, false);
        row.id='thinkingRow';
        row.setAttribute('data-live-thinking','1');
        row.setAttribute('data-live-thinking-key',thinkingKey);
        if(segmentSeq) row.setAttribute('data-live-segment-seq',segmentSeq);
        if(burstId) row.setAttribute('data-activity-burst-id',burstId);
        blocks.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(el=>{
          if(el!==row){
            el.removeAttribute('id');
            el.removeAttribute('data-thinking-active');
            el.removeAttribute('data-live-thinking');
          }
        });
        row.setAttribute('data-thinking-active','1');
        const liveFooter=blocks.querySelector('#liveRunStatus');
        if(liveFooter&&liveFooter.parentElement===blocks) blocks.insertBefore(row,liveFooter);
        else blocks.appendChild(row);
      }else{
        _renderThinkingInto(row, clean);
      }
      row.id='thinkingRow';
      row.setAttribute('data-thinking-active','1');
      const existingEventAt=row.getAttribute('data-event-at');
      const nextTs=_firstValidTimestampSeconds(
        options.ts,
        options.timestamp,
        options.created_at,
        existingEventAt
      );
      _decorateTransparentEventRow(row,{
        type:'thinking',
        text:clean,
        preview:clean,
        ts:nextTs||undefined,
        live:true,
        segmentSeq,
        burstId,
      });
      _syncTransparentEventControls(turn);
      if(typeof scrollIfPinned==='function') scrollIfPinned();
      return;
    }
    const group=ensureLiveWorklogContainer(blocks,{
      activityKey:options.activityKey||(S.activeStreamId?'live:'+S.activeStreamId:null),
    });
    const list=_toolWorklogListEl(group);
    if(list){
      let row=list.querySelector(`.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="${CSS.escape(thinkingKey)}"]`);
      if(!row){
        row=_thinkingActivityNode(clean, false, thinkingKey);
        row.setAttribute('data-live-thinking','1');
        row.setAttribute('data-live-thinking-key',thinkingKey);
        if(segmentSeq) row.setAttribute('data-live-segment-seq',segmentSeq);
        if(burstId) row.setAttribute('data-activity-burst-id',burstId);
        list.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(el=>{
          if(el!==row){
            el.removeAttribute('data-thinking-active');
            el.removeAttribute('data-live-thinking');
          }
        });
        row.setAttribute('data-thinking-active','1');
        list.appendChild(row);
      }else{
        _renderThinkingInto(row, clean);
      }
      row.setAttribute('data-thinking-active','1');
      _syncToolCallGroupSummary(group);
    }
  }
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}

function updateThinking(text='', options){appendThinking(text, options);}

function removeThinking(){
  if(isTransparentStream()){
    const liveTurn=$('liveAssistantTurn');
    const blocks=_assistantTurnBlocks(liveTurn);
    if(blocks) blocks.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(row=>{
      row.removeAttribute('id');
      row.removeAttribute('data-thinking-active');
      row.removeAttribute('data-live-thinking');
    });
    if(liveTurn&&blocks&&!blocks.children.length) liveTurn.remove();
    return;
  }
  if(!isSimplifiedToolCalling()){
    const el=$('thinkingRow');
    if(el) el.remove();
    const liveTurn=$('liveAssistantTurn');
    const blocks=_assistantTurnBlocks(liveTurn);
    if(liveTurn&&blocks&&!blocks.children.length) liveTurn.remove();
    return;
  }
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
  if(blocks) blocks.querySelectorAll('.agent-activity-thinking:not([data-anchor-scene-row="1"])').forEach(el=>el.remove());
  if(blocks) blocks.querySelectorAll('.wl-reason[data-worklog-anchor-reason="1"],.wl-reason[data-worklog-reason-source="reasoning"]').forEach(el=>el.remove());
  if(blocks) blocks.querySelectorAll('.live-worklog[data-live-worklog-shell="1"],.tool-worklog-group[data-live-tool-call-group="1"]:not([data-anchor-scene-owner="1"]),.tool-call-group[data-live-tool-call-group="1"]:not([data-anchor-scene-owner="1"]),.tool-call-group[data-agent-activity-group="1"]:not([data-anchor-scene-owner="1"])').forEach(group=>{
    _syncToolCallGroupSummary(group);
    if(!group.querySelector('.tool-card-row,.agent-activity-thinking,.wl-reason')){
      if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
      group.remove();
    }
  });
  if(turn&&blocks&&!blocks.children.length) turn.remove();
}

export {
  _thinkingMarkup,
  _renderThinkingInto,
  finalizeThinkingCard,
  appendThinking,
  updateThinking,
  removeThinking,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _thinkingMarkup: { enumerable: true, get: () => _thinkingMarkup, set: (value) => { _thinkingMarkup = value; } },
  _renderThinkingInto: { enumerable: true, get: () => _renderThinkingInto, set: (value) => { _renderThinkingInto = value; } },
  finalizeThinkingCard: { enumerable: true, get: () => finalizeThinkingCard, set: (value) => { finalizeThinkingCard = value; } },
  appendThinking: { enumerable: true, get: () => appendThinking, set: (value) => { appendThinking = value; } },
  updateThinking: { enumerable: true, get: () => updateThinking, set: (value) => { updateThinking = value; } },
  removeThinking: { enumerable: true, get: () => removeThinking, set: (value) => { removeThinking = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
