import { _copyText } from './clipboard.js';
import { $, esc } from './state.js';

function highlightCode(container) {
  // Apply Prism.js syntax highlighting only to *new* code blocks.
  // Previously every renderMessages() called Prism.highlightAllUnder() which
  // re-scanned and re-highlighted every <pre> in the container — expensive in
  // long sessions with dozens of code blocks. Now only unmarked blocks cross
  // the syntax-highlighting seam.
  if(typeof Prism === 'undefined') return;
  const el = container || $('msgInner');
  if(!el) return;
  const blocks = el.querySelectorAll('pre code:not([data-highlighted])');
  if(blocks.length === 0) return;
  for(let i = 0; i < blocks.length; i++){
    const block = blocks[i];
    if(typeof Prism.highlightElement === 'function') Prism.highlightElement(block);
    block.dataset.highlighted = '1';
  }
}

// Lazy load js-yaml for YAML tree view support.
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
  s.onerror=()=>{ _jsyamlLoading=false; }; // blocked: retain the raw view
  document.head.appendChild(s);
}

function _structuredCodeMode(){
  const m=(typeof window!=='undefined')?window._structuredCodeDefaultView:undefined;
  return (m==='on'||m==='off'||m==='auto')?m:'auto';
}

function _structuredCodeThreshold(){
  const raw=(typeof window!=='undefined')?window._structuredCodeAutoTreeLines:undefined;
  const n=parseInt(raw,10);
  return (Number.isFinite(n)&&n>=1&&n<=1000)?n:10;
}

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
    try{ parsed=JSON.parse(rawText); }catch(e){ parseFailed=(lang==='json'); }
    if(!parsed && lang==='yaml'){
      if(typeof jsyaml!=='undefined'){
        try{ parsed=jsyaml.load(rawText); }catch(e){ parseFailed=true; }
      }else{
        wrap.removeAttribute('data-tree-init');
        _loadJsyamlThen(initTreeViews);
        return;
      }
    }
    wrap.setAttribute('data-tree-init','1');
    if(!parsed || typeof parsed!=='object'){
      void parseFailed;
      return;
    }
    const lineCount=rawText.split('\n').length;
    const showTree=_structuredCodeShowTree(_structuredCodeMode(),_structuredCodeThreshold(),lineCount);
    const treeDiv=document.createElement('div');
    treeDiv.className='tree-view'+(showTree?'':' tree-hidden');
    treeDiv.appendChild(_buildTreeDOM(parsed, 0));
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
    const rawPre=wrap.querySelector('.tree-raw-view');
    if(rawPre) rawPre.style.display=showTree?'none':'';
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

export {
  highlightCode,
  _loadJsyamlThen,
  _structuredCodeMode,
  _structuredCodeThreshold,
  _structuredCodeShowTree,
  initTreeViews,
  _buildTreeDOM,
  addCopyButtons,
  _jsyamlLoading,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  highlightCode: { enumerable: true, get: () => highlightCode, set: (value) => { highlightCode = value; } },
  _loadJsyamlThen: { enumerable: true, get: () => _loadJsyamlThen, set: (value) => { _loadJsyamlThen = value; } },
  _structuredCodeMode: { enumerable: true, get: () => _structuredCodeMode, set: (value) => { _structuredCodeMode = value; } },
  _structuredCodeThreshold: { enumerable: true, get: () => _structuredCodeThreshold, set: (value) => { _structuredCodeThreshold = value; } },
  _structuredCodeShowTree: { enumerable: true, get: () => _structuredCodeShowTree, set: (value) => { _structuredCodeShowTree = value; } },
  initTreeViews: { enumerable: true, get: () => initTreeViews, set: (value) => { initTreeViews = value; } },
  _buildTreeDOM: { enumerable: true, get: () => _buildTreeDOM, set: (value) => { _buildTreeDOM = value; } },
  addCopyButtons: { enumerable: true, get: () => addCopyButtons, set: (value) => { addCopyButtons = value; } },
  _jsyamlLoading: { enumerable: true, get: () => _jsyamlLoading, set: (value) => { _jsyamlLoading = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
