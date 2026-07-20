import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { sidebarStateBindings } from './sidebar-store.js';

// ── Project helpers ─────────────────────────────────────────────────────

const PROJECT_COLORS=['#7cb9ff','#f5c542','#e94560','#50c878','#c084fc','#fb923c','#67e8f9','#f472b6'];

function _showProjectPicker(session, anchorEl){
  document.querySelectorAll('.project-picker').forEach(p=>p.remove());
  const picker=document.createElement('div');
  picker.className='project-picker';
  const none=document.createElement('div');
  none.className='project-picker-item'+(!session.project_id?' active':'');
  none.textContent='No project';
  none.onclick=async()=>{
    picker.remove();
    document.removeEventListener('click',close);
    try {
      await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:null})});
      const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===session.session_id);
      if(idx>=0) sidebarStateBindings._allSessions[idx].project_id=null;
      renderSessionListFromCache();
      showToast('Removed from project');
    } catch(e) {
      showToast('Unassign failed: '+(e.message||e));
    }
  };
  picker.appendChild(none);
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
        const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===session.session_id);
        if(idx>=0) sidebarStateBindings._allSessions[idx].project_id=p.project_id;
        renderSessionListFromCache();
        showToast('Moved to '+p.name);
      }catch(e){showToast('Move failed: '+(e.message||e));}
    };
    picker.appendChild(item);
  }
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
  document.body.appendChild(picker);
  const rect=anchorEl.getBoundingClientRect();
  picker.style.position='fixed';
  picker.style.zIndex='999';
  const spaceBelow=window.innerHeight-rect.bottom;
  if(spaceBelow<160&&rect.top>160){
    picker.style.bottom=(window.innerHeight-rect.top+4)+'px';
    picker.style.top='auto';
  }else{
    picker.style.top=(rect.bottom+4)+'px';
    picker.style.bottom='auto';
  }
  const pickerW=Math.min(220,Math.max(160,picker.scrollWidth||160));
  let left=rect.right-pickerW;
  if(left<8) left=8;
  picker.style.left=left+'px';
  const close=(e)=>{if(!picker.contains(e.target)&&e.target!==anchorEl){picker.remove();document.removeEventListener('click',close);}};
  setTimeout(()=>document.addEventListener('click',close),0);
}

function _resizeProjectInput(inp){
  const sizer=document.createElement('span');
  const cs=getComputedStyle(inp);
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
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:140px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  menu.style.left=e.clientX+'px';
  menu.style.top=e.clientY+'px';

  const renameItem=document.createElement('div');
  renameItem.textContent='Rename';
  renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
  renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
  renameItem.onmouseleave=()=>renameItem.style.background='';
  renameItem.onclick=()=>{menu.remove();_startProjectRename(proj,chip);};
  menu.appendChild(renameItem);

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

export { _showProjectContextMenu, _showProjectPicker, _startProjectCreate, _startProjectRename };
