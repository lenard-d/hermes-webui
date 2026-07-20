function _wsExpandKey(){
  const ws=S.session&&S.session.workspace;
  return ws?'hermes-webui-expanded:'+ws:null;
}
function _saveExpandedDirs(){
  const key=_wsExpandKey();if(!key)return;
  try{localStorage.setItem(key,JSON.stringify([...(S._expandedDirs||new Set())]));}catch(e){}
}
function _restoreExpandedDirs(){
  const key=_wsExpandKey();
  if(!key){S._expandedDirs=new Set();return;}
  try{
    const raw=localStorage.getItem(key);
    S._expandedDirs=raw?new Set(JSON.parse(raw)):new Set();
  }catch(e){S._expandedDirs=new Set();}
}

function _escapeGrantStore(){
  if(!S._escapeGrants) S._escapeGrants = Object.create(null);
  return S._escapeGrants;
}

function _normalizeWorkspaceRelPath(path){
  let raw = String(path || '').trim().replace(/\\/g, '/');
  if(!raw || raw === '.') return '.';
  if(raw.startsWith('/')) return '';
  const parts = [];
  for(const part of raw.split('/')){
    if(!part || part === '.') continue;
    if(part === '..'){
      if(parts.length) parts.pop();
      else return '';
      continue;
    }
    parts.push(part);
  }
  return parts.length ? parts.join('/') : '.';
}

function _isSameOrChildPath(base, path){
  const normalizedBase = _normalizeWorkspaceRelPath(base);
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedBase || !normalizedPath) return false;
  if(normalizedBase === '.') return true;
  return normalizedPath === normalizedBase || normalizedPath.startsWith(`${normalizedBase}/`);
}

function _workspaceEscapeGrantForPath(path){
  const grants = _escapeGrantStore();
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedPath || !S.session || !S.session.session_id) return null;
  const sessionId = S.session.session_id;
  let best = null;
  for(const root of Object.keys(grants)){
    const grant = grants[root];
    if(!grant || grant.sessionId !== sessionId) continue;
    if(grant.expiresAt && Date.now() >= grant.expiresAt){
      delete grants[root];
      continue;
    }
    if(!_isSameOrChildPath(root, normalizedPath)) continue;
    if(!best || root.length > best.root.length) best = {root, grant};
  }
  return best ? best.grant : null;
}

function _workspaceEscapeExactGrant(path){
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  if(!grant) return null;
  return grant.path === normalizedPath ? grant : null;
}

function _storeWorkspaceEscapeGrant(data){
  if(!S.session || !data || !data.token) return null;
  const grants = _escapeGrantStore();
  const root = _normalizeWorkspaceRelPath(data.path || '');
  if(!root) return null;
  const grant = {
    sessionId: S.session.session_id,
    path: root,
    token: String(data.token),
    expiresAt: Number(data.expires_at || 0) * 1000,
    isDir: !!data.is_dir,
  };
  grants[root] = grant;
  return grant;
}

function _clearWorkspaceEscapeGrant(path){
  const grants = S._escapeGrants;
  if(!grants) return;
  const root = _normalizeWorkspaceRelPath(path);
  if(root && grants[root]) delete grants[root];
}

function _workspacePathIsReadOnly(path){
  return !!_workspaceEscapeGrantForPath(path || S.currentDir || '.');
}

function _workspaceRouteForPath(path, kind, opts={}){
  // Resolve the app-relative "/api/…" route against document.baseURI so the
  // URLs that are consumed OUTSIDE api() — previewImg.src, the media/pdf/html
  // frame src, the download anchor, window.open — keep working under a subpath
  // mount like /hermes/. A bare "/api/…" string resolves to the server root
  // there and 404s. (api() strips the leading slash and re-resolves against
  // baseURI itself, so routes passed through it are unaffected by already
  // being absolute.)
  const route=_workspaceRouteForPathRel(path, kind, opts);
  if(!route) return route;
  // Non-browser test harnesses have no document/location: keep the app-relative form.
  const base=(typeof document!=='undefined'&&document.baseURI)||(typeof location!=='undefined'&&location.href)||'';
  if(!base||!/^https?:\/\//i.test(base)) return route;
  const rel=route.startsWith('/') ? route.slice(1) : route;
  return new URL(rel, base).href;
}

function _workspaceRouteForPathRel(path, kind, opts={}){
  if(!S.session) return '';
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  const sessionId = encodeURIComponent(S.session.session_id);
  const params = new URLSearchParams({session_id:S.session.session_id, path:normalizedPath || '.'});
  if(grant){
    params.set('token', grant.token);
    if(kind === 'raw' && opts.download) params.set('download', '1');
    if(kind === 'raw' && opts.inline) params.set('inline', '1');
    if(kind === 'list') return `/api/escape/list?${params.toString()}`;
    if(kind === 'read') return `/api/escape/file/read?${params.toString()}`;
    if(kind === 'raw') return `/api/escape/file/raw?${params.toString()}`;
  }
  if(kind === 'list') return `/api/list?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'read') return `/api/file?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'raw'){
    const extra = [];
    if(opts.download) extra.push('download=1');
    // Inline previews intentionally preserve a literal &inline=1 marker in this file.
    if(opts.inline) extra.push('inline=1');
    const suffix = extra.length ? `&${extra.join('&')}` : '';
    return `/api/file/raw?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}${suffix}`;
  }
  return '';
}

async function authorizeWorkspaceEscapeNavigation(item){
  if(!S.session || !item || !item.path) return null;
  const normalizedPath = _normalizeWorkspaceRelPath(item.path);
  const exactGrant = _workspaceEscapeExactGrant(normalizedPath);
  if(!exactGrant){
    const ok = await showConfirmDialog({
      title: item.name || normalizedPath,
      message: t('external_link_open_confirm'),
      confirmLabel: t('dialog_confirm_btn'),
      danger: false,
      hideCancel: true,
      focusCancel: false,
    });
    if(!ok) return null;
  }
  try{
    const data = await api('/api/escape/authorize', {
      method: 'POST',
      body: JSON.stringify({
        session_id: S.session.session_id,
        path: normalizedPath,
      }),
    });
    const grant = _storeWorkspaceEscapeGrant(data);
    if(!grant) throw new Error('Missing escape authorization token');
    showToast(t('external_link_read_only'), 2000);
    return grant;
  }catch(e){
    showToast(t('external_link_grant_expired') || (e && e.message ? e.message : String(e)), 5000, 'error');
    return null;
  }
}

let _workspacePanelActiveTab = 'files';
let _renderSessionArtifactsTimer = null;
let _workspaceTodosLastRenderedHash = null;

function _setWorkspacePanelTabDataset(){
  const panel = document.querySelector('.rightpanel');
  if(panel) panel.dataset.activeTab = _workspacePanelActiveTab;
}

function scheduleRenderSessionArtifacts(){
  if(_renderSessionArtifactsTimer) clearTimeout(_renderSessionArtifactsTimer);
  _renderSessionArtifactsTimer = setTimeout(()=>{
    _renderSessionArtifactsTimer = null;
    renderSessionArtifacts();
  }, 100);
}

function _workspaceTodosHash(items){
  if(!Array.isArray(items)) return '';
  let h=items.length+'|';
  for(let i=0;i<items.length;i++){
    const t=items[i]||{};
    h+=String(t.id==null?'':t.id)+'\x1f'+String(t.content==null?(t.text==null?'':t.text):t.content)+'\x1f'+String(t.status==null?'':t.status)+'\x1e';
  }
  return h;
}

function _workspaceTodosTabIsActive(){
  if(typeof window==='undefined'||window._workspaceTodosTab!==true) return false;
  if(typeof document==='undefined') return false;
  const rightPanel=document.querySelector('.rightpanel');
  if(!rightPanel||!rightPanel.dataset||rightPanel.dataset.activeTab!=='todos') return false;
  const tab=document.getElementById('workspaceTodosTab');
  const panel=document.getElementById('workspaceTodosPanel');
  return !!(tab&&panel&&!tab.hidden&&!panel.hidden);
}

function _resetWorkspaceTodosRenderCache(){
  _workspaceTodosLastRenderedHash=null;
}

function _refreshWorkspacePanelTodos(){
  if(!_workspaceTodosTabIsActive()) return;
  _loadWorkspacePanelTodos();
}

if(typeof document !== 'undefined'){
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _setWorkspacePanelTabDataset, {once:true});
  else _setWorkspacePanelTabDataset();
}

function switchWorkspacePanelTab(tab){
  _workspacePanelActiveTab = tab === 'artifacts' ? 'artifacts' : tab === 'todos' ? 'todos' : 'files';
  _setWorkspacePanelTabDataset();
  const filesTab = $('workspaceFilesTab');
  const artifactsTab = $('workspaceArtifactsTab');
  const todosTab = $('workspaceTodosTab');
  if(filesTab){
    filesTab.classList.toggle('active', _workspacePanelActiveTab === 'files');
    filesTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'files' ? 'true' : 'false');
  }
  if(artifactsTab){
    artifactsTab.classList.toggle('active', _workspacePanelActiveTab === 'artifacts');
    artifactsTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'artifacts' ? 'true' : 'false');
  }
  if(todosTab){
    todosTab.classList.toggle('active', _workspacePanelActiveTab === 'todos');
    todosTab.setAttribute('aria-selected', _workspacePanelActiveTab === 'todos' ? 'true' : 'false');
  }
  const artifacts = $('workspaceArtifacts');
  if(artifacts) artifacts.hidden = _workspacePanelActiveTab !== 'artifacts';
  const todosPanel = $('workspaceTodosPanel');
  if(todosPanel) todosPanel.hidden = _workspacePanelActiveTab !== 'todos';
  if(_workspacePanelActiveTab === 'artifacts') renderSessionArtifacts();
  if(_workspacePanelActiveTab === 'todos') _loadWorkspacePanelTodos();
}

function _loadWorkspacePanelTodos(){
  const panel = $('workspaceTodosPanel');
  if(!panel) return;
  let todos = [];
  try{
    if(S && Array.isArray(S.todos)){
      todos = S.todos;
    } else if(S && S.session && S.session.todo_state && Array.isArray(S.session.todo_state.todos)){
      todos = S.session.todo_state.todos;
    } else if(typeof _legacyTodosFromMessages === 'function'){
      todos = _legacyTodosFromMessages() || [];
    }
  }catch(e){ todos = []; }
  if(!todos.length){
    panel.innerHTML = renderTodoEmptyState({centered:true});
    return;
  }
  panel.innerHTML = renderTodoRows(todos, {metadata:true});
}

function _escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;
// Canonical Hermes mutators plus MCP filesystem aliases that can create/edit files.
const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);

function _normalizeArtifactPath(path){
  if(!path) return '';
  path = String(path).trim().replace(/[\`"'<>),.;:]+$/g,'').replace(/^[\`"'(<]+/g,'');
  if(!path || path.length > 240 || path.includes('://')) return '';
  // Canonicalize workspace-relative prefixes so a file-tree open ("foo.md") and a
  // tool arg recorded as "./foo.md" or "~/foo.md" compare equal for mutation
  // tracking; otherwise an agent edit via a ./-prefixed path leaves the open
  // preview stale (#3262 / pre-release regression-gate finding).
  path = path.replace(/^~\//,'').replace(/^(?:\.\/)+/,'');
  if(!path) return '';
  if(ARTIFACT_IGNORE_RE.test(path)) return '';
  if(!/[./]/.test(path)) return '';
  return path;
}

function _artifactCandidatesFromText(text){
  if(!text || typeof text !== 'string') return [];
  const out = [];
  const seen = new Set();
  const add = (path) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); out.push({path, kind:'diff'});
  };
  // Fallback text mining is intentionally narrow: only diff/patch fences imply
  // the session changed a file. Prose mentions such as "edited package.json" are
  // too noisy for an Artifacts list that should track write/edit outputs.
  const fenced = /```(?:diff|patch)\s*\n[\s\S]*?```/gi;
  let m;
  while((m = fenced.exec(text))){
    const block = m[0];
    const fm = block.match(/(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]\/)?([^\n\t]+)/);
    if(fm) add(fm[1].trim());
  }
  return out;
}

function _artifactCandidatesFromToolCall(tc){
  if(!tc) return [];
  const name = String(tc.name || '').replace(/^functions\./,'');
  const args = tc.arguments || tc.args || tc.input || {};
  const result = tc.result || tc.output || tc.snippet || '';
  const out = [];
  const add = (path, source=name || 'tool') => {
    path = _normalizeArtifactPath(path);
    if(path) out.push({path, kind:source});
  };
  if(ARTIFACT_MUTATION_TOOLS.has(name) && args && typeof args === 'object'){
    for(const key of ['path','file_path','source','destination']) add(args[key]);
    if(Array.isArray(args.paths)) args.paths.forEach(p=>add(p));
    if(Array.isArray(args.edits)) args.edits.forEach(e=>add(e&&e.path));
  }
  const resultText = typeof result === 'string' ? result : (result ? JSON.stringify(result) : '');
  // Tool results may include unified diffs from patch-style tools; scan those
  // narrowly after structured args so diff headers can still contribute paths.
  for(const a of _artifactCandidatesFromText(resultText)) out.push(a);
  if(!out.length && ARTIFACT_MUTATION_TOOLS.has(name)){
    const argsText = typeof args === 'string' ? args : JSON.stringify(args || {});
    for(const a of _artifactCandidatesFromText(argsText)) out.push(a);
  }
  return out;
}

const _turnMutatedPreviewPaths = new Set();

function resetTurnWorkspaceMutations(){
  _turnMutatedPreviewPaths.clear();
}

function noteWorkspaceMutationsFromToolCall(tc){
  for(const a of _artifactCandidatesFromToolCall(tc)){
    const path=_normalizeArtifactPath(a.path);
    if(path) _turnMutatedPreviewPaths.add(path);
  }
}

function noteWorkspaceMutationsFromToolCalls(toolCalls){
  if(!Array.isArray(toolCalls)) return;
  for(const tc of toolCalls) noteWorkspaceMutationsFromToolCall(tc);
}

function _isOpenPreviewPathMutated(){
  if(!_previewCurrentPath) return false;
  const current=_normalizeArtifactPath(_previewCurrentPath);
  return !!(current&&_turnMutatedPreviewPaths.has(current));
}

async function refreshOpenPreviewIfMutated(){
  if(typeof _previewDirty!=='undefined'&&_previewDirty) return;
  if(!_isOpenPreviewPathMutated()) return;
  if(!_previewCurrentPath||!S.session) return;
  await openFile(_previewCurrentPath, { bustCache: true });
}

function collectSessionArtifacts(){
  const items = [];
  const seen = new Set();
  const push = (path, source) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); items.push({path, source});
  };
  // Source 1: session-level tool call summaries (may be empty when messages
  // carry their own tool metadata — see _syncToolCallsForLoadedMessages).
  for(const tc of (S.toolCalls || [])){
    for(const a of _artifactCandidatesFromToolCall(tc)) push(a.path, a.kind || tc.name || 'tool');
  }
  // Source 2 & 3: message-level data — both text-mined diffs and structured
  // tool_calls / tool_use content blocks that survive the S.toolCalls clear.
  for(const msg of (S.messages || [])){
    if(!msg) continue;
    const text = msg.content || msg.text || msg.message || '';
    // Text-mined diff/patch fences (existing path).
    if(typeof text === 'string'){
      for(const a of _artifactCandidatesFromText(text)) push(a.path, a.kind);
    }
    // Structured tool_calls array (OpenAI format: {function:{name,arguments}}).
    if(Array.isArray(msg.tool_calls)){
      for(const tc of msg.tool_calls){
        if(!tc || typeof tc !== 'object') continue;
        const fn = (tc.function && typeof tc.function === 'object') ? tc.function : tc;
        const name = fn.name || tc.name || '';
        let args = fn.arguments || tc.arguments || tc.args || tc.input || {};
        if(typeof args === 'string'){ try{ args = JSON.parse(args); }catch(_){} }
        const fakeTc = {name, args, result: tc.result || tc.output || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || name || 'tool');
      }
    }
    // Structured content array with tool_use blocks (Anthropic format).
    if(Array.isArray(msg.content)){
      for(const block of msg.content){
        if(!block || block.type !== 'tool_use') continue;
        let inp = block.input || {};
        if(typeof inp === 'string'){ try{ inp = JSON.parse(inp); }catch(_){} }
        const fakeTc = {name: block.name || '', args: inp, result: block.result || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || block.name || 'tool');
      }
    }
  }
  return items.slice(0, 50);
}

function renderSessionArtifacts(){
  const root = $('workspaceArtifacts');
  const count = $('workspaceArtifactsCount');
  if(!root) return;
  const items = collectSessionArtifacts();
  if(count) count.textContent = String(items.length);
  if(!S.session){
    root.innerHTML = '<div class="workspace-artifact-empty">Open a conversation to see files changed in this session.</div>';
    return;
  }
  if(!items.length){
    root.innerHTML = '<div class="workspace-artifact-empty">No artifacts detected yet. Files created or edited during this session will appear here.</div>';
    return;
  }
  // Strip workspace prefix for display so long absolute paths don't clutter the list.
  const ws = S.session && S.session.workspace;
  const normWs = ws ? ws.replace(/\/+$/,'') + '/' : '';
  const displayPath = (p) => {
    if(normWs && p.startsWith(normWs)) return p.slice(normWs.length);
    return p;
  };
  root.innerHTML = items.map(item => {
    const dPath = displayPath(item.path);
    const idx = dPath.lastIndexOf('/');
    const fileName = idx >= 0 ? dPath.slice(idx + 1) : dPath;
    const dirPath = idx >= 0 ? dPath.slice(0, idx) : '';
    return `<button type="button" class="workspace-artifact-item" data-artifact-path="${esc(item.path)}" onclick="openArtifactPath(this.dataset.artifactPath)">
      <div class="workspace-artifact-name">${esc(fileName)}</div>
      ${dirPath ? `<div class="workspace-artifact-dir">${esc(dirPath)}</div>` : ''}
      <div class="workspace-artifact-meta">${esc(item.source || 'session')}</div>
    </button>`;
  }).join('');
}

async function _workspacePathExists(path){
  if(!S.session||!path) return false;
  const parts=String(path).split('/').filter(Boolean);
  const name=parts.pop();
  if(!name) return false;
  const dir=parts.length?parts.join('/'):'.';
  const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(dir)}`);
  return (data.entries||[]).some(entry=>entry&&((entry.path===path)||entry.name===name));
}

async function openArtifactPath(path){
  if(!path) return;
  switchWorkspacePanelTab('files');
  let rel = path.replace(/^~\//,'').replace(/^\.\/+/,'');
  // Strip workspace prefix so /api/list receives a workspace-relative path.
  const ws = S.session && S.session.workspace;
  if(ws){
    const normWs = ws.replace(/\/+$/,'') + '/';
    if(rel.startsWith(normWs)) rel = rel.slice(normWs.length);
    else if(rel === ws.replace(/\/+$/,'')) rel = '.';
  }
  if(!rel) rel = '.';
  try{
    if(!(await _workspacePathExists(rel))){
      setStatus(t('file_open_failed'));
      return;
    }
  }catch(_){
    setStatus(t('file_open_failed'));
    return;
  }
  openFile(rel);
}

// ── Workspace file-tree loading skeleton (#4662 Phase 1) ────────────────────
// During a profile switch the right-hand workspace panel would otherwise keep
// showing the previous profile's file tree until /api/list resolves. Show a
// clean tree-shaped skeleton in its place (panel stays open — hiding it is
// jarring). Varied bar widths + a small indent pattern so it reads as a real
// directory listing rather than a mechanical repeat.
const _WS_SKELETON_ROWS = [
  {w: 38, indent: 0, dir: true},
  {w: 72, indent: 0},
  {w: 44, indent: 1},
  {w: 63, indent: 1},
  {w: 80, indent: 0},
  {w: 51, indent: 1},
  {w: 67, indent: 0},
  {w: 39, indent: 1},
];

// Workspace-tree render generation. loadDir() captures this at call time and
// discards its render/cache writes if a newer generation started meanwhile.
// #4671 CORE: an empty-session profile switch REUSES the same session_id, so
// loadDir()'s session_id guard alone can't reject a pre-switch /api/list response
// that resolves after the new profile's loadDir('.') — it would paint the previous
// workspace's files over the switched-to profile. switchToProfile() bumps this
// UNCONDITIONALLY at switch start (even when the workspace panel is closed, since
// loadDir('.') still runs then), so the stale response is rejected.
let _wsTreeGen = 0;
function bumpWorkspaceTreeGen(){
  _wsTreeGen = (typeof _wsTreeGen === 'number' ? _wsTreeGen : 0) + 1;
  return _wsTreeGen;
}
if(typeof window!=='undefined') window.bumpWorkspaceTreeGen = bumpWorkspaceTreeGen;

function showWorkspaceTreeSkeleton(){
  const tree = $('fileTree');
  if(!tree) return;
  const wrap = document.createElement('div');
  wrap.className = 'skeleton-tree';
  wrap.setAttribute('aria-hidden', 'true');
  for(const spec of _WS_SKELETON_ROWS){
    const row = document.createElement('div');
    row.className = 'skeleton-tree-row';
    if(spec.indent) row.style.paddingLeft = (2 + spec.indent * 16) + 'px';
    const glyph = document.createElement('div');
    glyph.className = 'skeleton-glyph';
    const name = document.createElement('div');
    name.className = 'skeleton-bar skeleton-name';
    name.style.width = spec.w + '%';
    row.appendChild(glyph);
    row.appendChild(name);
    // Files (not dirs) show a size on the right; mirror that on leaf rows.
    if(!spec.dir){
      const size = document.createElement('div');
      size.className = 'skeleton-bar skeleton-size';
      row.appendChild(size);
    }
    wrap.appendChild(row);
  }
  tree.innerHTML = '';
  tree.appendChild(wrap);
  tree.style.display = '';
}

// Clear a stranded workspace-tree skeleton (#4662 Opus gate). showWorkspaceTreeSkeleton()
// is shown up front on a profile switch, but the real loadDir('.') that would
// replace it is skipped when the new profile has no bound workspace — leaving a
// shimmering skeleton forever. Call this on the no-workspace path so the tree
// empties instead. Only touches #fileTree when it still holds a skeleton, so
// it can't clobber a real render.
function clearWorkspaceTreeSkeleton(){
  const tree = $('fileTree');
  if(!tree) return;
  if(tree.querySelector('.skeleton-tree')) tree.innerHTML = '';
}

async function loadDir(path, opts={}){
  const preservePreview=!!(opts&&opts.preservePreview);
  const refreshExpanded=!!(opts&&opts.refreshExpanded);
  if(!S.session)return;
  const sessionId=S.session.session_id;
  const treeGen=_wsTreeGen;  // #4671: capture the workspace-tree generation. A profile
                             // switch bumps it (bumpWorkspaceTreeGen), so a stale response
                             // from the previous workspace — which would pass the session_id
                             // guard because an empty-session switch reuses the same id — is
                             // rejected here instead of painting the wrong profile's files.
  try{
    if(!path||path==='.'||refreshExpanded){
      S._dirCache={};
      _restoreExpandedDirs();  // restore per-workspace expanded state after root and refresh resets
    }
    S.currentDir=path||'.';
    const data=await api(
      _workspaceRouteForPath(path, 'list') ||
      `/api/list?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(path||'.')}`
    );
    if(!S.session||S.session.session_id!==sessionId||treeGen!==_wsTreeGen)return;
    S.entries=data.entries||[];renderBreadcrumb();renderFileTree();
    // #2673 — refresh Artifacts tab when its source data (the file tree) updates.
    if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();
    // Pre-fetch contents of restored expanded dirs so they render without a second click
    // (parallelized — avoids serial waterfall when multiple dirs are expanded)
    if(!path||path==='.'||refreshExpanded){
      const expanded=S._expandedDirs||new Set();
      const pending=[...expanded].filter(dirPath=>!S._dirCache[dirPath]);
      if(pending.length){
        const results=await Promise.all(pending.map(dirPath=>
          api(_workspaceRouteForPath(dirPath, 'list'))
            .then(dc=>({dirPath,entries:dc.entries||[]}))
            .catch(()=>({dirPath,entries:[]}))
        ));
        if(!S.session||S.session.session_id!==sessionId||treeGen!==_wsTreeGen)return;
        for(const {dirPath,entries} of results) S._dirCache[dirPath]=entries;
      }
      if(expanded.size>0)renderFileTree();
    }
    if(!preservePreview&&typeof clearPreview==='function'){
      if(typeof _previewDirty!=='undefined'&&_previewDirty){
        showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true}).then(ok=>{if(ok)clearPreview({keepPanelOpen:true});});
      }else{
        clearPreview({keepPanelOpen:true});
      }
    }else if(preservePreview){
      await refreshOpenPreviewIfMutated();
    }
    // Fetch git info for workspace root (non-blocking)
    if(!path||path==='.') _refreshGitBadge();
  }catch(e){
    const grant = _workspaceEscapeGrantForPath(path);
    if(grant && e && e.status===403){
      _clearWorkspaceEscapeGrant(grant.path);
      showToast(t('external_link_grant_expired') || t('file_open_failed'), 5000, 'error');
      return;
    }
    console.warn('loadDir',e);
  }
}

function refreshWorkspacePanel(){
  if(!S.session)return;
  const targetDir = S.currentDir || '.';
  loadDir(targetDir,{refreshExpanded:true});
}

async function _refreshGitBadge(){
  const badge=$('gitBadge');
  if(!badge||!S.session)return;
  const sessionId=S.session.session_id;
  try{
    const data=await api(`/api/git-info?session_id=${encodeURIComponent(sessionId)}`);
    if(!S.session||S.session.session_id!==sessionId)return;
    if(data.git&&data.git.is_git){
      const g=data.git;
      let text=g.branch||'git';
      if(g.dirty>0) text+=` \u00b7 ${g.dirty}\u2206`; // middot + delta
      if(g.behind>0) text+=` \u2193${g.behind}`;
      if(g.ahead>0) text+=` \u2191${g.ahead}`;
      badge.textContent=text;
      badge.className='git-badge'+(g.dirty>0?' dirty':'');
      badge.style.display='';
    } else {
      badge.style.display='none';
      badge.textContent='';
    }
  }catch(e){
    if(!S.session||S.session.session_id!==sessionId)return;
    badge.style.display='none';
  }
}

function navigateUp(){
  if(!S.session||S.currentDir==='.')return;
  const parts=S.currentDir.split('/');
  parts.pop();
  loadDir(parts.length?parts.join('/'):'.');
}


globalThis.HermesWorkspace.parts.navigation=Object.freeze({
  authorizeWorkspaceEscapeNavigation,
  scheduleRenderSessionArtifacts,
  switchWorkspacePanelTab,
  _resetWorkspaceTodosRenderCache,
  _refreshWorkspacePanelTodos,
  resetTurnWorkspaceMutations,
  noteWorkspaceMutationsFromToolCall,
  noteWorkspaceMutationsFromToolCalls,
  refreshOpenPreviewIfMutated,
  collectSessionArtifacts,
  renderSessionArtifacts,
  openArtifactPath,
  bumpWorkspaceTreeGen,
  showWorkspaceTreeSkeleton,
  clearWorkspaceTreeSkeleton,
  loadDir,
  refreshWorkspacePanel,
  navigateUp,
  normalizeWorkspaceRelPath:_normalizeWorkspaceRelPath,
  routeForPath:_workspaceRouteForPath,
});
