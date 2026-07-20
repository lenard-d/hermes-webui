import { _sessionDisplayTitle } from './session-display.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { sidebarStateBindings } from './sidebar-store.js';

let _searchDebounceTimer = null;
let _contentSearchResults = [];
let _lastSessionSearchQuery = '';
let _hideSearchPreviewsAfterSelect = false;
let _archivedSearchPagingQueryActive = false;

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
  if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:false});
}

function filterSessions(){
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

const sessionSearchBindings={};
Object.defineProperties(sessionSearchBindings,{
  _contentSearchResults:{enumerable:true,get:()=>_contentSearchResults,set:value=>{_contentSearchResults=value;}},
  _hideSearchPreviewsAfterSelect:{enumerable:true,get:()=>_hideSearchPreviewsAfterSelect,set:value=>{_hideSearchPreviewsAfterSelect=value;}},
});
Object.freeze(sessionSearchBindings);

export {
  _appendHighlightedText,
  _sessionSearchContentPreview,
  _sessionSearchMergeMatches,
  clearSessionSearch,
  filterSessions,
  sessionSearchBindings,
  syncSessionSearchClear,
};
