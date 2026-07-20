import { loadSession } from './session-lifecycle-port.js';
import { _clearHandoffStorageForSession } from './handoff-lifecycle.js';
import { _sessionListQueryString } from './sidebar-session-opening.js';
import { renderSessionList } from './session-list-render-port.js';
import { _optimisticallyRemoveSessionFromList, _sessionResponseRetainsWorktree, _sessionSnapshotById } from './sidebar-cache.js';
import { _captureSessionReflowPositions } from './sidebar-motion.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _optimisticallyRemovedSessionIds, sidebarStateBindings } from './sidebar-store.js';

async function removeWorktree(session){
  let status=null;
  try{
    const statusResp=await api('/api/session/worktree/status?session_id='+encodeURIComponent(session.session_id));
    status=statusResp.status;
  }catch(e){
    showToast(t('session_worktree_remove_status_failed')+e.message,0,'error');
    return;
  }
  if(!status){
    showToast(t('session_worktree_remove_status_failed'),0,'error');
    return;
  }
  let details='';
  if(!status.exists){
    details=t('session_worktree_remove_not_exists',status.path);
  }else{
    details=t('session_worktree_remove_confirm',status.path);
    if(status.locked_by_stream){
      showToast(t('session_worktree_remove_locked_by_stream'),0,'error');
      return;
    }
    if(status.locked_by_terminal){
      showToast(t('session_worktree_remove_locked_by_terminal'),0,'error');
      return;
    }
    if(status.dirty) details+='\n\n'+t('session_worktree_remove_dirty_warning');
    if(status.untracked_count>0) details+='\n'+t('session_worktree_remove_untracked_warning',status.untracked_count);
    if(status.ahead_behind&&status.ahead_behind.ahead>0) details+='\n'+t('session_worktree_remove_ahead_warning',status.ahead_behind.ahead);
    if(status.dirty||status.untracked_count>0||(status.ahead_behind&&status.ahead_behind.ahead>0)){
      showToast(t('session_worktree_remove_failed')+t('session_worktree_remove_unsafe_blocked'),0,'error');
      await showConfirmDialog({
        message:details,
        confirmLabel:t('dialog_confirm_btn'),
        danger:true,
        focusCancel:true
      });
      return;
    }
  }
  const ok=await showConfirmDialog({
    message:details,
    confirmLabel:t('session_worktree_remove_confirm_label'),
    danger:true
  });
  if(!ok)return;
  try{
    const result=await api('/api/session/worktree/remove',{
      method:'POST',
      body:JSON.stringify({session_id:session.session_id, force:false})
    });
    const warn=result.warnings&&result.warnings.length?(' '+result.warnings.join(' ')):'';
    showToast(t('session_worktree_removed')+warn);
    if(session.worktree_path) session.worktree_path=null;
    if(S.session&&S.session.session_id===session.session_id&&S.session.worktree_path) S.session.worktree_path=null;
    await renderSessionList();
  }catch(e){
    showToast(t('session_worktree_remove_failed')+e.message,0,'error');
  }
}

async function deleteSession(sid, beforeDelete=null){
  const session=_sessionSnapshotById(sid);
  const ok=await showConfirmDialog({
    message:session&&session.worktree_path?t('session_delete_worktree_confirm',session.worktree_path):t('session_delete_confirm'),
    confirmLabel:t('delete_title'),
    danger:true
  });
  if(!ok)return false;
  const reflowPositions=_captureSessionReflowPositions();
  const beforeDeleteHold=beforeDelete?Promise.resolve().then(beforeDelete):null;
  const previousSessions=sidebarStateBindings._allSessions;
  let optimisticRendered=false;
  const deleteRequest=api('/api/session/delete',{method:'POST',body:JSON.stringify({session_id:sid})}).then(response=>{
    _clearHandoffStorageForSession(sid);
    return {response};
  }, error=>({error}));
  if(beforeDeleteHold){
    await beforeDeleteHold;
    _optimisticallyRemovedSessionIds.add(sid);
    sidebarStateBindings._pendingSessionReflowPositions=reflowPositions;
    _optimisticallyRemoveSessionFromList(sid);
    optimisticRendered=true;
  }
  const deleteResult=await deleteRequest;
  if(deleteResult&&deleteResult.error){
    sidebarStateBindings._pendingSessionReflowPositions=null;
    if(optimisticRendered){
      _optimisticallyRemovedSessionIds.delete(sid);
      sidebarStateBindings._allSessions=previousSessions;
      renderSessionListFromCache();
    }
    const err=deleteResult.error;
    setStatus(`Delete failed: ${err&&err.message?err.message:String(err)}`);
    return false;
  }
  const response=deleteResult&&deleteResult.response;
  const cleanupFailed=!!(response&&response.state_db_cleanup_failed);
  if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);
  if(!optimisticRendered){
    sidebarStateBindings._pendingSessionReflowPositions=reflowPositions;
    _optimisticallyRemoveSessionFromList(sid);
  }
  if(S.session&&S.session.session_id===sid){
    S.session=null;S.messages=[];S.entries=[];
    if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(null);
    localStorage.removeItem('hermes-webui-session');
    const remaining=await api('/api/sessions'+_sessionListQueryString());
    if(remaining.sessions&&remaining.sessions.length){
      await loadSession(remaining.sessions[0].session_id);
    }else{
      const _tt=$('topbarTitle');if(_tt)_tt.textContent=assistantDisplayName();
      const _tm=$('topbarMeta');if(_tm)_tm.textContent='Start a new conversation';
      $('msgInner').innerHTML='';
      $('emptyState').style.display='';
      $('fileTree').innerHTML='';
      if(typeof S!=='undefined') S.session=null;
      if(typeof syncAppTitlebar==='function') syncAppTitlebar();
    }
  }
  if(cleanupFailed) showToast(t('delete_failed'),0,'error');
  else showToast(_sessionResponseRetainsWorktree(response,session)?t('session_deleted_worktree'):t('session_deleted'));
  if(optimisticRendered) void renderSessionList().finally(()=>_optimisticallyRemovedSessionIds.delete(sid));
  else await renderSessionList();
  return !cleanupFailed;
}

export { deleteSession, removeWorktree };
