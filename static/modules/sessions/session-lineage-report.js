import {
  _authoritativeLineageTipId,
  _collapseSessionLineageForSidebar,
  _isChildSession,
  _sidebarLineageKeyForRow,
} from './session-lineage.js';
import {
  _lineageReportCache,
  _lineageReportInflight,
  sidebarStateBindings,
} from './sidebar-store.js';

function _clearLineageReportCache(){
  _lineageReportCache.clear();
  _lineageReportInflight.clear();
  sidebarStateBindings._lineageReportCacheGeneration++;
}

function _lineageReportCacheKey(s,lineageKey){
  const key=lineageKey||_sidebarLineageKeyForRow(s)||null;
  const tip=_authoritativeLineageTipId(s);
  return key&&tip&&tip!==key?`${key}::${tip}`:key;
}

function _pruneLineageReportCacheToVisibleSessions(sessions){
  const visibleKeys=new Set();
  const rows=Array.isArray(sessions)?sessions:[];
  for(const s of rows){
    const key=_sidebarLineageKeyForRow(s);
    if(key) visibleKeys.add(key);
  }
  try{
    for(const row of _collapseSessionLineageForSidebar(rows)){
      if(!row||_isChildSession(row)) continue;
      const key=_lineageReportCacheKey(row,_sidebarLineageKeyForRow(row));
      if(key) visibleKeys.add(key);
    }
  }catch(_){ /* cache pruning must never prevent a session-list apply */ }
  for(const key of Array.from(_lineageReportCache.keys())){
    if(!visibleKeys.has(key)) _lineageReportCache.delete(key);
  }
  for(const key of Array.from(_lineageReportInflight.keys())){
    if(!visibleKeys.has(key)) _lineageReportInflight.delete(key);
  }
}

function _lineageLocalSegmentCount(s){
  if(!s) return 0;
  if(Array.isArray(s._lineage_segments)) return s._lineage_segments.length;
  return s.session_id?1:0;
}

function _lineageReportNeedsFetch(s,lineageKey,segmentCount){
  const key=_lineageReportCacheKey(s,lineageKey);
  if(!s||!s.session_id||!key) return false;
  const cached=_lineageReportCache.get(key);
  const expectedCount=Number(segmentCount||0);
  if(cached){
    const cachedCount=Array.isArray(cached.segments)?cached.segments.length:0;
    if(!cached.error&&expectedCount>0&&cachedCount>0&&cachedCount!==expectedCount){
      _lineageReportCache.delete(key);
    } else {
      return false;
    }
  }
  if(_lineageReportInflight.has(key)) return false;
  return expectedCount>_lineageLocalSegmentCount(s);
}

function _lineageSegmentsForRender(s,lineageKey,skipCached){
  const segments=[];
  const seen=new Set();
  const currentSid=s&&s.session_id;
  const addSegment=(seg)=>{
    if(!seg||!seg.session_id||seg.session_id===currentSid||seen.has(seg.session_id)) return;
    if(seg.role==='child_session') return;
    seen.add(seg.session_id);
    segments.push({...seg});
  };
  for(const seg of (Array.isArray(s&&s._lineage_segments)?s._lineage_segments:[])) addSegment(seg);
  if(!skipCached){
    const cached=_lineageReportCache.get(_lineageReportCacheKey(s,lineageKey));
    if(cached&&Array.isArray(cached.segments)){
      for(const seg of cached.segments) addSegment(seg);
    }
  }
  return segments;
}

function _fetchLineageReportForRow(s,lineageKey){
  const key=_lineageReportCacheKey(s,lineageKey);
  if(!s||!s.session_id||!key) return Promise.resolve(null);
  if(_lineageReportCache.has(key)) return Promise.resolve(_lineageReportCache.get(key));
  if(_lineageReportInflight.has(key)) return _lineageReportInflight.get(key);
  const generation=sidebarStateBindings._lineageReportCacheGeneration;
  let request;
  request=api('/api/session/lineage/report?session_id='+encodeURIComponent(s.session_id))
    .then(report=>{
      if(generation===sidebarStateBindings._lineageReportCacheGeneration&&_lineageReportInflight.get(key)===request){
        _lineageReportCache.set(key,(report&&report.found!==false)?report:{error:true});
      }
      return report;
    })
    .catch(err=>{
      console.warn('lineage report',err);
      if(generation===sidebarStateBindings._lineageReportCacheGeneration&&_lineageReportInflight.get(key)===request){
        _lineageReportCache.set(key,{error:true});
      }
      return null;
    })
    .finally(()=>{
      if(_lineageReportInflight.get(key)===request) _lineageReportInflight.delete(key);
    });
  _lineageReportInflight.set(key,request);
  return request;
}

export {
  _clearLineageReportCache,
  _fetchLineageReportForRow,
  _lineageReportCacheKey,
  _lineageReportNeedsFetch,
  _lineageSegmentsForRender,
  _pruneLineageReportCacheToVisibleSessions,
};
