import { _setMessageScrollToBottom } from './activity-and-scroll.js';
import { _isSessionEndlessScrollEnabled, _scheduleMessageVirtualizedRender, _updateSessionStartJumpButton } from './navigation.js';
import { $, _markMessageVirtualScrollActive, _scrollbarDragActive } from './state.js';
import { compatibilityBindings as stateBindings } from './state.js';

// When streaming, auto-scroll only while the user is following the live tail.
// Any manual scroll up sets a sticky unpinned flag until the user scrolls back
// to the bottom (near-bottom hysteresis on downward motion) or clicks ↓.
// Programmatic scrolls are ignored via _programmaticScroll. Fixes #1469 / #1360 / #1731.
let _scrollPinned=true;
let _programmaticScroll=false;
let _programmaticScrollSetAt=0;
let _programmaticScrollResetTimer=0;
function _deferClearProgrammaticScroll(ms){clearTimeout(_programmaticScrollResetTimer);_programmaticScrollResetTimer=setTimeout(()=>{_programmaticScroll=false;},ms||80);}
let _nearBottomCount=0;
let _lastScrollTop=null;
let _lastMessageClientHeight=null;   // #4702: track scroller height to ignore iOS portrait toolbar-settle reflows (a clientHeight increase fires a scroll event with decreased scrollTop that is NOT a user scroll)
// Sticky-unpin model (#3343 supersedes #3330's proximity re-pin): once the user
// scrolls up, streaming stops auto-following until they return to the bottom or
// click ↓. The upward-intent TIMEOUT mechanism (_lastMessageUpwardIntentMs /
// MESSAGE_UPWARD_INTENT_MS) is removed — sticky-unpin makes it unnecessary.
// Keep the non-message intent timestamp at -Infinity so load-time isn't read as
// intent (the #3330 follow-up fix); 0 would mark the first NON_MESSAGE_SCROLL_INTENT
// window after load as suppressed.
let _lastNonMessageScrollIntentMs=-Infinity;
let _messageUserUnpinned=false;
let _bottomSettleToken=0;
let _settleRAF=0;
let _settleRO=null;
let _settleTimer=0;
let _settleFinalTimer=0;
const NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS=350;
let _touchStartY=null;
let _messageTouchScrollActive=false;
let _lastMessageTouchScrollIntentMs=-Infinity;
let _deferredOlderMessagesTimer=0;
const MESSAGE_TOUCH_SCROLL_SUPPRESS_MS=1200;
// #4970 review: track recent LOW-DELTA upward message-pane wheel intent separately from
// the decisive deltaY<-30 sticky-unpin threshold. A gentle trackpad wheel
// (deltaY:-5) is real user intent but never crosses -30, so without this the
// post-render artifact suppression would swallow it for the whole window.
const MESSAGE_WHEEL_INTENT_SUPPRESS_MS=1200;
let _lastMessageWheelIntentMs=-Infinity;
let _lastMessageScrollIntentMs=-Infinity;
// #4970 review (greptile P1): keyboard scrolling of the message pane (PageUp/Down,
// arrows, Space, Home/End) fires a native `scroll` event with NO wheel/touch/
// scrollbar/non-message intent. Without recording it, a keyboard scroll-up inside
// the post-render artifact window is swallowed and live-follow snaps the reader
// back to the bottom. Stamp a generic scroll-key intent so the suppression skips it.
const MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS=1200;
let _lastMessageKeyScrollIntentMs=-Infinity;
let _newMessageCueVisible=false;
let _lastMessageRenderAt=-Infinity;
function _recentMessageRenderArtifactWindow(ms){
  return performance.now()-_lastMessageRenderAt<(ms||1400);
}
function _cancelBottomSettle(){ _bottomSettleToken++; if(_settleRO){ _settleRO.disconnect(); _settleRO=null; } clearTimeout(_settleTimer); clearTimeout(_settleFinalTimer); cancelAnimationFrame(_settleRAF); }
function _markMessageTouchScrollIntent(active=true){
  _messageTouchScrollActive=!!active;
  _lastMessageTouchScrollIntentMs=performance.now();
}
function _recentMessageTouchScrollIntent(){
  return _messageTouchScrollActive || performance.now()-_lastMessageTouchScrollIntentMs<MESSAGE_TOUCH_SCROLL_SUPPRESS_MS;
}
// #4970: true when the reader recently made ANY upward message-pane wheel
// motion, including gentle low-delta trackpad wheels below the -30 sticky-unpin
// threshold. The post-render artifact suppression must NOT fire when this is
// true, otherwise a real gentle scroll-up right after a render gets swallowed.
function _recentMessageWheelIntent(){
  return performance.now()-_lastMessageWheelIntentMs<MESSAGE_WHEEL_INTENT_SUPPRESS_MS;
}
function _recentMessageScrollIntent(){
  // This manual-reader snapshot signal intentionally excludes the raw
  // touch/key recency helpers: those also record near-tail events for render
  // artifact suppression. Only this timestamp is guarded by bottom distance.
  return performance.now()-_lastMessageScrollIntentMs<MESSAGE_WHEEL_INTENT_SUPPRESS_MS
    || (typeof _scrollbarDragActive!=='undefined'&&!!_scrollbarDragActive);
}
// #4970 review (greptile P1): true when the reader recently used the keyboard to
// scroll the message pane. Keyboard scrolls fire a native scroll event with no
// wheel/touch intent, so the post-render artifact suppression must skip them.
function _recentMessageKeyScrollIntent(){
  return performance.now()-_lastMessageKeyScrollIntentMs<MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS;
}
function _isMessageReaderUnpinned(){
  return !!_messageUserUnpinned;
}
function _olderMessagesPrefetchReady(){
  const el=document.getElementById('messages');
  if(!el) return false;
  const olderPrefetchPx=Math.max(600,el.clientHeight*1.5);
  return _isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function';
}
function _scheduleDeferredOlderMessagesLoad(){
  clearTimeout(_deferredOlderMessagesTimer);
  _deferredOlderMessagesTimer=setTimeout(()=>{
    _deferredOlderMessagesTimer=0;
    if(_recentMessageTouchScrollIntent()){
      _scheduleDeferredOlderMessagesLoad();
      return;
    }
    if(_olderMessagesPrefetchReady()) _loadOlderMessages();
  },MESSAGE_TOUCH_SCROLL_SUPPRESS_MS+50);
}
function _recordNonMessageScrollIntent(e){
  const el=document.getElementById('messages');
  const target=e&&e.target;
  if(!el||!target) return;
  if(!el.contains(target)){ _lastNonMessageScrollIntentMs=performance.now(); return; }
  // #4970: record ANY upward message-pane wheel motion as recent wheel intent,
  // including gentle low-delta trackpad wheels (e.g. deltaY:-5) that never reach
  // the decisive -30 sticky-unpin threshold below. The post-render artifact
  // suppression consults _recentMessageWheelIntent() so it cannot swallow a real
  // gentle scroll-up. This does NOT unpin on its own — only the <-30 branch and
  // the scroll listener's movedUp branch flip _messageUserUnpinned.
  if(e.type==='touchmove'||(typeof e.deltaY==='number'&&e.deltaY!==0)){
    const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
    if(bottomDistance>120) _lastMessageScrollIntentMs=performance.now();
  }
  if(typeof e.deltaY==='number'&&e.deltaY<0) _lastMessageWheelIntentMs=performance.now();
  if(e.type==='touchmove'||(typeof e.deltaY==='number'&&e.deltaY< -30)){
    _cancelBottomSettle();
    if(e.type==='touchmove') _markMessageTouchScrollIntent(true);
    if(typeof e.deltaY==='number'&&e.deltaY< -30){
      _messageUserUnpinned=true;
      _nearBottomCount=0;
      _scrollPinned=false;
    } else if(e.type==='touchmove'&&_touchStartY!==null&&e.touches&&e.touches[0]){
      // Detect upward-scroll intent on touch: dragging the finger DOWN the
      // screen scrolls the content up into earlier history (scrollTop
      // decreases) — the same "user scrolled away" signal the wheel deltaY<0
      // branch and the scroll listener's movedUp branch use. dy>0 = finger
      // moved down = reveal earlier content = unpin.
      const dy=e.touches[0].clientY-_touchStartY;
      if(dy>8){
        _messageUserUnpinned=true;
        _nearBottomCount=0;
        _scrollPinned=false;
      }
    }
  }
}
function _recentNonMessageScrollIntent(){
  return performance.now()-_lastNonMessageScrollIntentMs<NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS;
}
function _setScrollToBottomCueText(btn, textKey, labelKey){
  if(!btn) return;
  const label=btn.querySelector('.session-jump-btn__text');
  if(label){
    label.setAttribute('data-i18n',textKey);
    label.textContent=(typeof t==='function')?t(textKey):label.textContent;
  }
  btn.setAttribute('data-i18n-aria-label',labelKey);
  btn.setAttribute('data-i18n-title',labelKey);
  const accessible=(typeof t==='function')?t(labelKey):btn.getAttribute('aria-label')||'';
  if(accessible){
    btn.setAttribute('aria-label',accessible);
    btn.setAttribute('title',accessible);
  }
}
function _syncScrollToBottomCue(show, opts){
  const btn=$('scrollToBottomBtn');
  if(!btn) return;
  const newMessage=!!(opts&&opts.newMessage);
  btn.classList.toggle('scroll-to-bottom-btn--new-message',newMessage);
  if(newMessage) _setScrollToBottomCueText(btn,'session_new_message','session_new_message_label');
  else _setScrollToBottomCueText(btn,'session_jump_end','session_jump_end_label');
  btn.style.display=show?'flex':'none';
}
function _showNewMessageScrollCue(){
  _newMessageCueVisible=true;
  _syncScrollToBottomCue(true,{newMessage:true});
}
function _clearNewMessageScrollCue(){
  _newMessageCueVisible=false;
  _syncScrollToBottomCue(false,{newMessage:false});
}
function _maybeShowNewMessageScrollCue(scrollSnapshot){
  const el=document.getElementById('messages');
  if(!el||!scrollSnapshot) return;
  const previousHeight=Number(scrollSnapshot.scrollHeight)||0;
  const distance=el.scrollHeight-el.scrollTop-el.clientHeight;
  if(el.scrollHeight>previousHeight+24 && distance>80) _showNewMessageScrollCue();
  else _syncScrollToBottomCue(distance>80,{newMessage:_newMessageCueVisible});
}
if(typeof document!=='undefined'){
  document.addEventListener('wheel',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchmove',_recordNonMessageScrollIntent,{capture:true,passive:true});
  document.addEventListener('touchstart',function(e){
    const el=document.getElementById('messages');
    if(e.touches&&e.touches[0]) _touchStartY=e.touches[0].clientY;
    if(el&&e.target&&el.contains(e.target)) _markMessageTouchScrollIntent(true);
  },{capture:true,passive:true});
  document.addEventListener('touchend',function(){ _touchStartY=null; if(_messageTouchScrollActive) _markMessageTouchScrollIntent(false); },{capture:true,passive:true});
  document.addEventListener('touchcancel',function(){ _touchStartY=null; if(_messageTouchScrollActive) _markMessageTouchScrollIntent(false); },{capture:true,passive:true});
}
// Reset hook for session-switch — called from sessions.js loadSession() to
// prevent the new chat's first scroll comparing against the previous chat's
// scrollTop (Opus stage-302 SHOULD-FIX, #1731 follow-up).
function _resetScrollDirectionTracker(){
  _clearNewMessageScrollCue();
  _lastScrollTop=null;
  _lastMessageClientHeight=null;
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _touchStartY=null;
  _messageTouchScrollActive=false;
  _lastMessageTouchScrollIntentMs=-Infinity;
  // #4970 review: also clear low-delta wheel intent on session switch, else a
  // gentle wheel in the previous chat leaves _recentMessageWheelIntent() true
  // into the new chat's first post-render window — the artifact then isn't
  // suppressed, falls into movedUp, and falsely unpins live-follow.
  _lastMessageWheelIntentMs=-Infinity;
  _lastMessageScrollIntentMs=-Infinity;
  // #4970 review (greptile P1): same hygiene for keyboard scroll intent.
  _lastMessageKeyScrollIntentMs=-Infinity;
  clearTimeout(_deferredOlderMessagesTimer);
  _deferredOlderMessagesTimer=0;
}
function _resetStreamScrollFollow(){
  _clearNewMessageScrollCue();
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=0;
  _lastScrollTop=null;
  // #4970 review: clear low-delta wheel intent on fresh stream start too, else a
  // gentle upward wheel within the prior 1200ms can under-suppress a genuine
  // no-intent render artifact and silently disable live follow for the new stream.
  _lastMessageWheelIntentMs=-Infinity;
  _lastMessageScrollIntentMs=-Infinity;
  // #4970 review (greptile P1): same hygiene for keyboard scroll intent.
  _lastMessageKeyScrollIntentMs=-Infinity;
  _cancelBottomSettle();
}
if(typeof window!=='undefined'){
  window._resetScrollDirectionTracker=_resetScrollDirectionTracker;
  window._resetStreamScrollFollow=_resetStreamScrollFollow;
}
/* ── Pull-to-refresh for PWA standalone (Android) ── */
(function(){
  if(typeof document==='undefined') return;
  const isStandalone=window.navigator?.standalone||matchMedia('(display-mode:standalone),(display-mode:fullscreen)').matches;
  if(!isStandalone) return;
  const el=document.getElementById('messages');
  if(!el) return;
  let _ptrState=0; // 0=idle, 1=pulling, 2=ready
  let _ptrStartY=0;
  let _ptrCurrentY=0;
  const THRESHOLD=80;
  let _indicator=null;
  function _ptrCreateIndicator(){
    if(_indicator) return;
    _indicator=document.createElement('div');
    _indicator.className='pull-to-refresh-indicator';
    _indicator.innerHTML='<span class="ptr-icon">↓</span> <span class="ptr-text">Pull to refresh</span>';
    el.parentNode.insertBefore(_indicator,el);
  }
  function _ptrUpdate(progress){
    _ptrCreateIndicator();
    const pulling=progress<1;
    _indicator.classList.toggle('active',progress>0);
    const icon=_indicator.querySelector('.ptr-icon');
    const text=_indicator.querySelector('.ptr-text');
    if(icon) icon.classList.toggle('ready',!pulling);
    if(text) text.textContent=pulling?'Pull to refresh':'Release to refresh';
  }
  function _ptrReset(){
    _ptrState=0;
    _ptrStartY=0;
    _ptrCurrentY=0;
    if(_indicator) _indicator.classList.remove('active');
  }
  el.addEventListener('touchstart',function(e){
    if(el.scrollTop>0||_ptrState!==0) return;
    _ptrStartY=e.touches[0].clientY;
    _ptrState=1;
  },{passive:true});
  el.addEventListener('touchmove',function(e){
    if(_ptrState!==1) return;
    _ptrCurrentY=e.touches[0].clientY;
    const pull=_ptrCurrentY-_ptrStartY;
    if(pull<0){ _ptrReset(); return; }
    /* If not at the top, smooth-scroll to top first.
       Next pull gesture will trigger the refresh. */
    if(el.scrollTop>0){
      el.scrollTo({top:0,behavior:'smooth'});
      _ptrReset();
      return;
    }
    const progress=Math.min(pull/THRESHOLD,1);
    _ptrUpdate(progress);
    _ptrState=progress>=1?2:1;
    if(progress>0.3) e.preventDefault();
  },{passive:false});
  el.addEventListener('touchend',function(){
    if(_ptrState===2){
      if(typeof window.refreshSessionList==='function'){
        Promise.resolve(window.refreshSessionList('pull', {force:true, refreshActive:true})).catch(()=>{}).finally(_ptrReset);
      }else{
        window.location.reload();
      }
      return;
    }
    _ptrReset();
  },{passive:true});
  el.addEventListener('touchcancel',_ptrReset,{passive:true});
})();
(function(){
  const el=document.getElementById('messages');
  if(!el) return;
  el.addEventListener('pointerdown',(e)=>{
    if(e.target===el&&e.offsetX>=el.clientWidth) stateBindings._scrollbarDragActive=true;
  },{passive:true});
  window.addEventListener('pointerup',()=>{
    if(!_scrollbarDragActive) return;
    stateBindings._scrollbarDragActive=false;
    _scheduleMessageVirtualizedRender(true);
  },{passive:true});
  window.addEventListener('pointercancel',()=>{
    if(!_scrollbarDragActive) return;
    stateBindings._scrollbarDragActive=false;
    _scheduleMessageVirtualizedRender(true);
  },{passive:true});
  window.addEventListener('blur',()=>{ stateBindings._scrollbarDragActive=false; },{passive:true});
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='hidden') stateBindings._scrollbarDragActive=false;
  },{passive:true});
  // #4970 review (greptile P1): record keyboard-driven message-pane scrolling as
  // user intent. PageUp/PageDown, Arrow keys, Space/Shift+Space, Home/End scroll
  // the pane and fire a native scroll event with no wheel/touch intent — without
  // this stamp a keyboard scroll-up inside the post-render artifact window is
  // swallowed and live-follow snaps the reader back to the bottom. Only count it
  // when the scroll container (or a descendant) is the active/scrolling target,
  // not when typing in the composer or activating an in-transcript control.
  const _MESSAGE_SCROLL_KEYS=new Set([
    'PageUp','PageDown','ArrowUp','ArrowDown','Home','End','Spacebar',' ',
  ]);
  const _isMessageInteractiveKeyTarget=(node)=>{
    if(!node||!el.contains(node)) return false;
    if(node.tagName==='INPUT'||node.tagName==='TEXTAREA'||node.isContentEditable) return true;
    return !!(node.closest&&node.closest('button,a[href],select,summary,[role="button"],[role="tab"],[role="menuitem"],[contenteditable="true"]'));
  };
  document.addEventListener('keydown',(e)=>{
    if(!e||!_MESSAGE_SCROLL_KEYS.has(e.key)) return;
    const a=document.activeElement;
    const t=e.target;
    // Ignore keys aimed at editable fields (composer, inputs, contenteditable).
    if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.isContentEditable)) return;
    // Space/Spacebar activates focused transcript controls (buttons, role=button,
    // links, tabs) rather than scrolling. The listener is capture-phase, so target
    // handlers have not yet preventDefault()/stopPropagation()'d; inspect the
    // active/target control path directly.
    if((e.key===' '||e.key==='Spacebar')&&(_isMessageInteractiveKeyTarget(t)||_isMessageInteractiveKeyTarget(a))) return;
    // Count only when the message pane itself is the scroll target: it is focused,
    // contains the focus, or the pointer is over it (keyboard scroll w/o focus).
    if(a===el||el.contains(a)||el.matches(':hover')){
      const now=performance.now();
      _lastMessageKeyScrollIntentMs=now;
      const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
      if(bottomDistance>120) _lastMessageScrollIntentMs=now;
    }
  },{capture:true,passive:true});
  let _scrollRaf=0;
  el.addEventListener('scroll',()=>{
    _scheduleMessageVirtualizedRender();
    if(_programmaticScroll&&(performance.now()-_programmaticScrollSetAt)>150) _programmaticScroll=false;
    if(_programmaticScroll) return;
    _markMessageVirtualScrollActive();
    cancelAnimationFrame(_scrollRaf);
    _scrollRaf=requestAnimationFrame(()=>{
      const top=el.scrollTop;
      const bottomDistance=el.scrollHeight-top-el.clientHeight;
      const nearBottom=bottomDistance<250;
      // #4702: iOS Safari (esp. portrait) resolves its dynamic toolbar height
      // AFTER first paint. When the toolbar collapses the scroller GROWS
      // (clientHeight increases), which fires a scroll event with a DECREASED
      // scrollTop even though the user never scrolled. Without this guard that
      // reflow is misread as an upward scroll and falsely unpins a freshly-opened
      // session, stranding portrait readers at the top (sibling: #4701). On
      // desktop/landscape the scroller height is stable, so `grew` is always
      // false and behavior is byte-identical.
      const grew=_lastMessageClientHeight!==null&&el.clientHeight>_lastMessageClientHeight+1;
      _lastMessageClientHeight=el.clientHeight;
      const movedUp=!grew&&_lastScrollTop!==null&&top<_lastScrollTop-2;
      const movedDown=_lastScrollTop!==null&&top>_lastScrollTop+2;
      // Suppress the post-render scroll artifact: right after renderMessages()
      // rebuilds #msgInner, the browser can emit a non-user upward scroll event.
      // The typeof guards keep this branch inert in unit harnesses that inject
      // the listener body without these helpers (and short-circuit before any
      // call), while production evaluates the real intent/recency helpers.
      // #4970: also require no recent low-delta message-pane wheel intent, so a
      // gentle trackpad scroll-up (deltaY>-30) right after a render still unpins
      // instead of being swallowed for the artifact window.
      // #4970 review: and never suppress while a scrollbar drag is active — a
      // manual scrollbar-drag upward scroll inside the window is real intent.
      // typeof guard keeps the #4295 node harness (no _scrollbarDragActive
      // injected) inert via short-circuit.
      // #4970 review (greptile P1): likewise skip suppression when the reader
      // recently scrolled the pane with the keyboard — a keyboard scroll-up is
      // real intent that produces a native scroll event with no wheel/touch.
      if(movedUp
        && typeof _recentMessageRenderArtifactWindow==='function'
        && typeof _recentMessageTouchScrollIntent==='function'
        && typeof _recentNonMessageScrollIntent==='function'
        && typeof _recentMessageWheelIntent==='function'
        && typeof _recentMessageKeyScrollIntent==='function'
        && (typeof _scrollbarDragActive==='undefined' || !_scrollbarDragActive)
        && _recentMessageRenderArtifactWindow(1400)
        && !_recentMessageTouchScrollIntent()
        && !_recentNonMessageScrollIntent()
        && !_recentMessageWheelIntent()
        && !_recentMessageKeyScrollIntent()){
        _lastScrollTop=top;
        return;
      }
      _lastScrollTop=top;
      if(movedUp){
        _cancelBottomSettle();
        _nearBottomCount=0;
        _scrollPinned=false;
        _messageUserUnpinned=true;
      }else if(movedDown&&nearBottom){
        _nearBottomCount=_nearBottomCount+1;
        if(_nearBottomCount>=2){
          // Only re-pin when the reader has genuinely reached the true bottom
          // tail (<=80px). nearBottom spans a ~250px band, so proximity alone
          // must NOT clear the sticky unpin flag (#4295) — a reader scanning the
          // last lines mid-stream would otherwise get yanked back to the bottom.
          if(!_messageUserUnpinned||bottomDistance<=80){
            _messageUserUnpinned=false;
            _scrollPinned=true;
          }
          _nearBottomCount=0;
        }
      }else if(!_messageUserUnpinned){
        if(nearBottom){
          _nearBottomCount=_nearBottomCount+1;
          if(_nearBottomCount>=2){_scrollPinned=true;_nearBottomCount=0;}
        }else if(!movedUp && _autoScrollFollow && _scrollPinned){
          // Content-grew-beneath-a-pinned-viewport case (NOT a user scroll-away).
          // During streaming on a tall transcript (esp. mobile, where chunks land
          // fast), new content increases scrollHeight under a stationary viewport,
          // so bottomDistance crosses the nearBottom threshold even though the
          // reader never scrolled (top did NOT move up, _messageUserUnpinned is
          // false). Previously this fell through to `_scrollPinned=false`, killing
          // auto-follow mid-stream: the follow writer and this listener then fought
          // frame-by-frame, the viewport stalled while content kept growing, and it
          // was progressively stranded mid-transcript (the "jump back" report).
          // Keep the pin and re-snap to the true bottom instead of unpinning.
          _nearBottomCount=0;
          if(typeof _setMessageScrollToBottom==='function') _setMessageScrollToBottom();
        }else{
          _nearBottomCount=0;
          _scrollPinned=false;
        }
      }else if(!nearBottom){
        _nearBottomCount=0;
        _scrollPinned=false;
      }
      if(nearBottom) _clearNewMessageScrollCue();
      const showBottomButton=!_scrollPinned && el.scrollHeight-top-el.clientHeight>80;
      _syncScrollToBottomCue(showBottomButton,{newMessage:_newMessageCueVisible});
      if(typeof _updateSessionStartJumpButton==='function') _updateSessionStartJumpButton();
      // Prefetch older messages before the reader hits the hard top. Prepending
      // then preserving scrollTop is seamless only if there is runway left for
      // the user's continued upward wheel/touch movement.
      const olderPrefetchPx=Math.max(600,el.clientHeight*1.5);
      if(_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx && typeof _messagesTruncated!=='undefined' && _messagesTruncated && typeof _loadOlderMessages==='function'){
        if(_recentMessageTouchScrollIntent()) _scheduleDeferredOlderMessagesLoad();
        else _loadOlderMessages();
      }
    });
  });
})();

export {
  _deferClearProgrammaticScroll,
  _recentMessageRenderArtifactWindow,
  _cancelBottomSettle,
  _markMessageTouchScrollIntent,
  _recentMessageTouchScrollIntent,
  _recentMessageWheelIntent,
  _recentMessageScrollIntent,
  _recentMessageKeyScrollIntent,
  _isMessageReaderUnpinned,
  _olderMessagesPrefetchReady,
  _scheduleDeferredOlderMessagesLoad,
  _recordNonMessageScrollIntent,
  _recentNonMessageScrollIntent,
  _setScrollToBottomCueText,
  _syncScrollToBottomCue,
  _showNewMessageScrollCue,
  _clearNewMessageScrollCue,
  _maybeShowNewMessageScrollCue,
  _resetScrollDirectionTracker,
  _resetStreamScrollFollow,
  NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS,
  MESSAGE_TOUCH_SCROLL_SUPPRESS_MS,
  MESSAGE_WHEEL_INTENT_SUPPRESS_MS,
  MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS,
  _scrollPinned,
  _programmaticScroll,
  _programmaticScrollSetAt,
  _programmaticScrollResetTimer,
  _nearBottomCount,
  _lastScrollTop,
  _lastMessageClientHeight,
  _lastNonMessageScrollIntentMs,
  _messageUserUnpinned,
  _bottomSettleToken,
  _settleRAF,
  _settleRO,
  _settleTimer,
  _settleFinalTimer,
  _touchStartY,
  _messageTouchScrollActive,
  _lastMessageTouchScrollIntentMs,
  _deferredOlderMessagesTimer,
  _lastMessageWheelIntentMs,
  _lastMessageScrollIntentMs,
  _lastMessageKeyScrollIntentMs,
  _newMessageCueVisible,
  _lastMessageRenderAt,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _deferClearProgrammaticScroll: { enumerable: true, get: () => _deferClearProgrammaticScroll, set: (value) => { _deferClearProgrammaticScroll = value; } },
  _recentMessageRenderArtifactWindow: { enumerable: true, get: () => _recentMessageRenderArtifactWindow, set: (value) => { _recentMessageRenderArtifactWindow = value; } },
  _cancelBottomSettle: { enumerable: true, get: () => _cancelBottomSettle, set: (value) => { _cancelBottomSettle = value; } },
  _markMessageTouchScrollIntent: { enumerable: true, get: () => _markMessageTouchScrollIntent, set: (value) => { _markMessageTouchScrollIntent = value; } },
  _recentMessageTouchScrollIntent: { enumerable: true, get: () => _recentMessageTouchScrollIntent, set: (value) => { _recentMessageTouchScrollIntent = value; } },
  _recentMessageWheelIntent: { enumerable: true, get: () => _recentMessageWheelIntent, set: (value) => { _recentMessageWheelIntent = value; } },
  _recentMessageScrollIntent: { enumerable: true, get: () => _recentMessageScrollIntent, set: (value) => { _recentMessageScrollIntent = value; } },
  _recentMessageKeyScrollIntent: { enumerable: true, get: () => _recentMessageKeyScrollIntent, set: (value) => { _recentMessageKeyScrollIntent = value; } },
  _isMessageReaderUnpinned: { enumerable: true, get: () => _isMessageReaderUnpinned, set: (value) => { _isMessageReaderUnpinned = value; } },
  _olderMessagesPrefetchReady: { enumerable: true, get: () => _olderMessagesPrefetchReady, set: (value) => { _olderMessagesPrefetchReady = value; } },
  _scheduleDeferredOlderMessagesLoad: { enumerable: true, get: () => _scheduleDeferredOlderMessagesLoad, set: (value) => { _scheduleDeferredOlderMessagesLoad = value; } },
  _recordNonMessageScrollIntent: { enumerable: true, get: () => _recordNonMessageScrollIntent, set: (value) => { _recordNonMessageScrollIntent = value; } },
  _recentNonMessageScrollIntent: { enumerable: true, get: () => _recentNonMessageScrollIntent, set: (value) => { _recentNonMessageScrollIntent = value; } },
  _setScrollToBottomCueText: { enumerable: true, get: () => _setScrollToBottomCueText, set: (value) => { _setScrollToBottomCueText = value; } },
  _syncScrollToBottomCue: { enumerable: true, get: () => _syncScrollToBottomCue, set: (value) => { _syncScrollToBottomCue = value; } },
  _showNewMessageScrollCue: { enumerable: true, get: () => _showNewMessageScrollCue, set: (value) => { _showNewMessageScrollCue = value; } },
  _clearNewMessageScrollCue: { enumerable: true, get: () => _clearNewMessageScrollCue, set: (value) => { _clearNewMessageScrollCue = value; } },
  _maybeShowNewMessageScrollCue: { enumerable: true, get: () => _maybeShowNewMessageScrollCue, set: (value) => { _maybeShowNewMessageScrollCue = value; } },
  _resetScrollDirectionTracker: { enumerable: true, get: () => _resetScrollDirectionTracker, set: (value) => { _resetScrollDirectionTracker = value; } },
  _resetStreamScrollFollow: { enumerable: true, get: () => _resetStreamScrollFollow, set: (value) => { _resetStreamScrollFollow = value; } },
  NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS: { enumerable: true, get: () => NON_MESSAGE_SCROLL_INTENT_SUPPRESS_MS },
  MESSAGE_TOUCH_SCROLL_SUPPRESS_MS: { enumerable: true, get: () => MESSAGE_TOUCH_SCROLL_SUPPRESS_MS },
  MESSAGE_WHEEL_INTENT_SUPPRESS_MS: { enumerable: true, get: () => MESSAGE_WHEEL_INTENT_SUPPRESS_MS },
  MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS: { enumerable: true, get: () => MESSAGE_KEY_SCROLL_INTENT_SUPPRESS_MS },
  _scrollPinned: { enumerable: true, get: () => _scrollPinned, set: (value) => { _scrollPinned = value; } },
  _programmaticScroll: { enumerable: true, get: () => _programmaticScroll, set: (value) => { _programmaticScroll = value; } },
  _programmaticScrollSetAt: { enumerable: true, get: () => _programmaticScrollSetAt, set: (value) => { _programmaticScrollSetAt = value; } },
  _programmaticScrollResetTimer: { enumerable: true, get: () => _programmaticScrollResetTimer, set: (value) => { _programmaticScrollResetTimer = value; } },
  _nearBottomCount: { enumerable: true, get: () => _nearBottomCount, set: (value) => { _nearBottomCount = value; } },
  _lastScrollTop: { enumerable: true, get: () => _lastScrollTop, set: (value) => { _lastScrollTop = value; } },
  _lastMessageClientHeight: { enumerable: true, get: () => _lastMessageClientHeight, set: (value) => { _lastMessageClientHeight = value; } },
  _lastNonMessageScrollIntentMs: { enumerable: true, get: () => _lastNonMessageScrollIntentMs, set: (value) => { _lastNonMessageScrollIntentMs = value; } },
  _messageUserUnpinned: { enumerable: true, get: () => _messageUserUnpinned, set: (value) => { _messageUserUnpinned = value; } },
  _bottomSettleToken: { enumerable: true, get: () => _bottomSettleToken, set: (value) => { _bottomSettleToken = value; } },
  _settleRAF: { enumerable: true, get: () => _settleRAF, set: (value) => { _settleRAF = value; } },
  _settleRO: { enumerable: true, get: () => _settleRO, set: (value) => { _settleRO = value; } },
  _settleTimer: { enumerable: true, get: () => _settleTimer, set: (value) => { _settleTimer = value; } },
  _settleFinalTimer: { enumerable: true, get: () => _settleFinalTimer, set: (value) => { _settleFinalTimer = value; } },
  _touchStartY: { enumerable: true, get: () => _touchStartY, set: (value) => { _touchStartY = value; } },
  _messageTouchScrollActive: { enumerable: true, get: () => _messageTouchScrollActive, set: (value) => { _messageTouchScrollActive = value; } },
  _lastMessageTouchScrollIntentMs: { enumerable: true, get: () => _lastMessageTouchScrollIntentMs, set: (value) => { _lastMessageTouchScrollIntentMs = value; } },
  _deferredOlderMessagesTimer: { enumerable: true, get: () => _deferredOlderMessagesTimer, set: (value) => { _deferredOlderMessagesTimer = value; } },
  _lastMessageWheelIntentMs: { enumerable: true, get: () => _lastMessageWheelIntentMs, set: (value) => { _lastMessageWheelIntentMs = value; } },
  _lastMessageScrollIntentMs: { enumerable: true, get: () => _lastMessageScrollIntentMs, set: (value) => { _lastMessageScrollIntentMs = value; } },
  _lastMessageKeyScrollIntentMs: { enumerable: true, get: () => _lastMessageKeyScrollIntentMs, set: (value) => { _lastMessageKeyScrollIntentMs = value; } },
  _newMessageCueVisible: { enumerable: true, get: () => _newMessageCueVisible, set: (value) => { _newMessageCueVisible = value; } },
  _lastMessageRenderAt: { enumerable: true, get: () => _lastMessageRenderAt, set: (value) => { _lastMessageRenderAt = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
