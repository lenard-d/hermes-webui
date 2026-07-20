import { _hasUnreadForSession, _isSessionEffectivelyStreaming } from './state.js';
import { _isExternalSession, _isMessagingSession, _isReadOnlySession } from './message-loading.js';
import { _expandedChildSessionKeys, _expandedLineageKeys, _lineageReportCache, _lineageReportInflight, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionList } from './session-list-render-port.js';
import { _sessionDisplayTitle } from './session-display.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

let _searchDebounceTimer = null;
let _contentSearchResults = [];  // results from /api/sessions/search content scan
let _lastSessionSearchQuery = '';
let _hideSearchPreviewsAfterSelect = false;
let _archivedSearchPagingQueryActive = false;
let _serverTimeDelta = 0;       // ms offset: client clock - server clock (for clock-skew compensation)
let _serverTz = '';              // server timezone offset string (e.g. "+0800", "+0000", "-0500")

function _sessionSearchRanges(text, query){
  const source=String(text||'');
  const q=String(query||'').trim();
  if(!source||!q) return [];
  const lower=source.toLowerCase();
  const full=q.toLowerCase();
  const ranges=[];
  const collect=(needle)=>{
    if(!needle) return;
    let from=0;
    while(from<lower.length){
      const idx=lower.indexOf(needle,from);
      if(idx<0) break;
      const end=idx+needle.length;
      if(!ranges.some(r=>idx<r.end&&end>r.start)) ranges.push({start:idx,end});
      from=Math.max(end,idx+1);
    }
  };
  collect(full);
  if(!ranges.length&&/\s/.test(full)){
    const seen=new Set();
    full.split(/\s+/).filter(Boolean).sort((a,b)=>b.length-a.length).forEach(token=>{
      if(seen.has(token)) return;
      seen.add(token);
      collect(token);
    });
  }
  return ranges.sort((a,b)=>a.start-b.start);
}

function _appendHighlightedText(parent, text, query, highlightClass){
  const source=String(text||'');
  const ranges=_sessionSearchRanges(source,query);
  if(!ranges.length){
    parent.appendChild(document.createTextNode(source));
    return ranges;
  }
  let pos=0;
  for(const r of ranges){
    if(r.start>pos) parent.appendChild(document.createTextNode(source.slice(pos,r.start)));
    const mark=document.createElement('span');
    mark.className=highlightClass||'session-search-hit';
    mark.textContent=source.slice(r.start,r.end);
    parent.appendChild(mark);
    pos=r.end;
  }
  if(pos<source.length) parent.appendChild(document.createTextNode(source.slice(pos)));
  return ranges;
}

function _sessionSearchContentPreview(session, query){
  if(!session||!query||_hideSearchPreviewsAfterSelect) return '';
  if(session.match_type!=='content') return '';
  const preview=String(session.match_preview||'').replace(/\s+/g,' ').trim();
  return preview||'';
}

function _sessionSearchAddIdCandidate(candidates, seen, value){
  const raw=String(value||'').trim();
  if(!raw) return;
  const add=(candidate)=>{
    const sid=String(candidate||'').trim();
    if(!sid||seen.has(sid)) return;
    seen.add(sid);
    candidates.push(sid);
  };
  add(raw);
  try{add(decodeURIComponent(raw));}catch(_e){}
}

function _sessionSearchCleanUrlToken(token){
  let value=String(token||'').trim();
  value=value.replace(/[\],.;]+$/g,'');
  while(value.endsWith(')')&&value.indexOf('(')<0) value=value.slice(0,-1);
  return value;
}

function _sessionSearchSessionIdCandidates(query){
  const source=String(query||'').trim();
  const candidates=[];
  const seen=new Set();
  if(!source) return candidates;
  _sessionSearchAddIdCandidate(candidates,seen,source);

  const inspectUrl=(token)=>{
    const cleaned=_sessionSearchCleanUrlToken(token);
    if(!cleaned) return;
    try{
      const url=new URL(cleaned,'http://webui.local');
      const parts=url.pathname.split('/').filter(Boolean);
      const sessionIdx=parts.findIndex(p=>p.toLowerCase()==='session');
      if(sessionIdx>=0&&parts[sessionIdx+1]) _sessionSearchAddIdCandidate(candidates,seen,parts[sessionIdx+1]);
      for(const key of ['session_id','session','sid']){
        const value=url.searchParams.get(key);
        if(value) _sessionSearchAddIdCandidate(candidates,seen,value);
      }
    }catch(_e){}
  };

  const markdownLinkRe=/\]\(([^\s)]+)\)/g;
  let match;
  while((match=markdownLinkRe.exec(source))) inspectUrl(match[1]);

  const sessionSchemeRe=/session:\/\/([^\s)>\]]+)/gi;
  while((match=sessionSchemeRe.exec(source))) _sessionSearchAddIdCandidate(candidates,seen,match[1]);

  const urlRe=/(?:https?:\/\/[^\s<>\]]+|\/session\/[^\s<>\]]+|\?[^\s<>\]]+)/gi;
  while((match=urlRe.exec(source))) inspectUrl(match[0]);

  const queryParamRe=/(?:^|[?&\s])(session_id|session|sid)=([^&#\s)]+)/gi;
  while((match=queryParamRe.exec(source))) _sessionSearchAddIdCandidate(candidates,seen,match[2]);
  return candidates;
}

function _sessionSearchDirectSessionMatches(sessions, query){
  const candidates=_sessionSearchSessionIdCandidates(query);
  if(!candidates.length) return [];
  const candidateIds=new Set(candidates.map(s=>String(s)));
  return (sessions||[]).filter(s=>s&&candidateIds.has(String(s.session_id||'')));
}

function _sessionSearchDirectAndTitleMatches(sessions, query){
  const source=String(query||'').trim();
  if(!source) return sessions||[];
  const q=source.toLowerCase();
  const titleMatches=(sessions||[]).filter(s=>_sessionDisplayTitle(s).toLowerCase().includes(q));
  const directSessionMatches=_sessionSearchDirectSessionMatches(sessions,source);
  const directSessionIds=new Set(directSessionMatches.map(s=>s.session_id));
  return [...directSessionMatches,...titleMatches.filter(s=>!directSessionIds.has(s.session_id))];
}

function _sessionSearchMergeMatches(sessions, query, contentResults){
  const source=String(query||'').trim();
  if(!source) return sessions||[];
  const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(sessions,source);
  const directOrTitleIds=new Set(directAndTitleMatches.map(s=>s.session_id));
  return [
    ...directAndTitleMatches,
    ...(contentResults||[]).filter(s=>s&&s.match_type==='content'&&!directOrTitleIds.has(s.session_id))
  ];
}

function syncSessionSearchClear(){
  const input=$('sessionSearch');
  const clear=$('sessionSearchClear');
  if(!input||!clear) return;
  clear.hidden=!Boolean(input.value);
}

function clearSessionSearch(focusInput=true){
  const input=$('sessionSearch');
  if(!input) return;
  if(input.value){
    input.value='';
    filterSessions();
  }else{
    syncSessionSearchClear();
  }
  if(focusInput) input.focus();
}

function _syncArchivedSearchPagingRefresh(query){
  const queryActive=Boolean(String(query||'').trim());
  const previous=_archivedSearchPagingQueryActive;
  _archivedSearchPagingQueryActive=queryActive;
  if(!sidebarStateBindings._showArchived||queryActive===previous) return;
  // Archived title/id filtering is client-side. When search becomes active,
  // refetch without archived_limit so matches beyond the first archived page are
  // reachable; when search clears, refetch again to restore normal archive paging.
  if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:false});
}

function filterSessions(){
  // Immediate client-side title filter (no flicker)
  // Debounced content search via API for message text
  syncSessionSearchClear();
  const q = ($('sessionSearch').value || '').trim();
  _syncArchivedSearchPagingRefresh(q);
  if(q!==_lastSessionSearchQuery){
    _lastSessionSearchQuery=q;
    _hideSearchPreviewsAfterSelect=false;
  }
  renderSessionListFromCache();
  clearTimeout(_searchDebounceTimer);
  if (!q) { _contentSearchResults = []; return; }
  _searchDebounceTimer = setTimeout(async () => {
    const requestedQ = q;
    try {
      const data = await api(`/api/sessions/search?q=${encodeURIComponent(requestedQ)}&content=1&depth=5`);
      const currentQ = ($('sessionSearch').value || '').trim();
      if(currentQ!==requestedQ) return;
      const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(sidebarStateBindings._allSessions,currentQ);
      const directOrTitleIds=new Set(directAndTitleMatches.map(s=>s.session_id));
      _contentSearchResults = (data.sessions||[]).filter(s => s.match_type === 'content' && !directOrTitleIds.has(s.session_id));
      renderSessionListFromCache();
    } catch(e) { /* ignore */ }
  }, 350);
}

function _sessionTimestampMs(session) {
  const raw = Number(session && (session._sidebar_activity_at || session.last_message_at || session.updated_at || session.created_at || 0));
  return Number.isFinite(raw) ? raw * 1000 : 0;
}

function _sessionSortTimestampMs(session) {
  const base = _sessionTimestampMs(session);
  const pending = Number(session && session.pending_started_at);
  const pendingMs = Number.isFinite(pending) ? pending * 1000 : 0;
  return Math.max(base, pendingMs);
}

function _sessionRunningSortRank(session) {
  if(_isSessionEffectivelyStreaming(session)) return 1;
  return session && session.active_stream_id && session.has_pending_user_message ? 1 : 0;
}

function _sessionSidebarSortCompare(a, b) {
  const activeDelta = _sessionRunningSortRank(b) - _sessionRunningSortRank(a);
  if(activeDelta) return activeDelta;
  return _sessionSortTimestampMs(b) - _sessionSortTimestampMs(a);
}

function _serverNowMs() {
  // Compensate for clock skew between client and server (issue #1144).
  // Returns an approximation of the current server time in ms.
  return Date.now() - _serverTimeDelta;
}

function _serverTzOptions() {
  // Build a timeZone option from _serverTz (e.g. "+0800" → "Etc/GMT-8").
  // Falls back to undefined (uses browser timezone) when:
  //   - _serverTz is not set or is UTC (no offset to apply)
  //   - _serverTz is malformed
  //   - _serverTz has a fractional-hour component (India +0530, Iran +0330,
  //     Newfoundland -0330, Nepal +0545, etc.) — IANA Etc/GMT zones cannot
  //     express half/quarter-hour offsets; use _formatInServerTz() instead
  //     for correct fractional-offset formatting.
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') return undefined;
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m) return undefined;
  if (m[3] !== '00') return undefined;  // fractional offset — caller must use _formatInServerTz
  // IANA Etc/GMT uses inverted sign: UTC+8 → "Etc/GMT-8"
  const sign = m[1] === '+' ? '-' : '+';
  return { timeZone: `Etc/GMT${sign}${parseInt(m[2])}` };
}

function _formatInServerTz(date, options) {
  // Format `date` in the server's wall-clock timezone, including correct
  // handling of fractional-hour offsets that Etc/GMT cannot express.
  //
  // Strategy: shift the timestamp by the server's offset, then format with
  // timeZone:'UTC' so no further conversion is applied — the formatted
  // output reads as the wall-clock time in the server's timezone.
  //
  // Falls back to plain `date.toLocaleString(undefined, options)` (browser
  // timezone) when _serverTz is absent, UTC, or malformed.
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') {
    return date.toLocaleString(undefined, options);
  }
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m) return date.toLocaleString(undefined, options);
  const sign = m[1] === '+' ? 1 : -1;
  const offsetMin = sign * (parseInt(m[2]) * 60 + parseInt(m[3]));
  const adjusted = new Date(date.getTime() + offsetMin * 60 * 1000);
  return adjusted.toLocaleString(undefined, { ...options, timeZone: 'UTC' });
}

function _localDayOrdinal(timestampMs) {
  const date = new Date(timestampMs);
  return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
}

function _sessionCalendarBoundaries(nowMs) {
  nowMs = nowMs || _serverNowMs();
  const now = new Date(nowMs);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  startOfWeek.setDate(startOfWeek.getDate() - ((startOfWeek.getDay() + 6) % 7));
  const startOfLastWeek = new Date(startOfWeek);
  startOfLastWeek.setDate(startOfLastWeek.getDate() - 7);
  return {
    startOfToday: startOfToday.getTime(),
    startOfYesterday: startOfYesterday.getTime(),
    startOfWeek: startOfWeek.getTime(),
    startOfLastWeek: startOfLastWeek.getTime(),
  };
}

function _formatSessionDate(timestampMs, nowMs) {
  nowMs = nowMs || _serverNowMs();
  const date = new Date(timestampMs);
  const now = new Date(nowMs);
  const options = {month:'short', day:'numeric'};
  if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function _formatRelativeSessionTime(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_unknown');
  nowMs = nowMs || _serverNowMs();
  const diffMs = Math.max(0, nowMs - timestampMs);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  const dayDiff = Math.max(0, _localDayOrdinal(nowMs) - _localDayOrdinal(timestampMs));
  if (timestampMs >= startOfToday) {
    if (diffMs < minute) return t('session_time_minutes_ago', 1);
    if (diffMs < hour) {
      const minutes = Math.floor(diffMs / minute);
      return t('session_time_minutes_ago', minutes);
    }
    const hours = Math.floor(diffMs / hour);
    return t('session_time_hours_ago', hours);
  }
  if (timestampMs >= startOfYesterday) return t('session_time_days_ago', 1);
  if (timestampMs >= startOfWeek) return t('session_time_days_ago', dayDiff);
  if (timestampMs >= startOfLastWeek) return t('session_time_last_week');
  return _formatSessionDate(timestampMs, nowMs);
}

function _sessionTimeBucketLabel(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_bucket_older');
  nowMs = nowMs || _serverNowMs();
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  if (timestampMs >= startOfToday) return t('session_time_bucket_today');
  if (timestampMs >= startOfYesterday) return t('session_time_bucket_yesterday');
  if (timestampMs >= startOfWeek) return t('session_time_bucket_this_week');
  if (timestampMs >= startOfLastWeek) return t('session_time_bucket_last_week');
  return t('session_time_bucket_older');
}

function _isChildSession(s){
  return !!(s&&s.parent_session_id&&s.relationship_type==='child_session');
}

function _isForkWithResolvableParent(s, sessionIdsInList){
  return !!(s&&s.session_source==='fork'&&s.parent_session_id&&sessionIdsInList&&sessionIdsInList.has(s.parent_session_id));
}

function _sessionLineageKey(s, sessionIdsInList, sessionsById){
  if(!s||!s.session_id) return null;
  if(_isChildSession(s)) return null;
  if(s.session_source==='fork') return null;
  const lineageKey=s._lineage_root_id||s.lineage_root_id||null;
  if(lineageKey) return lineageKey;
  // WebUI-native context compression may only persist parent_session_id:
  // the preserved parent snapshot is marked pre_compression_snapshot while
  // the new continuation points at it.  When both rows are in the sidebar
  // payload, still collapse them into one conversation (#2489).
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
  // If parent_session_id points to another session in the current list,
  // this is a subagent/fork child without compression metadata — don't
  // collapse it into lineage (#494).
  if(s.parent_session_id && sessionIdsInList && sessionIdsInList.has(s.parent_session_id)){
    return null;
  }
  return s.parent_session_id || null;
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

function _resolveSessionIdFromSidebarLineage(sid){
  sid=String(sid||'').trim();
  if(!sid||!Array.isArray(sidebarStateBindings._allSessions)||!sidebarStateBindings._allSessions.length) return sid||null;
  const visibleRows=_collapseSessionLineageForSidebar(sidebarStateBindings._allSessions).filter(row=>row&&!_isChildSession(row));
  if(visibleRows.some(row=>row&&row.session_id===sid)) return sid;
  const candidates=[];
  for(const row of visibleRows){
    if(!row||!row.session_id) continue;
    if(row.relationship_type==='child_session') continue;
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

function _clearLineageReportCache(){
  _lineageReportCache.clear();
  _lineageReportInflight.clear();
  sidebarStateBindings._lineageReportCacheGeneration++;
}

function _pruneLineageReportCacheToVisibleSessions(sessions){
  const visibleKeys=new Set();
  const rows=Array.isArray(sessions)?sessions:[];
  for(const s of rows){
    const key=_sidebarLineageKeyForRow(s);
    if(key) visibleKeys.add(key);
  }
  // Also retain the cache keys derived from the COLLAPSED/rendered rows. The
  // render loop keys the lineage-report cache by _sidebarLineageKeyForRow on
  // the COLLAPSED row (see renderSessionListFromCache), and collapse can merge
  // segments so a collapsed row's key differs from any single raw input row's
  // key. Mirroring the precedent in _resolveSessionIdFromSidebarLineage, fold
  // the collapsed rows' keys in so a still-visible expanded row is never evicted
  // (and re-fetched every payload) on a chain the raw keys alone wouldn't cover.
  try{
    for(const row of _collapseSessionLineageForSidebar(rows)){
      if(!row||_isChildSession(row)) continue;
      const key=_lineageReportCacheKey(row,_sidebarLineageKeyForRow(row));
      if(key) visibleKeys.add(key);
    }
  }catch(_){ /* defensive: never let a prune-key derivation break list apply */ }
  for(const key of Array.from(_lineageReportCache.keys())){
    if(!visibleKeys.has(key)) _lineageReportCache.delete(key);
  }
  for(const key of Array.from(_lineageReportInflight.keys())){
    if(!visibleKeys.has(key)) _lineageReportInflight.delete(key);
  }
}

function _lineageReportCacheKey(s,lineageKey){
  const key=lineageKey||_sidebarLineageKeyForRow(s)||null;
  const tip=typeof _authoritativeLineageTipId==='function'
    ? _authoritativeLineageTipId(s)
    : s&&(s._lineage_tip_id||s._parent_lineage_tip_id)||null;
  return key&&tip&&tip!==key?`${key}::${tip}`:key;
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
  return Number(segmentCount||0)>_lineageLocalSegmentCount(s);
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

function _sidebarLineageKeyForRow(s){
  if(!s) return null;
  if(s.session_source==='fork') return s.session_id||s.parent_session_id||null;
  return s._lineage_key||s._lineage_root_id||s.lineage_root_id||s.parent_session_id||s.session_id||null;
}

function _truncatedSessionId(sid){
  sid=String(sid||'').trim();
  if(!sid) return '';
  if(sid.length<=16) return sid;
  return sid.slice(0,12)+'...';
}

function _sessionTitleForForkParent(parentSid){
  if(!parentSid||!Array.isArray(sidebarStateBindings._allSessions)) return '';
  const parent=sidebarStateBindings._allSessions.find(item=>item&&item.session_id===parentSid);
  const title=parent&&String(parent.title||'').trim();
  if(!title||title==='Untitled') return '';
  return title;
}

function _sessionFullTitleTooltip(rawTitle, cleanTitle, session){
  const fallback=String(cleanTitle||'Untitled').trim()||'Untitled';
  const full=String(rawTitle||fallback).trim()||fallback;
  const title=full.startsWith('[SYSTEM:') ? fallback : full;
  if(typeof t==='function'&&_isReadOnlySession(session)) return t('session_readonly_title_hint', title);
  return title;
}

function _sessionForkTooltip(parentLabel){
  const parent=String(parentLabel||'').trim()||'unknown parent';
  // Preserve the localized "Forked from" base (the catalog key exists in all
  // locales) rather than hardcoding English — the only regression risk in the
  // tooltip rework was dropping t('forked_from') here.
  const prefix=(typeof t==='function'?t('forked_from'):'Forked from');
  return `${prefix}: ${parent}`;
}

function _sessionLineageBadgeTooltip(label, canExpand){
  const base=String(label||'Prior turns').trim()||'Prior turns';
  if(typeof t==='function'){
    return canExpand
      ? t('session_lineage_toggle_hint', base)
      : t('session_lineage_static_hint', base);
  }
  return base;
}

function _sessionChildBadgeTooltip(label){
  const base=String(label||'Child sessions').trim()||'Child sessions';
  if(typeof t==='function') return t('session_child_toggle_hint', base);
  return base;
}

function _sessionStateTooltip({isStreaming=false,hasUnread=false}={}){
  if(isStreaming) return 'Conversation is running';
  if(hasUnread) return 'Unread completion';
  return '';
}

function _attachChildSessionsToSidebarRows(collapsedRows, rawSessions, rawReferenceSessions){
  const referenceSessions=Array.isArray(rawReferenceSessions)?rawReferenceSessions:(rawSessions||[]);
  const sessionIdsInList=new Set(referenceSessions.map(s=>s&&s.session_id).filter(Boolean));
  const rawSessionsById=new Map(referenceSessions.filter(s=>s&&s.session_id).map(s=>[s.session_id,s]));
  const cleanSidebarRow=(s)=>{
    const row={...s};
    // Child-session decoration is render-derived.  Drop stale copies so an
    // archived child disappears immediately on the next list rebuild instead of
    // lingering under a copied parent row (#4293).
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
    // Cross-surface rows (for example a WebUI continuation from a Telegram
    // conversation) should remain top-level when there is no WebUI-owned parent
    // row to stack under.  But if the parent is visible in this same sidebar
    // render, attach normally — delegated subagent rows are also cross-source
    // relative to their WebUI parent and should not be forced into orphans.
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
      // #5305: a delegated subagent child whose WebUI parent is NOT a visible
      // row in this render (filtered out by the active project / profile / source
      // scope, or otherwise absent) must NOT be promoted to a contextless
      // top-level "Subagent Session" orphan — that is the confusing orphan #5244
      // set out to remove for the common case. The parent still exists; it is
      // simply out of the current view, so the child follows its parent's scope
      // and is suppressed here (it re-stacks under the parent once that scope is
      // active). This mirrors the archived-hidden-parent suppression above
      // (hasHiddenArchivedAncestor / #4293), generalizing the "parent hidden"
      // trigger from archived to filtered-out. A cross-surface WebUI child of a
      // genuinely external (messaging/CLI) parent is handled by the parentIsExternal
      // branch above and still orphans as before.
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
      if(bSeg||aSeg){
        if(bSeg!==aSeg) return bSeg-aSeg;
      }
      // Preserved pre-compression parents can share the same backend segment
      // count as the continuation. Prefer the non-snapshot tip before falling
      // back to timestamps, otherwise a recently-polled parent reopens the
      // older transcript and makes the active continuation look lost.
      const bSnapshot=!!(b&&b.pre_compression_snapshot);
      const aSnapshot=!!(a&&a.pre_compression_snapshot);
      if(bSnapshot!==aSnapshot) return aSnapshot-bSnapshot;
      return _sessionTimestampMs(b)-_sessionTimestampMs(a);
    });
    const tipIds=new Set(items.map(item=>typeof _authoritativeLineageTipId==='function'
      ? _authoritativeLineageTipId(item)
      : item&&(item._lineage_tip_id||item._parent_lineage_tip_id)||null).filter(Boolean));
    const chosen=sorted.find(item=>tipIds.has(item&&item.session_id))||sorted[0];
    result.push({...chosen,_lineage_key:key,_lineage_collapsed_count:items.length,_lineage_segments:sorted});
  }
  return result;
}

export const sessionDiscovery=Object.freeze({filter:filterSessions,clear:clearSessionSearch,resolveLineage:_resolveSessionIdFromSidebarLineage,collapseLineage:_collapseSessionLineageForSidebar});

export { _appendHighlightedText, _attachChildSessionsToSidebarRows, _collapseSessionLineageForSidebar, _fetchLineageReportForRow, _formatInServerTz, _formatRelativeSessionTime, _isChildSession, _lineageReportCacheKey, _lineageReportNeedsFetch, _lineageSegmentsForRender, _pruneLineageReportCacheToVisibleSessions, _resolveSessionIdFromSidebarLineage, _serverNowMs, _sessionChildBadgeTooltip, _sessionForkTooltip, _sessionFullTitleTooltip, _sessionLineageBadgeTooltip, _sessionLineageContainsSession, _sessionSearchContentPreview, _sessionSearchMergeMatches, _sessionSegmentCount, _sessionSidebarSortCompare, _sessionSortTimestampMs, _sessionStateTooltip, _sessionTimeBucketLabel, _sessionTimestampMs, _sessionTitleForForkParent, _sidebarLineageKeyForRow, _syncSidebarExpansionForActiveSession, _truncatedSessionId, clearSessionSearch, filterSessions, syncSessionSearchClear };

export const sessionDiscoveryBindings=Object.freeze({
  get _contentSearchResults(){ return _contentSearchResults; },
  set _contentSearchResults(value){ _contentSearchResults=value; },
  get _hideSearchPreviewsAfterSelect(){ return _hideSearchPreviewsAfterSelect; },
  set _hideSearchPreviewsAfterSelect(value){ _hideSearchPreviewsAfterSelect=value; },
  get _serverTimeDelta(){ return _serverTimeDelta; },
  set _serverTimeDelta(value){ _serverTimeDelta=value; },
  get _serverTz(){ return _serverTz; },
  set _serverTz(value){ _serverTz=value; },
});
