import { SESSION_LIST_FLIP_TIMEOUT_MS, SESSION_REFLOW_TIMEOUT_MS, SESSION_SWIPE_DURATION_MS, SESSION_SWIPE_REFLOW_LEAD_MS, sessionListCoordination } from './session-list-coordination.js';
import { SESSION_ICONS as ICONS, _formatSessionModelWithGateway } from './session-display.js';
import { _isSessionEffectivelyStreaming, _purgeStaleInflightEntries, _rememberRenderedSessionSnapshot, _rememberRenderedStreamingState } from './session-run-state.js';
import { _hasUnreadForSession } from './session-unread.js';
import { _getChannelLabel, _isCliSession, _isMessagingSession, _isReadOnlySession, _openSidebarSession, _sessionArchivePagingFilterActive, _sessionSourceLabel, _sessionSourceTabCount, _setActiveProjectFilter, _setSessionSourceFilter, _sourceKeyForSession } from './message-loading.js';
import { _buildSessionRenameStarter, _openSessionActionMenu, closeSessionActionMenu } from './sidebar-actions.js';
import { _captureSessionReflowPositions, _makeSessionSwipeAffordance, _playSessionRowsReflowFromPositions, _sessionPrefersReducedMotion } from './sidebar-motion.js';
import { _renderBatchActionBar, exitSessionSelectMode, selectAllSessions, setSessionSelected, toggleSessionSelectMode } from './sidebar-selection.js';
import { NO_PROJECT_FILTER, SESSION_ARCHIVED_MAX_LOADED_LIMIT, SESSION_ARCHIVED_PAGE_SIZE, SESSION_VIRTUAL_BUFFER_ROWS, SESSION_VIRTUAL_ROW_HEIGHT, SESSION_VIRTUAL_THRESHOLD_ROWS, _expandedChildSessionKeys, _expandedLineageKeys, _lineageReportInflight, _selectedSessions, _sessionSwipeReturnOffsets, _setShowAllProfiles, sidebarStateBindings } from './sidebar-store.js';
import { _renderSessionListLoadErrorNote } from './session-list-loader.js';
import { renderSessionList } from './session-list-render-port.js';
import { sessionListViewBindings as sessionListBindings } from './session-list-skeleton.js';
import { registerSidebarRenderer } from './sidebar-render-port.js';
import { _appendHighlightedText, _fetchLineageReportForRow, _formatRelativeSessionTime, _lineageReportCacheKey, _lineageReportNeedsFetch, _lineageSegmentsForRender, _serverNowMs, _sessionChildBadgeTooltip, _sessionForkTooltip, _sessionFullTitleTooltip, _sessionLineageBadgeTooltip, _sessionLineageContainsSession, _sessionSearchContentPreview, _sessionSearchMergeMatches, _sessionSegmentCount, _sessionSidebarSortCompare, _sessionSortTimestampMs, _sessionStateTooltip, _sessionTimeBucketLabel, _sessionTimestampMs, _sessionTitleForForkParent, _sidebarLineageKeyForRow, _syncSidebarExpansionForActiveSession, _truncatedSessionId, filterSessions, sessionDiscoveryBindings } from './session-discovery.js';
import { _attachProjectQuickCreateButton, _ensureActiveSessionRowPresent, _ensureSessionVirtualScrollHandler, _installForkChildSwipe, _installSessionRowGestures, _partitionSidebarSessionRows, _renderSidebarRowsFromRawSessions, _resyncSessionVirtualWindowAfterRender, _scopedSidebarReferenceRows, _sessionAttentionState, _sessionRowsWithActiveEphemeralSession, _sessionVirtualSpacer, _sessionVirtualWindow } from './sidebar-interactions.js';
import { _sessionDisplayTitle, _sessionTitleTags } from './session-display.js';
import { _activeSessionIdForSidebar } from './session-navigation.js';
import { _showProjectContextMenu, _startProjectCreate, _startProjectRename } from './management.js';

function _renderOneSession(s, isPinnedGroup=false, renderContext){
  const {activeSidForSidebar,searchQueryRaw,animateRefresh,enterAllAnimatedRows,flipBefore,committedSwipeDuration,committedSwipeReflowDelay}=renderContext;
  const el=document.createElement('div');
  const isActive=_sessionLineageContainsSession(s,activeSidForSidebar);
  const ownStreaming=_isSessionEffectivelyStreaming(s);
  const isStreaming=ownStreaming||!!s._child_session_streaming;
  _rememberRenderedStreamingState(s, ownStreaming);
  _rememberRenderedSessionSnapshot(s);
  const hasUnread=(_hasUnreadForSession(s)||!!s._child_session_has_unread)&&!isActive;
  const attention=_sessionAttentionState(s)||_sessionAttentionState({_child:true,attention:s._child_session_attention});
  const attentionClass=attention?(attention.kind==='approval'?' attention-approval':(attention.kind==='clarify'?' attention-clarify':' attention-attention')):'';
  const readOnly=_isReadOnlySession(s);
  el.className='session-item'+(isActive?' active':'')+(isActive&&S.session&&S.session._flash?' new-flash':'')+(s.archived?' archived':'')+(ownStreaming?' streaming':'')+(hasUnread?' unread':'')+(attention?' needs-attention':'')+attentionClass;
  const swipeReturnOffset=_sessionSwipeReturnOffsets.get(s.session_id);
  if(swipeReturnOffset!==undefined){
    _sessionSwipeReturnOffsets.delete(s.session_id);
    el.style.setProperty('--session-swipe-return-offset',swipeReturnOffset);
    el.classList.add('session-swipe-returning');
    el.addEventListener('animationend',()=>{
      el.classList.remove('session-swipe-returning');
      el.style.removeProperty('--session-swipe-return-offset');
    },{once:true});
  }
  if(animateRefresh&&(enterAllAnimatedRows||!(flipBefore&&flipBefore.has(s.session_id)))){
    el.classList.add('session-list-flip-enter');
  }
  if(s.is_cli_session||_isMessagingSession(s)){
    el.classList.add('cli-session');
    el.dataset.source=_getChannelLabel(s)||'CLI';
    el.dataset.sourceKey=_sourceKeyForSession(s)||'cli';
  }
  if(readOnly) el.classList.add('read-only-session');
  if(isActive&&S.session&&S.session._flash)delete S.session._flash;
  const rawTitle=_sessionDisplayTitle(s);
  const tags=_sessionTitleTags(rawTitle);
  let cleanTitle=tags.length?rawTitle.replace(/#(?!\d+\b)[\w-]+/g,'').trim():rawTitle;
  // Guard: system prompt content must never surface as a visible session title
  if(cleanTitle.startsWith('[SYSTEM:')){
    cleanTitle='Session';
  }
  // Checkbox for batch select mode
  if(sidebarStateBindings._sessionSelectMode&&!readOnly){
    const cbWrapper=document.createElement('label');cbWrapper.className='session-select-cb-wrapper';
    const cb=document.createElement('input');cb.type='checkbox';cb.className='session-select-cb';
    cb.dataset.sid=s.session_id;cb.checked=_selectedSessions.has(s.session_id);
    cb.onchange=(e)=>{e.stopPropagation();setSessionSelected(s.session_id,cb.checked);};
    cb.onclick=(e)=>{e.stopPropagation();};
    cb.onpointerup=(e)=>{e.stopPropagation();};
    cbWrapper.onpointerup=(e)=>{e.stopPropagation();};
    cbWrapper.onclick=(e)=>{e.stopPropagation();};
    cbWrapper.appendChild(cb);
    el.classList.toggle('selected',_selectedSessions.has(s.session_id));
    el.appendChild(cbWrapper);
  }
  const sessionText=document.createElement('div');
  sessionText.className='session-text';
  const titleRow=document.createElement('div');
  titleRow.className='session-title-row';
  if(s.pinned&&!isPinnedGroup){
    const pinInd=document.createElement('span');
    pinInd.className='session-pin-indicator';
    pinInd.innerHTML=ICONS.pin;
    titleRow.appendChild(pinInd);
  }
  if(s.worktree_path){
    const wtInd=document.createElement('span');
    wtInd.className='session-worktree-indicator';
    wtInd.innerHTML=li('git-branch',12);
    const wtLabel=(typeof t==='function'?t('session_worktree_badge'):'Worktree');
    wtInd.title=`${wtLabel}: ${s.worktree_branch||s.worktree_path}`;
    titleRow.appendChild(wtInd);
  }
  // Parent session indicator for forked/branched sessions (#465)
  if(s.parent_session_id){
    const branchInd=document.createElement('span');
    branchInd.className='session-branch-indicator';
    branchInd.innerHTML=li('git-branch',12);
    const parentLabel=_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id);
    branchInd.title=_sessionForkTooltip(parentLabel);
    titleRow.appendChild(branchInd);
  }
  const title=document.createElement('span');
  title.className='session-title';
  const displayTitle=cleanTitle||'Untitled';
  const titleMatched=Boolean(searchQueryRaw&&displayTitle.toLowerCase().includes(searchQueryRaw.toLowerCase()));
  if(titleMatched) _appendHighlightedText(title,displayTitle,searchQueryRaw,'session-search-hit');
  else title.textContent=displayTitle;
  title.title=_sessionFullTitleTooltip(rawTitle,cleanTitle,s);
  const tsMs=_sessionTimestampMs(s);
  const ts=document.createElement('span');
  const hasAttentionState=isStreaming||hasUnread||Boolean(attention);
  ts.className='session-time'+(hasAttentionState?' is-hidden':'');
  ts.textContent=hasAttentionState?'':_formatRelativeSessionTime(tsMs);
  titleRow.appendChild(title);
  // Project color dot: placed BETWEEN title and timestamp, not inside the
  // title span. Inside the title span it would be clipped by the ellipsis
  // truncation, becoming invisible exactly when the title is long enough
  // to need the project marker. As a flex-flow sibling it stays visible
  // regardless of title length and sits next to the timestamp on the right.
  if(s.project_id){
    const proj=sidebarStateBindings._allProjects.find(p=>p.project_id===s.project_id);
    if(proj){
      const dot=document.createElement('span');
      dot.className='session-project-dot';
      dot.style.background=proj.color||'var(--blue)';
      dot.title=proj.name;
      titleRow.appendChild(dot);
    }
  }
  const density=(window._sidebarDensity==='detailed'?'detailed':'compact');
  const showLineageMetadata=density==='detailed';
  const lineageKey=_sidebarLineageKeyForRow(s);
  const segmentCount=showLineageMetadata?_sessionSegmentCount(s):0;
  const needsLineageReport=showLineageMetadata?_lineageReportNeedsFetch(s,lineageKey,segmentCount):false;
  const lineageSegments=showLineageMetadata?_lineageSegmentsForRender(s,lineageKey,needsLineageReport):[];
  const lineageReportKey=showLineageMetadata?_lineageReportCacheKey(s,lineageKey):null;
  const canExpandLineageSegments=showLineageMetadata&&Boolean(lineageKey&&segmentCount>1&&(lineageSegments.length>0||needsLineageReport||_lineageReportInflight.has(lineageReportKey)));
  const lineageSegmentsExpanded=canExpandLineageSegments&&_expandedLineageKeys.has(lineageKey);
  if(lineageSegmentsExpanded&&needsLineageReport){
    _fetchLineageReportForRow(s,lineageKey).then(()=>renderSessionListFromCache());
  }
  if(segmentCount>0){
    const segmentCountEl=document.createElement('span');
    segmentCountEl.className='session-lineage-count'+(canExpandLineageSegments?' expandable':'');
    const segmentLabel=t('session_meta_segments', segmentCount);
    segmentCountEl.textContent=segmentLabel;
    segmentCountEl.title=_sessionLineageBadgeTooltip(segmentLabel,canExpandLineageSegments);
    if(canExpandLineageSegments){
      segmentCountEl.setAttribute('role','button');
      segmentCountEl.setAttribute('tabindex','0');
      segmentCountEl.setAttribute('aria-expanded',lineageSegmentsExpanded?'true':'false');
      ['pointerdown','pointerup','click'].forEach(ev=>segmentCountEl.addEventListener(ev,e=>e.stopPropagation()));
      const toggleLineageSegments=(e)=>{
        e.preventDefault();
        e.stopPropagation();
        if(_expandedLineageKeys.has(lineageKey)) _expandedLineageKeys.delete(lineageKey);
        else {
          _expandedLineageKeys.add(lineageKey);
          if(needsLineageReport) _fetchLineageReportForRow(s,lineageKey).then(()=>renderSessionListFromCache());
        }
        renderSessionListFromCache();
      };
      segmentCountEl.onclick=toggleLineageSegments;
      segmentCountEl.onkeydown=(e)=>{
        if(e.key==='Enter'||e.key===' '){toggleLineageSegments(e);}
      };
    }
    titleRow.appendChild(segmentCountEl);
  }
  const childCount=typeof s._child_session_count==='number'?s._child_session_count:(Array.isArray(s._child_sessions)?s._child_sessions.length:0);
  if(childCount>0){
    const childCountEl=document.createElement('span');
    childCountEl.className='session-child-count';
    const childLabel=t('session_meta_children', childCount);
    childCountEl.textContent=childLabel;
    childCountEl.title=_sessionChildBadgeTooltip(childLabel);
    ['pointerdown','pointerup','click'].forEach(ev=>childCountEl.addEventListener(ev,e=>e.stopPropagation()));
    childCountEl.onclick=(e)=>{
      e.stopPropagation();
      const key=_sidebarLineageKeyForRow(s);
      if(_expandedChildSessionKeys.has(key)) _expandedChildSessionKeys.delete(key);
      else _expandedChildSessionKeys.add(key);
      renderSessionListFromCache();
    };
    titleRow.appendChild(childCountEl);
  }
  if(s.is_cli_session||_isMessagingSession(s)){
    const chipLabel=_getChannelLabel(s)||'CLI';
    const chip=document.createElement('span');
    chip.className='session-source-chip';
    chip.textContent=chipLabel;
    chip.dataset.sourceKey=_sourceKeyForSession(s)||'cli';
    titleRow.appendChild(chip);
  }
  titleRow.appendChild(ts);
  sessionText.appendChild(titleRow);
  if(density==='detailed'){
    const metaBits=[];
    const msgCount=typeof s.message_count==='number'?s.message_count:0;
    const msgLabel=(typeof t==='function')
      ? t('session_meta_messages', msgCount)
      : `${msgCount} msg${msgCount===1?'':'s'}`;
    metaBits.push(msgLabel);
    if(childCount>0) metaBits.push(t('session_meta_children', childCount));
    const modelMeta=_formatSessionModelWithGateway(s);
    if(modelMeta) metaBits.push(modelMeta);
    const sourceLabel=_getChannelLabel(s);
    if(sourceLabel&&(s.is_cli_session||_isMessagingSession(s))) metaBits.push(sourceLabel);
    if(readOnly) metaBits.push('read-only');
    if(sidebarStateBindings._showAllProfiles&&s.profile) metaBits.push(s.profile);
    const meta=document.createElement('div');
    meta.className='session-meta';
    meta.textContent=metaBits.join(' · ');
    sessionText.appendChild(meta);
  }
  const contentPreview=titleMatched?'':_sessionSearchContentPreview(s,searchQueryRaw);
  if(contentPreview){
    const preview=document.createElement('div');
    preview.className='session-search-preview';
    preview.title=contentPreview;
    _appendHighlightedText(preview,contentPreview,searchQueryRaw,'session-search-hit session-search-hit-preview');
    sessionText.appendChild(preview);
  }
  if(lineageSegmentsExpanded){
    const lineageList=document.createElement('div');
    lineageList.className='session-lineage-segments';
    ['pointerdown','pointerup','click'].forEach(ev=>lineageList.addEventListener(ev,e=>e.stopPropagation()));
    const sortedSegments=[...lineageSegments].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
    for(const seg of sortedSegments){
      const row=document.createElement('button');
      row.type='button';
      row.className='session-lineage-segment'+(activeSidForSidebar&&seg.session_id===activeSidForSidebar?' active':'');
      const segTitle=_sessionDisplayTitle(seg)||t('session_lineage_segment_untitled');
      const segTime=_formatRelativeSessionTime(_sessionTimestampMs(seg));
      row.textContent=`-> ${segTitle} - ${segTime}`;
      row.title=t('session_lineage_segment_open');
      row.onclick=async(e)=>{
        e.stopPropagation();
        await _openSidebarSession(seg, {skipLineageResolve:true});
      };
      lineageList.appendChild(row);
    }
    sessionText.appendChild(lineageList);
  }
  if(childCount>0&&Array.isArray(s._child_sessions)&&(_expandedChildSessionKeys.has(lineageKey)||!!searchQueryRaw)){
    const childList=document.createElement('div');
    childList.className='session-child-sessions';
    ['pointerdown','pointerup','click','touchstart','touchmove','touchend','touchcancel'].forEach(ev=>childList.addEventListener(ev,e=>e.stopPropagation()));
    const sortedChildren=[...s._child_sessions].sort((a,b)=>_sessionTimestampMs(b)-_sessionTimestampMs(a));
    const openChildSession=async(childSession)=>{
      await _openSidebarSession(childSession, {skipLineageResolve:true});
    };
    const childLabelFor=(child)=>{
      const childTitle=_sessionDisplayTitle(child)||'Untitled child session';
      const childTime=_formatRelativeSessionTime(_sessionTimestampMs(child));
      const parentNote=child._parent_segment_title?` via ${child._parent_segment_title}`:'';
      return `-> ${childTitle}${parentNote} - ${childTime}`;
    };
    for(const child of sortedChildren){
      if(child.session_source==='fork'){
        const childIsActive=!!(activeSidForSidebar&&child.session_id===activeSidForSidebar);
        const childStreaming=_isSessionEffectivelyStreaming(child);
        const childHasUnread=_hasUnreadForSession(child)&&!childIsActive;
        const childAttention=_sessionAttentionState(child);
        const childAttentionClass=childAttention?(childAttention.kind==='approval'?' attention-approval':(childAttention.kind==='clarify'?' attention-clarify':' attention-attention')):'';
        const row=document.createElement('div');
        row.className='session-child-session session-child-session-fork'
          +(childIsActive?' active':'')
          +(childStreaming?' streaming':'')
          +(childHasUnread?' unread':'')
          +(childAttention?' needs-attention':'')
          +childAttentionClass;
        row.dataset.sid=child.session_id;
        if(sidebarStateBindings._sessionSelectMode&&!_isReadOnlySession(child)){
          const cbW=document.createElement('label');cbW.className='session-select-cb-wrapper';
          const cb=document.createElement('input');cb.type='checkbox';cb.className='session-select-cb';
          cb.dataset.sid=child.session_id;cb.checked=_selectedSessions.has(child.session_id);
          cb.onchange=(e)=>{e.stopPropagation();setSessionSelected(child.session_id,cb.checked);};
          cb.onclick=(e)=>{e.stopPropagation();};
          cb.onpointerup=(e)=>{e.stopPropagation();};
          cbW.onpointerup=(e)=>{e.stopPropagation();};
          cbW.onclick=(e)=>{e.stopPropagation();};
          cbW.appendChild(cb);
          row.classList.toggle('selected',_selectedSessions.has(child.session_id));
          row.appendChild(cbW);
        }
        const mainBtn=document.createElement('button');
        mainBtn.type='button';
        mainBtn.className='session-child-session-main'+(childIsActive?' active':'');
        mainBtn.textContent=childLabelFor(child);
        mainBtn.title='Open forked session';
        mainBtn.onclick=async(e)=>{
          if(row._skipNextChildOpen){
            row._skipNextChildOpen=false;
            e.stopPropagation();
            e.preventDefault();
            return;
          }
          e.stopPropagation();
          await openChildSession(child);
        };
        row._startRename=_buildSessionRenameStarter(child, mainBtn, ()=>{
          mainBtn.textContent=childLabelFor(child);
        });
        row.appendChild(mainBtn);
        const state=document.createElement('span');
        state.className='session-state-indicator session-child-session-state'
          +(childStreaming?' is-streaming':'')
          +(childHasUnread?' is-unread':'')
          +(childAttention?(childAttention.kind==='approval'?' is-attention-approval':(childAttention.kind==='clarify'?' is-attention-clarify':' is-attention-generic')):'');
        state.setAttribute('aria-hidden','true');
        const childStateTip=_sessionStateTooltip({isStreaming:childStreaming,hasUnread:childHasUnread});
        if(childAttention&&childAttention.title) state.title=childAttention.title;
        else if(childStateTip) state.title=childStateTip;
        row.appendChild(state);
        const readOnlyChild=_isReadOnlySession(child);
        let actions=null;
        if(!readOnlyChild){
          actions=document.createElement('div');
          actions.className='session-actions';
          const menuBtn=document.createElement('button');
          menuBtn.type='button';
          menuBtn.className='session-actions-trigger';
          menuBtn.title='Conversation actions';
          menuBtn.setAttribute('aria-haspopup','menu');
          menuBtn.setAttribute('aria-expanded','false');
          menuBtn.setAttribute('aria-label','Conversation actions');
          menuBtn.innerHTML=ICONS.more;
          const stopMenuPointer=(e)=>e.stopPropagation();
          menuBtn.onpointerdown=stopMenuPointer;
          menuBtn.onpointerup=stopMenuPointer;
          menuBtn.onclick=(e)=>{
            e.stopPropagation();
            e.preventDefault();
            _openSessionActionMenu(child, menuBtn);
          };
          actions.appendChild(menuBtn);
          row.appendChild(actions);
          row.append(
            _makeSessionSwipeAffordance('right',child.archived?'undo':'archive',child.archived?'Restore':t('session_batch_archive')),
            _makeSessionSwipeAffordance('left','trash-2',t('session_batch_delete')),
          );
          _installForkChildSwipe(row, child, actions, committedSwipeDuration, committedSwipeReflowDelay);
        }
        row.oncontextmenu=(e)=>{
          if(readOnlyChild) return;
          e.preventDefault();
          if(e.pointerType==='touch'||e.pointerType==='pen') return;
          e.stopPropagation();
          _openSessionActionMenu(child, actions||row);
        };
        childList.appendChild(row);
        continue;
      }
      const row=document.createElement('button');
      row.type='button';
      row.className='session-child-session'+(activeSidForSidebar&&child.session_id===activeSidForSidebar?' active':'');
      row.textContent=childLabelFor(child);
      row.title='Open child session';
      row.onclick=async(e)=>{
        e.stopPropagation();
        await openChildSession(child);
      };
      childList.appendChild(row);
    }
    sessionText.appendChild(childList);
  }
  // Append tag chips after the title text
  for(const tag of tags){
    const chip=document.createElement('span');
    chip.className='session-tag';
    chip.textContent=tag;
    chip.title='Click to filter by '+tag;
    chip.onclick=(e)=>{
      e.stopPropagation();
      const searchBox=$('sessionSearch');
      if(searchBox){searchBox.value=tag;filterSessions();}
    };
    title.appendChild(chip);
  }

  // Rename: called directly when we confirm it's a double-click
  const startRename=_buildSessionRenameStarter(
    s,
    title,
    (nextTitle)=>{
      title.textContent=nextTitle;
      title.title=_sessionFullTitleTooltip(nextTitle,nextTitle,s);
    }
  );
  // Expose the rename closure on the row so the three-dot action menu
  // (`_openSessionActionMenu`, defined elsewhere) can trigger it without
  // needing a separate DOM hunt or a duplicate copy of all this state
  // (oldTitle / applyTitle / finish / _renamingSid bookkeeping). The
  // double-click path on this element still calls startRename() directly.
  el._startRename = startRename;
  el.dataset.sid = s.session_id;

  // (Project dot is appended above, between title and timestamp, so it
  // sits outside the truncating title span and stays visible.)
  el.appendChild(sessionText);
  const state=document.createElement('span');
  const attentionDotClass=attention?(attention.kind==='approval'?' is-attention-approval':(attention.kind==='clarify'?' is-attention-clarify':' is-attention-generic')):'';
  state.className='session-attention-indicator session-state-indicator'+(isStreaming?' is-streaming':(hasUnread?' is-unread':''))+attentionDotClass;
  state.setAttribute('aria-hidden','true');
  // Tooltip precedence: a localized attention title (pending approval/clarify,
  // from the attention-indicator feature) is more specific and actionable than
  // the generic running/unread state tooltip, so it wins. Fall back to the state
  // tooltip only when there is no attention title AND the state tooltip is
  // non-empty — never blank an otherwise-meaningful tooltip.
  const _stateTip=_sessionStateTooltip({isStreaming,hasUnread});
  if(attention&&attention.title) state.title=attention.title;
  else if(_stateTip) state.title=_stateTip;
  el.appendChild(state);
  // Single trigger button that opens a shared dropdown menu
  let actions=null;
  if(!readOnly){
    actions=document.createElement('div');
    actions.className='session-actions';
    const menuBtn=document.createElement('button');
    menuBtn.type='button';
    menuBtn.className='session-actions-trigger';
    menuBtn.title='Conversation actions';
    menuBtn.setAttribute('aria-haspopup','menu');
    menuBtn.setAttribute('aria-expanded','false');
    menuBtn.setAttribute('aria-label','Conversation actions');
    menuBtn.innerHTML=ICONS.more;
    const stopMenuPointer=(e)=>e.stopPropagation();
    menuBtn.onpointerdown=stopMenuPointer;
    menuBtn.onpointerup=stopMenuPointer;
    menuBtn.onclick=(e)=>{
      e.stopPropagation();
      e.preventDefault();
      _openSessionActionMenu(s, menuBtn);
    };
    actions.appendChild(menuBtn);
    el.appendChild(actions);
  }
  el.oncontextmenu=(e)=>{
    if(readOnly) return;
    e.preventDefault();
    if(e.pointerType==='touch'||e.pointerType==='pen') return;
    e.stopPropagation();
    clearTimeout(_tapTimer);
    _tapTimer=null;
    _lastTapTime=0;
    _clearPointerDragState();
    _openSessionActionMenu(s, actions||el);
  };

  if(!readOnly){
    el.append(
      _makeSessionSwipeAffordance('right',s.archived?'undo':'archive',s.archived?'Restore':t('session_batch_archive')),
      _makeSessionSwipeAffordance('left','trash-2',t('session_batch_delete')),
    );
  }

  _installSessionRowGestures(el, s, actions, readOnly, committedSwipeDuration, committedSwipeReflowDelay, startRename);
  return el;
}

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
  const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,sessionDiscoveryBindings._contentSearchResults);
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
  // Project filter bar — show when there are real projects OR there are
  // unassigned sessions (so the Unassigned chip has something to filter to).
  const hasUnprojected=profileFiltered.some(s=>!s.project_id);
  if(sidebarStateBindings._allProjects.length>0||hasUnprojected){
    const bar=document.createElement('div');
    bar.className='project-bar';
    // "All" chip
    const allChip=document.createElement('span');
    allChip.className='project-chip'+(!sidebarStateBindings._activeProject?' active':'');
    allChip.textContent='All';
    allChip.onclick=()=>{_setActiveProjectFilter(null);};
    bar.appendChild(allChip);
    // "Unassigned" chip — only when there are sessions with no project to
    // filter to. Hidden in the common case where every session is already
    // organized, to keep the chip bar uncluttered.
    if(hasUnprojected){
      const noneChip=document.createElement('span');
      noneChip.className='project-chip no-project'+(sidebarStateBindings._activeProject===NO_PROJECT_FILTER?' active':'');
      noneChip.textContent='Unassigned';
      noneChip.title='Show conversations not yet assigned to a project';
      noneChip.onclick=()=>{_setActiveProjectFilter(NO_PROJECT_FILTER);};
      bar.appendChild(noneChip);
    }
    // Project chips
    for(const p of sidebarStateBindings._allProjects){
      const chip=document.createElement('span');
      chip.className='project-chip'+(p.project_id===sidebarStateBindings._activeProject?' active':'');
      if(p.color){
        const dot=document.createElement('span');
        dot.className='color-dot';
        dot.style.background=p.color;
        chip.appendChild(dot);
      }
      const nameSpan=document.createElement('span');
      nameSpan.textContent=p.name;
      chip.appendChild(nameSpan);
      let _pClickTimer=null;
      chip.onclick=(e)=>{
        clearTimeout(_pClickTimer);
        _pClickTimer=setTimeout(()=>{_pClickTimer=null;_setActiveProjectFilter(p.project_id);},220);
      };
      chip.ondblclick=(e)=>{e.stopPropagation();clearTimeout(_pClickTimer);_pClickTimer=null;_startProjectRename(p,chip);};
      chip.oncontextmenu=(e)=>{e.preventDefault();_showProjectContextMenu(e,p,chip);};
      // Touch long-press → context menu (mobile UX: project chips can only be
      // deleted via the right-click menu, which has no touch equivalent).
      let _lpTimer=null;
      let _lpHandled=false;
      let _lpStartX=0,_lpStartY=0;
      chip.addEventListener('touchstart',(e)=>{
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        // Clear any in-flight timer before scheduling a new one, mirroring the
        // session-item long-press path (_clearLongPressTimer). Without this a
        // second finger / stray touchstart orphans the prior timer, which then
        // fires unsuppressed ~500ms later and pops the menu after the gesture
        // was cancelled.
        if(_lpTimer){clearTimeout(_lpTimer);_lpTimer=null;}
        _lpHandled=false;_lpStartX=t.clientX;_lpStartY=t.clientY;
        chip.classList.add('long-pressing');
        _lpTimer=setTimeout(()=>{
          _lpTimer=null;
          if(_lpHandled) return;  // already consumed by another gesture — stale fire is a no-op
          _lpHandled=true;
          chip.classList.remove('long-pressing');
          clearTimeout(_pClickTimer);_pClickTimer=null;
          const syn={clientX:t.clientX,clientY:t.clientY,preventDefault:()=>{}};
          _showProjectContextMenu(syn,p,chip);
        },500);
      },{passive:true});
      chip.addEventListener('touchmove',(e)=>{
        if(!_lpTimer) return;
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        if(Math.abs(t.clientX-_lpStartX)>10||Math.abs(t.clientY-_lpStartY)>10){
          clearTimeout(_lpTimer);_lpTimer=null;
          chip.classList.remove('long-pressing');
        }
      },{passive:true});
      chip.addEventListener('touchend',(e)=>{
        clearTimeout(_lpTimer);_lpTimer=null;
        chip.classList.remove('long-pressing');
        if(_lpHandled){e.preventDefault();e.stopPropagation();}
      },{passive:false});
      chip.addEventListener('touchcancel',()=>{
        clearTimeout(_lpTimer);_lpTimer=null;_lpHandled=false;
        chip.classList.remove('long-pressing');
      },{passive:true});
      if(window._projectQuickCreate) _attachProjectQuickCreateButton(chip,p);
      bar.appendChild(chip);
    }
    // Create button
    const addBtn=document.createElement('button');
    addBtn.className='project-create-btn';
    addBtn.textContent='+';
    addBtn.title='New project';
    addBtn.onclick=(e)=>{e.stopPropagation();_startProjectCreate(bar,addBtn);};
    bar.appendChild(addBtn);
    list.appendChild(bar);
  }
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

registerSidebarRenderer(renderSessionListFromCache);



export const sidebarRendering=Object.freeze({renderRow:_renderOneSession,renderList:renderSessionListFromCache});

export { renderSessionListFromCache };
