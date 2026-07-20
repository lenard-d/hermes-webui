import { renderMd } from './markdown-renderer.js';
import { _renderMessagesWithScrollSnapshot } from './render-support.js';
import { S, esc } from './state.js';

function _isHandoffSummaryToolPayload(value){
  if(!value||typeof value!=='object'||Array.isArray(value)) return false;
  return value._handoff_summary_card === true;
}
function _parseHandoffSummaryPayload(content){
  if(!content) return null;
  if(typeof content==='object' && !Array.isArray(content)) return _isHandoffSummaryToolPayload(content)?content:null;
  if(typeof content!=='string') return null;
  try {
    const parsed=JSON.parse(content);
    return _isHandoffSummaryToolPayload(parsed)?parsed:null;
  } catch (e) {
    return null;
  }
}
function _handoffSummaryStateFromMessage(m){
  if(!m||m.role!=='tool') return null;
  const payload = _parseHandoffSummaryPayload(m.content);
  if(!payload) return null;
  if(String(payload.session_id||'') && S.session && String(m.session_id||'') && String(payload.session_id)!==String(S.session.session_id||'')) {
    return null;
  }
  const summary = String(payload.summary||'').trim();
  if(!summary) return null;
  return {
    phase: 'done',
    channel: payload.channel || null,
    rounds: Number.isFinite(payload.rounds)?payload.rounds:null,
    summary,
    fallback: !!payload.fallback,
    generatedAt: Number(payload.generated_at) || null,
  };
}
function _collectHandoffSummaryStates(messages){
  const states=[];
  if(!Array.isArray(messages)) return states;
  for(let i=0;i<messages.length;i++){
    const state=_handoffSummaryStateFromMessage(messages[i]);
    if(state) states.push({state, rawIdx:i});
  }
  return states;
}

function _handoffStateForCurrentSession(){
  const state=window._handoffUi;
  if(!state||!S.session||state.sessionId!==S.session.session_id) return null;
  return state;
}
function clearHandoffUi(){
  window._handoffUi=null;
  _renderMessagesWithScrollSnapshot();
}
function setHandoffUi(state){
  if(!state){
    clearHandoffUi();
    return;
  }
  window._handoffUi={...state};
  _renderMessagesWithScrollSnapshot();
}
function _handoffCardsHtml(state){
  if(!state) return '';
  const channel=String(state.channel||'').trim();
  const label=channel?`${channel} handoff summary`:'Handoff summary';
  const isError=state.phase==='error';
  const isDone=state.phase==='done';
  const isFallback=!!state.fallback;
  const detail=isError
    ? String(state.errorText||'Could not generate summary. Please try again.')
    : isDone
      ? String(state.summary||'')
      : 'Generating handoff summary...';
  const meta=typeof state.rounds==='number'
    ? `${state.rounds} external conversation rounds`
    : '';
  const icon=isError
    ? li('x',13)
    : isDone
      ? li('check',13)
      : '<span class="tool-card-running-dot"></span>';
  const bodyHtml=isDone&&!isError
    ? (
      `${renderMd(detail)}${
        isFallback
          ? '<p class="handoff-summary-fallback-note">Fallback summary generated from recent turns; no model-based rewrite was used.</p>'
          : ''
      }`
    )
    : `<p>${esc(detail)}</p>`;
  return `
    <div class="tool-card-row compression-card-row handoff-card-row" data-compression-card="1" data-handoff-card="1">
      <div class="tool-card tool-card-handoff-summary${isError?' tool-card-compress-error':''} open">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          ${icon}
          <span class="tool-card-name">${esc(label)}</span>
          ${meta?`<span class="tool-card-preview">${esc(meta)}</span>`:''}
          <span class="tool-card-toggle">${li('chevron-right',12)}</span>
        </div>
        <div class="tool-card-detail">
          <div class="tool-card-result handoff-summary-body">${bodyHtml}</div>
        </div>
      </div>
    </div>`;
}
function _handoffCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn handoff-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_handoffCardsHtml(state)}</div>`;
  return wrap;
}

export {
  _isHandoffSummaryToolPayload,
  _parseHandoffSummaryPayload,
  _handoffSummaryStateFromMessage,
  _collectHandoffSummaryStates,
  _handoffStateForCurrentSession,
  clearHandoffUi,
  setHandoffUi,
  _handoffCardsHtml,
  _handoffCardsNode,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _isHandoffSummaryToolPayload: { enumerable: true, get: () => _isHandoffSummaryToolPayload, set: (value) => { _isHandoffSummaryToolPayload = value; } },
  _parseHandoffSummaryPayload: { enumerable: true, get: () => _parseHandoffSummaryPayload, set: (value) => { _parseHandoffSummaryPayload = value; } },
  _handoffSummaryStateFromMessage: { enumerable: true, get: () => _handoffSummaryStateFromMessage, set: (value) => { _handoffSummaryStateFromMessage = value; } },
  _collectHandoffSummaryStates: { enumerable: true, get: () => _collectHandoffSummaryStates, set: (value) => { _collectHandoffSummaryStates = value; } },
  _handoffStateForCurrentSession: { enumerable: true, get: () => _handoffStateForCurrentSession, set: (value) => { _handoffStateForCurrentSession = value; } },
  clearHandoffUi: { enumerable: true, get: () => clearHandoffUi, set: (value) => { clearHandoffUi = value; } },
  setHandoffUi: { enumerable: true, get: () => setHandoffUi, set: (value) => { setHandoffUi = value; } },
  _handoffCardsHtml: { enumerable: true, get: () => _handoffCardsHtml, set: (value) => { _handoffCardsHtml = value; } },
  _handoffCardsNode: { enumerable: true, get: () => _handoffCardsNode, set: (value) => { _handoffCardsNode = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
