import { ICONS, _manualTitleRegenerateTimeoutMs, sessionStateBindings } from './state.js';
import { loadSession } from './lifecycle.js';
import { _clearHandoffStorageForSession, _isCliSession, _isMessagingSession, _isReadOnlySession, _restoreSessionSourceFilter, _sessionListQueryString } from './message-loading.js';
import { _dropStaleOptimisticSessionRow, renderSessionList } from './session-list.js';
import { _sessionDisplayTitle } from './sidebar-interactions.js';
import { renderSessionListFromCache } from './sidebar-renderer.js';
import { _showProjectPicker, deleteSession, removeWorktree } from './management.js';

const SESSION_ARCHIVED_PAGE_SIZE = 100;
const SESSION_ARCHIVED_MAX_LOADED_LIMIT = 2000;
let _allSessions = [];  // cached for search filter
let _sidebarReferenceSessions = [];  // hidden archived ancestor rows used only for nesting/suppression
let _allSessionsScope = null;  // {profile, allProfiles} the cache was loaded under (#4167)
let _sessionAttentionSoundPrimed = false;
const _sessionAttentionSoundState = new Map();
let _renamingSid = null;  // session_id currently being renamed (blocks list re-renders)
let _showArchived = false;  // toggle to show archived sessions
let _sessionSelectMode = false;  // batch select mode
const _selectedSessions = new Set();  // selected session IDs
let _allProjects = [];  // cached project list
// Sentinel value for the _activeProject state when filtering to sessions
// that have no project_id assigned. Distinct from real project IDs so the
// equality check below can branch cleanly on it. The literal string is
// not user-visible (the chip renders the localized label) — it just has
// to be something a user-created project_id can never collide with, which
// double-underscore prefixes provide.
const NO_PROJECT_FILTER = '__none__';
let _activeProject = null;  // project_id filter (null = show all, NO_PROJECT_FILTER = unassigned only)
const SHOW_ALL_PROFILES_STORAGE_KEY = 'hermes-show-all-profiles';
let _showAllProfiles = false;  // false = filter to active profile only
let _profileSwitchOpeningExistingSession = false;  // true while cross-profile sidebar click switches profile before loadSession()
let _otherProfileCount = 0;       // count of sessions from other profiles (server-reported)
let _archivedWebuiCount = 0;      // archived WebUI sessions not fetched until requested
let _archivedCliCount = 0;        // archived non-WebUI sessions not fetched until requested
let _archivedRowsLoadedLimit = SESSION_ARCHIVED_PAGE_SIZE;
let _serverWebuiSessionCount = null;  // explicit server count for WebUI sessions
let _serverCliSessionCount = null;    // explicit server count for CLI sessions
let _sessionSourceFilter = 'webui';  // 'webui' keeps WebUI chats separate from read-only CLI sessions

function _restoreShowAllProfiles(){
  try{
    const raw=localStorage.getItem(SHOW_ALL_PROFILES_STORAGE_KEY);
    _showAllProfiles = raw === '1' || raw === 'true';
  }catch(_e){ _showAllProfiles = false; }
}

function _setShowAllProfiles(enabled){
  _showAllProfiles=!!enabled;
  try{ localStorage.setItem(SHOW_ALL_PROFILES_STORAGE_KEY,_showAllProfiles?'1':'0'); }catch(_e){}
}

_restoreShowAllProfiles();
_restoreSessionSourceFilter();
let _sessionActionMenu = null;
let _sessionActionAnchor = null;
let _sessionActionSessionId = null;
let _sessionActionMenuId = 0;
let _sessionActionPreviousFocus = null;
const _expandedChildSessionKeys = new Set();
const _expandedLineageKeys = new Set();
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
let _lineageReportCacheGeneration = 0;
let _sessionVisibleSidebarIds = [];
let _pendingSessionReflowPositions = null;
const _optimisticallyRemovedSessionIds = new Set();
const _sessionSwipeReturnOffsets = new Map();

function _captureSessionReflowPositions(){
  const list=$('sessionList');
  if(!list) return null;
  const positions=new Map();
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    positions.set(row.dataset.sid,row.getBoundingClientRect().top);
  });
  return positions;
}

function _waitForSessionMotion(ms){
  return new Promise(resolve=>setTimeout(resolve,ms));
}

function _playSessionRowsReflowFromPositions(before, timeoutMs, prefersReducedMotion){
  if(!before||!before.size) return;
  if(prefersReducedMotion&&prefersReducedMotion()) return;
  const list=$('sessionList');
  if(!list) return;
  const movingRows=[];
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    const oldTop=before.get(row.dataset.sid);
    if(oldTop===undefined) return;
    const delta=oldTop-row.getBoundingClientRect().top;
    if(Math.abs(delta)<1) return;
    movingRows.push({row,delta});
  });
  if(!movingRows.length) return;
  movingRows.forEach(({row,delta})=>{
    row.style.transition='none';
    row.style.setProperty('--session-reflow-offset',delta+'px');
    row.classList.add('session-reflowing');
  });
  list.getBoundingClientRect();
  movingRows.forEach(({row})=>{
    let reflowCleared=false;
    const clearReflow=()=>{
      if(reflowCleared) return;
      reflowCleared=true;
      row.classList.remove('session-reflowing');
      row.style.removeProperty('--session-reflow-offset');
      row.removeEventListener('transitionend',onReflowEnd);
    };
    const onReflowEnd=(event)=>{
      if(event.propertyName==='transform') clearReflow();
    };
    row.addEventListener('transitionend',onReflowEnd);
    row.style.removeProperty('transition');
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(!reflowCleared) row.style.setProperty('--session-reflow-offset','0px');
    }));
    setTimeout(clearReflow,timeoutMs);
  });
}

function _sessionPrefersReducedMotion(){
  try{
    return Boolean(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }catch(_){
    return false;
  }
}

function _makeSessionSwipeAffordance(side, icon, label){
  const affordance=document.createElement('div');
  affordance.className='session-swipe-affordance session-swipe-affordance-'+side;
  affordance.setAttribute('aria-hidden','true');
  const stack=document.createElement('span');
  stack.className='session-swipe-action-stack';
  const badge=document.createElement('span');
  badge.className='session-swipe-badge';
  badge.innerHTML=li(icon,18);
  const text=document.createElement('span');
  text.className='session-swipe-label';
  text.textContent=label;
  stack.append(badge,text);
  affordance.append(stack);
  return affordance;
}
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionVirtualScrollList = null;
let _sessionVirtualScrollRaf = 0;

function _sessionSnapshotById(sid){
  if(!sid)return null;
  if(S.session&&S.session.session_id===sid) return S.session;
  return (_allSessions||[]).find(s=>s&&s.session_id===sid)||null;
}
function _pinnedSessionCount(){
  return (_allSessions||[]).filter(s=>s&&s.pinned&&!s.archived).length;
}
function _getPinnedSessionsLimit(){
  const limit=parseInt(window._pinnedSessionsLimit||3,10);
  return (Number.isFinite(limit)&&limit>0)?limit:3;
}
function _pinnedSessionsLimitMessage(){
  const limit=_getPinnedSessionsLimit();
  return `Only ${limit} conversations can be pinned. Unpin one before pinning another.`;
}
function _worktreeSessionCount(ids){
  return (ids||[]).reduce((count,sid)=>{
    const session=_sessionSnapshotById(sid);
    return count+(session&&session.worktree_path?1:0);
  },0);
}
function _sessionResponseRetainsWorktree(response, session){
  if(response&&typeof response.worktree_retained==='boolean') return response.worktree_retained;
  return !!(session&&session.worktree_path);
}
function _worktreeResponseCount(results){
  return (results||[]).reduce((count,result)=>{
    return count+(_sessionResponseRetainsWorktree(result&&result.response,result&&result.session)?1:0);
  },0);
}
function _sessionArchiveDescription(session){
  return session&&session.worktree_path?t('session_archive_worktree_desc'):t('session_archive_desc');
}
function _sessionArchiveToast(response, session){
  return _sessionResponseRetainsWorktree(response,session)?t('session_archived_worktree'):t('session_archived');
}
function _sessionDeleteDescription(session){
  return session&&session.worktree_path?t('session_delete_worktree_desc'):t('session_delete_desc');
}
function _optimisticallyArchiveSessionInList(sid, archived){
  if(!sid||!Array.isArray(_allSessions)) return;
  let changed=false;
  _allSessions=_allSessions.map(s=>{
    if(!s||s.session_id!==sid) return s;
    changed=true;
    return {...s,archived:!!archived};
  });
  if(changed) renderSessionListFromCache();
}
function _optimisticallyRemoveSessionFromList(sid){
  if(!sid||!Array.isArray(_allSessions)) return;
  const before=_allSessions.length;
  _allSessions=_allSessions.filter(s=>!s||s.session_id!==sid);
  if(_selectedSessions&&_selectedSessions.has(sid)) _selectedSessions.delete(sid);
  if(typeof _dropStaleOptimisticSessionRow==='function') _dropStaleOptimisticSessionRow(sid);
  if(_allSessions.length!==before) renderSessionListFromCache();
}

function _sessionIdFromLocation(){
  if(typeof window==='undefined'||!window.location) return null;
  const marker='/session/';
  const path=window.location.pathname||'';
  const idx=path.indexOf(marker);
  if(idx>=0){
    const raw=path.slice(idx+marker.length).split('/')[0];
    if(raw){try{return decodeURIComponent(raw);}catch(_e){return raw;}}
  }
  try{
    const qs=new URLSearchParams(window.location.search||'');
    return qs.get('session')||qs.get('session_id')||null;
  }catch(_e){return null;}
}
function _composerPrefillIntentFromLocation(){
  const empty={hasParams:false,hasText:false,text:'',autoSend:false};
  if(typeof window==='undefined'||!window.location) return empty;
  try{
    const qs=new URLSearchParams(window.location.search||'');
    const hasQ=qs.has('q');
    const hasPrompt=qs.has('prompt');
    const hasSend=qs.has('send');
    if(!hasQ&&!hasPrompt&&!hasSend) return empty;
    const text=hasQ?(qs.get('q')||''):(hasPrompt?(qs.get('prompt')||''):'');
    return {
      hasParams:true,
      hasText:!!String(text).trim(),
      text,
      autoSend:false
    };
  }catch(_e){return empty;}
}
function _profileQueryIntentFromLocation(){
  const empty={hasParam:false,valid:false,name:''};
  if(typeof window==='undefined'||!window.location) return empty;
  try{
    const qs=new URLSearchParams(window.location.search||'');
    if(!qs.has('profile')) return empty;
    const name=String(qs.get('profile')||'');
    return {
      hasParam:true,
      valid:/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name),
      name
    };
  }catch(_e){return empty;}
}
function _consumeProfileQueryParamFromLocation(){
  if(typeof window==='undefined'||!window.location||!window.history||typeof window.history.replaceState!=='function') return;
  try{
    const current=new URL(window.location.href);
    const before=current.searchParams.toString();
    current.searchParams.delete('profile');
    const after=current.searchParams.toString();
    if(after===before) return;
    const next=current.pathname+(after?`?${after}`:'')+(current.hash||'');
    window.history.replaceState(window.history.state||null,'',next);
  }catch(_e){}
}
function _consumeComposerPrefillParamsFromLocation(){
  if(typeof window==='undefined'||!window.location||!window.history||typeof window.history.replaceState!=='function') return;
  try{
    const current=new URL(window.location.href);
    const before=current.searchParams.toString();
    current.searchParams.delete('q');
    current.searchParams.delete('prompt');
    current.searchParams.delete('send');
    const after=current.searchParams.toString();
    if(after===before) return;
    const next=current.pathname+(after?`?${after}`:'')+(current.hash||'');
    window.history.replaceState(window.history.state||null,'',next);
  }catch(_e){}
}
function _appRootPath(){
  try{
    const base = new URL(document.baseURI||window.location.origin+'/', window.location.origin);
    return base.pathname || '/';
  }catch(_e){return '/';}
}
function _sessionUrlForSid(sid){
  const encoded=encodeURIComponent(sid);
  let base;
  try{base=new URL(`session/${encoded}`, document.baseURI||window.location.origin+'/');}
  catch(_e){base=new URL(`/session/${encoded}`, window.location.origin);}
  try{
    const current=new URL(window.location.href);
    current.searchParams.delete('session');
    current.searchParams.delete('session_id');
    current.searchParams.delete('q');
    current.searchParams.delete('prompt');
    current.searchParams.delete('send');
    base.search=current.searchParams.toString();
    base.hash=current.hash;
  }catch(_e){}
  return base.pathname+base.search+base.hash;
}
function _setActiveSessionUrl(sid){
  if(typeof window==='undefined'||!window.history||!sid) return;
  const next=_sessionUrlForSid(sid);
  if(next && next!==(window.location.pathname+window.location.search+window.location.hash)){
    window.history.pushState({session_id:sid},'',next);
  }
}

// ── Batch select mode ──
function toggleSessionSelectMode(){
  _sessionSelectMode=!_sessionSelectMode;
  _selectedSessions.clear();
  renderSessionListFromCache();
}
function exitSessionSelectMode(){
  _sessionSelectMode=false;
  _selectedSessions.clear();
  const bar=$('batchActionBar');
  if(bar) bar.style.display='none';
  renderSessionListFromCache();
}
function toggleSessionSelect(sid){
  if(_selectedSessions.has(sid)) _selectedSessions.delete(sid);
  else _selectedSessions.add(sid);
  _updateBatchActionBar();
  const cb=document.querySelector('.session-select-cb[data-sid="'+sid+'"]');
  const item=cb?cb.closest('.session-item,.session-child-session-fork'):null;
  if(item){item.classList.toggle('selected',_selectedSessions.has(sid));if(cb)cb.checked=_selectedSessions.has(sid);}
}
function setSessionSelected(sid, selected){
  if(selected) _selectedSessions.add(sid);
  else _selectedSessions.delete(sid);
  _updateBatchActionBar();
  const cb=document.querySelector('.session-select-cb[data-sid="'+sid+'"]');
  const item=cb?cb.closest('.session-item,.session-child-session-fork'):null;
  if(item){item.classList.toggle('selected',_selectedSessions.has(sid));if(cb)cb.checked=_selectedSessions.has(sid);}
}
function selectAllSessions(){
  _selectedSessions.clear();
  const ids=Array.isArray(_sessionVisibleSidebarIds)&&_sessionVisibleSidebarIds.length
    ? _sessionVisibleSidebarIds
    : Array.from(document.querySelectorAll('.session-select-cb')).map(cb=>cb.dataset.sid).filter(Boolean);
  ids.forEach(sid=>_selectedSessions.add(sid));
  document.querySelectorAll('.session-select-cb').forEach(cb=>{
    const sid=cb.dataset.sid;
    if(sid){cb.checked=_selectedSessions.has(sid);const item=cb.closest('.session-item,.session-child-session-fork');if(item)item.classList.toggle('selected',_selectedSessions.has(sid));}
  });
  _updateBatchActionBar();
}
function deselectAllSessions(){
  _selectedSessions.clear();
  document.querySelectorAll('.session-select-cb').forEach(cb=>{cb.checked=false;const item=cb.closest('.session-item,.session-child-session-fork');if(item)item.classList.remove('selected');});
  _updateBatchActionBar();
}
function _updateBatchActionBar(){
  const bar=$('batchActionBar');if(!bar)return;
  const count=_selectedSessions.size;
  if(count>0){_renderBatchActionBar();}
  else{bar.style.display='none';}
}
function _renderBatchActionBar(){
  const bar=$('batchActionBar');if(!bar)return;
  bar.innerHTML='';bar.style.display=_selectedSessions.size>0?'flex':'none';
  const countBadge=document.createElement('span');countBadge.className='batch-count';
  countBadge.textContent=t('session_selected_count',_selectedSessions.size);bar.appendChild(countBadge);
  // Archive
  const archiveBtn=document.createElement('button');archiveBtn.className='batch-action-btn';
  archiveBtn.textContent=t('session_batch_archive');
  archiveBtn.onclick=async()=>{
    const ids=[..._selectedSessions];
    const wtCount=_worktreeSessionCount(ids);
    const sessionsById=new Map(ids.map(sid=>[sid,_sessionSnapshotById(sid)]));
    const ok=await showConfirmDialog({
      message:wtCount?t('session_batch_archive_worktree_confirm',ids.length,wtCount):t('session_batch_archive_confirm',ids.length),
      confirmLabel:t('session_batch_archive'),
      danger:true
    });
    if(!ok)return;
    try{
      const results=await Promise.all(ids.map(async sid=>{
        const response=await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:sid,archived:true})});
        return {response,session:sessionsById.get(sid)||null};
      }));
      const retainedCount=_worktreeResponseCount(results);
      showToast(retainedCount?t('session_archived_worktree'):t('session_archived'));exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Archive failed: '+(e.message||e));}
  };bar.appendChild(archiveBtn);
  // Move
  const moveBtn=document.createElement('button');moveBtn.className='batch-action-btn';
  moveBtn.textContent=t('session_batch_move');
  moveBtn.onclick=(e)=>{e.stopPropagation();_showBatchProjectPicker();};bar.appendChild(moveBtn);
  // Delete
  const deleteBtn=document.createElement('button');deleteBtn.className='batch-action-btn batch-action-btn-danger';
  deleteBtn.textContent=t('session_batch_delete');
  deleteBtn.onclick=async()=>{
    const ids=[..._selectedSessions];
    const wtCount=_worktreeSessionCount(ids);
    const sessionsById=new Map(ids.map(sid=>[sid,_sessionSnapshotById(sid)]));
    const ok=await showConfirmDialog({
      message:wtCount?t('session_batch_delete_worktree_confirm',ids.length,wtCount):t('session_batch_delete_confirm',ids.length),
      confirmLabel:t('delete_title'),
      danger:true
    });
    if(!ok)return;
    try{
      const results=await Promise.all(ids.map(async sid=>{
        const response=await api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})});
        return {response,session:sessionsById.get(sid)||null};
      }));
      const retainedCount=_worktreeResponseCount(results);
      const cleanupFailedCount=results.filter(result=>result.response&&result.response.state_db_cleanup_failed).length;
      ids.forEach(_clearHandoffStorageForSession);
      if(S.session&&ids.includes(S.session.session_id)){
        S.session=null;S.messages=[];S.entries=[];localStorage.removeItem('hermes-webui-session');
        if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(null);
        const remaining=await api('/api/sessions'+_sessionListQueryString());
        if(remaining.sessions&&remaining.sessions.length){await loadSession(remaining.sessions[0].session_id);}
        else{$('msgInner').innerHTML='';$('emptyState').style.display='';}
      }
      if(cleanupFailedCount) showToast(t('delete_failed')+' ('+cleanupFailedCount+'/'+ids.length+')',0,'error');
      else showToast((retainedCount?t('session_deleted_worktree'):t('session_delete'))+' ('+ids.length+')');
      exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Delete failed: '+(e.message||e));}
  };bar.appendChild(deleteBtn);
}
function _showBatchProjectPicker(){
  const ids=[..._selectedSessions];if(!ids.length)return;
  const bar=$('batchActionBar');if(!bar)return;
  bar.querySelectorAll('.batch-project-picker').forEach(p=>p.remove());
  const picker=document.createElement('div');picker.className='project-picker batch-project-picker';
  const none=document.createElement('div');none.className='project-picker-item';none.textContent='No project';
  none.onclick=async()=>{picker.remove();
    try{await Promise.all(ids.map(sid=>api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:sid,project_id:null})})));
      showToast('Removed from project');exitSessionSelectMode();await renderSessionList();
    }catch(e){showToast('Move failed: '+(e.message||e));}
  };picker.appendChild(none);
  for(const p of(_allProjects||[])){
    const item=document.createElement('div');item.className='project-picker-item';
    if(p.color){const dot=document.createElement('span');dot.className='color-dot';
      dot.style.cssText='width:6px;height:6px;border-radius:50%;background:'+p.color+';flex-shrink:0;';item.appendChild(dot);}
    const name=document.createElement('span');name.textContent=p.name;item.appendChild(name);
    item.onclick=async()=>{picker.remove();
      try{await Promise.all(ids.map(sid=>api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:sid,project_id:p.project_id})})));
        showToast('Moved to '+p.name);exitSessionSelectMode();await renderSessionList();
      }catch(e){showToast('Move failed: '+(e.message||e));}
    };picker.appendChild(item);
  }
  bar.appendChild(picker);
  const close=(e)=>{if(!picker.contains(e.target)){picker.remove();document.removeEventListener('click',close);}};
  setTimeout(()=>document.addEventListener('click',close),0);
}

function _focusSessionActionMenuRestoreTarget(target){
  if(!target||!target.isConnected||typeof target.focus!=='function') return false;
  try{target.focus({preventScroll:true});}catch(_){target.focus();}
  return document.activeElement===target;
}

function closeSessionActionMenu({restoreFocus=false}={}){
  const focusTarget=restoreFocus?_sessionActionAnchor:null;
  const fallbackFocusTarget=restoreFocus?_sessionActionPreviousFocus:null;
  if(_sessionActionMenu){
    _sessionActionMenu.remove();
    _sessionActionMenu = null;
  }
  if(_sessionActionAnchor){
    if(_sessionActionAnchor.classList&&_sessionActionAnchor.classList.contains('session-actions-trigger')){
      _sessionActionAnchor.classList.remove('active');
      _sessionActionAnchor.setAttribute('aria-expanded','false');
      _sessionActionAnchor.removeAttribute('aria-controls');
    }
    const row=_sessionActionAnchor.closest('.session-item,.session-child-session');
    if(row) row.classList.remove('menu-open','long-pressing');
    _sessionActionAnchor = null;
  }
  _sessionActionSessionId = null;
  _sessionActionPreviousFocus = null;
  if(!_focusSessionActionMenuRestoreTarget(focusTarget)) _focusSessionActionMenuRestoreTarget(fallbackFocusTarget);
}

function _sessionActionMenuShouldIgnoreScrollTarget(target){
  if(!target || typeof target.closest !== 'function') return false;
  // #5347: active-chat auto-scroll / manual wheel must not dismiss the sidebar menu.
  return Boolean(target.closest('#messages, #msgInner, .messages-inner'));
}

function _sessionActionMenuShouldRepositionOnScroll(target){
  if(!target || typeof target.closest !== 'function') return false;
  return Boolean(target.closest('#sessionList, .session-list'));
}

function _positionSessionActionMenu(anchorEl){
  if(!_sessionActionMenu || !anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(280, Math.max(220, _sessionActionMenu.scrollWidth || 220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  _sessionActionMenu.style.left=left+'px';
  _sessionActionMenu.style.top='8px';
  // Reset any prior clamp so we measure the menu's natural height.
  _sessionActionMenu.style.maxHeight='';
  const menuH=_sessionActionMenu.offsetHeight || 0;
  const margin=8;
  const maxAvail=window.innerHeight-margin*2;
  let top=rect.bottom+6;
  // Prefer flipping above the row when the menu would overflow the bottom and
  // there's room above.
  if(top+menuH>window.innerHeight-margin && rect.top>menuH+12){
    top=rect.top-menuH-6;
  }
  // If the menu is taller than the viewport, or still overflows after the flip
  // attempt (e.g. a top-anchored row with a tall menu and no room above), cap
  // its height to the viewport and let it scroll instead of clipping off-screen.
  if(menuH>maxAvail){
    _sessionActionMenu.style.maxHeight=maxAvail+'px';
    top=margin;
  } else {
    // Clamp vertically so the whole menu stays on-screen at both edges.
    if(top+menuH>window.innerHeight-margin) top=window.innerHeight-margin-menuH;
    if(top<margin) top=margin;
  }
  _sessionActionMenu.style.top=top+'px';
}

function _buildSessionAction(label, meta, icon, onSelect, extraClass=''){
  const opt=document.createElement('button');
  opt.type='button';
  opt.className='ws-opt session-action-opt'+(extraClass?` ${extraClass}`:'');
  opt.setAttribute('role','menuitem');
  // Compact context-menu shape (#3223 redesign, Nathan 2026-06-01): show only
  // icon + label, matching VS Code / browser / ChatGPT conversation menus. The
  // descriptive `meta` is preserved as a hover tooltip (title=) so the
  // information stays discoverable without consuming permanent vertical space —
  // this also keeps the menu short enough to avoid viewport clipping.
  if(meta) opt.title=meta;
  opt.innerHTML=
    `<span class="ws-opt-action">`
      + `<span class="ws-opt-icon">${icon}</span>`
      + `<span class="session-action-copy">`
        + `<span class="ws-opt-name">${esc(label)}</span>`
      + `</span>`
    + `</span>`;
  opt.onclick=async(e)=>{
    e.preventDefault();
    e.stopPropagation();
    await onSelect();
  };
  return opt;
}

function _sessionMarkdownLabel(session){
  const sid=session&&session.session_id?String(session.session_id):'';
  const title=String((session&&(session.title||session.name))||'Conversation').replace(/\s+/g,' ').trim()||'Conversation';
  const shortSid=sid?sid.slice(0,12):'';
  const label=shortSid?`${title} (${shortSid})`:title;
  return label.replace(/([\\\[\]])/g,'\\$1').slice(0,120);
}

function _sessionMarkdownUrlSid(sid){
  return encodeURIComponent(String(sid||'')).replace(/[()]/g, ch => ch==='('?'%28':'%29');
}

function _sessionInternalReferenceForSession(session){
  const sid=session&&session.session_id;
  if(!sid) return '';
  return `[${_sessionMarkdownLabel(session)}](session://${_sessionMarkdownUrlSid(sid)})`;
}

async function _copyTextToClipboard(text){
  if(navigator&&navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){
    await navigator.clipboard.writeText(text);
    return true;
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.setAttribute('readonly','');
  ta.style.position='fixed';
  ta.style.left='-9999px';
  ta.style.top='0';
  document.body.appendChild(ta);
  ta.select();
  try{return document.execCommand('copy');}
  finally{ta.remove();}
}

async function _copySessionLink(session){
  const sid=session&&session.session_id;
  if(!sid) return;
  const ref=(window.location.origin||'')+_sessionUrlForSid(sid);
  try{
    await _copyTextToClipboard(ref);
    showToast(t('session_link_copied'));
  }catch(err){
    showToast(t('session_link_copy_failed')+(err&&err.message?err.message:err));
  }
}

function _mountSessionActionMenu(menu, session, anchorEl){
  _sessionActionPreviousFocus=document.activeElement;
  document.body.appendChild(menu);
  _sessionActionMenu = menu;
  _sessionActionAnchor = anchorEl;
  _sessionActionSessionId = session.session_id;
  if(anchorEl.classList&&anchorEl.classList.contains('session-actions-trigger')){
    anchorEl.classList.add('active');
    anchorEl.setAttribute('aria-expanded','true');
    anchorEl.setAttribute('aria-controls',menu.id);
  }
  const row=anchorEl.closest('.session-item,.session-child-session');
  if(row) row.classList.add('menu-open');
  _positionSessionActionMenu(anchorEl);
  _playSessionActionMenuEntrance(menu);
  const menuItems=()=>Array.from(menu.querySelectorAll('.session-action-opt:not([disabled])'));
  menu.addEventListener('keydown',e=>{
    const items=menuItems();
    if(e.key==='Escape'){
      e.preventDefault();
      e.stopPropagation();
      closeSessionActionMenu({restoreFocus:true});
      return;
    }
    if(!items.length) return;
    const currentIndex=Math.max(0,items.indexOf(document.activeElement));
    let nextIndex=null;
    if(e.key==='ArrowDown') nextIndex=(currentIndex+1)%items.length;
    else if(e.key==='ArrowUp') nextIndex=(currentIndex-1+items.length)%items.length;
    else if(e.key==='Home') nextIndex=0;
    else if(e.key==='End') nextIndex=items.length-1;
    if(nextIndex===null) return;
    e.preventDefault();
    try{items[nextIndex].focus({preventScroll:true});}catch(_){items[nextIndex].focus();}
  });
  const firstAction=menuItems()[0];
  if(firstAction){
    try{firstAction.focus({preventScroll:true});}catch(_){firstAction.focus();}
  }
}

function _findSessionRenameRow(sessionId){
  const sid=String(sessionId||'');
  if(!sid) return null;
  return document.querySelector('.session-item[data-sid="'+sid+'"], .session-child-session[data-sid="'+sid+'"]');
}

function _buildSessionRenameStarter(session, displayEl, renderDisplay){
  return ()=>{
    if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be renamed.',3000); return; }
    if(sessionStateBindings._loadingSessionId&&sessionStateBindings._loadingSessionId!==session.session_id) return;

    closeSessionActionMenu();
    _renamingSid=session.session_id;
    const oldTitle=_sessionDisplayTitle(session)||'Untitled';
    const inp=document.createElement('input');
    inp.className='session-title-input';
    inp.value=oldTitle;
    ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
      inp.addEventListener(ev, e2=>e2.stopPropagation())
    );
    const applyLocalTitle=(target, nextTitle)=>{
      if(!target) return;
      target.title=nextTitle;
      target.display_title=nextTitle;
      target._state_db_title=nextTitle;
    };
    const applyTitle=(nextTitle, updateDom=true)=>{
      applyLocalTitle(session, nextTitle);
      const cached=_allSessions.find(item=>item&&item.session_id===session.session_id);
      applyLocalTitle(cached, nextTitle);
      if(S.session&&S.session.session_id===session.session_id){applyLocalTitle(S.session, nextTitle);syncTopbar();}
      if(updateDom) renderDisplay(_sessionDisplayTitle(session), session);
    };
    let finishDone=false;
    const finish=async(save)=>{
      if(finishDone) return;
      finishDone=true;
      const releaseRename=()=>{
        _renamingSid=null;
        if(inp.isConnected) inp.replaceWith(displayEl);
        setTimeout(()=>{ if(_renamingSid===null) renderSessionListFromCache(); },50);
      };
      if(!save){
        applyTitle(oldTitle,false);
        releaseRename();
        return;
      }
      const newTitle=inp.value.trim()||'Untitled';
      try{
        if(newTitle!==oldTitle){
          await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:session.session_id,title:newTitle})});
        }
        applyTitle(newTitle);
      }catch(err){
        applyTitle(oldTitle,false);
        const msg='Rename failed: '+(err&&err.message?err.message:String(err));
        setStatus(msg);
        if(typeof showToast==='function') showToast(msg,3000,'error');
      }finally{
        releaseRename();
      }
    };
    inp.onkeydown=e2=>{
      if(e2.key==='Enter'){
        if(window._isImeEnter&&window._isImeEnter(e2)){return;}
        e2.preventDefault();
        e2.stopPropagation();
        finish(true);
      }
      if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
    };
    inp.onblur=()=>{ if(_renamingSid===session.session_id) finish(true); };
    displayEl.replaceWith(inp);
    setTimeout(()=>{inp.focus();inp.select();},10);
  };
}

function _appendSessionCopyLinkAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_copy_link'),
    t('session_copy_link_desc'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      await _copySessionLink(session);
    }
  ));
}

function _sessionPublicShareUrl(session){
  const token=session&&session.share_token?String(session.share_token).trim():'';
  if(!token) return '';
  return new URL(`/share/${encodeURIComponent(token)}`,location.origin).href;
}

function _syncSessionShareState(session, nextSession){
  if(!session||!nextSession) return;
  session.share_token=nextSession.share_token||null;
  session.share_created_at=nextSession.share_created_at||null;
  const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);
  if(cached){
    cached.share_token=session.share_token;
    cached.share_created_at=session.share_created_at;
  }
  if(S.session&&S.session.session_id===session.session_id){
    S.session.share_token=session.share_token;
    S.session.share_created_at=session.share_created_at;
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  }
  renderSessionListFromCache();
  void renderSessionList();
}

async function _createOrRefreshSessionShare(session){
  if(!session||!session.session_id) return;
  const existing=_sessionPublicShareUrl(session);
  if(existing){
    const reuse=await showConfirmDialog({
      title:t('share_session'),
      message:t('share_session_existing_confirm'),
      confirmLabel:t('share_session_copy_existing'),
      cancelLabel:t('share_session_refresh_snapshot'),
    });
    if(reuse){
      let copied=true;
      try{ await _copyTextToClipboard(existing); }catch(_){ copied=false; }
      showToast(copied?t('share_session_link_copied'):(t('share_session_status_active')+' — '+existing),copied?2500:6000);
      window.open(existing,'_blank','noopener');
      return;
    }
  }
  const res=await api('/api/share/create',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
  if(res&&res.session) _syncSessionShareState(session,res.session);
  const href=new URL(String(res&&res.share&&res.share.url||''),location.origin).href;
  // The share is now created server-side. A clipboard-copy failure (permissions,
  // focus, non-secure context) must NOT be reported as "Share failed" — the link
  // exists and we still open it. Only surface the copied-vs-not-copied distinction.
  let copied=true;
  try{ await _copyTextToClipboard(href); }catch(_){ copied=false; }
  if(copied){
    showToast(existing?t('share_session_link_copied'):t('share_session_created'));
  }else{
    showToast((existing?t('share_session_created'):t('share_session_created'))+' — '+href,6000);
  }
  window.open(href,'_blank','noopener');
}

async function _revokeSessionShare(session){
  if(!session||!session.session_id||!session.share_token) return;
  const ok=await showConfirmDialog({
    title:t('stop_sharing_session'),
    message:t('stop_sharing_session_confirm'),
    confirmLabel:t('stop_sharing_session'),
    danger:true,
  });
  if(!ok) return;
  const res=await api('/api/share/revoke',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
  if(res&&res.session) _syncSessionShareState(session,res.session);
  showToast(t('share_session_revoked'));
}

function _appendSessionShareActions(menu, session){
  const hasMessages=Number(session&&session.message_count||0)>0;
  if(!hasMessages) return;
  menu.appendChild(_buildSessionAction(
    t('share_session'),
    session&&session.share_token?t('share_session_status_active'):t('share_session_tooltip'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      try{
        await _createOrRefreshSessionShare(session);
      }catch(err){
        showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    },
    session&&session.share_token?'is-active':''
  ));
  if(!(session&&session.share_token)) return;
  menu.appendChild(_buildSessionAction(
    t('share_session_copy_existing'),
    t('share_session_tooltip'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      try{
        const href=_sessionPublicShareUrl(session);
        if(!href) return;
        await _copyTextToClipboard(href);
        showToast(t('share_session_link_copied'));
      }catch(err){
        showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    }
  ));
  menu.appendChild(_buildSessionAction(
    t('stop_sharing_session'),
    t('stop_sharing_session_tooltip'),
    ICONS.trash,
    async()=>{
      closeSessionActionMenu();
      try{
        await _revokeSessionShare(session);
      }catch(err){
        showToast(t('share_session_revoke_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    },
    'danger'
  ));
}

function _appendSessionDuplicateAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_duplicate'),
    t('session_duplicate_desc'),
    ICONS.dup,
    async()=>{
      closeSessionActionMenu();
      try{
        const res=await api('/api/session/duplicate',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
        if(res.session){
          await loadSession(res.session.session_id);
          await renderSessionList();
          showToast(t('session_duplicated'));
        }
      }catch(err){showToast(t('session_duplicate_failed')+err.message);}
    }
  ));
}

function _appendSessionExportHtmlAction(menu, session){
  // Per-conversation "Export as HTML" — the sidebar ⋮ menu is the app's uniform
  // home for per-conversation actions (matches ChatGPT / Open WebUI). Operates
  // on THIS row's session, not just the active one; the export endpoint accepts
  // any session_id in the active profile and is non-mutating, so it's offered
  // for read-only/imported sessions too. exportSessionHTML(session) is a global
  // defined in boot.js (loaded after sessions.js under defer, so it's bound by
  // the time this click can fire).
  menu.appendChild(_buildSessionAction(
    t('session_export_html'),
    t('session_export_html_desc'),
    ICONS.download,
    ()=>{
      closeSessionActionMenu();
      if(typeof exportSessionHTML==='function') exportSessionHTML(session);
    }
  ));
}

function _playSessionActionMenuEntrance(menu){
  if(!menu) return;
  const reduce=_sessionPrefersReducedMotion();
  if(reduce) return;
  if(typeof menu.animate==='function'){
    try{
      const anim=menu.animate(
        [
          {opacity:0, transform:'translate3d(0,-4px,0) scale(.985)'},
          {opacity:1, transform:'translate3d(0,0,0) scale(1)'}
        ],
        {duration:450, easing:'cubic-bezier(.2,.8,.2,1)'}
      );
      if(anim&&anim.finished) anim.finished.catch(()=>{});
      return;
    }catch(_){}
  }
  menu.classList.add('open-animated');
}

async function _archiveSession(session, archived=true, beforeListRender=null){
  if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000); return false; }
  const reflowPositions=_captureSessionReflowPositions();
  const renderHold=beforeListRender?Promise.resolve().then(beforeListRender):null;
  try{
    const response=await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived})});
    session.archived=archived;
    const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);
    if(cached) cached.archived=archived;
    if(S.session&&S.session.session_id===session.session_id) S.session.archived=archived;
    try{ if(archived&&session.session_id&&localStorage.getItem('hermes-webui-session')===session.session_id) localStorage.removeItem('hermes-webui-session'); }catch(_){ }
    showToast(session.archived?_sessionArchiveToast(response,session):t('session_restored'));
    if(renderHold) await renderHold;
    if(_showArchived&&!_sessionPrefersReducedMotion()) _sessionSwipeReturnOffsets.set(session.session_id,'0px');
    _pendingSessionReflowPositions=reflowPositions;
    renderSessionListFromCache();
    void renderSessionList();
    return true;
  }catch(err){if(renderHold) await renderHold.catch(()=>{});_pendingSessionReflowPositions=null;showToast(t('session_archive_failed')+err.message);return false;}
}

function _openSessionActionMenu(session, anchorEl){
  const isReadOnly = _isReadOnlySession(session);
  if(_sessionActionMenu && _sessionActionSessionId===session.session_id && _sessionActionAnchor===anchorEl){
    closeSessionActionMenu();
    return;
  }
  closeSessionActionMenu();
  const isMessagingSession = _isMessagingSession(session);
  const isCliSession = _isCliSession(session);
  const isExternalSession = isMessagingSession || isCliSession;
  const menu=document.createElement('div');
  menu.className='session-action-menu';
  menu.id='sessionActionMenu-'+(++_sessionActionMenuId);
  menu.setAttribute('role','menu');
  menu.setAttribute('aria-label', 'Conversation actions');
  _appendSessionCopyLinkAction(menu, session);
  if(isReadOnly){
    _appendSessionExportHtmlAction(menu, session);
    _mountSessionActionMenu(menu, session, anchorEl);
    return;
  }
  // Rename — first menu item by request (#1764). Double-click rename is
  // timing-sensitive: the first click frequently registers as "open the
  // chat" before the second click arrives, so users open the conversation
  // when they meant to rename it. Putting Rename in the menu eliminates
  // the timing entirely. Only shown for sessions that support rename
  // (read-only imported sessions skip it; same gate as startRename's
  // _isReadOnlySession check).
  if(!_isReadOnlySession(session)){
    menu.appendChild(_buildSessionAction(
      t('session_rename'),
      t('session_rename_desc'),
      ICONS.edit,
      ()=>{
        closeSessionActionMenu();
        // Find the row for this session and call its attached startRename.
        // Falls back to a no-op toast if the row isn't currently rendered
        // (e.g. archived-and-hidden) — extremely rare since the menu only
        // opens from a visible row's three-dot button.
        const row=_findSessionRenameRow(session.session_id);
        if(row && typeof row._startRename === 'function'){
          row._startRename();
        } else if(typeof showToast==='function'){
          showToast(t('session_rename_failed_no_row')||'Could not start rename — row not found.', 3000, 'error');
        }
      }
    ));
  }
  _appendSessionShareActions(menu, session);
  menu.appendChild(_buildSessionAction(
    session.pinned?t('session_unpin'):t('session_pin'),
    session.pinned?t('session_unpin_desc'):t('session_pin_desc'),
    session.pinned?ICONS.pin:ICONS.unpin,
    async()=>{
      closeSessionActionMenu();
      const newPinned=!session.pinned;
      try{
        await api('/api/session/pin',{method:'POST',body:JSON.stringify({session_id:session.session_id,pinned:newPinned})});
        session.pinned=newPinned;
        const cached=(_allSessions||[]).find(s=>s&&s.session_id===session.session_id);
        if(cached) cached.pinned=newPinned;
        if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;
        renderSessionListFromCache();
        void renderSessionList();
      }catch(err){
        showToast(t('session_pin_failed')+err.message);
        await renderSessionList();
      }
    },
    session.pinned?'is-active':''
  ));
  menu.appendChild(_buildSessionAction(
    t('session_move_project'),
    session.project_id?t('session_move_project_desc_has'):t('session_move_project_desc_none'),
    ICONS.folder,
    async()=>{
      closeSessionActionMenu();
      _showProjectPicker(session, anchorEl);
    }
  ));
  menu.appendChild(_buildSessionAction(
    session.archived?t('session_restore'):t('session_archive'),
    session.archived?t('session_restore_desc'):_sessionArchiveDescription(session),
    session.archived?ICONS.unarchive:ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      await _archiveSession(session,!session.archived);
    }
  ));
  if(isExternalSession && !session.archived){
    menu.appendChild(_buildSessionAction(
      t('session_hide_external'),
      t('session_hide_external_desc'),
      ICONS.archive,
      async()=>{
        closeSessionActionMenu();
        try{
          await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived:true})});
          _optimisticallyArchiveSessionInList(session.session_id,true);
          session.archived=true;
          if(S.session&&S.session.session_id===session.session_id) S.session.archived=true;
          void renderSessionList();
          showToast(t('session_hidden'));
        }catch(err){showToast(t('session_archive_failed')+err.message);}
      }
    ));
  }
  if(!isExternalSession){
    _appendSessionDuplicateAction(menu, session);
  }
  _appendSessionExportHtmlAction(menu, session);
  if(session.active_stream_id){
    menu.appendChild(_buildSessionAction(
      t('session_stop_response'),
      t('session_stop_response_desc'),
      ICONS.stop,
      async()=>{
        closeSessionActionMenu();
        await cancelSessionStream(session);
        showToast(t('stream_stopped'));
      }
    ));
  }
  // Title regeneration stays available for writable imported sessions.
  // Read-only sessions return earlier through the shared action-menu guard.
  menu.appendChild(_buildSessionAction(
    t('session_title_regenerate'),
    t('session_title_regenerate_desc'),
    ICONS.spark,
    async()=>{
      closeSessionActionMenu();
      try{
        if(typeof showToast==='function') showToast(t('session_title_regenerating'), 1600);
        const requestOpts={method:'POST',body:JSON.stringify({session_id:session.session_id})};
        const timeoutMs=await _manualTitleRegenerateTimeoutMs();
        if(timeoutMs) requestOpts.timeoutMs=timeoutMs;
        const response=await api('/api/session/title/regenerate',requestOpts);
        const nextTitle=(response&&response.title)||(response&&response.session&&response.session.title)||'';
        if(nextTitle){
          session.title=nextTitle;
          const cached=(_allSessions||[]).find(item=>item&&item.session_id===session.session_id);
          if(cached) cached.title=nextTitle;
          if(S.session&&S.session.session_id===session.session_id){S.session.title=nextTitle;syncTopbar();}
          renderSessionListFromCache();
        }
        if(typeof showToast==='function') showToast(t('session_title_regenerated', nextTitle||t('untitled')), 2400);
      }catch(err){
        const msg=t('session_title_regenerate_failed')+(err&&err.message?err.message:String(err));
        setStatus(msg);
        if(typeof showToast==='function') showToast(msg,3000,'error');
      }
    }
  ));
  if(!isExternalSession){
    if(session.worktree_path){
      menu.appendChild(_buildSessionAction(
        t('session_worktree_remove'),
        t('session_worktree_remove_desc', session.worktree_path),
        ICONS.trash,
        async()=>{
          closeSessionActionMenu();
          await removeWorktree(session);
        },
        'danger'
      ));
    }
    menu.appendChild(_buildSessionAction(
      t('session_delete'),
      _sessionDeleteDescription(session),
      ICONS.trash,
      async()=>{
        closeSessionActionMenu();
        // Menu Delete has no swipe/removal animation to wait for. Pass an
        // immediate beforeDelete hook so deleteSession() removes the sidebar row
        // optimistically while slow backend cleanup (/api/session/delete,
        // state.db/FTS/journal cleanup) continues.
        await deleteSession(session.session_id,()=>Promise.resolve());
      },
      'danger'
    ));
  }
  _mountSessionActionMenu(menu, session, anchorEl);
}

document.addEventListener('click',e=>{
  if(!_sessionActionMenu) return;
  if(_sessionActionMenu.contains(e.target)) return;
  if(_sessionActionAnchor && _sessionActionAnchor.contains(e.target)) return;
  closeSessionActionMenu();
});
document.addEventListener('scroll',e=>{
  if(!_sessionActionMenu) return;
  if(_sessionActionMenu.contains(e.target)) return;
  if(_sessionActionMenuShouldIgnoreScrollTarget(e.target)) return;
  if(_sessionActionMenuShouldRepositionOnScroll(e.target) && _sessionActionAnchor){
    if(!_sessionActionAnchor.isConnected){
      closeSessionActionMenu();
      return;
    }
    _positionSessionActionMenu(_sessionActionAnchor);
    return;
  }
  closeSessionActionMenu();
}, true);
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && _sessionActionMenu) closeSessionActionMenu({restoreFocus:true});
});
window.addEventListener('resize',()=>{
  if(_sessionActionMenu && _sessionActionAnchor) _positionSessionActionMenu(_sessionActionAnchor);
});

// Generation counter to discard stale API responses (issue #1430).
// Multiple callers (message send, rename, session switch) fire renderSessionList()
// concurrently. Without this guard, a slower older response can overwrite _allSessions
// with stale data, causing sessions to vanish from the sidebar.
let _renderSessionListGen = 0;
let _renderSessionListInFlight = null;
let _renderSessionListQueuedRequest = null;
let _sessionListRefreshAnimationPending = false;
let _sessionListFirstRenderAnimated = false;
let _sessionListEnterAllAnimationPending = false;

// #4671: invalidate any session-list render that is in flight or queued. Called at
// profile-switch start (with showSessionListSkeleton) so a pre-switch /api/sessions
// response — which carries the OLD profile's rows but was issued before the switch
// bumped the generation, so it would otherwise pass the _renderSessionListGen guard,
// clear the skeleton flag, and paint stale rows over the skeleton — is discarded.
// Bumping the generation makes every outstanding response stale; clearing the
// pending/queued payloads drops a deferred apply that would do the same.
function _invalidateSessionListRenders(){
  _renderSessionListGen++;
  sessionStateBindings._pendingSessionListPayload = null;
  _renderSessionListQueuedRequest = null;
  // A retry whose fetch is invalidated here (e.g. a profile switch mid-retry)
  // would otherwise leave the error note stuck as an inert "Retrying…" button
  // with no request in flight — the stale fetch returns before
  // _showSessionListLoadError and the .finally() bails when the old button was
  // removed. Clear the pending retry markers so the next repaint shows an
  // actionable idle Retry again.
  if(sessionStateBindings._sessionListLoadError && (sessionStateBindings._sessionListLoadError.retrying || sessionStateBindings._sessionListLoadError._retryFailedFocus)){
    sessionStateBindings._sessionListLoadError = {...sessionStateBindings._sessionListLoadError};
    delete sessionStateBindings._sessionListLoadError.retrying;
    delete sessionStateBindings._sessionListLoadError._retryFailedFocus;
  }
}
if(typeof window!=='undefined') window._invalidateSessionListRenders = _invalidateSessionListRenders;

// #4671: profile-switch session-list EMBARGO. Point-in-time invalidation isn't enough —
// a renderSessionList() can START after the skeleton is shown but BEFORE /api/profile/switch
// returns (the profile cookie is only set by the switch response), so that GET fetches the
// OLD profile's rows, passes the generation guard, and clobbers the skeleton. While the
// embargo is on, _runRenderSessionListRefresh drops ALL payloads (none may paint), so only
// the switch-owned render — which runs after the switch clears the embargo — replaces the
// skeleton. The switch sets it before showSessionListSkeleton() and clears it immediately
// before its own renderSessionList() (and in the failure-restore path).
export const sidebarControls=Object.freeze({setUrl:_setActiveSessionUrl,toggleSelect:toggleSessionSelectMode,closeActionMenu:closeSessionActionMenu,copyLink:_copySessionLink,openActionMenu:_openSessionActionMenu,archive:_archiveSession});

export { NO_PROJECT_FILTER, SESSION_ARCHIVED_MAX_LOADED_LIMIT, SESSION_ARCHIVED_PAGE_SIZE, SESSION_VIRTUAL_BUFFER_ROWS, SESSION_VIRTUAL_ROW_HEIGHT, SESSION_VIRTUAL_THRESHOLD_ROWS, SHOW_ALL_PROFILES_STORAGE_KEY, _appRootPath, _archiveSession, _buildSessionRenameStarter, _captureSessionReflowPositions, _composerPrefillIntentFromLocation, _consumeComposerPrefillParamsFromLocation, _consumeProfileQueryParamFromLocation, _expandedChildSessionKeys, _expandedLineageKeys, _invalidateSessionListRenders, _lineageReportCache, _lineageReportInflight, _makeSessionSwipeAffordance, _openSessionActionMenu, _optimisticallyRemoveSessionFromList, _optimisticallyRemovedSessionIds, _playSessionRowsReflowFromPositions, _profileQueryIntentFromLocation, _renderBatchActionBar, _selectedSessions, _sessionActionMenu, _sessionAttentionSoundState, _sessionIdFromLocation, _sessionPrefersReducedMotion, _sessionResponseRetainsWorktree, _sessionSnapshotById, _sessionSwipeReturnOffsets, _sessionUrlForSid, _setActiveSessionUrl, _setShowAllProfiles, _waitForSessionMotion, closeSessionActionMenu, exitSessionSelectMode, selectAllSessions, setSessionSelected, toggleSessionSelect, toggleSessionSelectMode };

export const sidebarStateBindings=Object.freeze({
  get _activeProject(){ return _activeProject; },
  set _activeProject(value){ _activeProject=value; },
  get _allProjects(){ return _allProjects; },
  set _allProjects(value){ _allProjects=value; },
  get _allSessions(){ return _allSessions; },
  set _allSessions(value){ _allSessions=value; },
  get _allSessionsScope(){ return _allSessionsScope; },
  set _allSessionsScope(value){ _allSessionsScope=value; },
  get _archivedCliCount(){ return _archivedCliCount; },
  set _archivedCliCount(value){ _archivedCliCount=value; },
  get _archivedRowsLoadedLimit(){ return _archivedRowsLoadedLimit; },
  set _archivedRowsLoadedLimit(value){ _archivedRowsLoadedLimit=value; },
  get _archivedWebuiCount(){ return _archivedWebuiCount; },
  set _archivedWebuiCount(value){ _archivedWebuiCount=value; },
  get _lineageReportCacheGeneration(){ return _lineageReportCacheGeneration; },
  set _lineageReportCacheGeneration(value){ _lineageReportCacheGeneration=value; },
  get _otherProfileCount(){ return _otherProfileCount; },
  set _otherProfileCount(value){ _otherProfileCount=value; },
  get _pendingSessionReflowPositions(){ return _pendingSessionReflowPositions; },
  set _pendingSessionReflowPositions(value){ _pendingSessionReflowPositions=value; },
  get _profileSwitchOpeningExistingSession(){ return _profileSwitchOpeningExistingSession; },
  set _profileSwitchOpeningExistingSession(value){ _profileSwitchOpeningExistingSession=value; },
  get _renamingSid(){ return _renamingSid; },
  set _renamingSid(value){ _renamingSid=value; },
  get _renderSessionListGen(){ return _renderSessionListGen; },
  set _renderSessionListGen(value){ _renderSessionListGen=value; },
  get _renderSessionListInFlight(){ return _renderSessionListInFlight; },
  set _renderSessionListInFlight(value){ _renderSessionListInFlight=value; },
  get _renderSessionListQueuedRequest(){ return _renderSessionListQueuedRequest; },
  set _renderSessionListQueuedRequest(value){ _renderSessionListQueuedRequest=value; },
  get _serverCliSessionCount(){ return _serverCliSessionCount; },
  set _serverCliSessionCount(value){ _serverCliSessionCount=value; },
  get _serverWebuiSessionCount(){ return _serverWebuiSessionCount; },
  set _serverWebuiSessionCount(value){ _serverWebuiSessionCount=value; },
  get _sessionAttentionSoundPrimed(){ return _sessionAttentionSoundPrimed; },
  set _sessionAttentionSoundPrimed(value){ _sessionAttentionSoundPrimed=value; },
  get _sessionListEnterAllAnimationPending(){ return _sessionListEnterAllAnimationPending; },
  set _sessionListEnterAllAnimationPending(value){ _sessionListEnterAllAnimationPending=value; },
  get _sessionListFirstRenderAnimated(){ return _sessionListFirstRenderAnimated; },
  set _sessionListFirstRenderAnimated(value){ _sessionListFirstRenderAnimated=value; },
  get _sessionListRefreshAnimationPending(){ return _sessionListRefreshAnimationPending; },
  set _sessionListRefreshAnimationPending(value){ _sessionListRefreshAnimationPending=value; },
  get _sessionSelectMode(){ return _sessionSelectMode; },
  set _sessionSelectMode(value){ _sessionSelectMode=value; },
  get _sessionSourceFilter(){ return _sessionSourceFilter; },
  set _sessionSourceFilter(value){ _sessionSourceFilter=value; },
  get _sessionVirtualScrollList(){ return _sessionVirtualScrollList; },
  set _sessionVirtualScrollList(value){ _sessionVirtualScrollList=value; },
  get _sessionVirtualScrollRaf(){ return _sessionVirtualScrollRaf; },
  set _sessionVirtualScrollRaf(value){ _sessionVirtualScrollRaf=value; },
  get _sessionVisibleSidebarIds(){ return _sessionVisibleSidebarIds; },
  set _sessionVisibleSidebarIds(value){ _sessionVisibleSidebarIds=value; },
  get _showAllProfiles(){ return _showAllProfiles; },
  set _showAllProfiles(value){ _showAllProfiles=value; },
  get _showArchived(){ return _showArchived; },
  set _showArchived(value){ _showArchived=value; },
  get _sidebarReferenceSessions(){ return _sidebarReferenceSessions; },
  set _sidebarReferenceSessions(value){ _sidebarReferenceSessions=value; },
});
