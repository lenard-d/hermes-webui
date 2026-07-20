import { _showWorkspaceRootContextMenu } from './workspace-file-actions.js';
import { $, S, esc } from './state.js';

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
  if(typeof renderFileTree==='function')renderFileTree();
}

try{S.showHiddenWorkspaceFiles=localStorage.getItem('hermes-workspace-show-hidden-files')==='1';}catch(_){}

// Hidden-file visibility is a set-once preference, so it lives in a compact
// menu instead of permanently consuming workspace-panel height.
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

export {
  WORKSPACE_HIDDEN_FILE_NAMES,
  WORKSPACE_HIDDEN_FILE_PREFIXES,
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
  _workspacePrefsMenu,
  _workspacePrefsAnchor,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  WORKSPACE_HIDDEN_FILE_NAMES: { enumerable: true, get: () => WORKSPACE_HIDDEN_FILE_NAMES },
  WORKSPACE_HIDDEN_FILE_PREFIXES: { enumerable: true, get: () => WORKSPACE_HIDDEN_FILE_PREFIXES },
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
  _workspacePrefsMenu: { enumerable: true, get: () => _workspacePrefsMenu, set: (value) => { _workspacePrefsMenu = value; } },
  _workspacePrefsAnchor: { enumerable: true, get: () => _workspacePrefsAnchor, set: (value) => { _workspacePrefsAnchor = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
