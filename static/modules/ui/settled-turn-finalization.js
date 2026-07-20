import { _formatGatewayModelLabel, _gatewayModelWarningText, _gatewayRoutingFailoverText } from './activity-and-scroll.js';
import { _fmtTokens, _formatFirstToken, _formatTurnDuration } from './composer-controls.js';
import { isCompactWorklogMode, isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks } from './assistant-turn-presentation.js';
import { S } from './state.js';
import { _applyTransparentRowFading, _materializeDeferredWorklogRows, _renderTransparentTurnFooter, _transparentTurnCollapsedStates, _wireTransparentTurnToggle } from './transparent-worklog.js';

// Applies metadata and safety invariants after message and Activity nodes exist.
// Callers provide the render-owned maps; this module owns all settled-turn
// decoration, transparent footer wiring, and never-blank recovery behavior.
function finalizeSettledTurns(context) {
  const {
    inner,
    sid,
    assistantSegments,
    assistantThinking,
    toolCallAssistantIdxs,
  }=context;
  // Render per-turn duration and optional token usage on assistant messages.
  // Duration stays visible even when token usage is disabled, because it answers
  // the basic "how long did that turn take?" UX question. Only walk rendered
  // assistant segments so hidden messages above the DOM window cannot skew the
  // footer-to-message mapping.
  {
    const renderedAssistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    for(const mi of renderedAssistantIdxs){
      const msg=S.messages[mi]||{};
      if(msg.role!=='assistant') continue;
      const routing=msg._gatewayRouting||null;
      const gatewayText=_formatGatewayModelLabel(S.session&&S.session.model||'', '', routing);
      const failoverText=_gatewayRoutingFailoverText(routing);
      const modelWarningText=_gatewayModelWarningText(routing);
      const hasTurnUsage=!!msg._turnUsage;
      // The Worklog summary owns the "Done in …" duration whenever this
      // assistant message contributes tool or thinking detail to a folded
      // Worklog above the final answer.
      const compactWorklogForMessage=isCompactWorklogMode()&&(toolCallAssistantIdxs.has(mi)||assistantThinking.has(mi));
      const durationText=compactWorklogForMessage?'':_formatTurnDuration(msg._turnDuration);
      if(!hasTurnUsage&&!durationText&&!gatewayText&&!failoverText&&!modelWarningText) continue;
      const seg=assistantSegments.get(mi);
      const row=seg?seg.closest('.assistant-turn'):null;
      const footerRows=row?row.querySelectorAll('.msg-foot'):[];
      const targetFoot=footerRows.length?footerRows[footerRows.length-1]:null;
      if(!targetFoot||targetFoot.querySelector('.msg-usage-inline,.msg-duration-inline,.msg-gateway-inline,.gateway-failover-inline,.msg-model-warning-inline')) continue;
      const fragments=[];
      if(modelWarningText){
        const warning=document.createElement('span');
        warning.className='msg-model-warning-inline';
        warning.textContent=modelWarningText;
        fragments.push(warning);
      }
      if(failoverText){
        const failover=document.createElement('span');
        failover.className='gateway-failover-inline';
        failover.textContent=failoverText;
        fragments.push(failover);
      }
      if(gatewayText){
        const gateway=document.createElement('span');
        gateway.className='msg-gateway-inline';
        gateway.textContent=gatewayText;
        fragments.push(gateway);
      }
      if(durationText){
        const duration=document.createElement('span');
        duration.className='msg-duration-inline';
        duration.textContent=`Done in ${durationText}`;
        fragments.push(duration);
      }
      if(window._showTokenUsage&&hasTurnUsage){
        const usage=document.createElement('span');
        usage.className='msg-usage-inline';
        const inTok=msg._turnUsage.input_tokens||0;
        const outTok=msg._turnUsage.output_tokens||0;
        const cost=msg._turnUsage.estimated_cost;
        let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
        if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
        const cacheHitPct=msg._turnUsage.cache_hit_percent;
        if(cacheHitPct!=null) text+=` · ${t('usage_cached_percent',cacheHitPct)}`;
        usage.textContent=text;
        fragments.push(usage);
      }
      if(fragments.length){
        targetFoot.classList.add('msg-foot-with-usage');
        for(let i=fragments.length-1;i>=0;i--){
          // Guard: firstChild may be null (empty foot) or orphaned.
          const firstChild=targetFoot.firstChild;
          if(firstChild&&firstChild.parentNode===targetFoot) targetFoot.insertBefore(fragments[i], firstChild);
          else targetFoot.appendChild(fragments[i]);
        }
      }
    }
  }
  // Transparent mode per-turn wiring: collapsible Hermes chat name tag, old-event
  // fading, and the bottom-of-turn footer (elapsed · tokens · TTFT · status).
  // Runs after the per-turn duration block above so the footer can reuse the
  // computed durationText / tokens / TTFT for each settled assistant turn.
  if(isTransparentStream()){
    for(const turn of inner.querySelectorAll('.assistant-turn')){
      if(turn.id==='liveAssistantTurn') continue;
      const blocks=_assistantTurnBlocks(turn);
      if(!blocks) continue;
      const hasTransparentRows=blocks.querySelector(':scope > .transparent-event-row');
      _wireTransparentTurnToggle(turn);
      // Restore collapse state from the map (survives DOM rebuild).
      const seg=turn.querySelector('.assistant-segment');
      if(seg&&sid){
        const mi=seg.getAttribute('data-msg-idx');
        if(mi!=null&&_transparentTurnCollapsedStates[`${sid}:${mi}`]){
          turn.setAttribute('data-transparent-turn-collapsed','1');
          const role=turn.querySelector('.msg-role.assistant');
          if(role) role.setAttribute('aria-expanded','false');
        }
      }
      _applyTransparentRowFading(turn);
      if(hasTransparentRows){
        // Find the corresponding message to read duration/usage.
        const seg=turn.querySelector('.assistant-segment');
        let durationText='';
        let ttftText='';
        let tokensText='';
        if(seg){
          const mi=seg.getAttribute('data-msg-idx');
          if(mi!=null){
            const msg=S.messages[Number(mi)]||{};
            if(msg._turnDuration!=null) durationText=_formatTurnDuration(msg._turnDuration);
            if(msg._firstTokenMs!=null) ttftText=_formatFirstToken(msg._firstTokenMs);
            if(msg._turnUsage){
              const inTok=msg._turnUsage.input_tokens||0;
              const outTok=msg._turnUsage.output_tokens||0;
              tokensText=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
            }
          }
        }
        _renderTransparentTurnFooter(turn,{
          durationText,
          ttftText,
          tokensText,
          statusText: t('done')||'Done',
        });
      }else{
        // No transparent rows → no footer needed.
        _renderTransparentTurnFooter(turn,{});
      }
    }
  }
  // Fail-safe invariant (#3875): a settled assistant turn must never render with
  // ZERO visible content. The Worklog redesign (#3401) folds intermediate
  // assistant segments into a collapsed Worklog card and hides the source segment
  // (`assistant-segment-worklog-source` → display:none). That is correct WHEN the
  // turn also has a visible final answer. But when a turn's ONLY content is folded
  // into a collapsed Worklog (e.g. an autonomous/interrupted run whose final
  // assistant message is empty, or a reload where S.toolCalls didn't hydrate so the
  // worklog card built with no expandable tool steps), every segment is hidden and
  // the turn paints as nothing — leaving the transcript a bare stack of date
  // separators (#3875 brick). Reveal such turns so their content is never silently
  // swallowed: expand the turn's Worklog group(s) when the turn has no other
  // visible content. This NEVER touches a turn that has any visible segment, so the
  // intended collapsed-Worklog UX is preserved whenever a visible answer exists.
  // The live turn is excluded by its `liveAssistantTurn` id (it drives its own
  // state during a stream), so this sweep is safe to run even while busy — a
  // historical blank turn must not re-paint blank during a follow-up stream
  // (Opus advisor, stage-342).
  {
    const _turnHasVisibleContent=(turn)=>{
      const segs=turn.querySelectorAll('.assistant-segment');
      for(const seg of segs){
        // A segment shows real content only when it is NOT worklog-folded AND its
        // body/files/status actually painted (the anchor-only placeholder class
        // carries no visible body).
        if(seg.classList.contains('assistant-segment-worklog-source')) continue;
        if(seg.classList.contains('assistant-segment-anchor')) continue;
        if((seg.textContent||'').trim()) return true;
      }
      return false;
    };
    for(const turn of inner.querySelectorAll('.assistant-turn')){
      if(turn.id==='liveAssistantTurn') continue; // live turn drives its own state
      if(_turnHasVisibleContent(turn)) continue;
      // No visible content — surface the folded Worklog so the turn isn't blank.
      const groups=turn.querySelectorAll('.tool-worklog-group,.tool-call-group');
      let revealed=false;
      for(const group of groups){
        if(!(group.textContent||'').trim()) continue; // empty group can't help
        if(group.classList.contains('tool-call-group-collapsed')){
          group.classList.remove('tool-call-group-collapsed');
          group.classList.add('open');
          const summary=group.querySelector('.tool-call-group-summary,.activity-summary');
          if(summary) summary.setAttribute('aria-expanded','true');
          // #5839: this turn is otherwise blank, so materialize any deferred
          // settled rows now that we're force-expanding the worklog to fill it.
          if(typeof _materializeDeferredWorklogRows==='function') _materializeDeferredWorklogRows(group);
        }
        // `revealed` means "this turn has a non-empty Worklog group that the user
        // can see" — NOT "we just expanded something". An already-open non-empty
        // group is itself visible (it slips past _turnHasVisibleContent only
        // because that check inspects .assistant-segment nodes, not group bodies),
        // so the turn isn't truly blank and the last-resort un-hide below is
        // unnecessary. Keep this assignment OUTSIDE the if(collapsed) branch.
        revealed=true;
      }
      // Last resort: no usable worklog group either, but hidden worklog-source
      // segments carry the real text — un-hide them so nothing is lost.
      if(!revealed){
        for(const seg of turn.querySelectorAll('.assistant-segment-worklog-source')){
          if(!(seg.textContent||'').trim()) continue;
          seg.classList.remove('assistant-segment-worklog-source');
          seg.removeAttribute('aria-hidden');
          seg.hidden=false;
        }
      }
    }
  }
}

const compatibilityBindings=Object.freeze({});

export {
  finalizeSettledTurns,
  compatibilityBindings,
};
