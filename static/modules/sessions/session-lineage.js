import { sidebarStateBindings } from './sidebar-store.js';
import { _sessionTimestampMs } from './session-time.js';

function _isChildSession(s){
  return !!(s&&s.parent_session_id&&s.relationship_type==='child_session');
}

function _isForkWithResolvableParent(s, sessionIdsInList){
  return !!(s&&s.session_source==='fork'&&s.parent_session_id&&sessionIdsInList&&sessionIdsInList.has(s.parent_session_id));
}

function _sessionLineageKey(s, sessionIdsInList, sessionsById){
  if(!s||!s.session_id) return null;
  if(_isChildSession(s)||s.session_source==='fork') return null;
  const lineageKey=s._lineage_root_id||s.lineage_root_id||null;
  if(lineageKey) return lineageKey;
  const parent=s.parent_session_id&&sessionsById?sessionsById.get(s.parent_session_id):null;
  if(s.pre_compression_snapshot||parent&&parent.pre_compression_snapshot){
    let root=s;
    const seen=new Set();
    while(root&&root.parent_session_id&&sessionsById&&sessionsById.has(root.parent_session_id)&&!seen.has(root.parent_session_id)){
      const next=sessionsById.get(root.parent_session_id);
      if(!next||_isChildSession(next)||next.session_source==='fork'||!(root.pre_compression_snapshot||next.pre_compression_snapshot)) break;
      seen.add(root.session_id);
      root=next;
    }
    return root&&root.session_id||s.parent_session_id||s.session_id;
  }
  if(s.parent_session_id&&sessionIdsInList&&sessionIdsInList.has(s.parent_session_id)) return null;
  return s.parent_session_id||null;
}

function _sessionLineageContainsSession(s, sid){
  if(!s||!sid) return false;
  if(s.session_id===sid) return true;
  if(Array.isArray(s._lineage_segments)&&s._lineage_segments.some(seg=>seg&&seg.session_id===sid)) return true;
  if(Array.isArray(s._child_sessions)&&s._child_sessions.some(child=>child&&child.session_id===sid)) return true;
  return false;
}

function _authoritativeLineageTipId(s){
  if(!s) return null;
  return s._lineage_tip_id||s._parent_lineage_tip_id||null;
}

function _sidebarLineageKeyForRow(s){
  if(!s) return null;
  if(s.session_source==='fork') return s.session_id||s.parent_session_id||null;
  return s._lineage_key||s._lineage_root_id||s.lineage_root_id||s.parent_session_id||s.session_id||null;
}

function _collapseSessionLineageForSidebar(sessions){
  const result=[];
  const sessionIdsInList=new Set((sessions||[]).map(s=>s.session_id));
  const sessionsById=new Map((sessions||[]).filter(s=>s&&s.session_id).map(s=>[s.session_id,s]));
  const groups=new Map();
  for(const s of sessions||[]){
    const key=_sessionLineageKey(s, sessionIdsInList, sessionsById);
    if(!key){result.push(s);continue;}
    if(!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(s);
  }
  for(const [key,items] of groups.entries()){
    if(items.length<=1){result.push(items[0]);continue;}
    const sorted=[...items].sort((a,b)=>{
      const bSeg=Number(b&&b._compression_segment_count||0);
      const aSeg=Number(a&&a._compression_segment_count||0);
      if((bSeg||aSeg)&&bSeg!==aSeg) return bSeg-aSeg;
      const bSnapshot=!!(b&&b.pre_compression_snapshot);
      const aSnapshot=!!(a&&a.pre_compression_snapshot);
      if(bSnapshot!==aSnapshot) return aSnapshot-bSnapshot;
      return _sessionTimestampMs(b)-_sessionTimestampMs(a);
    });
    const tipIds=new Set(items.map(_authoritativeLineageTipId).filter(Boolean));
    const chosen=sorted.find(item=>tipIds.has(item&&item.session_id))||sorted[0];
    result.push({...chosen,_lineage_key:key,_lineage_collapsed_count:items.length,_lineage_segments:sorted});
  }
  return result;
}

function _resolveSessionIdFromSidebarLineage(sid){
  sid=String(sid||'').trim();
  if(!sid||!Array.isArray(sidebarStateBindings._allSessions)||!sidebarStateBindings._allSessions.length) return sid||null;
  const visibleRows=_collapseSessionLineageForSidebar(sidebarStateBindings._allSessions).filter(row=>row&&!_isChildSession(row));
  if(visibleRows.some(row=>row&&row.session_id===sid)) return sid;
  const candidates=[];
  for(const row of visibleRows){
    if(!row||!row.session_id||row.relationship_type==='child_session') continue;
    const lineageLike=!!(
      row._lineage_key||row._lineage_root_id||row.lineage_root_id||
      row._compression_segment_count||row.pre_compression_snapshot||
      (Array.isArray(row._lineage_segments)&&row._lineage_segments.length>1)
    );
    if(!lineageLike) continue;
    const key=_sidebarLineageKeyForRow(row);
    if(key===sid||row.parent_session_id===sid||row._lineage_root_id===sid||row.lineage_root_id===sid||_sessionLineageContainsSession(row,sid)){
      candidates.push(row);
    }
  }
  if(!candidates.length) return sid;
  candidates.sort((a,b)=>{
    const bSeg=Number(b&&b._compression_segment_count||b&&b._lineage_collapsed_count||0);
    const aSeg=Number(a&&a._compression_segment_count||a&&a._lineage_collapsed_count||0);
    if(bSeg!==aSeg) return bSeg-aSeg;
    const bSnapshot=!!(b&&b.pre_compression_snapshot);
    const aSnapshot=!!(a&&a.pre_compression_snapshot);
    if(bSnapshot!==aSnapshot) return aSnapshot-bSnapshot;
    return _sessionTimestampMs(b)-_sessionTimestampMs(a);
  });
  return candidates[0].session_id||sid;
}

function _sessionSegmentCount(s){
  if(!s) return 0;
  const counts=[];
  if(typeof s._lineage_collapsed_count==='number') counts.push(s._lineage_collapsed_count);
  if(typeof s._compression_segment_count==='number') counts.push(s._compression_segment_count);
  if(Array.isArray(s._lineage_segments)) counts.push(s._lineage_segments.length);
  const count=Math.max(0,...counts.map(n=>Number.isFinite(n)?n:0));
  return count>1?count:0;
}

export {
  _authoritativeLineageTipId,
  _collapseSessionLineageForSidebar,
  _isChildSession,
  _isForkWithResolvableParent,
  _resolveSessionIdFromSidebarLineage,
  _sessionLineageContainsSession,
  _sessionLineageKey,
  _sessionSegmentCount,
  _sidebarLineageKeyForRow,
};
