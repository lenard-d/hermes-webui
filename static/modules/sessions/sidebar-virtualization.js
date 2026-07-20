import { sessionListCoordination } from './session-list-coordination.js';
import { SESSION_VIRTUAL_BUFFER_ROWS, SESSION_VIRTUAL_ROW_HEIGHT, SESSION_VIRTUAL_THRESHOLD_ROWS, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _schedulePendingSessionListApply } from './session-list-reconciliation.js';
import { sessionListViewBindings as sessionListBindings } from './session-list-skeleton.js';

function _sessionVirtualWindow(opts){
  const total=Math.max(0, Number(opts&&opts.total)||0);
  const threshold=Math.max(1, Number(opts&&opts.threshold)||SESSION_VIRTUAL_THRESHOLD_ROWS);
  const itemHeight=Math.max(1, Number(opts&&opts.itemHeight)||SESSION_VIRTUAL_ROW_HEIGHT);
  const buffer=Math.max(0, Number(opts&&opts.buffer)||SESSION_VIRTUAL_BUFFER_ROWS);
  const viewportHeight=Math.max(itemHeight, Number(opts&&opts.viewportHeight)||itemHeight*10);
  const visibleRows=Math.max(1, Math.ceil(viewportHeight/itemHeight));
  if(total<=threshold){
    return {virtualized:false,start:0,end:total,topPad:0,bottomPad:0,itemHeight,total};
  }
  let start=Math.floor((Number(opts&&opts.scrollTop)||0)/itemHeight)-buffer;
  start=Math.max(0, Math.min(start, Math.max(0,total-visibleRows)));
  let end=Math.min(total, start+visibleRows+(buffer*2));
  const activeIndex=Number.isFinite(Number(opts&&opts.activeIndex))?Number(opts.activeIndex):-1;
  if(activeIndex>=0&&activeIndex<total&&(activeIndex<start||activeIndex>=end)){
    start=Math.max(0, Math.min(activeIndex-buffer, Math.max(0,total-visibleRows-(buffer*2))));
    end=Math.min(total, start+visibleRows+(buffer*2));
  }
  return {
    virtualized:true,
    start,
    end,
    topPad:start*itemHeight,
    bottomPad:Math.max(0,(total-end)*itemHeight),
    itemHeight,
    total,
  };
}

function _sessionVirtualSpacer(height, where){
  const spacer=document.createElement('div');
  spacer.className='session-virtual-spacer';
  spacer.dataset.virtualSpacer=where||'gap';
  spacer.setAttribute('aria-hidden','true');
  spacer.style.height=Math.max(0,Math.round(height||0))+'px';
  spacer.style.flex='0 0 auto';
  return spacer;
}

function _scheduleSessionVirtualizedRender(){
  sessionListCoordination.lastScrollAt=Date.now();
  // While a profile-switch skeleton is up, ignore virtual-scroll events: the
  // cached rows are the PREVIOUS profile's, and repainting them here would
  // clobber the skeleton before the new /api/sessions response lands (#4662
  // Codex gate). The real render clears _sessionListSkeletonActive.
  if(sessionListBindings._sessionListSkeletonActive) return;
  if(sidebarStateBindings._renamingSid||sidebarStateBindings._sessionVirtualScrollRaf) return;
  const list=sidebarStateBindings._sessionVirtualScrollList;
  const total=Number(list&&list.dataset&&list.dataset.sessionVirtualTotal||0);
  // Skip the re-render if the list is below the virtualization threshold —
  // there's no virtual window to recompute, and re-rendering would just
  // rebuild the whole DOM on every scroll tick. Without this guard, the
  // unconditional scroll listener (attached for any list) caused
  // user-facing scroll jumps on small lists. (#1669 follow-up)
  if(total>0&&total<=SESSION_VIRTUAL_THRESHOLD_ROWS) return;
  sidebarStateBindings._sessionVirtualScrollRaf=requestAnimationFrame(()=>{
    sidebarStateBindings._sessionVirtualScrollRaf=0;
    const liveList=sidebarStateBindings._sessionVirtualScrollList;
    const liveTotal=Number(liveList&&liveList.dataset&&liveList.dataset.sessionVirtualTotal||0);
    if(liveList&&liveTotal>SESSION_VIRTUAL_THRESHOLD_ROWS){
      const nextWindow=_sessionVirtualWindow({
        total:liveTotal,
        scrollTop:liveList.scrollTop||0,
        viewportHeight:liveList.clientHeight||520,
        itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
        buffer:SESSION_VIRTUAL_BUFFER_ROWS,
        threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
        activeIndex:-1,
      });
      const currentStart=Number(liveList.dataset.sessionVirtualStart||0);
      const currentEnd=Number(liveList.dataset.sessionVirtualEnd||0);
      if(nextWindow.virtualized&&nextWindow.start===currentStart&&nextWindow.end===currentEnd) return;
    }
    renderSessionListFromCache();
  });
}

function _ensureSessionVirtualScrollHandler(list){
  if(!list) return;
  if(sidebarStateBindings._sessionVirtualScrollList===list) return;
  if(sidebarStateBindings._sessionVirtualScrollList){
    sidebarStateBindings._sessionVirtualScrollList.removeEventListener('scroll', _scheduleSessionVirtualizedRender);
    sidebarStateBindings._sessionVirtualScrollList.removeEventListener('pointerdown', _markSessionListPointerDown);
    sidebarStateBindings._sessionVirtualScrollList.removeEventListener('pointerup', _markSessionListPointerUp);
    sidebarStateBindings._sessionVirtualScrollList.removeEventListener('pointercancel', _markSessionListPointerUp);
    sidebarStateBindings._sessionVirtualScrollList.removeEventListener('pointerleave', _markSessionListPointerUp);
  }
  sidebarStateBindings._sessionVirtualScrollList=list;
  list.addEventListener('scroll', _scheduleSessionVirtualizedRender, {passive:true});
  list.addEventListener('pointerdown', _markSessionListPointerDown, {passive:true});
  list.addEventListener('pointerup', _markSessionListPointerUp, {passive:true});
  list.addEventListener('pointercancel', _markSessionListPointerUp, {passive:true});
  list.addEventListener('pointerleave', _markSessionListPointerUp, {passive:true});
}

function _markSessionListPointerDown(){
  sessionListCoordination.pointerActive=true;
  sessionListCoordination.lastScrollAt=Date.now();
}

function _markSessionListPointerUp(){
  sessionListCoordination.pointerActive=false;
  sessionListCoordination.lastScrollAt=Date.now();
  if(sessionListCoordination.pendingPayload) _schedulePendingSessionListApply();
}

let _sessionVirtualResyncRaf = 0;
function _resyncSessionVirtualWindowAfterRender(list, expectedScrollTop, virtualWindow){
  if(!list||!virtualWindow||!virtualWindow.virtualized) return;
  expectedScrollTop=Number(expectedScrollTop)||0;
  if(expectedScrollTop<=0) return;
  if(_sessionVirtualResyncRaf) cancelAnimationFrame(_sessionVirtualResyncRaf);
  _sessionVirtualResyncRaf=requestAnimationFrame(()=>{
    _sessionVirtualResyncRaf=0;
    if(sidebarStateBindings._renamingSid) return;
    const actualScrollTop=Number(list.scrollTop)||0;
    const tolerance=Math.max(2, Number(virtualWindow.itemHeight||SESSION_VIRTUAL_ROW_HEIGHT)/2);
    if(Math.abs(actualScrollTop-expectedScrollTop)<=tolerance) return;
    renderSessionListFromCache();
  });
}

export const sidebarVirtualization=Object.freeze({
  window:_sessionVirtualWindow,
  spacer:_sessionVirtualSpacer,
  attach:_ensureSessionVirtualScrollHandler,
  resync:_resyncSessionVirtualWindowAfterRender,
});

export {
  _ensureSessionVirtualScrollHandler,
  _resyncSessionVirtualWindowAfterRender,
  _sessionVirtualSpacer,
  _sessionVirtualWindow,
};
