import { _mountMermaidViewer } from './media-and-quota.js';
import { esc } from './state.js';

let _mermaidLoading=false;
let _mermaidReady=false;

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
      const renderedSvg=block.querySelector('svg');
      if(renderedSvg) _mountMermaidViewer(renderedSvg,{mode:'inline'});
      block.classList.add('mermaid-rendered');
    }catch(e){
      const tmp=document.getElementById('d'+id);
      if(tmp) tmp.remove();
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
      el.outerHTML=`<code>${esc(src)}</code>`;
    }
  });
}

export {
  renderMermaidBlocks,
  _isStreamingEquationPending,
  renderKatexBlocks,
  _mermaidLoading,
  _mermaidReady,
  _katexLoading,
  _katexReady,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderMermaidBlocks: { enumerable: true, get: () => renderMermaidBlocks, set: (value) => { renderMermaidBlocks = value; } },
  _isStreamingEquationPending: { enumerable: true, get: () => _isStreamingEquationPending, set: (value) => { _isStreamingEquationPending = value; } },
  renderKatexBlocks: { enumerable: true, get: () => renderKatexBlocks, set: (value) => { renderKatexBlocks = value; } },
  _mermaidLoading: { enumerable: true, get: () => _mermaidLoading, set: (value) => { _mermaidLoading = value; } },
  _mermaidReady: { enumerable: true, get: () => _mermaidReady, set: (value) => { _mermaidReady = value; } },
  _katexLoading: { enumerable: true, get: () => _katexLoading, set: (value) => { _katexLoading = value; } },
  _katexReady: { enumerable: true, get: () => _katexReady, set: (value) => { _katexReady = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
