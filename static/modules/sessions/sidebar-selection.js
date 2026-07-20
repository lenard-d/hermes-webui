import { loadSession } from './lifecycle.js';
import { _clearHandoffStorageForSession, _sessionListQueryString } from './message-loading.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _sessionResponseRetainsWorktree, _sessionSnapshotById, _worktreeResponseCount, _worktreeSessionCount } from './sidebar-cache.js';
import { _selectedSessions, sidebarStateBindings } from './sidebar-store.js';

function toggleSessionSelectMode(){
  sidebarStateBindings._sessionSelectMode=!sidebarStateBindings._sessionSelectMode;
  _selectedSessions.clear();
  renderSessionListFromCache();
}
function exitSessionSelectMode(){
  sidebarStateBindings._sessionSelectMode=false;
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
  const ids=Array.isArray(sidebarStateBindings._sessionVisibleSidebarIds)&&sidebarStateBindings._sessionVisibleSidebarIds.length
    ? sidebarStateBindings._sessionVisibleSidebarIds
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
  for(const p of(sidebarStateBindings._allProjects||[])){
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


export { _renderBatchActionBar, deselectAllSessions, exitSessionSelectMode, selectAllSessions, setSessionSelected, toggleSessionSelect, toggleSessionSelectMode };
