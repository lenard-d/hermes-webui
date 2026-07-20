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
  _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
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
  else requestAnimationFrame(()=>{ setTimeout(()=>{ _programmaticScroll=false; },0); });
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
  _programmaticScroll=true;
  container.scrollTop=_messageVirtualScrollTopForVisibleIdx(visWithIdx,visIdx,container);
  _messageVirtualWindowKey='';
  _messageViewportAnchorRemounting=true;
  try{
    renderMessages({preserveScroll:true});
  }finally{
    _messageViewportAnchorRemounting=false;
    requestAnimationFrame(()=>{ setTimeout(()=>{ _programmaticScroll=false; },0); });
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
        _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
        container.scrollTop=Math.max(0,scrollTopBefore+padDelta);
        _lastScrollTop=container.scrollTop;
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
  _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
  container.scrollTop=scrollTopBefore+delta;
  _lastScrollTop=container.scrollTop;
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
const _userRowIntrinsicHeightBySessionIdx=Object.create(null);
// Cleared on session switch alongside _messageVirtualHeightCache (both are
// per-session measured-height caches keyed by session-relative index). Without this,
// keys collide across sessions — _messageSessionIndexForRawIdx = _messageSessionIndexBase()
// + rawIdx and the base is 0 for the common non-offset session — so a new session's
// off-screen user rows would inherit the previous session's remembered heights and
// inflate scrollHeight until each is re-measured. Delete keys in place to keep the
// const binding stable for any closure that captured it.
function _clearUserRowIntrinsicHeightCache(){
  for(const k in _userRowIntrinsicHeightBySessionIdx) delete _userRowIntrinsicHeightBySessionIdx[k];
}
function _rememberUserRowIntrinsicHeight(sessionMsgIdx, height){
  const key=Number(sessionMsgIdx);
  if(!Number.isFinite(key)||!(height>0)) return;
  _userRowIntrinsicHeightBySessionIdx[key]=Math.round(height);
}
function _estimateUserRowIntrinsicHeight(rawText){
  const t=String(rawText||'');
  if(!t) return 96;
  // ~48 half-width chars/line at the mobile user-bubble width (≈90% of a phone viewport),
  // ~22px per line + ~24px row chrome; floored at the stylesheet's 96px so a short row never
  // reserves LESS than today (estimate can only add reserved height for tall rows, never
  // regress). CJK / full-width characters occupy ~2 columns each, so a Chinese/Japanese/
  // Korean paste wraps at ~24 chars/line — counting them as 1 badly UNDER-estimates the
  // height (a 3k-char CJK paste is ~2x taller than the naive length/48 guess). Weight wide
  // characters as 2 columns so the fresh-row reserve is close to reality even for a row the
  // reader has never scrolled into view (content-visibility:auto reports only the reserve
  // for a never-painted row, so a good estimate is the only backstop there). Uses a Unicode
  // range test (no \p{} — keep the RegExp engine-portable across the supported browsers).
  const explicitLines=(t.match(/\n/g)||[]).length+1;
  let columns=0;
  for(let i=0;i<t.length;i++){
    const c=t.charCodeAt(i);
    // CJK Unified + Ext-A, Hiragana/Katakana, Hangul, CJK symbols/punctuation, full-width forms.
    const wide=(c>=0x1100&&c<=0x115F)||(c>=0x2E80&&c<=0xA4CF)||(c>=0xAC00&&c<=0xD7A3)||
               (c>=0xF900&&c<=0xFAFF)||(c>=0xFE30&&c<=0xFE4F)||(c>=0xFF00&&c<=0xFF60)||(c>=0xFFE0&&c<=0xFFE6);
    columns+=wide?2:1;
  }
  const wrapLines=Math.ceil(columns/48);
  const lines=Math.max(explicitLines, wrapLines);
  return Math.max(96, Math.round(lines*22+24));
}
function _applyUserRowIntrinsicHeight(row, rawText){
  if(!row||!row.style||!row.dataset) return;
  const key=Number(row.dataset.sessionMsgIdx);
  const remembered=Number.isFinite(key)?Number(_userRowIntrinsicHeightBySessionIdx[key])||0:0;
  const estimate=_estimateUserRowIntrinsicHeight(rawText!=null?rawText:row.dataset.rawText);
  // Reserve the LARGER of the remembered measurement and the content estimate. A remembered
  // height can be a PARTIAL paint: a user row taller than the viewport that only ever had its
  // top slice scrolled through content-visibility:auto reports just the painted portion, not
  // its full height — persisting that would under-reserve and let scrollHeight collapse on the
  // next rebuild (the jump-back). Taking the max means a good estimate floors the reserve even
  // when the measurement under-read, while a full measurement (row shorter than the viewport,
  // fully painted) still wins when it exceeds the estimate.
  const h=Math.max(remembered, estimate);
  if(h>0) row.style.containIntrinsicSize='auto '+Math.round(h)+'px';
}
function _measureMessageVirtualRow(inner, entry){
  if(!inner||!entry) return 0;
  const primary=inner.querySelector(`[data-msg-idx="${entry.rawIdx}"]`);
  if(!primary) return 0;
  let totalHeight=Math.max(0, primary.getBoundingClientRect().height||0);
  if(primary.classList.contains('assistant-segment')){
    let sibling=primary.nextElementSibling;
    while(sibling){
      if(sibling.hasAttribute('data-msg-idx')) break;
      if(!(sibling.matches&&sibling.matches('.tool-call-group,.tool-card-row,.agent-activity-thinking,.thinking-card-row'))) break;
      totalHeight+=Math.max(0, sibling.getBoundingClientRect().height||0);
      sibling=sibling.nextElementSibling;
    }
  }
  // Persist the measured height so a later wipe-and-rebuild of this user row reserves its
  // real off-screen height instead of collapsing to the 96px estimate (the collapse that
  // clamps/re-anchors the viewport — #5637/#5638 mobile jump-back, both classes). The
  // typeof guard keeps _measureMessageVirtualRow runnable in the node test harnesses that
  // extract it without this helper (they stub every collaborator by name).
  if(totalHeight>0 && primary.dataset && primary.dataset.role==='user'
     && typeof _rememberUserRowIntrinsicHeight==='function'){
    _rememberUserRowIntrinsicHeight(primary.dataset.sessionMsgIdx, totalHeight);
    primary.style.containIntrinsicSize='auto '+Math.round(totalHeight)+'px';
  }
  return totalHeight;
}
function _updateMessageVirtualMeasurements(renderVisWithIdx, renderVisibleIdxs, virtualWindow){
  const inner=$('msgInner');
  if(!inner||!virtualWindow||!virtualWindow.virtualized||!renderVisWithIdx.length) return;
  let changed=false;
  let measuredCount=0;
  let measuredTotal=0;
  for(let vi=0;vi<renderVisWithIdx.length;vi++){
    const entry=renderVisWithIdx[vi];
    if(!entry) continue;
    const totalHeight=_measureMessageVirtualRow(inner, entry);
    if(totalHeight<=0) continue;
    const visibleIdx=Number(renderVisibleIdxs&&renderVisibleIdxs[vi]);
    if(!Number.isFinite(visibleIdx)) continue;
    if(Math.abs((Number(_messageVirtualHeightCache[visibleIdx])||0)-totalHeight)>1){
      _messageVirtualHeightCache[visibleIdx]=totalHeight;
      changed=true;
    }
    measuredTotal+=totalHeight;
    measuredCount++;
  }
  if(measuredCount>0){
    _messageVirtualEstimatedRowHeight=Math.max(60, Math.round(measuredTotal/measuredCount));
  }
  if(changed){
    _scheduleMessageVirtualMeasurementRefresh(virtualWindow);
  }else{
    _markMessageVirtualMeasurementsSettled(virtualWindow);
  }
}
// #5638 follow-up — the non-virtualized transcript path (the #4325 opt-out, where
// _virtualizeTranscript===false renders every row with no windowing) never runs the
// virtualized measure pass above, so a user row's real height is never remembered.
// content-visibility:auto on user rows then collapses a freshly-rebuilt off-screen tall
// user row to its flat contain-intrinsic-size estimate on every renderMessages() rebuild
// (each streaming frame does inner.innerHTML='' then rebuilds all rows as FRESH elements
// that have never painted at full size). scrollHeight shrinks by (realHeight-estimate),
// the browser force-clamps scrollTop, and the viewport jumps backward — the desktop/mobile
// jump-back, with JS=none because the clamp is the browser's own.
//
// The reliable moment to read a user row's REAL height is JUST BEFORE the wipe: the old
// rows are still in the DOM, laid out at full height (content-visibility:auto reports the
// true rect height once an element has painted, at any scroll position — verified: a tall
// off-screen user row still measures its real height pre-wipe). A POST-render read is
// unreliable because a freshly-rebuilt off-screen row reports its collapsed reserve, not
// its real size, so it would persist the wrong (small) value. Capture pre-wipe, keyed by
// the stable session-relative index, so the rebuild's _applyUserRowIntrinsicHeight reserves
// the real off-screen height and scrollHeight stays stable across the rebuild.
// Desktop rests at content-visibility:visible (intrinsic-size ignored) → inert there.
function _rememberRenderedUserRowIntrinsicHeights(){
  const container=$('messages');
  const inner=$('msgInner');
  if(!container||!inner) return;
  const rows=inner.querySelectorAll('.msg-row[data-role="user"][data-msg-idx]');
  if(!rows.length) return;
  const cRect=container.getBoundingClientRect();
  // Only trust a row that is currently WITHIN (or straddling) the viewport: such a row has
  // been painted at full size, so getBoundingClientRect().height is its REAL height. A row
  // that content-visibility:auto is skipping (fully off-screen and never painted this
  // session) reports only its contain-intrinsic-size reserve — persisting THAT would poison
  // the remembered height with the collapsed value and defeat the estimate backstop for a
  // never-seen row. The viewport intersection test is the reliable "has this row painted?"
  // signal (an off-screen row that WAS painted earlier keeps its real height too, but we
  // don't need it here — it either was captured on a prior in-view pass or the estimate
  // covers it). Small margin so a row just above/below the fold still counts as painted.
  const margin=Math.max(0, cRect.height||0);
  for(let i=0;i<rows.length;i++){
    const row=rows[i];
    if(!row||!row.dataset||!row.style) continue;
    const r=row.getBoundingClientRect();
    const measured=Math.max(0, r.height||0);
    if(!(measured>0)) continue;
    // In-viewport (with a one-screen margin) ⇒ painted ⇒ height is trustworthy — but only
    // for a row that FITS the viewport. A row taller than the viewport only ever paints the
    // intersecting slice under content-visibility:auto, so its measured height is a PARTIAL
    // value, not the full row. Floor every persisted height at the content estimate so a
    // partial paint can never lower the reserve below a reasonable full-row guess; a full
    // paint (short row) still wins when it exceeds the estimate.
    const inView=(r.bottom>=cRect.top-margin)&&(r.top<=cRect.bottom+margin);
    if(!inView) continue;
    const estimate=(typeof _estimateUserRowIntrinsicHeight==='function')
      ? _estimateUserRowIntrinsicHeight(row.dataset.rawText) : 0;
    const h=Math.max(measured, estimate);
    if(!(h>0)) continue;
    const key=Number(row.dataset.sessionMsgIdx);
    const remembered=Number.isFinite(key)?Number(_userRowIntrinsicHeightBySessionIdx[key])||0:0;
    // Keep the tallest reserve seen — a row mid-collapse (rebuild transient) can report a
    // shrunken size; never let that overwrite a good taller remembered value.
    if(h>=remembered && typeof _rememberUserRowIntrinsicHeight==='function'){
      _rememberUserRowIntrinsicHeight(row.dataset.sessionMsgIdx, h);
      row.style.containIntrinsicSize='auto '+Math.round(h)+'px';
    }
  }
}
function _scheduleMessageVirtualizedRender(force){
  const container=$('messages');
  const inner=$('msgInner');
  if(!container||!inner) return;
  const visWithIdx=_getVisibleMessagesWithIdx();
  const virtualWindow=_currentMessageVirtualWindow(visWithIdx,_messageVirtualKeepTailCount());
  const nextKey=_messageVirtualWindowKeyFor(virtualWindow);
  if(!force&&nextKey===_messageVirtualWindowKey) return;
  if(!virtualWindow.virtualized){
    _messageVirtualWindowKey=nextKey;
    return;
  }
  if(_messageVirtualScrollRaf) return;
  _messageVirtualScrollRaf=requestAnimationFrame(()=>{
    _messageVirtualScrollRaf=0;
    const liveVisWithIdx=_getVisibleMessagesWithIdx();
    const liveWindow=_currentMessageVirtualWindow(liveVisWithIdx,_messageVirtualKeepTailCount());
    const liveKey=_messageVirtualWindowKeyFor(liveWindow);
    if(!force&&liveKey===_messageVirtualWindowKey) return;
    if(_scrollbarDragActive){
      _programmaticScroll=true;
      _programmaticScrollSetAt=performance.now();
      _compensateScrollForMeasurementDelta(()=>{ renderMessages({ preserveScroll:true }); });
      _deferClearProgrammaticScroll();
      _messageVirtualWindowKey=liveKey;
      return;
    }
    _msgNodeRecycleEnabled=true;
    try{
      _compensateScrollForMeasurementDelta(()=>{ renderMessages({ preserveScroll:true }); });
    }
    finally{ _msgNodeRecycleEnabled=false; }
  });
}

// ── renderMd / _renderUserFencedBlocks cache ──────────────────────────────
// Long sessions re-render the same messages on every renderMessages() call.
// Cache the rendered HTML so unchanged messages skip the expensive regex
// pipeline entirely.  ~95% of messages are identical between renders.
const _renderCache = new Map();
const _renderCacheMax = 300;
function _clearRenderCache(){ _renderCache.clear(); }
function _renderCacheKey(text, isUser){
  // Fold render_user_markdown state into user-message keys so toggling the
  // setting invalidates cached plain-text renders (#3870).
  const p = isUser ? (window._renderUserMarkdown ? 'um' : 'u') : 'a';
  // Short content: use the full string as key (cheap Map lookup).
  // Long content: length + prefix + suffix is good enough — collisions on
  // 20-char prefix+suffix are vanishingly rare for chat messages.
  if(text.length <= 500) return p + ':' + text;
  return p + ':' + text.length + ':' + text.slice(0,20) + ':' + text.slice(-20);
}
function _getCachedRender(text, isUser){
  const key = _renderCacheKey(text, isUser);
  const hit = _renderCache.get(key);
  if(hit !== undefined) return hit;
  const rendered = isUser
    ? (window._renderUserMarkdown ? renderMd(text) : _renderUserFencedBlocks(text))
    : renderMd(_stripXmlToolCallsDisplay(String(text)));
  if(_renderCache.size > _renderCacheMax) _renderCache.clear();
  _renderCache.set(key, rendered);
  return rendered;
}
function _currentMessageRenderWindowSize(){
  return Math.max(
    MESSAGE_RENDER_WINDOW_DEFAULT,
    Number(_messageRenderWindowSize)||MESSAGE_RENDER_WINDOW_DEFAULT
  );
}
function _messageRenderableMessageCount(){
  return _getVisibleMessagesWithIdx().length;
}
function _messageHiddenBeforeCount(){
  return Math.max(0,_messageRenderableMessageCount()-_currentMessageRenderWindowSize());
}
function _isSessionEndlessScrollEnabled(){
  return window._sessionEndlessScrollEnabled===true;
}
function _wireMessageWindowLoadEarlierButton(){
  const indicator=$('loadOlderIndicator');
  if(!indicator) return;
  indicator.onclick=()=>{
    if(typeof _loadOlderMessages==='function') _loadOlderMessages();
  };
}
function _isSessionJumpButtonsEnabled(){
  return window._sessionJumpButtonsEnabled===true;
}
function _applySessionNavigationPrefs(){
  const container=$('messages');
  if(container) container.classList.toggle('session-nav-enabled',_isSessionJumpButtonsEnabled());
  _updateSessionStartJumpButton();
}
function _updateSessionStartJumpButton(){
  const btn=$('jumpToSessionStartBtn');
  const container=$('messages');
  if(!btn||!container) return;
  if(!_isSessionJumpButtonsEnabled()){
    btn.style.display='none';
    return;
  }
  const hasSession=!!(S&&S.session&&S.messages&&S.messages.length);
  const awayFromStart=container.scrollTop>Math.max(240,container.clientHeight*0.35);
  const hasScrollableHistory=container.scrollHeight>container.clientHeight+Math.max(240,container.clientHeight*0.35);
  const canRevealStart=hasScrollableHistory||_messageHiddenBeforeCount()>0||!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  btn.style.display=(hasSession&&canRevealStart&&awayFromStart)?'flex':'none';
}
async function jumpToSessionStart(){
  const container=$('messages');
  if(!container||!S.session) return;
  _scrollPinned=false;
  _messageUserUnpinned=true;
  _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
  try{
    // During active streaming, skip full message load — API response won't
    // include live messages from the current turn, and replacing S.messages
    // would lose user/assistant/tool messages.
    if(!(S.busy||S.activeStreamId)){
      if(typeof _ensureAllMessagesLoaded==='function') await _ensureAllMessagesLoaded();
    }
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    container.scrollTop=0;
    _messageVirtualWindowKey='';
    // During streaming, skip renderMessages — it rebuilds the DOM but tool card
    // insertion is blocked by !S.busy, losing Activity until "done" fires.
    if(!(S.busy||S.activeStreamId)){
      renderMessages({ preserveScroll:true });
    }
    requestAnimationFrame(()=>{
      container.scrollTop=0;
      _updateSessionStartJumpButton();
      _deferClearProgrammaticScroll();
    });
  }catch(e){
    console.warn('jumpToSessionStart failed:',e);
    _programmaticScroll=false;
  }
}

function _userMessageDomId(rawIdx){
  return `msg-user-${rawIdx}`;
}

function _questionJumpButtonHtml(questionRawIdx, assistantRawIdx){
  if(typeof questionRawIdx!=='number'||questionRawIdx<0) return '';
  const label=t('jump_to_question')||'Response';
  const title=t('jump_to_question_label')||'Jump to the start of this response';
  const aIdx=(typeof assistantRawIdx==='number'&&assistantRawIdx>=0)?assistantRawIdx:-1;
  return `<button class="msg-question-jump-btn session-jump-btn session-jump-btn--inline" type="button" title="${esc(title)}" aria-label="${esc(title)}" onclick="jumpToTurnQuestion(${questionRawIdx},${aIdx})"><span aria-hidden="true">↑</span><span>${esc(label)}</span></button>`;
}

function _highlightQuestionRow(row){
  if(!row) return;
  row.classList.remove('msg-question-highlight');
  void row.offsetWidth;
  row.classList.add('msg-question-highlight');
  window.setTimeout(()=>row.classList.remove('msg-question-highlight'),1800);
}

async function jumpToTurnQuestion(questionRawIdx, assistantRawIdx){
  const container=$('messages');
  if(!container||typeof questionRawIdx!=='number'||questionRawIdx<0) return;
  const scrollToTarget=()=>{
    const hasAssistant=typeof assistantRawIdx==='number'&&assistantRawIdx>=0;
    if(hasAssistant){
      // A single assistant rawIdx can render multiple segment nodes — some hidden
      // (assistant-segment-worklog-source / assistant-segment-anchor are display:none).
      // scrollIntoView() on a hidden node silently no-ops, so only treat a VISIBLE
      // segment (getClientRects().length>0) as a successful target; otherwise fall
      // through to the question-row fallback rather than suppressing it. (#3934)
      const segs=container.querySelectorAll('[data-msg-idx="'+assistantRawIdx+'"]');
      for(const seg of segs){
        if(seg.getClientRects().length>0){
          seg.scrollIntoView({block:'start',behavior:'smooth'});
          return true;
        }
      }
    }
    const row=document.getElementById(_userMessageDomId(questionRawIdx));
    if(!row) return false;
    row.scrollIntoView({block:'center',behavior:'smooth'});
    _highlightQuestionRow(row);
    return true;
  };
  if(scrollToTarget()) return;
  const visWithIdx=_getVisibleMessagesWithIdx();
  const visibleIdx=_messageVisibleIndexForRawIdx(questionRawIdx, visWithIdx);
  if(visibleIdx>=0){
    _scrollPinned=false;
    _messageUserUnpinned=true;
    _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
    container.scrollTop=_messageVirtualScrollTopForVisibleIdx(visWithIdx, visibleIdx, container);
    _messageVirtualWindowKey='';
    renderMessages({ preserveScroll:true });
    requestAnimationFrame(()=>{
      if(!scrollToTarget()&&_messageHiddenBeforeCount()>0){
        _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
        _messageVirtualWindowKey='';
        renderMessages({ preserveScroll:true });
        requestAnimationFrame(scrollToTarget);
      }
      _deferClearProgrammaticScroll();
    });
    return;
  }
  if(_messageHiddenBeforeCount()>0){
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    _messageVirtualWindowKey='';
    renderMessages({ preserveScroll:true });
    requestAnimationFrame(scrollToTarget);
  }
}

const DASHBOARD_STATUS_TTL_MS=60000;
let _dashboardStatusCache=null;
let _dashboardStatusFetchedAt=0;
let _dashboardLastNonNeverMode='auto'; // Server-scoped dashboard config keeps this restore target session-global on purpose.
let _dashboardSettingsLoadSeq=0;
let _dashboardSettingsWriteSeq=0;

function _dashboardIsBrowserLoopback(){
  const host=(window.location.hostname||'').replace(/^\[|\]$/g,'').toLowerCase();
  return host==='127.0.0.1'||host==='localhost'||host==='::1';
}

function _normalizeDashboardEnabledMode(mode){
  return mode==='auto'||mode==='always'||mode==='never'?mode:'auto';
}

function _setDashboardModeForChip(mode){
  mode=_normalizeDashboardEnabledMode(mode);
  if(mode==='auto'||mode==='always') _dashboardLastNonNeverMode=mode;
}

function _getDashboardChipRestoreMode(){
  return _dashboardLastNonNeverMode||'auto';
}

function _dashboardBrowserUrl(status){
  if(!status||!status.running) return '';
  if(status.browser_url||status.url){
    try{return new URL(status.browser_url||status.url).toString().replace(/\/$/,'');}
    catch(_){}
  }
  if(!status.port) return '';
  let source;
  try{source=new URL('http://127.0.0.1:'+status.port);}
  catch(_){return '';}
  const browserHost=window.location.hostname||source.hostname;
  const displayHost=browserHost.includes(':')&&!browserHost.startsWith('[')?'['+browserHost+']':browserHost;
  return source.protocol+'//'+displayHost+':'+status.port;
}
function _stripInlineEventHandlers(node){
  if(!node)return;
  const strip=el=>{
    Array.from(el.attributes||[]).forEach(attr=>{
      if(attr.name&&attr.name.toLowerCase().startsWith('on'))el.removeAttribute(attr.name);
    });
    if('onclick' in el)el.onclick=null;
    Array.from(el.children||[]).forEach(strip);
  };
  strip(node);
}
function _syncNavActionMirrors(){
  const rail=document.querySelector('.rail');
  const sidebar=document.querySelector('.sidebar-nav');
  if(!rail||!sidebar)return;
  const sources=Array.from(rail.querySelectorAll('.nav-tab:not([data-panel]):not([data-dashboard-link])')).filter(source=>source.id);
  const mirrors=Array.from(sidebar.querySelectorAll('[data-nav-action-mirror]'));
  const sourceIds=new Set(sources.map(source=>source.id));
  mirrors.forEach(mirror=>{
    if(!sourceIds.has(mirror.getAttribute('data-nav-action-mirror')))mirror.remove();
  });
  sources.forEach(source=>{
    const sourceVisible=(()=>{
      if(source.hidden||source.getAttribute('aria-hidden')==='true')return false;
      if(source.classList.contains('nav-tab-hidden'))return false;
      if(source.style&&(source.style.display==='none'||source.style.visibility==='hidden'))return false;
      if(typeof window!=='undefined'&&typeof window.getComputedStyle==='function'){
        const computed=window.getComputedStyle(source);
        if(computed&&(computed.display==='none'||computed.visibility==='hidden'))return false;
      }
      return true;
    })();
    let mirror=mirrors.find(el=>el.getAttribute('data-nav-action-mirror')===source.id);
    if(!mirror){
      mirror=source.cloneNode(true);
      _stripInlineEventHandlers(mirror);
      mirror.id=source.id+'Mobile';
      mirror.classList.remove('rail-btn');
      mirror.classList.add('has-tooltip--bottom');
      mirror.setAttribute('data-nav-action-mirror',source.id);
      mirror.addEventListener('click',e=>{
        e.preventDefault();
        if(mirror._navActionSource)mirror._navActionSource.click();
        if(typeof closeMobileSidebar==='function')closeMobileSidebar();
      });
      const anchor=sidebar.querySelector('.dashboard-link,[data-dashboard-link]')||sidebar.querySelector('[data-panel="logs"]');
      sidebar.insertBefore(mirror,anchor||null);
    }else{
      mirror.innerHTML=source.innerHTML;
      _stripInlineEventHandlers(mirror);
    }
    mirror._navActionSource=source;
    mirror.classList.toggle('nav-action-visible',sourceVisible);
    const label=source.getAttribute('data-tooltip')||source.getAttribute('aria-label')||'';
    if(label)mirror.setAttribute('data-label',label);
  });
}
function _initNavActionMirrors(){
  _syncNavActionMirrors();
  const rail=document.querySelector('.rail');
  if(rail&&window.MutationObserver)new MutationObserver(_syncNavActionMirrors).observe(rail,{
    childList:true,
    subtree:true,
    attributes:true,
    attributeFilter:['class','style','hidden','aria-hidden','data-tooltip','aria-label'],
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_initNavActionMirrors,{once:true});
else _initNavActionMirrors();
function _applyDashboardStatus(status){
  const running=!!(status&&status.running);
  const url=running?_dashboardBrowserUrl(status):'';
  const warning=running&&!_dashboardIsBrowserLoopback()?t('dashboard_loopback_warning'):'';
  document.querySelectorAll('[data-dashboard-link]').forEach(btn=>{
    btn.classList.toggle('dashboard-link-visible',running);
    btn.classList.toggle('nav-action-visible',running);
    btn.style.display=running?'':'none';
    btn.dataset.dashboardUrl=url;
    const tipText=warning||t('tab_dashboard');
    if(btn.hasAttribute('data-tooltip')){
      // Sync the custom CSS tooltip and explicitly clear the native title so
      // the slow ~1.5s native browser tooltip does not co-fire alongside the
      // fast custom tooltip (#1775).
      btn.setAttribute('data-tooltip',tipText);
      if(btn.hasAttribute('title')) btn.removeAttribute('title');
    } else {
      btn.title=tipText;
    }
    btn.setAttribute('aria-label',tipText);
  });
}
async function refreshDashboardStatus(force=false){
  const now=Date.now();
  // Skip the interval-driven poll while the tab is hidden: the 60s interval
  // equals the cache TTL, so every background tick was a real /api/dashboard/status
  // fetch that never hit the cache — a needless wakeup on a tab nobody is
  // looking at (battery/CPU, #2476). Forced calls (settings save, init, the
  // visibilitychange catch-up) still run. A visible tab keeps its live status.
  if(!force&&typeof document!=='undefined'&&document.hidden){
    return _dashboardStatusCache;
  }
  if(!force&&_dashboardStatusCache&&(now-_dashboardStatusFetchedAt)<DASHBOARD_STATUS_TTL_MS){
    _applyDashboardStatus(_dashboardStatusCache);
    return _dashboardStatusCache;
  }
  try{
    const status=await api('/api/dashboard/status',{timeoutToast:false});
    _dashboardStatusCache=status||{running:false};
  }catch(_){
    _dashboardStatusCache={running:false};
  }
  _dashboardStatusFetchedAt=Date.now();
  _applyDashboardStatus(_dashboardStatusCache);
  return _dashboardStatusCache;
}
async function loadDashboardSettings(){
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  if(!modeEl&&!urlEl) return;
  const loadSeq=++_dashboardSettingsLoadSeq;
  const writeSeq=_dashboardSettingsWriteSeq;
  try{
    const cfg=await api('/api/dashboard/config');
    if(loadSeq!==_dashboardSettingsLoadSeq||writeSeq!==_dashboardSettingsWriteSeq) return;
    const mode=_normalizeDashboardEnabledMode(cfg&&cfg.enabled);
    if(modeEl) modeEl.value=mode;
    _setDashboardModeForChip(mode);
    if(urlEl) urlEl.value=cfg.url||'';
    if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();
  }catch(_){/* leave defaults visible */}
}
async function saveDashboardSettings(opts){
  opts=opts||{};
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  const statusEl=$('settingsDashboardStatus');
  const payload={enabled:(modeEl&&modeEl.value)||'auto',url:(urlEl&&urlEl.value||'').trim()};
  _dashboardSettingsWriteSeq+=1;
  try{
    const saved=await api('/api/dashboard/config',{method:'POST',body:JSON.stringify(payload)});
    const mode=_normalizeDashboardEnabledMode(saved&&saved.enabled);
    if(modeEl) modeEl.value=mode;
    _setDashboardModeForChip(mode);
    if(urlEl) urlEl.value=saved.url||'';
    if(statusEl) statusEl.textContent='Dashboard link settings saved.';
    await refreshDashboardStatus(true);
    if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();
  }catch(err){
    if(statusEl) statusEl.textContent='Dashboard link settings failed to save.';
    else if(typeof showToast==='function') showToast('Dashboard link settings failed to save.');
    try{await loadDashboardSettings();}catch(_){}
    if(opts.raiseOnError) throw err;
  }
}
function openHermesDashboard(event){
  if(event){event.preventDefault();event.stopPropagation();}
  const btn=event&&event.currentTarget?event.currentTarget:document.querySelector('[data-dashboard-link]');
  const url=(btn&&btn.dataset&&btn.dataset.dashboardUrl)||_dashboardBrowserUrl(_dashboardStatusCache);
  if(!url) return false;
  window.open(url,'_blank','noopener,noreferrer');
  return false;
}
function _initDashboardLinkProbe(){
  loadDashboardSettings();
  refreshDashboardStatus(true);
  setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS);
  // Catch up once when the tab becomes visible again, since the interval poll
  // was skipped while hidden and its cache is now stale.
  if(typeof document!=='undefined'&&typeof document.addEventListener==='function'){
    document.addEventListener('visibilitychange',()=>{
      if(!document.hidden) refreshDashboardStatus(true);
    });
  }
}
if(document.readyState==='complete'){
  _initDashboardLinkProbe();
}else{
  document.addEventListener('DOMContentLoaded',_initDashboardLinkProbe,{once:true});
}

/* ── Image lightbox — click any .msg-media-img to enlarge ─────────────────── */

window.HermesUI.register('viewport', {
  clearVisibleMessageRowCache,
  jumpToSessionStart,
  jumpToTurnQuestion,
  openHermesDashboard,
});
