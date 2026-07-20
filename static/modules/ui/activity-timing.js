import { _compressionStateForCurrentSession } from './live-activity.js';
import { S } from './state.js';

function _fmtTokens(n){if(!n||n<0)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return String(n);}
function _formatTurnDuration(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.round(n));
  if(total<60)return`${total}s`;
  const h=Math.floor(total/3600);
  const m=Math.floor((total%3600)/60);
  const s=total%60;
  if(h)return`${h}h ${m}m`;
  return`${m}m ${s}s`;
}
function _formatFirstToken(ms){
  const n=Number(ms);
  if(!Number.isFinite(n)||n<0)return'';
  if(n<1000)return`${Math.round(n)}ms`;
  return`${(n/1000).toFixed(2)}s`;
}
function _formatActiveElapsedTimer(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'';
  const total=Math.max(0,Math.floor(n));
  const m=Math.floor(total/60);
  const s=total%60;
  return`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function _processedElapsedLabel(seconds){
  const text=_formatTurnDuration(seconds);
  return text?t('processed_elapsed',text):'';
}
const _COMPRESSION_ELAPSED_MAX_SECONDS=5*60;
let _compressionElapsedTimer=null;
function _compressionElapsedStartedAt(state){const n=Number(state&&state.startedAt);return Number.isFinite(n)&&n>0?n:null;}
function _compressionElapsedLabel(state){
  const started=_compressionElapsedStartedAt(state);
  if(!started)return'';
  const elapsed=Math.max(0,(Date.now()/1000)-started);
  if(elapsed>=_COMPRESSION_ELAPSED_MAX_SECONDS)return '5+ min';
  return _formatActiveElapsedTimer(elapsed);
}
function _compressionElapsedExpired(state){const started=_compressionElapsedStartedAt(state);return !!(started&&((Date.now()/1000)-started)>=_COMPRESSION_ELAPSED_MAX_SECONDS);}
function _compressionLiveCardNode(){return document.querySelector('[data-live-compression-card="1"][data-compression-started-at]');}
function _compressionLiveCardState(){
  const node=_compressionLiveCardNode();
  const started=Number(node&&node.getAttribute('data-compression-started-at'));
  if(!node||!S.session||!Number.isFinite(started)||started<=0)return null;
  return {sessionId:S.session.session_id,phase:'running',automatic:true,message:node.getAttribute('data-compression-message')||'Auto-compressing context...',startedAt:started};
}
function _updateCompressionElapsedCards(state){
  if(!state)return false;
  return false;
}
function _updateCompressionElapsedTimer(){
  const state=_compressionStateForCurrentSession()||_compressionLiveCardState();
  if(state&&state.automatic&&state.phase==='running'){
    _updateCompressionElapsedCards(state);
    if(_compressionElapsedExpired(state)) _clearCompressionElapsedTimer();
  }else _clearCompressionElapsedTimer();
}
function _startCompressionElapsedTimer(){if(!_compressionElapsedTimer)_compressionElapsedTimer=setInterval(_updateCompressionElapsedTimer,1000);}
function _clearCompressionElapsedTimer(){if(_compressionElapsedTimer){clearInterval(_compressionElapsedTimer);_compressionElapsedTimer=null;}}
let _activityElapsedTimer=null;
let _activityElapsedTimerGroup=null;
function _activityNowSeconds(){return Date.now()/1000;}
function _isActivityTimerGroup(group){
  return !!(group&&group.getAttribute('data-run-activity-group')==='1');
}
function _activityElapsedStartedAt(group){
  if(!group)return null;
  const raw=(group.dataset&&group.dataset.turnStartedAt!==undefined&&group.dataset.turnStartedAt!=='')
    ?group.dataset.turnStartedAt
    :(S.session&&S.session.pending_started_at);
  const started=Number(raw);
  return Number.isFinite(started)&&started>0?started:null;
}
function _activityElapsedLabel(group){
  const started=_activityElapsedStartedAt(group);
  if(!started)return'';
  return _formatActiveElapsedTimer(_activityNowSeconds()-started);
}
function _activityProcessedElapsedLabel(group){
  const started=_activityElapsedStartedAt(group);
  if(!started)return'';
  return _processedElapsedLabel(_activityNowSeconds()-started);
}
function _activitySettledProcessedLabel(group){
  let durationText=_formatTurnDuration(group&&group.dataset&&group.dataset.turnDuration);
  if(!durationText&&group){
    const durationEl=group.querySelector&&group.querySelector('.tool-call-group-duration');
    const legacy=String(durationEl&&durationEl.textContent||'').replace(/^\s*Done in\s+/i,'').trim();
    if(legacy) durationText=legacy;
  }
  return durationText?t('processed_elapsed',durationText):'';
}
function _activityMarkObserved(group, ts){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const stamp=Number(ts||_activityNowSeconds());
  if(Number.isFinite(stamp)&&stamp>0) group.setAttribute('data-last-activity-at',String(stamp));
}
function _activityLastObservedAge(group){
  const stamp=Number(group&&group.getAttribute('data-last-activity-at'));
  if(!Number.isFinite(stamp)||stamp<=0)return null;
  return Math.max(0,_activityNowSeconds()-stamp);
}
function _activityClockLabel(ts){
  const stamp=Number(ts||_activityNowSeconds());
  if(!Number.isFinite(stamp)||stamp<=0)return'';
  try{return new Date(stamp*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});}catch(_){return'';}
}
// Full date+time label for the worklog event-time tooltip (title attr). Guards the
// same valid-Date range as _timestampSeconds so a bad epoch never yields "Invalid
// Date" in the tooltip. (#5739)
function _activityFullClockLabel(ts){
  const stamp=Number(ts);
  if(!Number.isFinite(stamp)||stamp<=0||stamp>8.64e12)return'';
  try{
    const d=new Date(stamp*1000);
    if(isNaN(d.getTime()))return'';
    return d.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }catch(_){return'';}
}
function _timestampSeconds(value){
  if(value===undefined||value===null||value==='') return null;
  if(value instanceof Date){
    const stamp=value.getTime()/1000;
    return (Number.isFinite(stamp)&&stamp>0&&Math.abs(stamp)<=8.64e12)?stamp:null;
  }
  const numeric=Number(value);
  if(Number.isFinite(numeric)&&numeric>0){
    const stamp=numeric>1e12?numeric/1000:numeric;
    // Reject epochs outside JavaScript's valid Date range (±8.64e15 ms = ±8.64e12 s);
    // otherwise new Date(stamp*1000) yields "Invalid Date" and renders literally
    // (e.g. a garbage numeric timestamp like 1e20 passes finite/>0). (#5739 gate.)
    return (Number.isFinite(stamp)&&stamp>0&&stamp<=8.64e12)?stamp:null;
  }
  if(typeof value==='string'){
    const text=value.trim();
    if(!text||/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(text)) return null;
    const parsed=Date.parse(text);
    if(Number.isFinite(parsed)&&parsed>0){
      const stamp=parsed/1000;
      return stamp<=8.64e12?stamp:null;
    }
  }
  return null;
}
function _firstValidTimestampSeconds(...values){
  for(const value of values){
    const stamp=_timestampSeconds(value);
    if(stamp) return stamp;
  }
  return null;
}
function _transparentEventTimestampSeconds(row, opts){
  opts=opts||{};
  for(const key of ['ts','timestamp','created_at']){
    const stamp=_timestampSeconds(opts[key]);
    if(stamp) return stamp;
  }
  const toolCall=opts.toolCall||row&&row._tcData||null;
  if(toolCall&&typeof toolCall==='object'){
    for(const key of ['ts','timestamp','created_at','started_at','completed_at']){
      const stamp=_timestampSeconds(toolCall[key]);
      if(stamp) return stamp;
    }
  }
  if(row&&typeof row.getAttribute==='function'){
    for(const key of ['data-event-at','data-activity-at']){
      const stamp=_timestampSeconds(row.getAttribute(key));
      if(stamp) return stamp;
    }
  }
  if(opts.live===true) return _activityNowSeconds();
  return null;
}
function _syncTransparentEventTimestamp(row, header, opts){
  if(!row||!header) return null;
  opts=opts||{};
  const showEventTimestamp=!(typeof window!=='undefined'&&window._transparentEventTimestamps===false);
  const live=opts.live===true||row.getAttribute&&(
    row.getAttribute('data-live-tid')==='1'||
    row.getAttribute('data-live-thinking')==='1'||
    row.getAttribute('data-live-assistant')==='1'||
    row.getAttribute('data-live-stream-owned')==='1'
  );
  const explicitTs=_firstValidTimestampSeconds(opts.ts, opts.timestamp, opts.created_at);
  const toolCall=opts.toolCall||row&&row._tcData||null;
  const toolTs=toolCall&&typeof toolCall==='object'
    ? _firstValidTimestampSeconds(
      toolCall.ts,
      toolCall.timestamp,
      toolCall.created_at,
      toolCall.started_at,
      toolCall.completed_at
    )
    : null;
  const attrTs=row&&typeof row.getAttribute==='function'
    ? _firstValidTimestampSeconds(
      row.getAttribute('data-event-at'),
      row.getAttribute('data-activity-at')
    )
    : null;
  const ts=explicitTs||toolTs||attrTs||(live?_activityNowSeconds():null);
  const label=ts?_activityClockLabel(ts):'';
  let timeEl=header.querySelector('.transparent-event-time');
  if(!label){
    if(timeEl) timeEl.remove();
    row.removeAttribute('data-event-at');
    row.removeAttribute('data-event-at-source');
    return null;
  }
  const source=explicitTs||toolTs||attrTs?'event':'live';
  row.setAttribute('data-event-at',String(ts));
  row.setAttribute('data-event-at-source',source);
  if(!showEventTimestamp){
    if(timeEl) timeEl.remove();
    return null;
  }
  if(!timeEl){
    timeEl=document.createElement('span');
    timeEl.className='transparent-event-time';
  }
  timeEl.textContent=label;
  // Full date+time tooltip: the bare clock label is date-ambiguous when a settled
  // session is reviewed days later (or a run crosses midnight), and timing is the
  // whole point of this label. (#5739 Fable UX fix.)
  const fullLabel=_activityFullClockLabel(ts);
  if(fullLabel) timeEl.setAttribute('title',fullLabel); else timeEl.removeAttribute('title');
  timeEl.setAttribute('data-event-at',String(ts));
  timeEl.setAttribute('data-event-at-source',source);
  const anchor=header.querySelector('.transparent-event-status,.thinking-card-btn-row,.tool-card-toggle,.thinking-card-toggle');
  if(timeEl.parentNode!==header){
    if(anchor&&anchor.parentNode===header) header.insertBefore(timeEl,anchor);
    else header.appendChild(timeEl);
  }else if(anchor&&timeEl.nextSibling!==anchor){
    header.insertBefore(timeEl,anchor);
  }
  return timeEl;
}



export {
  _fmtTokens,
  _formatTurnDuration,
  _formatFirstToken,
  _formatActiveElapsedTimer,
  _processedElapsedLabel,
  _compressionElapsedStartedAt,
  _compressionElapsedLabel,
  _compressionElapsedExpired,
  _compressionLiveCardNode,
  _compressionLiveCardState,
  _updateCompressionElapsedCards,
  _updateCompressionElapsedTimer,
  _startCompressionElapsedTimer,
  _clearCompressionElapsedTimer,
  _activityNowSeconds,
  _isActivityTimerGroup,
  _activityElapsedStartedAt,
  _activityElapsedLabel,
  _activityProcessedElapsedLabel,
  _activitySettledProcessedLabel,
  _activityMarkObserved,
  _activityLastObservedAge,
  _activityClockLabel,
  _activityFullClockLabel,
  _timestampSeconds,
  _firstValidTimestampSeconds,
  _transparentEventTimestampSeconds,
  _syncTransparentEventTimestamp,
  _COMPRESSION_ELAPSED_MAX_SECONDS,
  _compressionElapsedTimer,
  _activityElapsedTimer,
  _activityElapsedTimerGroup,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _fmtTokens: { enumerable: true, get: () => _fmtTokens, set: (value) => { _fmtTokens = value; } },
  _formatTurnDuration: { enumerable: true, get: () => _formatTurnDuration, set: (value) => { _formatTurnDuration = value; } },
  _formatFirstToken: { enumerable: true, get: () => _formatFirstToken, set: (value) => { _formatFirstToken = value; } },
  _formatActiveElapsedTimer: { enumerable: true, get: () => _formatActiveElapsedTimer, set: (value) => { _formatActiveElapsedTimer = value; } },
  _processedElapsedLabel: { enumerable: true, get: () => _processedElapsedLabel, set: (value) => { _processedElapsedLabel = value; } },
  _compressionElapsedStartedAt: { enumerable: true, get: () => _compressionElapsedStartedAt, set: (value) => { _compressionElapsedStartedAt = value; } },
  _compressionElapsedLabel: { enumerable: true, get: () => _compressionElapsedLabel, set: (value) => { _compressionElapsedLabel = value; } },
  _compressionElapsedExpired: { enumerable: true, get: () => _compressionElapsedExpired, set: (value) => { _compressionElapsedExpired = value; } },
  _compressionLiveCardNode: { enumerable: true, get: () => _compressionLiveCardNode, set: (value) => { _compressionLiveCardNode = value; } },
  _compressionLiveCardState: { enumerable: true, get: () => _compressionLiveCardState, set: (value) => { _compressionLiveCardState = value; } },
  _updateCompressionElapsedCards: { enumerable: true, get: () => _updateCompressionElapsedCards, set: (value) => { _updateCompressionElapsedCards = value; } },
  _updateCompressionElapsedTimer: { enumerable: true, get: () => _updateCompressionElapsedTimer, set: (value) => { _updateCompressionElapsedTimer = value; } },
  _startCompressionElapsedTimer: { enumerable: true, get: () => _startCompressionElapsedTimer, set: (value) => { _startCompressionElapsedTimer = value; } },
  _clearCompressionElapsedTimer: { enumerable: true, get: () => _clearCompressionElapsedTimer, set: (value) => { _clearCompressionElapsedTimer = value; } },
  _activityNowSeconds: { enumerable: true, get: () => _activityNowSeconds, set: (value) => { _activityNowSeconds = value; } },
  _isActivityTimerGroup: { enumerable: true, get: () => _isActivityTimerGroup, set: (value) => { _isActivityTimerGroup = value; } },
  _activityElapsedStartedAt: { enumerable: true, get: () => _activityElapsedStartedAt, set: (value) => { _activityElapsedStartedAt = value; } },
  _activityElapsedLabel: { enumerable: true, get: () => _activityElapsedLabel, set: (value) => { _activityElapsedLabel = value; } },
  _activityProcessedElapsedLabel: { enumerable: true, get: () => _activityProcessedElapsedLabel, set: (value) => { _activityProcessedElapsedLabel = value; } },
  _activitySettledProcessedLabel: { enumerable: true, get: () => _activitySettledProcessedLabel, set: (value) => { _activitySettledProcessedLabel = value; } },
  _activityMarkObserved: { enumerable: true, get: () => _activityMarkObserved, set: (value) => { _activityMarkObserved = value; } },
  _activityLastObservedAge: { enumerable: true, get: () => _activityLastObservedAge, set: (value) => { _activityLastObservedAge = value; } },
  _activityClockLabel: { enumerable: true, get: () => _activityClockLabel, set: (value) => { _activityClockLabel = value; } },
  _activityFullClockLabel: { enumerable: true, get: () => _activityFullClockLabel, set: (value) => { _activityFullClockLabel = value; } },
  _timestampSeconds: { enumerable: true, get: () => _timestampSeconds, set: (value) => { _timestampSeconds = value; } },
  _firstValidTimestampSeconds: { enumerable: true, get: () => _firstValidTimestampSeconds, set: (value) => { _firstValidTimestampSeconds = value; } },
  _transparentEventTimestampSeconds: { enumerable: true, get: () => _transparentEventTimestampSeconds, set: (value) => { _transparentEventTimestampSeconds = value; } },
  _syncTransparentEventTimestamp: { enumerable: true, get: () => _syncTransparentEventTimestamp, set: (value) => { _syncTransparentEventTimestamp = value; } },
  _COMPRESSION_ELAPSED_MAX_SECONDS: { enumerable: true, get: () => _COMPRESSION_ELAPSED_MAX_SECONDS },
  _compressionElapsedTimer: { enumerable: true, get: () => _compressionElapsedTimer, set: (value) => { _compressionElapsedTimer = value; } },
  _activityElapsedTimer: { enumerable: true, get: () => _activityElapsedTimer, set: (value) => { _activityElapsedTimer = value; } },
  _activityElapsedTimerGroup: { enumerable: true, get: () => _activityElapsedTimerGroup, set: (value) => { _activityElapsedTimerGroup = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
