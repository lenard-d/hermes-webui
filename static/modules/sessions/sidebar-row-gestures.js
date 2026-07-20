import { SESSION_ARCHIVE_SWIPE_THRESHOLD_PX, SESSION_DELETE_SWIPE_THRESHOLD_PX, SESSION_LONG_PRESS_DELAY_MS, SESSION_SWIPE_CANCEL_RATIO } from './session-list-coordination.js';
import { sessionLoadState } from './session-load-state.js';
import { _isCliSession, _isMessagingSession } from './session-source.js';
import { _openSidebarSession } from './sidebar-session-opening.js';
import { _openSessionActionMenu } from './sidebar-actions.js';
import { _archiveSession } from './session-archive-actions.js';
import { closeSessionActionMenu } from './session-action-menu.js';
import { _waitForSessionMotion } from './sidebar-motion.js';
import { toggleSessionSelect } from './sidebar-selection.js';
import { sidebarStateBindings } from './sidebar-store.js';
import { sessionSearchBindings } from './session-search.js';
import { deleteSession } from './session-removal.js';

function _installForkChildSwipe(rowEl, childSession, actionsEl, committedSwipeDuration, committedSwipeReflowDelay){
  let _pointerDownX=0;
  let _pointerDownY=0;
  let _pointerX=0;
  let _pointerY=0;
  let _gestureState='idle';
  let _swipeTracking=false;
  let _gesturePointerType='';
  let _clearDragTimer=null;
  let _longPressTimer=null;
  let _longPressMenuOpened=false;
  const _isForkSwipeTarget=()=>_gesturePointerType!=='mouse'&&!sidebarStateBindings._sessionSelectMode;
  const _isForkActionTarget=(target)=>!!(actionsEl&&target&&actionsEl.contains(target));
  const _clearForkLongPressTimer=()=>{
    if(_longPressTimer){clearTimeout(_longPressTimer);_longPressTimer=null;}
    if(!_longPressMenuOpened) rowEl.classList.remove('long-pressing');
  };
  const _beginForkGesture=(clientX,clientY,pointerType='')=>{
    _gesturePointerType=pointerType;
    _pointerDownX=clientX;
    _pointerDownY=clientY;
    _pointerX=clientX;
    _pointerY=clientY;
    _gestureState='pressing';
    _swipeTracking=false;
    _longPressMenuOpened=false;
    if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
    rowEl.classList.remove('dragging','swipe-committed','swipe-removing');
    rowEl.style.removeProperty('height');
    rowEl.style.removeProperty('min-height');
  };
  const _scheduleForkLongPressMenu=()=>{
    _clearForkLongPressTimer();
    rowEl.classList.add('long-pressing');
    _longPressTimer=setTimeout(()=>{
      if(_gestureState!=='pressing'||sidebarStateBindings._renamingSid||sidebarStateBindings._sessionSelectMode) return;
      _longPressMenuOpened=true;
      rowEl._skipNextChildOpen=true;
      _openSessionActionMenu(childSession, rowEl);
    },SESSION_LONG_PRESS_DELAY_MS);
  };
  const _paintForkSwipe=(signedDx)=>{
    const rawOffset=signedDx*.55;
    const revealedOffset=Math.max(-72,Math.min(72,rawOffset));
    const overshoot=Math.max(0,Math.abs(rawOffset)-72);
    const offset=Math.sign(rawOffset)*(Math.abs(revealedOffset)+Math.sqrt(overshoot)*5);
    const progress=Math.min(1,Math.abs(revealedOffset)/72);
    const reveal=Math.abs(offset);
    const actionRevealScale=1.15;
    const iconScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
    const badgeSize=34*iconScale;
    const iconSize=18*iconScale;
    const labelScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
    const actionOpacity=Math.min(1,Math.max(.01,progress*actionRevealScale));
    const actionInset=6;
    const tileGap=6;
    const stretchStart=72/actionRevealScale;
    const stretchProgress=Math.max(0,reveal-stretchStart);
    const badgeStretch=Math.min(Math.max(0,reveal-34),stretchProgress*1.15,Math.max(0,reveal-badgeSize-actionInset-tileGap));
    rowEl.style.setProperty('--session-swipe-offset',offset+'px');
    rowEl.style.setProperty('--session-swipe-reveal',reveal+'px');
    rowEl.style.setProperty('--session-swipe-badge-size',badgeSize+'px');
    rowEl.style.setProperty('--session-swipe-icon-size',iconSize+'px');
    rowEl.style.setProperty('--session-swipe-label-scale',labelScale);
    rowEl.style.setProperty('--session-swipe-badge-stretch',badgeStretch+'px');
    rowEl.style.setProperty('--session-swipe-progress',actionOpacity);
    rowEl.classList.toggle('swiping-right',offset>0);
    rowEl.classList.toggle('swiping-left',offset<0);
  };
  const _clearForkSwipePaint=()=>{
    rowEl.style.removeProperty('--session-swipe-offset');
    rowEl.style.removeProperty('--session-swipe-reveal');
    rowEl.style.removeProperty('--session-swipe-badge-size');
    rowEl.style.removeProperty('--session-swipe-icon-size');
    rowEl.style.removeProperty('--session-swipe-label-scale');
    rowEl.style.removeProperty('--session-swipe-badge-stretch');
    rowEl.style.removeProperty('--session-swipe-progress');
    rowEl.style.removeProperty('height');
    rowEl.style.removeProperty('min-height');
    rowEl.classList.remove('swiping-right','swiping-left','swipe-committed','swipe-removing');
  };
  const _settleForkSwipePaint=()=>{
    rowEl.classList.remove('dragging');
    requestAnimationFrame(()=>requestAnimationFrame(_clearForkSwipePaint));
  };
  const _completeForkSwipePaint=(signedDx)=>{
    rowEl.classList.remove('dragging');
    rowEl.classList.add('swipe-committed');
    rowEl.style.setProperty('--session-swipe-progress','0');
    rowEl.style.setProperty('--session-swipe-offset',(signedDx>0?1:-1)*window.innerWidth+'px');
    const rect=rowEl.getBoundingClientRect();
    rowEl.style.height=rect.height+'px';
    rowEl.style.minHeight=rect.height+'px';
    requestAnimationFrame(()=>rowEl.classList.add('swipe-removing'));
  };
  const _canSwipeDeleteFork=()=>_isForkSwipeTarget()&&!_isMessagingSession(childSession)&&!_isCliSession(childSession);
  const _handleForkSwipe=(signedDx,signedDy)=>{
    if(_gestureState==='committed'||!_isForkSwipeTarget()) return false;
    const actionThreshold=signedDx>0?SESSION_ARCHIVE_SWIPE_THRESHOLD_PX:SESSION_DELETE_SWIPE_THRESHOLD_PX;
    if(Math.abs(signedDx)<actionThreshold) return false;
    if(Math.abs(signedDy)>Math.abs(signedDx)*SESSION_SWIPE_CANCEL_RATIO) return false;
    _gestureState='committed';
    closeSessionActionMenu();
    if(signedDx>0){
      if(childSession.archived){
        _settleForkSwipePaint();
        _archiveSession(childSession,false,()=>_waitForSessionMotion(committedSwipeDuration)).then((restored)=>{
          if(!restored) _settleForkSwipePaint();
        });
      }else if(sidebarStateBindings._showArchived){
        _settleForkSwipePaint();
        _archiveSession(childSession,true,()=>_waitForSessionMotion(committedSwipeDuration)).then((archived)=>{
          if(!archived) _settleForkSwipePaint();
        });
      }else{
        _completeForkSwipePaint(signedDx);
        _archiveSession(childSession,true,()=>_waitForSessionMotion(committedSwipeReflowDelay)).then((archived)=>{
          if(!archived) _settleForkSwipePaint();
        });
      }
    }else if(_canSwipeDeleteFork()){
      rowEl.classList.remove('dragging');
      deleteSession(childSession.session_id,async()=>{
        _completeForkSwipePaint(signedDx);
        await _waitForSessionMotion(committedSwipeReflowDelay);
      }).then((deleted)=>{
        if(!deleted) _settleForkSwipePaint();
      });
    }else if(typeof showToast==='function'){
      showToast('Imported sessions cannot be deleted here.',3000);
      _gestureState='dragging';
      _settleForkSwipePaint();
    }
    return true;
  };
  const _clearForkPointerState=()=>{
    _clearForkLongPressTimer();
    const wasDragging=_gestureState==='dragging'||_swipeTracking;
    _gestureState='idle';
    if(wasDragging){
      if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
      _clearDragTimer=setTimeout(()=>{_settleForkSwipePaint();_clearDragTimer=null;},50);
    }
  };
  rowEl.onpointerdown=(e)=>{
    if(e.pointerType==='mouse'||e.button!==0||_isForkActionTarget(e.target)) return;
    _beginForkGesture(e.clientX,e.clientY,e.pointerType||'');
    if(e.pointerType==='touch'||e.pointerType==='pen') _scheduleForkLongPressMenu();
  };
  rowEl.onpointermove=(e)=>{
    if(e.pointerType==='mouse'||_gestureState==='idle') return;
    _pointerX=e.clientX;
    _pointerY=e.clientY;
    const signedDx=e.clientX-_pointerDownX;
    const signedDy=e.clientY-_pointerDownY;
    const dx=Math.abs(signedDx);
    const dy=Math.abs(signedDy);
    if(dx>8&&dx>dy*1.1) _swipeTracking=true;
    if(_gestureState==='pressing'&&(dx>5||dy>5)){
      _clearForkLongPressTimer();
      _gestureState='dragging';
      rowEl.classList.add('dragging');
    }
    if(_isForkSwipeTarget()&&(_swipeTracking||dx>dy)) _paintForkSwipe(signedDx);
  };
  rowEl.onpointerup=(e)=>{
    if(e.pointerType==='mouse'||e.button!==0) return;
    if(_gestureState==='idle') return;
    if(_longPressMenuOpened){_gestureState='idle';return;}
    if(_isForkActionTarget(e.target)){_gestureState='idle';return;}
    _pointerX=e.clientX;
    _pointerY=e.clientY;
    if(_handleForkSwipe(_pointerX-_pointerDownX,_pointerY-_pointerDownY)){
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    _clearForkPointerState();
  };
  rowEl.onpointercancel=()=>_clearForkPointerState();
  rowEl.onpointerleave=()=>{
    if(_gesturePointerType!=='mouse'&&_gestureState!=='idle') _clearForkPointerState();
  };
}

function _installSessionRowGestures(el, s, actions, readOnly, committedSwipeDuration, committedSwipeReflowDelay, startRename){
let _lastTapTime=0;
let _tapTimer=null;
let _pointerDownX=0;
let _pointerDownY=0;
let _gestureState='idle'; // idle | pressing | dragging | committed
let _clearDragTimer=null;
let _longPressTimer=null;
let _longPressMenuOpened=false;
let _swipeTracking=false;
let _pointerX=0;
let _pointerY=0;
let _gesturePointerType='';
const _clearLongPressTimer=()=>{
  if(_longPressTimer){clearTimeout(_longPressTimer);_longPressTimer=null;}
  if(!_longPressMenuOpened) el.classList.remove('long-pressing');
};
const _beginSessionGesture=(clientX,clientY,pointerType='')=>{
  _gesturePointerType=pointerType;
  _pointerDownX=clientX;
  _pointerDownY=clientY;
  _pointerX=clientX;
  _pointerY=clientY;
  _gestureState='pressing';
  _swipeTracking=false;
  _longPressMenuOpened=false;
  if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
  el.classList.remove('dragging','swipe-committed','swipe-removing');
  el.style.removeProperty('height');
  el.style.removeProperty('min-height');
};
const _scheduleSessionLongPressMenu=()=>{
  _clearLongPressTimer();
  el.classList.add('long-pressing');
  _longPressTimer=setTimeout(()=>{
    if(_gestureState!=='pressing'||sidebarStateBindings._renamingSid||sidebarStateBindings._sessionSelectMode||readOnly) return;
    _longPressMenuOpened=true;
    clearTimeout(_tapTimer);
    _tapTimer=null;
    _lastTapTime=0;
    _openSessionActionMenu(s, el);
  },SESSION_LONG_PRESS_DELAY_MS);
};
const _isSessionSwipeTarget=()=>{
  return _gesturePointerType!=='mouse'&&!readOnly&&!sidebarStateBindings._renamingSid&&!sidebarStateBindings._sessionSelectMode;
};
const _isSessionActionTarget=(target)=>{
  return !!(actions&&target&&actions.contains(target));
};
const _trackHorizontalSwipe=(dx,dy)=>{
  if(dx>8&&dx>dy*1.1) _swipeTracking=true;
};
const _promoteSessionDrag=(dx,dy)=>{
  if(_gestureState!=='pressing'||(dx<=5&&dy<=5)) return;
  if(dy>8||dx>10) _clearLongPressTimer();
  _gestureState='dragging';
  el.classList.add('dragging');
  if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
};
const _updateSessionGesture=(clientX,clientY)=>{
  if(_gestureState==='idle') return false;
  _pointerX=clientX;
  _pointerY=clientY;
  const signedDx=clientX-_pointerDownX;
  const signedDy=clientY-_pointerDownY;
  const dx=Math.abs(signedDx);
  const dy=Math.abs(signedDy);
  _promoteSessionDrag(dx,dy);
  _trackHorizontalSwipe(dx,dy);
  if(_isSessionSwipeTarget()&&(_swipeTracking||dx>dy)) _paintSessionSwipe(signedDx);
  return _swipeTracking;
};
const _canSwipeDeleteSession=()=>{
  return _isSessionSwipeTarget()&&!_isMessagingSession(s)&&!_isCliSession(s);
};
const _paintSessionSwipe=(signedDx)=>{
  const rawOffset=signedDx*.55;
  const revealedOffset=Math.max(-72,Math.min(72,rawOffset));
  const overshoot=Math.max(0,Math.abs(rawOffset)-72);
  const offset=Math.sign(rawOffset)*(Math.abs(revealedOffset)+Math.sqrt(overshoot)*5);
  const progress=Math.min(1,Math.abs(revealedOffset)/72);
  const reveal=Math.abs(offset);
  const actionRevealScale=1.15;
  const iconScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
  const badgeSize=34*iconScale;
  const iconSize=18*iconScale;
  const labelScale=Math.min(1,Math.max(.01,progress*actionRevealScale));
  const actionOpacity=Math.min(1,Math.max(.01,progress*actionRevealScale));
  const actionInset=6;
  const tileGap=6;
  const stretchStart=72/actionRevealScale;
  const stretchProgress=Math.max(0,reveal-stretchStart);
  const badgeStretch=Math.min(Math.max(0,reveal-34),stretchProgress*1.15,Math.max(0,reveal-badgeSize-actionInset-tileGap));
  el.style.setProperty('--session-swipe-offset',offset+'px');
  el.style.setProperty('--session-swipe-reveal',reveal+'px');
  el.style.setProperty('--session-swipe-badge-size',badgeSize+'px');
  el.style.setProperty('--session-swipe-icon-size',iconSize+'px');
  el.style.setProperty('--session-swipe-label-scale',labelScale);
  el.style.setProperty('--session-swipe-badge-stretch',badgeStretch+'px');
  el.style.setProperty('--session-swipe-progress',actionOpacity);
  el.classList.toggle('swiping-right',offset>0);
  el.classList.toggle('swiping-left',offset<0);
};
const _clearSessionSwipePaint=()=>{
  el.style.removeProperty('--session-swipe-offset');
  el.style.removeProperty('--session-swipe-reveal');
  el.style.removeProperty('--session-swipe-badge-size');
  el.style.removeProperty('--session-swipe-icon-size');
  el.style.removeProperty('--session-swipe-label-scale');
  el.style.removeProperty('--session-swipe-badge-stretch');
  el.style.removeProperty('--session-swipe-progress');
  el.style.removeProperty('height');
  el.style.removeProperty('min-height');
  el.classList.remove('swiping-right','swiping-left','swipe-committed','swipe-removing');
};
const _settleSessionSwipePaint=()=>{
  el.classList.remove('dragging');
  requestAnimationFrame(()=>requestAnimationFrame(_clearSessionSwipePaint));
};
const _completeSessionSwipePaint=(signedDx)=>{
  el.classList.remove('dragging');
  el.classList.add('swipe-committed');
  el.style.setProperty('--session-swipe-progress','0');
  el.style.setProperty('--session-swipe-offset',(signedDx>0?1:-1)*window.innerWidth+'px');
  const rect=el.getBoundingClientRect();
  el.style.height=rect.height+'px';
  el.style.minHeight=rect.height+'px';
  requestAnimationFrame(()=>el.classList.add('swipe-removing'));
};
const _handleSessionSwipe=(signedDx,signedDy)=>{
  if(_gestureState==='committed'||!_isSessionSwipeTarget()) return false;
  const actionThreshold=signedDx>0?SESSION_ARCHIVE_SWIPE_THRESHOLD_PX:SESSION_DELETE_SWIPE_THRESHOLD_PX;
  if(Math.abs(signedDx)<actionThreshold) return false;
  if(Math.abs(signedDy)>Math.abs(signedDx)*SESSION_SWIPE_CANCEL_RATIO) return false;
  _gestureState='committed';
  _clearLongPressTimer();
  clearTimeout(_tapTimer);
  _tapTimer=null;
  _lastTapTime=0;
  if(signedDx>0){
    if(s.archived){
      _settleSessionSwipePaint();
      _archiveSession(s,false,()=>_waitForSessionMotion(committedSwipeDuration)).then((restored)=>{
        if(!restored) _settleSessionSwipePaint();
      });
    }else if(sidebarStateBindings._showArchived){
      _settleSessionSwipePaint();
      _archiveSession(s,true,()=>_waitForSessionMotion(committedSwipeDuration)).then((archived)=>{
        if(!archived) _settleSessionSwipePaint();
      });
    }else{
      _completeSessionSwipePaint(signedDx);
      _archiveSession(s,true,()=>_waitForSessionMotion(committedSwipeReflowDelay)).then((archived)=>{
        if(!archived) _settleSessionSwipePaint();
      });
    }
  }else if(_canSwipeDeleteSession()){
    el.classList.remove('dragging');
    deleteSession(s.session_id,async()=>{
      _completeSessionSwipePaint(signedDx);
      await _waitForSessionMotion(committedSwipeReflowDelay);
    }).then((deleted)=>{
      if(!deleted) _settleSessionSwipePaint();
    });
  }else if(typeof showToast==='function'){
    showToast('Imported sessions cannot be deleted here.',3000);
    _gestureState='dragging';
    _settleSessionSwipePaint();
  }
  return true;
};
const _commitSessionSwipe=()=>{
  return _handleSessionSwipe(_pointerX-_pointerDownX,_pointerY-_pointerDownY);
};
const _clearPointerDragState=()=>{
  if(_gestureState==='committed'){
    _clearLongPressTimer();
    return;
  }
  const wasDragging=_gestureState==='dragging'||_swipeTracking;
  _gestureState='idle';
  _clearLongPressTimer();
  if(wasDragging){
    if(_clearDragTimer){clearTimeout(_clearDragTimer);_clearDragTimer=null;}
    _clearDragTimer=setTimeout(()=>{_settleSessionSwipePaint();_clearDragTimer=null;},50);
  }
};
const _finishSessionGesture=(clientX,clientY,target,pointerType)=>{
  if(_gestureState==='idle') return false;  // press never began on this row
  const wasDragging=_gestureState==='dragging'||_swipeTracking;
  _clearLongPressTimer();
  if(sidebarStateBindings._renamingSid){_gestureState='idle';return false;}
  if(_isSessionActionTarget(target)){_gestureState='idle';return false;}
  _pointerX=clientX;
  _pointerY=clientY;
  _commitSessionSwipe();
  if(_longPressMenuOpened){_gestureState='idle';return true;}
  if(_gestureState==='committed') return true;
  if(sidebarStateBindings._sessionActionMenu&&!sidebarStateBindings._sessionActionMenu.contains(target)){
    closeSessionActionMenu();
    return true;
  }
  if(target&&target.closest&&target.closest('.session-child-count,.session-child-sessions,.session-child-session,.session-lineage-count,.session-lineage-segments,.session-lineage-segment')) return false;
  if(sidebarStateBindings._sessionSelectMode){if(!readOnly)toggleSessionSelect(s.session_id);return true;}
  if(wasDragging){
    clearTimeout(_tapTimer);_tapTimer=null;_lastTapTime=0;
    _gestureState='idle';
    _clearDragTimer=setTimeout(()=>{_settleSessionSwipePaint();_clearDragTimer=null;},50);
    return false;
  }
  _gestureState='idle';
  const now=Date.now();
  if(now-_lastTapTime<350){
    clearTimeout(_tapTimer);
    _tapTimer=null;
    _lastTapTime=0;
    el.classList.remove('loading');
    startRename();
    return false;
  }
  _lastTapTime=now;
  clearTimeout(_tapTimer);
  const delay=pointerType==='mouse'?0:300;
  if(pointerType!=='mouse') el.classList.add('loading');
  _tapTimer=setTimeout(async()=>{
    _tapTimer=null;
    _lastTapTime=0;
    if(sidebarStateBindings._renamingSid) return;
    try{
      if(($('sessionSearch').value||'').trim()) sessionSearchBindings._hideSearchPreviewsAfterSelect=true;
      await _openSidebarSession(s);
    }finally{
      el.classList.remove('loading');
    }
  }, delay);
  return false;
};
el.onpointerdown=(e)=>{
  if(e.pointerType==='touch') return;
  if(e.pointerType==='mouse' && e.button!==0) return;
  if(_isSessionActionTarget(e.target)) return;
  _beginSessionGesture(e.clientX,e.clientY,e.pointerType||'');
  if(e.pointerType==='pen'){
    _scheduleSessionLongPressMenu();
  }
};
el.onpointermove=(e)=>{
  if(e.pointerType==='touch') return;
  // Plain hover also dispatches pointermove. Only mark a row as dragging
  // after an actual press starts on this row; otherwise hovered rows stay
  // faded until the next sidebar rerender clears their DOM nodes.
  _updateSessionGesture(e.clientX,e.clientY);
};
el.onpointercancel=(e)=>{
  if(e.pointerType==='touch') return;
  _clearPointerDragState();
};
el.onpointerleave=()=>{
  if(_gesturePointerType==='mouse'&&_gestureState!=='idle') _clearPointerDragState();
};
el.onpointerup=(e)=>{
  if(e.pointerType==='touch') return;
  if(e.pointerType==='mouse' && e.button!==0) return;  // ignore right/middle click
  if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType)) e.stopPropagation();
};
// Add ondblclick for more reliable double-click detection
el.ondblclick=(e)=>{
  if(e.pointerType==='mouse' && e.button!==0) return;
  if(sidebarStateBindings._renamingSid) return;
  if(actions&&actions.contains(e.target)) return;
  if(sidebarStateBindings._sessionSelectMode){e.stopPropagation();if(!readOnly)toggleSessionSelect(s.session_id);return;}
  // Guard: prevent renaming if session is currently being loaded
  if (sessionLoadState.loadingSessionId && sessionLoadState.loadingSessionId !== s.session_id) return;
  startRename();
};
el.addEventListener('touchstart',(e)=>{
  if(_isSessionActionTarget(e.target)) return;
  const touch=e.changedTouches&&e.changedTouches[0];
  if(!touch) return;
  _beginSessionGesture(touch.clientX,touch.clientY,'touch');
  _scheduleSessionLongPressMenu();
},{passive:true});
el.addEventListener('touchmove',(e)=>{
  const touch=e.changedTouches&&e.changedTouches[0];
  if(!touch) return;
  if(_updateSessionGesture(touch.clientX,touch.clientY)) e.preventDefault();
},{passive:false});
el.addEventListener('touchcancel',_clearPointerDragState,{passive:true});
el.addEventListener('touchend',(e)=>{
  const touch=e.changedTouches&&e.changedTouches[0];
  if(!touch) return;
  if(_finishSessionGesture(touch.clientX,touch.clientY,e.target,'touch')) e.stopPropagation();
},{passive:true});
return Object.freeze({
  cancelPendingNavigation(){
    clearTimeout(_tapTimer);
    _tapTimer=null;
    _lastTapTime=0;
    _clearPointerDragState();
  },
});
}

export const sidebarGestures=Object.freeze({
  installRow:_installSessionRowGestures,
  installFork:_installForkChildSwipe,
});

export { _installForkChildSwipe, _installSessionRowGestures };
