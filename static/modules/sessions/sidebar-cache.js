import { _selectedSessions, sidebarStateBindings } from './sidebar-store.js';
import { _dropStaleOptimisticSessionRow } from './session-list-reconciliation.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

function _sessionSnapshotById(sid){
  if(!sid)return null;
  if(S.session&&S.session.session_id===sid) return S.session;
  return (sidebarStateBindings._allSessions||[]).find(s=>s&&s.session_id===sid)||null;
}
function _pinnedSessionCount(){
  return (sidebarStateBindings._allSessions||[]).filter(s=>s&&s.pinned&&!s.archived).length;
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
  if(!sid||!Array.isArray(sidebarStateBindings._allSessions)) return;
  let changed=false;
  sidebarStateBindings._allSessions=sidebarStateBindings._allSessions.map(s=>{
    if(!s||s.session_id!==sid) return s;
    changed=true;
    return {...s,archived:!!archived};
  });
  if(changed) renderSessionListFromCache();
}
function _optimisticallyRemoveSessionFromList(sid){
  if(!sid||!Array.isArray(sidebarStateBindings._allSessions)) return;
  const before=sidebarStateBindings._allSessions.length;
  sidebarStateBindings._allSessions=sidebarStateBindings._allSessions.filter(s=>!s||s.session_id!==sid);
  if(_selectedSessions&&_selectedSessions.has(sid)) _selectedSessions.delete(sid);
  if(typeof _dropStaleOptimisticSessionRow==='function') _dropStaleOptimisticSessionRow(sid);
  if(sidebarStateBindings._allSessions.length!==before) renderSessionListFromCache();
}


export { _getPinnedSessionsLimit, _optimisticallyArchiveSessionInList, _optimisticallyRemoveSessionFromList, _pinnedSessionCount, _pinnedSessionsLimitMessage, _sessionArchiveDescription, _sessionArchiveToast, _sessionDeleteDescription, _sessionResponseRetainsWorktree, _sessionSnapshotById, _worktreeResponseCount, _worktreeSessionCount };
