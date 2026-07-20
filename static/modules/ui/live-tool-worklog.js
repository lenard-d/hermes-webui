import { _clearActivityElapsedTimer, _startActivityElapsedTimer, scrollIfPinned } from './activity-and-scroll.js';
import { _clearLiveActivityUserIntent, _renderLiveAnchorActivitySceneForStream, ensureActivityGroup } from './anchor-scenes.js';
import { _transparentEventTimestampSeconds } from './composer-controls.js';
import { appendThinking, removeThinking } from './thinking-lifecycle.js';
import { _moveLiveRunStatusToTurnEnd } from './live-run-status.js';
import { _decorateTransparentEventRow, _setTransparentCardOpen, _setTransparentDetailMode, _syncTransparentEventControls, _transparentToolStatus, isCompactWorklogMode, isFinalAnswerOnlyMode, isSimplifiedToolCalling, isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks, _createAssistantTurn } from './assistant-turn-presentation.js';
import { $, S } from './state.js';
import { buildToolCard } from './tool-card-presentation.js';
import { _liveToolStepEl, _syncToolCallGroupSummary, _toolWorklogListEl } from './worklog-tool-groups.js';
import { _activityKeyForLiveTurn } from './worklog-disclosure.js';
import { _dedupeLiveProcessedWorklogAnchors, isLiveAnchorActivitySceneOwner } from './live-anchor-reconciliation.js';
import { ensureLiveWorklogContainer } from './worklog-reasoning.js';

// ── Live tool card helpers (called during SSE streaming) ──
// Live cards are inserted INLINE inside #msgInner (tagged with data-live-tid)
// so the streaming layout matches the settled layout produced by renderMessages
// (user → thinking → tool cards → response). The legacy #liveToolCards
// sibling container is no longer used for placement — keeping the cards in the
// message column eliminates the visible "jump" users saw when renderMessages
// fired on the done event.
function appendLiveToolCard(tc){
  // Guard: ignore if session was switched. Prevents stale tool events from
  // a previous session's SSE stream from manipulating the new session's DOM.
  if(!S.session||!S.activeStreamId) return;
  const opts=arguments[1]||{};
  if(opts.sessionId&&S.session.session_id!==opts.sessionId) return;
  if(opts.streamId&&S.activeStreamId!==opts.streamId) return;
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return;
  if(isLiveAnchorActivitySceneOwner(opts.streamId||S.activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(opts.streamId||S.activeStreamId, opts.sessionId||S.session.session_id);
    return;
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;  // see #1366
    $('msgInner').appendChild(turn);
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return;
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  const children=Array.from(inner.children);
  const burstId=tc.activityBurstId!==undefined&&tc.activityBurstId!==null&&String(tc.activityBurstId)!=='0'?String(tc.activityBurstId):'';
  const segmentSeq=tc.activitySegmentSeq!==undefined&&tc.activitySegmentSeq!==null&&String(tc.activitySegmentSeq)!=='0'?String(tc.activitySegmentSeq):'';
  const segmentAnchor=segmentSeq?_findLiveAssistantAnchorForSegment(inner, segmentSeq):null;
  const burstAnchor=burstId?_findLatestVisibleLiveAssistantByBurst(inner, burstId):null;
  const anchor=segmentAnchor||burstAnchor||_findLatestVisibleLiveAssistant(inner)||children.filter(el=>el.matches('[data-live-assistant="1"]')).pop();
  const effectiveSegmentSeq=anchor&&anchor.getAttribute?anchor.getAttribute('data-live-segment-seq')||segmentSeq:segmentSeq;
  if(isTransparentStream()){
    const insertTransparentRow=(row)=>{
      const liveFooter=inner.querySelector('#liveRunStatus');
      if(liveFooter&&liveFooter.parentElement===inner){
        inner.insertBefore(row,liveFooter);
      }else{
        inner.appendChild(row);
      }
    };
    if(tid){
      const existing=inner.querySelector(`.transparent-event-row[data-live-tid="${CSS.escape(tid)}"],.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
      if(existing){
        const replacementTs=_transparentEventTimestampSeconds(existing,{toolCall:tc});
        const replacement=_decorateTransparentEventRow(buildToolCard(tc),{
          type:'tool',
          name:tc&&tc.name,
          status:_transparentToolStatus(tc),
          toolCall:tc,
          ts:replacementTs,
          live:true,
          segmentSeq:effectiveSegmentSeq,
          burstId,
        });
        replacement.dataset.liveTid=tid;
        // Preserve the user's expand state + detail tab across tool completion:
        // the running row is rebuilt fresh on toolComplete, which would otherwise
        // snap an expanded row shut and reset its Full/Output tab. The detail-mode
        // is preserved regardless of open state (a user who picked Output then
        // collapsed should still get Output on re-open). (Trifecta O-Bug2 + r2.)
        try{
          const _oldCard=existing.querySelector('.tool-card,.thinking-card');
          const _newCard=replacement.querySelector('.tool-card,.thinking-card');
          const _oldDetail=existing.querySelector('.tool-card-detail');
          const _newDetail=replacement.querySelector('.tool-card-detail');
          const _mode=_oldDetail&&_oldDetail.getAttribute('data-transparent-detail-mode');
          if(_newDetail&&_mode){
            const _tab=_newDetail.querySelector(`.transparent-detail-mode[data-mode="${_mode}"]`);
            if(_tab) _setTransparentDetailMode(_tab,_mode);
          }
          if(_oldCard&&_newCard&&_oldCard.classList.contains('open')){
            _setTransparentCardOpen(_newCard,true);
          }
        }catch(_){ /* non-fatal: completion still renders, just collapsed */ }
        existing.replaceWith(replacement);
        _syncTransparentEventControls(turn);
        _moveLiveRunStatusToTurnEnd();
        if(typeof scrollIfPinned==='function') scrollIfPinned();
        return;
      }
    }
    const row=_decorateTransparentEventRow(buildToolCard(tc),{
      type:'tool',
      name:tc&&tc.name,
      status:_transparentToolStatus(tc),
      toolCall:tc,
      live:true,
      segmentSeq:effectiveSegmentSeq,
      burstId,
    });
    if(tid) row.dataset.liveTid=tid;
    insertTransparentRow(row);
    _syncTransparentEventControls(turn);
    _moveLiveRunStatusToTurnEnd();
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return;
  }
  if(anchor) _removeEmptyLiveWorklogShells(inner);
  const group=ensureLiveWorklogContainer(inner,{
    anchor,
    activityKey:_activityKeyForLiveTurn(),
    segmentSeq:effectiveSegmentSeq,
    burstId,
  });
  const list=_liveToolStepEl(group);
  if(!list) return;
  // toolComplete can replace the existing live card with the same tid.
  if(tid){
    const existing=group.querySelector(`.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
    if(existing){
      const replacement=buildToolCard(tc);
      replacement.dataset.liveTid=tid;
      existing.replaceWith(replacement);
      _syncToolCallGroupSummary(group);
      _moveLiveRunStatusToTurnEnd();
      if(typeof scrollIfPinned==='function') scrollIfPinned();
      return;
    }
  }
  const worklog=_toolWorklogListEl(group) || list;
  const waiting=worklog.querySelector('.agent-activity-status[data-activity-event-id="thinking-placeholder"] .agent-activity-status-label');
  if(waiting&&tc.done===false) waiting.textContent='Waiting on tool result';
  const row=buildToolCard(tc);
  if(tid) row.dataset.liveTid=tid;
  list.appendChild(row);
  _syncToolCallGroupSummary(group);
  _moveLiveRunStatusToTurnEnd();
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}

function _findLatestLiveAssistantByBurst(inner, burstId){
  if(!inner || !burstId) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-activity-burst-id="${CSS.escape(String(burstId))}"]`))
    .filter(el=>el.isConnected!==false);
  return candidates[candidates.length-1] || null;
}
function _findLatestLiveAssistantBySegment(inner, segmentSeq){
  if(!inner || !segmentSeq) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-live-segment-seq="${CSS.escape(String(segmentSeq))}"]`)).filter(el=>el.isConnected!==false);
  return candidates[candidates.length-1] || null;
}
function _liveAssistantHasVisibleText(el){
  if(!el||!el.matches||!el.matches('[data-live-assistant="1"]')) return false;
  const body=el.querySelector&&el.querySelector('.msg-body');
  const text=(body?body.textContent:el.textContent)||el.dataset&&el.dataset.rawText||'';
  return !!String(text||'').trim();
}
function _findPreviousVisibleLiveAssistant(inner, beforeNode){
  if(!inner) return null;
  let node=beforeNode&&beforeNode.previousElementSibling;
  while(node){
    if(_liveAssistantHasVisibleText(node)) return node;
    node=node.previousElementSibling;
  }
  return null;
}
function _findLatestVisibleLiveAssistant(inner){
  if(!inner) return null;
  const candidates=Array.from(inner.querySelectorAll('[data-live-assistant="1"]')).filter(el=>el.isConnected!==false&&_liveAssistantHasVisibleText(el));
  return candidates[candidates.length-1] || null;
}
function _findLatestVisibleLiveAssistantByBurst(inner, burstId){
  if(!inner || !burstId) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-activity-burst-id="${CSS.escape(String(burstId))}"]`))
    .filter(el=>el.isConnected!==false&&_liveAssistantHasVisibleText(el));
  return candidates[candidates.length-1] || null;
}
function _findLiveAssistantAnchorForSegment(inner, segmentSeq){
  const exact=_findLatestLiveAssistantBySegment(inner, segmentSeq);
  if(exact&&_liveAssistantHasVisibleText(exact)) return exact;
  return _findPreviousVisibleLiveAssistant(inner, exact) || _findLatestVisibleLiveAssistant(inner) || exact;
}

function clearLiveToolCards(){
  if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
  const inner=_assistantTurnBlocks($('liveAssistantTurn'));
  if(inner) inner.querySelectorAll('.live-worklog[data-live-worklog-shell],.tool-worklog-group[data-live-tool-call-group],.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid]:not(.transparent-event-row),[data-anchor-scene-owner="1"],[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  // Reset the per-turn user expand intent so the next turn starts at the
  // default collapsed state (#1298).
  if(typeof _clearLiveActivityUserIntent==='function') _clearLiveActivityUserIntent();
  // Legacy #liveToolCards container cleanup — kept for safety in case any
  // leftover cards were inserted there before this refactor took effect.
  const container=$('liveToolCards');
  if(container){container.innerHTML='';container.style.display='none';}
}
function _hideLiveActivityForFinalAnswerOnly(){
  clearLiveToolCards();
  if(typeof removeThinking==='function') removeThinking();
  const turn=$('liveAssistantTurn');
  const inner=_assistantTurnBlocks(turn);
  if(inner){
    inner.querySelectorAll('.transparent-event-row,.agent-activity-thinking,.wl-reason,#liveRunStatus,.live-worklog[data-live-worklog-shell],.tool-worklog-group[data-live-tool-call-group],.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid],[data-anchor-scene-owner="1"],[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  }
  const legacyThinking=$('thinkingRow');
  if(legacyThinking) legacyThinking.remove();
  if(turn&&inner&&!inner.children.length) turn.remove();
}
if(typeof window!=='undefined') window._hideLiveActivityForFinalAnswerOnly=_hideLiveActivityForFinalAnswerOnly;
function _removeEmptyLiveWorklogShells(inner){
  if(!inner) return;
  inner.querySelectorAll('.live-worklog[data-live-worklog-shell="1"],.tool-worklog-group[data-live-worklog-shell="1"],.tool-call-group[data-live-worklog-shell="1"]').forEach(group=>{
    if(!group.querySelector('.tool-card-row,.wl-reason,.agent-activity-thinking')) group.remove();
  });
}
function _setLiveWorklogThinkingPlaceholder(group){
  if(!group) return;
  group.setAttribute('data-prestart-thinking','1');
  const label=group.querySelector&&(
    group.querySelector('.tool-worklog-label') || group.querySelector('.tool-call-group-label')
  );
  if(label){
    const text=typeof t==='function'?t('worklog_thinking'):'Thinking';
    label.textContent=text;
    label.setAttribute('data-sweep-label', text);
  }
  const durationEl=group.querySelector&&group.querySelector('.tool-call-group-duration');
  if(durationEl){
    durationEl.textContent='';
    durationEl.style.display='none';
  }
}
function ensureLiveWorklogShell(){
  if(!S.session) return null;
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return null;
  const activeStreamId=S.activeStreamId||'';
  if(activeStreamId&&typeof _renderLiveAnchorActivitySceneForStream==='function'&&_renderLiveAnchorActivitySceneForStream(activeStreamId, S.session.session_id)){
    _dedupeLiveProcessedWorklogAnchors($('liveAssistantTurn'));
    return $('liveAssistantTurn');
  }
  if(activeStreamId&&isLiveAnchorActivitySceneOwner(activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(activeStreamId, S.session.session_id);
    _dedupeLiveProcessedWorklogAnchors($('liveAssistantTurn'));
    return $('liveAssistantTurn');
  }
  $('emptyState').style.display='none';
  const compactWorklog=typeof isCompactWorklogMode==='function'&&isCompactWorklogMode();
  if(!compactWorklog&&!isSimplifiedToolCalling()){
    appendThinking();
    return $('thinkingRow');
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    $('msgInner').appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return null;
  if(isTransparentStream()){
    _moveLiveRunStatusToTurnEnd();
    scrollIfPinned();
    return blocks;
  }
  const group=ensureActivityGroup(blocks,{
    live:true,
    collapsed:false,
    activityKey:_activityKeyForLiveTurn(),
    turnStartedAt:S.session&&S.session.pending_started_at,
  });
  if(!group) return null;
  if(activeStreamId){
    group.removeAttribute('data-prestart-thinking');
    if(typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(group);
  }else{
    _setLiveWorklogThinkingPlaceholder(group);
  }
  _moveLiveRunStatusToTurnEnd();
  _dedupeLiveProcessedWorklogAnchors(turn);
  scrollIfPinned();
  return group;
}

export {
  appendLiveToolCard,
  _findLatestLiveAssistantByBurst,
  _findLatestLiveAssistantBySegment,
  _liveAssistantHasVisibleText,
  _findPreviousVisibleLiveAssistant,
  _findLatestVisibleLiveAssistant,
  _findLatestVisibleLiveAssistantByBurst,
  _findLiveAssistantAnchorForSegment,
  clearLiveToolCards,
  _hideLiveActivityForFinalAnswerOnly,
  _removeEmptyLiveWorklogShells,
  _setLiveWorklogThinkingPlaceholder,
  ensureLiveWorklogShell,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  appendLiveToolCard: { enumerable: true, get: () => appendLiveToolCard, set: (value) => { appendLiveToolCard = value; } },
  _findLatestLiveAssistantByBurst: { enumerable: true, get: () => _findLatestLiveAssistantByBurst, set: (value) => { _findLatestLiveAssistantByBurst = value; } },
  _findLatestLiveAssistantBySegment: { enumerable: true, get: () => _findLatestLiveAssistantBySegment, set: (value) => { _findLatestLiveAssistantBySegment = value; } },
  _liveAssistantHasVisibleText: { enumerable: true, get: () => _liveAssistantHasVisibleText, set: (value) => { _liveAssistantHasVisibleText = value; } },
  _findPreviousVisibleLiveAssistant: { enumerable: true, get: () => _findPreviousVisibleLiveAssistant, set: (value) => { _findPreviousVisibleLiveAssistant = value; } },
  _findLatestVisibleLiveAssistant: { enumerable: true, get: () => _findLatestVisibleLiveAssistant, set: (value) => { _findLatestVisibleLiveAssistant = value; } },
  _findLatestVisibleLiveAssistantByBurst: { enumerable: true, get: () => _findLatestVisibleLiveAssistantByBurst, set: (value) => { _findLatestVisibleLiveAssistantByBurst = value; } },
  _findLiveAssistantAnchorForSegment: { enumerable: true, get: () => _findLiveAssistantAnchorForSegment, set: (value) => { _findLiveAssistantAnchorForSegment = value; } },
  clearLiveToolCards: { enumerable: true, get: () => clearLiveToolCards, set: (value) => { clearLiveToolCards = value; } },
  _hideLiveActivityForFinalAnswerOnly: { enumerable: true, get: () => _hideLiveActivityForFinalAnswerOnly, set: (value) => { _hideLiveActivityForFinalAnswerOnly = value; } },
  _removeEmptyLiveWorklogShells: { enumerable: true, get: () => _removeEmptyLiveWorklogShells, set: (value) => { _removeEmptyLiveWorklogShells = value; } },
  _setLiveWorklogThinkingPlaceholder: { enumerable: true, get: () => _setLiveWorklogThinkingPlaceholder, set: (value) => { _setLiveWorklogThinkingPlaceholder = value; } },
  ensureLiveWorklogShell: { enumerable: true, get: () => ensureLiveWorklogShell, set: (value) => { ensureLiveWorklogShell = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
