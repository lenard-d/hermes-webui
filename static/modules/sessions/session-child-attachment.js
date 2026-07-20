import { _isExternalSession, _isMessagingSession } from './session-source.js';
import { _sessionDisplayTitle } from './session-display.js';
import {
  _isChildSession,
  _isForkWithResolvableParent,
  _sidebarLineageKeyForRow,
} from './session-lineage.js';
import { _isSessionEffectivelyStreaming } from './session-run-state.js';
import { _hasUnreadForSession } from './session-unread.js';
import {
  _expandedChildSessionKeys,
  _expandedLineageKeys,
  sidebarStateBindings,
} from './sidebar-store.js';

function _attachChildSessionsToSidebarRows(collapsedRows, rawSessions, rawReferenceSessions){
  const referenceSessions=Array.isArray(rawReferenceSessions)?rawReferenceSessions:(rawSessions||[]);
  const sessionIdsInList=new Set(referenceSessions.map(s=>s&&s.session_id).filter(Boolean));
  const rawSessionsById=new Map(referenceSessions.filter(s=>s&&s.session_id).map(s=>[s.session_id,s]));
  const cleanSidebarRow=(s)=>{
    const row={...s};
    delete row._child_sessions;
    delete row._child_session_count;
    delete row._child_session_streaming;
    delete row._child_session_has_unread;
    delete row._child_session_attention;
    delete row._child_session_latest_at;
    delete row._sidebar_activity_at;
    return row;
  };
  const rows=(collapsedRows||[])
    .filter(s=>!_isChildSession(s)&&((s&&s.pinned)||!_isForkWithResolvableParent(s, sessionIdsInList)))
    .map(cleanSidebarRow);
  const isChildStreaming=(childRow)=>typeof _isSessionEffectivelyStreaming==='function'
    ? _isSessionEffectivelyStreaming(childRow)
    : !!(childRow&&(childRow.active_stream_id||childRow.pending_user_message));
  const childHasUnread=(childRow)=>typeof _hasUnreadForSession==='function'
    ? _hasUnreadForSession(childRow)
    : !!(childRow&&childRow.has_unread);
  const bubbleSidebarState=(parentRow, childRow)=>{
    if(isChildStreaming(childRow)) parentRow._child_session_streaming=true;
    if(childHasUnread(childRow)) parentRow._child_session_has_unread=true;
    const childActivityRaw=childRow
      ? (childRow._sidebar_activity_at??childRow.last_message_at??childRow.updated_at??childRow.created_at??0)
      : 0;
    const childActivitySec=Number(childActivityRaw);
    if(Number.isFinite(childActivitySec)&&childActivitySec>Number(parentRow._child_session_latest_at||0)){
      parentRow._child_session_latest_at=childActivitySec;
    }
    const childAttention=childRow&&childRow.attention&&typeof childRow.attention==='object'?childRow.attention:null;
    if(!childAttention||!childAttention.kind||!Number.isFinite(Number(childAttention.count))||Number(childAttention.count)<=0) return;
    const priorityFor=(kind)=>kind==='approval'?3:(kind==='clarify'?2:1);
    const current=parentRow._child_session_attention&&typeof parentRow._child_session_attention==='object'
      ? parentRow._child_session_attention
      : null;
    const nextPriority=priorityFor(String(childAttention.kind));
    const currentPriority=current?priorityFor(String(current.kind)):0;
    if(!current||nextPriority>currentPriority||(nextPriority===currentPriority&&Number(childAttention.count||0)>Number(current.count||0))){
      parentRow._child_session_attention={...childAttention};
    }
  };
  const visibleBySid=new Map();
  const visibleBySegmentSid=new Map();
  const visibleByLineageKey=new Map();
  const attachDepthCache=new Map();
  const attachDepthFor=(session, seen=new Set())=>{
    if(!session||!session.session_id) return 0;
    if(attachDepthCache.has(session.session_id)) return attachDepthCache.get(session.session_id);
    if(seen.has(session.session_id)) return 0;
    seen.add(session.session_id);
    const parent=session.parent_session_id&&rawSessionsById.get(session.parent_session_id);
    let depth=0;
    if(parent&&(_isChildSession(session)||(_isForkWithResolvableParent(session, sessionIdsInList)&&!(session&&session.pinned)))){
      depth=1+attachDepthFor(parent, seen);
    }
    attachDepthCache.set(session.session_id, depth);
    return depth;
  };
  for(const row of rows){
    if(row&&row.session_id) visibleBySid.set(row.session_id,row);
    const lineageKey=_sidebarLineageKeyForRow(row);
    if(lineageKey&&!visibleByLineageKey.has(lineageKey)) visibleByLineageKey.set(lineageKey,row);
    for(const seg of (Array.isArray(row._lineage_segments)?row._lineage_segments:[])){
      if(seg&&seg.session_id) visibleBySegmentSid.set(seg.session_id,{row,seg});
    }
  }
  const hiddenArchivedChildTree=new Set();
  const archivedRowsVisible=typeof sidebarStateBindings._showArchived!=='undefined'&&!!sidebarStateBindings._showArchived;
  const hasHiddenArchivedAncestor=(session)=>{
    if(!session||!session.session_id||archivedRowsVisible) return false;
    const seen=new Set();
    let parentSid=session.parent_session_id;
    while(parentSid){
      if(hiddenArchivedChildTree.has(parentSid)) return true;
      if(seen.has(parentSid)) break;
      seen.add(parentSid);
      const rawParent=rawSessionsById.get(parentSid);
      if(!rawParent) break;
      if(rawParent.archived) return true;
      parentSid=rawParent.parent_session_id;
    }
    return false;
  };
  const orphans=[];
  const renderableChildIds=new Set((rawSessions||[]).map(s=>s&&s.session_id).filter(Boolean));
  const attachQueueById=new Map();
  for(const candidate of [...(rawSessions||[]),...(referenceSessions||[])]){
    if(candidate&&candidate.session_id&&!attachQueueById.has(candidate.session_id)) attachQueueById.set(candidate.session_id,candidate);
  }
  const attachQueue=[...attachQueueById.values()].sort((a,b)=>attachDepthFor(a)-attachDepthFor(b));
  for(const child of attachQueue){
    const childRenderable=!!(child&&child.session_id&&renderableChildIds.has(child.session_id));
    if(child&&child.session_id&&visibleBySid.has(child.session_id)) continue;
    const isForkChild=_isForkWithResolvableParent(child, sessionIdsInList)&&!(child&&child.pinned);
    const childLineageKey=child&&(child._lineage_root_id||child.lineage_root_id||child.parent_session_id);
    const isHiddenLineageReferenceChild=!!(child&&child.archived&&child.parent_session_id&&childLineageKey&&!child.pinned&&!childRenderable);
    if(!_isChildSession(child)&&!isForkChild&&!isHiddenLineageReferenceChild) continue;
    const parentSid=child.parent_session_id;
    let parentRow=visibleBySid.get(parentSid);
    let parentSegment=null;
    if(!parentRow&&visibleBySegmentSid.has(parentSid)){
      const resolved=visibleBySegmentSid.get(parentSid);
      parentRow=resolved.row;
      parentSegment=resolved.seg;
    }
    if(!parentRow&&child._parent_lineage_tip_id){
      parentRow=visibleBySid.get(child._parent_lineage_tip_id)||null;
    }
    if(!parentRow&&child._parent_lineage_root_id){
      parentRow=visibleByLineageKey.get(child._parent_lineage_root_id)||null;
    }
    if(!parentRow){
      parentRow=visibleByLineageKey.get(childLineageKey||parentSid)||null;
    }
    if(!parentRow&&hasHiddenArchivedAncestor(child)){
      hiddenArchivedChildTree.add(child.session_id);
      continue;
    }
    const parentSourceMarker=String(parentRow&&(
      parentRow.session_source||parentRow.raw_source||parentRow.source_tag||parentRow.source
    )||'').toLowerCase();
    const parentIsExternal=parentRow&&(
      (typeof _isExternalSession==='function'&&_isExternalSession(parentRow))||
      (typeof _isMessagingSession==='function'&&_isMessagingSession(parentRow))||
      parentRow.is_cli_session===true||
      parentRow.session_source==='messaging'||
      (parentSourceMarker&&parentSourceMarker!=='webui'&&parentSourceMarker!=='subagent'&&parentSourceMarker!=='other'&&parentSourceMarker!=='fork')
    );
    if(parentRow&&child._cross_surface_child_session&&parentIsExternal){
      if(childRenderable) orphans.push({...child,_orphan_child_session:true});
      continue;
    }
    if(parentRow){
      const childCopy={...child};
      if(parentSegment){
        childCopy._parent_segment_id=parentSegment.session_id;
        childCopy._parent_segment_title=_sessionDisplayTitle(parentSegment)||child.parent_title||'Untitled';
      }
      if(childRenderable&&!isHiddenLineageReferenceChild){
        if(!Array.isArray(parentRow._child_sessions)) parentRow._child_sessions=[];
        parentRow._child_sessions.push(childCopy);
        parentRow._child_session_count=parentRow._child_sessions.length;
      }
      bubbleSidebarState(parentRow, childCopy);
      visibleBySegmentSid.set(childCopy.session_id,{row: parentRow, seg: childCopy});
    } else if(childRenderable) {
      if(child&&child._cross_surface_child_session&&_isChildSession(child)) continue;
      orphans.push({...child,_orphan_child_session:true});
    }
  }
  return [...rows,...orphans];
}

function _syncSidebarExpansionForActiveSession(rows, activeSid){
  if(!activeSid) return;
  for(const row of rows||[]){
    const key=_sidebarLineageKeyForRow(row);
    if(!key) continue;
    if(Array.isArray(row._child_sessions)&&row._child_sessions.some(child=>child&&child.session_id===activeSid)){
      _expandedChildSessionKeys.add(key);
    }
    if(Array.isArray(row._lineage_segments)&&row._lineage_segments.some(seg=>seg&&seg.session_id===activeSid&&seg.session_id!==row.session_id)){
      _expandedLineageKeys.add(key);
    }
  }
}

export { _attachChildSessionsToSidebarRows, _syncSidebarExpansionForActiveSession };
