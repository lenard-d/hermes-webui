import { showConfirmDialog } from './dialogs-and-reconnect.js';
import { showToast } from './composer.js';
import { $, S } from './state.js';
import {
  deleteWorkspaceDir,
  deleteWorkspaceFile,
  _showFileContextMenu,
} from './workspace-file-actions.js';
import {
  _bindWorkspaceMoveDropTarget,
  _clearWorkspaceMoveDragOver,
  _clearWsDragData,
  _setWsDragData,
} from './workspace-drag-drop.js';
import { _visibleWorkspaceEntries } from './workspace-preferences.js';

function fileIcon(name, type){
  if(type==='dir') return li('folder',14);
  const e=fileExt(name);
  if(IMAGE_EXTS.has(e)) return li('image',14);
  if(MD_EXTS.has(e))    return li('file-text',14);
  if(typeof DOWNLOAD_EXTS!=='undefined'&&DOWNLOAD_EXTS.has(e)) return li('download',14);
  if(e==='.py')   return li('file-code',14);
  if(e==='.js'||e==='.ts'||e==='.jsx'||e==='.tsx') return li('zap',14);
  if(e==='.json'||e==='.yaml'||e==='.yml'||e==='.toml') return li('settings',14);
  if(e==='.sh'||e==='.bash') return li('terminal',14);
  if(e==='.pdf') return li('download',14);
  return li('file-text',14);
}

function renderBreadcrumb(){
  const bar=$('breadcrumbBar');
  const upBtn=$('btnUpDir');
  if(!bar)return;
  if(S.currentDir==='.'){
    bar.style.display='none';
    if(upBtn)upBtn.style.display='none';
    return;
  }
  bar.style.display='flex';
  if(upBtn)upBtn.style.display='';
  bar.innerHTML='';
  const root=document.createElement('span');
  root.className='breadcrumb-seg breadcrumb-link';
  root.textContent='~';
  root.onclick=()=>loadDir('.');
  _bindWorkspaceMoveDropTarget(root,'.');
  _bindWorkspaceOsUploadDropTarget(root,'.');
  bar.appendChild(root);
  const parts=S.currentDir.split('/');
  let accumulated='';
  for(let i=0;i<parts.length;i++){
    const sep=document.createElement('span');
    sep.className='breadcrumb-sep';sep.textContent='/';
    bar.appendChild(sep);
    accumulated+=(accumulated?'/':'')+parts[i];
    const seg=document.createElement('span');
    seg.textContent=parts[i];
    if(i<parts.length-1){
      seg.className='breadcrumb-seg breadcrumb-link';
      const target=accumulated;
      seg.onclick=()=>loadDir(target);
      _bindWorkspaceMoveDropTarget(seg,target);
      _bindWorkspaceOsUploadDropTarget(seg,target);
    } else {
      seg.className='breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

function _ensureWorkspaceTreeState(){
  // State is initialized lazily because native-module cycles link before
  // state.js initializes the shared session state object.
  if(!S._expandedDirs) S._expandedDirs=new Set();
  if(!S._dirCache) S._dirCache={};
}

function renderFileTree(){
  _ensureWorkspaceTreeState();
  const box=$('fileTree');
  // Preserve scroll through the DOM replacement. Expand/collapse only changes
  // rows below the clicked disclosure, so restoring scrollTop is sufficient.
  const prevScrollTop=box?box.scrollTop:0;
  box.innerHTML='';
  S._dirCache[S.currentDir||'.']=S.entries;
  const emptyEl=$('wsEmptyState');
  const hasWorkspace=!!(S.session&&S.session.workspace);
  if(!hasWorkspace){
    if(emptyEl){emptyEl.textContent=t('workspace_empty_no_path');emptyEl.style.display='flex';}
    box.style.display='none';
    return;
  }
  if(emptyEl) emptyEl.style.display='none';
  box.style.display='';
  const visibleEntries=_visibleWorkspaceEntries(S.entries);
  if(!visibleEntries.length){
    if(emptyEl){emptyEl.textContent=t('workspace_empty_dir');emptyEl.style.display='flex';}
    return;
  }
  _renderTreeItems(box, visibleEntries, 0);
  if(box) box.scrollTop=prevScrollTop;
}

function elideMiddle(str, maxLen = 60) {
  if (str.length <= maxLen) return str;
  const half = Math.floor((maxLen - 3) / 2);
  return str.slice(0, half) + '...' + str.slice(str.length - half);
}

function _renderTreeItems(container, entries, depth){
  for(const item of entries){
    const el=document.createElement('div');el.className='file-item';
    el.style.paddingLeft=(8+depth*16)+'px';
    el.setAttribute('draggable','true');
    el.dataset.wsType=item.type;
    el.oncontextmenu=(e)=>{
      const grant=typeof _workspaceEscapeGrantForPath==='function' ? _workspaceEscapeGrantForPath(item.path) : null;
      const isDirRow=item.type==='dir'||(item.type==='symlink'&&item.is_dir);
      if(grant&&!isDirRow){e.preventDefault();e.stopPropagation();return;}
      e.preventDefault();e.stopPropagation();_showFileContextMenu(e,item);
    };
    el.ondragstart=(e)=>{_setWsDragData(e,item);e.dataTransfer.effectAllowed='copy';el.classList.add('dragging');};
    el.ondragend=()=>{el.classList.remove('dragging');_clearWorkspaceMoveDragOver();_clearWsDragData();};

    const isLk = item.type === 'symlink';
    const isExternalLink = isLk && item.target_outside_workspace;
    const escapeGrant = typeof _workspaceEscapeGrantForPath === 'function' ? _workspaceEscapeGrantForPath(item.path) : null;
    const exactEscapeGrant = typeof _workspaceEscapeExactGrant === 'function' ? _workspaceEscapeExactGrant(item.path) : null;
    const isReadOnlyEscape = !!escapeGrant;
    const isNestedEscape = !!escapeGrant && !exactEscapeGrant;
    const isDirLike = !isExternalLink && (item.type === 'dir' || (isLk && item.is_dir));
    const isFileLike = !isExternalLink && !isDirLike;
    el.dataset.wsIsDir = String(isDirLike);
    if(isExternalLink || isReadOnlyEscape){el.removeAttribute('draggable');el.ondragstart=null;}

    if(isDirLike){
      const arrow=document.createElement('span');
      arrow.className='file-tree-toggle';
      const isExpanded=S._expandedDirs.has(item.path);
      arrow.textContent=isExpanded?'\u25BE':'\u25B8';
      el.appendChild(arrow);
    }else{
      const spacer=document.createElement('span');
      spacer.className='file-tree-toggle-placeholder';
      spacer.setAttribute('aria-hidden','true');
      el.appendChild(spacer);
    }

    const iconEl=document.createElement('span');
    iconEl.className='file-icon';
    iconEl.innerHTML = isExternalLink
      ? li('external-link', 14)
      : isDirLike
        ? (isLk ? li('link', 14) : li('folder', 14))
        : (isLk ? li('link', 14) : fileIcon(item.name, item.type));
    el.appendChild(iconEl);

    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;
    if(isLk && item.target)
      nameEl.title = t('symlink_link_to').replace('{target}', () => elideMiddle(item.target));
    else if(isExternalLink)
      nameEl.title = (typeof isReadOnlyEscape!=='undefined'
        ? isReadOnlyEscape
        : (typeof _workspaceEscapeGrantForPath==='function' ? !!_workspaceEscapeGrantForPath(item.path) : false))
        ? t('external_link_read_only')
        : t('external_link_open_confirm');
    else if(typeof isReadOnlyEscape!=='undefined'
      ? isReadOnlyEscape
      : (typeof _workspaceEscapeGrantForPath==='function' ? !!_workspaceEscapeGrantForPath(item.path) : false))
      nameEl.title = t('external_link_read_only');
    else if(!isDirLike)
      nameEl.title = t('double_click_rename');
    const nameIsReadOnlyEscape=typeof isReadOnlyEscape!=='undefined'
      ? isReadOnlyEscape
      : (typeof _workspaceEscapeGrantForPath==='function' ? !!_workspaceEscapeGrantForPath(item.path) : false);
    let _nameClickTimer=null;
    nameEl.onclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      _nameClickTimer=setTimeout(()=>{
        _nameClickTimer=null;
        if(typeof el.onclick==='function')el.onclick(e);
      },300);
    };
    nameEl.ondblclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      if(isDirLike){loadDir(item.path);return;}
      if(nameIsReadOnlyEscape){
        if(isExternalLink){if(typeof el.onclick==='function')el.onclick(e);return;}
        openFile(item.path);
        return;
      }
      const inp=document.createElement('input');
      inp.className='file-rename-input';inp.value=item.name;
      inp.onclick=(e2)=>e2.stopPropagation();
      const finish=async(save)=>{
        inp.onblur=null;
        if(save){
          const newName=inp.value.trim();
          if(newName&&newName!==item.name){
            try{
              await api('/api/file/rename',{method:'POST',body:JSON.stringify({
                session_id:S.session.session_id,path:item.path,new_name:newName
              })});
              showToast(t('renamed_to')+newName);
              if(isDirLike&&S._expandedDirs){
                S._expandedDirs.delete(item.path);
                const parent=item.path.includes('/')?item.path.substring(0,item.path.lastIndexOf('/')):'.';
                const newPath=parent==='.'?newName:parent+'/'+newName;
                S._expandedDirs.add(newPath);
                if(S._dirCache[item.path]){S._dirCache[newPath]=S._dirCache[item.path];delete S._dirCache[item.path];}
                if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
              }
              delete S._dirCache[S.currentDir];
              await loadDir(S.currentDir);
            }catch(err){showToast(t('rename_failed')+err.message);}
          }
        }
        inp.replaceWith(nameEl);
      };
      inp.onkeydown=(e2)=>{
        if(e2.key==='Enter'){
          if(window._isImeEnter&&window._isImeEnter(e2)){return;}
          e2.preventDefault();
          finish(true);
        }
        if(e2.key==='Escape'){e2.preventDefault();finish(false);}
      };
      inp.onblur=()=>finish(false);
      nameEl.replaceWith(inp);
      setTimeout(()=>{inp.focus();inp.select();},10);
    };
    el.appendChild(nameEl);

    if(isFileLike&&item.size){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      sizeEl.textContent=`${(item.size/1024).toFixed(1)}k`;
      el.appendChild(sizeEl);
    }

    if(isFileLike){
      if(!isReadOnlyEscape){
        const del=document.createElement('button');
        del.className='file-del-btn';del.title=t('delete_title');del.textContent='\u00d7';
        del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceFile(item.path,item.name);};
        el.appendChild(del);
      }
    }else if(isDirLike&& !isReadOnlyEscape){
      const del=document.createElement('button');
      del.className='file-del-btn';del.title=t('delete_title');del.textContent='\u00d7';
      del.onclick=async(e)=>{e.stopPropagation();await deleteWorkspaceDir(item.path,item.name);};
      el.appendChild(del);
    }

    if(isDirLike){
      if(!isReadOnlyEscape){
        _bindWorkspaceMoveDropTarget(el,item.path);
        _bindWorkspaceOsUploadDropTarget(el,item.path);
      }
      el.onclick=async(e)=>{
        e.stopPropagation();
        if(S._expandedDirs.has(item.path)){
          S._expandedDirs.delete(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          renderFileTree();
        }else{
          S._expandedDirs.add(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          if(!S._dirCache[item.path]){
            try{
              const data=await api(_workspaceRouteForPath(item.path, 'list'));
              S._dirCache[item.path]=data.entries||[];
            }catch(e2){S._dirCache[item.path]=[];}
          }
          renderFileTree();
        }
      };
    }else if(isExternalLink){
      el.onclick=async(e)=>{
        e.stopPropagation();
        if(isNestedEscape){
          await showConfirmDialog({
            title:item.name,
            message:t('external_link_read_only'),
            confirmLabel:t('dialog_confirm_btn'),
            danger:false,
            hideCancel:true,
            focusCancel:false,
          });
          return;
        }
        const grant = await authorizeWorkspaceEscapeNavigation(item);
        if(!grant) return;
        if(grant.isDir) await loadDir(item.path);
        else await openFile(item.path);
      };
    }else{
      el.onclick=async()=>openFile(item.path);
    }

    container.appendChild(el);

    if(isDirLike&&S._expandedDirs.has(item.path)){
      const children=_visibleWorkspaceEntries(S._dirCache[item.path]||[]);
      if(children.length){
        _renderTreeItems(container, children, depth+1);
      }else{
        const empty=document.createElement('div');
        empty.className='file-item file-empty';
        empty.style.paddingLeft=(8+(depth+1)*16)+'px';
        empty.textContent=t('empty_dir');
        container.appendChild(empty);
      }
    }
  }
}

export {
  fileIcon,
  renderBreadcrumb,
  renderFileTree,
  elideMiddle,
  _renderTreeItems,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  fileIcon: { enumerable: true, get: () => fileIcon, set: (value) => { fileIcon = value; } },
  renderBreadcrumb: { enumerable: true, get: () => renderBreadcrumb, set: (value) => { renderBreadcrumb = value; } },
  renderFileTree: { enumerable: true, get: () => renderFileTree, set: (value) => { renderFileTree = value; } },
  elideMiddle: { enumerable: true, get: () => elideMiddle, set: (value) => { elideMiddle = value; } },
  _renderTreeItems: { enumerable: true, get: () => _renderTreeItems, set: (value) => { _renderTreeItems = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
