import { isCompactWorklogMode } from './activity-presentation.js';
import { _assistantTurnBlocks, _createAssistantTurn } from './assistant-turn-presentation.js';
import { _fmtTokens } from './composer-controls.js';
import { $, S } from './state.js';
import { _finalizeLiveActivityDisclosureGroup } from './transparent-worklog.js';


// ── LiveFooter timer (module-level singleton) ──────────────────────────────
const _liveRunStatusTimers={};  // keyed by sessionId, max 1 active
let _liveRunStatusTokens=null;
let _liveRunStatusSessionId=null;
function _formatRunElapsed(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'00:00';
  const total=Math.max(0,Math.floor(n));
  if(total>=3600){
    const h=Math.floor(total/3600);
    const m=Math.floor((total%3600)/60);
    return h+'h '+String(m).padStart(2,'0')+'m';
  }
  const m=Math.floor(total/60);
  const s=total%60;
  return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
}
function _moveLiveRunStatusToTurnEnd(el){
  el=el||$('liveRunStatus');
  if(!el) return null;
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
  if(blocks&&el.parentElement===blocks&&blocks.lastElementChild!==el) blocks.appendChild(el);
  return el;
}
function placeLiveRunStatusHost(){
  let el=$('liveRunStatus');
  if(!el){
    el=document.createElement('div');
    el.id='liveRunStatus';
    el.hidden=true;
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
  if(blocks&&el.parentElement!==blocks) blocks.appendChild(el);
  el.className='live-run-status live-footer';
  return _moveLiveRunStatusToTurnEnd(el);
}
function showLiveRunStatus(sid,opts){
  if(typeof isCompactWorklogMode==='function'&&isCompactWorklogMode()){
    _liveRunStatusSessionId=sid;
    _liveRunStatusTokens=opts&&opts.tokens||null;
    const el=$('liveRunStatus');
    if(el){el.hidden=true;el.innerHTML='';}
    return;
  }
  const el=placeLiveRunStatusHost();
  if(!el)return;
  _liveRunStatusSessionId=sid;
  const startedAt=opts&&opts.startedAt||null;
  _liveRunStatusTokens=opts&&opts.tokens||null;
  el.hidden=false;
  _renderLiveRunStatusContent(el,startedAt);
  _startLiveRunStatusTimer(sid,startedAt);
}
function _renderLiveRunStatusContent(el,startedAt){
  if(!el)return;
  const now=Date.now()/1000;
  const elapsed=startedAt?Math.max(0,now-startedAt):0;
  const timeStr=_formatRunElapsed(elapsed);
  const tokens=_liveRunStatusTokens;
  el.innerHTML=`<span class="live-run-status-dot tool-card-running-dot"></span><span class="live-run-status-text lf-time">${timeStr}</span>${tokens?`<span class="lf-sep">·</span><span class="lf-tokens">${_fmtTokens(tokens)} tokens</span>`:''}<span class="lf-sep">·</span><span class="lf-status">Running</span>`;
}
function updateLiveRunStatus(opts){
  if(opts&&opts.sessionId&&_liveRunStatusSessionId&&opts.sessionId!==_liveRunStatusSessionId) return;
  if(opts&&opts.tokens!==undefined)_liveRunStatusTokens=opts.tokens;
  const el=$('liveRunStatus');
  if(el&&!el.hidden){
    _moveLiveRunStatusToTurnEnd(el);
    const timer=_liveRunStatusTimers[_liveRunStatusSessionId];
    const startedAt=timer&&timer.startedAt||null;
    _renderLiveRunStatusContent(el,startedAt);
  }
}
function _syncLiveRunStatusAfterRender(){
  const sid=S.session&&S.session.session_id;
  if(!sid||!S.activeStreamId||!S.busy) return;
  const timer=_liveRunStatusTimers[sid];
  const startedAt=(timer&&timer.startedAt)||((S.session&&S.session.pending_started_at)||Date.now()/1000);
  if(typeof isCompactWorklogMode==='function'&&isCompactWorklogMode()){
    const el=$('liveRunStatus');
    if(el){el.hidden=true;el.innerHTML='';}
    return;
  }
  const el=$('liveRunStatus');
  if(el&&el.isConnected&&!el.hidden){
    _moveLiveRunStatusToTurnEnd(el);
    _renderLiveRunStatusContent(el,startedAt);
    return;
  }
  showLiveRunStatus(sid,{startedAt,tokens:_liveRunStatusTokens});
}
function hideLiveRunStatus(sid){
  if(sid&&_liveRunStatusSessionId&&sid!==_liveRunStatusSessionId) return;
  const el=$('liveRunStatus');
  if(el){el.hidden=true;el.innerHTML='';}
  _clearLiveRunStatusTimer(sid||_liveRunStatusSessionId);
  _liveRunStatusTokens=null;
  _liveRunStatusSessionId=null;
}
function _startLiveRunStatusTimer(sid,startedAt){
  if(!sid)return;
  _clearLiveRunStatusTimer(sid);
  _liveRunStatusTimers[sid]={startedAt,interval:setInterval(()=>{
    const el=$('liveRunStatus');
    if(!el||el.hidden){_clearLiveRunStatusTimer(sid);return;}
    if(_liveRunStatusSessionId!==sid)return;
    _renderLiveRunStatusContent(el,startedAt);
  },1000)};
}
function _clearLiveRunStatusTimer(sid){
  const t=_liveRunStatusTimers[sid];
  if(t){clearInterval(t.interval);delete _liveRunStatusTimers[sid];}
}
function ensureRunActivityForCurrentTurn(){
  // Phase C: disabled — top live run Activity card removed
  return null;
}
function closeCurrentLiveActivityGroup(){
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  turn.querySelectorAll('.tool-worklog-group[data-live-tool-call-group="1"][data-live-activity-current="1"],.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]').forEach(group=>{
    group.removeAttribute('data-live-activity-current');
    _finalizeLiveActivityDisclosureGroup(group);
  });
}

export {
  _formatRunElapsed,
  _moveLiveRunStatusToTurnEnd,
  placeLiveRunStatusHost,
  showLiveRunStatus,
  _renderLiveRunStatusContent,
  updateLiveRunStatus,
  _syncLiveRunStatusAfterRender,
  hideLiveRunStatus,
  _startLiveRunStatusTimer,
  _clearLiveRunStatusTimer,
  ensureRunActivityForCurrentTurn,
  closeCurrentLiveActivityGroup,
  _liveRunStatusTimers,
  _liveRunStatusTokens,
  _liveRunStatusSessionId,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _formatRunElapsed: { enumerable: true, get: () => _formatRunElapsed, set: (value) => { _formatRunElapsed = value; } },
  _moveLiveRunStatusToTurnEnd: { enumerable: true, get: () => _moveLiveRunStatusToTurnEnd, set: (value) => { _moveLiveRunStatusToTurnEnd = value; } },
  placeLiveRunStatusHost: { enumerable: true, get: () => placeLiveRunStatusHost, set: (value) => { placeLiveRunStatusHost = value; } },
  showLiveRunStatus: { enumerable: true, get: () => showLiveRunStatus, set: (value) => { showLiveRunStatus = value; } },
  _renderLiveRunStatusContent: { enumerable: true, get: () => _renderLiveRunStatusContent, set: (value) => { _renderLiveRunStatusContent = value; } },
  updateLiveRunStatus: { enumerable: true, get: () => updateLiveRunStatus, set: (value) => { updateLiveRunStatus = value; } },
  _syncLiveRunStatusAfterRender: { enumerable: true, get: () => _syncLiveRunStatusAfterRender, set: (value) => { _syncLiveRunStatusAfterRender = value; } },
  hideLiveRunStatus: { enumerable: true, get: () => hideLiveRunStatus, set: (value) => { hideLiveRunStatus = value; } },
  _startLiveRunStatusTimer: { enumerable: true, get: () => _startLiveRunStatusTimer, set: (value) => { _startLiveRunStatusTimer = value; } },
  _clearLiveRunStatusTimer: { enumerable: true, get: () => _clearLiveRunStatusTimer, set: (value) => { _clearLiveRunStatusTimer = value; } },
  ensureRunActivityForCurrentTurn: { enumerable: true, get: () => ensureRunActivityForCurrentTurn, set: (value) => { ensureRunActivityForCurrentTurn = value; } },
  closeCurrentLiveActivityGroup: { enumerable: true, get: () => closeCurrentLiveActivityGroup, set: (value) => { closeCurrentLiveActivityGroup = value; } },
  _liveRunStatusTimers: { enumerable: true, get: () => _liveRunStatusTimers },
  _liveRunStatusTokens: { enumerable: true, get: () => _liveRunStatusTokens, set: (value) => { _liveRunStatusTokens = value; } },
  _liveRunStatusSessionId: { enumerable: true, get: () => _liveRunStatusSessionId, set: (value) => { _liveRunStatusSessionId = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
