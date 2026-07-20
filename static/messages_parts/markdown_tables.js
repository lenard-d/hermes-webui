var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

function enhanceMarkdownTables(root){
  if(!root||!root.querySelectorAll) return;
  const scope=root;
  const tables=scope.querySelectorAll('.msg-body table:not([data-markdown-table-enhanced])');
  const sortLabel=typeof t==='function'?t('markdown_table_sort_column'):'Sort column';
  const filterLabel=typeof t==='function'?t('markdown_table_filter'):'Filter table';
  tables.forEach((table)=>{
    if(table.closest('.csv-table-wrap')) return;
    const headRows=table.tHead?Array.from(table.tHead.rows):[];
    const body=table.tBodies&&table.tBodies.length?table.tBodies[0]:table;
    const bodyRows=Array.from(body.rows||[]).filter((row)=>row.parentElement===body);
    const headerRow=headRows[0]||table.querySelector('tr');
    if(!headerRow||!bodyRows.length) return;
    table.setAttribute('data-markdown-table-enhanced','1');
    bodyRows.forEach((row,idx)=>{ row.dataset.markdownTableOriginalIndex=String(idx); });

    if(bodyRows.length>=4&&table.parentElement){
      const filter=document.createElement('input');
      filter.type='search';
      filter.className='markdown-table-filter';
      filter.placeholder=filterLabel;
      filter.setAttribute('aria-label',filterLabel);
      filter.autocomplete='off';
      filter.spellcheck=false;
      filter.addEventListener('input',()=>{
        const query=_markdownTableText(filter.value).toLowerCase();
        bodyRows.forEach((row)=>{
          row.hidden=!!query&&!_markdownTableText(row.textContent).toLowerCase().includes(query);
        });
      });
      table.parentElement.insertBefore(filter,table);
    }

    Array.from(headerRow.cells||[]).forEach((cell,colIdx)=>{
      const button=document.createElement('button');
      button.type='button';
      button.className='markdown-table-sort';
      const columnName=_markdownTableText(cell.textContent)||String(colIdx+1);
      const columnSortLabel=`${sortLabel}: ${columnName}`;
      button.setAttribute('aria-label',columnSortLabel);
      button.title=columnSortLabel;
      cell.setAttribute('aria-sort','none');
      const label=document.createElement('span');
      label.className='markdown-table-sort-label';
      while(cell.firstChild) label.appendChild(cell.firstChild);
      const indicator=document.createElement('span');
      indicator.className='markdown-table-sort-indicator';
      indicator.setAttribute('aria-hidden','true');
      button.appendChild(label);
      button.appendChild(indicator);
      button.addEventListener('click',()=>{
        const nextDir=table.dataset.markdownTableSortCol===String(colIdx)&&table.dataset.markdownTableSortDir==='asc'?'desc':'asc';
        table.dataset.markdownTableSortCol=String(colIdx);
        table.dataset.markdownTableSortDir=nextDir;
        Array.from(headerRow.cells||[]).forEach((other)=>{
          other.setAttribute('aria-sort','none');
        });
        cell.setAttribute('aria-sort',nextDir==='asc'?'ascending':'descending');
        const rows=Array.from(body.rows||[]).filter((row)=>row.parentElement===body);
        rows.sort((a,b)=>{
          const av=_markdownTableCellText(a.cells[colIdx]);
          const bv=_markdownTableCellText(b.cells[colIdx]);
          const cmp=av.localeCompare(bv,undefined,{numeric:true,sensitivity:'base'});
          if(cmp!==0) return nextDir==='asc'?cmp:-cmp;
          const ai=Number(a.dataset.markdownTableOriginalIndex||0);
          const bi=Number(b.dataset.markdownTableOriginalIndex||0);
          return ai-bi;
        });
        rows.forEach((row)=>body.appendChild(row));
      });
      cell.appendChild(button);
    });
  });
}

function _sanitizeMarkdownTableCellText(cell){
  if(!cell) return '';
  const sortButton=cell.querySelector?cell.querySelector('.markdown-table-sort'):null;
  if(sortButton){
    const sortLabel=sortButton.querySelector?sortButton.querySelector('.markdown-table-sort-label'):null;
    if(sortLabel) return _markdownTableCellText(sortLabel);
    return _markdownTableCellText(sortButton);
  }
  return _markdownTableCellText(cell);
}

function _markdownTableCopyHtmlEscape(value){
  return String(value||'')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;');
}

function _markdownTableCopyPayloadForTable(table){
  if(!table||!table.rows) return null;
  const rows=Array.from(table.rows||[]);
  if(!rows.length) return null;
  let headerRowCount=0;
  while(headerRowCount<rows.length){
    const cells=Array.from(rows[headerRowCount].cells||[]);
    if(!cells.length||!cells.every((cell)=>cell&&cell.tagName==='TH')) break;
    headerRowCount++;
  }

  const renderRows=(rowSet)=>rowSet.map((row)=>{
    const cellTag=(cell)=>String(cell&&cell.tagName?cell.tagName.toLowerCase():'td');
    const cells=Array.from(row.cells||[])
      .filter((cell)=>cell&&cell.nodeType===1)
      .map((cell)=>{
        const tag=cellTag(cell);
        const text=_sanitizeMarkdownTableCellText(cell);
        return `<${tag}>${_markdownTableCopyHtmlEscape(text)}</${tag}>`;
      })
      .join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  const headerRows=headerRowCount?renderRows(rows.slice(0, headerRowCount)):'';
  const bodyRows=renderRows(rows.slice(headerRowCount));
  const tableSections=[
    headerRows?`<thead>${headerRows}</thead>`:'',
    bodyRows?`<tbody>${bodyRows}</tbody>`:'',
  ].join('');

  const plainRows=rows.map((row)=>{
    return Array.from(row.cells||[])
      .map(_sanitizeMarkdownTableCellText)
      .join('\t');
  }).join('\n');

  return {html:`<table>${tableSections}</table>`, plain:plainRows};
}

function _findEnhancedMarkdownTable(node){
  let current=node&&node.nodeType===3?node.parentElement:node;
  while(current){
    if(current.matches&&current.matches('table[data-markdown-table-enhanced]')) return current;
    current=current.parentElement||current.parentNode;
  }
  return null;
}

function _findMarkdownTableCell(node){
  let current=node&&node.nodeType===3?node.parentElement:node;
  while(current){
    if(current.matches&&current.matches('th,td')) return current;
    current=current.parentElement||current.parentNode;
  }
  return null;
}

function _markdownTableNodeChildren(node){
  if(!node) return [];
  if(node.childNodes&&typeof node.childNodes.length==='number') return Array.from(node.childNodes);
  if(node.children&&typeof node.children.length==='number') return Array.from(node.children);
  return [];
}

function _markdownTableNodeBoundaryLength(node){
  if(!node) return 0;
  if(node.nodeType===3) return String(node.textContent||'').length;
  return _markdownTableNodeChildren(node).length;
}

function _markdownTableBoundaryWithinCell(container, offset, cell, edge){
  if(!container||!cell||typeof offset!=='number') return false;
  const atStart=edge==='start';
  let current=container;
  let currentOffset=offset;
  while(current){
    const boundaryLength=_markdownTableNodeBoundaryLength(current);
    if(atStart){
      if(currentOffset!==0) return false;
    }else if(currentOffset!==boundaryLength){
      return false;
    }
    if(current===cell) return true;
    const parent=current.parentElement||current.parentNode;
    if(!parent) return false;
    const siblings=_markdownTableNodeChildren(parent);
    const index=siblings.indexOf(current);
    if(index===-1) return false;
    currentOffset=atStart?index:index+1;
    current=parent;
  }
  return false;
}

function _markdownTableEdgeCell(table, edge){
  const rows=Array.from(table&&table.rows||[]);
  if(!rows.length) return null;
  const row=edge==='start'?rows[0]:rows[rows.length-1];
  const cells=Array.from(row&&row.cells||[]);
  if(!cells.length) return null;
  return edge==='start'?cells[0]:cells[cells.length-1];
}

function _isFullEnhancedMarkdownTableSelection(range, table){
  if(!range||!table) return false;
  const firstCell=_markdownTableEdgeCell(table,'start');
  const lastCell=_markdownTableEdgeCell(table,'end');
  if(!firstCell||!lastCell) return false;
  const startCell=_findMarkdownTableCell(range.startContainer);
  const endCell=_findMarkdownTableCell(range.endContainer);
  if(startCell!==firstCell||endCell!==lastCell) return false;
  return _markdownTableBoundaryWithinCell(range.startContainer, range.startOffset, firstCell, 'start')
    && _markdownTableBoundaryWithinCell(range.endContainer, range.endOffset, lastCell, 'end');
}

function _findEnhancedMarkdownTableFromRange(range){
  if(!range) return null;
  const found=_findEnhancedMarkdownTable(range.startContainer)
    || _findEnhancedMarkdownTable(range.endContainer)
    || _findEnhancedMarkdownTable(range.commonAncestorContainer);
  if(found) return found;
  const container=range.commonAncestorContainer&&range.commonAncestorContainer.nodeType===3
    ? range.commonAncestorContainer.parentElement
    : range.commonAncestorContainer;
  if(!container||!container.querySelectorAll||typeof range.intersectsNode!=='function') return null;
  for(const table of container.querySelectorAll('table[data-markdown-table-enhanced]')){
    try{
      if(range.intersectsNode(table)) return table;
    }catch(_){}
  }
  return null;
}

function _handleMarkdownTableCopy(event){
  if(!event) return;
  if(!window.getSelection)return;
  const selection=window.getSelection();
  if(!selection||selection.isCollapsed||!selection.rangeCount) return;
  const range=selection.getRangeAt(0);
  if(!range) return;
  const startCell=_findMarkdownTableCell(range.startContainer);
  const endCell=_findMarkdownTableCell(range.endContainer);
  if(startCell&&endCell&&startCell===endCell) return;
  const table=_findEnhancedMarkdownTableFromRange(range);
  if(!table||!table.matches||!table.matches('table[data-markdown-table-enhanced]')) return;
  if(!_isFullEnhancedMarkdownTableSelection(range, table)) return;
  const payload=_markdownTableCopyPayloadForTable(table);
  if(!payload) return;
  const clipboardData=event.clipboardData||event.originalEvent&&event.originalEvent.clipboardData;
  if(!clipboardData||typeof clipboardData.setData!=='function') return;
  if(typeof event.preventDefault==='function') event.preventDefault();
  clipboardData.setData('text/html', payload.html);
  clipboardData.setData('text/plain', payload.plain);
}

function _wireMarkdownTableCopyHandler(root){
  if(!root||!root.addEventListener||root.__markdownTableCopyHandlerInstalled) return;
  root.addEventListener('copy', _handleMarkdownTableCopy);
  root.__markdownTableCopyHandlerInstalled=true;
}

function _markdownTableText(value){
  return String(value||'').replace(/\s+/g,' ').trim();
}

function _markdownTableCellText(cell){
  return _markdownTableText(cell?cell.textContent:'');
}

window.enhanceMarkdownTables=enhanceMarkdownTables;

(function _wireMarkdownTableEnhancer(){
  if(typeof window==='undefined'||typeof window.renderMessages!=='function'||window.renderMessages._markdownTablesEnhanced) return;
  const baseRenderMessages=window.renderMessages;
  window.renderMessages=function(...args){
    const result=baseRenderMessages.apply(this,args);
    const inner=typeof $==='function'?$('msgInner'):document.getElementById('msgInner');
    enhanceMarkdownTables(inner);
    _wireMarkdownTableCopyHandler(inner);
    return result;
  };
  window.renderMessages._markdownTablesEnhanced=true;
})();

Object.assign(HermesMessages, {
  enhanceMarkdownTables,
});
