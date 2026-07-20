import { _profileMatchesActiveProfile } from './session-profile-scope.js';
import { _getChannelLabel, _isCliSession, _isExternalSession, _isMessagingSession, _sourceKeyForSession } from './session-source.js';
import { loadSession } from './session-lifecycle-port.js';
import { NO_PROJECT_FILTER, SESSION_ARCHIVED_MAX_LOADED_LIMIT, SESSION_ARCHIVED_PAGE_SIZE, _selectedSessions, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

function _externalImportPayload(session) {
  const payload = {session_id: session.session_id};
  if (sidebarStateBindings._showAllProfiles && session && typeof session.profile === 'string' && session.profile) {
    payload.all_profiles = true;
    payload.profile = session.profile;
  }
  return payload;
}

function _sidebarSessionProfileName(session){
  const raw=session&&typeof session.profile==='string'?session.profile.trim():'';
  return raw||'';
}

async function _ensureSidebarSessionProfile(session){
  const targetProfile=_sidebarSessionProfileName(session);
  if(!sidebarStateBindings._showAllProfiles||!targetProfile) return false;
  const activeProfile=S.activeProfile||'default';
  if(_profileMatchesActiveProfile(targetProfile,activeProfile)) return false;
  if(typeof switchToProfile!=='function') return false;
  sidebarStateBindings._profileSwitchOpeningExistingSession=true;
  try{
    await switchToProfile(targetProfile);
  }finally{
    sidebarStateBindings._profileSwitchOpeningExistingSession=false;
  }
  return _profileMatchesActiveProfile(targetProfile,S.activeProfile||'default');
}

async function _openSidebarSession(session, loadOpts={}){
  if(!session||!session.session_id) return;
  // Extension pre-open hook — before any side-effects (external import, profile switching).
  // Handler returns {cancel:true} to prevent the open.
  if(!loadOpts.skipExtHooks && typeof _hermesNotifySessionOpen==='function'){
    var _preResult=_hermesNotifySessionOpen(session.session_id, null, {preload:true, opts:loadOpts});
    if(_preResult&&_preResult.cancel===true) return;
  }
  // #5409: close mobile sidebar AFTER veto guard passes — only close if open proceeds.
  if(typeof closeMobileSidebar==='function')closeMobileSidebar();
  if(_isExternalSession(session)){
    try{await api('/api/session/import_cli',{method:'POST',body:JSON.stringify(_externalImportPayload(session))});}
    catch(_e){ /* import failed -- fall through to read-only view */ }
  }
  await _ensureSidebarSessionProfile(session);
  // Tell loadSession to skip its pre-hook — we already ran it above.
  await loadSession(session.session_id, Object.assign({}, loadOpts, {_preloadNotified:true}));
  renderSessionListFromCache();
}

function _isReadOnlySession(session) {
  return !!(session && (session.read_only || session.is_read_only));
}

function _isBranchableReadOnlySession(session) {
  if (!_isReadOnlySession(session)) return false;
  const sources = [
    session && session.source_tag,
    session && session.raw_source,
    session && session.source,
  ].map(v => String(v || '').trim().toLowerCase());
  return sources.includes('cron');
}

function _sessionSourceLabel(filter, count) {
  const n = Number(count) || 0;
  return filter === 'cli' ? `CLI sessions (${n})` : `WebUI sessions (${n})`;
}

function _clearSessionSourceTabCounts() {
  sidebarStateBindings._serverWebuiSessionCount = null;
  sidebarStateBindings._serverCliSessionCount = null;
}

function _requestedSessionSidebarSource() {
  return window._showCliSessions ? sidebarStateBindings._sessionSourceFilter : 'webui';
}

function _sessionListExcludeHiddenEnabled() {
  return sidebarStateBindings._activeProject===null || sidebarStateBindings._activeProject===NO_PROJECT_FILTER;
}

function _sessionArchivePagingFilterActive() {
  let searchActive=false;
  try{
    const searchEl=typeof $==='function' ? $('sessionSearch') : null;
    searchActive=Boolean(searchEl&&String(searchEl.value||'').trim());
  }catch(_e){ searchActive=false; }
  return Boolean(searchActive||sidebarStateBindings._activeProject);
}

function _sessionListQueryString() {
  const qs = new URLSearchParams();
  qs.set('sidebar_source', _requestedSessionSidebarSource());
  if(_sessionListExcludeHiddenEnabled()) qs.set('exclude_hidden','1');
  if(sidebarStateBindings._showAllProfiles) qs.set('all_profiles','1');
  if(sidebarStateBindings._showArchived){
    qs.set('include_archived','1');
    if(!_sessionArchivePagingFilterActive()){
      const archiveLimit=Math.min(
        SESSION_ARCHIVED_MAX_LOADED_LIMIT,
        Math.max(SESSION_ARCHIVED_PAGE_SIZE, Number(sidebarStateBindings._archivedRowsLoadedLimit)||SESSION_ARCHIVED_PAGE_SIZE)
      );
      qs.set('archived_limit', String(archiveLimit));
    }
  }
  return `?${qs.toString()}`;
}

function _sessionSourceTabCount(filter, renderedWebuiSessionCount, renderedCliSessionCount) {
  const serverCount = filter === 'cli' ? sidebarStateBindings._serverCliSessionCount : sidebarStateBindings._serverWebuiSessionCount;
  if (Number.isFinite(serverCount)) return serverCount;
  return filter === 'cli' ? renderedCliSessionCount : renderedWebuiSessionCount;
}

function _setActiveProjectFilter(projectId) {
  const next = projectId === NO_PROJECT_FILTER ? NO_PROJECT_FILTER : (projectId || null);
  if (sidebarStateBindings._activeProject === next) return;
  sidebarStateBindings._activeProject = next;
  renderSessionListFromCache();
  void renderSessionList({deferWhileInteracting:false});
}

function _setSessionSourceFilter(filter) {
  const next = filter === 'cli' ? 'cli' : 'webui';
  if (sidebarStateBindings._sessionSourceFilter === next) return;
  sidebarStateBindings._sessionSourceFilter = next;
  sidebarStateBindings._activeProject = null;
  _selectedSessions.clear();
  sidebarStateBindings._sessionSelectMode = false;
  try { localStorage.setItem('hermes-session-source-filter', next); } catch (_e) {}
  renderSessionListFromCache();
  void renderSessionList({deferWhileInteracting:false});
}

function _restoreSessionSourceFilter() {
  try {
    const raw = localStorage.getItem('hermes-session-source-filter');
    if (raw === 'cli' || raw === 'webui') sidebarStateBindings._sessionSourceFilter = raw;
  } catch (_e) {}
}

function _normalizeMessageForCliImportComparison(message) {
  if (!message || typeof message !== 'object') return message;
  const clone = { ...message };
  delete clone.timestamp;
  delete clone._ts;
  return clone;
}

function _isCliImportRefreshPrefixMatch(localMessages, freshMessages) {
  if (!Array.isArray(localMessages) || !Array.isArray(freshMessages)) return false;
  if (localMessages.length > freshMessages.length) return false;
  for (let i = 0; i < localMessages.length; i += 1) {
    if (JSON.stringify(_normalizeMessageForCliImportComparison(localMessages[i])) !== JSON.stringify(_normalizeMessageForCliImportComparison(freshMessages[i]))) {
      return false;
    }
  }
  return true;
}

export const sidebarSessionOpening=Object.freeze({open:_openSidebarSession,setProjectFilter:_setActiveProjectFilter,setSourceFilter:_setSessionSourceFilter,queryString:_sessionListQueryString});

export { _clearSessionSourceTabCounts, _externalImportPayload, _getChannelLabel, _isBranchableReadOnlySession, _isCliImportRefreshPrefixMatch, _isCliSession, _isExternalSession, _isMessagingSession, _isReadOnlySession, _openSidebarSession, _requestedSessionSidebarSource, _restoreSessionSourceFilter, _sessionArchivePagingFilterActive, _sessionListExcludeHiddenEnabled, _sessionListQueryString, _sessionSourceLabel, _sessionSourceTabCount, _setActiveProjectFilter, _setSessionSourceFilter, _sourceKeyForSession };
