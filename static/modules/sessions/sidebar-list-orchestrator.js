import { SESSION_LIST_FLIP_TIMEOUT_MS, SESSION_REFLOW_TIMEOUT_MS, SESSION_SWIPE_DURATION_MS, SESSION_SWIPE_REFLOW_LEAD_MS, sessionListCoordination } from './session-list-coordination.js';
import { _purgeStaleInflightEntries } from './session-run-state.js';
import { _isCliSession } from './session-source.js';
import { _sessionArchivePagingFilterActive, _sessionSourceLabel, _sessionSourceTabCount, _setSessionSourceFilter } from './sidebar-session-opening.js';
import { closeSessionActionMenu } from './session-action-menu.js';
import { _captureSessionReflowPositions, _playSessionRowsReflowFromPositions, _sessionPrefersReducedMotion } from './sidebar-motion.js';
import { _renderBatchActionBar, exitSessionSelectMode, selectAllSessions, toggleSessionSelectMode } from './sidebar-selection.js';
import { NO_PROJECT_FILTER, SESSION_ARCHIVED_MAX_LOADED_LIMIT, SESSION_ARCHIVED_PAGE_SIZE, SESSION_VIRTUAL_BUFFER_ROWS, SESSION_VIRTUAL_ROW_HEIGHT, SESSION_VIRTUAL_THRESHOLD_ROWS, _expandedChildSessionKeys, _selectedSessions, _setShowAllProfiles, sidebarStateBindings } from './sidebar-store.js';
import { _renderSessionListLoadErrorNote } from './session-list-loader.js';
import { renderSessionList } from './session-list-render-port.js';
import { sessionListViewBindings as sessionListBindings } from './session-list-skeleton.js';
import { _sessionLineageContainsSession, _sidebarLineageKeyForRow, _syncSidebarExpansionForActiveSession } from './session-lineage.js';
import { _sessionSearchMergeMatches, sessionSearchBindings } from './session-search.js';
import { _serverNowMs, _sessionSidebarSortCompare, _sessionSortTimestampMs, _sessionTimeBucketLabel } from './session-time.js';
import { _activeSessionIdForSidebar } from './session-navigation.js';
import { _renderOneSession } from './sidebar-row-presentation.js';
import { _ensureActiveSessionRowPresent, _partitionSidebarSessionRows, _renderSidebarRowsFromRawSessions, _scopedSidebarReferenceRows, _sessionRowsWithActiveEphemeralSession } from './sidebar-row-state.js';
import { _ensureSessionVirtualScrollHandler, _resyncSessionVirtualWindowAfterRender, _sessionVirtualSpacer, _sessionVirtualWindow } from './sidebar-virtualization.js';
import { renderSidebarProjectBar } from './sidebar-project-controls.js';

function renderSessionListFromCache(){
  // #4671: while a profile-switch skeleton is up, bail — _allSessions still holds the
  // PREVIOUS profile's rows until /api/sessions resolves, so any unrelated caller
  // (sidebar SSE syncs, stream/unread updates, gateway-poll timers, panel-resync
  // repairs) hitting this mid-switch would repaint the wrong profile's rows over the
  // skeleton. The authoritative switch render clears the flag from inside
  // _applySessionListPayload — once _allSessions is fresh — so only a render backed by
  // up-to-date data replaces the skeleton. The failure-restore path clears it too.
  if(sessionListBindings._sessionListSkeletonActive) return;
  // Don't re-render while user is actively renaming a session (would destroy the input)
  if(sidebarStateBindings._renamingSid) return;
  // Keep the per-conversation actions menu stable while the user is trying to
  // click it. Sidebar syncs, stream/unread updates, and panel-resync repairs can
  // all call this while the fixed-position menu is open; rebuilding the row DOM
  // here removes the anchor and makes the menu feel unclickable.
  if(sidebarStateBindings._sessionActionMenu) return;
  closeSessionActionMenu();
  // Purge stale INFLIGHT entries for sessions the server confirms are NOT
  // streaming. This runs on every list refresh to prevent memory leaks from
  // interrupted streams. (#2066)
  _purgeStaleInflightEntries();
  const searchQueryRaw=($('sessionSearch').value||'').trim();
  const q=searchQueryRaw.toLowerCase();
  const activeSidForSidebar=_activeSessionIdForSidebar();
  const sidebarRows=_sessionRowsWithActiveEphemeralSession(sidebarStateBindings._allSessions);
  // Merge direct session-id/link matches, title matches, then content matches (deduped).
  // Direct matches must not disable content search: if a user pasted the same
  // session id into another conversation, that content hit should still appear.
  const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,sessionSearchBindings._contentSearchResults);
  const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);
  const {
    cliSessionCount,
    profileFiltered,
    sessionsRaw,
    archivedCount,
    webuiReferenceRaw,
    cliReferenceRaw,
    webuiSessionsRaw,
    cliSessionsRaw,
  }=_partitionSidebarSessionRows(allMatched, activeSidForSidebar);
  const referenceRaw=sidebarStateBindings._sessionSourceFilter==='cli'?cliReferenceRaw:webuiReferenceRaw;
  const isCliView=sidebarStateBindings._sessionSourceFilter==='cli';
  const sessions=_renderSidebarRowsFromRawSessions(sessionsRaw, [...referenceRaw, ..._scopedSidebarReferenceRows(isCliView)]);
  // Server-provided source bucket counts are authoritative for the current
  // payload. When present, skip the expensive cross-bucket render/count pass;
  // null is a deliberate "not computed" sentinel consumed only by
  // _sessionSourceTabCount's fallback path below.
  const renderedWebuiSessionCount=sidebarStateBindings._serverWebuiSessionCount===null
    ? _renderSidebarRowsFromRawSessions(webuiSessionsRaw, [...webuiReferenceRaw, ..._scopedSidebarReferenceRows(false)]).length
    : null;
  const renderedCliSessionCount=sidebarStateBindings._serverCliSessionCount===null
    ? _renderSidebarRowsFromRawSessions(cliSessionsRaw, [...cliReferenceRaw, ..._scopedSidebarReferenceRows(true)]).length
    : null;
  const webuiSessionTabCount=_sessionSourceTabCount('webui', renderedWebuiSessionCount, renderedCliSessionCount);
  const cliSessionTabCount=_sessionSourceTabCount('cli', renderedWebuiSessionCount, renderedCliSessionCount);
  _syncSidebarExpansionForActiveSession(sessions, activeSidForSidebar);
  const list=$('sessionList');
  const animateRefresh=sidebarStateBindings._sessionListRefreshAnimationPending;
  sidebarStateBindings._sessionListRefreshAnimationPending=false;
  const enterAllAnimatedRows=animateRefresh&&sidebarStateBindings._sessionListEnterAllAnimationPending;
  sidebarStateBindings._sessionListEnterAllAnimationPending=false;
  const flipBefore=animateRefresh?_captureSessionReflowPositions():null;
  const committedSwipeDuration=_sessionPrefersReducedMotion()?0:SESSION_SWIPE_DURATION_MS;
  const committedSwipeReflowDelay=Math.max(0,committedSwipeDuration-SESSION_SWIPE_REFLOW_LEAD_MS);
  const listScrollTopBeforeRender=list.scrollTop||0;
  list.innerHTML='';
  // #4671: belt-and-suspenders. The authoritative skeleton-clear happens in
  // _applySessionListPayload (once fresh data is in hand) BEFORE this function is
  // reached, and the guard at the top of renderSessionListFromCache bails while the
  // flag is still true — so by the time we paint here the flag is already false. Keep
  // this assignment as a defensive backstop for any future non-switch caller that
  // reaches a real paint with the flag somehow still set.
  sessionListBindings._sessionListSkeletonActive=false;
  // Batch select bar (when in select mode)
  if(sidebarStateBindings._sessionSelectMode){
    const selectBar=document.createElement('div');selectBar.className='session-select-bar';
    const exitBtn=document.createElement('button');exitBtn.className='batch-exit-btn';
    exitBtn.textContent='\u2715';exitBtn.title='Exit select mode';
    exitBtn.onclick=(e)=>{e.stopPropagation();exitSessionSelectMode();};
    selectBar.appendChild(exitBtn);
    const selectAllBtn=document.createElement('button');selectAllBtn.className='batch-select-all-btn';
    selectAllBtn.textContent=t('session_select_all');
    selectAllBtn.onclick=(e)=>{e.stopPropagation();selectAllSessions();};
    selectBar.appendChild(selectAllBtn);
    list.appendChild(selectBar);
  }
  // Ensure batch action bar exists in DOM
  let batchBar=$('batchActionBar');
  if(!batchBar){batchBar=document.createElement('div');batchBar.id='batchActionBar';batchBar.className='batch-action-bar';}
  list.appendChild(batchBar);
  if(sidebarStateBindings._sessionSelectMode&&_selectedSessions.size>0){batchBar.style.display='flex';_renderBatchActionBar();}
  else{batchBar.style.display='none';}
  if(sessionListCoordination.loadError){
    const note=_renderSessionListLoadErrorNote();
    if(note) list.appendChild(note);
  }
  if(window._showCliSessions || cliSessionCount>0){
    const sourceTabs=document.createElement('div');
    sourceTabs.className='session-source-tabs';
    for(const filter of ['webui','cli']){
      const count=filter==='cli'?cliSessionTabCount:webuiSessionTabCount;
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='session-source-tab'+(sidebarStateBindings._sessionSourceFilter===filter?' active':'');
      btn.textContent=_sessionSourceLabel(filter,count);
      btn.setAttribute('aria-pressed', sidebarStateBindings._sessionSourceFilter===filter?'true':'false');
      btn.onclick=()=>_setSessionSourceFilter(filter);
      sourceTabs.appendChild(btn);
    }
    list.appendChild(sourceTabs);
  }
  renderSidebarProjectBar(profileFiltered,list);
  // Profile filter toggle (show sessions from other profiles).
  // Cross-profile rows live SERVER-SIDE behind ?all_profiles=1, so the toggle
  // must trigger a refetch — there's no client-cached aggregate to slice through.
  // The server is authoritative for the count (renamed-root cross-alias is
  // server-side). A naive strict-equality client fallback would mis-count.
  const otherProfileCount = sidebarStateBindings._otherProfileCount;
  if(otherProfileCount>0&&!sidebarStateBindings._showAllProfiles){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show '+otherProfileCount+' from other profiles';
    pfToggle.onclick=()=>{_setShowAllProfiles(true);renderSessionList({deferWhileInteracting:false});};
    list.appendChild(pfToggle);
  } else if(sidebarStateBindings._showAllProfiles){
    const pfToggle=document.createElement('div');
    pfToggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    pfToggle.textContent='Show active profile only';
    pfToggle.onclick=()=>{_setShowAllProfiles(false);renderSessionList({deferWhileInteracting:false});};
    list.appendChild(pfToggle);
  }
  // Show/hide archived toggle if there are archived sessions. Archived rows
  // are fetched on demand so large histories do not bloat every sidebar poll.
  if(archivedCount>0||sidebarStateBindings._showArchived){
    const toggle=document.createElement('div');
    toggle.style.cssText='font-size:10px;padding:4px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.7;';
    toggle.textContent=sidebarStateBindings._showArchived?'Hide archived':'Show '+archivedCount+' archived';
    toggle.onclick=()=>{
      sidebarStateBindings._showArchived=!sidebarStateBindings._showArchived;
      if(sidebarStateBindings._showArchived) sidebarStateBindings._archivedRowsLoadedLimit=SESSION_ARCHIVED_PAGE_SIZE;
      renderSessionList();
    };
    list.appendChild(toggle);
  }
  // Empty state for active project filter
  if(sidebarStateBindings._sessionSourceFilter==='cli'&&sessions.length===0){
    const empty=document.createElement('div');
    empty.className='session-empty-note';
    empty.textContent=window._showCliSessions?'No CLI sessions found.':'Enable Show agent sessions in Settings to list CLI sessions here.';
    list.appendChild(empty);
  } else if(sidebarStateBindings._activeProject&&sessions.length===0){
    const empty=document.createElement('div');
    empty.className='session-empty-note';
    empty.textContent=sidebarStateBindings._activeProject===NO_PROJECT_FILTER?'No unassigned sessions.':'No sessions in this project yet.';
    list.appendChild(empty);
  }
  const orderedSessions=[...sessions].sort(_sessionSidebarSortCompare);
  // Separate pinned from unpinned
  const pinned=orderedSessions.filter(s=>s.pinned);
  const unpinned=orderedSessions.filter(s=>!s.pinned);
  // Date grouping: Pinned / Today / Yesterday / This week / Last week / Older
  const now=_serverNowMs();
  // Collapse state persisted in localStorage
  let _groupCollapsed={};
  try{_groupCollapsed=JSON.parse(localStorage.getItem('hermes-date-groups-collapsed')||'{}');}catch(e){}
  const _saveCollapsed=()=>{try{localStorage.setItem('hermes-date-groups-collapsed',JSON.stringify(_groupCollapsed));}catch(e){}};
  // Group sessions by date
  const groups=[];
  let curLabel=null,curItems=[];
  if(pinned.length) groups.push({label:'\u2605 Pinned',items:pinned,isPinned:true});
  for(const s of unpinned){
    const ts=_sessionSortTimestampMs(s);
    const label=_sessionTimeBucketLabel(ts, now);
    if(label!==curLabel){
      if(curItems.length) groups.push({label:curLabel,items:curItems});
      curLabel=label;curItems=[s];
    } else { curItems.push(s); }
  }
  if(curItems.length) groups.push({label:curLabel,items:curItems});
  const flatSessionRows=[];
  for(const g of groups){
    if(_groupCollapsed[g.label]) continue;
    for(const s of g.items){ flatSessionRows.push({group:g,session:s}); }
  }
  sidebarStateBindings._sessionVisibleSidebarIds=flatSessionRows.map(row=>row.session&&row.session.session_id).filter(Boolean);
  for(const row of flatSessionRows){
    const s=row.session;
    if(!s||!Array.isArray(s._child_sessions)) continue;
    const key=_sidebarLineageKeyForRow(s);
    if(!_expandedChildSessionKeys.has(key)&&!searchQueryRaw) continue;
    for(const child of s._child_sessions){
      if(child&&child.session_source==='fork'&&child.session_id&&!_isReadOnlySession(child)){
        sidebarStateBindings._sessionVisibleSidebarIds.push(child.session_id);
      }
    }
  }
  _ensureSessionVirtualScrollHandler(list);
  const activeIndex=flatSessionRows.findIndex(row=>_sessionLineageContainsSession(row.session,activeSidForSidebar));
  const shouldAnchorActive=activeSidForSidebar&&activeIndex>=0&&(
    list.dataset.sessionVirtualActiveAnchor!==activeSidForSidebar||
    list.dataset.sessionVirtualFilter!==q
  );
  const virtualWindowBeforeActiveAnchor=_sessionVirtualWindow({
    total:flatSessionRows.length,
    scrollTop:listScrollTopBeforeRender,
    viewportHeight:list.clientHeight||520,
    itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
    buffer:SESSION_VIRTUAL_BUFFER_ROWS,
    threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex:-1,
  });
  const activeWasAlreadyVisible=activeIndex>=virtualWindowBeforeActiveAnchor.start&&activeIndex<virtualWindowBeforeActiveAnchor.end;
  const shouldMoveSidebarToActive=shouldAnchorActive&&!activeWasAlreadyVisible;
  let virtualWindow=_sessionVirtualWindow({
    total:flatSessionRows.length,
    scrollTop:listScrollTopBeforeRender,
    viewportHeight:list.clientHeight||520,
    itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
    buffer:SESSION_VIRTUAL_BUFFER_ROWS,
    threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex:shouldMoveSidebarToActive?activeIndex:-1,
  });
  let virtualAnchorScrollTop=null;
  if(shouldMoveSidebarToActive&&virtualWindow.virtualized){
    list.dataset.sessionVirtualActiveAnchor=activeSidForSidebar;
    virtualAnchorScrollTop=virtualWindow.topPad;
  }else if(activeSidForSidebar){
    list.dataset.sessionVirtualActiveAnchor=activeSidForSidebar;
  }else{
    delete list.dataset.sessionVirtualActiveAnchor;
  }
  list.dataset.sessionVirtualTotal=String(flatSessionRows.length);
  list.dataset.sessionVirtualFilter=q;
  list.dataset.sessionVirtualStart=String(virtualWindow.start);
  list.dataset.sessionVirtualEnd=String(virtualWindow.end);
  const rowRenderContext={activeSidForSidebar,searchQueryRaw,animateRefresh,enterAllAnimatedRows,flipBefore,committedSwipeDuration,committedSwipeReflowDelay};
  // Render groups with collapsible headers. Large sidebars render only the
  // current session-row window plus top/bottom spacers inside each group body;
  // headers remain real DOM so pin/archive/date grouping and clicks survive.
  let globalSessionRowIndex=0;
  for(const g of groups){
    const wrapper=document.createElement('div');
    wrapper.className='session-date-group';
    const hdr=document.createElement('div');
    hdr.className='session-date-header'+(g.isPinned?' pinned':'');
    const caret=document.createElement('span');
    caret.className='session-date-caret';
    caret.textContent='\u25BE'; // down when expanded; rotated right when collapsed
    const label=document.createElement('span');
    label.textContent=g.label;
    hdr.appendChild(caret);hdr.appendChild(label);
    const body=document.createElement('div');
    body.className='session-date-body';
    const isGroupCollapsed=Boolean(_groupCollapsed[g.label]);
    if(isGroupCollapsed){body.style.display='none';caret.classList.add('collapsed');}
    hdr.onclick=()=>{
      const isCollapsed=body.style.display==='none';
      body.style.display=isCollapsed?'':'none';
      caret.classList.toggle('collapsed',!isCollapsed);
      _groupCollapsed[g.label]=!isCollapsed;
      _saveCollapsed();
      renderSessionListFromCache();
    };
    wrapper.appendChild(hdr);
    let groupTopPad=0;
    let groupBottomPad=0;
    for(const s of g.items){
      if(isGroupCollapsed) continue;
      const rowIndex=globalSessionRowIndex++;
      const inWindow=!virtualWindow.virtualized||(rowIndex>=virtualWindow.start&&rowIndex<virtualWindow.end);
      if(inWindow){ body.appendChild(_renderOneSession(s, Boolean(g.isPinned), rowRenderContext)); }
      else if(rowIndex<virtualWindow.start){ groupTopPad+=virtualWindow.itemHeight; }
      else { groupBottomPad+=virtualWindow.itemHeight; }
    }
    if(groupTopPad>0){ body.insertBefore(_sessionVirtualSpacer(groupTopPad,'before'), body.firstChild); }
    if(groupBottomPad>0){ body.appendChild(_sessionVirtualSpacer(groupBottomPad,'after')); }
    wrapper.appendChild(body);
    list.appendChild(wrapper);
  }
  if(virtualAnchorScrollTop!==null){
    list.scrollTop=virtualAnchorScrollTop;
  }else if(listScrollTopBeforeRender>0){
    // Always restore the user's scroll position after re-render, regardless
    // of whether the virtualization window applies. Lists below the
    // virtualization threshold (≤80 rows) still have their DOM rebuilt by
    // every renderSessionListFromCache() call, and without this restore the
    // scrollTop drops to 0 — producing a "scroll keeps jumping back" feel
    // when the list scrolls naturally. Fixed for #1669 follow-up.
    list.scrollTop=listScrollTopBeforeRender;
    _resyncSessionVirtualWindowAfterRender(list, listScrollTopBeforeRender, virtualWindow);
  }
  const archivePagingFilterActive=_sessionArchivePagingFilterActive();
  if(sidebarStateBindings._showArchived&&!archivePagingFilterActive){
    const activeArchivedTotal=sidebarStateBindings._sessionSourceFilter==='cli'?sidebarStateBindings._archivedCliCount:sidebarStateBindings._archivedWebuiCount;
    const loadedArchivedCount=sidebarRows.filter(s=>s&&s.archived&&(sidebarStateBindings._sessionSourceFilter==='cli'?_isCliSession(s):!_isCliSession(s))).length;
    const archiveLoadCapReached=Number(sidebarStateBindings._archivedRowsLoadedLimit||0)>=SESSION_ARCHIVED_MAX_LOADED_LIMIT;
    const remainingArchived=archiveLoadCapReached?0:Math.max(0, Number(activeArchivedTotal||0)-loadedArchivedCount);
    if(remainingArchived>0){
      const more=document.createElement('div');
      more.className='session-archive-more';
      more.style.cssText='font-size:10px;padding:6px 10px;color:var(--muted);cursor:pointer;text-align:center;opacity:.8;';
      more.textContent='Load '+Math.min(SESSION_ARCHIVED_PAGE_SIZE, remainingArchived)+' more archived ('+remainingArchived+' remaining)';
      more.onclick=()=>{
        sidebarStateBindings._archivedRowsLoadedLimit=Math.min(
          SESSION_ARCHIVED_MAX_LOADED_LIMIT,
          Math.max(SESSION_ARCHIVED_PAGE_SIZE, Number(sidebarStateBindings._archivedRowsLoadedLimit)||SESSION_ARCHIVED_PAGE_SIZE)+SESSION_ARCHIVED_PAGE_SIZE
        );
        renderSessionList();
      };
      list.appendChild(more);
    }
  }
  // Select mode toggle button (only when NOT in select mode)
  if(!sidebarStateBindings._sessionSelectMode){
    const toggleBtn=document.createElement('div');toggleBtn.className='session-select-toggle';
    toggleBtn.textContent=t('session_select_mode');
    toggleBtn.onclick=(e)=>{e.stopPropagation();toggleSessionSelectMode();};
    list.appendChild(toggleBtn);
  }
  // Refresh FLIP and queued archive/delete reflow both drive
  // --session-reflow-offset. Refresh wins so one render has one transform writer.
  const reflowBefore=animateRefresh?flipBefore:sidebarStateBindings._pendingSessionReflowPositions;
  const reflowTimeout=animateRefresh?SESSION_LIST_FLIP_TIMEOUT_MS:SESSION_REFLOW_TIMEOUT_MS;
  sidebarStateBindings._pendingSessionReflowPositions=null;
  _playSessionRowsReflowFromPositions(reflowBefore,reflowTimeout,_sessionPrefersReducedMotion);

}

export const sidebarListOrchestration=Object.freeze({render:renderSessionListFromCache});

export { renderSessionListFromCache };
