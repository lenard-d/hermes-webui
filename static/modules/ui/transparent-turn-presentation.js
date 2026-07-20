import { isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks } from './assistant-turn-presentation.js';
import { S, esc } from './state.js';

const _transparentTurnCollapsedStates={}; // key: `${sid}:${turnMsgIdx}` → boolean
function _wireTransparentTurnToggle(turn){
  if(!turn) return;
  if(!isTransparentStream()) return;
  const role=turn.querySelector('.msg-role.assistant');
  if(!role) return;
  turn.setAttribute('data-transparent-turn-toggle-bound','1');
  // Add chevron if missing.
  if(!role.querySelector('.transparent-turn-chevron')){
    const chev=document.createElement('span');
    chev.className='transparent-turn-chevron';
    chev.innerHTML=li('chevron-down',10);
    role.appendChild(chev);
  }
  role.setAttribute('role','button');
  role.setAttribute('tabindex','0');
  role.setAttribute('aria-expanded',turn.getAttribute('data-transparent-turn-collapsed')==='1'?'false':'true');
  const toggle=function(ev){
    if(ev&&ev.target&&ev.target.closest&&ev.target.closest('.msg-tps-inline')) return;
    const collapsed=turn.getAttribute('data-transparent-turn-collapsed')==='1';
    turn.setAttribute('data-transparent-turn-collapsed',collapsed?'0':'1');
    role.setAttribute('aria-expanded',collapsed?'true':'false');
    // Persist state across DOM rebuilds.
    if(S.session){
      const seg=turn.querySelector('.assistant-segment');
      if(seg){
        const mi=seg.getAttribute('data-msg-idx');
        if(mi!=null) _transparentTurnCollapsedStates[`${S.session.session_id}:${mi}`]=!collapsed;
      }
    }
  };
  role.onclick=toggle;
  role.onkeydown=function(ev){
    if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();toggle(ev);}
  };
}
// ── Transparent old-event fading (medium → low) ───────────────────────────
// In long streams the earliest rows fade to a lower opacity so the user's
// eye lands on the most recent activity. The fade is per-turn: the newest
// event stays at full opacity, each earlier event drops one step. Floors
// at 0.32 so labels stay readable.
function _applyTransparentRowFading(turn){
  if(!turn||!isTransparentStream()) return;
  // Recency-fading only makes sense on the LIVE turn (draw the eye to the most
  // recent activity). On settled/historical turns it permanently dims the trace
  // below readable contrast (floor .32) — the opposite of a transparent record.
  // So clear any fade on non-live turns and only fade the live turn.
  // (Trifecta finding V8.)
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const rows=Array.from(blocks.querySelectorAll(':scope > .transparent-event-row'));
  const isLive=turn.id==='liveAssistantTurn'||turn.getAttribute('data-live-assistant-turn')==='1';
  if(!isLive){
    rows.forEach(row=>row.removeAttribute('data-transparent-fade'));
    return;
  }
  const total=rows.length;
  for(let i=0;i<total;i++){
    const row=rows[i];
    // Newest = full opacity; each step back drops by 1 (floors at 5).
    const stepsFromEnd=total-1-i;
    if(stepsFromEnd<=0){row.removeAttribute('data-transparent-fade');continue;}
    const step=Math.min(5,stepsFromEnd);
    row.setAttribute('data-transparent-fade',String(step));
  }
}
// ── Transparent turn footer (elapsed · tokens · TTFT · status) ───────────
// Mirrors the live run-status line for settled turns in transparent
// mode. Shows duration, first-token time, token usage, and final status.
// Only rendered for turns that have transparent event rows.
function _transparentTurnFooterHtml(durationText, ttftText, tokensText, statusText){
  const parts=[];
  if(durationText) parts.push(`<span class="lf-time">${esc(durationText)}</span>`);
  if(ttftText) parts.push(`<span class="lf-ttft" title="${esc(t('first_token_time')||'Time to first token')}">TTFT ${esc(ttftText)}</span>`);
  if(tokensText) parts.push(`<span class="lf-tokens">${esc(tokensText)}</span>`);
  if(statusText) parts.push(`<span class="lf-status">${esc(statusText)}</span>`);
  if(!parts.length) return '';
  return `<div class="transparent-turn-footer">${parts.join('<span class="lf-sep">·</span>')}</div>`;
}
function _renderTransparentTurnFooter(turn, opts){
  if(!turn||!isTransparentStream()) return;
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const hasRows=blocks.querySelector(':scope > .transparent-event-row');
  if(!hasRows){
    // No events → no footer (the answer itself carries the duration).
    const existing=turn.querySelector('.transparent-turn-footer');
    if(existing) existing.remove();
    return;
  }
  const durationText=opts&&opts.durationText||'';
  const ttftText=opts&&opts.ttftText||'';
  const tokensText=opts&&opts.tokensText||'';
  const statusText=opts&&opts.statusText||(t('done')||'Done');
  const html=_transparentTurnFooterHtml(durationText, ttftText, tokensText, statusText);
  let footer=turn.querySelector('.transparent-turn-footer');
  if(!html){
    if(footer) footer.remove();
    return;
  }
  if(!footer){
    footer=document.createElement('div');
    footer.className='transparent-turn-footer';
    const blocks=turn.querySelector('.assistant-turn-blocks');
    // Guard: nextSibling may be null (blocks is last child) or orphaned from
    // a prior DOM rebuild. Only insertBefore when it is still a child of turn.
    if(blocks&&blocks.nextSibling&&blocks.nextSibling.parentNode===turn){
      turn.insertBefore(footer, blocks.nextSibling);
    }else{
      turn.appendChild(footer);
    }
  }
  footer.innerHTML=html.replace(/^<div class="transparent-turn-footer">|<\/div>$/g,'');
}
// ── Activity-group user expand intent (#1298) ──────────────────────────────
// When the user manually expands the live "Activity" dropdown during streaming,
// preserve that intent across the destroy/recreate cycle that fires on every
// thinking/tool event. Without this, ensureActivityGroup() re-creates the group
// with the default collapsed state and finalizeThinkingCard() force-collapses
// it whenever the assistant transitions from thinking → tool → thinking, so
// the panel snaps shut every few seconds while the user is trying to read it.
//
// The tracker is a singleton boolean: there is at most one live activity group
// at a time (selector .tool-call-group[data-live-tool-call-group="1"]). It is
// set to true when the user clicks the summary to expand, false when they
// click to collapse, and cleared back to undefined when the live group is
// finalized into a settled assistant turn (the live attribute is removed in
// _convertLiveActivityGroupToSettled / when liveAssistantTurn loses its id).

export {
  _wireTransparentTurnToggle,
  _applyTransparentRowFading,
  _transparentTurnFooterHtml,
  _renderTransparentTurnFooter,
  _transparentTurnCollapsedStates,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _wireTransparentTurnToggle: { enumerable: true, get: () => _wireTransparentTurnToggle, set: (value) => { _wireTransparentTurnToggle = value; } },
  _applyTransparentRowFading: { enumerable: true, get: () => _applyTransparentRowFading, set: (value) => { _applyTransparentRowFading = value; } },
  _transparentTurnFooterHtml: { enumerable: true, get: () => _transparentTurnFooterHtml, set: (value) => { _transparentTurnFooterHtml = value; } },
  _renderTransparentTurnFooter: { enumerable: true, get: () => _renderTransparentTurnFooter, set: (value) => { _renderTransparentTurnFooter = value; } },
  _transparentTurnCollapsedStates: { enumerable: true, get: () => _transparentTurnCollapsedStates },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
