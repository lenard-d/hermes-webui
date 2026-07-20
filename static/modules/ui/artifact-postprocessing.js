import { S, esc } from './state.js';

const DIFF_MAX_SIZE=512*1024;
const CSV_MAX_SIZE=256*1024;
const EXCALIDRAW_MAX_SIZE=512*1024;
const PDF_MAX_SIZE=4*1024*1024;
const HTML_MAX_SIZE=256*1024;

function _mediaSessionQuery(){
  const mediaSessionId=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
  return mediaSessionId?'&session_id='+encodeURIComponent(mediaSessionId):'';
}

function loadDiffInline(container){
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
    try{
      const data=JSON.parse(dataStr);
      const elements=data.elements||[];
      if(!elements.length){el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_empty')}</div>`;return;}
      let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
      elements.forEach(el=>{
        const b=[el.x||0,el.y||0,(el.x||0)+(el.width||0),(el.y||0)+(el.height||0)];
        minX=Math.min(minX,b[0]);minY=Math.min(minY,b[1]);
        maxX=Math.max(maxX,b[2]);maxY=Math.max(maxY,b[3]);
      });
      const pad=20;minX-=pad;minY-=pad;maxX+=pad;maxY+=pad;
      const width=Math.max(maxX-minX,200);const height=Math.max(maxY-minY,150);
      const _sa=v=>String(v==null?'':v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const _num=(v,fb)=>{const n=Number(v);return Number.isFinite(n)?n:fb;};
      const svgParts=[`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${_num(minX,0)} ${_num(minY,0)} ${_num(width,200)} ${_num(height,150)}" class="excalidraw-svg">`];
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
        }else if(el.type==='line'||el.type==='arrow'){
          const pts=(el.points||[]).filter(p=>Array.isArray(p)&&p.length>=2);
          if(!pts.length) return;
          let d=`M ${_num(x+_num(pts[0][0],0),0)} ${_num(y+_num(pts[0][1],0),0)}`;
          for(let i=1;i<pts.length;i++) d+=` L ${_num(x+_num(pts[i][0],0),0)} ${_num(y+_num(pts[i][1],0),0)}`;
          const marker=el.type==='arrow'?' marker-end="url(#arrowhead)"':'';
          svgParts.push(`<path d="${d}" stroke="${stroke}" stroke-width="${sw}" fill="none" stroke-linecap="round" stroke-linejoin="round"${marker}/>`);
        }else if(el.type==='text'){
          const fontSize=_num(el.fontSize,20);
          const txt=String(el.text==null?'':el.text);
          txt.split('\n').forEach((line,i)=>{
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
      });
      svgParts.unshift(`<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#1e1e1e"/></marker></defs>`);
      svgParts.push('</svg>');
      el.innerHTML=svgParts.join('');
    }catch(e){
      el.innerHTML=`<div class="excalidraw-empty">${t('excalidraw_render_error')}</div>`;
    }
  });
}

// ── PDF inline preview (first page) ────────────────────────────────────────
let _pdfjsReady=false, _pdfjsLoading=false;
function loadPdfInline(container){
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
          const MAX_PAGES=20;
          const n=Math.min(total,MAX_PAGES);
          if(total>MAX_PAGES){
            const notice=document.createElement('div');
            notice.className='pdf-preview-truncated';
            notice.textContent=t('pdf_truncated',MAX_PAGES,total);
            body.appendChild(notice);
          }
          const renderPage=(i)=>{
            if(i>n) return;
            pdf.getPage(i).then(page=>{
              const canvas=document.createElement('canvas');
              const scale=1.5;
              const viewport=page.getViewport({scale});
              canvas.width=viewport.width;
              canvas.height=viewport.height;
              canvas.className='pdf-preview-canvas';
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

export {
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
  CSV_MAX_SIZE,
  _excalidrawScriptLoaded,
  _pdfjsReady,
  _pdfjsLoading,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
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
  CSV_MAX_SIZE: { enumerable: true, get: () => CSV_MAX_SIZE },
  _excalidrawScriptLoaded: { enumerable: true, get: () => _excalidrawScriptLoaded, set: (value) => { _excalidrawScriptLoaded = value; } },
  _pdfjsReady: { enumerable: true, get: () => _pdfjsReady, set: (value) => { _pdfjsReady = value; } },
  _pdfjsLoading: { enumerable: true, get: () => _pdfjsLoading, set: (value) => { _pdfjsLoading = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
