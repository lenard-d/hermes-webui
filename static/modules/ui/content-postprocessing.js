import { _clearActivityElapsedTimer, _sanitizeThinkingDisplayText, scrollIfPinned } from './activity-and-scroll.js';
import { _renderLiveAnchorActivitySceneForStream } from './anchor-scenes.js';
import { _firstValidTimestampSeconds, _scrollPinned } from './composer-controls.js';
import { setStatus } from './composer.js';
import { _copyText } from './dialogs-and-reconnect.js';
import { _mountMermaidViewer } from './media-and-quota.js';
import { _deliberateSessionModelPick, _reArmRecoveryPick } from './model-state.js';
import { _suppressBrowserOverflowAnchor } from './navigation.js';
import { _decorateTransparentEventRow, _syncTransparentEventControls, _thinkingActivityNode, _worklogDetailsExpandedDefault, isFinalAnswerOnlyMode, isSimplifiedToolCalling, isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks, _createAssistantTurn, msgContent } from './assistant-turn-presentation.js';
import { renderMessages } from './renderer.js';
import { $, S, esc } from './state.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './tool-worklog.js';
import { _resetMismatchedLiveAssistantTurnForSession, _updateLiveAnchorReasoningRowForFallback, ensureLiveWorklogContainer, isLiveAnchorActivitySceneOwner } from './transparent-worklog.js';

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const msgIdx = parseInt(row.dataset.msgIdx, 10);
  const originalText = row.dataset.rawText || '';
  const body = row.querySelector('.msg-body');
  if(!body || row.dataset.editing) return;
  row.dataset.editing = '1';

  // Replace msg-body with an editable textarea
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = originalText;
  body.replaceWith(ta);
  // Resize after DOM insertion so scrollHeight is correct
  requestAnimationFrame(() => { autoResizeTextarea(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  ta.addEventListener('input', () => autoResizeTextarea(ta));

  // Action bar below the textarea
  const bar = document.createElement('div');
  bar.className = 'msg-edit-bar';
  bar.innerHTML = `<button class="msg-edit-send">Send edit</button><button class="msg-edit-cancel">Cancel</button>`;
  ta.after(bar);

  bar.querySelector('.msg-edit-send').onclick = async () => {
    const newText = ta.value.trim();
    if(!newText) return;
    await submitEdit(msgIdx, newText);
  };
  bar.querySelector('.msg-edit-cancel').onclick = () => cancelEdit(row, originalText, body);

  ta.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey) { if(window._isImeEnter&&window._isImeEnter(e)) return; e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
    if(e.key==='Escape') { e.preventDefault(); cancelEdit(row, originalText, body); }
  });
}

function cancelEdit(row, originalText, originalBody) {
  delete row.dataset.editing;
  const ta = row.querySelector('.msg-edit-area');
  const bar = row.querySelector('.msg-edit-bar');
  if(ta) ta.replaceWith(originalBody);
  if(bar) bar.remove();
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
}

async function submitEdit(msgIdx, newText) {
  if(!S.session || S.busy) return;
  const initialSid = S.session.session_id;
  const absoluteKeepCount = _oldestIdx + msgIdx;
  // #5924: capture the deliberate-pick signal up front (pre-network), scoped to
  // initialSid — a non-default session model (vs profile default), which is
  // inference-free and survives the failed send's marker consumption. See
  // _deliberateSessionModelPick. null → no re-arm → server resolution runs.
  const _recoveryPick=_deliberateSessionModelPick(initialSid);
  if(typeof _ensureAllMessagesLoaded==='function'){
    await _ensureAllMessagesLoaded();
  }
  if(!S.session || S.session.session_id !== initialSid) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: initialSid,
      keep_count: absoluteKeepCount
    })});
    // #5924 SILENT-race guard: a session switch during the truncate await must not
    // let this recovery apply session A's intent (truncate/re-arm/send) to the
    // newly-visible session.
    if(!S.session || S.session.session_id !== initialSid) return;
    S.messages = S.messages.slice(0, absoluteKeepCount);
    renderMessages();
    $('msg').value = newText;
    // #5924 (Facet 1 + Facet 4): edit-resubmit is a recovery send. Re-arm the
    // Re-arm the single-shot explicit-pick marker from the captured non-default
    // pick — only if still safe at fire time (session unchanged, current model
    // still matches, no newer onchange marker to clobber). See _reArmRecoveryPick.
    _reArmRecoveryPick(initialSid, _recoveryPick);
    await send();
  } catch(e) { setStatus(t('edit_failed') + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const assistantIdx = parseInt(row.dataset.msgIdx, 10);
  const absoluteKeepCount = _oldestIdx + assistantIdx;
  const initialSid = S.session.session_id;
  let lastUserText = '';
  for(let i = assistantIdx - 1; i >= 0; i--) {
    const m = S.messages[i];
    if(m && m.role === 'user') { lastUserText = msgContent(m); break; }
  }
  if(!lastUserText) return;
  if(typeof _ensureAllMessagesLoaded==='function'){
    await _ensureAllMessagesLoaded();
  }
  if(!S.session || S.session.session_id !== initialSid) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: initialSid,
      keep_count: absoluteKeepCount
    })});
    S.messages = S.messages.slice(0, absoluteKeepCount);
    renderMessages();
    $('msg').value = lastUserText;
    await send();
  } catch(e) { setStatus(t('regen_failed') + e.message); }
}

// postProcessRenderedMessages() runs one frame AFTER the render + JS scroll
// restore (it is scheduled via requestAnimationFrame). It performs syntax
// highlighting, inline diff/csv/pdf/html/excalidraw hydration, mermaid/katex
// rendering — all of which can CHANGE the height of rows above the viewport.
//
// On mobile the scroller rests at overflow-anchor:auto, so any above-viewport
// height change in this post-render frame makes the browser's native anchor
// engine compensate scrollTop a SECOND time — after the JS restore already
// settled the reader's position — yanking them to an unrelated turn ("往回大跳").
// The synchronous _fixMobileScrollJank / _suppressBrowserOverflowAnchor guards
// only cover the render frame itself; they have already released by the time
// this rAF fires. Wrap the post-process (and the media-reflow frame right after
// it) in the same suppression so the browser layer cannot re-anchor during the
// async settle window. Desktop rests at `none`, so this is a no-op there.
function _postProcessWithAnchorSuppression(container){
  const scroller=$('messages');
  const release=(scroller&&typeof _suppressBrowserOverflowAnchor==='function')
    ? _suppressBrowserOverflowAnchor(scroller) : null;
  try{
    postProcessRenderedMessages(container);
  }finally{
    // Hold suppression across ONE more frame so late media/layout reflow
    // (image decode, katex/mermaid measure) cannot re-anchor either, then let
    // _suppressBrowserOverflowAnchor's own rAF-deferred restore run.
    if(release){
      if(typeof requestAnimationFrame==='function') requestAnimationFrame(release);
      else release();
    }
  }
}
function postProcessRenderedMessages(container) {
  highlightCode(container);
  addCopyButtons(container);
  loadDiffInline(container);
  loadCsvInline(container);
  loadExcalidrawInline(container);
  loadPdfInline(container);
  loadHtmlInline(container);
  renderMermaidBlocks(container);
  renderKatexBlocks(container);
  initTreeViews(container);
}

function highlightCode(container) {
  // Apply Prism.js syntax highlighting only to *new* code blocks.
  // Previously every renderMessages() called Prism.highlightAllUnder() which
  // re-scanned and re-highlighted every <pre> in the container — expensive in
  // long sessions with dozens of code blocks.  Now we only touch blocks that
  // don't already have the data-highlighted marker.
  if(typeof Prism === 'undefined') return;
  const el = container || $('msgInner');
  if(!el) return;
  // Prefer per-element highlight (avoids the full DOM walk of highlightAllUnder)
  const blocks = el.querySelectorAll('pre code:not([data-highlighted])');
  if(blocks.length === 0) return;
  for(let i = 0; i < blocks.length; i++){
    const block = blocks[i];
    if(typeof Prism.highlightElement === 'function') Prism.highlightElement(block);
    block.dataset.highlighted = '1';
  }
}

// Lazy load js-yaml for YAML tree view support
let _jsyamlLoading=false;
function _loadJsyamlThen(cb){
  if(typeof jsyaml!=='undefined'){ cb(); return; }
  if(_jsyamlLoading){ setTimeout(()=>_loadJsyamlThen(cb),100); return; }
  _jsyamlLoading=true;
  const s=document.createElement('script');
  s.src='static/vendor/js-yaml/4.1.0/js-yaml.min.js';
  s.integrity='sha384-+pxiN6T7yvpryuJmE1gM9PX7yQit15auDb+ZwwvJOd/4be2Cie5/IuVXgQb/S9du';
  s.crossOrigin='anonymous';
  s.onload=()=>{ _jsyamlLoading=false; cb(); };
  s.onerror=()=>{ _jsyamlLoading=false; }; // CDN blocked, fall back to raw
  document.head.appendChild(s);
}

// ── JSON/YAML structured code-block default-view configuration (#484) ──
// Read the user's configured default-view mode for valid JSON/YAML fenced
// blocks. Falls back to 'auto' for any missing/invalid value so the renderer
// stays safe before settings load and in non-browser test contexts.
function _structuredCodeMode(){
  const m=(typeof window!=='undefined')?window._structuredCodeDefaultView:undefined;
  return (m==='on'||m==='off'||m==='auto')?m:'auto';
}
// Read the configured 'auto'-mode line threshold, clamped to a sane integer
// range. Invalid/missing values fall back to 10 (the original hardcoded value).
function _structuredCodeThreshold(){
  const raw=(typeof window!=='undefined')?window._structuredCodeAutoTreeLines:undefined;
  const n=parseInt(raw,10);
  return (Number.isFinite(n)&&n>=1&&n<=1000)?n:10;
}
// Pure decision helper: should a structured block default to Tree view?
// Factored out so the (mode, threshold, lineCount) contract is unit-testable.
//   mode 'on'   => always Tree
//   mode 'off'  => always Raw
//   mode 'auto' => Tree only when lineCount >= threshold (threshold sanitized,
//                  fallback 10)
function _structuredCodeShowTree(mode,threshold,lineCount){
  if(mode==='on') return true;
  if(mode==='off') return false;
  const th=(Number.isFinite(threshold)&&threshold>=1&&threshold<=1000)?threshold:10;
  return lineCount>=th;
}

function initTreeViews(container){
  const root=container||document;
  root.querySelectorAll('.code-tree-wrap:not([data-tree-init])').forEach(wrap=>{
    const rawText=wrap.dataset.raw;
    const lang=wrap.dataset.lang;
    let parsed=null;
    let parseFailed=false;
    // Try JSON parse
    try{ parsed=JSON.parse(rawText); }catch(e){ parseFailed=(lang==='json'); }
    // YAML: lazy-load js-yaml if needed
    if(!parsed && lang==='yaml'){
      if(typeof jsyaml!=='undefined'){
        try{ parsed=jsyaml.load(rawText); }catch(e){ parseFailed=true; }
      }else{
        // Defer: remove init marker so we retry after load.
        // Note: if CDN load fails, s.onerror does NOT call back —
        // the wrap stays un-initialised (raw view only), which is safe.
        wrap.removeAttribute('data-tree-init');
        _loadJsyamlThen(initTreeViews);
        return;
      }
    }
    // Mark as initialised only after we've committed to a render decision
    wrap.setAttribute('data-tree-init','1');
    if(!parsed || typeof parsed!=='object'){
      // No tree view for non-object values or unparseable content. LLMs often
      // emit JSON fragments (a bare "key": "val" line, snippets with ..., etc.)
      // that legitimately fail JSON.parse; surfacing a "parse failed" note for
      // those was pure noise. The block still renders as syntax-highlighted raw,
      // so just fall through silently. (parseFailed is retained for clarity.)
      void parseFailed;
      return; // leave as raw view
    }
    const lineCount=rawText.split('\n').length;
    // Default view is user-configurable (#484 follow-up). 'on' => always Tree,
    // 'off' => always Raw, 'auto' => Tree only when the block is >= the
    // configured line threshold (default 10, preserving the original behavior).
    // The per-block Raw/Tree toggle below always remains available regardless.
    const showTree=_structuredCodeShowTree(_structuredCodeMode(),_structuredCodeThreshold(),lineCount);
    // Build tree DOM
    const treeDiv=document.createElement('div');
    treeDiv.className='tree-view'+(showTree?'':' tree-hidden');
    treeDiv.appendChild(_buildTreeDOM(parsed, 0));
    // Toggle button in header
    const header=wrap.querySelector('.pre-header');
    if(header){
      const toggle=document.createElement('button');
      toggle.className='tree-toggle-btn';
      toggle.textContent=showTree?t('raw_view'):t('tree_view');
      toggle.onclick=(e)=>{
        e.stopPropagation();
        const isTreeHidden=treeDiv.classList.contains('tree-hidden');
        treeDiv.classList.toggle('tree-hidden',!isTreeHidden);
        const rawPre=wrap.querySelector('.tree-raw-view');
        if(rawPre) rawPre.style.display=isTreeHidden?'none':'';
        toggle.textContent=isTreeHidden?t('raw_view'):t('tree_view');
      };
      header.style.display='flex';
      header.style.justifyContent='space-between';
      header.style.alignItems='center';
      header.appendChild(toggle);
    }
    if(!showTree){
      const rawPre=wrap.querySelector('.tree-raw-view');
      if(rawPre) rawPre.style.display='';
    } else {
      const rawPre=wrap.querySelector('.tree-raw-view');
      if(rawPre) rawPre.style.display='none';
    }
    wrap.appendChild(treeDiv);
  });
}

function _buildTreeDOM(val, depth){
  const el=document.createElement('div');
  el.className='tree-node';
  if(val===null){ el.innerHTML=`<span class="tree-val tree-null">null</span>`; return el; }
  if(typeof val==='boolean'){ el.innerHTML=`<span class="tree-val tree-bool">${val}</span>`; return el; }
  if(typeof val==='number'){ el.innerHTML=`<span class="tree-val tree-num">${val}</span>`; return el; }
  if(typeof val==='string'){ el.innerHTML=`<span class="tree-val tree-str">&quot;${esc(val)}&quot;</span>`; return el; }
  if(Array.isArray(val)){
    el.classList.add('tree-array');
    const collapsed=depth>=2;
    const header=document.createElement('span');
    header.className='tree-collapsible';
    header.innerHTML=(collapsed?'▸ ': '▾ ')+`<span class="tree-bracket">[</span><span class="tree-count">${val.length}</span><span class="tree-bracket">]</span>`;
    const body=document.createElement('div');
    body.className='tree-children'+(collapsed?' tree-collapsed':'');
    val.forEach((item,i)=>{
      const child=document.createElement('div');
      child.className='tree-item';
      child.appendChild(_buildTreeDOM(item, depth+1));
      if(i<val.length-1) child.innerHTML+='<span class="tree-comma">,</span>';
      body.appendChild(child);
    });
    el.appendChild(header);
    el.appendChild(body);
    header.onclick=(()=>{const c=body.classList.contains('tree-collapsed'); body.classList.toggle('tree-collapsed'); header.innerHTML=(c?'▾ ':'▸ ')+`<span class="tree-bracket">[</span><span class="tree-count">${val.length}</span><span class="tree-bracket">]</span>`;});
    return el;
  }
  if(typeof val==='object'){
    el.classList.add('tree-object');
    const keys=Object.keys(val);
    const collapsed=depth>=2;
    const header=document.createElement('span');
    header.className='tree-collapsible';
    header.innerHTML=(collapsed?'▸ ': '▾ ')+`<span class="tree-bracket">{</span><span class="tree-count">${keys.length}</span><span class="tree-bracket">}</span>`;
    const body=document.createElement('div');
    body.className='tree-children'+(collapsed?' tree-collapsed':'');
    keys.forEach((key,i)=>{
      const child=document.createElement('div');
      child.className='tree-item';
      child.innerHTML=`<span class="tree-key">&quot;${esc(key)}&quot;</span><span class="tree-colon">: </span>`;
      child.appendChild(_buildTreeDOM(val[key], depth+1));
      if(i<keys.length-1) child.innerHTML+='<span class="tree-comma">,</span>';
      body.appendChild(child);
    });
    el.appendChild(header);
    el.appendChild(body);
    header.onclick=(()=>{const c=body.classList.contains('tree-collapsed'); body.classList.toggle('tree-collapsed'); header.innerHTML=(c?'▾ ':'▸ ')+`<span class="tree-bracket">{</span><span class="tree-count">${keys.length}</span><span class="tree-bracket">}</span>`;});
    return el;
  }
  el.innerHTML=`<span class="tree-val">${esc(String(val))}</span>`;
  return el;
}

function addCopyButtons(container){
  const el=container||$('msgInner');
  if(!el) return;
  el.querySelectorAll('pre > code').forEach(codeEl=>{
    const pre=codeEl.parentElement;
    const header=pre.previousElementSibling;
    if(pre.querySelector('.code-copy-btn')||(header&&header.classList.contains('pre-header')&&header.querySelector('.code-copy-btn'))) return;
    const btn=document.createElement('button');
    btn.className='code-copy-btn';
    btn.textContent=t('copy');
    btn.onclick=(e)=>{
      e.stopPropagation();
      _copyText(codeEl.textContent).then(()=>{
        btn.textContent=t('copied');
        setTimeout(()=>{btn.textContent=t('copy');},1500);
      }).catch(()=>{btn.textContent=t('copy_failed');setTimeout(()=>{btn.textContent=t('copy');},1500);});
    };
    if(header&&header.classList.contains('pre-header')){
      header.style.display='flex';
      header.style.justifyContent='space-between';
      header.style.alignItems='center';
      header.appendChild(btn);
    }else{
      pre.style.position='relative';
      btn.style.cssText='position:absolute;top:6px;right:6px;';
      pre.appendChild(btn);
    }
  });
}

let _mermaidLoading=false;
let _mermaidReady=false;

function loadDiffInline(container){
  const DIFF_MAX_SIZE=512*1024; // 512 KB cap for inline diff rendering
  const root=container||document;
  root.querySelectorAll('.diff-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        if(text.length>DIFF_MAX_SIZE){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('diff_too_large')}</span></div>`;
          return;
        }
        const lines=text.split('\n').map(line=>{
          const e=esc(line);
          if(e.startsWith('@@')) return `<span class="diff-line diff-hunk">${e}</span>`;
          if(e.startsWith('+')) return `<span class="diff-line diff-plus">${e}</span>`;
          if(e.startsWith('-')) return `<span class="diff-line diff-minus">${e}</span>`;
          return `<span class="diff-line">${e}</span>`;
        }).join('\n');
        el.outerHTML=`<div class="diff-inline"><div class="pre-header">${esc(path.split('/').pop())}</div><pre class="diff-block"><code>${lines}</code></pre></div>`;
      })
      .catch(()=>{
        el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('diff_error')}</span></div>`;
      });
  });
}

const CSV_MAX_SIZE=256*1024; // 256 KB cap for inline CSV rendering

function _mediaSessionQuery(){
  const mediaSessionId=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
  return mediaSessionId?'&session_id='+encodeURIComponent(mediaSessionId):'';
}

function _csvMediaUrl(path, opts={}){
  let url='api/media?path='+encodeURIComponent(path)+_mediaSessionQuery();
  if(opts.download) url+='&download=1';
  return url;
}

function buildCsvTablePreview(path, text, downloadUrl=''){
  if(typeof text!=='string') return {errorKey:'csv_error'};
  if(text.length>CSV_MAX_SIZE) return {errorKey:'csv_too_large'};
  const rows=text.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n').filter(r=>r.trim());
  if(rows.length<2) return {errorKey:'csv_no_data'};
  // Auto-detect separator (comma, semicolon, tab)
  // Heuristic: uses the first separator found in the header row. Edge case:
  // quoted fields containing commas without non-quoted commas in the header
  // could cause misdetection — acceptable trade-off for a preview renderer.
  const firstLine=rows[0];
  const separators=[',',';','\t'];
  const sep=separators.find(s=>firstLine.includes(s))||',';
  const headers=rows[0].split(sep).map(c=>c.trim().replace(/^["']|["']$/g,''));
  const bodyRows=rows.slice(1).map(r=>'<tr>'+r.split(sep).map(c=>`<td>${esc(c.trim().replace(/^["']|["']$/g,''))}</td>`).join('')+'</tr>').join('');
  const headerRow=headers.map(h=>`<th>${esc(h)}</th>`).join('');
  const fname=path.split('/').pop()||path;
  const downloadLink=downloadUrl
    ? `<a class="csv-download-link msg-media-link" href="${esc(downloadUrl)}" download="${esc(fname)}">📎 ${esc(fname)}</a>`
    : '';
  return {
    html:`<div class="csv-table-wrap"><div class="pre-header csv-preview-header"><span class="csv-preview-title">${esc(fname)} <span style="opacity:.5;font-size:11px">${t('csv_header_note')}</span></span>${downloadLink}</div><table class="csv-table"><thead><tr>${headerRow}</tr></thead><tbody>${bodyRows}</tbody></table></div>`,
  };
}

function _csvPreviewErrorHtml(path, errorKey){
  const fname=path.split('/').pop()||path;
  const downloadUrl=_csvMediaUrl(path,{download:true});
  return `<div class="diff-inline-error">${esc(fname)}<br><a class="msg-media-link" href="${esc(downloadUrl)}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t(errorKey)}</span></div>`;
}

function loadCsvInline(container){
  const root=container||document;
  root.querySelectorAll('.csv-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const mediaUrl=_csvMediaUrl(path);
    const downloadUrl=_csvMediaUrl(path,{download:true});
    fetch(mediaUrl)
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        const preview=buildCsvTablePreview(path, text, downloadUrl);
        el.outerHTML=preview.html||_csvPreviewErrorHtml(path, preview.errorKey||'csv_error');
      })
      .catch(()=>{
        el.outerHTML=_csvPreviewErrorHtml(path, 'csv_error');
      });
  });
}

function loadExcalidrawInline(container){
  const EXCALIDRAW_MAX_SIZE=512*1024; // 512 KB cap
  const root=container||document;
  root.querySelectorAll('.excalidraw-inline-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    fetch('api/media?path='+encodeURIComponent(path))
      .then(r=>{if(!r.ok) throw new Error(r.status);return r.text();})
      .then(text=>{
        if(text.length>EXCALIDRAW_MAX_SIZE){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_too_large')}</span></div>`;
          return;
        }
        // Validate it looks like Excalidraw JSON
        let data;
        try{data=JSON.parse(text);}catch(e){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_invalid')}</span></div>`;
          return;
        }
        if(!data.type||data.type!=='excalidraw'){
          el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_invalid')}</span></div>`;
          return;
        }
        const fname=esc(path.split('/').pop());
        const downloadUrl='api/media?path='+encodeURIComponent(path)+'&download=1';
        el.outerHTML=`<div class="excalidraw-embed-wrap" title="${t('excalidraw_simplified')}">
  <div class="msg-artifact-header">
    <span class="msg-media-label">${t('excalidraw_label')}</span>
    <a class="excalidraw-open-link" href="${downloadUrl}" download="${fname}">${t('excalidraw_download')} ${fname}</a>
  </div>
  <div class="excalidraw-canvas" data-excalidraw='${esc(text)}'></div>
</div>`;
        // Lazy-init Excalidraw render after DOM insertion
        requestAnimationFrame(()=>_renderExcalidrawCanvases());
      })
      .catch(()=>{
        el.outerHTML=`<div class="diff-inline-error">${esc(path.split('/').pop())}<br><span style="color:var(--muted);font-size:12px">${t('excalidraw_error')}</span></div>`;
      });
  });
}

let _excalidrawScriptLoaded=false;
function _renderExcalidrawCanvases(){
  document.querySelectorAll('.excalidraw-canvas:not([data-rendered])').forEach(el=>{
    el.setAttribute('data-rendered','1');
    const dataStr=el.getAttribute('data-excalidraw');
    if(!dataStr) return;
    // Render a simple SVG preview using the Excalidraw elements
    try{
      const data=JSON.parse(dataStr);
      const elements=data.elements||[];
      if(!elements.length){el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_empty')}</div>`;return;}
      // Calculate bounds
      let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
      elements.forEach(el=>{
        const b=[el.x||0,el.y||0,(el.x||0)+(el.width||0),(el.y||0)+(el.height||0)];
        minX=Math.min(minX,b[0]);minY=Math.min(minY,b[1]);
        maxX=Math.max(maxX,b[2]);maxY=Math.max(maxY,b[3]);
      });
      const pad=20;minX-=pad;minY-=pad;maxX+=pad;maxY+=pad;
      const w=Math.max(maxX-minX,200);const h=Math.max(maxY-minY,150);
      // SVG attributes are rendered via innerHTML below, so attacker-controlled
      // values from JSON (e.g. strokeColor='red"/><script>...') would break out
      // of the attribute. Escape strings; coerce numerics.
      const _sa=v=>String(v==null?'':v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const _num=(v,fb)=>{const n=Number(v);return Number.isFinite(n)?n:fb;};
      const svgParts=[`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${_num(minX,0)} ${_num(minY,0)} ${_num(w,200)} ${_num(h,150)}" class="excalidraw-svg">`];
      elements.forEach(el=>{
        const stroke=_sa(el.strokeColor||'#1e1e1e');
        const fill=_sa(el.backgroundColor||'transparent');
        const sw=_num(el.strokeWidth,2);
        const x=_num(el.x,0),y=_num(el.y,0),w=_num(el.width,0),h=_num(el.height,0);
        if(el.type==='rectangle'){
          svgParts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}" rx="${el.roundness?.type===3?8:0}"/>`);
        }else if(el.type==='diamond'){
          const cx=x+w/2,cy=y+h/2;
          svgParts.push(`<polygon points="${cx},${y} ${x+w},${cy} ${cx},${y+h} ${x},${cy}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`);
        }else if(el.type==='ellipse'){
          svgParts.push(`<ellipse cx="${x+w/2}" cy="${y+h/2}" rx="${w/2}" ry="${h/2}" stroke="${stroke}" stroke-width="${sw}" fill="${fill}"/>`);
        }else if(el.type==='line'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(!pts.length) return;
          let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
          for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
          svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
        }else if(el.type==='arrow'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(!pts.length) return;
          let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
          for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
          svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#arrowhead)"/>`);
        }else if(el.type==='text'){
          const fontSize=_num(el.fontSize,20);
          const txt=String(el.text==null?'':el.text);
          const lines=txt.split('\n');
          lines.forEach((line,i)=>{
            svgParts.push(`<text x="${x}" y="${y+i*fontSize*1.2+fontSize}" fill="${stroke}" font-size="${fontSize}" font-family="Virgil, Segoe UI Emoji, sans-serif">${esc(line)}</text>`);
          });
        }else if(el.type==='draw'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(pts.length>1){
            let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
            for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
            svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
          }
        }
        // Unknown element types (e.g. image, frame, group, freedraw) are
        // silently skipped to avoid breaking the render. This is a simplified
        // SVG preview, not a pixel-identical Excalidraw canvas reproduction.
      });
      // Arrow marker definition
      svgParts.unshift(`<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#1e1e1e"/></marker></defs>`);
      svgParts.push('</svg>');
      el.innerHTML=svgParts.join('');
    }catch(e){
      el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_render_error')}</div>`;
    }
  });
}

// ── PDF inline preview (first page) ────────────────────────────────────────
// NOTE: PDF.js is loaded from CDN (jsdelivr). Offline/air-gapped deployments
// will not get inline previews; the 15 s fallback timeout degrades to a
// download link in that case. The 4 MB size cap is checked client-side after
// the full buffer is received — ideally the server would enforce it before
// streaming (out of scope for this client-side PR).
let _pdfjsReady=false, _pdfjsLoading=false;
function loadPdfInline(container){
  const PDF_MAX_SIZE=4*1024*1024; // 4 MB cap for inline PDF preview
  const root=container||document;
  root.querySelectorAll('.pdf-preview-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const fname=path.split('/').pop()||path;
    const mediaSessionId=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
    const publicMediaUrl='api/media?path='+encodeURIComponent(path);
    const mediaUrl=publicMediaUrl+(mediaSessionId?'&session_id='+encodeURIComponent(mediaSessionId):'');
    const loadPdf=(pdfjsLib)=>{
      fetch(mediaUrl)
        .then(r=>{if(!r.ok) throw new Error(r.status); return r.arrayBuffer();})
        .then(buf=>{
          if(buf.byteLength>PDF_MAX_SIZE){
            const dlUrl=publicMediaUrl+'&download=1';
            el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_too_large')}</span></div>`;
            return;
          }
          return pdfjsLib.getDocument({data:buf, isEvalSupported:false}).promise;
        })
        .then(pdf=>{
          if(!pdf) return;
          const dlUrl=publicMediaUrl+'&download=1';
          const total=pdf.numPages;
          const pagesLabel=total>1?` · ${total} pages`:'';
          const wrap=document.createElement('div');
          wrap.className='pdf-preview-wrap';
          wrap.innerHTML=`<div class="pdf-preview-header"><span>📄 ${esc(fname)}${pagesLabel}</span><a href="${dlUrl}" download="${esc(fname)}" class="pdf-download-link">${t('pdf_download')} ↓</a></div><div class="pdf-preview-body"></div>`;
          const body=wrap.querySelector('.pdf-preview-body');
          el.replaceWith(wrap);
          // Render every page (capped) sequentially to limit memory; the
          // canvases stack vertically in the scrollable preview body.
          const MAX_PAGES=20;
          const n=Math.min(total,MAX_PAGES);
          if(total>MAX_PAGES){
            const notice=document.createElement('div');
            notice.className='pdf-preview-truncated';
            notice.textContent=t('pdf_truncated',MAX_PAGES,total);
            body.appendChild(notice);
          }
          // On a per-page failure, skip that page and continue so one malformed
          // page can't silently halt the preview or surface an unhandled
          // promise rejection (renderPage runs outside the outer .catch chain).
          const renderPage=(i)=>{
            if(i>n) return;
            pdf.getPage(i).then(page=>{
              const canvas=document.createElement('canvas');
              const scale=1.5;
              const viewport=page.getViewport({scale});
              canvas.width=viewport.width;
              canvas.height=viewport.height;
              canvas.className='pdf-preview-canvas';
              // Attach only after a successful render, so a render rejection
              // (corrupt page data, null 2d context) can't leave a blank canvas
              // behind — the .catch then simply skips to the next page.
              return page.render({canvasContext:canvas.getContext('2d'),viewport}).promise.then(()=>{ body.appendChild(canvas); });
            }).then(()=>renderPage(i+1)).catch(()=>renderPage(i+1));
          };
          renderPage(1);
        })
        .catch(()=>{
          const dlUrl=publicMediaUrl+'&download=1';
          el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_error')}</span></div>`;
        });
    };
    if(_pdfjsReady){
      loadPdf(window._pdfjsLib);
    } else if(!_pdfjsLoading){
      _pdfjsLoading=true;
      const _pdfSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.min.mjs';
      const _pdfWorker='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.9.155/build/pdf.worker.min.mjs';
      const _pdfBlob=new Blob([`import*as p from'${_pdfSrc}';p.GlobalWorkerOptions.workerSrc='${_pdfWorker}';window._pdfjsLib=p;window._pdfjsReady=true;window.dispatchEvent(new Event('pdfjs-ready'));`],{type:'application/javascript'});
      const s=document.createElement('script');
      s.type='module';
      const _pdfBlobUrl=URL.createObjectURL(_pdfBlob);
      s.src=_pdfBlobUrl;
      s.onload=()=>URL.revokeObjectURL(_pdfBlobUrl);
      document.head.appendChild(s);
      window.addEventListener('pdfjs-ready',()=>{ _pdfjsReady=true; loadPdf(window._pdfjsLib); },{once:true});
      setTimeout(()=>{
        if(!_pdfjsReady){
          const dlUrl=publicMediaUrl+'&download=1';
          if(el.parentNode){
            el.outerHTML=`<div class="pdf-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('pdf_error')}</span></div>`;
          }
        }
      },15000);
    } else {
      window.addEventListener('pdfjs-ready',()=>{ loadPdf(window._pdfjsLib); },{once:true});
    }
  });
}

// ── HTML inline preview (sandboxed iframe) ─────────────────────────────────
function loadHtmlInline(container){
  const HTML_MAX_SIZE=256*1024; // 256 KB cap for inline HTML preview
  const root=container||document;
  root.querySelectorAll('.html-preview-load:not([data-loaded])').forEach(el=>{
    el.setAttribute('data-loaded','1');
    const path=el.dataset.path;
    const fname=path.split('/').pop()||path;
    const mediaSessionId=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
    const publicMediaUrl='api/media?path='+encodeURIComponent(path);
    const mediaUrl=publicMediaUrl+(mediaSessionId?'&session_id='+encodeURIComponent(mediaSessionId):'');
    fetch(mediaUrl)
      .then(r=>{if(!r.ok) throw new Error(r.status); return r.text();})
      .then(html=>{
        if(html.length>HTML_MAX_SIZE){
          const openUrl=publicMediaUrl+'&inline=1';
          el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${openUrl}" target="_blank" rel="noopener">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_too_large')}</span></div>`;
          return;
        }
        const openUrl=publicMediaUrl+'&inline=1';
        const safeHtml=html.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        el.outerHTML=`<div class="html-preview-wrap"><div class="html-preview-header"><span>${t('html_sandbox_label')}</span><a href="${openUrl}" target="_blank" rel="noopener" class="html-open-link">${t('html_open_full')} ↗</a></div><iframe srcdoc="${safeHtml}" sandbox="allow-scripts" class="html-preview-iframe" loading="lazy"></iframe></div>`;
      })
      .catch(()=>{
        const dlUrl=publicMediaUrl+'&download=1';
        el.outerHTML=`<div class="html-preview-fallback"><a class="msg-media-link" href="${dlUrl}" download="${esc(fname)}">📎 ${esc(fname)}</a><br><span style="color:var(--muted);font-size:12px">${t('html_error')}</span></div>`;
      });
  });
}

function renderMermaidBlocks(container){
  const root=container||document;
  const blocks=root.querySelectorAll('.mermaid-block:not([data-rendered])');
  if(!blocks.length) return;
  if(!_mermaidReady){
    if(!_mermaidLoading){
      _mermaidLoading=true;
      const script=document.createElement('script');
      script.src='https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js';
      script.integrity='sha384-R63zfMfSwJF4xCR11wXii+QUsbiBIdiDzDbtxia72oGWfkT7WHJfmD/I/eeHPJyT';
      script.crossOrigin='anonymous';
      script.onload=()=>{
        if(typeof mermaid!=='undefined'){
          mermaid.initialize({startOnLoad:false,theme:document.documentElement.classList.contains('dark')?'dark':'default',themeVariables:{
            fontFamily:'inherit',fontSize:'14px',
            primaryColor:'#4a6fa5',primaryTextColor:'#e2e8f0',lineColor:'#718096',
            secondaryColor:'#2d3748',tertiaryColor:'#1a202c',primaryBorderColor:'#4a5568',
          }});
          _mermaidReady=true;
          renderMermaidBlocks();
        }
      };
      document.head.appendChild(script);
    }
    return;
  }
  blocks.forEach(async(block)=>{
    block.dataset.rendered='true';
    const code=block.textContent;
    const id=block.dataset.mermaidId||('m-'+Math.random().toString(36).slice(2));
    try{
      const {svg}=await mermaid.render(id,code);
      const tmp=document.getElementById('d'+id);
      if(tmp) tmp.remove();
      block.innerHTML=svg;
      const renderedSvg = block.querySelector('svg');
      if(renderedSvg) _mountMermaidViewer(renderedSvg, {mode:'inline'});
      block.classList.add('mermaid-rendered');
    }catch(e){
      const tmp=document.getElementById('d'+id);
      if(tmp) tmp.remove();
      // Fall back to showing as a code block. Remove the mermaid marker so a
      // later render pass cannot retry this already-failed block.
      block.classList.remove('mermaid-block');
      block.classList.add('prewrap');
      block.innerHTML=`<div class="pre-header">mermaid</div><pre><code>${esc(code)}</code></pre>`;
    }
  });
}

let _katexLoading=false;
let _katexReady=false;

function _isStreamingEquationPending(el,root){
  const tagName=(el&&el.tagName||'').toLowerCase();
  if(tagName!=='equation-block'&&tagName!=='equation-inline') return false;
  // streaming-markdown fills custom equation elements while the parser owns the
  // open node. If the equation is currently the last descendant of the live
  // assistant body, we cannot tell whether more TeX is still coming. Skip it
  // during live debounce passes so a partial source is not permanently marked
  // data-rendered before the final parser_end flush.
  let node=el;
  while(node&&node!==root){
    if(node.nextSibling) return false;
    node=node.parentNode;
  }
  return Boolean(node===root);
}

function renderKatexBlocks(container,options){
  const root=container||document;
  const streaming=Boolean(options&&options.streaming);
  const blocks=root.querySelectorAll(
    '.katex-block:not([data-rendered]),.katex-inline:not([data-rendered]),'+
    'equation-block:not([data-rendered]),equation-inline:not([data-rendered])'
  );
  if(!blocks.length) return;
  if(!_katexReady){
    if(!_katexLoading){
      _katexLoading=true;
      const script=document.createElement('script');
      script.src='static/vendor/katex/0.16.22/katex.min.js';
      script.integrity='sha384-cMkvdD8LoxVzGF/RPUKAcvmm49FQ0oxwDF3BGKtDXcEc+T1b2N+teh/OJfpU0jr6';
      script.crossOrigin='anonymous';
      script.onload=()=>{
        if(typeof katex!=='undefined'){
          _katexReady=true;
          renderKatexBlocks();
        }
      };
      document.head.appendChild(script);
    }
    return;
  }
  blocks.forEach(el=>{
    if(streaming&&_isStreamingEquationPending(el,root)) return;
    el.dataset.rendered='true';
    const src=el.textContent||'';
    const tagName=(el.tagName||'').toLowerCase();
    const displayMode=el.dataset.katex==='display'||tagName==='equation-block';
    try{
      katex.render(src,el,{
        displayMode,
        throwOnError:false,
        trust:false,
        strict:'ignore',
      });
    }catch(e){
      // Leave as raw text in a code span on failure
      el.outerHTML=`<code>${esc(src)}</code>`;
    }
  });
}

function _thinkingMarkup(text=''){
  const clean=_sanitizeThinkingDisplayText(text);
  const openClass=_worklogDetailsExpandedDefault()?' open':'';
  return (clean&&String(clean).trim())
    ? `<div class="thinking-card${openClass}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-toggle">${li('chevron-right',12)}</span></div><div class="thinking-card-body"><pre>${esc(String(clean).trim())}</pre></div></div>`
    : `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
}
function _renderThinkingInto(row,text=''){
  if(!row) return;
  const clean=_sanitizeThinkingDisplayText(text);
  if(!clean){
    row.innerHTML=_thinkingMarkup(text);
    return;
  }
  const pre=row.querySelector('.thinking-card-body pre');
  if(pre){
    pre.textContent=clean;
    return;
  }
  row.innerHTML=_thinkingMarkup(text);
}
function finalizeThinkingCard(){
  // Guard: only finalize thinking card if we're looking at the session that started it.
  // Without this check, switching tabs while a stream is running causes finalizeThinkingCard
  // to remove/modify the thinking card DOM of the wrong session — the card belongs to the
  // stream that started it, not the session currently displayed.
  const _guardTurn = $('liveAssistantTurn');
  if(_guardTurn && S.session && _guardTurn.dataset.sessionId !== S.session.session_id) return;
  if(isTransparentStream()){
    const row=$('thinkingRow');
    if(row){
      row.removeAttribute('id');
      row.removeAttribute('data-thinking-active');
      row.removeAttribute('data-live-thinking');
    }
    return;
  }
  if(!isSimplifiedToolCalling()){
    const row=$('thinkingRow');
    if(!row) return;
    // If the row is still just a spinner (no thinking content rendered),
    // remove it entirely — it's the initial waiting dots.
    const hasContent=!!row.querySelector('.thinking-card');
    if(!hasContent && row.getAttribute('data-thinking-active')==='1'){
      row.remove();
      return;
    }
    // If the user was watching (scroll pinned = at bottom), scroll the thinking
    // card back to the top so the completed response is visible underneath without
    // the thinking content blocking it. If they scrolled up to read history,
    // leave their scroll position intact.
    if(_scrollPinned){
      const body=row&&row.querySelector('.thinking-card-body');
      if(body) body.scrollTop=0;
    }
    row.removeAttribute('id');
    row.removeAttribute('data-thinking-active');
    return;
  }
  const turn=$('liveAssistantTurn');
  const group=turn&&turn.querySelector('.live-worklog[data-live-tool-call-group="1"],.tool-worklog-group[data-live-tool-call-group="1"],.tool-call-group[data-live-tool-call-group="1"]');
  if(group){
    const activeReason=turn.querySelector('.wl-reason[data-worklog-reason-active="1"]');
    if(activeReason) activeReason.removeAttribute('data-worklog-reason-active');
    turn.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(active=>{
      active.removeAttribute('data-thinking-active');
      active.removeAttribute('data-live-thinking');
    });
    _syncToolCallGroupSummary(group);
  }
}
function appendThinking(text='', options){
  // Guard: ignore if session was switched during an async SSE stream.
  // The old stream's reasoning events can still fire after switch;
  // without this check they would pollute the new session's DOM.
  options=options||{};
  const allowPendingPlaceholder=!!(options&&options.pending===true);
  const anchorRenderFallback=!!(options&&options.anchorRenderFallback===true);
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return;
  if(!S.session||(!S.activeStreamId&&!allowPendingPlaceholder)) return;
  if(options.sessionId&&String(options.sessionId)!==String(S.session.session_id||'')) return;
  if(options.streamId&&String(options.streamId)!==String(S.activeStreamId||'')) return;
  const existingLiveTurn=$('liveAssistantTurn');
  if(anchorRenderFallback&&existingLiveTurn&&existingLiveTurn.dataset&&
      existingLiveTurn.dataset.sessionId&&
      String(existingLiveTurn.dataset.sessionId)!==String(S.session.session_id||'')){
    if(!_resetMismatchedLiveAssistantTurnForSession(existingLiveTurn, S.session.session_id)) return;
  }
  if(anchorRenderFallback&&existingLiveTurn&&_updateLiveAnchorReasoningRowForFallback(existingLiveTurn,text,options)) return;
  if(!allowPendingPlaceholder&&!anchorRenderFallback&&isLiveAnchorActivitySceneOwner(S.activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(S.activeStreamId, S.session.session_id);
    return;
  }
  const empty=$('emptyState');
  if(empty) empty.style.display='none';
  if(!isSimplifiedToolCalling()){
    let row=$('thinkingRow');
    if(!row){
      row=document.createElement('div');
      row.id='thinkingRow';
      row.className='thinking-card-row';
      const inner=$('msgInner');
      if(inner) inner.appendChild(row);
    }
    row.setAttribute('data-thinking-active','1');
    _renderThinkingInto(row,text);
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return;
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    const inner=$('msgInner');
    if(inner) inner.appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const clean=_sanitizeThinkingDisplayText(text);
  if(clean&&window._showThinking!==false){
    const segmentSeq=options.segmentSeq!==undefined&&options.segmentSeq!==null?String(options.segmentSeq):'';
    const burstId=options.burstId!==undefined&&options.burstId!==null?String(options.burstId):'';
    const thinkingKey=String(options.thinkingKey||(
      segmentSeq?`segment:${segmentSeq}`:
      burstId?`burst:${burstId}`:
      'turn'
    ));
    if(isTransparentStream()){
      let row=blocks.querySelector(`.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="${CSS.escape(thinkingKey)}"]`);
      if(!row){
        row=_thinkingActivityNode(clean, false);
        row.id='thinkingRow';
        row.setAttribute('data-live-thinking','1');
        row.setAttribute('data-live-thinking-key',thinkingKey);
        if(segmentSeq) row.setAttribute('data-live-segment-seq',segmentSeq);
        if(burstId) row.setAttribute('data-activity-burst-id',burstId);
        blocks.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(el=>{
          if(el!==row){
            el.removeAttribute('id');
            el.removeAttribute('data-thinking-active');
            el.removeAttribute('data-live-thinking');
          }
        });
        row.setAttribute('data-thinking-active','1');
        const liveFooter=blocks.querySelector('#liveRunStatus');
        if(liveFooter&&liveFooter.parentElement===blocks) blocks.insertBefore(row,liveFooter);
        else blocks.appendChild(row);
      }else{
        _renderThinkingInto(row, clean);
      }
      row.id='thinkingRow';
      row.setAttribute('data-thinking-active','1');
      const existingEventAt=row.getAttribute('data-event-at');
      const nextTs=_firstValidTimestampSeconds(
        options.ts,
        options.timestamp,
        options.created_at,
        existingEventAt
      );
      _decorateTransparentEventRow(row,{
        type:'thinking',
        text:clean,
        preview:clean,
        ts:nextTs||undefined,
        live:true,
        segmentSeq,
        burstId,
      });
      _syncTransparentEventControls(turn);
      if(typeof scrollIfPinned==='function') scrollIfPinned();
      return;
    }
    const group=ensureLiveWorklogContainer(blocks,{
      activityKey:options.activityKey||(S.activeStreamId?'live:'+S.activeStreamId:null),
    });
    const list=_toolWorklogListEl(group);
    if(list){
      let row=list.querySelector(`.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="${CSS.escape(thinkingKey)}"]`);
      if(!row){
        row=_thinkingActivityNode(clean, false, thinkingKey);
        row.setAttribute('data-live-thinking','1');
        row.setAttribute('data-live-thinking-key',thinkingKey);
        if(segmentSeq) row.setAttribute('data-live-segment-seq',segmentSeq);
        if(burstId) row.setAttribute('data-activity-burst-id',burstId);
        list.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(el=>{
          if(el!==row){
            el.removeAttribute('data-thinking-active');
            el.removeAttribute('data-live-thinking');
          }
        });
        row.setAttribute('data-thinking-active','1');
        list.appendChild(row);
      }else{
        _renderThinkingInto(row, clean);
      }
      row.setAttribute('data-thinking-active','1');
      _syncToolCallGroupSummary(group);
    }
  }
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}
function updateThinking(text='', options){appendThinking(text, options);}
function removeThinking(){
  if(isTransparentStream()){
    const liveTurn=$('liveAssistantTurn');
    const blocks=_assistantTurnBlocks(liveTurn);
    if(blocks) blocks.querySelectorAll('.agent-activity-thinking[data-thinking-active="1"]').forEach(row=>{
      row.removeAttribute('id');
      row.removeAttribute('data-thinking-active');
      row.removeAttribute('data-live-thinking');
    });
    if(liveTurn&&blocks&&!blocks.children.length) liveTurn.remove();
    return;
  }
  if(!isSimplifiedToolCalling()){
    const el=$('thinkingRow');
    if(el) el.remove();
    const liveTurn=$('liveAssistantTurn');
    const blocks=_assistantTurnBlocks(liveTurn);
    if(liveTurn&&blocks&&!blocks.children.length) liveTurn.remove();
    return;
  }
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
  if(blocks) blocks.querySelectorAll('.agent-activity-thinking:not([data-anchor-scene-row="1"])').forEach(el=>el.remove());
  if(blocks) blocks.querySelectorAll('.wl-reason[data-worklog-anchor-reason="1"],.wl-reason[data-worklog-reason-source="reasoning"]').forEach(el=>el.remove());
  if(blocks) blocks.querySelectorAll('.live-worklog[data-live-worklog-shell="1"],.tool-worklog-group[data-live-tool-call-group="1"]:not([data-anchor-scene-owner="1"]),.tool-call-group[data-live-tool-call-group="1"]:not([data-anchor-scene-owner="1"]),.tool-call-group[data-agent-activity-group="1"]:not([data-anchor-scene-owner="1"])').forEach(group=>{
    _syncToolCallGroupSummary(group);
    if(!group.querySelector('.tool-card-row,.agent-activity-thinking,.wl-reason')){
      if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
      group.remove();
    }
  });
  if(turn&&blocks&&!blocks.children.length) turn.remove();
}



export {
  editMessage,
  cancelEdit,
  autoResizeTextarea,
  _postProcessWithAnchorSuppression,
  postProcessRenderedMessages,
  highlightCode,
  _loadJsyamlThen,
  _structuredCodeMode,
  _structuredCodeThreshold,
  _structuredCodeShowTree,
  initTreeViews,
  _buildTreeDOM,
  addCopyButtons,
  loadDiffInline,
  _mediaSessionQuery,
  _csvMediaUrl,
  buildCsvTablePreview,
  _csvPreviewErrorHtml,
  loadCsvInline,
  loadExcalidrawInline,
  _renderExcalidrawCanvases,
  loadPdfInline,
  loadHtmlInline,
  renderMermaidBlocks,
  _isStreamingEquationPending,
  renderKatexBlocks,
  _thinkingMarkup,
  _renderThinkingInto,
  finalizeThinkingCard,
  appendThinking,
  updateThinking,
  removeThinking,
  submitEdit,
  regenerateResponse,
  CSV_MAX_SIZE,
  _jsyamlLoading,
  _mermaidLoading,
  _mermaidReady,
  _excalidrawScriptLoaded,
  _pdfjsReady,
  _katexLoading,
  _katexReady,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  editMessage: { enumerable: true, get: () => editMessage, set: (value) => { editMessage = value; } },
  cancelEdit: { enumerable: true, get: () => cancelEdit, set: (value) => { cancelEdit = value; } },
  autoResizeTextarea: { enumerable: true, get: () => autoResizeTextarea, set: (value) => { autoResizeTextarea = value; } },
  _postProcessWithAnchorSuppression: { enumerable: true, get: () => _postProcessWithAnchorSuppression, set: (value) => { _postProcessWithAnchorSuppression = value; } },
  postProcessRenderedMessages: { enumerable: true, get: () => postProcessRenderedMessages, set: (value) => { postProcessRenderedMessages = value; } },
  highlightCode: { enumerable: true, get: () => highlightCode, set: (value) => { highlightCode = value; } },
  _loadJsyamlThen: { enumerable: true, get: () => _loadJsyamlThen, set: (value) => { _loadJsyamlThen = value; } },
  _structuredCodeMode: { enumerable: true, get: () => _structuredCodeMode, set: (value) => { _structuredCodeMode = value; } },
  _structuredCodeThreshold: { enumerable: true, get: () => _structuredCodeThreshold, set: (value) => { _structuredCodeThreshold = value; } },
  _structuredCodeShowTree: { enumerable: true, get: () => _structuredCodeShowTree, set: (value) => { _structuredCodeShowTree = value; } },
  initTreeViews: { enumerable: true, get: () => initTreeViews, set: (value) => { initTreeViews = value; } },
  _buildTreeDOM: { enumerable: true, get: () => _buildTreeDOM, set: (value) => { _buildTreeDOM = value; } },
  addCopyButtons: { enumerable: true, get: () => addCopyButtons, set: (value) => { addCopyButtons = value; } },
  loadDiffInline: { enumerable: true, get: () => loadDiffInline, set: (value) => { loadDiffInline = value; } },
  _mediaSessionQuery: { enumerable: true, get: () => _mediaSessionQuery, set: (value) => { _mediaSessionQuery = value; } },
  _csvMediaUrl: { enumerable: true, get: () => _csvMediaUrl, set: (value) => { _csvMediaUrl = value; } },
  buildCsvTablePreview: { enumerable: true, get: () => buildCsvTablePreview, set: (value) => { buildCsvTablePreview = value; } },
  _csvPreviewErrorHtml: { enumerable: true, get: () => _csvPreviewErrorHtml, set: (value) => { _csvPreviewErrorHtml = value; } },
  loadCsvInline: { enumerable: true, get: () => loadCsvInline, set: (value) => { loadCsvInline = value; } },
  loadExcalidrawInline: { enumerable: true, get: () => loadExcalidrawInline, set: (value) => { loadExcalidrawInline = value; } },
  _renderExcalidrawCanvases: { enumerable: true, get: () => _renderExcalidrawCanvases, set: (value) => { _renderExcalidrawCanvases = value; } },
  loadPdfInline: { enumerable: true, get: () => loadPdfInline, set: (value) => { loadPdfInline = value; } },
  loadHtmlInline: { enumerable: true, get: () => loadHtmlInline, set: (value) => { loadHtmlInline = value; } },
  renderMermaidBlocks: { enumerable: true, get: () => renderMermaidBlocks, set: (value) => { renderMermaidBlocks = value; } },
  _isStreamingEquationPending: { enumerable: true, get: () => _isStreamingEquationPending, set: (value) => { _isStreamingEquationPending = value; } },
  renderKatexBlocks: { enumerable: true, get: () => renderKatexBlocks, set: (value) => { renderKatexBlocks = value; } },
  _thinkingMarkup: { enumerable: true, get: () => _thinkingMarkup, set: (value) => { _thinkingMarkup = value; } },
  _renderThinkingInto: { enumerable: true, get: () => _renderThinkingInto, set: (value) => { _renderThinkingInto = value; } },
  finalizeThinkingCard: { enumerable: true, get: () => finalizeThinkingCard, set: (value) => { finalizeThinkingCard = value; } },
  appendThinking: { enumerable: true, get: () => appendThinking, set: (value) => { appendThinking = value; } },
  updateThinking: { enumerable: true, get: () => updateThinking, set: (value) => { updateThinking = value; } },
  removeThinking: { enumerable: true, get: () => removeThinking, set: (value) => { removeThinking = value; } },
  submitEdit: { enumerable: true, get: () => submitEdit, set: (value) => { submitEdit = value; } },
  regenerateResponse: { enumerable: true, get: () => regenerateResponse, set: (value) => { regenerateResponse = value; } },
  CSV_MAX_SIZE: { enumerable: true, get: () => CSV_MAX_SIZE },
  _jsyamlLoading: { enumerable: true, get: () => _jsyamlLoading, set: (value) => { _jsyamlLoading = value; } },
  _mermaidLoading: { enumerable: true, get: () => _mermaidLoading, set: (value) => { _mermaidLoading = value; } },
  _mermaidReady: { enumerable: true, get: () => _mermaidReady, set: (value) => { _mermaidReady = value; } },
  _excalidrawScriptLoaded: { enumerable: true, get: () => _excalidrawScriptLoaded, set: (value) => { _excalidrawScriptLoaded = value; } },
  _pdfjsReady: { enumerable: true, get: () => _pdfjsReady, set: (value) => { _pdfjsReady = value; } },
  _katexLoading: { enumerable: true, get: () => _katexLoading, set: (value) => { _katexLoading = value; } },
  _katexReady: { enumerable: true, get: () => _katexReady, set: (value) => { _katexReady = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
