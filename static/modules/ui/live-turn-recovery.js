import { _startActivityElapsedTimer } from './activity-and-scroll.js';
import { _rehydrateTransparentStreamDom } from './activity-presentation.js';
import { normalizeLiveActivityGroupPlacement } from './anchor-scenes.js';
import { _assistantTurnBlocks } from './assistant-turn-presentation.js';
import { _postProcessWithAnchorSuppression } from './content-postprocessing.js';
import { $, INFLIGHT, S } from './state.js';
import { _dedupeLiveProcessedWorklogAnchors } from './transparent-worklog.js';

function snapshotLiveTurnHtmlForSession(sid){
  // Keep the DOM snapshot memory-only. Persisted INFLIGHT state intentionally
  // stores structured stream state, not outerHTML, so a hard reload still uses
  // the safer flat replay path instead of reviving stale nodes/listeners.
  if(!sid||!INFLIGHT[sid]) return;
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  if(turn.dataset&&turn.dataset.sessionId&&turn.dataset.sessionId!==sid) return;
  INFLIGHT[sid].liveTurnHtml=turn.outerHTML;
}

function _liveAssistantSegmentTextLength(seg){
  if(!seg) return 0;
  const body=seg.querySelector('.msg-body')||seg;
  return String(body.textContent||'').trim().length;
}

function _mergeRestoredLiveAssistantSegment(restored, existing){
  if(!restored||!existing) return;
  const existingLive=existing.querySelector('[data-live-assistant="1"]');
  if(!existingLive) return;
  const restoredLive=restored.querySelector('[data-live-assistant="1"]');
  const existingLen=_liveAssistantSegmentTextLength(existingLive);
  const restoredLen=_liveAssistantSegmentTextLength(restoredLive);
  if(existingLen<=restoredLen) return;
  const replacement=existingLive.cloneNode(true);
  if(restoredLive){
    restoredLive.replaceWith(replacement);
    return;
  }
  const blocks=_assistantTurnBlocks(restored);
  if(!blocks) return;
  const anchor=Array.from(blocks.children).filter(el=>
    el.matches('.tool-call-group,.tool-card-row,.agent-activity-thinking,.thinking-card-row,[data-live-assistant="1"]')
  ).pop();
  if(anchor) anchor.insertAdjacentElement('afterend', replacement);
  else blocks.appendChild(replacement);
}

function restoreLiveTurnHtmlForSession(sid){
  const inflight=INFLIGHT[sid];
  if(!sid||!inflight||!inflight.liveTurnHtml) return false;
  const inner=$('msgInner');
  if(!inner) return false;
  const template=document.createElement('template');
  template.innerHTML=String(inflight.liveTurnHtml||'').trim();
  const restored=template.content.firstElementChild;
  if(!restored) return false;
  restored.id='liveAssistantTurn';
  if(S.session) restored.dataset.sessionId=S.session.session_id;
  const existing=$('liveAssistantTurn');
  _mergeRestoredLiveAssistantSegment(restored, existing);
  if(existing) existing.replaceWith(restored);
  else inner.appendChild(restored);
  // Transparent Stream: liveTurnHtml is restored via template.innerHTML, which
  // drops the property-bound onclick/onkeydown handlers wired by
  // _wireTransparentHeaderToggle / _attachCopyButton / _syncTransparentEventControls /
  // _wireTransparentTurnToggle. The settled cache fast-path re-runs the rehydrate;
  // this active-session live-turn restore path must too, or row toggles, copy
  // buttons, expand/collapse, and the turn chevron silently stop working after a
  // session-switch/reconnect restore. (Codex trifecta finding C1.)
  if(typeof _rehydrateTransparentStreamDom==='function') _rehydrateTransparentStreamDom(restored);
  if(typeof normalizeLiveActivityGroupPlacement==='function') normalizeLiveActivityGroupPlacement(restored);
  if(typeof _dedupeLiveProcessedWorklogAnchors==='function') _dedupeLiveProcessedWorklogAnchors(restored);
  const liveGroup=restored.querySelector('.tool-call-group[data-live-tool-call-group="1"]');
  if(liveGroup&&typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(liveGroup);
  if(typeof placeLiveToolCardsHost==='function') placeLiveToolCardsHost();
  requestAnimationFrame(()=>_postProcessWithAnchorSuppression(restored));
  return true;
}

export {
  snapshotLiveTurnHtmlForSession,
  _liveAssistantSegmentTextLength,
  _mergeRestoredLiveAssistantSegment,
  restoreLiveTurnHtmlForSession,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  snapshotLiveTurnHtmlForSession: { enumerable: true, get: () => snapshotLiveTurnHtmlForSession, set: value => { snapshotLiveTurnHtmlForSession = value; } },
  _liveAssistantSegmentTextLength: { enumerable: true, get: () => _liveAssistantSegmentTextLength, set: value => { _liveAssistantSegmentTextLength = value; } },
  _mergeRestoredLiveAssistantSegment: { enumerable: true, get: () => _mergeRestoredLiveAssistantSegment, set: value => { _mergeRestoredLiveAssistantSegment = value; } },
  restoreLiveTurnHtmlForSession: { enumerable: true, get: () => restoreLiveTurnHtmlForSession, set: value => { restoreLiveTurnHtmlForSession = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
