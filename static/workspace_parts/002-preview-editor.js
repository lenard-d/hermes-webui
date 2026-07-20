// File extension sets for preview routing (must match server-side sets)
const IMAGE_EXTS  = new Set(['.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.bmp']);
const MD_EXTS     = new Set(['.md','.markdown','.mdown']);
const HTML_EXTS   = new Set(['.html','.htm']);
const PDF_EXTS    = new Set(['.pdf']);
const AUDIO_EXTS  = new Set(['.mp3','.wav','.m4a','.aac','.ogg','.oga','.opus','.flac']);
const VIDEO_EXTS  = new Set(['.mp4','.mov','.m4v','.webm','.ogv','.avi','.mkv']);
const MD_PREVIEW_RICH_RENDER_MAX_BYTES = 256 * 1024;
const MD_PREVIEW_RICH_RENDER_MAX_LINES = 5000;
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.doc','.xls','.ppt','.odt','.ods','.odp',
  '.zip','.tar','.gz','.bz2','.7z','.rar',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

function markdownPreviewByteLength(content){
  const text=String(content||'');
  if(typeof Blob==='function') return new Blob([text]).size;
  if(typeof TextEncoder==='function') return new TextEncoder().encode(text).length;
  return unescape(encodeURIComponent(text)).length;
}

function markdownPreviewLineCount(content){
  const text=String(content||'');
  if(!text) return 1;
  return text.split('\n').length;
}

function shouldRenderMarkdownPreviewAsPlainText(content){
  return markdownPreviewByteLength(content)>MD_PREVIEW_RICH_RENDER_MAX_BYTES
    || markdownPreviewLineCount(content)>MD_PREVIEW_RICH_RENDER_MAX_LINES;
}

function largeMarkdownPlainTextStatus(content){
  const bytes=markdownPreviewByteLength(content);
  const lines=markdownPreviewLineCount(content);
  const sizeLabel=bytes>=1024?`${Math.round(bytes/1024)} KB`:`${bytes} B`;
  return `Large markdown file (${sizeLabel}, ${lines} lines) shown as plain text. Click "Render as markdown anyway" to force rich rendering, or Edit to view raw.`;
}

function setLargeMarkdownForceRenderVisible(visible){
  const btn=$('btnRenderMarkdownAnyway');
  if(btn) btn.style.display=visible?'inline-flex':'none';
}

function renderMarkdownPreviewContent(data){
  const target=data&&data.el?data.el:$('previewMd');
  if(!data||!data.el) showPreview('md');
  target.innerHTML=renderMd(data.content);
  requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
}

function renderCodePreviewContent(path, content){
  showPreview('code');
  const codeEl=document.createElement('code');
  codeEl.textContent=content;
  const lang=_prismLanguageForPath(path);
  if(lang) codeEl.className='language-'+lang;
  const pre=$('previewCode');
  pre.textContent='';
  // Prism.highlightElement() propagates the language-* class onto the
  // parent <pre>, so a previously-previewed code file leaves e.g.
  // "language-css" on #previewCode. A subsequent plain-text file builds a
  // class-less <code>, and Prism walks up to that stale ancestor class and
  // mis-highlights prose. Strip any inherited language-* token from the
  // <pre> before each render so highlighting never leaks across files.
  pre.className=pre.className.replace(/\blanguage-\S+/g,'').replace(/\s+/g,' ').trim();
  pre.appendChild(codeEl);
  // Only invoke Prism when we actually assigned a language; otherwise the
  // class-less <code> would inherit any ancestor language-* class.
  if(lang&&typeof Prism!=='undefined'&&typeof Prism.highlightElement==='function'){
    Prism.highlightElement(codeEl);
  }
}

function renderCsvPreviewContent(path, content){
  if(typeof buildCsvTablePreview!=='function') return false;
  const preview=buildCsvTablePreview(path, content);
  if(!preview) return false;
  showPreview('csv');
  // Preserve the raw CSV text so the Edit flow can repopulate the textarea and
  // a save can re-render the table from the edited source (#4025 review, Codex).
  if(typeof content==='string'){
    _previewRawContent = content;
    _previewRawContentPath = path;
  }
  if(preview.html){
    $('previewMd').innerHTML=preview.html;
    return true;
  }
  if(preview.errorKey&&typeof _csvPreviewErrorHtml==='function'){
    $('previewMd').innerHTML=_csvPreviewErrorHtml(path, preview.errorKey);
    return true;
  }
  return false;
}

function forceRenderMarkdownPreview(){
  // #3378 review (Codex): don't force-render from a dirty/open editor — the
  // cached raw content would not reflect the unsaved edit. Require a saved,
  // non-dirty state and cached content that belongs to the current file.
  if(_previewDirty || $('previewEditArea').style.display!=='none') return;
  if(!_previewRawContent || _previewRawContentPath!==_previewCurrentPath) return;
  openFile(_previewCurrentPath,{forceRichMarkdown:true});
  setStatus('Markdown rendered for this file.');
}

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'csv' | 'md' | 'image' | 'html' | 'pdf' | 'audio' | 'video'
let _previewDirty = false;     // true when edits are unsaved
let _previewServerEditable = null;  // backend editability metadata when available
let _previewSaveRoute = '/api/file/save';  // current save adapter for the open preview
let _previewOfficeFormat = '';  // current claimed Office format, if any
let _previewPreviewKind = '';  // preview family returned by the backend

function showPreview(mode){
  // mode: 'code' | 'csv' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = (mode==='md'||mode==='csv') ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='csv'?'csv':mode==='md'?'md':mode==='html'?'html':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf')?'inline-flex':'none';
  setLargeMarkdownForceRenderVisible(false);
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = !_workspacePathIsReadOnly(_previewCurrentPath)
    && (_previewServerEditable===null
      ? (_previewCurrentMode==='code'||_previewCurrentMode==='md'||_previewCurrentMode==='csv')
      : !!_previewServerEditable);
  btn.style.display = editable?'':'none';
  const editing = $('previewEditArea').style.display!=='none';
  btn.innerHTML = editing ? `&#128190; ${t('save')}` : `&#9998; ${t('edit')}`;
  btn.title = editing ? t('save_title') : t('edit_title');
  btn.style.color = editing ? 'var(--blue)' : '';
  if(_previewDirty) btn.innerHTML = '&#128190; Save*';
}

async function toggleEditMode(){
  const editing = $('previewEditArea').style.display!=='none';
  if(_workspacePathIsReadOnly(_previewCurrentPath)){
    showToast(t('external_link_read_only'), 2000);
    return;
  }
  if(!editing && _previewServerEditable===false){
    showToast('This Office document is preview-only.', 3000, 'error');
    return;
  }
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      const saved=await api(_previewSaveRoute||'/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      const savedContent=saved&&typeof saved.content==='string'?saved.content:content;
      if(saved && typeof saved.editable==='boolean') _previewServerEditable = saved.editable;
      if(saved && saved.preview_kind) _previewPreviewKind = saved.preview_kind;
      if(saved && saved.office_format) _previewOfficeFormat = saved.office_format;
      if(saved && saved.preview_kind==='office' && saved.office_format==='docx'){
        _previewSaveRoute = '/api/file/office-save';
      }
      _previewDirty=false;
      // Update read-only views AND the cached raw content so a later
      // "Render as markdown anyway" force-render reflects the just-saved text
      // (not the stale pre-edit fetch). #3378 review (Codex).
      _previewRawContent = savedContent;
      _previewRawContentPath = _previewCurrentPath;
      if(_previewCurrentMode==='code') $('previewCode').textContent=savedContent;
      else if(_previewCurrentMode==='csv') renderCsvPreviewContent(_previewCurrentPath, savedContent);
      else renderMarkdownPreviewContent({content:savedContent});
      $('previewEditArea').style.display='none';
      if(_previewCurrentMode==='code') $('previewCode').style.display='';
      else $('previewMd').style.display='';
      showToast(t('saved'));
    }catch(e){setStatus(t('save_failed')+e.message);}
  }else{
    // Enter edit mode: populate textarea with current content
    const currentText = _previewCurrentMode==='code'
      ? $('previewCode').textContent
      : _previewRawContent||'';
    $('previewEditArea').value=currentText;
    $('previewEditArea').style.display='';
    if(_previewCurrentMode==='code') $('previewCode').style.display='none';
    else $('previewMd').style.display='none';
    // Escape cancels the edit without saving
    $('previewEditArea').onkeydown=e=>{
      if(e.key==='Escape'){e.preventDefault();cancelEditMode();}
    };
  }
  updateEditBtn();
}

let _previewRawContent = '';  // raw text for md files (to populate editor)
let _previewRawContentPath = '';  // path that _previewRawContent belongs to (#3378 force-render cache guard)

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

// Map file extensions to Prism.js language identifiers.
// Prism autoloader fetches missing language components from CDN on demand.
const _PRISM_LANG_MAP={
  js:'javascript',mjs:'javascript',jsx:'jsx',ts:'typescript',tsx:'tsx',
  py:'python',pyw:'python',pyi:'python',
  rb:'ruby',go:'go',rs:'rust',java:'java',kt:'kotlin',kts:'kotlin',
  c:'c',h:'c',cpp:'cpp',cxx:'cpp',hpp:'cpp',cc:'cpp',
  cs:'csharp',swift:'swift',scala:'scala',
  php:'php',pl:'perl',pm:'perl',r:'r',lua:'lua',
  sh:'bash',bash:'bash',zsh:'bash',fish:'bash',
  ps1:'powershell',psm1:'powershell',
  sql:'sql',graphql:'graphql',
  json:'json',yaml:'yaml',yml:'yaml',toml:'toml',xml:'xml',
  html:'markup',htm:'markup',svg:'markup',vue:'markup',
  css:'css',scss:'scss',sass:'sass',less:'less',
  md:'markdown',markdown:'markdown',
  dockerfile:'docker',makefile:'makefile',cmake:'cmake',
  ini:'ini',cfg:'ini',conf:'ini',properties:'properties',
  diff:'diff',patch:'diff',
  txt:'',log:'',csv:'',tsv:'',
};
const _PRISM_BASENAME_LANG_MAP={
  'dockerfile':'docker','makefile':'makefile','gnumakefile':'makefile',
  'cmakelists.txt':'cmake',
  '.gitignore':'ignore','.dockerignore':'ignore',
};
function _prismLanguageForPath(path){
  const base=String(path||'').split(/[\\/]/).pop().toLowerCase();
  if(base.startsWith('dockerfile.')) return 'docker';
  if(_PRISM_BASENAME_LANG_MAP[base]!==undefined) return _PRISM_BASENAME_LANG_MAP[base];
  const ext=fileExt(path).replace(/^\./,'');
  return _PRISM_LANG_MAP[ext]!==undefined?_PRISM_LANG_MAP[ext]:'plaintext';
}

async function openFile(path, opts={}){
  if(!S.session)return;
  const ext=fileExt(path);
  const bustCache=!!(opts&&opts.bustCache);
  const forceRichMarkdown=!!(opts&&opts.forceRichMarkdown);
  const cacheBust=bustCache?`&_=${Date.now()}`:'';

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  _previewServerEditable = null;
  _previewSaveRoute = '/api/file/save';
  _previewOfficeFormat = '';
  _previewPreviewKind = '';

  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';

  _previewCurrentPath = path;
  renderFileBreadcrumb(path);
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=_workspaceRouteForPath(path, 'raw') + cacheBust;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext)||VIDEO_EXTS.has(ext)){
    const mode=VIDEO_EXTS.has(ext)?'video':'audio';
    showPreview(mode);
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const wrap=$('previewMediaWrap');
    if(wrap){
      wrap.innerHTML=(typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode,url,path.split('/').pop()||path)
        : `<${mode} src="${url.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const frame=$('previewPdfFrame');
    if(frame){
      frame.src=''; // clear first to avoid stale content
      frame.src=url;
      frame.title=`PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      // #3378 review (Codex): only reuse cached raw content when it actually
      // belongs to the requested path. `path===_previewCurrentPath` is tautological
      // here (_previewCurrentPath was just assigned above), so guard on the
      // dedicated _previewRawContentPath instead — otherwise a force-render after a
      // file switch could re-render the previous file's cached content.
      const data=forceRichMarkdown&&path===_previewRawContentPath&&_previewRawContent
        ? {content:_previewRawContent}
        : await api(_workspaceRouteForPath(path, 'read'));
      _previewRawContent = data.content;
      _previewRawContentPath = path;
      if(!forceRichMarkdown && shouldRenderMarkdownPreviewAsPlainText(data.content)){
        showPreview('code');
        $('previewCode').textContent=data.content;
        setLargeMarkdownForceRenderVisible(true);
        setStatus(largeMarkdownPlainTextStatus(data.content));
        return;
      }
      renderMarkdownPreviewContent(data);
    }catch(e){setStatus(t('file_open_failed'));}
  } else if(HTML_EXTS.has(ext)){
    // HTML: render in sandboxed iframe via raw endpoint.
    // SECURITY TRADEOFF: We use sandbox="allow-scripts" which lets inline JS run
    // but prevents access to the parent frame (origin isolation). This is a
    // deliberate choice — the user is previewing their own workspace files, so
    // blocking scripts entirely would break most HTML documents. The sandbox
    // still prevents the preview from navigating the parent, accessing cookies,
    // or reading other origin data. If a stricter mode is needed, remove
    // allow-scripts (or add sandbox="") to disable all JS execution.
    showPreview('html');
    const url=_workspaceRouteForPath(path, 'raw', {inline:true}) + cacheBust;
    const iframe=$('previewHtmlIframe');
    if(iframe){
      iframe.src=''; // clear first to avoid stale content
      iframe.src=url;
    }
  } else if(ext==='.csv'){
    try{
      const data=await api(_workspaceRouteForPath(path, 'read'));
      if(data.binary){
        downloadFile(path);
        return;
      }
      if(renderCsvPreviewContent(path, data.content)) return;
      renderCodePreviewContent(path, data.content);
    }catch(e){
      downloadFile(path);
    }
  } else {
    // Plain code / text -- but fall back to download if server signals binary
    try{
      const data=await api(_workspaceRouteForPath(path, 'read'));
      if(data.binary){
        // Server flagged this as binary content
        downloadFile(path);
        return;
      }
      if(data.preview_kind==='office'){
        _previewRawContent = data.content || '';
        _previewRawContentPath = path;
        _previewServerEditable = typeof data.editable === 'boolean' ? data.editable : null;
        _previewPreviewKind = data.preview_kind || '';
        _previewOfficeFormat = data.office_format || '';
        _previewSaveRoute = data.preview_kind==='office' ? '/api/file/office-save' : '/api/file/save';
      }
      renderCodePreviewContent(path, data.content);
  }catch(e){
      const grant = _workspaceEscapeGrantForPath(path);
      if(grant && e && e.status===403){
        _clearWorkspaceEscapeGrant(grant.path);
        showToast(t('external_link_grant_expired') || t('file_open_failed'), 5000, 'error');
        return;
      }
      // If it's a 400/too-large error, offer download instead
      downloadFile(path);
    }
  }
}

function downloadFile(path){
  if(!S.session)return;
  // Trigger browser download via the raw file endpoint with content-disposition attachment
  const url=_workspaceRouteForPath(path, 'raw', {download:true});
  const filename=path.split('/').pop();
  const a=document.createElement('a');
  a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();
  setTimeout(()=>document.body.removeChild(a),100);
  showToast(t('downloading',filename),2000);
}


// ── Render breadcrumb for file preview mode ──────────────────────────────────
function renderFileBreadcrumb(filePath) {
  const bar = $('breadcrumbBar');
  if (!bar) return;
  bar.style.display = 'flex';
  const upBtn = $('btnUpDir');
  if (upBtn) upBtn.style.display = '';

  bar.innerHTML = '';
  // Root
  const root = document.createElement('span');
  root.className = 'breadcrumb-seg breadcrumb-link';
  root.textContent = '~';
  root.onclick = () => { loadDir('.'); };
  bar.appendChild(root);

  const parts = filePath.split('/');
  let accumulated = '';
  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement('span');
    sep.className = 'breadcrumb-sep';
    sep.textContent = '/';
    bar.appendChild(sep);

    accumulated += (accumulated ? '/' : '') + parts[i];
    const seg = document.createElement('span');
    seg.textContent = parts[i];
    if (i < parts.length - 1) {
      seg.className = 'breadcrumb-seg breadcrumb-link';
      const target = accumulated;
      seg.onclick = () => { loadDir(target); };
    } else {
      seg.className = 'breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

function openInBrowser(){
  if(!_previewCurrentPath||!S.session) return;
  const url=_workspaceRouteForPath(_previewCurrentPath, 'raw', {inline:true});
  window.open(url,'_blank','noopener');
}
// openInBrowser keeps the helper-based raw path, which expands to an explicit &inline=1 URL.

async function copyPreviewRelativePath(){
  if(!_previewCurrentPath) return;
  const btn=$('btnCopyPreviewRelPath');
  if(btn&&btn.disabled) return;
  if(btn) btn.disabled=true;
  try{
    const rel=_normalizeWorkspaceRelPath(_previewCurrentPath)||_previewCurrentPath;
    if(typeof _copyTextWithFallback==='function'){
      await _copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'));
      return;
    }
    try{
      await navigator.clipboard.writeText(rel);
      showToast(t('path_copied'));
    }catch(clipErr){
      const ta=document.createElement('textarea');
      ta.value=rel;
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
  }finally{
    if(btn) btn.disabled=false;
  }
}


globalThis.HermesWorkspace.parts.previewEditor=Object.freeze({
  fileExt,
  shouldRenderMarkdownPreviewAsPlainText,
  renderMarkdownPreviewContent,
  renderCodePreviewContent,
  renderCsvPreviewContent,
  forceRenderMarkdownPreview,
  showPreview,
  updateEditBtn,
  toggleEditMode,
  cancelEditMode,
  openFile,
  downloadFile,
  renderFileBreadcrumb,
  openInBrowser,
  copyPreviewRelativePath,
});
