import { _shouldFollowMessagesOnDomReplace } from './activity-and-scroll.js';
import {
  _deferClearProgrammaticScroll,
  _lastMessageClientHeight,
  _lastScrollTop,
  _messageUserUnpinned,
  _nearBottomCount,
  _programmaticScroll,
  _programmaticScrollSetAt,
  _recentMessageScrollIntent,
  _scrollPinned,
  compatibilityBindings as composerControlsBindings,
} from './composer-controls.js';
import {
  _browserOverflowAnchorActive,
  _captureMessageViewportAnchor,
  _remountMessageViewportAnchor,
  _restoreMessageViewportAnchor,
} from './message-viewport-anchor.js';
import { $ } from './state.js';

function _captureMessageScrollSnapshot(){
  const el=$('messages');
  if(!el) return null;
  const bottom=Math.max(0,el.scrollHeight-el.scrollTop-el.clientHeight);
  const readerAwayFromBottom=bottom>250&&(
    _messageUserUnpinned ||
    _scrollPinned===false ||
    (typeof _recentMessageScrollIntent==='function'&&_recentMessageScrollIntent())
  );
  return {
    anchor:(typeof _captureMessageViewportAnchor==='function')?_captureMessageViewportAnchor():null,
    top:el.scrollTop,
    bottom,
    scrollHeight:el.scrollHeight,
    pinned:readerAwayFromBottom?false:_shouldFollowMessagesOnDomReplace(),
    userUnpinned:readerAwayFromBottom?true:_messageUserUnpinned,
  };
}
function _restorePinnedMessageScrollSnapshot(snapshot){
  const el=$('messages');
  if(!el||!snapshot||snapshot.pinned!==true||snapshot.userUnpinned===true) return false;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  const bottom=Number(snapshot.bottom);
  const target=Number.isFinite(bottom)?maxTop-Math.max(0,bottom):maxTop;
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  el.scrollTop=Math.max(0,Math.min(target,maxTop));
  // Sync _lastScrollTop after programmatic restore so sticky-unpin does not false-trigger (#1731).
  composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
  composerControlsBindings._messageUserUnpinned=false;
  composerControlsBindings._scrollPinned=true;
  composerControlsBindings._nearBottomCount=2;
  if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
  else requestAnimationFrame(()=>{ setTimeout(()=>{ composerControlsBindings._programmaticScroll=false; },0); });
  return true;
}
function _restoreMessageScrollSnapshot(snapshot){
  const el=$('messages');
  if(!el||!snapshot) return;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  // If the reader was following the live tail, preserve the tail-relative bottom
  // distance. Do not semantic-anchor to the first visible row: live Worklog/
  // activity rebuilds can remount an older top-of-viewport anchor and yank a
  // pinned streaming transcript upward. Semantic anchors remain for manual
  // unpinned reading positions below.
  if(_restorePinnedMessageScrollSnapshot(snapshot)) return;
  let restoredViaAnchor=(snapshot.anchor&&typeof _restoreMessageViewportAnchor==='function')
    ? _restoreMessageViewportAnchor(snapshot.anchor,0)
    : false;
  if(!restoredViaAnchor&&typeof _remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor)){
    restoredViaAnchor=(typeof _restoreMessageViewportAnchor==='function')
      ? _restoreMessageViewportAnchor(snapshot.anchor,0)
      : false;
  }
  if(!restoredViaAnchor){
    composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
    el.scrollTop=Math.max(0,Math.min(Number(snapshot.top)||0,maxTop));
  }
  // Sync _lastScrollTop after programmatic restore so sticky-unpin does not false-trigger (#1731).
  composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
  if(snapshot.userUnpinned===true){
    composerControlsBindings._messageUserUnpinned=true;
    composerControlsBindings._scrollPinned=false;
    composerControlsBindings._nearBottomCount=0;
  }else if(snapshot.pinned===true){
    composerControlsBindings._messageUserUnpinned=false;
    composerControlsBindings._scrollPinned=true;
    composerControlsBindings._nearBottomCount=2;
  }else{
    const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
    if(bottomDistance>250){
      composerControlsBindings._messageUserUnpinned=true;
      composerControlsBindings._scrollPinned=false;
      composerControlsBindings._nearBottomCount=0;
    }else if(bottomDistance<=120){
      composerControlsBindings._messageUserUnpinned=false;
      composerControlsBindings._scrollPinned=true;
      composerControlsBindings._nearBottomCount=2;
    }
  }
  if(!restoredViaAnchor){
    if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
    else requestAnimationFrame(()=>{ setTimeout(()=>{ composerControlsBindings._programmaticScroll=false; },0); });
  }
}
/**
 * Mobile scroll-jank guard: temporarily disable overflow-anchor so
 * Chromium cannot re-anchor to the topmost row during the innerHTML=''
 * wipe-and-rebuild gap. The rAF callback restores CSS default afterward.
 */
// Mobile scroll jump-back root fix. On touch devices #messages rests at
// overflow-anchor:auto, so the browser's native scroll-anchoring engine
// re-compensates scrollTop in the LAYOUT phase whenever content above the
// viewport changes height — worklog live→settled collapse, tool-card inserts,
// media/katex reflow, virtual-scroll topPad recompute, the STREAM_DONE
// multi-render sequence. That compensation happens in the browser's layout step,
// INDEPENDENT of which frame our JS wrote scrollTop in, so per-write suppression
// (a single-rAF guard) could not reach it: the collapse/reflow lands a frame or
// two later, after the guard already released. Real mobile flight-recorder data
// (captured jumps with dTop -101/+350/+748/-400, call stack = rAF sampler only =
// NO JS frame) confirmed the compensation is the browser engine, not our scroll
// writes.
//
// Fix: DEFER the restore AND track CSS animations. Each call re-arms suppression
// and cancels any pending release, so a burst of renders (STREAM_DONE fires
// several back-to-back) shares ONE suppression window. The base window is two
// animation frames + a settle timeout, which covers churn that is NOT a CSS
// animation (virtual topPad recompute, image-decode, katex measure). But the
// dominant churn is CSS max-height collapse/expand animations on worklog rows —
// .activity-body (.34s), .tool-group-body (.3s), .tool-card-detail (.26s) — which
// run LONGER than a fixed window; a fixed window lifts mid-animation and the rest
// of the animation still jumps. So we also bind transitionrun/transitionend on
// #messages: an animation start holds suppression (cancels the pending release);
// an animation end schedules a short settle after the LAST one. Hard-capped so a
// looping transition can't pin overflow-anchor:none forever. Desktop rests at
// none (predicate false) → the whole guard is a no-op.
const _MOBILE_ANCHOR_BASE_SETTLE_MS=400;
const _MOBILE_ANCHOR_POST_TRANSITION_MS=90;
const _MOBILE_ANCHOR_MAX_HOLD_MS=1200;
let _mobileAnchorSuppressReleaseTimer=null;
let _mobileAnchorSuppressRafId=0;
let _mobileAnchorTransitionListenerBound=false;
let _mobileAnchorSuppressArmedAt=0;
// Independent hard-cap timer. Unlike the settle/rAF release (which the
// transitionrun handler CANCELS to hold across an animation), this one is NEVER
// cancelled by re-arm or by onRun — it is only ever cleared when suppression is
// actually lifted, and re-armed to a fresh deadline on each _fixMobileScrollJank
// call. This guarantees overflow-anchor returns to the mobile resting 'auto'
// even if EVERY transitionend/transitioncancel is missed (animation interrupted,
// element detached mid-transition, etc.) — the #5338 contract that mobile rests
// at 'auto' must hold no matter what. (Gate-cert defect: the previous
// _MOBILE_ANCHOR_MAX_HOLD_MS was only a guard clause inside onRun, so a missed
// transitionend pinned 'none' forever.)
let _mobileAnchorMaxHoldTimer=null;
function _liftMobileAnchorSuppression(el){
  if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
  if(_mobileAnchorMaxHoldTimer){ clearTimeout(_mobileAnchorMaxHoldTimer); _mobileAnchorMaxHoldTimer=null; }
  if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
  _mobileAnchorSuppressRafId=0;
  // Only clear the inline value we set; a concurrent path may have legitimately
  // re-armed it (checked via the 'none' guard).
  if(el&&el.style&&el.style.overflowAnchor==='none') el.style.overflowAnchor='';
}
function _bindMobileAnchorTransitionExtender(el){
  if(_mobileAnchorTransitionListenerBound||!el||!el.addEventListener) return;
  _mobileAnchorTransitionListenerBound=true;
  // Only act while suppression is actually armed (inline 'none') and within the
  // hard cap, so we never pin overflow-anchor:none indefinitely.
  const onRun=(e)=>{
    if(!e||e.propertyName!=='max-height') return;
    if(el.style.overflowAnchor!=='none') return;
    if(_mobileAnchorSuppressArmedAt && (performance.now()-_mobileAnchorSuppressArmedAt)>_MOBILE_ANCHOR_MAX_HOLD_MS) return;
    // An animation is running — cancel the pending SETTLE release so we stay
    // suppressed until it ends (transitionend re-schedules the settle). The
    // independent max-hold timer is deliberately NOT cancelled here.
    if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
    if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
    _mobileAnchorSuppressRafId=0;
  };
  const onEnd=(e)=>{
    if(!e||e.propertyName!=='max-height') return;
    if(el.style.overflowAnchor!=='none') return;
    // This animation ended; settle shortly after (another may still be running,
    // in which case its own transitionrun already cancelled this timer).
    if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); }
    _mobileAnchorSuppressReleaseTimer=setTimeout(()=>{
      _mobileAnchorSuppressReleaseTimer=null;
      _liftMobileAnchorSuppression(el);
    },_MOBILE_ANCHOR_POST_TRANSITION_MS);
  };
  el.addEventListener('transitionrun',onRun,{passive:true});
  el.addEventListener('transitionstart',onRun,{passive:true});
  el.addEventListener('transitionend',onEnd,{passive:true});
  el.addEventListener('transitioncancel',onEnd,{passive:true});
}
window._fixMobileScrollJank=function _fixMobileScrollJank(){
  const el=document.getElementById('messages');
  if(!el) return;
  // Engage when the browser scroll-anchor layer is active (mobile auto), OR when
  // WE are already holding an inline suppression from a prior call in the same
  // burst. The predicate reads the COMPUTED value, which our own inline
  // overflow-anchor:none flips to 'none' — so on the 2nd..Nth call of a
  // STREAM_DONE burst the predicate would say false and short-circuit the re-arm
  // below, collapsing the whole "consecutive renders extend the window" behavior
  // to a single first-call window. Treat an inline 'none' WE set as still-armed
  // so re-arm actually runs. Desktop rests at computed 'none' with EMPTY inline,
  // so `alreadySuppressed` is false there and this stays a no-op. (Gate-cert
  // defect: re-arm was dead code without this.)
  const alreadySuppressed=el.style.overflowAnchor==='none';
  if(!alreadySuppressed && !_browserOverflowAnchorActive(el)) return;
  el.style.overflowAnchor='none';
  _bindMobileAnchorTransitionExtender(el);
  _mobileAnchorSuppressArmedAt=performance.now();
  // Re-arm: cancel any pending release so consecutive renders EXTEND, not shorten,
  // the suppression window (the STREAM_DONE settle fires renderMessages several
  // times back-to-back, plus a deferred postProcess reflow).
  if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
  if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
  _mobileAnchorSuppressRafId=0;
  // Independent hard cap: (re)arm a release that NOTHING cancels except an actual
  // lift, so a missed transitionend can never pin 'none' past the cap.
  if(_mobileAnchorMaxHoldTimer){ clearTimeout(_mobileAnchorMaxHoldTimer); }
  _mobileAnchorMaxHoldTimer=setTimeout(()=>{
    _mobileAnchorMaxHoldTimer=null;
    _liftMobileAnchorSuppression(el);
  },_MOBILE_ANCHOR_MAX_HOLD_MS);
  const rafHop=(cb)=>{ if(typeof requestAnimationFrame==='function') return requestAnimationFrame(cb); return setTimeout(cb,16); };
  // Base window: two animation frames (paint + post-render reflow settle) THEN a
  // settle timeout. CSS max-height animations are covered by the transitionrun/
  // transitionend extender above; this floor covers non-animated churn.
  _mobileAnchorSuppressRafId=rafHop(()=>{
    _mobileAnchorSuppressRafId=rafHop(()=>{
      _mobileAnchorSuppressReleaseTimer=setTimeout(()=>{
        _mobileAnchorSuppressReleaseTimer=null;
        _liftMobileAnchorSuppression(el);
      },_MOBILE_ANCHOR_BASE_SETTLE_MS);
    });
  });
};

// Desktop stale-snapshot residue (issue #5637 follow-up). Reached only when
// _restoreMessageViewportAnchor already CONCEDED (anchor row unrecoverable by its
// per-tier lookup) and the desktop fallback would otherwise write the ABSOLUTE
// snapshot.top — which is stale once above-viewport content grew since capture,
// yanking a still reader backward. The correct hold is the app's own realign
// idiom: shift the CURRENT scrollTop by how far the anchor row moved since capture,
// `scrollTop += (currentOffset - capturedOffset)` (mirrors _restoreMessageViewportAnchor
// ui.js and _compensateScrollForMeasurementDelta). NOT `snapshot.top + delta`: a
// row's offset is scroll-relative (rect.top - containerRect.top = rowContentPos -
// scrollTop), so only a delta applied to the LIVE scrollTop holds the row put
// regardless of where scrollTop was carried to. Returns the realign delta (may be
// 0), or null when the anchor row can't be measured under the SAME per-tier guard
// _restoreMessageViewportAnchor uses (key -> sessionIdx, never the rawIdx
// degradation — rawIdx maps to a different message after a virtualization
// re-window, ui.js per-tier guard) so the caller can fall back to the topPad-delta
// idiom or keep raw rather than guessing.
function _desktopAnchorRealignDelta(container, anchor){
  if(!container||!anchor||typeof container.querySelector!=='function') return null;
  const capturedOffset=Number(anchor.topOffset);
  if(!Number.isFinite(capturedOffset)) return null;
  const anchorKey=String(anchor.key||'');
  let row=anchorKey
    ? Array.from(container.querySelectorAll('[data-message-anchor-key]')).find(el=>el&&el.dataset&&el.dataset.messageAnchorKey===anchorKey)
    : null;
  if(row&&row.getClientRects&&row.getClientRects().length===0) row=null;
  const sessionIdx=Number(anchor.sessionIdx);
  if(!row&&Number.isFinite(sessionIdx)) row=container.querySelector(`[data-session-msg-idx="${sessionIdx}"]`);
  // Per-tier guard mirror (ui.js _restoreMessageViewportAnchor): a genuinely-gone
  // anchor misses key AND sessionIdx -> concede (null). Do NOT degrade to rawIdx.
  if(!row) return null;
  if(typeof row.getBoundingClientRect!=='function') return null;
  if(row.getClientRects&&row.getClientRects().length===0) return null;
  const containerRect=container.getBoundingClientRect();
  const rect=row.getBoundingClientRect();
  const currentOffset=rect.top-containerRect.top;
  return currentOffset-capturedOffset;
}

export {
  _captureMessageScrollSnapshot,
  _restorePinnedMessageScrollSnapshot,
  _restoreMessageScrollSnapshot,
  _liftMobileAnchorSuppression,
  _bindMobileAnchorTransitionExtender,
  _desktopAnchorRealignDelta,
  _MOBILE_ANCHOR_BASE_SETTLE_MS,
  _MOBILE_ANCHOR_POST_TRANSITION_MS,
  _MOBILE_ANCHOR_MAX_HOLD_MS,
  _mobileAnchorSuppressReleaseTimer,
  _mobileAnchorSuppressRafId,
  _mobileAnchorTransitionListenerBound,
  _mobileAnchorSuppressArmedAt,
  _mobileAnchorMaxHoldTimer,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _captureMessageScrollSnapshot: { enumerable: true, get: () => _captureMessageScrollSnapshot, set: (value) => { _captureMessageScrollSnapshot = value; } },
  _restorePinnedMessageScrollSnapshot: { enumerable: true, get: () => _restorePinnedMessageScrollSnapshot, set: (value) => { _restorePinnedMessageScrollSnapshot = value; } },
  _restoreMessageScrollSnapshot: { enumerable: true, get: () => _restoreMessageScrollSnapshot, set: (value) => { _restoreMessageScrollSnapshot = value; } },
  _liftMobileAnchorSuppression: { enumerable: true, get: () => _liftMobileAnchorSuppression, set: (value) => { _liftMobileAnchorSuppression = value; } },
  _bindMobileAnchorTransitionExtender: { enumerable: true, get: () => _bindMobileAnchorTransitionExtender, set: (value) => { _bindMobileAnchorTransitionExtender = value; } },
  _desktopAnchorRealignDelta: { enumerable: true, get: () => _desktopAnchorRealignDelta, set: (value) => { _desktopAnchorRealignDelta = value; } },
  _MOBILE_ANCHOR_BASE_SETTLE_MS: { enumerable: true, get: () => _MOBILE_ANCHOR_BASE_SETTLE_MS },
  _MOBILE_ANCHOR_POST_TRANSITION_MS: { enumerable: true, get: () => _MOBILE_ANCHOR_POST_TRANSITION_MS },
  _MOBILE_ANCHOR_MAX_HOLD_MS: { enumerable: true, get: () => _MOBILE_ANCHOR_MAX_HOLD_MS },
  _mobileAnchorSuppressReleaseTimer: { enumerable: true, get: () => _mobileAnchorSuppressReleaseTimer, set: (value) => { _mobileAnchorSuppressReleaseTimer = value; } },
  _mobileAnchorSuppressRafId: { enumerable: true, get: () => _mobileAnchorSuppressRafId, set: (value) => { _mobileAnchorSuppressRafId = value; } },
  _mobileAnchorTransitionListenerBound: { enumerable: true, get: () => _mobileAnchorTransitionListenerBound, set: (value) => { _mobileAnchorTransitionListenerBound = value; } },
  _mobileAnchorSuppressArmedAt: { enumerable: true, get: () => _mobileAnchorSuppressArmedAt, set: (value) => { _mobileAnchorSuppressArmedAt = value; } },
  _mobileAnchorMaxHoldTimer: { enumerable: true, get: () => _mobileAnchorMaxHoldTimer, set: (value) => { _mobileAnchorMaxHoldTimer = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
