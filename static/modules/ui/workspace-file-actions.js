import { setStatus, showToast } from './composer.js';
import { showConfirmDialog, showPromptDialog } from './dialogs-and-reconnect.js';
import { $, S } from './state.js';
import { _workspaceParentDir } from './workspace-drag-drop.js';

function _workspaceContextMenuItem(label, onClick, opts={}){
  const item=document.createElement('div');
  item.textContent=label;
  item.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:'+(opts.danger?'var(--error,#e94560)':'var(--text)')+';';
  item.onmouseenter=()=>item.style.background='var(--hover-bg)';
  item.onmouseleave=()=>item.style.background='';
  item.onclick=onClick;
  return item;
}

function _copyTextWithFallback(text, successMsg, failurePrefix){
  const done=()=>showToast(successMsg);
  const fail=(err)=>showToast(failurePrefix+(err&&err.message?err.message:String(err||'')));
  if(navigator.clipboard&&navigator.clipboard.writeText){
    return navigator.clipboard.writeText(text).then(done).catch(err=>{
      const ta=document.createElement('textarea');
      ta.value=text;
      ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      let copied=false;
      try{copied=document.execCommand('copy');}catch(_){}
      ta.remove();
      if(copied) done(); else fail(err);
    });
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
  document.body.appendChild(ta);
  ta.select();
  let copied=false;
  try{copied=document.execCommand('copy');}catch(err){ta.remove();fail(err);return Promise.resolve();}
  ta.remove();
  if(copied) done(); else fail('clipboard unavailable');
  return Promise.resolve();
}

function _workspaceCreateTargetLabel(targetDir){
  return targetDir && targetDir !== '.' ? targetDir : t('workspace_root');
}

function _workspaceJoinTargetPath(targetDir, name){
  const cleanName=String(name||'').trim();
  if(!cleanName) return '';
  return (!targetDir||targetDir==='.') ? cleanName : `${targetDir}/${cleanName}`;
}

function _showWorkspaceRootContextMenu(e){
  document.querySelectorAll('.file-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='file-ctx-menu workspace-root-ctx-menu';
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:160px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  const vw=window.innerWidth,vh=window.innerHeight;
  menu.style.left=(e.clientX+160>vw?e.clientX-170:e.clientX)+'px';
  menu.style.top=(e.clientY+80>vh?e.clientY-80:e.clientY)+'px';

  menu.appendChild(_workspaceContextMenuItem(t('new_file'),async()=>{
    menu.remove();
    await promptNewFile('.');
  }));
  menu.appendChild(_workspaceContextMenuItem(t('new_folder'),async()=>{
    menu.remove();
    await promptNewFolder('.');
  }));

  const createSep=document.createElement('hr');
  createSep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
  menu.appendChild(createSep);

  menu.appendChild(_workspaceContextMenuItem(t('reveal_in_finder'),async()=>{
    menu.remove();
    try{await api('/api/file/reveal',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});}
    catch(err){showToast(t('reveal_failed')+(err.message||err));}
  }));
  menu.appendChild(_workspaceContextMenuItem(t('open_in_vscode'),async()=>{
    menu.remove();
    try{await api('/api/file/open-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});}
    catch(err){showToast(t('open_in_vscode_failed')+(err.message||err));}
  }));
  menu.appendChild(_workspaceContextMenuItem(t('copy_file_path'),async()=>{
    menu.remove();
    try{
      const r=await api('/api/file/path',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:'.'})});
      await _copyTextWithFallback((r&&r.path)||'.',t('path_copied'),t('path_copy_failed'));
    }catch(err){showToast(t('path_copy_failed')+(err.message||err));}
  }));

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

async function deleteWorkspaceDir(relPath, name){
  if(!S.session)return;
  if(typeof _workspacePathIsReadOnly==='function'&&_workspacePathIsReadOnly(relPath)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const ok=await showConfirmDialog({title:t('delete_dir_confirm',name),message:'',confirmLabel:'Delete',danger:true,focusCancel:true});
  if(!ok)return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,recursive:true})});
    showToast(t('deleted')+name);
    if(S._expandedDirs){S._expandedDirs.delete(relPath);if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();}
    delete S._dirCache[relPath];
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('delete_failed')+e.message);}
}

function _showFileContextMenu(e, item){
  document.querySelectorAll('.file-ctx-menu').forEach(el=>el.remove());
  const menu=document.createElement('div');
  menu.className='file-ctx-menu';
  menu.style.cssText='position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 0;z-index:9999;min-width:140px;box-shadow:0 4px 16px rgba(0,0,0,.35);';
  const vw=window.innerWidth,vh=window.innerHeight;
  menu.style.left=(e.clientX+140>vw?e.clientX-150:e.clientX)+'px';
  menu.style.top=(e.clientY+100>vh?e.clientY-100:e.clientY)+'px';
  const isDirLike=item.type==='dir'||(item.type==='symlink'&&item.is_dir);
  const targetDir=isDirLike ? item.path : _workspaceParentDir(item.path);
  const isReadOnlyEscape=typeof _workspaceEscapeGrantForPath==='function' ? !!_workspaceEscapeGrantForPath(item.path) : false;

  if(!isReadOnlyEscape){
    menu.appendChild(_workspaceContextMenuItem(t('new_file'),async()=>{
      menu.remove();
      await promptNewFile(targetDir);
    }));
    menu.appendChild(_workspaceContextMenuItem(t('new_folder'),async()=>{
      menu.remove();
      await promptNewFolder(targetDir);
    }));

    const createSep=document.createElement('hr');
    createSep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
    menu.appendChild(createSep);

    const renameItem=document.createElement('div');
    renameItem.textContent=t('rename_title');
    renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
    renameItem.onmouseleave=()=>renameItem.style.background='';
    renameItem.onclick=()=>{menu.remove();_inlineRenameFileItem(item);};
    menu.appendChild(renameItem);

    const revealItem=document.createElement('div');
    revealItem.textContent=t('reveal_in_finder');
    revealItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    revealItem.onmouseenter=()=>revealItem.style.background='var(--hover-bg)';
    revealItem.onmouseleave=()=>revealItem.style.background='';
    revealItem.onclick=async()=>{menu.remove();try{await api('/api/file/reveal',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('reveal_failed')+(err.message||err));}};
    menu.appendChild(revealItem);

    const vscodeItem=document.createElement('div');
    vscodeItem.textContent=t('open_in_vscode');
    vscodeItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    vscodeItem.onmouseenter=()=>vscodeItem.style.background='var(--hover-bg)';
    vscodeItem.onmouseleave=()=>vscodeItem.style.background='';
    vscodeItem.onclick=async()=>{menu.remove();try{await api('/api/file/open-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('open_in_vscode_failed')+(err.message||err));}};
    menu.appendChild(vscodeItem);

    const copyPathItem=document.createElement('div');
    copyPathItem.textContent=t('copy_file_path');
    copyPathItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    copyPathItem.onmouseenter=()=>copyPathItem.style.background='var(--hover-bg)';
    copyPathItem.onmouseleave=()=>copyPathItem.style.background='';
    copyPathItem.onclick=async()=>{
      menu.remove();
      try{
        const r=await api('/api/file/path',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});
        const abs=(r&&r.path)||item.path;
        try{
          await navigator.clipboard.writeText(abs);
          showToast(t('path_copied'));
        }catch(clipErr){
          const ta=document.createElement('textarea');
          ta.value=abs;
          ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
          document.body.appendChild(ta);
          ta.select();
          let copied=false;
          try{copied=document.execCommand('copy');}catch(_){}
          ta.remove();
          if(copied) showToast(t('path_copied'));
          else showToast(t('path_copy_failed')+(clipErr&&clipErr.message?clipErr.message:String(clipErr)));
        }
      }catch(err){
        showToast(t('path_copy_failed')+(err.message||err));
      }
    };
    menu.appendChild(copyPathItem);

    const copyRelPathItem=document.createElement('div');
    copyRelPathItem.textContent=t('copy_relative_path');
    copyRelPathItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    copyRelPathItem.onmouseenter=()=>copyRelPathItem.style.background='var(--hover-bg)';
    copyRelPathItem.onmouseleave=()=>copyRelPathItem.style.background='';
    copyRelPathItem.onclick=async()=>{
      menu.remove();
      try{
        const rel=_normalizeWorkspaceRelPath(item.path)||item.path;
        await _copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'));
      }catch(err){
        showToast(t('path_copy_failed')+(err.message||err));
      }
    };
    menu.appendChild(copyRelPathItem);
  }

  if(isDirLike){
    const dlItem=document.createElement('div');
    dlItem.textContent=t('download_folder');
    dlItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    dlItem.onmouseenter=()=>dlItem.style.background='var(--hover-bg)';
    dlItem.onmouseleave=()=>dlItem.style.background='';
    dlItem.onclick=()=>{
      menu.remove();
      const rel='/api/folder/download?session_id='+encodeURIComponent(S.session.session_id)
              + '&path='+encodeURIComponent(item.path||'');
      window.location.href=new URL(rel.slice(1), document.baseURI||location.href).href;
    };
    menu.appendChild(dlItem);
  }

  if(!isReadOnlyEscape){
    const sep=document.createElement('hr');
    sep.style.cssText='border:none;border-top:1px solid var(--border);margin:4px 0;';
    menu.appendChild(sep);
    const delItem=document.createElement('div');
    delItem.textContent=t('delete_title');
    delItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--error,#e94560);';
    delItem.onmouseenter=()=>delItem.style.background='var(--hover-bg)';
    delItem.onmouseleave=()=>delItem.style.background='';
    delItem.onclick=()=>{menu.remove();if(isDirLike)deleteWorkspaceDir(item.path,item.name);else deleteWorkspaceFile(item.path,item.name);};
    menu.appendChild(delItem);
  }

  document.body.appendChild(menu);
  const dismiss=()=>{menu.remove();document.removeEventListener('click',dismiss);};
  setTimeout(()=>document.addEventListener('click',dismiss),0);
}

async function _inlineRenameFileItem(item){
  if(!S.session)return;
  if(typeof _workspacePathIsReadOnly==='function'&&_workspacePathIsReadOnly(item.path)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const isDirLike=item.type==='dir'||(item.type==='symlink'&&item.is_dir);
  const newName=await showPromptDialog({
    message:t('rename_prompt'),
    value:item.name,
    confirmLabel:t('rename_title'),
    selectStem:!isDirLike,
    selectAll:isDirLike
  });
  if(!newName||newName===item.name)return;
  try{
    await api('/api/file/rename',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path,new_name:newName})});
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

async function deleteWorkspaceFile(relPath, name){
  if(!S.session)return;
  if(typeof _workspacePathIsReadOnly==='function'&&_workspacePathIsReadOnly(relPath)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const confirmed=await showConfirmDialog({title:t('delete_confirm',name),message:'',confirmLabel:'Delete',danger:true,focusCancel:true});
  if(!confirmed) return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('deleted')+name);
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('delete_failed')+e.message);}
}

async function _ensureWorkspaceSession(errorPrefix){
  if(S.session)return true;
  const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
  if(!ws)return false;
  try{
    // System-minted session (#6022): explicit worktree:false — creating a
    // workspace item from a blank page must not inherit the config default.
    const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws,worktree:false})});
    if(r&&r.session){
      S._pendingSessionToolsets=null;
      S.session=r.session;
      S.messages=[];
      if(typeof syncTopbar==='function')syncTopbar();
      if(typeof renderMessages==='function')renderMessages();
      if(typeof renderSessionList==='function')await renderSessionList();
    }
  }catch(e){setStatus(errorPrefix+e.message);return false;}
  return !!S.session;
}

async function promptNewFile(targetDir = S.currentDir || '.'){
  if(!await _ensureWorkspaceSession(t('create_failed')))return;
  if(typeof _workspacePathIsReadOnly==='function'&&_workspacePathIsReadOnly(targetDir)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const targetLabel=_workspaceCreateTargetLabel(targetDir);
  const name=await showPromptDialog({
    title:t('new_file_prompt_title', targetLabel),
    placeholder:'filename.txt',
    confirmLabel:t('create')
  });
  if(!name||!name.trim()) return;
  const relPath=_workspaceJoinTargetPath(targetDir,name);
  try{
    await api('/api/file/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath,content:''})});
    showToast(t('created')+name.trim());
    delete S._dirCache[targetDir || '.'];
    await loadDir(S.currentDir);
    openFile(relPath);
  }catch(e){setStatus(t('create_failed')+e.message);}
}

async function promptNewFolder(targetDir = S.currentDir || '.'){
  if(!await _ensureWorkspaceSession(t('folder_create_failed')))return;
  if(typeof _workspacePathIsReadOnly==='function'&&_workspacePathIsReadOnly(targetDir)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  const targetLabel=_workspaceCreateTargetLabel(targetDir);
  const name=await showPromptDialog({
    title:t('new_folder_prompt_title', targetLabel),
    placeholder:'folder-name',
    confirmLabel:t('create')
  });
  if(!name||!name.trim()) return;
  const relPath=_workspaceJoinTargetPath(targetDir,name);
  try{
    await api('/api/file/create-dir',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('folder_created')+name.trim());
    delete S._dirCache[targetDir || '.'];
    await loadDir(S.currentDir);
    const absPath=S.session.workspace?(targetDir==='.'?`${S.session.workspace}/${name.trim()}`:`${S.session.workspace}/${targetDir}/${name.trim()}`):null;
    if(absPath){
      const addAsSpace=await showConfirmDialog({
        title:t('folder_add_as_space_title'),
        message:t('folder_add_as_space_msg'),
        confirmLabel:t('folder_add_as_space_btn'),
        cancelLabel:t('status_no'),
        focusCancel:true
      });
      if(addAsSpace){
        try{
          const data=await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path:absPath})});
          if(typeof _workspaceList!=='undefined')_workspaceList=data.workspaces||_workspaceList||[];
          if(typeof renderWorkspacesPanel==='function')renderWorkspacesPanel(_workspaceList);
          showToast(t('workspace_added'));
        }catch(e2){setStatus((t('error_prefix')||'Error: ')+e2.message);}
      }
    }
  }catch(e){setStatus(t('folder_create_failed')+e.message);}
}

export {
  _workspaceContextMenuItem,
  _copyTextWithFallback,
  _workspaceCreateTargetLabel,
  _workspaceJoinTargetPath,
  _showWorkspaceRootContextMenu,
  deleteWorkspaceDir,
  _showFileContextMenu,
  _inlineRenameFileItem,
  deleteWorkspaceFile,
  promptNewFile,
  promptNewFolder,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _workspaceContextMenuItem: { enumerable: true, get: () => _workspaceContextMenuItem, set: (value) => { _workspaceContextMenuItem = value; } },
  _copyTextWithFallback: { enumerable: true, get: () => _copyTextWithFallback, set: (value) => { _copyTextWithFallback = value; } },
  _workspaceCreateTargetLabel: { enumerable: true, get: () => _workspaceCreateTargetLabel, set: (value) => { _workspaceCreateTargetLabel = value; } },
  _workspaceJoinTargetPath: { enumerable: true, get: () => _workspaceJoinTargetPath, set: (value) => { _workspaceJoinTargetPath = value; } },
  _showWorkspaceRootContextMenu: { enumerable: true, get: () => _showWorkspaceRootContextMenu, set: (value) => { _showWorkspaceRootContextMenu = value; } },
  deleteWorkspaceDir: { enumerable: true, get: () => deleteWorkspaceDir, set: (value) => { deleteWorkspaceDir = value; } },
  _showFileContextMenu: { enumerable: true, get: () => _showFileContextMenu, set: (value) => { _showFileContextMenu = value; } },
  _inlineRenameFileItem: { enumerable: true, get: () => _inlineRenameFileItem, set: (value) => { _inlineRenameFileItem = value; } },
  deleteWorkspaceFile: { enumerable: true, get: () => deleteWorkspaceFile, set: (value) => { deleteWorkspaceFile = value; } },
  promptNewFile: { enumerable: true, get: () => promptNewFile, set: (value) => { promptNewFile = value; } },
  promptNewFolder: { enumerable: true, get: () => promptNewFolder, set: (value) => { promptNewFolder = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
