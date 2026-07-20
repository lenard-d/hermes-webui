import { showToast } from './toast-notifications.js';
import { S } from './state.js';

let _wsActiveDragPath=null;
let _wsActiveDragType=null;

function _setWsDragData(e,item){
  e.dataTransfer.setData('application/ws-path',item.path);
  e.dataTransfer.setData('application/ws-type',item.type);
  e.dataTransfer.setData('text/plain',item.path);
  _wsActiveDragPath=item.path;
  _wsActiveDragType=item.type;
}

function _clearWsDragData(){
  _wsActiveDragPath=null;
  _wsActiveDragType=null;
}

// Window-level fallback cleanup: if a workspace drag is abandoned without the
// row's ondragend firing (drag cancelled, dropped outside any target, tab
// blurred/hidden mid-drag), the active-drag flag must not survive — otherwise a
// later FOREIGN text/plain drag could be misread as a workspace move.
if(typeof window!=='undefined'&&!window._wsDragCleanupBound){
  window._wsDragCleanupBound=true;
  window.addEventListener('dragend',_clearWsDragData,true);
  // Defer the drop cleanup a tick: this capture-phase window listener fires
  // BEFORE the target element's ondrop, so clearing synchronously here would
  // wipe _wsActiveDragPath before _isWorkspaceTreeMoveDrag()/_wsDragSrcPath()
  // run in the target handler — re-breaking the macOS stripped-MIME move.
  window.addEventListener('drop',()=>setTimeout(_clearWsDragData,0),true);
  window.addEventListener('pagehide',_clearWsDragData);
  window.addEventListener('blur',_clearWsDragData);
}

function _isWorkspaceTreeMoveDrag(e){
  if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('Files')) return false;
  if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('application/ws-path')) return true;
  // Stripped-MIME (macOS WebKit) fallback: accept text/plain ONLY while a
  // workspace drag is genuinely in flight. The drop handler additionally
  // proves text/plain === _wsActiveDragPath before performing the move.
  return !!(_wsActiveDragPath&&e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('text/plain'));
}

function _wsDragSrcPath(e){
  const custom=e.dataTransfer.getData('application/ws-path');
  if(custom) return custom;
  const plain=e.dataTransfer.getData('text/plain')||'';
  if(_wsActiveDragPath&&plain===_wsActiveDragPath) return _wsActiveDragPath;
  return '';
}

function _wsDragSrcType(e){
  const custom=e.dataTransfer.getData('application/ws-type');
  if(custom) return custom;
  return _wsActiveDragType||'file';
}

function _workspaceParentDir(relPath){
  if(!relPath||relPath==='.')return '.';
  const idx=relPath.lastIndexOf('/');
  return idx===-1?'.':relPath.substring(0,idx);
}

function _clearWorkspaceMoveDragOver(){
  document.querySelectorAll('.file-item.drag-over,.breadcrumb-seg.drag-over').forEach(el=>el.classList.remove('drag-over'));
}

function _remapWorkspaceCachesAfterMove(oldPath,newPath,isDir){
  if(isDir&&S._expandedDirs){
    if(S._expandedDirs.has(oldPath)){
      S._expandedDirs.delete(oldPath);
      S._expandedDirs.add(newPath);
    }
    for(const expandedPath of [...S._expandedDirs]){
      if(expandedPath.startsWith(oldPath+'/')){
        S._expandedDirs.delete(expandedPath);
        S._expandedDirs.add(newPath+expandedPath.slice(oldPath.length));
      }
    }
    if(S._dirCache[oldPath]){
      S._dirCache[newPath]=S._dirCache[oldPath];
      delete S._dirCache[oldPath];
    }
    for(const cachePath of Object.keys(S._dirCache)){
      if(cachePath.startsWith(oldPath+'/')){
        const remapped=newPath+cachePath.slice(oldPath.length);
        S._dirCache[remapped]=S._dirCache[cachePath];
        delete S._dirCache[cachePath];
      }
    }
    if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
  }
  delete S._dirCache[_workspaceParentDir(oldPath)];
  delete S._dirCache[_workspaceParentDir(newPath)];
  if(typeof _previewCurrentPath!=='undefined'&&_previewCurrentPath){
    if(_previewCurrentPath===oldPath)_previewCurrentPath=newPath;
    else if(_previewCurrentPath.startsWith(oldPath+'/'))_previewCurrentPath=newPath+_previewCurrentPath.slice(oldPath.length);
  }
}

async function _performWorkspaceMove(srcPath,destDir,isDir){
  if(!S.session||!srcPath)return;
  const normDest=destDir||'.';
  if(srcPath===normDest)return;
  if(normDest.startsWith(srcPath+'/'))return;
  if(_workspaceParentDir(srcPath)===normDest)return;
  try{
    const data=await api('/api/file/move',{method:'POST',body:JSON.stringify({
      session_id:S.session.session_id,path:srcPath,dest_dir:normDest
    })});
    const movedName=data.new_path.includes('/')?data.new_path.slice(data.new_path.lastIndexOf('/')+1):data.new_path;
    showToast((t('moved_to')||'Moved to ')+movedName);
    _remapWorkspaceCachesAfterMove(data.old_path||srcPath,data.new_path||srcPath,isDir);
    await loadDir(S.currentDir);
    if(typeof refreshOpenPreviewIfMutated==='function')await refreshOpenPreviewIfMutated();
  }catch(err){
    showToast((t('move_failed')||'Move failed: ')+err.message,5000,'error');
  }
}

function _bindWorkspaceMoveDropTarget(el,destDir){
  el.ondragenter=(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    el.classList.add('drag-over');
  };
  el.ondragover=(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    e.dataTransfer.dropEffect='move';
    el.classList.add('drag-over');
  };
  el.ondragleave=(e)=>{
    if(el.contains(e.relatedTarget))return;
    el.classList.remove('drag-over');
  };
  el.ondrop=async(e)=>{
    if(!_isWorkspaceTreeMoveDrag(e))return;
    e.preventDefault();e.stopPropagation();
    el.classList.remove('drag-over');
    try{
      const srcPath=_wsDragSrcPath(e);
      if(!srcPath)return;
      const srcType=_wsDragSrcType(e);
      await _performWorkspaceMove(srcPath,destDir,srcType==='dir');
    }finally{
      _clearWsDragData();
    }
  };
}

export {
  _setWsDragData,
  _clearWsDragData,
  _isWorkspaceTreeMoveDrag,
  _wsDragSrcPath,
  _wsDragSrcType,
  _workspaceParentDir,
  _clearWorkspaceMoveDragOver,
  _remapWorkspaceCachesAfterMove,
  _performWorkspaceMove,
  _bindWorkspaceMoveDropTarget,
  _wsActiveDragPath,
  _wsActiveDragType,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _setWsDragData: { enumerable: true, get: () => _setWsDragData, set: (value) => { _setWsDragData = value; } },
  _clearWsDragData: { enumerable: true, get: () => _clearWsDragData, set: (value) => { _clearWsDragData = value; } },
  _isWorkspaceTreeMoveDrag: { enumerable: true, get: () => _isWorkspaceTreeMoveDrag, set: (value) => { _isWorkspaceTreeMoveDrag = value; } },
  _wsDragSrcPath: { enumerable: true, get: () => _wsDragSrcPath, set: (value) => { _wsDragSrcPath = value; } },
  _wsDragSrcType: { enumerable: true, get: () => _wsDragSrcType, set: (value) => { _wsDragSrcType = value; } },
  _workspaceParentDir: { enumerable: true, get: () => _workspaceParentDir, set: (value) => { _workspaceParentDir = value; } },
  _clearWorkspaceMoveDragOver: { enumerable: true, get: () => _clearWorkspaceMoveDragOver, set: (value) => { _clearWorkspaceMoveDragOver = value; } },
  _remapWorkspaceCachesAfterMove: { enumerable: true, get: () => _remapWorkspaceCachesAfterMove, set: (value) => { _remapWorkspaceCachesAfterMove = value; } },
  _performWorkspaceMove: { enumerable: true, get: () => _performWorkspaceMove, set: (value) => { _performWorkspaceMove = value; } },
  _bindWorkspaceMoveDropTarget: { enumerable: true, get: () => _bindWorkspaceMoveDropTarget, set: (value) => { _bindWorkspaceMoveDropTarget = value; } },
  _wsActiveDragPath: { enumerable: true, get: () => _wsActiveDragPath, set: (value) => { _wsActiveDragPath = value; } },
  _wsActiveDragType: { enumerable: true, get: () => _wsActiveDragType, set: (value) => { _wsActiveDragType = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
