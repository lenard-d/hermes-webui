import { _liveAssistantSegmentTextLength } from './live-turn-recovery.js';
import { INFLIGHT, S } from './state.js';

  // Mid-stream flicker fix (#3877): when a renderMessages() rebuild is reached
  // while THIS session is actively streaming (e.g. the clarify-response echo at
  // messages.js, or a CLI-import refresh), the `inner.innerHTML=''` below detaches
  // the live `#liveAssistantTurn` node — and the smd parser keeps writing into
  // that now-orphaned node, so the streamed text vanishes until the next stream
  // event rebuilds the turn ("disappears, then reappears"). Capture the live
  // turn's actual DOM node (not its HTML — the parser holds a live reference into
  // it) so it can be re-attached after the rebuild, keeping the parser target
  // connected and the streamed text visible. Only for the streaming session's own
  // live turn; never affects settled transcripts.
function captureLiveAssistantTurn(sid) {
  let _preservedLiveTurn=null;
  if(sid&&INFLIGHT[sid]){
    const _lt=document.getElementById('liveAssistantTurn');
    if(_lt&&(!_lt.dataset||!_lt.dataset.sessionId||_lt.dataset.sessionId===sid)){
      // Blank-turn fix (对话消失): only preserve the live turn across the DOM
      // wipe if it is GENUINELY live — either an active stream is still running
      // (S.activeStreamId set: the #3877 mid-stream flicker case this preserve
      // was written for), or the turn already holds real rendered content (a
      // visible answer body, a tool card, or a reasoning row). A DEAD shell —
      // an interrupted turn whose stream dropped (S.activeStreamId cleared to
      // null) but whose INFLIGHT[sid] entry was not cleaned, leaving only an
      // empty worklog group ("Processed Ns" with no body/tool rows) — must NOT
      // be preserved: re-attaching it on a session-updated swap re-render pins
      // an avatar-only empty turn OVER the settled transcript, hiding the real
      // (already-persisted) answer. That is the reported blank. Reproduced +
      // fix verified on an isolated debug instance (8710): stale INFLIGHT +
      // empty live-turn survived the swap → blank; gating on real-content /
      // active-stream clears it while a genuine live turn still renders.
      const _hasRealLiveContent=!!_lt.querySelector('.msg-body, .tool-card-row, .wl-reason');
      if(_hasRealLiveContent || S.activeStreamId){
        _preservedLiveTurn=_lt;
      }
    }
  }

  return _preservedLiveTurn;
}

function restoreLiveAssistantTurn(_preservedLiveTurn, inner) {
  // Re-attach the preserved live turn (#3877). The rebuild above recreated a
  // live turn from S.messages, but the live assistant message's content lags the
  // stream (it is only persisted to S.messages on a throttled write-back) — so the
  // fresh node often shows LESS streamed text than the ORIGINAL node, which is
  // still referenced by the smd parser and holds the real in-progress reply. Swap
  // the preserved (parser) node back in so the parser target stays connected and
  // the visible text never blanks.
  //
  // The swap fires when the preserved node carries at least as much streamed text
  // as the rebuilt one (`_rebuiltLen <= _preservedLen`). The `<=` (not `<`) is
  // load-bearing: at the throttled-persist boundary the rebuilt turn's live
  // content can EQUAL the preserved length, and the old `<` guard then skipped the
  // swap — leaving the smd parser writing into the detached original node, which
  // is exactly the residual "disappears, then reappears" frame (#3877 reopen). On
  // a tie the preserved node is strictly preferable (it holds the live parser
  // reference; identical length means nothing is lost). When the rebuilt turn
  // genuinely has MORE content (e.g. a reconnect where S.messages caught up past
  // the parser), the guard correctly skips and lets the parser re-resolve to the
  // fuller node.
  //
  // Swap at the SEGMENT level — replace only the rebuilt live segment with the
  // preserved one — so a multi-segment turn (earlier settled segments + tool/
  // worklog groups built by the rebuild) keeps that rebuilt-only structure; a
  // whole-turn replaceWith would discard it when the preserved snapshot predates
  // those segments. Fall back to whole-turn replace only when the rebuilt turn has
  // no live segment to swap into. No-op for a settled turn or when nothing was
  // streaming.
  if(_preservedLiveTurn){
    const _rebuilt=document.getElementById('liveAssistantTurn');
    // Pick the PARSER-OWNED live segment, not just the first one. On reconnect /
    // post-tool activity boundaries a live turn can carry MULTIPLE
    // [data-live-assistant="1"] segments, and the smd parser writes into the
    // LAST (tail) one (see ensureAssistantRow in messages.js — it re-attaches to
    // the last live segment). Prefer the preserved segment whose
    // data-live-segment-seq matches the rebuilt tail (same logical segment), then
    // fall back to the last preserved live segment. Using querySelector() (first)
    // here would move the wrong segment and leave the parser-owned tail detached
    // in a multi-segment turn.
    const _rebuiltSegs=_rebuilt?_rebuilt.querySelectorAll('[data-live-assistant="1"]'):null;
    const _rebuiltSeg=(_rebuiltSegs&&_rebuiltSegs.length)?_rebuiltSegs[_rebuiltSegs.length-1]:null;
    const _preservedSegs=_preservedLiveTurn.querySelectorAll('[data-live-assistant="1"]');
    let _preservedSeg=_preservedSegs.length?_preservedSegs[_preservedSegs.length-1]:null;
    const _rebuiltSeq=_rebuiltSeg?_rebuiltSeg.getAttribute('data-live-segment-seq'):null;
    if(_rebuiltSeq){
      for(const _seg of _preservedSegs){
        if(_seg.getAttribute('data-live-segment-seq')===_rebuiltSeq){_preservedSeg=_seg;break;}
      }
    }
    const _preservedLen=_liveAssistantSegmentTextLength(_preservedSeg||_preservedLiveTurn);
    // Structural-block counts: a live turn can be AHEAD of S.messages with
    // Activity/tool/worklog blocks that haven't persisted yet — even with ZERO
    // streamed text (e.g. an Activity-only turn mid-tool-call). The text-length
    // gate alone would skip preservation in that case, so a scroll-triggered
    // rebuild on a long (virtualized) transcript could blink those live-only
    // blocks for a frame. Also restore when the preserved turn carries more
    // structure than the rebuilt (lagging-S.messages) turn. (#3714 ship-review)
    const _structuralCount=(turn)=> turn?turn.querySelectorAll(
      '[data-live-assistant="1"],.tool-call-group,.tool-card-row,'+
      '.tool-worklog-group,.live-worklog[data-live-worklog-shell="1"],'+
      '.wl-reason,.agent-activity-thinking,.thinking-card-row'
    ).length:0;
    const _preservedStructure=_structuralCount(_preservedLiveTurn);
    const _rebuiltStructure=_structuralCount(_rebuilt);
    if(_preservedLen>0 || _preservedStructure>_rebuiltStructure){
      const _rebuiltLen=_rebuilt?_liveAssistantSegmentTextLength(_rebuiltSeg||_rebuilt):-1;
      if(_rebuiltLen<=_preservedLen){
        // Decide segment-level vs whole-turn restore. Segment-level keeps the
        // rebuilt turn's structure (good when the rebuild is the structural
        // superset). But the whole premise here is that the live DOM can be
        // AHEAD of S.messages: a tool/worklog group can land in the live turn
        // between the last throttled persist and this rebuild, so the rebuilt
        // turn (built from the lagging S.messages) may have FEWER structural
        // blocks. In that case a segment-only swap would drop those live-only
        // blocks for a frame — so restore the WHOLE preserved turn instead.
        // Otherwise (rebuild has >= the preserved turn's structural blocks) do
        // the precise segment swap so rebuilt-only structure is kept.
        if(_rebuilt&&_rebuiltSeg&&_preservedSeg&&_rebuiltStructure>=_preservedStructure){
          // Rebuild is the structural superset — swap only the parser-owned
          // (tail) live segment, keeping rebuilt-only segments / tool groups.
          // (No dataset.sessionId stamp here: only the segment enters the DOM;
          // the rebuilt turn was already stamped at build time, see above.)
          _rebuiltSeg.replaceWith(_preservedSeg);
        }else if(_rebuilt){
          // Rebuilt turn lacks structure the live turn already has (live-only
          // tool card not yet persisted), or has no live segment to target —
          // restore the whole preserved turn so nothing the user saw vanishes.
          if(S.session) _preservedLiveTurn.dataset.sessionId=S.session.session_id;
          _rebuilt.replaceWith(_preservedLiveTurn);
        }else{
          if(S.session) _preservedLiveTurn.dataset.sessionId=S.session.session_id;
          inner.appendChild(_preservedLiveTurn);
        }
      }
    }
  }

}

const compatibilityBindings=Object.freeze({});

export {
  captureLiveAssistantTurn,
  restoreLiveAssistantTurn,
  compatibilityBindings,
};
