import { esc } from './state.js';

function _matchBacktickFenceLine(line){
  const m=String(line||'').match(/^[ ]{0,3}(`{3,})([^`]*)$/);
  if(!m) return null;
  return {fence:m[1],len:m[1].length,info:(m[2]||'').trim()};
}
function _isBacktickFenceClose(line,minLen){
  const m=String(line||'').match(/^[ ]{0,3}(`{3,})[ \t]*$/);
  return !!(m&&m[1].length>=minLen);
}
/**
 * Render fenced code blocks inside user messages.
 * Extracts ```…``` fences, replaces them with placeholders,
 * escapes remaining text as plain HTML, then restores code blocks
 * with the same <pre><code> pipeline used by renderMd().
 * All non-fenced text stays escaped (no bold/italic/link interpretation).
 */

function _stripWorkspaceDisplayPrefix(text){
  // v1 sentinel format `[Workspace::v1: <escaped path>]\n` injected since #1918.
  // Legacy format `[Workspace: <path>]\n` may still be present in transcripts
  // saved before the v1 migration; fall through to the legacy regex when the
  // v1 strip didn't match. Mirrors the Python `include_legacy=True` branch in
  // api/streaming.py:_strip_workspace_prefix(). Per Opus advisor on stage-322.
  const value = String(text||'');
  const stripped = value.replace(/^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*/,'');
  if(stripped !== value) return stripped.trim();
  return value.replace(/^\s*\[Workspace:[^\]]+\]\s*/,'').trim();
}
function _renderUserFencedBlocks(text){
  const stash=[];
  const contextStash=[];
  const mathStash=[];
  const stashMath=(type,src)=>{mathStash.push({type,src});return '\x00UM'+(mathStash.length-1)+'\x00';};
  const sentContextHtml=(label,quoteText)=>{
    const safeLabel=String(label||'').trim()||'Context';
    const safeQuote=String(quoteText||'').replace(/\s+$/,'');
    return `<figure class="sent-selection-context" data-selected-context="1"><figcaption class="sent-selection-context-label">${esc(safeLabel)}</figcaption><blockquote class="sent-selection-context-quote">${esc(safeQuote)}</blockquote></figure>`;
  };
  const stashContext=(label,quote)=>{contextStash.push(sentContextHtml(label,quote));return '\x00UC'+(contextStash.length-1)+'\x00';};
  const stashSelectedContextBlocks=(value)=>{
    const lines=String(value||'').split('\n');
    const marker='<!-- hermes-selected-context -->';
    const out=[];
    for(let i=0;i<lines.length;i++){
      const labelMatch=lines[i].match(/^\*\*([^\n]{1,200}):\*\*\s*$/);
      if(!labelMatch){out.push(lines[i]);continue;}
      const quoteLines=[];
      let j=i+1;
      if(lines[j]!==marker){out.push(lines[i]);continue;}
      j++;
      while(j<lines.length&&/^>/.test(lines[j])){
        quoteLines.push(lines[j].replace(/^>[ \t]?/,''));
        j++;
      }
      if(!quoteLines.length){out.push(lines[i]);continue;}
      out.push(stashContext(labelMatch[1], quoteLines.join('\n')));
      i=j-1;
    }
    return out.join('\n');
  };
  const restoreMath=html=>String(html||'').replace(/\x00UM(\d+)\x00/g,(_,i)=>{
    const item=mathStash[+i];
    if(!item) return '';
    if(item.type==='display') return `<div class="katex-block" data-katex="display">${esc(item.src)}</div>`;
    return `<span class="katex-inline" data-katex="inline">${esc(item.src)}</span>`;
  });
  let s=String(text||'');
  // Extract fenced code blocks FIRST so math regexes never run inside fenced
  // content. If math were stashed first, a user-typed code block containing
  // \[..\] / \(..\) / $$..$$ would be rendered as a KaTeX block inside
  // <pre><code> instead of as literal source. Mirrors renderMd()'s ordering.
  // CommonMark §4.5 line-anchored fence: the closing run must use at least
  // as many backticks as the opener, so inner triple-backtick fences remain content.
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g,(_,lead,_fence,info,code)=>{
    const langInfo=(info||'').trim();
    const langMatch=langInfo.match(/^(\w[\w+-]*)$/);
    let lang=langMatch?(langMatch[1]||'').trim().toLowerCase():'';
    code=code||'';
    // Remove one trailing newline if present (the fence consumes its own)
    if(code.endsWith('\n')) code=code.slice(0,-1);
    const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';
    const langAttr=lang?` class="language-${esc(lang)}"`:'';
    const preClass=/^(md|markdown|mdx)$/.test(lang)?' class="md-source-block"':'';
    if(lang==='diff'||lang==='patch'){
      const colored=esc(code).split('\n').map(line=>{
        if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
        if(line.startsWith('+')) return `<span class="diff-line diff-plus">${line}</span>`;
        if(line.startsWith('-')) return `<span class="diff-line diff-minus">${line}</span>`;
        return `<span class="diff-line">${line}</span>`;
      }).join('\n');
      stash.push(`${h}<pre class="diff-block"><code${langAttr}>${colored}</code></pre>`);
    } else {
      stash.push(`${h}<pre${preClass}><code${langAttr}>${esc(code)}</code></pre>`);
    }
    return lead+'\x00UF'+(stash.length-1)+'\x00';
  });
  // Now stash math from the OUTSIDE-of-fence text. Display delimiters must
  // run before inline so $$..$$ isn't mis-parsed as $..$..$..$.
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(_,m)=>stashMath('display',m));
  s=s.replace(/\\\[([\s\S]+?)\\\]/g,(_,m)=>stashMath('display',m));
  s=s.replace(/\$([^\s$\n][^$\n]*?[^\s$\n]|\S)\$/g,(_,m)=>stashMath('inline',m));
  s=s.replace(/\\\((.+?)\\\)/g,(_,m)=>stashMath('inline',m));
  // Render selected-context payloads produced by Reply with selection as calm
  // quote cards in the sent user bubble. Keep ordinary user Markdown escaped;
  // only blocks carrying the internal marker get custom treatment.
  s=stashSelectedContextBlocks(s);
  // Escape remaining plain text and convert newlines to <br>
  s=esc(s).replace(/\n/g,'<br>');
  // Restore stashed code/context blocks, then math placeholders as KaTeX targets.
  s=s.replace(/\x00UF(\d+)\x00/g,(_,i)=>stash[+i]);
  s=s.replace(/\x00UC(\d+)\x00/g,(_,i)=>contextStash[+i]||'');
  s=restoreMath(s);
  return s;
}
function _statusCardHtml(card){
  card=card||{};
  const rows=Array.isArray(card.rows)?card.rows:[];
  const sessionId=String(card.sessionId||'');
  const shortSessionId=sessionId.length>22?`${sessionId.slice(0,10)}…${sessionId.slice(-8)}`:sessionId;
  const copyIcon=(typeof li==='function')?li('copy',13):'Copy';
  const copyBtn=sessionId
    ? `<button class="status-card-session-copy" type="button" data-copy-status-session="${esc(card.sessionId||'')}" title="${esc(t('copy'))}" onclick="copyStatusSessionId(this);event.stopPropagation()"><span>${esc(shortSessionId)}</span>${copyIcon}</button>`
    : '';
  const rowHtml=rows.map(row=>`
    <div class="status-card-row">
      <span class="status-card-label">${esc(row.label||'')}</span>
      <span class="status-card-value">${esc(row.value||'')}</span>
    </div>`).join('');
  return `<div class="status-card" data-status-card="1">
    <div class="status-card-head">
      <div class="status-card-title-wrap">
        <div class="status-card-title">${esc(card.title||t('status_heading'))}</div>
        <div class="status-card-subtitle">${esc(card.subtitle||'')}</div>
      </div>
      ${copyBtn}
    </div>
    <div class="status-card-grid">${rowHtml}</div>
  </div>`;
}

export {
  _matchBacktickFenceLine,
  _isBacktickFenceClose,
  _stripWorkspaceDisplayPrefix,
  _renderUserFencedBlocks,
  _statusCardHtml,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _matchBacktickFenceLine: { enumerable: true, get: () => _matchBacktickFenceLine, set: value => { _matchBacktickFenceLine = value; } },
  _isBacktickFenceClose: { enumerable: true, get: () => _isBacktickFenceClose, set: value => { _isBacktickFenceClose = value; } },
  _stripWorkspaceDisplayPrefix: { enumerable: true, get: () => _stripWorkspaceDisplayPrefix, set: value => { _stripWorkspaceDisplayPrefix = value; } },
  _renderUserFencedBlocks: { enumerable: true, get: () => _renderUserFencedBlocks, set: value => { _renderUserFencedBlocks = value; } },
  _statusCardHtml: { enumerable: true, get: () => _statusCardHtml, set: value => { _statusCardHtml = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
