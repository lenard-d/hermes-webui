import { SESSION_ICONS as ICONS, _formatSessionModelWithGateway, _sessionDisplayTitle, _sessionTitleTags } from './session-display.js';
import { _isSessionEffectivelyStreaming, _rememberRenderedSessionSnapshot, _rememberRenderedStreamingState } from './session-run-state.js';
import { _hasUnreadForSession } from './session-unread.js';
import { _getChannelLabel, _isMessagingSession, _sourceKeyForSession } from './session-source.js';
import { _isReadOnlySession, _openSidebarSession } from './sidebar-session-opening.js';
import { _openSessionActionMenu } from './sidebar-actions.js';
import { _buildSessionRenameStarter } from './session-rename.js';
import { _makeSessionSwipeAffordance } from './sidebar-motion.js';
import { setSessionSelected } from './sidebar-selection.js';
import { _expandedChildSessionKeys, _expandedLineageKeys, _lineageReportInflight, _selectedSessions, _sessionSwipeReturnOffsets, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _fetchLineageReportForRow, _lineageReportCacheKey, _lineageReportNeedsFetch, _lineageSegmentsForRender } from './session-lineage-report.js';
import { _sessionLineageContainsSession, _sessionSegmentCount, _sidebarLineageKeyForRow } from './session-lineage.js';
import { _sessionChildBadgeTooltip, _sessionForkTooltip, _sessionFullTitleTooltip, _sessionLineageBadgeTooltip, _sessionStateTooltip, _sessionTitleForForkParent, _truncatedSessionId } from './session-row-labels.js';
import { _appendHighlightedText, _sessionSearchContentPreview, filterSessions } from './session-search.js';
import { _formatRelativeSessionTime, _sessionTimestampMs } from './session-time.js';
import { _installForkChildSwipe, _installSessionRowGestures } from './sidebar-row-gestures.js';
import { _sessionAttentionState } from './sidebar-row-state.js';

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
  let gestureController=null;
  el.oncontextmenu=(e)=>{
    if(readOnly) return;
    e.preventDefault();
    if(e.pointerType==='touch'||e.pointerType==='pen') return;
    e.stopPropagation();
    if(gestureController) gestureController.cancelPendingNavigation();
    _openSessionActionMenu(s, actions||el);
  };

  if(!readOnly){
    el.append(
      _makeSessionSwipeAffordance('right',s.archived?'undo':'archive',s.archived?'Restore':t('session_batch_archive')),
      _makeSessionSwipeAffordance('left','trash-2',t('session_batch_delete')),
    );
  }

  gestureController=_installSessionRowGestures(el, s, actions, readOnly, committedSwipeDuration, committedSwipeReflowDelay, startRename);
  return el;
}

export const sidebarRowPresentation=Object.freeze({render:_renderOneSession});

export { _renderOneSession };
