import { isNewSessionInFlight, newSession } from './session-lifecycle-port.js';
import { _setActiveProjectFilter } from './sidebar-session-opening.js';
import { NO_PROJECT_FILTER, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _showProjectContextMenu, _startProjectCreate, _startProjectRename } from './session-projects.js';

function _attachProjectQuickCreateButton(chip, project){
  const btn=document.createElement('button');
  btn.type='button';
  btn.className='project-chip-quick-create';
  btn.textContent='+';
  btn.title='New conversation in this project';
  btn.setAttribute('aria-label','New conversation in this project');
  const stop=function(e){
    if(!e) return;
    if(typeof e.preventDefault==='function') e.preventDefault();
    if(typeof e.stopPropagation==='function') e.stopPropagation();
    if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
  };
  const stopTouchBubble=function(e){
    if(!e) return;
    if(typeof e.stopPropagation==='function') e.stopPropagation();
    if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
  };
  btn.onclick=async(e)=>{
    stop(e);
    if(isNewSessionInFlight()){
      // The initiating tap already owns the filter change and rollback path.
      try{
        await newSession(false,{project_id:project.project_id});
      }catch(_){
        // The initiating tap already owns the visible failure path.
      }
      return;
    }
    const previousProject=(typeof sidebarStateBindings._activeProject!=='undefined')?sidebarStateBindings._activeProject:NO_PROJECT_FILTER;
    _setActiveProjectFilter(project.project_id);
    try{
      await newSession(false,{project_id:project.project_id});
      // newSession() does not repaint the sidebar (callers own that — see the
      // newSession contract). Repaint from the post-create state so the new
      // project-assigned session appears deterministically.
      try{ if(typeof renderSessionListFromCache==='function') renderSessionListFromCache(); }catch(_){}
      try{ if(typeof renderSessionList==='function') void renderSessionList({deferWhileInteracting:false}); }catch(_){}
    }catch(err){
      _setActiveProjectFilter(previousProject);
      if(typeof showToast==='function') showToast('New conversation failed: '+(err&&err.message||err));
    }
  };
  btn.ondblclick=(e)=>{stop(e);};
  btn.oncontextmenu=(e)=>{stop(e);};
  btn.ontouchstart=(e)=>{stopTouchBubble(e);};
  btn.ontouchend=(e)=>{stopTouchBubble(e);};
  chip.appendChild(btn);
}

function renderSidebarProjectBar(profileFiltered, list){
  // Project filter bar — show when there are real projects OR there are
  // unassigned sessions (so the Unassigned chip has something to filter to).
  const hasUnprojected=profileFiltered.some(s=>!s.project_id);
  if(sidebarStateBindings._allProjects.length>0||hasUnprojected){
    const bar=document.createElement('div');
    bar.className='project-bar';
    // "All" chip
    const allChip=document.createElement('span');
    allChip.className='project-chip'+(!sidebarStateBindings._activeProject?' active':'');
    allChip.textContent='All';
    allChip.onclick=()=>{_setActiveProjectFilter(null);};
    bar.appendChild(allChip);
    // "Unassigned" chip — only when there are sessions with no project to
    // filter to. Hidden in the common case where every session is already
    // organized, to keep the chip bar uncluttered.
    if(hasUnprojected){
      const noneChip=document.createElement('span');
      noneChip.className='project-chip no-project'+(sidebarStateBindings._activeProject===NO_PROJECT_FILTER?' active':'');
      noneChip.textContent='Unassigned';
      noneChip.title='Show conversations not yet assigned to a project';
      noneChip.onclick=()=>{_setActiveProjectFilter(NO_PROJECT_FILTER);};
      bar.appendChild(noneChip);
    }
    // Project chips
    for(const p of sidebarStateBindings._allProjects){
      const chip=document.createElement('span');
      chip.className='project-chip'+(p.project_id===sidebarStateBindings._activeProject?' active':'');
      if(p.color){
        const dot=document.createElement('span');
        dot.className='color-dot';
        dot.style.background=p.color;
        chip.appendChild(dot);
      }
      const nameSpan=document.createElement('span');
      nameSpan.textContent=p.name;
      chip.appendChild(nameSpan);
      let _pClickTimer=null;
      chip.onclick=(e)=>{
        clearTimeout(_pClickTimer);
        _pClickTimer=setTimeout(()=>{_pClickTimer=null;_setActiveProjectFilter(p.project_id);},220);
      };
      chip.ondblclick=(e)=>{e.stopPropagation();clearTimeout(_pClickTimer);_pClickTimer=null;_startProjectRename(p,chip);};
      chip.oncontextmenu=(e)=>{e.preventDefault();_showProjectContextMenu(e,p,chip);};
      // Touch long-press → context menu (mobile UX: project chips can only be
      // deleted via the right-click menu, which has no touch equivalent).
      let _lpTimer=null;
      let _lpHandled=false;
      let _lpStartX=0,_lpStartY=0;
      chip.addEventListener('touchstart',(e)=>{
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        // Clear any in-flight timer before scheduling a new one, mirroring the
        // session-item long-press path (_clearLongPressTimer). Without this a
        // second finger / stray touchstart orphans the prior timer, which then
        // fires unsuppressed ~500ms later and pops the menu after the gesture
        // was cancelled.
        if(_lpTimer){clearTimeout(_lpTimer);_lpTimer=null;}
        _lpHandled=false;_lpStartX=t.clientX;_lpStartY=t.clientY;
        chip.classList.add('long-pressing');
        _lpTimer=setTimeout(()=>{
          _lpTimer=null;
          if(_lpHandled) return;  // already consumed by another gesture — stale fire is a no-op
          _lpHandled=true;
          chip.classList.remove('long-pressing');
          clearTimeout(_pClickTimer);_pClickTimer=null;
          const syn={clientX:t.clientX,clientY:t.clientY,preventDefault:()=>{}};
          _showProjectContextMenu(syn,p,chip);
        },500);
      },{passive:true});
      chip.addEventListener('touchmove',(e)=>{
        if(!_lpTimer) return;
        const t=e.changedTouches&&e.changedTouches[0];
        if(!t) return;
        if(Math.abs(t.clientX-_lpStartX)>10||Math.abs(t.clientY-_lpStartY)>10){
          clearTimeout(_lpTimer);_lpTimer=null;
          chip.classList.remove('long-pressing');
        }
      },{passive:true});
      chip.addEventListener('touchend',(e)=>{
        clearTimeout(_lpTimer);_lpTimer=null;
        chip.classList.remove('long-pressing');
        if(_lpHandled){e.preventDefault();e.stopPropagation();}
      },{passive:false});
      chip.addEventListener('touchcancel',()=>{
        clearTimeout(_lpTimer);_lpTimer=null;_lpHandled=false;
        chip.classList.remove('long-pressing');
      },{passive:true});
      if(window._projectQuickCreate) _attachProjectQuickCreateButton(chip,p);
      bar.appendChild(chip);
    }
    // Create button
    const addBtn=document.createElement('button');
    addBtn.className='project-create-btn';
    addBtn.textContent='+';
    addBtn.title='New project';
    addBtn.onclick=(e)=>{e.stopPropagation();_startProjectCreate(bar,addBtn);};
    bar.appendChild(addBtn);
    list.appendChild(bar);
  }
}

export const sidebarProjectControls=Object.freeze({
  render:renderSidebarProjectBar,
  attachQuickCreate:_attachProjectQuickCreateButton,
});

export { _attachProjectQuickCreateButton, renderSidebarProjectBar };
