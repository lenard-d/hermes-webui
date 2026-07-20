import { _deferClearProgrammaticScroll, _recentMessageScrollIntent, _recentMessageTouchScrollIntent } from './composer-controls.js';
import { rerenderMessages as renderMessages } from './transcript-render-dispatch.js';
import { $ } from './state.js';
import { _getVisibleMessagesWithIdx, _messageRawIdxForSessionIndex, _messageSessionIndexForRawIdx, _messageVirtualScrollTopForVisibleIdx, _messageVisibleIndexForAnchorKey, _messageVisibleIndexForRawIdx, compatibilityBindings as virtualStateBindings } from './message-virtualization-state.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';

function _captureMessageViewportAnchor(){
  const container=$('messages');
  if(!container) return null;
  const containerRect=container.getBoundingClientRect();
  const rows=Array.from(container.querySelectorAll('[data-msg-idx]'));
  for(const row of rows){
    const rawIdx=Number(row&&row.dataset&&row.dataset.msgIdx);
    if(!Number.isFinite(rawIdx)) continue;
    const rect=row.getBoundingClientRect();
    if(rect.bottom>containerRect.top+1){
      const sessionIdx=Number(row&&row.dataset&&row.dataset.sessionMsgIdx);
      // Record the current top-spacer (virtual topPad) height so the compensation
      // path can fall back to a topPad-delta shift when the anchor row itself is
      // recycled out of the render window after a measurement-driven re-render.
      const spacer=container.querySelector('[data-virtual-spacer="before"]');
      const topPadBefore=spacer?parseFloat(spacer.style.height||'0')||0:0;
      return {
        rawIdx,
        sessionIdx:Number.isFinite(sessionIdx)?sessionIdx:_messageSessionIndexForRawIdx(rawIdx),
        key:row&&row.dataset?String(row.dataset.messageAnchorKey||''):'',
        topOffset:rect.top-containerRect.top,
        topPadBefore,
        // Snapshot the scroll height at capture so a later realign can detect that
        // content grew between capture and restore — the streaming case where the
        // anchor's topOffset is stale and realigning to it would yank a still reader
        // backward (issue #5637).
        scrollHeightAtCapture:container.scrollHeight,
      };
    }
  }
  return null;
}
// Temporarily suppress the browser's native overflow-anchor on a scroll
// container so a JS scrollTop write is not double-compensated by the browser's
// own scroll-anchoring in the same frame. Returns a release fn that restores the
// prior inline value on the NEXT frame (after layout settles). No-op on desktop,
// where the resting computed value is already `none` (CSS hover/fine-pointer
// media query) — suppressing `none` changes nothing and the release restores the
// same empty inline value. Only mobile (resting `auto`) is actually affected,
// which is exactly where the double-compensation jump-back happens.
//
// Both this helper and _fixMobileScrollJank() gate on the SAME question — "is
// the browser's native scroll-anchor layer currently active on this element?" —
// routed through this one predicate so the two guards can't drift apart if the
// CSS media query ever changes (maintainer review on #5338). The computed-value
// test is more robust than a matchMedia('(hover:hover) and (pointer:fine)')
// check because it reflects the real resting value, including any inline
// override, not just the viewport media state.
function _browserOverflowAnchorActive(el){
  if(!el) return false;
  try{ return getComputedStyle(el).overflowAnchor==='auto'; }catch(_){ return false; }
}
// iOS/iPadOS WebKit detection for the issue #5637 stale-anchor hold gate. CSS
// overflow-anchor is INERT on iOS WebKit (see static/style.css — the mobile
// content-visibility block deliberately does NOT set overflow-anchor:none because
// it is a no-op on iOS and, on Android, re-opens the #4856/#5338 jump-to-top
// regression). So `overflow-anchor:auto` computes on `.messages` on iOS but the
// engine never actually holds the viewport there. The stale-anchor refusal relies
// on that engine to hold the reader, so it is only safe on Android (working
// overflow-anchor), NOT iOS — refusing on iOS leaves a scrolled-up reader unheld,
// the same class as the desktop regression, one platform over.
// Detection covers classic iPhone/iPod/iPad UAs AND iPadOS 13+, which reports a
// desktop 'MacIntel' platform but is distinguishable by touch support (a real Mac
// has maxTouchPoints 0). Excludes MSStream (old IE on Windows Phone false-matched
// 'like iPhone').
function _isIOSWebKit(){
  try{
    const nav=(typeof navigator!=='undefined')?navigator:null;
    if(!nav) return false;
    if(nav.MSStream) return false;
    const ua=String(nav.userAgent||'');
    if(/iP(ad|hone|od)/.test(ua)) return true;
    // iPadOS 13+ masquerades as macOS; a Mac has no touch, an iPad does.
    if(nav.platform==='MacIntel' && Number(nav.maxTouchPoints)>1) return true;
  }catch(_){}
  return false;
}
// Stable "native overflow-anchor holds this viewport" predicate for the issue
// #5637 stale-anchor hold gate. The two stale-anchor refusals below assume the
// browser's native overflow-anchor layer will hold the viewport once the JS
// restore is refused. That is only true where the engine ACTUALLY compensates:
//   - desktop (hover+fine-pointer): CSS keeps `.messages` at overflow-anchor:none
//     -> engine off -> refusing leaves nothing to hold the reader. Excluded via
//     matchMedia('(pointer:coarse)') being false.
//   - iOS WebKit: overflow-anchor is INERT (see _isIOSWebKit) even though it
//     computes to `auto` -> engine never holds -> refusing strands a scrolled-up
//     reader. Excluded via _isIOSWebKit().
//   - Android touch: overflow-anchor:auto AND the engine works -> refusing is safe,
//     native anchoring holds. This is the ONLY platform the refusal targets.
// We must NOT decide this with `_browserOverflowAnchorActive(#messages)` alone,
// because `_restoreMessageViewportAnchor` temporarily writes an inline
// `overflowAnchor:'none'` on #messages for its own scroll write and only restores
// it on the next frame; when the realign fires every live tick that inline 'none'
// persists across ticks, so a computed-value probe would read 'none' mid-realign
// and wrongly classify a touch device as "desktop", letting the stale realign
// through. A matchMedia('(pointer:coarse)') test reflects the input device and
// cannot be mutated by that inline override, so it stays steady mid-realign;
// desktop (fine pointer) stays false. Fall back to the computed-anchor probe when
// matchMedia is unavailable.
function _isTouchLikeMessageViewport(el){
  // iOS WebKit is touch (pointer:coarse) but overflow-anchor is inert there, so the
  // refusal's premise fails — treat it like desktop (keep the semantic realign).
  if(_isIOSWebKit()) return false;
  try{
    if(typeof matchMedia==='function' && matchMedia('(pointer:coarse)').matches) return true;
  }catch(_){}
  // Best-effort fallback for the (today essentially non-existent) no-matchMedia
  // environment: the computed-anchor probe can transiently read 'none' during a
  // realign burst (see comment above), so on such a touch device this could
  // re-admit the original yank. matchMedia('(pointer:coarse)') is universally
  // supported in every browser this UI targets, so the primary path is what runs.
  return _browserOverflowAnchorActive(el);
}
function _suppressBrowserOverflowAnchor(container){
  if(!container||!container.style) return null;
  // Only engage when the browser layer is actually active (auto). On desktop
  // (none) there is nothing to suppress.
  if(!_browserOverflowAnchorActive(container)) return null;
  const prevInline=container.style.overflowAnchor||'';
  container.style.overflowAnchor='none';
  let released=false;
  return function _release(){
    if(released) return;
    released=true;
    const restore=()=>{
      // Only restore if we still own the suppression (another render may have
      // re-set it); compare against the value we wrote.
      if(container.style.overflowAnchor==='none') container.style.overflowAnchor=prevInline;
    };
    if(typeof requestAnimationFrame==='function') requestAnimationFrame(restore);
    else restore();
  };
}
function _restoreMessageViewportAnchor(anchor, rawIdxDelta){
  const container=$('messages');
  if(!container||!anchor) return false;
  const anchorKey=String(anchor.key||'');
  const sessionIdx=Number(anchor.sessionIdx);
  const hasSessionIdx=Number.isFinite(sessionIdx);
  let row=anchorKey?Array.from(container.querySelectorAll('[data-message-anchor-key]')).find(el=>el&&el.dataset&&el.dataset.messageAnchorKey===anchorKey):null;
  if(row&&row.getClientRects&&row.getClientRects().length===0) row=null;
  // The anchor key is content-derived (role|ts|attachments|first-160-chars, built by
  // _messageViewportAnchorKeyForMessage) so it goes STALE while a live assistant
  // message is still streaming: every chunk that changes the first 160 chars
  // recomputes that row's data-message-anchor-key, so a snapshot captured mid-stream
  // no longer matches by key. We used to concede the moment the keyed lookup missed
  // (`if(!row&&anchorKey) return false`), and the caller then fell back to an ABSOLUTE
  // scrollTop=snapshot.top that does NOT compensate the above-viewport height growth
  // from that same streaming chunk — the residual DESKTOP scroll jump-back. (Desktop
  // rests at overflow-anchor:none, so #5392's mobile overflow-anchor guard is a no-op
  // here; this is a distinct code path.) The anchored row is still in the DOM under
  // its STABLE session-relative index, so recover it via sessionIdx before conceding.
  // A genuinely removed anchor (message compressed/deleted away) misses key AND
  // sessionIdx and still returns false. A missing sessionIdx is NOT degraded to the
  // window-relative rawIdx (which could resolve to a different message), preserving
  // the original per-tier guard.
  if(!row&&hasSessionIdx) row=container.querySelector(`[data-session-msg-idx="${sessionIdx}"]`);
  if(!row&&(anchorKey||hasSessionIdx)) return false;
  const targetIdx=Number(anchor.rawIdx)+Number(rawIdxDelta||0);
  if(!row&&Number.isFinite(targetIdx)) row=container.querySelector(`[data-msg-idx="${targetIdx}"]`);
  if(!row) return false;
  const containerRect=container.getBoundingClientRect();
  const rect=row.getBoundingClientRect();
  const targetTop=Number(anchor.topOffset)||0;
  // Streaming stale-anchor guard (issue #5637). During a live stream, content grows
  // ABOVE the viewport between anchor capture and this restore, so the anchor's
  // captured topOffset is stale and the realign delta becomes a spurious few-hundred-px
  // value that yanks a still reader backward. Detect it by content growth + absence of
  // real input intent — NOT by a scrollTop diff, because on an overflow-anchor:auto
  // container the browser itself moves scrollTop to compensate the growth (so a still
  // reader's scrollTop is not stationary). _recentMessage*ScrollIntent reflects genuine
  // touch/wheel/key input, which the browser's anchor layer never writes. If content
  // grew since capture AND there is no recent input intent AND the realign would move
  // scrollTop non-trivially, refuse it and let the browser overflow-anchor hold. An
  // actively scrolling reader (recent intent) keeps the legitimate realign; legacy
  // snapshots without the captured geometry keep prior behavior.
  //
  // Desktop guard (issue #5637 gate cert): the refusal is only safe where the
  // browser's native overflow-anchor layer can actually hold the viewport, i.e.
  // touch viewports where `.messages` computes to `overflow-anchor:auto`. On
  // hover+fine-pointer desktops `.messages` is `overflow-anchor:none`, so refusing
  // the realign would leave NOTHING to hold the reader after above-viewport growth
  // — the very yank this fixes on mobile, reintroduced on desktop. Gate the refusal
  // on `_isTouchLikeMessageViewport` so desktop keeps its semantic scrollTop realign.
  const _realignDelta=(rect.top-containerRect.top)-targetTop;
  const _shAtCap=Number(anchor.scrollHeightAtCapture);
  if(Number.isFinite(_shAtCap)){
    const _grewSinceCapture=(container.scrollHeight-_shAtCap)>4;
    const _activeIntent=(typeof _recentMessageScrollIntent==='function' && _recentMessageScrollIntent())
      || (typeof _recentMessageTouchScrollIntent==='function' && _recentMessageTouchScrollIntent());
    const _touchHold=(typeof _isTouchLikeMessageViewport==='function' && _isTouchLikeMessageViewport(container));
    if(_touchHold&&_grewSinceCapture&&!_activeIntent&&Math.abs(_realignDelta)>8){
      return false;
    }
  }
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  // Mobile-only jump fix: the resting overflow-anchor on .messages is `auto` on
  // touch devices (CSS media query keeps it `none` only for hover+fine-pointer
  // desktops). When we write scrollTop here to realign the anchor row, a mobile
  // browser's OWN overflow-anchor machinery ALSO shifts scrollTop in the same
  // frame if content height above the viewport changed — the two compensations
  // stack and yank the reader to an unrelated turn (the mobile jump-back). This is why the
  // bug is mobile-only and never reproduces on a desktop (none) browser. Suppress
  // the browser layer for this write; _releaseAnchorSuppression restores it next
  // frame. Desktop is already `none`, so this is a no-op there.
  const _releaseAnchorSuppression=(typeof _suppressBrowserOverflowAnchor==='function')
    ? _suppressBrowserOverflowAnchor(container) : null;
  container.scrollTop+=(rect.top-containerRect.top)-targetTop;
  if(_releaseAnchorSuppression) _releaseAnchorSuppression();
  if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
  else requestAnimationFrame(()=>{ setTimeout(()=>{ composerControlsBindings._programmaticScroll=false; },0); });
  return true;
}
let _messageViewportAnchorRemounting=false;
function _remountMessageViewportAnchor(anchor){
  const container=$('messages');
  if(!container||!anchor||_messageViewportAnchorRemounting) return false;
  const anchorKey=String(anchor.key||'');
  const visibleKeyNode=anchorKey
    ? Array.from(container.querySelectorAll('[data-message-anchor-key]')).find(node=>node&&node.dataset&&node.dataset.messageAnchorKey===anchorKey&&(!node.getClientRects||node.getClientRects().length>0))
    : null;
  if(visibleKeyNode) return true;
  const sessionIdx=Number(anchor.sessionIdx);
  const hasSessionIdx=Number.isFinite(sessionIdx);
  if(!anchorKey&&hasSessionIdx&&container.querySelector(`[data-session-msg-idx="${sessionIdx}"]`)) return true;
  const targetIdx=Number(anchor.rawIdx);
  if(!anchorKey&&!hasSessionIdx&&Number.isFinite(targetIdx)&&container.querySelector(`[data-msg-idx="${targetIdx}"]`)) return true;
  if(typeof _getVisibleMessagesWithIdx!=='function'||
     typeof _messageVisibleIndexForRawIdx!=='function'||
     typeof _messageVirtualScrollTopForVisibleIdx!=='function'||
     typeof renderMessages!=='function') return false;
  const visWithIdx=_getVisibleMessagesWithIdx();
  let visIdx=anchorKey?_messageVisibleIndexForAnchorKey(anchorKey,visWithIdx):-1;
  if(visIdx<0&&hasSessionIdx){
    const rawFromSession=_messageRawIdxForSessionIndex(sessionIdx);
    if(Number.isFinite(rawFromSession)) visIdx=_messageVisibleIndexForRawIdx(rawFromSession,visWithIdx);
  }
  if(visIdx<0&&Number.isFinite(targetIdx)) visIdx=_messageVisibleIndexForRawIdx(targetIdx,visWithIdx);
  if(visIdx<0) return false;
  // A virtualized anchor may be outside the current DOM. Scroll to its virtual
  // row and render once so the semantic restore below has a real target.
  composerControlsBindings._programmaticScroll=true;
  container.scrollTop=_messageVirtualScrollTopForVisibleIdx(visWithIdx,visIdx,container);
  virtualStateBindings._messageVirtualWindowKey='';
  _messageViewportAnchorRemounting=true;
  try{
    renderMessages({preserveScroll:true});
  }finally{
    _messageViewportAnchorRemounting=false;
    requestAnimationFrame(()=>{ setTimeout(()=>{ composerControlsBindings._programmaticScroll=false; },0); });
  }
  if(anchorKey){
    return !!Array.from(container.querySelectorAll('[data-message-anchor-key]')).find(node=>node&&node.dataset&&node.dataset.messageAnchorKey===anchorKey&&(!node.getClientRects||node.getClientRects().length>0));
  }
  if(hasSessionIdx) return !!container.querySelector(`[data-session-msg-idx="${sessionIdx}"]`);
  return Number.isFinite(targetIdx)&&!!container.querySelector(`[data-msg-idx="${targetIdx}"]`);
}
function _compensateScrollForMeasurementDelta(renderFn){
  const container=$('messages');
  if(!container) return renderFn();
  const anchorBefore=_captureMessageViewportAnchor();
  const scrollTopBefore=container.scrollTop;
  container.classList.add('vscroll-measuring');
  try{ renderFn(); }finally{ container.classList.remove('vscroll-measuring'); }
  if(!anchorBefore) return;
  if(scrollTopBefore<1){
    const spacer=container.querySelector('[data-virtual-spacer="before"]');
    if(!spacer||parseFloat(spacer.style.height||'0')<=0) return;
  }
  // Re-find the anchor row after the measurement-driven re-render. The primary
  // lookup is by rawIdx (the DOM index), but on a big virtualized session a large
  // scroll delta can RECYCLE the old anchor row out of the render window entirely
  // (verified via real-device telemetry: DOM collapsed to 1 row, scrollHeight
  // lurched by tens of thousands of px). The old code did `if(!row) return` here,
  // abandoning compensation → the full estimated↔measured height lurch hit
  // scrollTop uncompensated and threw the viewport to the top (the recurring
  // mobile scroll jump-back). Fall back to the stable sessionIdx anchor (captured in
  // _captureMessageViewportAnchor) before giving up, mirroring the "recover via
  // sessionIdx when the primary anchor key is gone" approach used elsewhere but for
  // the virtualization-measurement compensation path.
  let row=container.querySelector(`[data-msg-idx="${anchorBefore.rawIdx}"]`);
  if(!row&&Number.isFinite(Number(anchorBefore.sessionIdx))){
    row=container.querySelector(`[data-session-msg-idx="${anchorBefore.sessionIdx}"]`);
  }
  if(!row){
    // Anchor row is no longer rendered (recycled out of the virtual window). We
    // cannot measure its live offset, but we CAN keep the viewport visually
    // stable by compensating for the top-spacer (topPad) height change: the
    // whole reason scrollHeight lurched is that the estimated topPad was replaced
    // by a measured one. Shift scrollTop by that same delta so content under the
    // viewport does not appear to jump. Without this the browser lands at an
    // uncompensated absolute scrollTop against a wildly different scrollHeight.
    const spacerAfter=container.querySelector('[data-virtual-spacer="before"]');
    const topPadAfter=spacerAfter?parseFloat(spacerAfter.style.height||'0')||0:0;
    const topPadBefore=Number(anchorBefore.topPadBefore);
    if(Number.isFinite(topPadBefore)){
      const padDelta=topPadAfter-topPadBefore;
      if(Math.abs(padDelta)>=2){
        composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
        container.scrollTop=Math.max(0,scrollTopBefore+padDelta);
        composerControlsBindings._lastScrollTop=container.scrollTop;
        _deferClearProgrammaticScroll();
      }
    }
    return;
  }
  const containerRect=container.getBoundingClientRect();
  const rowRect=row.getBoundingClientRect();
  const actualOffset=rowRect.top-containerRect.top;
  const delta=actualOffset-anchorBefore.topOffset;
  if(Math.abs(delta)<2) return;
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  container.scrollTop=scrollTopBefore+delta;
  composerControlsBindings._lastScrollTop=container.scrollTop;
  _deferClearProgrammaticScroll();
}
function _messageViewportIntersectsRenderedRow(){
  const container=$('messages');
  if(!container) return true;
  const containerRect=container.getBoundingClientRect();
  const rows=Array.from(container.querySelectorAll('[data-msg-idx]'));
  for(const row of rows){
    const rect=row.getBoundingClientRect();
    if(rect.bottom>containerRect.top+1&&rect.top<containerRect.bottom-1) return true;
  }
  return false;
}
// #5637/#5638 follow-up — kill the content-visibility scrollHeight collapse at its
// source. A virtualization wipe-and-rebuild recreates user rows as FRESH elements, which
// discards content-visibility:auto's last-remembered size, so an off-screen user row
// falls back to the flat `contain-intrinsic-size: auto 96px` estimate in the stylesheet.
// A tall user row (e.g. a long paste) then collapses scrollHeight by (realHeight-96px)
// the instant it's rebuilt off-screen, and the browser either force-clamps scrollTop
// (dTop≈dH layer-1 jump) or re-anchors to a far row (dTop≫dH browser re-anchor jump) —
// both mobile jump-back classes trace to this one collapse. Remember each user row's
// height keyed by its STABLE session-relative index so a rebuild reserves the real
// height, not 96px. Measured height (exact) wins; before a row is ever measured, a
// content-length estimate reserves the bulk so the fresh-element frame doesn't collapse
// either. Refreshed every measure pass, so edits self-heal. Desktop rests at
// content-visibility:visible (intrinsic-size ignored) → inert there, zero behavior change.

export {
  _captureMessageViewportAnchor,
  _browserOverflowAnchorActive,
  _isIOSWebKit,
  _isTouchLikeMessageViewport,
  _suppressBrowserOverflowAnchor,
  _restoreMessageViewportAnchor,
  _remountMessageViewportAnchor,
  _compensateScrollForMeasurementDelta,
  _messageViewportIntersectsRenderedRow,
  _messageViewportAnchorRemounting,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _captureMessageViewportAnchor: { enumerable:true, get:()=>_captureMessageViewportAnchor, set:value=>{ _captureMessageViewportAnchor=value; } },
  _browserOverflowAnchorActive: { enumerable:true, get:()=>_browserOverflowAnchorActive, set:value=>{ _browserOverflowAnchorActive=value; } },
  _isIOSWebKit: { enumerable:true, get:()=>_isIOSWebKit, set:value=>{ _isIOSWebKit=value; } },
  _isTouchLikeMessageViewport: { enumerable:true, get:()=>_isTouchLikeMessageViewport, set:value=>{ _isTouchLikeMessageViewport=value; } },
  _suppressBrowserOverflowAnchor: { enumerable:true, get:()=>_suppressBrowserOverflowAnchor, set:value=>{ _suppressBrowserOverflowAnchor=value; } },
  _restoreMessageViewportAnchor: { enumerable:true, get:()=>_restoreMessageViewportAnchor, set:value=>{ _restoreMessageViewportAnchor=value; } },
  _remountMessageViewportAnchor: { enumerable:true, get:()=>_remountMessageViewportAnchor, set:value=>{ _remountMessageViewportAnchor=value; } },
  _compensateScrollForMeasurementDelta: { enumerable:true, get:()=>_compensateScrollForMeasurementDelta, set:value=>{ _compensateScrollForMeasurementDelta=value; } },
  _messageViewportIntersectsRenderedRow: { enumerable:true, get:()=>_messageViewportIntersectsRenderedRow, set:value=>{ _messageViewportIntersectsRenderedRow=value; } },
  _messageViewportAnchorRemounting: { enumerable:true, get:()=>_messageViewportAnchorRemounting, set:value=>{ _messageViewportAnchorRemounting=value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
