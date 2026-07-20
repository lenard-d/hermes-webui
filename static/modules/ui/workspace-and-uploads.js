import { setStatus, showToast, updateSendBtn } from './composer.js';
import { showConfirmDialog, showPromptDialog } from './dialogs-and-reconnect.js';
import { _ARCHIVE_EXTS, _IMAGE_EXTS, _SVG_EXTS, _mediaKindForName } from './media-and-quota.js';
import { syncTopbar } from './presentation.js';
import { renderMessages } from './renderer.js';
import { $, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, S, _redirectIfUnauth, esc } from './state.js';

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
  // Root segment
  const root=document.createElement('span');
  root.className='breadcrumb-seg breadcrumb-link';
  root.textContent='~';
  root.onclick=()=>loadDir('.');
  _bindWorkspaceMoveDropTarget(root,'.');
  _bindWorkspaceOsUploadDropTarget(root,'.');
  bar.appendChild(root);
  // Path segments
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

const WORKSPACE_HIDDEN_FILE_NAMES=new Set([
  '.DS_Store','._.DS_Store','.AppleDouble','.Spotlight-V100','.Trashes','.fseventsd',
  'Thumbs.db','Desktop.ini','ehthumbs.db','$RECYCLE.BIN',
  '.directory','.git','.svn','.hg','node_modules','__pycache__',
  '.pytest_cache','.mypy_cache','.ruff_cache','.tox','.venv','venv'
]);
const WORKSPACE_HIDDEN_FILE_PREFIXES=['._','.Trash-'];
function _workspaceShouldHideEntry(item){
  if(!item||S.showHiddenWorkspaceFiles)return false;
  const name=String(item.name||'');
  if(!name)return false;
  if(WORKSPACE_HIDDEN_FILE_NAMES.has(name))return true;
  return WORKSPACE_HIDDEN_FILE_PREFIXES.some(prefix=>name.startsWith(prefix));
}
function _visibleWorkspaceEntries(entries){
  const list=Array.isArray(entries)?entries:[];
  return S.showHiddenWorkspaceFiles?list:list.filter(item=>!_workspaceShouldHideEntry(item));
}
function _syncWorkspaceHiddenToggle(){
  const el=$('workspaceShowHiddenFiles');
  if(el)el.checked=!!S.showHiddenWorkspaceFiles;
  // Reflect "hidden files are visible" state on the panel heading + kebab dot,
  // so users can see they've flipped a non-default workspace pref without
  // having to open the menu. The menu itself stays out of the way otherwise.
  const ind=$('workspaceHiddenIndicator');
  if(ind){
    if(S.showHiddenWorkspaceFiles){ ind.hidden=false; ind.removeAttribute('hidden'); }
    else { ind.hidden=true; ind.setAttribute('hidden',''); }
  }
  const dot=$('workspacePrefsDot');
  if(dot){
    if(S.showHiddenWorkspaceFiles){ dot.hidden=false; dot.removeAttribute('hidden'); }
    else { dot.hidden=true; dot.setAttribute('hidden',''); }
  }
}
function toggleWorkspaceHiddenFiles(value){
  S.showHiddenWorkspaceFiles=!!value;
  try{localStorage.setItem('hermes-workspace-show-hidden-files',S.showHiddenWorkspaceFiles?'1':'0');}catch(_){}
  _syncWorkspaceHiddenToggle();
  renderFileTree();
}
try{S.showHiddenWorkspaceFiles=localStorage.getItem('hermes-workspace-show-hidden-files')==='1';}catch(_){}

// ── Workspace preferences kebab menu (#1793 UX refinement) ───────────────
// The "Show hidden files" toggle used to live as a permanent inline row
// below the breadcrumb bar. That ate ~32px of vertical space on every
// panel view (root, subdir, file preview), even though the toggle is a
// set-once preference — most users flip it once or never. Moving the
// control into a kebab dropdown reclaims the space; the small "(hidden
// files visible)" indicator on the heading reflects the non-default state
// so the affordance isn't lost.
let _workspacePrefsMenu = null;
let _workspacePrefsAnchor = null;
function _closeWorkspacePrefsMenu(){
  if(_workspacePrefsMenu){ _workspacePrefsMenu.remove(); _workspacePrefsMenu=null; }
  if(_workspacePrefsAnchor){
    _workspacePrefsAnchor.classList.remove('active');
    _workspacePrefsAnchor.setAttribute('aria-expanded','false');
    _workspacePrefsAnchor=null;
  }
}
function _positionWorkspacePrefsMenu(anchorEl){
  if(!_workspacePrefsMenu||!anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(260, Math.max(220, _workspacePrefsMenu.scrollWidth||220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  let top=rect.bottom+6;
  const menuH=_workspacePrefsMenu.offsetHeight||0;
  if(top+menuH>window.innerHeight-8 && rect.top>menuH+12) top=rect.top-menuH-6;
  if(top<8) top=8;
  _workspacePrefsMenu.style.left=left+'px';
  _workspacePrefsMenu.style.top=top+'px';
}
function _buildWorkspacePrefsMenu(){
  const menu=document.createElement('div');
  menu.className='workspace-prefs-menu open';
  menu.setAttribute('role','menu');
  // The checkbox keeps id="workspaceShowHiddenFiles" so existing call
  // sites (and the existing test_issue1793_file_tree_cruft_filter test)
  // can find it the same way as before. Only the parent container moves.
  const labelTxt = (typeof t==='function' ? t('workspace_show_hidden_files') : 'Show hidden files');
  const descTxt  = (typeof t==='function' ? t('workspace_show_hidden_files_desc') : 'Include .DS_Store, .git, node_modules, and other hidden / system files in the file tree.');
  const row=document.createElement('label');
  row.className='workspace-prefs-item';
  row.setAttribute('role','menuitemcheckbox');
  row.innerHTML=
    '<input type="checkbox" id="workspaceShowHiddenFiles" '+
    'onchange="toggleWorkspaceHiddenFiles(this.checked)">'+
    '<span class="workspace-prefs-copy">'+
      '<span class="workspace-prefs-name">'+esc(labelTxt)+'</span>'+
      '<span class="workspace-prefs-meta">'+esc(descTxt)+'</span>'+
    '</span>';
  const cb=row.querySelector('input');
  if(cb) cb.checked=!!S.showHiddenWorkspaceFiles;
  menu.appendChild(row);
  return menu;
}
function toggleWorkspacePrefsMenu(e){
  if(e&&e.preventDefault) e.preventDefault();
  if(e&&e.stopPropagation) e.stopPropagation();
  // Anchor preference: the kebab button. The indicator chip can also open
  // the same menu (click on "(hidden visible)"), but anchor positioning
  // always references the kebab so the menu lands in the same place.
  const anchor=$('btnWorkspacePrefs')||(e&&e.currentTarget)||null;
  if(_workspacePrefsMenu&&_workspacePrefsAnchor===anchor){ _closeWorkspacePrefsMenu(); return; }
  _closeWorkspacePrefsMenu();
  const menu=_buildWorkspacePrefsMenu();
  document.body.appendChild(menu);
  _workspacePrefsMenu=menu;
  _workspacePrefsAnchor=anchor;
  if(anchor){ anchor.classList.add('active'); anchor.setAttribute('aria-expanded','true'); }
  _positionWorkspacePrefsMenu(anchor);
}
document.addEventListener('click',e=>{
  if(!_workspacePrefsMenu) return;
  if(_workspacePrefsMenu.contains(e.target)) return;
  if(_workspacePrefsAnchor&&_workspacePrefsAnchor.contains(e.target)) return;
  // Indicator chip is also an opener — clicking it should toggle, not close.
  const ind=$('workspaceHiddenIndicator');
  if(ind&&ind.contains(e.target)) return;
  _closeWorkspacePrefsMenu();
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&_workspacePrefsMenu) _closeWorkspacePrefsMenu();
});
window.addEventListener('resize',()=>{
  if(_workspacePrefsMenu&&_workspacePrefsAnchor) _positionWorkspacePrefsMenu(_workspacePrefsAnchor);
});

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_syncWorkspaceHiddenToggle);
else _syncWorkspaceHiddenToggle();

function bindWorkspaceHeadingActions(){
  const heading=$('workspacePanelHeading');
  if(!heading||heading.dataset.bound==='1')return;
  heading.dataset.bound='1';
  const goRoot=()=>{
    if(S.session&&S.session.workspace) loadDir('.');
  };
  heading.onclick=goRoot;
  heading.onkeydown=(e)=>{
    if(!(S.session&&S.session.workspace)) return;
    if(e.key==='Enter'||e.key===' '){
      e.preventDefault();
      goRoot();
    }
  };
  heading.oncontextmenu=(e)=>{
    if(!(S.session&&S.session.workspace)) return;
    e.preventDefault();
    e.stopPropagation();
    _showWorkspaceRootContextMenu(e);
  };
  _syncWorkspaceHeadingState();
}

function _syncWorkspaceHeadingState(){
  const heading=$('workspacePanelHeading');
  if(!heading) return;
  const enabled=!!(S.session&&S.session.workspace);
  heading.classList.toggle('workspace-panel-heading--enabled',enabled);
  if(enabled){
    heading.setAttribute('role','button');
    heading.setAttribute('tabindex','0');
    heading.setAttribute('aria-disabled','false');
    heading.title='Workspace root';
  } else {
    heading.removeAttribute('role');
    heading.removeAttribute('tabindex');
    heading.setAttribute('aria-disabled','true');
    heading.title=t('no_workspace');
  }
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',bindWorkspaceHeadingActions);
else bindWorkspaceHeadingActions();

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

function _ensureWorkspaceTreeState(){
  // These collections belong to the shared session state, but must be created
  // lazily: native-module cycles are linked before state.js initializes S.
  if(!S._expandedDirs) S._expandedDirs=new Set();
  if(!S._dirCache) S._dirCache={};
}

function renderFileTree(){
  _ensureWorkspaceTreeState();
  const box=$('fileTree');
  // #5657: capture the scroll position before wiping the container. box.innerHTML=''
  // detaches every row, collapsing scrollHeight so the browser clamps scrollTop to 0;
  // without this, every expand/collapse, breadcrumb nav, refresh, and hidden-files
  // toggle that re-runs renderFileTree() teleports the reader back to the top of a
  // long tree. Restored only after the normal render tail below — the two early-return
  // paths (no-workspace hides the box; empty-dir has nothing to scroll) legitimately
  // reset. A plain scrollTop restore suffices here: expand/collapse insert/remove rows
  // BELOW the clicked disclosure, so the clicked row keeps its offset from the top (no
  // getBoundingClientRect anchor delta needed — that's only for prepend-above cases).
  const prevScrollTop=box?box.scrollTop:0;
  box.innerHTML='';
  // Cache current dir entries
  S._dirCache[S.currentDir||'.']=S.entries;
  // Show empty-state when no workspace is set or the directory is empty (#703)
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
  // #5657: restore the pre-wipe scroll position now that the tree is tall again.
  if(box) box.scrollTop=prevScrollTop;
}

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
  // run in the target handler — re-breaking the macOS stripped-MIME move. The
  // setTimeout lets the real drop handler complete, then clears the lingering flag.
  window.addEventListener('drop',()=>setTimeout(_clearWsDragData,0),true);
  window.addEventListener('pagehide',_clearWsDragData);
  window.addEventListener('blur',_clearWsDragData);
}
function _isWorkspaceTreeMoveDrag(e){
  if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('Files')) return false;
  if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('application/ws-path')) return true;
  // Stripped-MIME (macOS WebKit) fallback: accept text/plain ONLY while a
  // workspace drag is genuinely in flight. dragover/drop events can't read the
  // payload, so gate on the active flag alone here; the drop handler additionally
  // proves text/plain === _wsActiveDragPath before performing the move.
  return !!(_wsActiveDragPath&&e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.includes('text/plain'));
}
function _wsDragSrcPath(e){
  const custom=e.dataTransfer.getData('application/ws-path');
  if(custom) return custom;
  // Stripped-MIME fallback: only trust the active flag when the drop's own
  // text/plain matches it. A foreign text/plain drag (different/empty content)
  // must NOT resolve to our tracked workspace path even if the flag lingered.
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
    // External symlinks are display-only: not expandable, not openable.
    // The read gate (safe_resolve_ws) still blocks navigation through them.
    const isDirLike = !isExternalLink && (item.type === 'dir' || (isLk && item.is_dir));
    const isFileLike = !isExternalLink && !isDirLike;
    el.dataset.wsIsDir = String(isDirLike);
    if(isExternalLink || isReadOnlyEscape){el.removeAttribute('draggable');el.ondragstart=null;}

    if(isDirLike){
      // Toggle arrow for directories
      const arrow=document.createElement('span');
      arrow.className='file-tree-toggle';
      const isExpanded=S._expandedDirs.has(item.path);
      arrow.textContent=isExpanded?'\u25BE':'\u25B8';
      el.appendChild(arrow);
    }else{
      // Keep file icons aligned with sibling directories that occupy this
      // slot with the expand/collapse toggle. #2554
      const spacer=document.createElement('span');
      spacer.className='file-tree-toggle-placeholder';
      spacer.setAttribute('aria-hidden','true');
      el.appendChild(spacer);
    }

    // Icon
    const iconEl=document.createElement('span');
    iconEl.className='file-icon';
    iconEl.innerHTML = isExternalLink
      ? li('external-link', 14)
      : isDirLike
        ? (isLk ? li('link', 14) : li('folder', 14))
        : (isLk ? li('link', 14) : fileIcon(item.name, item.type));
    el.appendChild(iconEl);

    // Name
    const nameEl=document.createElement('span');
    nameEl.className='file-name';nameEl.textContent=item.name;
    // Tooltip only on FILES — dblclick renames them. On directories, dblclick
    // navigates into the folder; rename lives in the right-click context menu
    // (the "Double-click to rename" hint here would be misleading). #1710.
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
    // Single-click opens (file) or expand-toggles (dir) but is debounced 300ms so a
    // double-click can cancel it and trigger rename instead. Without the debounce, the
    // click bubbles to el.onclick before dblclick can fire — that's #1698. Without the
    // restored activation, single-click on the filename does nothing — that's #1707.
    let _nameClickTimer=null;
    nameEl.onclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      _nameClickTimer=setTimeout(()=>{
        _nameClickTimer=null;
        // Delegate to the row's existing single-click handler (openFile / dir toggle).
        if(typeof el.onclick==='function')el.onclick(e);
      },300);
    };
    nameEl.ondblclick=(e)=>{
      e.stopPropagation();
      if(_nameClickTimer){clearTimeout(_nameClickTimer);_nameClickTimer=null;}
      // For directories, double-click navigates (breadcrumb view)
      if(isDirLike){loadDir(item.path);return;}
      // Escape-root rows remain browse-only, nested escape rows stay display-only.
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
              // Update expanded dirs cache key if renaming a directory
              if(isDirLike&&S._expandedDirs){
                S._expandedDirs.delete(item.path);
                const parent=item.path.includes('/')?item.path.substring(0,item.path.lastIndexOf('/')):'.';
                const newPath=parent==='.'?newName:parent+'/'+newName;
                S._expandedDirs.add(newPath);
                if(S._dirCache[item.path]){S._dirCache[newPath]=S._dirCache[item.path];delete S._dirCache[item.path];}
                if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
              }
              // Invalidate cache and re-render
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

    // Size -- for real files and symlinks that resolve to files
    if(isFileLike&&item.size){
      const sizeEl=document.createElement('span');
      sizeEl.className='file-size';
      sizeEl.textContent=`${(item.size/1024).toFixed(1)}k`;
      el.appendChild(sizeEl);
    }

    // Delete button -- for file-like rows and directory-like rows
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
      // Single-click toggles expand/collapse
      el.onclick=async(e)=>{
        e.stopPropagation();
        if(S._expandedDirs.has(item.path)){
          S._expandedDirs.delete(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          renderFileTree();
        }else{
          S._expandedDirs.add(item.path);
          if(typeof _saveExpandedDirs==='function')_saveExpandedDirs();
          // Fetch children if not cached
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
      // Display-only: the link points outside the workspace. We do NOT disclose
      // the resolved outside path (#4581 hardening) and do NOT recursively
      // authorize nested escape rows under an already-authorized external root.
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

    // Render children if directory is expanded
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
    // Remove from expanded dirs cache
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
  // Keep menu within viewport
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

    // Rename
    const renameItem=document.createElement('div');
    renameItem.textContent=t('rename_title');
    renameItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    renameItem.onmouseenter=()=>renameItem.style.background='var(--hover-bg)';
    renameItem.onmouseleave=()=>renameItem.style.background='';
    renameItem.onclick=()=>{menu.remove();_inlineRenameFileItem(item);};
    menu.appendChild(renameItem);

    // Reveal in File Manager
    const revealItem=document.createElement('div');
    revealItem.textContent=t('reveal_in_finder');
    revealItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    revealItem.onmouseenter=()=>revealItem.style.background='var(--hover-bg)';
    revealItem.onmouseleave=()=>revealItem.style.background='';
    revealItem.onclick=async()=>{menu.remove();try{await api('/api/file/reveal',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('reveal_failed')+(err.message||err));}};
    menu.appendChild(revealItem);

    // Open in VS Code (#2735)
    const vscodeItem=document.createElement('div');
    vscodeItem.textContent=t('open_in_vscode');
    vscodeItem.style.cssText='padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text);';
    vscodeItem.onmouseenter=()=>vscodeItem.style.background='var(--hover-bg)';
    vscodeItem.onmouseleave=()=>vscodeItem.style.background='';
    vscodeItem.onclick=async()=>{menu.remove();try{await api('/api/file/open-vscode',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:item.path})});}catch(err){showToast(t('open_in_vscode_failed')+(err.message||err));}};
    menu.appendChild(vscodeItem);

    // Copy file path — resolves the absolute on-disk path on the server (so the
    // user gets the full /home/.../workspace/foo.py rather than the relative
    // path the file tree shows) and writes it to the OS clipboard. Useful for
    // pasting into terminals, editors, or other apps without taking the slower
    // Reveal-in-Finder round trip.
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
  // Pre-fill the input with the current name and select just the stem
  // (everything before the last '.') so the user can immediately retype the
  // basename while preserving the extension — matches macOS Finder. For
  // directories or names with no '.', the helper selects the full value.
  // `selectStem` also handles dotfiles ('.gitignore') by full-selecting.
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
    // Update expanded dirs cache key if renaming a directory
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
  const _delFile=await showConfirmDialog({title:t('delete_confirm',name),message:'',confirmLabel:'Delete',danger:true,focusCancel:true});
  if(!_delFile) return;
  try{
    await api('/api/file/delete',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,path:relPath})});
    showToast(t('deleted')+name);
    // Close preview if we just deleted the viewed file
    if($('previewPathText').textContent===relPath)$('btnClearPreview').onclick();
    await loadDir(S.currentDir);
  }catch(e){setStatus(t('delete_failed')+e.message);}
}

async function promptNewFile(targetDir = S.currentDir || '.'){
  if(!S.session){
    const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws) return;
    try{
      // System-minted session (#6022): explicit worktree:false — creating a
      // file from a blank page must not inherit the config worktree default.
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws,worktree:false})});
      if(r&&r.session){S._pendingSessionToolsets=null;S.session=r.session;S.messages=[];syncTopbar();renderMessages();await renderSessionList();}
    }catch(e){setStatus(t('create_failed')+e.message);return;}
  }
  if(!S.session)return;
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
  if(!S.session){
    const ws=(typeof S._profileDefaultWorkspace==='string'&&S._profileDefaultWorkspace)||'';
    if(!ws) return;
    try{
      // System-minted session (#6022): explicit worktree:false — creating a
      // folder from a blank page must not inherit the config worktree default.
      const r=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace:ws,worktree:false})});
      if(r&&r.session){S._pendingSessionToolsets=null;S.session=r.session;S.messages=[];syncTopbar();renderMessages();await renderSessionList();}
    }catch(e){setStatus(t('folder_create_failed')+e.message);return;}
  }
  if(!S.session)return;
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

function renderTray(){ // non-media files use paperclip chip
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){tray.classList.remove('has-files');updateSendBtn();return;}
  tray.classList.add('has-files');
  updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    const mediaKind=_mediaKindForName(f.name);
    if(_IMAGE_EXTS.test(f.name)||mediaKind==='audio'||mediaKind==='video'){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--media attach-chip--'+mediaKind; // attach-chip--audio attach-chip--video
      chip.dataset.blobUrl=blobUrl;
      if(mediaKind==='image'){
        chip.innerHTML=`<img class="attach-thumb" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(_SVG_EXTS.test(f.name)){
        chip.innerHTML=`<img class="attach-thumb attach-thumb--svg" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='audio'){
        chip.innerHTML=`<span class="attach-chip-media">🎵 ${esc(f.name)}</span><audio controls preload="metadata" src="${esc(blobUrl)}"></audio><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='video'){
        chip.innerHTML=`<span class="attach-chip-media">🎬 ${esc(f.name)}</span><video controls preload="metadata" src="${esc(blobUrl)}"></video><button title="${t('remove_title')}">${li('x',12)}</button>`;
      }
    } else {
      chip.innerHTML=`${li('paperclip',12)} ${esc(f.name)} <button title="${t('remove_title')}">${li('x',12)}</button>`;
    }
    chip.querySelector('button').onclick=()=>{
      // Revoke blob URL to avoid memory leak before removing
      if(chip.dataset.blobUrl) URL.revokeObjectURL(chip.dataset.blobUrl);
      S.pendingFiles.splice(i,1);renderTray();
    };
    tray.appendChild(chip);
  });
}
function _uploadTooLargeMessage(file){
  const fileSizeMb=Math.ceil(((file&&file.size)||0)/1024/1024);
  return t('upload_too_large',MAX_UPLOAD_MB,fileSizeMb);
}
function _showUploadTooLarge(file){
  const message=`${t('upload_failed')}${file&&file.name?file.name:'file'} \u2014 ${_uploadTooLargeMessage(file)}`;
  if(typeof setStatus==='function')setStatus(`\u274c ${message}`);
  else if(typeof showToast==='function')showToast(message,5000,'error');
}
function addFiles(files){
  for(const f of files){
    if(f&&f.size>MAX_UPLOAD_BYTES){_showUploadTooLarge(f);continue;}
    if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);
  }
  renderTray();
}
const _uploadPendingFilesProgressBySession=new Map();
function _uploadPendingFilesCurrentSession(sessionId){
  return !!(!sessionId||(S.session&&S.session.session_id===sessionId));
}
function _uploadPendingFilesHideProgressBar(){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  barWrap.classList.remove('active');
  bar.style.width='0%';
  if(barWrap.dataset)delete barWrap.dataset.uploadSessionId;
}
function _uploadPendingFilesShowProgressBar(owner,percent){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  if(barWrap.dataset)barWrap.dataset.uploadSessionId=owner;
  barWrap.classList.add('active');
  bar.style.width=`${Math.max(0,Math.min(100,Number(percent)||0))}%`;
}
function _uploadPendingFilesSyncProgressForSession(sessionId){
  const owner=String(sessionId||'');
  const state=owner?_uploadPendingFilesProgressBySession.get(owner):null;
  if(state){_uploadPendingFilesShowProgressBar(owner,state.percent);return;}
  _uploadPendingFilesHideProgressBar();
}
function _uploadPendingFilesUpdateProgress(sessionId,percent){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  const owner=String(sessionId||'');
  const activeForOwner=barWrap.dataset&&barWrap.dataset.uploadSessionId===owner;
  if(percent===null){
    if(owner)_uploadPendingFilesProgressBySession.delete(owner);
    if(activeForOwner){
      _uploadPendingFilesHideProgressBar();
    }
    return;
  }
  const clamped=Math.max(0,Math.min(100,Number(percent)||0));
  if(owner)_uploadPendingFilesProgressBySession.set(owner,{percent:clamped});
  if(!_uploadPendingFilesCurrentSession(sessionId)){
    if(activeForOwner)_uploadPendingFilesHideProgressBar();
    return;
  }
  _uploadPendingFilesShowProgressBar(owner,clamped);
}
async function uploadPendingFiles(options={}){
  const opts=options||{};
  const pendingFiles=Array.isArray(opts.files)?opts.files.filter(Boolean):[...(S.pendingFiles||[])];
  const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');
  if(!pendingFiles.length||!sessionId)return[];
  const clearPending=!(opts&&opts.clearPending===false);
  const names=[];let failures=0;
  _uploadPendingFilesUpdateProgress(sessionId,0);
  const total=pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=pendingFiles[i];
    try{
      if(f&&f.size>MAX_UPLOAD_BYTES)throw new Error(_uploadTooLargeMessage(f));
      const fd=new FormData();
      fd.append('session_id',sessionId);fd.append('file',f,f.name);
      const isArchive=_ARCHIVE_EXTS.test(f.name);
      const url=new URL(isArchive?'api/upload/extract':'api/upload',document.baseURI||location.href).href;
      const res=await fetch(url,{method:'POST',credentials:'include',body:fd});
      if(_redirectIfUnauth(res)) return;
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      if(isArchive){
        names.push({name: data.dest, path: data.dest, extracted: data.extracted});
        if(typeof loadDir==='function'&&_uploadPendingFilesCurrentSession(sessionId))loadDir(S.currentDir||'.');
      }else{
        names.push({name: data.filename, path: data.path, mime: data.mime, size: data.size, is_image: !!data.is_image});
      }
    }catch(e){failures++;setStatus(`\u274c ${t('upload_failed')}${f.name} \u2014 ${e.message}`);}
    _uploadPendingFilesUpdateProgress(sessionId,Math.round((i+1)/total*100));
  }
  _uploadPendingFilesUpdateProgress(sessionId,null);
  if(clearPending&&_uploadPendingFilesCurrentSession(sessionId)){S.pendingFiles=[];renderTray();}
  else if(typeof renderTray==='function'&&_uploadPendingFilesCurrentSession(sessionId))renderTray();
  if(failures===total&&total>0)throw new Error(t('all_uploads_failed',total));
  // Show extraction summary
  const extracted=names.filter(n=>n.extracted);
  if(extracted.length)showToast(t('archive_extracted',extracted.reduce((s,n)=>s+n.extracted,0),extracted.length));
  return names;
}


export {
  fileIcon,
  renderBreadcrumb,
  _workspaceShouldHideEntry,
  _visibleWorkspaceEntries,
  _syncWorkspaceHiddenToggle,
  toggleWorkspaceHiddenFiles,
  _closeWorkspacePrefsMenu,
  _positionWorkspacePrefsMenu,
  _buildWorkspacePrefsMenu,
  toggleWorkspacePrefsMenu,
  bindWorkspaceHeadingActions,
  _syncWorkspaceHeadingState,
  _workspaceContextMenuItem,
  _copyTextWithFallback,
  _workspaceCreateTargetLabel,
  _workspaceJoinTargetPath,
  _showWorkspaceRootContextMenu,
  renderFileTree,
  _setWsDragData,
  _clearWsDragData,
  _isWorkspaceTreeMoveDrag,
  _wsDragSrcPath,
  _wsDragSrcType,
  _workspaceParentDir,
  _clearWorkspaceMoveDragOver,
  _remapWorkspaceCachesAfterMove,
  _bindWorkspaceMoveDropTarget,
  elideMiddle,
  _renderTreeItems,
  _showFileContextMenu,
  renderTray,
  _uploadTooLargeMessage,
  _showUploadTooLarge,
  addFiles,
  _uploadPendingFilesCurrentSession,
  _uploadPendingFilesHideProgressBar,
  _uploadPendingFilesShowProgressBar,
  _uploadPendingFilesSyncProgressForSession,
  _uploadPendingFilesUpdateProgress,
  _performWorkspaceMove,
  deleteWorkspaceDir,
  _inlineRenameFileItem,
  deleteWorkspaceFile,
  promptNewFile,
  promptNewFolder,
  uploadPendingFiles,
  WORKSPACE_HIDDEN_FILE_NAMES,
  WORKSPACE_HIDDEN_FILE_PREFIXES,
  _uploadPendingFilesProgressBySession,
  _workspacePrefsMenu,
  _workspacePrefsAnchor,
  _wsActiveDragPath,
  _wsActiveDragType,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  fileIcon: { enumerable: true, get: () => fileIcon, set: (value) => { fileIcon = value; } },
  renderBreadcrumb: { enumerable: true, get: () => renderBreadcrumb, set: (value) => { renderBreadcrumb = value; } },
  _workspaceShouldHideEntry: { enumerable: true, get: () => _workspaceShouldHideEntry, set: (value) => { _workspaceShouldHideEntry = value; } },
  _visibleWorkspaceEntries: { enumerable: true, get: () => _visibleWorkspaceEntries, set: (value) => { _visibleWorkspaceEntries = value; } },
  _syncWorkspaceHiddenToggle: { enumerable: true, get: () => _syncWorkspaceHiddenToggle, set: (value) => { _syncWorkspaceHiddenToggle = value; } },
  toggleWorkspaceHiddenFiles: { enumerable: true, get: () => toggleWorkspaceHiddenFiles, set: (value) => { toggleWorkspaceHiddenFiles = value; } },
  _closeWorkspacePrefsMenu: { enumerable: true, get: () => _closeWorkspacePrefsMenu, set: (value) => { _closeWorkspacePrefsMenu = value; } },
  _positionWorkspacePrefsMenu: { enumerable: true, get: () => _positionWorkspacePrefsMenu, set: (value) => { _positionWorkspacePrefsMenu = value; } },
  _buildWorkspacePrefsMenu: { enumerable: true, get: () => _buildWorkspacePrefsMenu, set: (value) => { _buildWorkspacePrefsMenu = value; } },
  toggleWorkspacePrefsMenu: { enumerable: true, get: () => toggleWorkspacePrefsMenu, set: (value) => { toggleWorkspacePrefsMenu = value; } },
  bindWorkspaceHeadingActions: { enumerable: true, get: () => bindWorkspaceHeadingActions, set: (value) => { bindWorkspaceHeadingActions = value; } },
  _syncWorkspaceHeadingState: { enumerable: true, get: () => _syncWorkspaceHeadingState, set: (value) => { _syncWorkspaceHeadingState = value; } },
  _workspaceContextMenuItem: { enumerable: true, get: () => _workspaceContextMenuItem, set: (value) => { _workspaceContextMenuItem = value; } },
  _copyTextWithFallback: { enumerable: true, get: () => _copyTextWithFallback, set: (value) => { _copyTextWithFallback = value; } },
  _workspaceCreateTargetLabel: { enumerable: true, get: () => _workspaceCreateTargetLabel, set: (value) => { _workspaceCreateTargetLabel = value; } },
  _workspaceJoinTargetPath: { enumerable: true, get: () => _workspaceJoinTargetPath, set: (value) => { _workspaceJoinTargetPath = value; } },
  _showWorkspaceRootContextMenu: { enumerable: true, get: () => _showWorkspaceRootContextMenu, set: (value) => { _showWorkspaceRootContextMenu = value; } },
  renderFileTree: { enumerable: true, get: () => renderFileTree, set: (value) => { renderFileTree = value; } },
  _setWsDragData: { enumerable: true, get: () => _setWsDragData, set: (value) => { _setWsDragData = value; } },
  _clearWsDragData: { enumerable: true, get: () => _clearWsDragData, set: (value) => { _clearWsDragData = value; } },
  _isWorkspaceTreeMoveDrag: { enumerable: true, get: () => _isWorkspaceTreeMoveDrag, set: (value) => { _isWorkspaceTreeMoveDrag = value; } },
  _wsDragSrcPath: { enumerable: true, get: () => _wsDragSrcPath, set: (value) => { _wsDragSrcPath = value; } },
  _wsDragSrcType: { enumerable: true, get: () => _wsDragSrcType, set: (value) => { _wsDragSrcType = value; } },
  _workspaceParentDir: { enumerable: true, get: () => _workspaceParentDir, set: (value) => { _workspaceParentDir = value; } },
  _clearWorkspaceMoveDragOver: { enumerable: true, get: () => _clearWorkspaceMoveDragOver, set: (value) => { _clearWorkspaceMoveDragOver = value; } },
  _remapWorkspaceCachesAfterMove: { enumerable: true, get: () => _remapWorkspaceCachesAfterMove, set: (value) => { _remapWorkspaceCachesAfterMove = value; } },
  _bindWorkspaceMoveDropTarget: { enumerable: true, get: () => _bindWorkspaceMoveDropTarget, set: (value) => { _bindWorkspaceMoveDropTarget = value; } },
  elideMiddle: { enumerable: true, get: () => elideMiddle, set: (value) => { elideMiddle = value; } },
  _renderTreeItems: { enumerable: true, get: () => _renderTreeItems, set: (value) => { _renderTreeItems = value; } },
  _showFileContextMenu: { enumerable: true, get: () => _showFileContextMenu, set: (value) => { _showFileContextMenu = value; } },
  renderTray: { enumerable: true, get: () => renderTray, set: (value) => { renderTray = value; } },
  _uploadTooLargeMessage: { enumerable: true, get: () => _uploadTooLargeMessage, set: (value) => { _uploadTooLargeMessage = value; } },
  _showUploadTooLarge: { enumerable: true, get: () => _showUploadTooLarge, set: (value) => { _showUploadTooLarge = value; } },
  addFiles: { enumerable: true, get: () => addFiles, set: (value) => { addFiles = value; } },
  _uploadPendingFilesCurrentSession: { enumerable: true, get: () => _uploadPendingFilesCurrentSession, set: (value) => { _uploadPendingFilesCurrentSession = value; } },
  _uploadPendingFilesHideProgressBar: { enumerable: true, get: () => _uploadPendingFilesHideProgressBar, set: (value) => { _uploadPendingFilesHideProgressBar = value; } },
  _uploadPendingFilesShowProgressBar: { enumerable: true, get: () => _uploadPendingFilesShowProgressBar, set: (value) => { _uploadPendingFilesShowProgressBar = value; } },
  _uploadPendingFilesSyncProgressForSession: { enumerable: true, get: () => _uploadPendingFilesSyncProgressForSession, set: (value) => { _uploadPendingFilesSyncProgressForSession = value; } },
  _uploadPendingFilesUpdateProgress: { enumerable: true, get: () => _uploadPendingFilesUpdateProgress, set: (value) => { _uploadPendingFilesUpdateProgress = value; } },
  _performWorkspaceMove: { enumerable: true, get: () => _performWorkspaceMove, set: (value) => { _performWorkspaceMove = value; } },
  deleteWorkspaceDir: { enumerable: true, get: () => deleteWorkspaceDir, set: (value) => { deleteWorkspaceDir = value; } },
  _inlineRenameFileItem: { enumerable: true, get: () => _inlineRenameFileItem, set: (value) => { _inlineRenameFileItem = value; } },
  deleteWorkspaceFile: { enumerable: true, get: () => deleteWorkspaceFile, set: (value) => { deleteWorkspaceFile = value; } },
  promptNewFile: { enumerable: true, get: () => promptNewFile, set: (value) => { promptNewFile = value; } },
  promptNewFolder: { enumerable: true, get: () => promptNewFolder, set: (value) => { promptNewFolder = value; } },
  uploadPendingFiles: { enumerable: true, get: () => uploadPendingFiles, set: (value) => { uploadPendingFiles = value; } },
  WORKSPACE_HIDDEN_FILE_NAMES: { enumerable: true, get: () => WORKSPACE_HIDDEN_FILE_NAMES },
  WORKSPACE_HIDDEN_FILE_PREFIXES: { enumerable: true, get: () => WORKSPACE_HIDDEN_FILE_PREFIXES },
  _uploadPendingFilesProgressBySession: { enumerable: true, get: () => _uploadPendingFilesProgressBySession },
  _workspacePrefsMenu: { enumerable: true, get: () => _workspacePrefsMenu, set: (value) => { _workspacePrefsMenu = value; } },
  _workspacePrefsAnchor: { enumerable: true, get: () => _workspacePrefsAnchor, set: (value) => { _workspacePrefsAnchor = value; } },
  _wsActiveDragPath: { enumerable: true, get: () => _wsActiveDragPath, set: (value) => { _wsActiveDragPath = value; } },
  _wsActiveDragType: { enumerable: true, get: () => _wsActiveDragType, set: (value) => { _wsActiveDragType = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
