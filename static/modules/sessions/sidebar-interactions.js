import { SESSION_ARCHIVE_SWIPE_THRESHOLD_PX, SESSION_DELETE_SWIPE_THRESHOLD_PX, SESSION_LONG_PRESS_DELAY_MS, SESSION_SWIPE_CANCEL_RATIO, sessionListCoordination } from './session-list-coordination.js';
import { sessionLoadState } from './session-load-state.js';
import { sessionRunRegistry } from './session-run-registry.js';
import { _isSessionEffectivelyStreaming, _rememberSessionListSource } from './session-run-state.js';
import { _forgetObservedStreamingSession } from './session-unread.js';

const _sessionStreamingById=sessionRunRegistry.streamingById;
import { _newSessionInFlight, newSession } from './lifecycle.js';
import { _isCliSession, _isMessagingSession, _openSidebarSession, _setActiveProjectFilter } from './message-loading.js';
import { _archiveSession, _openSessionActionMenu, closeSessionActionMenu } from './sidebar-actions.js';
import { _waitForSessionMotion } from './sidebar-motion.js';
import { toggleSessionSelect } from './sidebar-selection.js';
import { NO_PROJECT_FILTER, SESSION_VIRTUAL_BUFFER_ROWS, SESSION_VIRTUAL_ROW_HEIGHT, SESSION_VIRTUAL_THRESHOLD_ROWS, sidebarStateBindings } from './sidebar-store.js';
import { _activeSessionIdForSidebar, _sessionIdFromLocation } from './session-navigation.js';
import { _sessionDisplayTitle, _sessionTitleIsDefaultWebUI, _sessionTitleTags } from './session-display.js';
import { renderSessionList } from './session-list-render-port.js';
import { _schedulePendingSessionListApply } from './session-list-reconciliation.js';
import { sessionListViewBindings as sessionListBindings } from './session-list-skeleton.js';
import { _attachChildSessionsToSidebarRows, _collapseSessionLineageForSidebar, _isChildSession, sessionDiscoveryBindings } from './session-discovery.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { deleteSession } from './management.js';

function upsertActiveSessionForLocalTurn({title='', messageCount=0, timestampMs=Date.now()}={}){
  if(!S.session||!S.session.session_id) return;
  const sid=S.session.session_id;
  const nowSec=Math.floor((Number(timestampMs)||Date.now())/1000);
  const localCount=Array.isArray(S.messages)?S.messages.length:0;
  const count=Math.max(Number(S.session.message_count||0),Number(messageCount||0),localCount,1);
  S.session.message_count=count;
  S.session.last_message_at=nowSec;
  S.session.updated_at=nowSec;
  if((S.session.title==='Untitled'||!S.session.title)&&title){
    S.session.title=title;
  }
  const existingIdx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===sid);
  const row={
    ...S.session,
    session_id:sid,
    title:S.session.title||title||'New chat',
    message_count:count,
    last_message_at:nowSec,
    updated_at:nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:true,
  };
  if(existingIdx>=0) sidebarStateBindings._allSessions[existingIdx]={...sidebarStateBindings._allSessions[existingIdx],...row};
  else sidebarStateBindings._allSessions.unshift(row);
  renderSessionListFromCache();
}

function _sessionRowsWithActiveEphemeralSession(rows){
  rows=Array.isArray(rows)?rows:[];
  if(!S.session||!S.session.session_id) return rows;
  const sid=S.session.session_id;
  if(rows.some(s=>s&&s.session_id===sid)) return rows;
  const nowSec=Math.floor(Date.now()/1000);
  const activeRow={
    ...S.session,
    session_id:sid,
    title:S.session.title||'New Chat',
    display_title:S.session.display_title||S.session.title||'New Chat',
    message_count:0,
    last_message_at:S.session.last_message_at||S.session.updated_at||nowSec,
    updated_at:S.session.updated_at||S.session.last_message_at||nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:false,
  };
  return [activeRow,...rows];
}

function _ensureActiveSessionRowPresent(rows, sourceRows){
  rows=Array.isArray(rows)?rows:[];
  const activeSid=_activeSessionIdForSidebar();
  if(!activeSid||rows.some(s=>s&&s.session_id===activeSid)) return rows;
  const activeRow=(Array.isArray(sourceRows)?sourceRows:[]).find(s=>s&&s.session_id===activeSid);
  // Only re-inject the active FRESHLY-CREATED 0-message ephemeral chat. An active
  // conversation that already has messages and was filtered out by the search
  // query must stay filtered — re-adding it here would pollute unrelated search
  // results with the current chat (#3408 review, Codex).
  if(activeRow && Number(activeRow.message_count||0)<=0){
    return [activeRow,...rows];
  }
  return rows;
}

function clearOptimisticSessionStreaming(sid){
  sid=sid||(S.session&&S.session.session_id)||'';
  if(!sid) return;
  if(typeof _rememberSessionListSource==='function') _rememberSessionListSource(null, sid, false);
  if(S.session&&S.session.session_id===sid){
    S.session.active_stream_id=null;
    S.activeStreamId=null;
  }
  if(Array.isArray(sidebarStateBindings._allSessions)){
    const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===sid);
    if(idx>=0){
      sidebarStateBindings._allSessions[idx]={
        ...sidebarStateBindings._allSessions[idx],
        active_stream_id:null,
        pending_user_message:null,
        pending_started_at:null,
        is_streaming:false,
      };
    }
  }
  if(typeof _sessionStreamingById!=='undefined'&&_sessionStreamingById&&typeof _sessionStreamingById.set==='function'){
    _sessionStreamingById.set(sid,false);
  }
  if(typeof _forgetObservedStreamingSession==='function') _forgetObservedStreamingSession(sid);
  renderSessionListFromCache();
}


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

// Top-level so BOTH the sidebar visibility predicate (_sidebarRowHasVisibleMessages,
// reached via renderSessionListFromCache -> _partitionSidebarSessionRows) and the
// per-row renderer (_renderOneSession, nested in renderSessionListFromCache) can call
// it. It was previously declared INSIDE renderSessionListFromCache and relied on
// function hoisting — but hoisting is scoped to the enclosing function, so the
// top-level _sidebarRowHasVisibleMessages threw "ReferenceError: _sessionAttentionState
// is not defined" on every cache render, crashing the sidebar (#3696, regressed in
// #3672 when _sidebarRowHasVisibleMessages was extracted to top level). Pure function
// (only its arg `s` plus the i18n global `t`), so hoisting it is safe.
function _sessionAttentionState(s){
  const attention=s&&s.attention&&typeof s.attention==='object'?s.attention:null;
  if(!attention||!attention.kind||!Number.isFinite(Number(attention.count))||Number(attention.count)<=0)return null;
  const kind=String(attention.kind)==='approval'?'approval':(String(attention.kind)==='clarify'?'clarify':'attention');
  const count=Math.max(1,Number(attention.count)||1);
  const labelKey=kind==='approval'?'session_attention_approval':(kind==='clarify'?'session_attention_clarify':'session_attention_generic');
  const titleKey=kind==='approval'?'session_attention_approval_title':(kind==='clarify'?'session_attention_clarify_title':'session_attention_generic_title');
  const fallback=kind==='approval'?(count===1?'Approval':`${count} approvals`):(kind==='clarify'?(count===1?'Question':`${count} questions`):(count===1?'Attention':`${count} items`));
  const titleFallback=kind==='approval'?'Waiting for permission decision':(kind==='clarify'?'Waiting for your answer':'Waiting for user action');
  const label=(typeof t==='function')?t(labelKey,count):fallback;
  const title=(typeof t==='function')?t(titleKey,count):titleFallback;
  return {kind,count,severity:String(attention.severity||''),label,title};
}

function _sidebarRowHasVisibleMessages(s, activeSidForSidebar){
  return (s.message_count||0)>0 ||
    _sessionAttentionState(s) ||
    _isSessionEffectivelyStreaming(s) ||
    !!s.active_stream_id ||
    !!s.pending_user_message ||
    !!s.has_pending_user_message ||
    (activeSidForSidebar&&s.session_id===activeSidForSidebar) ||
    // #5306: a linked delegate child of the currently-active/streaming parent
    // must stay rendered for the duration of the parent's turn. A subagent child
    // that transiently reports message_count===0 between /api/sessions polls would
    // otherwise be dropped HERE (before _attachChildSessionsToSidebarRows ever sees
    // it), so it never reaches sessionsRaw, vanishes from the sidebar, then
    // reappears on the next refresh once its list metadata catches up — the flicker.
    // Scoped to children of the ACTIVE parent, mirroring the active-session
    // exception above, so unrelated truly-empty sessions are still hidden.
    (activeSidForSidebar&&s.parent_session_id===activeSidForSidebar&&_isChildSession(s)) ||
    (S.session&&s.session_id===S.session.session_id&&(S.session.message_count||0)>0);
}

function _partitionSidebarSessionRows(allMatched, activeSidForSidebar){
  let cliSessionCount=0;
  const webuiProfileFiltered=[];
  const cliProfileFiltered=[];
  const webuiReferenceRaw=[];
  const cliReferenceRaw=[];
  const webuiSessionsRaw=[];
  const cliSessionsRaw=[];
  let webuiArchivedCount=0;
  let cliArchivedCount=0;
  for(const s of allMatched){
    if(!_sidebarRowHasVisibleMessages(s, activeSidForSidebar)) continue;
    const isCli=_isCliSession(s);
    if(isCli) cliSessionCount++;
    if(s.default_hidden&&!(sidebarStateBindings._activeProject&&sidebarStateBindings._activeProject!==NO_PROJECT_FILTER&&s.project_id===sidebarStateBindings._activeProject)) continue;
    const profileFiltered=isCli ? cliProfileFiltered : webuiProfileFiltered;
    const referenceRaw=isCli ? cliReferenceRaw : webuiReferenceRaw;
    const sessionsRaw=isCli ? cliSessionsRaw : webuiSessionsRaw;
    profileFiltered.push(s);
    if(sidebarStateBindings._activeProject===NO_PROJECT_FILTER){
      if(s.project_id) continue;
    } else if(sidebarStateBindings._activeProject){
      if(s.project_id!==sidebarStateBindings._activeProject) continue;
    }
    referenceRaw.push(s);
    if(s.archived){
      if(isCli) cliArchivedCount++;
      else webuiArchivedCount++;
    }
    if(!sidebarStateBindings._showArchived&&s.archived) continue;
    sessionsRaw.push(s);
  }
  if(sidebarStateBindings._sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0){
    sidebarStateBindings._sessionSourceFilter='webui';
  }
  const showCliOnly=sidebarStateBindings._sessionSourceFilter==='cli';
  const serverArchivedCount=showCliOnly?sidebarStateBindings._archivedCliCount:sidebarStateBindings._archivedWebuiCount;
  return {
    cliSessionCount,
    profileFiltered: showCliOnly ? cliProfileFiltered : webuiProfileFiltered,
    sessionsRaw: showCliOnly ? cliSessionsRaw : webuiSessionsRaw,
    archivedCount: Math.max(showCliOnly ? cliArchivedCount : webuiArchivedCount, Number(serverArchivedCount||0)),
    webuiReferenceRaw,
    cliReferenceRaw,
    webuiSessionsRaw,
    cliSessionsRaw,
  };
}

// Hidden archived-ancestor reference rows (sidebar_reference_sessions) arrive
// from /api/sessions WITHOUT the client-side project/source scoping that
// _partitionSidebarSessionRows applies to the visible rows. Appending them to
// EVERY render unconditionally let an archived parent from a DIFFERENT project
// (or the other source bucket) enter a project/source-filtered render's
// suppression context — silently hiding a visible child/fork whose archived
// ancestor lives outside the current view. Scope the references to the same
// project + source bucket as the render they feed before using them.
function _scopedSidebarReferenceRows(isCli){
  if(typeof sidebarStateBindings._sidebarReferenceSessions==='undefined'||!Array.isArray(sidebarStateBindings._sidebarReferenceSessions)||!sidebarStateBindings._sidebarReferenceSessions.length) return [];
  return sidebarStateBindings._sidebarReferenceSessions.filter(s=>{
    if(!s) return false;
    // Source scope: only references in the same webui/cli bucket as this render.
    if(_isCliSession(s)!==!!isCli) return false;
    // Project scope: mirror _partitionSidebarSessionRows exactly.
    if(sidebarStateBindings._activeProject===NO_PROJECT_FILTER){ if(s.project_id) return false; }
    else if(sidebarStateBindings._activeProject){ if(s.project_id!==sidebarStateBindings._activeProject) return false; }
    return true;
  });
}

function _renderSidebarRowsFromRawSessions(sessionsRaw, referenceSessionsRaw){
  const referenceRows=Array.isArray(referenceSessionsRaw)?referenceSessionsRaw:sessionsRaw;
  return _attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw, referenceRows);
}

function _attachProjectQuickCreateButton(chip, project){
  const btn=document.createElement('button');
  btn.type='button';
  btn.className='project-chip-quick-create';
  btn.textContent='+';
  btn.title='New conversation in this project';
  btn.setAttribute('aria-label','New conversation in this project');
  const stop=function(e){
    if(!e) return;
    if(typeof e.preventDefault==='function') e.preventDefault();
    if(typeof e.stopPropagation==='function') e.stopPropagation();
    if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
  };
  const stopTouchBubble=function(e){
    if(!e) return;
    if(typeof e.stopPropagation==='function') e.stopPropagation();
    if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
  };
  btn.onclick=async(e)=>{
    stop(e);
    if(_newSessionInFlight){
      // The initiating tap already owns the filter change and rollback path.
      try{
        await newSession(false,{project_id:project.project_id});
      }catch(_){
        // The initiating tap already owns the visible failure path.
      }
      return;
    }
    const previousProject=(typeof sidebarStateBindings._activeProject!=='undefined')?sidebarStateBindings._activeProject:NO_PROJECT_FILTER;
    _setActiveProjectFilter(project.project_id);
    try{
      await newSession(false,{project_id:project.project_id});
      // newSession() does not repaint the sidebar (callers own that — see the
      // newSession contract). Repaint from the post-create state so the new
      // project-assigned session appears deterministically.
      try{ if(typeof renderSessionListFromCache==='function') renderSessionListFromCache(); }catch(_){}
      try{ if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:false}); }catch(_){}
    }catch(err){
      _setActiveProjectFilter(previousProject);
      if(typeof showToast==='function') showToast('New conversation failed: '+(err&&err.message||err));
    }
  };
  btn.ondblclick=(e)=>{stop(e);};
  btn.oncontextmenu=(e)=>{stop(e);};
  btn.ontouchstart=(e)=>{stopTouchBubble(e);};
  btn.ontouchend=(e)=>{stopTouchBubble(e);};
  chip.appendChild(btn);
}


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
      if(($('sessionSearch').value||'').trim()) sessionDiscoveryBindings._hideSearchPreviewsAfterSelect=true;
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
}

export const sidebarRowBehavior=Object.freeze({upsert:upsertActiveSessionForLocalTurn,clearOptimistic:clearOptimisticSessionStreaming,installGestures:_installSessionRowGestures,installForkGestures:_installForkChildSwipe});

export { _activeSessionIdForSidebar, _attachProjectQuickCreateButton, _ensureActiveSessionRowPresent, _ensureSessionVirtualScrollHandler, _installForkChildSwipe, _installSessionRowGestures, _partitionSidebarSessionRows, _renderSidebarRowsFromRawSessions, _resyncSessionVirtualWindowAfterRender, _scopedSidebarReferenceRows, _sessionAttentionState, _sessionDisplayTitle, _sessionRowsWithActiveEphemeralSession, _sessionTitleTags, _sessionVirtualSpacer, _sessionVirtualWindow, clearOptimisticSessionStreaming, upsertActiveSessionForLocalTurn };
