import { _stripXmlToolCallsDisplay } from './activity-and-scroll.js';
import { _deferClearProgrammaticScroll } from './composer-controls.js';
import { renderMd } from './markdown-renderer.js';
import { _renderUserFencedBlocks } from './user-message-presentation.js';
import { rerenderMessages as renderMessages } from './transcript-render-dispatch.js';
import { $ } from './state.js';
import { _currentMessageVirtualWindow, _getVisibleMessagesWithIdx, _markMessageVirtualMeasurementsSettled, _messageVirtualHeightCache, _messageVirtualKeepTailCount, _messageVirtualScrollRaf, _messageVirtualWindowKey, _messageVirtualWindowKeyFor, _scheduleMessageVirtualMeasurementRefresh, _scrollbarDragActive, registerMessageVirtualizationLifecycle, compatibilityBindings as virtualStateBindings } from './message-virtualization-state.js';
import { _compensateScrollForMeasurementDelta } from './message-viewport-anchor.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';

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
    virtualStateBindings._messageVirtualEstimatedRowHeight=Math.max(60, Math.round(measuredTotal/measuredCount));
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
    virtualStateBindings._messageVirtualWindowKey=nextKey;
    return;
  }
  if(_messageVirtualScrollRaf) return;
  virtualStateBindings._messageVirtualScrollRaf=requestAnimationFrame(()=>{
    virtualStateBindings._messageVirtualScrollRaf=0;
    const liveVisWithIdx=_getVisibleMessagesWithIdx();
    const liveWindow=_currentMessageVirtualWindow(liveVisWithIdx,_messageVirtualKeepTailCount());
    const liveKey=_messageVirtualWindowKeyFor(liveWindow);
    if(!force&&liveKey===_messageVirtualWindowKey) return;
    if(_scrollbarDragActive){
      composerControlsBindings._programmaticScroll=true;
      composerControlsBindings._programmaticScrollSetAt=performance.now();
      _compensateScrollForMeasurementDelta(()=>{ renderMessages({ preserveScroll:true }); });
      _deferClearProgrammaticScroll();
      virtualStateBindings._messageVirtualWindowKey=liveKey;
      return;
    }
    virtualStateBindings._msgNodeRecycleEnabled=true;
    try{
      _compensateScrollForMeasurementDelta(()=>{ renderMessages({ preserveScroll:true }); });
    }
    finally{ virtualStateBindings._msgNodeRecycleEnabled=false; }
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

queueMicrotask(()=>registerMessageVirtualizationLifecycle({
  clearRenderCache:_clearRenderCache,
  clearUserRowIntrinsicHeightCache:_clearUserRowIntrinsicHeightCache,
  scheduleRender:_scheduleMessageVirtualizedRender,
}));

export {
  _clearUserRowIntrinsicHeightCache,
  _rememberUserRowIntrinsicHeight,
  _estimateUserRowIntrinsicHeight,
  _applyUserRowIntrinsicHeight,
  _measureMessageVirtualRow,
  _updateMessageVirtualMeasurements,
  _rememberRenderedUserRowIntrinsicHeights,
  _scheduleMessageVirtualizedRender,
  _clearRenderCache,
  _renderCacheKey,
  _getCachedRender,
  _userRowIntrinsicHeightBySessionIdx,
  _renderCache,
  _renderCacheMax,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _clearUserRowIntrinsicHeightCache: { enumerable:true, get:()=>_clearUserRowIntrinsicHeightCache, set:value=>{ _clearUserRowIntrinsicHeightCache=value; } },
  _rememberUserRowIntrinsicHeight: { enumerable:true, get:()=>_rememberUserRowIntrinsicHeight, set:value=>{ _rememberUserRowIntrinsicHeight=value; } },
  _estimateUserRowIntrinsicHeight: { enumerable:true, get:()=>_estimateUserRowIntrinsicHeight, set:value=>{ _estimateUserRowIntrinsicHeight=value; } },
  _applyUserRowIntrinsicHeight: { enumerable:true, get:()=>_applyUserRowIntrinsicHeight, set:value=>{ _applyUserRowIntrinsicHeight=value; } },
  _measureMessageVirtualRow: { enumerable:true, get:()=>_measureMessageVirtualRow, set:value=>{ _measureMessageVirtualRow=value; } },
  _updateMessageVirtualMeasurements: { enumerable:true, get:()=>_updateMessageVirtualMeasurements, set:value=>{ _updateMessageVirtualMeasurements=value; } },
  _rememberRenderedUserRowIntrinsicHeights: { enumerable:true, get:()=>_rememberRenderedUserRowIntrinsicHeights, set:value=>{ _rememberRenderedUserRowIntrinsicHeights=value; } },
  _scheduleMessageVirtualizedRender: { enumerable:true, get:()=>_scheduleMessageVirtualizedRender, set:value=>{ _scheduleMessageVirtualizedRender=value; } },
  _clearRenderCache: { enumerable:true, get:()=>_clearRenderCache, set:value=>{ _clearRenderCache=value; } },
  _renderCacheKey: { enumerable:true, get:()=>_renderCacheKey, set:value=>{ _renderCacheKey=value; } },
  _getCachedRender: { enumerable:true, get:()=>_getCachedRender, set:value=>{ _getCachedRender=value; } },
  _userRowIntrinsicHeightBySessionIdx: { enumerable:true, get:()=>_userRowIntrinsicHeightBySessionIdx },
  _renderCache: { enumerable:true, get:()=>_renderCache },
  _renderCacheMax: { enumerable:true, get:()=>_renderCacheMax },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
