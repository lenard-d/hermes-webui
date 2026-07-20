import { loadSession } from './lifecycle.js';
import { _clearHandoffStorageForSession, _sessionListQueryString } from './message-loading.js';
import { SHOW_ALL_PROFILES_STORAGE_KEY, _optimisticallyRemovedSessionIds, sidebarStateBindings } from './sidebar-store.js';
import { _captureSessionReflowPositions } from './sidebar-motion.js';
import { _optimisticallyRemoveSessionFromList, _sessionResponseRetainsWorktree, _sessionSnapshotById } from './sidebar-cache.js';
import { _sessionIdFromLocation } from './session-navigation.js';
import { exitSessionSelectMode } from './sidebar-selection.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

async function _handleActiveSessionStorageEvent(e){
  if(!e || e.key !== 'hermes-webui-session') return;
  // Do not treat localStorage as a global active-session bus. Each tab owns its
  // active conversation via its URL (/session/<id>), so another tab switching
  // sessions must not force this tab to navigate away from an in-flight turn.
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

async function _handleShowAllProfilesStorageEvent(e){
  if(!e || e.key !== SHOW_ALL_PROFILES_STORAGE_KEY) return;
  const next=e.newValue==='1'||e.newValue==='true';
  if(sidebarStateBindings._showAllProfiles===next) return;
  sidebarStateBindings._showAllProfiles=next;
  if(typeof renderSessionList==='function') await renderSessionList({deferWhileInteracting:false});
}

if(typeof window!=='undefined'){
  window.addEventListener('storage', (e) => {
    void _handleActiveSessionStorageEvent(e);
    void _handleShowAllProfilesStorageEvent(e);
  });
  window.addEventListener('popstate', () => {
    const sid=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;
    if(!sid || (S.session && S.session.session_id===sid)) return;
    // Refuse to switch sessions mid-stream — same UX guard the storage-event
    // handler had. A user mid-turn who hits browser Back should NOT lose the
    // active stream. They can hit Back again once the turn ends.
    if(S.busy){
      if(typeof showToast==='function') showToast('Finish the current turn before switching sessions.',3000);
      return;
    }
    void loadSession(sid);
  });
}

async function removeWorktree(session){
  // Fetch status first
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
  // Build confirm message
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
    if(status.dirty){
      details+='\n\n'+t('session_worktree_remove_dirty_warning');
    }
    if(status.untracked_count>0){
      details+='\n'+t('session_worktree_remove_untracked_warning',status.untracked_count);
    }
    if(status.ahead_behind&&status.ahead_behind.ahead>0){
      details+='\n'+t('session_worktree_remove_ahead_warning',status.ahead_behind.ahead);
    }
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
    // Clear the worktree_path from cached session so menu doesn't show stale remove action
    if(session.worktree_path){
      session.worktree_path=null;
    }
    // Re-render the list if this is the active session
    if(S.session&&S.session.session_id===session.session_id&&S.session.worktree_path){
      S.session.worktree_path=null;
    }
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
    // load the most recent remaining session, or show blank if none left
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

// ── Project helpers ─────────────────────────────────────────────────────

const PROJECT_COLORS=['#7cb9ff','#f5c542','#e94560','#50c878','#c084fc','#fb923c','#67e8f9','#f472b6'];

function _showProjectPicker(session, anchorEl){
  // Close any existing picker
  document.querySelectorAll('.project-picker').forEach(p=>p.remove());
  const picker=document.createElement('div');
  picker.className='project-picker';
  // "No project" option
  const none=document.createElement('div');
  none.className='project-picker-item'+(!session.project_id?' active':'');
  none.textContent='No project';
  none.onclick=async()=>{
    picker.remove();
    document.removeEventListener('click',close);
    try {
      await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:null})});
      // Sidebar rows are shallow copies of _allSessions entries (see
      // _attachChildSessionsToSidebarRows), so mutating `session` only updates
      // the discarded copy. Write into the authoritative cache so the next
      // renderSessionListFromCache() reflects the move. (#2551)
      const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===session.session_id);
      if(idx>=0) sidebarStateBindings._allSessions[idx].project_id=null;
      renderSessionListFromCache();
      showToast('Removed from project');
    } catch(e) {
      showToast('Unassign failed: '+(e.message||e));
    }
  };
  picker.appendChild(none);
  // Project options — only show projects matching the session's profile.
  // #3331 follow-up (Codex gate): mirror the server's root-alias tolerance —
  // `_profiles_match` treats the literal 'default' and a renamed-root display
  // name as equivalent, so a server-approved `profile:'default'` project must
  // not be hidden for a session stamped with the renamed-root profile (and
  // vice versa). Only hide when BOTH sides are explicit, distinct, AND neither
  // is the 'default' alias; let the server's allowlist be authoritative for the
  // default/renamed-root case.
  const sessionProfile = session ? (session.profile || undefined) : undefined;
  const _profileHidesProject = (projProfile) => {
    if(!sessionProfile || !projProfile) return false;
    if(projProfile === sessionProfile) return false;
    if(projProfile === 'default' || sessionProfile === 'default') return false;
    return true;
  };
  for(const p of sidebarStateBindings._allProjects){
    if (_profileHidesProject(p.profile)) continue;
    const item=document.createElement('div');
    item.className='project-picker-item'+(session.project_id===p.project_id?' active':'');
    if(p.color){
      const dot=document.createElement('span');
      dot.className='color-dot';
      dot.style.cssText='width:6px;height:6px;border-radius:50%;background:'+p.color+';flex-shrink:0;';
      item.appendChild(dot);
    }
    const name=document.createElement('span');
    name.textContent=p.name;
    item.appendChild(name);
    item.onclick=async()=>{
      picker.remove();
      document.removeEventListener('click',close);
      try{
        await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:p.project_id})});
        // See #2551 — write to _allSessions, not the shallow sidebar copy.
        const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===session.session_id);
        if(idx>=0) sidebarStateBindings._allSessions[idx].project_id=p.project_id;
        renderSessionListFromCache();
        showToast('Moved to '+p.name);
      }catch(e){showToast('Move failed: '+(e.message||e));}
    };
    picker.appendChild(item);
  }
  // "+ New project" shortcut at the bottom
  const createItem=document.createElement('div');
  createItem.className='project-picker-item project-picker-create';
  createItem.textContent='+ New project';
  createItem.onclick=async()=>{
    picker.remove();
    document.removeEventListener('click',close);
    const name=await showPromptDialog({
      message:t('project_name_prompt'),
      confirmLabel:t('create'),
      placeholder:'Project name'
    });
    if(!name||!name.trim()) return;
    const color=PROJECT_COLORS[sidebarStateBindings._allProjects.length%PROJECT_COLORS.length];
    const profile = session.profile || undefined;
    const res=await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:name.trim(),color,profile})});
    if(res.project){
      sidebarStateBindings._allProjects.push(res.project);
      // Guard the move so a 503 (session busy/streaming, #3746) shows a toast
      // instead of an unhandled rejection. Keep the authoritative refetch (#2551).
      try{
        await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:res.project.project_id})});
        session.project_id=res.project.project_id;
        await renderSessionList();
        showToast('Created "'+res.project.name+'" and moved session');
      }catch(e){
        await renderSessionList();
        showToast('Created "'+res.project.name+'" but move failed: '+(e&&e.message||'try again'));
      }
    }
  };
  picker.appendChild(createItem);
  // Append to body and position using getBoundingClientRect so it isn't clipped
  // by overflow:hidden on .session-item ancestors
  document.body.appendChild(picker);
  const rect=anchorEl.getBoundingClientRect();
  picker.style.position='fixed';
  picker.style.zIndex='999';
  // Prefer opening below; flip above if too close to bottom of viewport
  const spaceBelow=window.innerHeight-rect.bottom;
  if(spaceBelow<160&&rect.top>160){
    picker.style.bottom=(window.innerHeight-rect.top+4)+'px';
    picker.style.top='auto';
  }else{
    picker.style.top=(rect.bottom+4)+'px';
    picker.style.bottom='auto';
  }
  // Align right edge of picker with right edge of button; keep within viewport
  const pickerW=Math.min(220,Math.max(160,picker.scrollWidth||160));
  let left=rect.right-pickerW;
  if(left<8) left=8;
  picker.style.left=left+'px';
  // Close on outside click
  const close=(e)=>{if(!picker.contains(e.target)&&e.target!==anchorEl){picker.remove();document.removeEventListener('click',close);}};
  setTimeout(()=>document.addEventListener('click',close),0);
}

// Resize a .project-create-input to fit its current value (or placeholder).
// Bounded by the CSS min-width:40px / max-width:180px on the same class so
// the input is never comically tiny nor wider than the project bar.
// Uses a hidden span sized with the same font/padding to measure text width.
function _resizeProjectInput(inp){
  const sizer=document.createElement('span');
  const cs=getComputedStyle(inp);
  // Read font from the live element so the sizer stays calibrated if CSS changes.
  // Horizontal padding only (0 vertical) — we're measuring width, not height.
  sizer.style.cssText='position:absolute;visibility:hidden;white-space:pre;';
  sizer.style.fontSize=cs.fontSize;
  sizer.style.fontFamily=cs.fontFamily;
  sizer.style.padding='0 '+cs.paddingRight;
  sizer.textContent=inp.value||inp.placeholder||' ';
  document.body.appendChild(sizer);
  const w=Math.min(180,Math.max(40,sizer.offsetWidth+2));
  document.body.removeChild(sizer);
  inp.style.width=w+'px';
}

function _startProjectCreate(bar, addBtn){
  const inp=document.createElement('input');
  inp.className='project-create-input';
  inp.placeholder='Project name';
  let _finishDone=false;
  const finish=async(save)=>{
    if(_finishDone) return;
    _finishDone=true;
    if(save&&inp.value.trim()){
      const color=PROJECT_COLORS[sidebarStateBindings._allProjects.length%PROJECT_COLORS.length];
      try{
        await api('/api/projects/create',{method:'POST',body:JSON.stringify({name:inp.value.trim(),color})});
      }catch(e){
        _finishDone=false;
        showToast('Project create failed: '+(e.message||e));
        return;
      }
      await renderSessionList();
      showToast('Project created');
    }else{
      inp.replaceWith(addBtn);
    }
  };
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){
      if(window._isImeEnter&&window._isImeEnter(e)){return;}
      e.preventDefault();
      finish(true);
    }
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(true);
  inp.addEventListener('input',()=>_resizeProjectInput(inp));
  addBtn.replaceWith(inp);
  _resizeProjectInput(inp);
  setTimeout(()=>inp.focus(),10);
}

function _startProjectRename(proj, chip){
  const inp=document.createElement('input');
  inp.className='project-create-input';
  inp.value=proj.name;
  let _finishDone=false;
  const finish=async(save)=>{
    if(_finishDone) return;
    _finishDone=true;
    if(save&&inp.value.trim()&&inp.value.trim()!==proj.name){
      try {
        await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:inp.value.trim()})});
        await renderSessionList();
        showToast('Project renamed');
      } catch(e) {
        _finishDone=false;
        showToast('Rename failed: '+(e.message||e));
      }
    }else{
      renderSessionListFromCache();
    }
  };
  inp.onkeydown=(e)=>{
    if(e.key==='Enter'){
      if(window._isImeEnter&&window._isImeEnter(e)){return;}
      e.preventDefault();
      finish(true);
    }
    if(e.key==='Escape'){e.preventDefault();finish(false);}
  };
  inp.onblur=()=>finish(true);
  inp.onclick=(e)=>e.stopPropagation();
  inp.addEventListener('input',()=>_resizeProjectInput(inp));
  chip.replaceWith(inp);
  _resizeProjectInput(inp);
  setTimeout(()=>{inp.focus();inp.select();},10);
}

function _showProjectContextMenu(e, proj, chip){
  document.querySelectorAll('.project-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='project-ctx-menu';
  // background: var(--surface) — fully-opaque theme variable (not var(--panel),
  // which is undefined in this codebase and falls back to transparent, letting
  // the session list show through the menu). Same variable used by
  // .session-action-menu and other floating popovers.
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:140px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  menu.style.left=e.clientX+'px';
  menu.style.top=e.clientY+'px';

  // Rename option
  const renameItem=document.createElement('div');
  renameItem.textContent='Rename';
  renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
  renameItem.onmouseleave=()=>renameItem.style.background='';
  renameItem.onclick=()=>{menu.remove();_startProjectRename(proj,chip);};
  menu.appendChild(renameItem);

  // Color picker row
  const colorRow=document.createElement('div');
  colorRow.style.cssText='display:flex;gap:5px;padding:7px 14px;align-items:center;';
  PROJECT_COLORS.forEach(hex=>{
    const dot=document.createElement('span');
    dot.style.cssText=`width:16px;height:16px;border-radius:50%;background:${hex};cursor:pointer;display:inline-block;flex-shrink:0;`;
    if(hex===(proj.color||'')) dot.style.outline='2px solid var(--text)';
    dot.onclick=async()=>{
      menu.remove();
      try {
        await api('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:proj.project_id,name:proj.name,color:hex})});
        await renderSessionList();
        showToast('Color updated');
      } catch(e) {
        showToast('Color update failed: '+(e.message||e));
      }
    };
    colorRow.appendChild(dot);
  });
  menu.appendChild(colorRow);

  // Divider + Delete
  const sep=document.createElement('hr');
  sep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
  menu.appendChild(sep);
  const delItem=document.createElement('div');
  delItem.textContent='Delete';
  delItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--error,#e94560);';
  delItem.onmouseenter=()=>delItem.style.background='var(--hover-bg)';
  delItem.onmouseleave=()=>delItem.style.background='';
  delItem.onclick=()=>{menu.remove();_confirmDeleteProject(proj);};
  menu.appendChild(delItem);

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

async function _confirmDeleteProject(proj){
  const ok=await showConfirmDialog({
    message:'Delete project "'+proj.name+'"? Sessions will be unassigned but not deleted.',
    confirmLabel:t('delete_title'),
    danger:true
  });
  if(!ok){return;}
  try {
    await api('/api/projects/delete',{method:'POST',body:JSON.stringify({project_id:proj.project_id})});
    if(sidebarStateBindings._activeProject===proj.project_id) sidebarStateBindings._activeProject=null;
    await renderSessionList();
    showToast('Project deleted');
  } catch(e) {
    showToast('Delete failed: '+(e.message||e));
  }
}

// Global Escape handler for batch select mode
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'&&sidebarStateBindings._sessionSelectMode) exitSessionSelectMode();
});

// Keyboard session navigation — J/K bindings
function navigateSession(dir){
  const rows=[...document.querySelectorAll('.session-item[data-sid]')];
  const sids=rows.map(r=>r.dataset.sid);
  const cur=S.session&&S.session.session_id;
  const i=sids.indexOf(cur);
  if(i<0||!sids.length)return;
  const next=sids[Math.min(Math.max(i+dir,0),sids.length-1)];
  if(next&&next!==cur) loadSession(next);
}

document.addEventListener('keydown',(e)=>{
  if(e.key!=='j'&&e.key!=='k') return;
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  if(typeof _isInteractiveSwipeTarget==='function'&&_isInteractiveSwipeTarget(e.target)) return;
  e.preventDefault();
  navigateSession(e.key==='j'?1:-1);
});
export const sessionManagement=Object.freeze({removeWorktree:removeWorktree,deleteSession:deleteSession,showProjectPicker:_showProjectPicker,navigate:navigateSession});

export { _showProjectContextMenu, _showProjectPicker, _startProjectCreate, _startProjectRename, deleteSession, removeWorktree };
