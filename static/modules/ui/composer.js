import { _clearActivityElapsedTimer, scrollIfPinned } from './activity-and-scroll.js';
import { _copyText } from './clipboard.js';
import { isCompressionUiRunning } from './compression-ui.js';
import { _dynamicModelLabels, _inlineMediaHtmlForRef, _isSafeDataImageUri, _mdImageHtml } from './media-and-quota.js';
import { syncModelChip } from './model-catalog.js';
import { _applyModelToDropdown } from './model-state.js';
import { $, S, SESSION_QUEUES, _clearPersistedSessionQueue, _getSessionQueue, _isBacktickFenceClose, _matchBacktickFenceLine, _persistSessionQueueStorage, _queueDrainSid, assistantDisplayName, esc, getQueuedSessionCount, queueSessionMessage, shiftQueuedSessionMessage } from './state.js';
import { renderTray } from './upload-tray.js';
import { compatibilityBindings as stateBindings } from './state.js';

function renderMd(raw){
  let s=(raw||'').replace(/\r\n/g,'\n').replace(/\r/g,'\n');
  // ── Entity decode: must run FIRST so &gt; lines become > for the blockquote
  // pre-pass below. LLMs sometimes emit HTML-entity-encoded output; without this
  // a blockquote sent as "&gt; text" would never be recognised as a blockquote.
  s=s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  // ── Blockquote pre-pass (must run BEFORE every other markdown pass) ────────
  // Group consecutive >-prefixed lines, strip the > prefix from each line,
  // recursively render the stripped content with the full pipeline, and
  // replace the group with a stash token. This is the only way fenced code,
  // headings, hr, and ordered lists inside a blockquote can render correctly:
  // the per-line passes downstream don't know about > prefixes, and by the
  // time the blockquote handler used to run those passes had already mangled
  // the >-prefixed lines.
  //
  // Walks lines (instead of using a single regex) so >-prefixed lines that
  // sit inside a non-blockquote fenced block (e.g. a shell prompt in a
  // ```bash``` example) are not miscaptured as a blockquote.
  const _bq_stash=[];
  s=(function _applyBlockquotes(input){
    const lines=input.split('\n');
    const out=[];
    let inFence=false;     // inside a non-blockquote backtick fence
    let fenceLen=0;
    let bqStart=-1;
    const flush=(end)=>{
      if(bqStart<0) return;
      // Strip "> " prefix (and bare ">" → empty) from each line
      const stripped=lines.slice(bqStart,end).map(l=>l.replace(/^> ?/,'')).join('\n');
      // Recursive call: full pipeline on stripped content. Handles fenced
      // code, headings, hr, ordered/unordered lists, nested blockquotes
      // (>>) — anything that renderMd handles at the top level.
      const rendered=renderMd(stripped);
      _bq_stash.push('<blockquote>'+rendered+'</blockquote>');
      // Surround the token with blank lines so the paragraph splitter
      // isolates it as its own chunk (otherwise the token gets wrapped
      // in <p>...<br> with adjacent text, producing invalid HTML).
      out.push('');
      out.push('\x00Q'+(_bq_stash.length-1)+'\x00');
      out.push('');
      bqStart=-1;
    };
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(inFence){
        out.push(line);
        if(_isBacktickFenceClose(line,fenceLen)){inFence=false;fenceLen=0;}
        continue;
      }
      const fenceOpen=_matchBacktickFenceLine(line);
      if(fenceOpen){
        flush(i);
        out.push(line);
        inFence=true;
        fenceLen=fenceOpen.len;
        continue;
      }
      if(/^>/.test(line)){
        if(bqStart<0) bqStart=i;
      } else {
        flush(i);
        out.push(line);
      }
    }
    flush(lines.length);
    return out.join('\n');
  })(s);
  // ── MEDIA: token stash (must run first, before any other processing) ───────
  // Detect MEDIA:<path-or-url> tokens emitted by the agent (e.g. screenshots,
  // generated images) and replace them with inline <img> or download links.
  // Stashed so the path/URL is never processed as markdown.
  const media_stash=[];
  s=s.replace(/MEDIA:([^\s\)\]]+)/g,(_,raw_ref)=>{
    media_stash.push(raw_ref);
    return '\x00D'+(media_stash.length-1)+'\x00';
  });
  // ── End MEDIA stash ─────────────────────────────────────────────────────────
  // Pre-pass: decode HTML entities first so markdown processing works correctly.
  // This prevents double-escaping when LLM outputs entities like &lt; &gt; &amp;
  const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
  s=decode(s);
  // Pre-pass: convert safe inline HTML tags the model may emit into their
  // markdown equivalents so the pipeline can render them correctly.
  // Only runs OUTSIDE fenced code blocks and backtick spans (stash + restore).
  // Unsafe tags (anything not in the allowlist) are left as-is and will be
  // HTML-escaped by esc() when they reach an innerHTML assignment -- no XSS risk.
  // Fence stash: protect code blocks and backtick spans from all further processing.
  // Must run BEFORE math_stash so $..$ inside code spans is not extracted as math.
  // Split into fenced blocks (\x00P — kept stashed until after all markdown passes)
  // and inline backtick spans (\x00F — restored before bold/italic so **`code`** works).
  // Fenced blocks are converted to <pre><code> here so their content is HTML-escaped
  // and never exposed to list/heading/table regexes that could corrupt the layout.
  // Fixes #1154: diff/patch lines inside fenced blocks (e.g. + added, - removed)
  // were matching the unordered-list regex and injecting <ul>/<li> inside <pre>,
  // breaking </pre> closure and corrupting all subsequent message rendering.
  const _preBlock_stash=[];
  const fence_stash=[];
  // CommonMark §4.5: opening fence must start a line (with up to 3 spaces of indent)
  // and closing fence must start a line with the same backtick char and at least
  // as many backticks as the opener. Without line/fence-length anchoring, a literal
  // ``` inside a code block (e.g. a nested markdown example) terminates the outer
  // block at the wrong place, leaking content into the markdown stream where
  // bold/italic/inline-code passes corrupt it. Fixes #1438 and #1696.
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g,(_,lead,_fence,info,code)=>{
    const langInfo=(info||'').trim();
    const langMatch=langInfo.match(/^(\w[\w+-]*)$/);
    const lang=langMatch?(langMatch[1]||'').trim().toLowerCase():'';
    code=code||'';
    const codeLines=code.split('\n');
    const firstCodeLine=codeLines.find(line=>line.trim())||'';
    const firstMermaidLine=codeLines.map(line=>line.trim()).find(line=>line&&!line.startsWith('%%'))||'';
    const looksLikeLineNumberedToolOutput=/^\s*\d+\|/.test(firstCodeLine);
    const looksLikeMermaidStart=firstMermaidLine==='---'||/^(graph|flowchart|sequenceDiagram|classDiagram|classDiagram-v2|stateDiagram|stateDiagram-v2|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic|c4Context|c4Container|c4Component|c4Dynamic|sankey-beta|block-beta|packet-beta|xychart-beta|kanban|architecture-beta)\b/.test(firstMermaidLine);
    if(lang==='mermaid'&&!looksLikeLineNumberedToolOutput&&looksLikeMermaidStart){
      const id='mermaid-'+Math.random().toString(36).slice(2,10);
      _preBlock_stash.push(`<div class="mermaid-block" data-mermaid-id="${id}">${esc(code.trim())}</div>`);
    } else {
      const h=lang?`<div class="pre-header">${esc(lang)}</div>`:'';
      const langAttr=lang?` class="language-${esc(lang)}"`:'';
      const preClass=/^(md|markdown|mdx)$/.test(lang)?' class="md-source-block"':'';
      // For diff/patch blocks, wrap each line in a colored span
      if(lang==='diff'||lang==='patch'){
        const colored=esc(code.replace(/\n$/,'')).split('\n').map(line=>{
          if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
          if(line.startsWith('+')) return `<span class="diff-line diff-plus">${line}</span>`;
          if(line.startsWith('-')) return `<span class="diff-line diff-minus">${line}</span>`;
          return `<span class="diff-line">${line}</span>`;
        }).join('\n');
        _preBlock_stash.push(`${h}<pre class="diff-block"><code${langAttr}>${colored}</code></pre>`);
      // For JSON/YAML blocks, add tree-view placeholder with raw data
      } else if(lang==='json'||lang==='yaml'){
        const rawCode=esc(code.replace(/\n$/,''));
        // Encode newlines as &#10; to prevent HTML attribute normalization
        // (browsers collapse \n to spaces inside attribute values).
        const rawAttr=rawCode.replace(/"/g,'&quot;').replace(/\n/g,'&#10;');
        const blockId='tree-'+Math.random().toString(36).slice(2,10);
        _preBlock_stash.push(`<div class="code-tree-wrap" data-raw="${rawAttr}" data-lang="${lang}" id="${blockId}">${h}<pre class="tree-raw-view"><code${langAttr}>${rawCode}</code></pre></div>`);
      // CSV blocks → render as styled table
      } else if(lang==='csv'){
        const rows=code.replace(/\n$/,'').split('\n').filter(r=>r.trim());
        if(rows.length>=2){
          const headers=rows[0].split(',').map(c=>c.trim());
          const body=rows.slice(1).map(r=>'<tr>'+r.split(',').map(c=>`<td>${esc(c.trim())}</td>`).join('')+'</tr>').join('');
          _preBlock_stash.push(`${h}<div class="csv-table-wrap"><table class="csv-table"><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`);
        } else {
          _preBlock_stash.push(`${h}<pre${preClass}><code${langAttr}>${esc(code.replace(/\n$/,''))}</code></pre>`);
        }
      } else {
        _preBlock_stash.push(`${h}<pre${preClass}><code${langAttr}>${esc(code.replace(/\n$/,''))}</code></pre>`);
      }
    }
    return lead+'\x00P'+(_preBlock_stash.length-1)+'\x00';
  });
  s=s.replace(/`([^`\n]+)`/g,(_,c)=>{fence_stash.push('<code>'+esc(c)+'</code>');return '\x00F'+(fence_stash.length-1)+'\x00';});
  // Math stash: protect $$..$$ and $..$ from markdown processing
  // Runs AFTER fence_stash so backtick code spans protect their dollar-sign contents
  const math_stash=[];
  // Display math: $$...$$ and \[...\] (must come before inline to avoid mis-parsing)
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Match a single literal backslash before the display delimiter (the common LLM form).
  s=s.replace(/\\\[([\s\S]+?)\\\]/g,(_,m)=>{math_stash.push({type:'display',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Inline math: $...$ — require non-space/non-digit at opening boundary to avoid
  // false positives on currency like "$1,000 xuống ~$95" or "costs $5 and $10".
  // Aligns with smd's se() guard which also rejects $ followed by digits.
  s=s.replace(/\$([^\s$\d\n][^$\n]*?[^\s$\n]|[^\s\d])\$/g,(_,m)=>{if(m.includes(' | '))return '\$'+m+'\$';math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Also stash \(...\) LaTeX delimiters.
  // Match a single literal backslash before the delimiter (the common LLM form).
  s=s.replace(/\\\((.+?)\\\)/g,(_,m)=>{math_stash.push({type:'inline',src:m});return '\x00M'+(math_stash.length-1)+'\x00';});
  // Safe tag → markdown equivalent (these produce the same output as **text** etc.)
  // Stash raw <pre> blocks so the inline <code> rewrite below does not run
  // inside them. Running that rewrite in <pre> content can introduce stray
  // backticks for multiline code and break subsequent code-box rendering.
  const rawPreStash=[];
  s=s.replace(/(<pre\b[^>]*>[\s\S]*?<\/pre>)/gi,m=>{rawPreStash.push(m);return `\x00R${rawPreStash.length-1}\x00`;});
  // Bare file:// artifact links → media. Some gateway/tool surfaces emit bare
  // file:// links for local artifacts instead of MEDIA: tokens; browser clients
  // cannot open the server filesystem directly, so route them through /api/media.
  // Runs AFTER fenced-block (\x00P), inline-code (\x00F), AND raw-<pre> (\x00R)
  // stashing so a file:// inside any code/preformatted region stays literal text
  // (#3219/#3234). Only bare URLs (line-start or whitespace-delimited) match, so
  // normal [label](file://...) markdown anchors keep the link path below.
  s=s.replace(/(^|\s)(file:\/\/[^\s<>"')\]]+)/g,(_,lead,raw_ref)=>{
    media_stash.push(raw_ref);
    return lead+'\x00D'+(media_stash.length-1)+'\x00';
  });
  s=s.replace(/<strong>([\s\S]*?)<\/strong>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<b>([\s\S]*?)<\/b>/gi,(_,t)=>'**'+t+'**');
  s=s.replace(/<em>([\s\S]*?)<\/em>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<i>([\s\S]*?)<\/i>/gi,(_,t)=>'*'+t+'*');
  s=s.replace(/<code>([^<]*?)<\/code>/gi,(_,t)=>'`'+t+'`');
  s=s.replace(/<br\s*\/?>/gi,'\n');
  // ── Glued-bold-heading lift (issue #1446) ────────────────────────────────
  // LLMs in thinking/reasoning mode frequently emit a "section header" glued
  // to the end of the previous paragraph with no whitespace, like:
  //
  //   Para 1 text.**Heading to Para 2**
  //
  //   Para 2 text.**Heading to Para 3**
  //
  // CommonMark renders that correctly as paragraph-end inline bold, but the
  // visual effect is a run-on label rather than a section break. Lift the
  // glued bold into its own paragraph when it follows a sentence terminator
  // and is followed by a blank line.
  //
  // Constraints (avoid false positives):
  //   - Trigger only on a sentence terminator (.!?) IMMEDIATELY before `**`
  //     (no space) — that pattern is almost always a glued heading, not
  //     intentional emphasis.
  //   - Inner text length ≤ 80 chars — long bold runs are usually emphasis
  //     prose, not headings.
  //   - Trailing `\n\n` required — preserves mid-paragraph emphasis like
  //     "this is **important**." untouched.
  //   - Inner text must not contain newlines or `*` (single-line bold only).
  //   - Runs after fenced code, math, and raw <pre> are stashed, so code
  //     content is protected (see pipeline notes).
  s=s.replace(/([.!?])\*\*([^*\n]{1,80})\*\*\n\n/g,'$1\n\n**$2**\n\n');
  // Inline backtick spans: restore <code> tags produced in the stash callback above.
  // Must happen BEFORE bold/italic so **`code`** → <strong><code>code</code></strong>.
  s=s.replace(/\x00F(\d+)\x00/g,(_,i)=>fence_stash[+i]);
  // inlineMd: process bold/italic/code/links within a single line of text.
  // Used inside list items and blockquotes where the text may already contain
  // HTML from the pre-pass → bold pipeline, so we cannot call esc() directly.
  function inlineMd(t){
    // Stash backtick code spans first so bold/italic never esc() their content
    const _code_stash=[];
    t=t.replace(/`([^`\n]+)`/g,(_,x)=>{_code_stash.push(`<code>${esc(x)}</code>`);return `\x00C${_code_stash.length-1}\x00`;});
    t=t.replace(/\*\*\*(.+?)\*\*\*/g,(_,x)=>`<strong><em>${esc(x)}</em></strong>`);
    t=t.replace(/\*\*(.+?)\*\*/g,(_,x)=>`<strong>${esc(x)}</strong>`);
    t=t.replace(/\*([^*\n]+)\*/g,(_,x)=>`<em>${esc(x)}</em>`);
    // Strikethrough: ~~text~~ → <del>text</del>
    t=t.replace(/~~(.+?)~~/g,(_,x)=>`<del>${esc(x)}</del>`);
    // #487: Image pass — runs while code stash is active so ![x](url) inside
    // backticks stays protected as a \x00C token and is never rendered as <img>.
    // Must run before _code_stash restore and before _link_stash so the image
    // is not consumed by the [label](url) link regex.
    t=t.replace(/!\[([^\]]*)\]\(((?:https?:\/\/|file:\/\/|data:image\/)[^\)]+)\)/g,(_,alt,url)=>(typeof _mdImageHtml==='function')?_mdImageHtml(alt,url):`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy">`);
    // Stash rendered <img> tags so autolink never matches URLs inside src=
    const _img_stash=[];
    t=t.replace(/(<img\b[^>]*>)/g,m=>{_img_stash.push(m);return `\x00G${_img_stash.length-1}\x00`;});
    t=t.replace(/\x00C(\d+)\x00/g,(_,i)=>_code_stash[+i]);
    // Stash [label](url) links before autolink so the URL in href= is not re-linked
    const _link_stash=[];
    t=t.replace(/\[([^\]]+)\]\(((?:https?:\/\/|file:\/\/|workspace:\/\/|session:\/\/|mailto:|tel:|message:)[^\s\)]+)\)/g,(_,lb,u)=>{_link_stash.push(_markdownAnchor(lb,u));return `\x00L${_link_stash.length-1}\x00`;});
    t=t.replace(/(https?:\/\/[^\s<>"')\]]+)/g,(url)=>{const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';const clean=trail?url.slice(0,-1):url;return `<a href="${clean}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;});
    t=t.replace(/\x00L(\d+)\x00/g,(_,i)=>_link_stash[+i]);
    t=t.replace(/\x00G(\d+)\x00/g,(_,i)=>_img_stash[+i]);
    // Escape any plain text that isn't already wrapped in a tag we produced
    // by escaping bare < > that are not part of our own tags
    const SAFE_INLINE=/^<\/?(strong|em|del|code|a|img)([\s>]|$)/i;
    t=t.replace(/<\/?[a-z][^>]*>/gi,tag=>SAFE_INLINE.test(tag)?tag:esc(tag));
    return t;
  }
  // Stash <code> tags from the backtick pass above so the outer bold/italic
  // regexes don't esc() their content (e.g. **`code`** → <strong><code>code</code></strong>)
  const _ob_stash=[];
  s=s.replace(/(<code\b[^>]*>[\s\S]*?<\/code>)/g,m=>{_ob_stash.push(m);return `\x00O${_ob_stash.length-1}\x00`;});
  s=s.replace(/\*\*\*(.+?)\*\*\*/g,(_,t)=>`<strong><em>${esc(t)}</em></strong>`);
  s=s.replace(/\*\*(.+?)\*\*/g,(_,t)=>`<strong>${esc(t)}</strong>`);
  s=s.replace(/\*([^*\n]+)\*/g,(_,t)=>`<em>${esc(t)}</em>`);
  s=s.replace(/~~(.+?)~~/g,(_,t)=>`<del>${esc(t)}</del>`);
  s=s.replace(/\x00O(\d+)\x00/g,(_,i)=>_ob_stash[+i]);
  s=s.replace(/^###### (.+)$/gm,(_,t)=>`<h6>${inlineMd(t)}</h6>`).replace(/^##### (.+)$/gm,(_,t)=>`<h5>${inlineMd(t)}</h5>`).replace(/^#### (.+)$/gm,(_,t)=>`<h4>${inlineMd(t)}</h4>`).replace(/^### (.+)$/gm,(_,t)=>`<h3>${inlineMd(t)}</h3>`).replace(/^## (.+)$/gm,(_,t)=>`<h2>${inlineMd(t)}</h2>`).replace(/^# (.+)$/gm,(_,t)=>`<h1>${inlineMd(t)}</h1>`);
  s=s.replace(/^---+$/gm,'<hr>');
  // (Blockquotes are handled by the pre-pass at the top of renderMd, before
  // fence_stash. The per-line passes below never see > prefixes.)
  function _renderListBlock(lines, ordered){
    const marker=ordered?'\\d+\\. ':'[-*+] ';
    let html=ordered?'<ol>':'<ul>';
    let item=null;
    const flush=()=>{
      if(!item) return;
      const body=item.parts.join('\n').trim();
      const text=body;
      let inner;
      if(!ordered && /^\[x\] /i.test(text)) inner='<span class="task-done">✅</span> '+inlineMd(text.slice(4));
      else if(!ordered && /^\[ \] /.test(text)) inner='<span class="task-todo">☐</span> '+inlineMd(text.slice(4));
      else inner=inlineMd(text);
      const valueAttr=item.value!==null?` value="${item.value}"`:'';
      const styleAttr=item.indent?` style="margin-left:16px"`:'';
      html+=`<li${valueAttr}${styleAttr}>${inner}</li>`;
      item=null;
    };
    for(const raw of lines){
      const line=String(raw||'');
      const nested=line.match(new RegExp(`^ {2,}(${marker})(.*)$`));
      if(nested){
        flush();
        item={indent:true,value:ordered?parseInt(nested[1],10):null,parts:[nested[2]]};
        continue;
      }
      const top=line.match(new RegExp(`^(?:  )?(${marker})(.*)$`));
      if(top){
        flush();
        item={indent:false,value:ordered?parseInt(top[1],10):null,parts:[top[2]]};
        continue;
      }
      if(!item) continue;
      item.parts.push(line.replace(/^ {2,}/,'').trim());
    }
    flush();
    return html+(ordered?'</ol>':'</ul>');
  }
  function _renderLists(src, ordered){
    const lines=src.split('\n');
    const out=[];
    const topRe=ordered?/^(?:  )?\d+\. /:/^(?:  )?[-*+] /;
    const nestedRe=ordered?/^ {2,}\d+\. /:/^ {2,}[-*+] /;
    const contRe=/^ {2,}\S/;
    let i=0;
    while(i<lines.length){
      if(!topRe.test(lines[i])){
        out.push(lines[i]);
        i++;
        continue;
      }
      const block=[lines[i]];
      i++;
      while(i<lines.length){
        const line=lines[i];
        if(topRe.test(line)||nestedRe.test(line)||contRe.test(line)){
          block.push(line);
          i++;
          continue;
        }
        if(!line.trim()){
          const next=lines[i+1]||'';
          if(topRe.test(next)||nestedRe.test(next)||contRe.test(next)){
            i++;
            continue;
          }
        }
        break;
      }
      out.push(_renderListBlock(block,ordered));
    }
    return out.join('\n');
  }
  // Preserve continuation lines, nested indentation, and LaTeX placeholder lines
  // inside list items without changing the wider markdown pipeline.
  s=_renderLists(s,false);
  // Ordered-list parsing intentionally runs on the post-unordered string; the
  // unordered pass emits <ul> HTML that cannot satisfy the ordered-item regex.
  // Keep continuation lines attached to their item and preserve explicit
  // numbering via value= even when blank lines split the markdown.
  s=_renderLists(s,true);
  // Tables: | col | col | header row followed by | --- | --- | separator then data rows
  // NOTE: table pass runs BEFORE outer link pass so [label](url) in table cells
  // is handled by inlineMd() only — prevents double-linking.
  s=s.replace(/((?:^ {0,3}\|.+\|[ \t]*\n?)+)/gm,block=>{
    const rows=block.trim().split('\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    const isSep=r=>/^\|[\s|:-]+\|$/.test(r.trim());
    if(!isSep(rows[1]))return block;
    // _protectPipes: temporarily swap pipes inside matching bracket pairs for a
    // sentinel before split('|'), then restore. Iterates until no more matches
    // so all pipes inside one pair are caught.
    // Note: both opening and closing brace literals in the character classes
    // are written as hex escapes (\x7b and \x7d) so the JS source contains no
    // bare brace glyphs that would confuse the brace-counting extractFunc in
    // tests/test_renderer_js_behaviour.py. Regex semantics are identical.
    // Bracket set is paren / square / curly only -- NOT angle brackets, since
    // angle brackets are overwhelmingly comparison operators in real LLM table
    // output (`| x < 5 | y > 10 |`) and treating them as a pair collapses cells.
    const _protectPipes=r=>{let prev;do{prev=r;r=r.replace(/([([\x7b][^)\]\x7d]*)[|]([^)\]\x7d]*[)\]\x7d])/g,(_,a,b)=>a+'\x00PIPE\x00'+b);r=r.replace(/(<code>[^<]*)[|]([^<]*<\/code>)/g,(_,a,b)=>a+'\x00PIPE\x00'+b);}while(r!==prev);return r;};
    const _restorePipes=s=>s.replace(/\x00PIPE\x00/g,'|');
    const parseRow=r=>{r=_protectPipes(r);return r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<td>${inlineMd(_restorePipes(c.trim()))}</td>`).join('');};
    const parseHeader=r=>{r=_protectPipes(r);return r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>`<th>${inlineMd(_restorePipes(c.trim()))}</th>`).join('');};
    const header=`<tr>${parseHeader(rows[0])}</tr>`;
    const body=rows.slice(2).map(r=>`<tr>${parseRow(r)}</tr>`).join('');
    // Surround with blank lines so the final paragraph splitter treats the
    // generated table as its own block even when the regex consumes one of the
    // markdown block's trailing newlines.
    return `\n\n<table><thead>${header}</thead><tbody>${body}</tbody></table>\n\n`;
  });
  // #487: Outer image pass — handles ![alt](url) in plain paragraphs (outside tables/lists).
  // Runs AFTER the table pass (images in table cells are handled by inlineMd() above).
  // Runs BEFORE the outer [label](url) link pass so the image is not consumed as a plain link.
  s=s.replace(/!\[([^\]]*)\]\(((?:https?:\/\/|file:\/\/|data:image\/)[^\)]+)\)/g,(_,alt,url)=>(typeof _mdImageHtml==='function')?_mdImageHtml(alt,url):`<img src="${url.replace(/"/g,'%22')}" alt="${esc(alt)}" class="msg-media-img" loading="lazy">`);
  // Outer link pass for labeled links in plain paragraphs (outside table cells).
  // Runs AFTER the table pass so table cells are processed by inlineMd() only.
  // Stash existing <a> tags first to avoid re-linking already-linked URLs.
  const _a_stash=[];
  s=s.replace(/(<a\b[^>]*>[\s\S]*?<\/a>)/g,m=>{_a_stash.push(m);return `\x00A${_a_stash.length-1}\x00`;});
  s=s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|file:\/\/|workspace:\/\/|session:\/\/|mailto:|tel:|message:)[^\s\)]+)\)/g,(_,label,url)=>_markdownAnchor(label,url));
  s=s.replace(/\x00A(\d+)\x00/g,(_,i)=>_a_stash[+i]);
  // Restore raw <pre> only after markdown rewrites so literal preformatted
  // content stays placeholder-protected, then let the sanitizer normalize tags.
  s=s.replace(/\x00R(\d+)\x00/g,(_,i)=>rawPreStash[+i]);
  // Sanitize any remaining HTML tags.  The renderer intentionally returns
  // HTML and inserts it with innerHTML later, so tag names alone are not enough:
  // raw/model-provided HTML like <img onerror=...> or <a href="javascript:...">
  // must lose executable attributes and dangerous schemes while preserving the
  // small set of attributes generated by this markdown pipeline.
  // Reference only — documents the allowed tag set. Superseded by _tag() allowlists.
  // Tests verify this list is complete; _tag() enforces it.
  const SAFE_TAGS=/^<\/?(?:strong|em|del|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|hr|blockquote|p|br|a|div|span|img)([\s>]|$)/i;
  function _safeAttrValue(v){
    return String(v||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&').trim();
  }
  function _markdownHref(raw){
    const href=String(raw||'').replace(/"/g,'%22');
    if(/^session:\/\//i.test(href)){
      const sid=href.replace(/^session:\/\//i,'').split(/[?#]/)[0];
      try{
        const decoded=decodeURIComponent(sid);
        if(typeof _sessionUrlForSid==='function') return _sessionUrlForSid(decoded);
        return 'session/'+encodeURIComponent(decoded);
      }catch(_){
        return 'session/'+encodeURIComponent(sid);
      }
    }
    if(/^workspace:\/\//i.test(href)){
      try{
        const rel=decodeURIComponent(href.replace(/^workspace:\/\//i,'')).replace(/^~\//,'').replace(/^\.\//,'');
        return '#workspace='+encodeURIComponent(rel);
      }catch(_){
        return '#';
      }
    }
    if(/^file:\/\//i.test(href)){
      try{
        const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
        return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
      }catch(_){
        return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
      }
    }
    return href;
  }
  function _isInternalSessionHref(raw){
    const href=String(raw||'').trim();
    if(/^session\/[^?#]+/i.test(href)) return true;
    try{
      const base=(typeof document!=='undefined'&&document.baseURI)||
        (typeof window!=='undefined'&&window.location&&window.location.href)||
        'http://localhost/';
      const url=new URL(href,base);
      const baseUrl=new URL(base,base);
      if(url.origin!==baseUrl.origin) return false;
      const basePath=baseUrl.pathname.replace(/(?:index\.html)?$/,'').replace(/\/[^/]*$/,'/');
      const root=basePath.endsWith('/')?basePath:basePath+'/';
      return url.pathname.startsWith(root+'session/')||url.pathname.startsWith('/session/');
    }catch(_){
      return false;
    }
  }
  function _isSafeLabelInline(tag){
    return /^<\/?(strong|em|del|code)([\s>]|$)/i.test(tag);
  }
  function _markdownLabelHtml(label){
    const _label_stash=[];
    const tokenized=String(label||'').replace(/<\/?[a-z][^>]*>/gi,tag=>{
      if(!_isSafeLabelInline(tag)) return tag;
      _label_stash.push(tag);
      return `\x00H${_label_stash.length-1}\x00`;
    });
    return esc(tokenized).replace(/\x00H(\d+)\x00/g,(_,i)=>_label_stash[+i]);
  }
  function _markdownAnchor(label,rawUrl){
    const href=_markdownHref(rawUrl);
    const internal=/^session:\/\//i.test(String(rawUrl||'')) || _isInternalSessionHref(href);
    return `<a${internal?' class="session-link"':''} href="${href}"${internal?'':' target="_blank" rel="noopener"'}>${_markdownLabelHtml(label)}</a>`;
  }
  function _isSafeUrl(v, img){
    const raw=_safeAttrValue(v);
    const compact=raw.replace(/[\u0000-\u001f\u007f\s]+/g,'').toLowerCase();
    if(!compact) return false;
    // data:image/* is permitted for <img> only, validated by the shared strict
    // predicate. Every other
    // data: scheme stays blocked for both anchors and images.
    if(/^data:/i.test(compact)) return !!(img && typeof _isSafeDataImageUri==='function' && _isSafeDataImageUri(raw));
    if(/^(javascript|vbscript):/i.test(compact)) return false;
    if(/^https?:\/\//i.test(raw)) return true;
    if(/^(mailto:|tel:|message:)/i.test(raw)) return true;
    if(img && /^api\//i.test(raw)) return true;
    if(!img && (/^api\//i.test(raw) || /^#/.test(raw) || _isInternalSessionHref(raw))) return true;
    return false;
  }
  function _attrs(raw){
    const out={};
    String(raw||'').replace(/([a-zA-Z0-9:_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g,(_,k,dq,sq,bare)=>{
      out[String(k).toLowerCase()]=dq!==undefined?dq:(sq!==undefined?sq:(bare!==undefined?bare:''));
      return '';
    });
    return out;
  }
  function _cls(v, allowed){
    const got=String(v||'').split(/\s+/).filter(c=>allowed.includes(c));
    return got.length?` class="${esc(got.join(' '))}"`:'';
  }
  function _tag(tag){
    const m=String(tag||'').match(/^<\s*(\/)?\s*([a-zA-Z][\w:-]*)([\s\S]*?)(\/)?\s*>$/);
    if(!m) return esc(tag);
    const closing=!!m[1];
    const name=m[2].toLowerCase();
    const rawAttrs=m[3]||'';
    const plain=['strong','em','del','pre','h1','h2','h3','h4','h5','h6','ul','ol','table','thead','tbody','tr','th','td','blockquote','p','br','hr'];
    if(closing) return plain.includes(name)||['a','div','span','li','code'].includes(name)?`</${name}>`:'';
    if(name==='code'){
      const a=_attrs(rawAttrs);
      const cls=/^language-[a-z0-9_+-]+$/i.test(a.class||'')?` class="${esc(a.class)}"`:'';
      return `<code${cls}>`;
    }
    if(plain.includes(name)) return `<${name}>`;
    const a=_attrs(rawAttrs);
    if(name==='li'){
      const value=/^\d+$/.test(a.value||'')?` value="${esc(a.value)}"`:'';
      const style=(a.style||'').replace(/\s+/g,'').toLowerCase()==='margin-left:16px'?` style="margin-left:16px"`:'';
      return `<li${value}${style}>`;
    }
    if(name==='span'){
      return `<span${_cls(a.class,['task-done','task-todo','katex-inline'])}${a['data-katex']==='inline'?' data-katex="inline"':''}>`;
    }
    if(name==='div'){
      const cls=_cls(a.class,['pre-header','mermaid-block','katex-block']);
      const mermaid=a['data-mermaid-id']?` data-mermaid-id="${esc(a['data-mermaid-id'])}"`:'';
      const katex=a['data-katex']==='display'?' data-katex="display"':'';
      return `<div${cls}${mermaid}${katex}>`;
    }
    if(name==='a'){
      if(!_isSafeUrl(a.href,false)) return '<a>';
      const target=a.target==='_blank'?' target="_blank"':'';
      const rel=a.rel==='noopener'?' rel="noopener"':'';
      const cls=_cls(a.class,['msg-media-link','skill-linked-file','skill-file-back','session-link']);
      const download=a.download?` download="${esc(a.download)}"`:'';
      return `<a${cls} href="${esc(_safeAttrValue(a.href))}"${target}${rel}${download}>`;
    }
    if(name==='img'){
      if(!_isSafeUrl(a.src,true)) return '';
      const cls=_cls(a.class,['msg-media-img']);
      const alt=` alt="${esc(_safeAttrValue(a.alt||''))}"`;
      const loading=a.loading==='lazy'?' loading="lazy"':'';
      return `<img${cls} src="${esc(_safeAttrValue(a.src))}"${alt}${loading}>`;
    }
    return '';
  }
  s=s.replace(/<\/?[a-z][^>]*>/gi,tag=>_tag(tag));
  // Incomplete raw tags must not survive until paragraph wrapping, where the
  // renderer's generated </p> could provide a closing ">" and turn them into
  // executable HTML in innerHTML (for example: <img src=x onerror=...//).
  s=s.replace(/<[a-zA-Z][\w:-]*[^>\n]*$/gm,tag=>esc(tag));
  // Autolink: convert plain URLs to clickable links.
  // Stash <a>, <img> and <pre> blocks so autolink never runs inside them.
  const _al_stash=[];
  s=s.replace(/(<a\b[^>]*>[\s\S]*?<\/a>|<img\b[^>]*>|<pre\b[^>]*>[\s\S]*?<\/pre>)/g,m=>{_al_stash.push(m);return `\x00B${_al_stash.length-1}\x00`;});
  s=s.replace(/(https?:\/\/[^\s<>"'\)\]]+)/g,(url)=>{
    // Strip trailing punctuation that was likely not part of the URL
    const trail=url.match(/[.,;:!?)]$/)?url.slice(-1):'';
    const clean=trail?url.slice(0,-1):url;
    return `<a href="${clean}" target="_blank" rel="noopener">${esc(clean)}</a>${trail}`;
  });
  s=s.replace(/\x00B(\d+)\x00/g,(_,i)=>_al_stash[+i]);
  // Restore math stash → katex placeholder spans/divs
  // These will be rendered by renderKatexBlocks() after DOM insertion
  s=s.replace(/\x00M(\d+)\x00/g,(_,i)=>{
    const item=math_stash[+i];
    if(item.type==='display'){
      return `<div class="katex-block" data-katex="display">${esc(item.src)}</div>`;
    }
    return `<span class="katex-inline" data-katex="inline">${esc(item.src)}</span>`;
  });
  // Restore fenced block stash (\x00P) → <pre><code> HTML.
  // Happens AFTER all markdown passes (lists, headings, tables, etc.) so
  // diff/patch content inside code blocks is never misinterpreted as markdown.
  // The _pre_stash below then protects these blocks from paragraph splitting.
  s=s.replace(/\x00P(\d+)\x00/g,(_,i)=>_preBlock_stash[+i]);
  // Stash rendered <pre> blocks (with optional pre-header div) and mermaid/katex
  // divs before paragraph splitting so \n inside code blocks is never replaced
  // with <br>. Token \x00E (next free after B D F G L M C O A).
  // Fixes #745: code blocks collapse to single line when not preceded by blank line.
  const _pre_stash=[];
  // #1463 / #1618: regex must match <pre> with ANY attributes — PR #484 added
  // <pre class="tree-raw-view"> for JSON/YAML and <pre class="diff-block"> for
  // diff/patch which the literal-<pre> shape missed. Newlines inside those
  // blocks were falling through to the paragraph wrap below and getting
  // converted to <br>, causing the YAML/JSON/diff collapse. PR #1516's CSS
  // fix targeted the wrong layer (Prism token white-space) — by the time it
  // ran, the \n had already been replaced. The CSS rule is kept as defense
  // in depth.
  s=s.replace(/(<div class="pre-header">[\s\S]*?<\/div>)?<pre[^>]*>[\s\S]*?<\/pre>|<div class="(mermaid-block|katex-block)"[\s\S]*?<\/div>/g,m=>{
    _pre_stash.push(m);
    return '\x00E'+(_pre_stash.length-1)+'\x00';
  });
  const parts=s.split(/\n{2,}/);
  s=parts.map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-6]|ul|ol|table|pre|hr|blockquote)|^\x00[EQ]/.test(p))return p;return `<p>${p.replace(/\n/g,'<br>')}</p>`;}).join('\n');
  s=s.replace(/\x00E(\d+)\x00/g,(_,i)=>_pre_stash[+i]);
  // ── Restore MEDIA stash → inline images or download links ─────────────────
  s=s.replace(/\x00D(\d+)\x00/g,(_,i)=>_inlineMediaHtmlForRef(media_stash[+i]));

  // ── End MEDIA restore ──────────────────────────────────────────────────────
  // Restore blockquote stash. Done last so the inner HTML (already produced
  // by the recursive renderMd in the pre-pass) is dropped into the final
  // string verbatim — no further passes can mangle it.
  s=s.replace(/\x00Q(\d+)\x00/g,(_,i)=>_bq_stash[+i]);
  return s;
}

function _stripAttachedFilesMarkerForDisplay(text){
  return String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
}

function setStatus(t){
  if(!t)return;
  showToast(t, 4000);
}

function setComposerStatus(t){
  const el=$('composerStatus');
  if(!el)return;
  const statusHidden=!!(window._composerControlVisibility&&window._composerControlVisibility.hide_composer_status);
  if(statusHidden){
    el.style.display='none';
    el.textContent='';
    return;
  }
  if(!t){
    el.style.display='none';
    el.textContent='';
    return;
  }
  // Defensive reset: a stale hidden class should never block live status text.
  el.classList.remove('composer-control-hidden');
  el.removeAttribute('aria-hidden');
  el.textContent=t;
  el.style.display='';
}

let _composerLockState=null;
let _compressionPlaceholderSaved=null;

function lockComposerForClarify(placeholderText){
  const input=$('msg');
  if(!input) return;
  // Save the current composer text as a server-side draft before locking,
  // so the user's draft is preserved if they switch sessions while a clarify
  // card is active (and survives page refresh / syncs across clients).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraftNow === 'function') {
    _saveComposerDraftNow(sid, input.value || '', S.pendingFiles ? [...S.pendingFiles] : []);
  }
  if(!_composerLockState){
    _composerLockState={
      disabled: input.disabled,
      placeholder: input.placeholder,
    };
  }
  input.disabled=true;
  if(placeholderText) input.placeholder=placeholderText;
  updateSendBtn();
}

function unlockComposerForClarify(){
  const input=$('msg');
  if(!input) return;
  if(_composerLockState){
    input.disabled=!!_composerLockState.disabled;
    if(typeof _composerLockState.placeholder==='string'){
      input.placeholder=_composerLockState.placeholder;
    }
    _composerLockState=null;
  }else{
    input.disabled=false;
  }
  updateSendBtn();
}

function _composerHasContent(){
  const msg=$('msg');
  return !!((msg&&msg.value.trim().length>0)||S.pendingFiles.length>0||(typeof window._hasPendingSelections==='function'&&window._hasPendingSelections()));
}

function _getExplicitBusyCommandAction(text){
  const trimmed=(text||'').trim();
  if(!trimmed.startsWith('/')) return null;
  const body=trimmed.slice(1);
  const name=(body.split(/\s+/)[0]||'').toLowerCase();
  const args=body.slice(name.length).trim();
  if(!args) return null;
  if(name==='queue') return 'queue';
  if(name==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(name==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return null;
}

function getComposerPrimaryAction(){
  const msg=$('msg');
  const hasContent=_composerHasContent();
  const locked=!!(msg&&msg.disabled);
  if(locked) return 'disabled';
  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  const isBusy=!!S.busy||compressionRunning;
  if(!isBusy) return hasContent?'send':'disabled';
  if(!hasContent){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'stop';
    if(compressionRunning) return 'queue';
    return 'disabled';
  }
  const explicitAction=_getExplicitBusyCommandAction(msg&&msg.value);
  if(explicitAction) return explicitAction;
  const defaultMessageMode=window._defaultMessageMode||'steer';
  if(defaultMessageMode==='steer'){
    if(S.activeStreamId&&typeof _trySteer==='function') return 'steer';
    return 'queue';
  }
  if(defaultMessageMode==='interrupt'){
    if(S.activeStreamId&&typeof cancelStream==='function') return 'interrupt';
    return 'queue';
  }
  return 'queue';
}

function _applyBusyComposerPlaceholder(){
  const input=$('msg');
  if(!input) return;
  if(_compressionPlaceholderSaved!==null) return;
  if(input.disabled) return;
  if(_composerHasContent()) return;
  const idlePlaceholder='Message '+assistantDisplayName()+'\u2026';
  if(!window._showBusyPlaceholderHint||!S.busy){
    input.placeholder=idlePlaceholder;
    return;
  }
  const busyMode=window._defaultMessageMode||'steer';
  const busyPlaceholderKey=busyMode==='interrupt'
    ? 'composer_placeholder_busy_interrupt'
    : busyMode==='steer'
      ? 'composer_placeholder_busy_steer'
      : 'composer_placeholder_busy_queue';
  const busyPlaceholderFallback=busyMode==='interrupt'
    ? 'Enter = interrupt | /queue | /background | /steer'
    : busyMode==='steer'
      ? 'Enter = steer | /queue | /background | /interrupt'
      : 'Enter = queue | /interrupt | /background | /steer';
  input.placeholder=typeof t==='function'
    ? (t(busyPlaceholderKey)||busyPlaceholderFallback)
    : busyPlaceholderFallback;
}

function _setComposerPrimaryButtonIcon(btn,action){
  // Queue/interrupt/steer icons are inline Lucide SVGs (ISC):
  // https://lucide.dev/icons/
  const icons={
    send:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
    queue:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 5H3"/><path d="M16 12H3"/><path d="M9 19H3"/><path d="m16 16-3 3 3 3"/><path d="M21 5v12a2 2 0 0 1-2 2h-6"/></svg>',
    interrupt:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 4v16"/><path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"/></svg>',
    steer:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/></svg>',
    stop:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"></rect></svg>',
    disabled:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>'
  };
  const next=icons[action]||icons.send;
  if(btn.innerHTML!==next) btn.innerHTML=next;
}

function updateSendBtn(){
  const btn=$('btnSend');
  if(!btn){
    if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
    return;
  }
  const action=getComposerPrimaryAction();
  btn.dataset.action=action;
  btn.classList.toggle('stop',action==='stop');
  btn.classList.toggle('queue',action==='queue');
  btn.classList.toggle('interrupt',action==='interrupt');
  btn.classList.toggle('steer',action==='steer');
  const _tt=(key,fb)=>{if(typeof t!=='function')return fb;const val=t(key);return val===key?fb:(val||fb);};
  let _btnTitle;
  if(action==='disabled'){
    const _dmsg=$('msg');
    if(_dmsg&&_dmsg.disabled) _btnTitle=_tt('composer_disabled_clarify','Respond to the clarification request');
    else _btnTitle=_tt('composer_disabled_empty','Type a message to send');
  }else if(action==='queue'&&typeof isCompressionUiRunning==='function'&&isCompressionUiRunning()){
    _btnTitle=_tt('composer_compression_will_queue','Type a message — it will queue and send after compression');
  }else{
    const _tmap={send:'Send message',queue:'Queue message',interrupt:'Interrupt and send',steer:'Steer current response',stop:'Stop generation'};
    _btnTitle=_tt('composer_'+action,_tmap[action]||'Send message');
  }
  btn.title=_btnTitle;
  btn.setAttribute('aria-label',_btnTitle);
  _setComposerPrimaryButtonIcon(btn,action);
  if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
  // Single primary action button: while busy/no-draft it becomes the red Stop
  // action; while busy with a draft it reflects queue/interrupt/steer.
  btn.style.display='';
  btn.disabled=action==='disabled';
  if(action!=='disabled'&&!btn.classList.contains('visible')){
    btn.classList.remove('visible');
    requestAnimationFrame(()=>btn.classList.add('visible'));
  } else if(action==='disabled'){
    btn.classList.remove('visible');
  }
}

async function handleComposerPrimaryAction(){
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  const action=typeof getComposerPrimaryAction==='function'?getComposerPrimaryAction():'send';
  if(action==='disabled') return;
  if(action==='stop'){
    if(typeof cancelStream==='function') await cancelStream('composer-stop');
    return;
  }
  await send();
}

function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
    setStatus('');
    setComposerStatus('');
    const sid=_queueDrainSid||(S.session&&S.session.session_id);
    stateBindings._queueDrainSid=null;
    updateQueueBadge(sid);
    // Drain one queued message for the finished session after UI settles
    const _isViewedSid=!S.session||sid===S.session.session_id;
    const next=sid&&_isViewedSid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        // Guard: if the user switched away from the drain session during
        // the 120ms settle window, the queued message must NOT go to the
        // wrong chat.  Put it back into the original session's queue and
        // skip sending — it will drain when the user returns to that session
        // or when its next stream completes while it is the active view.
        if(S.session&&S.session.session_id!==sid){
          queueSessionMessage(sid,next);
          updateQueueBadge(sid);
          return;
        }
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
        }
        if(next.model_provider&&S.session) S.session.model_provider=next.model_provider;
        if(next.model&&S.session){
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'),S.session.model_provider||null);
          if(typeof syncModelChip==='function') syncModelChip();
        }
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
}

// ── Queue chip display (Codex Desktop pattern) ─────────────────────────────
// Queued messages appear as chips inside #queueChips (above the textarea)
// while pending. When the session fires the queued message it becomes a
// normal user bubble in the chat — the chip is removed at drain time.
const _queueRenderKeys={};  // per-session fingerprint to avoid redundant rebuilds
const _queueCollapsed={};   // per-session: true when user explicitly collapsed the card
let _queueRenderEpoch=0;
function _clearQueueCardDisplay(sid){
  const card=document.getElementById('queueCard');
  const chips=document.getElementById('queueChips');
  if(sid) delete _queueRenderKeys[sid];
  if(card) card.classList.remove('visible');
  if(chips){
    const _chips=chips;
    const _card=card;
    const _sid=String(sid||'');
    const _epoch=_chips.getAttribute('data-queue-render-epoch')||'';
    setTimeout(()=>{
      if((_card&&!_card.classList.contains('visible'))||!_card){
        if((_chips.getAttribute('data-queue-render-sid')||'')===_sid&&(_chips.getAttribute('data-queue-render-epoch')||'')===_epoch){
          _chips.innerHTML='';
        }
      }
    },360);
  }
  const _msgsEl=document.getElementById('messages');
  if(_msgsEl) _msgsEl.classList.remove('queue-open');
  _updateQueuePill(sid,0);
}

function _renderQueueChips(sid){
  const card=document.getElementById('queueCard');
  const inner=document.getElementById('queueChips');
  if(!card||!inner) return;
  const q=_getSessionQueue(sid,false);
  const key=q.map(e=>{const t=e&&(e.text||e.message||e.content||'');return(e&&e._queued_at||0)+':'+t.length+':'+t.slice(0,20);}).join('|');
  if(key===(_queueRenderKeys[sid]||'')&&key!='') return;
  // Skip re-render if user is actively editing inside the queue panel
  if(inner.contains(document.activeElement)&&document.activeElement!==inner) return;
  _queueRenderKeys[sid]=key;
  inner.setAttribute('data-queue-render-sid',sid);
  inner.setAttribute('data-queue-render-epoch',String(++_queueRenderEpoch));
  inner.innerHTML='';
  if(!q.length){
    card.classList.remove('visible');
    const _msgs=document.getElementById('messages');
    if(_msgs) _msgs.classList.remove('queue-open');
    return;
  }
  // Respect user-collapsed state — don't reopen if user explicitly hid the card
  if(_queueCollapsed[sid]){
    // Update chips content without showing card (so data is fresh if user re-expands)
    inner.innerHTML='';
    // fall through to render rows into inner but skip making card visible
  } else {
    card.classList.add('visible');
  }
  // Push messages area up so content isn't hidden behind the flyout
  const _msgs=document.getElementById('messages');
  if(_msgs&&!_queueCollapsed[sid]){
    _msgs.classList.add('queue-open');
    // Measure after 350ms transition completes (not mid-animation — height would be wrong)
    setTimeout(()=>{
      if(!card.classList.contains('visible')) return;
      const h=card.getBoundingClientRect().height;
      if(h>0) _msgs.style.setProperty('--queue-card-height', h+'px');
      if(typeof scrollIfPinned==='function') scrollIfPinned();
    }, 360);
  }

  function _saveAndRefresh(){
    const liveQ=_getSessionQueue(sid,false);
    if(!liveQ.length){delete SESSION_QUEUES[sid];_clearPersistedSessionQueue(sid);}
    else{SESSION_QUEUES[sid]=[...liveQ];_persistSessionQueueStorage(sid,liveQ);}
    delete _queueRenderKeys[sid];
    updateQueueBadge(sid);
  }

  // Header (2+ items)
  if(q.length>1){
    const header=document.createElement('div');
    header.className='queue-card-header';
    const lbl=document.createElement('span');
    lbl.textContent=typeof t==='function'?t('queued_count',q.length):(q.length===1?'1 queued':`${q.length} queued`);
    lbl.title='Sends automatically after the current response completes';
    const actions=document.createElement('span');
    actions.className='queue-card-header-actions';
    const hasFiles=q.some(e=>e&&Array.isArray(e.files)&&e.files.length>0);
    const mergeBtn=document.createElement('button');
    mergeBtn.className='queue-card-btn';
    mergeBtn.title='Combine all into one message'+(hasFiles?' — attachments will be removed':'');
    mergeBtn.innerHTML=li('layers',12)+'Combine';
    mergeBtn.onclick=()=>{
      const _doMerge=(snapshot)=>{
        const combined=snapshot.map(e=>e&&(e.text||e.message||e.content||'')).filter(Boolean).join('\n\n');
        const liveQ=_getSessionQueue(sid,false);
        const first=snapshot.find(e=>e)||{};
        const firstFiles=(snapshot.find(e=>e&&Array.isArray(e.files)&&e.files.length)||{files:[]}).files;
        liveQ.length=0;liveQ.push({text:combined,files:firstFiles,model:first.model||'',model_provider:first.model_provider||null,_queued_at:Date.now()});
        SESSION_QUEUES[sid]=liveQ;
        _persistSessionQueueStorage(sid,liveQ);
        delete _queueRenderKeys[sid];
        updateQueueBadge(sid);
      };
      if(hasFiles){
        if(typeof showToast==='function') showToast('Attachments on queued items will be removed',2600,'warning');
      }
      // Merge from current live queue (no delay — snapshot + defer caused data-loss races)
      _doMerge([..._getSessionQueue(sid,false)]);
    };
    const clearBtn=document.createElement('button');
    clearBtn.className='queue-card-icon-btn';
    clearBtn.title='Clear all queued messages';
    clearBtn.setAttribute('aria-label','Clear all queued messages');
    clearBtn.innerHTML=li('x',13);
    clearBtn.onclick=()=>{q.length=0;_saveAndRefresh();};
    actions.appendChild(mergeBtn);
    actions.appendChild(clearBtn);
    // Hide button — collapses flyout entirely; queue pill re-shows it
    const hideBtn=document.createElement('button');
    hideBtn.className='queue-card-icon-btn';
    hideBtn.title='Hide queue (click the queue pill to show again)';
    hideBtn.setAttribute('aria-label','Hide queue panel');
    hideBtn.innerHTML=li('chevron-down',14);
    hideBtn.onclick=()=>{
      _queueCollapsed[sid]=true;
      card.classList.remove('visible');
      // Read live count at click time (not stale closure q)
      _updateQueuePill(sid,_getSessionQueue(sid,false).length);
    };
    actions.appendChild(hideBtn);
    header.appendChild(lbl);
    header.appendChild(actions);
    inner.appendChild(header);
  }

  let _dragTs=null;  // use _queued_at timestamp — survives re-renders, not an index
  q.forEach((entry,i)=>{
    const _entryTs=entry&&entry._queued_at;
    const entryText=entry&&(entry.text||entry.message||entry.content||'');
    const _files=entry&&Array.isArray(entry.files)?entry.files.filter(Boolean):[];
    const row=document.createElement('div');
    row.className='queue-card-row';
    row.setAttribute('role','listitem');
    row.setAttribute('draggable','true');
    row.ondragstart=(e)=>{if(_entryTs==null) return;_dragTs=_entryTs;row.style.opacity='.4';e.dataTransfer.effectAllowed='move';};
    row.ondragend=()=>{row.style.opacity='';};
    row.ondragover=(e)=>{e.preventDefault();row.style.background='var(--hover-bg)';};
    row.ondragleave=()=>{row.style.background='';};
    row.ondrop=(e)=>{
      e.preventDefault();row.style.background='';
      if(_dragTs!=null&&_dragTs!==_entryTs){
        const fromIdx=q.findIndex(e=>e&&e._queued_at===_dragTs);
        if(fromIdx!==-1&&fromIdx!==i){const moved=q.splice(fromIdx,1)[0];q.splice(i,0,moved);}
        _dragTs=null;_saveAndRefresh();
      }
    };
    // Drag handle
    const drag=document.createElement('span');
    drag.className='queue-card-drag';
    drag.setAttribute('aria-hidden','true');
    drag.innerHTML=typeof li==='function'?li('list-todo',13):'≡';
    // Inline-editable text
    const msgSpan=document.createElement('span');
    msgSpan.className='queue-card-text';
    msgSpan.setAttribute('contenteditable','true');
    msgSpan.setAttribute('role','textbox');
    msgSpan.setAttribute('aria-label','Queued message — edit in place');
    msgSpan.textContent=entryText||(_files.length?'':'—');
    msgSpan.setAttribute('draggable','false');
    msgSpan.onfocus=()=>{msgSpan.style.overflow='auto';msgSpan.style.whiteSpace='pre-wrap';msgSpan.style.textOverflow='clip';};
    msgSpan.onblur=()=>{
      msgSpan.style.overflow='';msgSpan.style.whiteSpace='';msgSpan.style.textOverflow='';
      const newText=msgSpan.textContent.trim();
      if(newText===''&&!_files.length){ msgSpan.textContent=entryText||'—'; return; }
      if(newText!==entryText){
        const liveQ=_getSessionQueue(sid,false);
        const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
        if(idx!==-1){
          liveQ[idx]={...liveQ[idx],text:newText};
          _persistSessionQueueStorage(sid,liveQ);
          delete _queueRenderKeys[sid];
          updateQueueBadge(sid);
        }
      }
    };
    msgSpan.onkeydown=(e)=>{if(e.key==='Enter'){e.preventDefault();msgSpan.blur();}if(e.key==='Escape'){msgSpan.textContent=entryText||'—';msgSpan.blur();}};
    // Compact badges (files, model, profile)
    const badges=document.createElement('span');
    badges.className='queue-card-badges';
    if(_files.length>0){
      const fb=document.createElement('span');
      fb.className='queue-card-file-badge';
      fb.title=_files.map(f=>f&&f.name||'file').join(', ');
      fb.innerHTML=li('paperclip',11)+_files.length;
      badges.appendChild(fb);
    }
    const _model=entry&&entry.model;
    if(_model){
      const mb=document.createElement('span');
      mb.title='Model: '+_model;
      // Use the app's friendly label system if available
      const _modelLabel=(typeof _dynamicModelLabels!=='undefined'&&_dynamicModelLabels[_model])
        ||_model.split('/').pop().replace(/^(gpt-|claude-3\.?5?-|claude-|gemini-)/,'').replace(/-\d{4}-\d{2}-\d{2}$/,'').slice(0,12);
      mb.textContent=_modelLabel;
      badges.appendChild(mb);
    }
    // Profile badge removed — drain cannot server-switch profiles so badge was misleading
    // Delete button
    const delBtn=document.createElement('button');
    delBtn.className='queue-card-icon-btn';
    delBtn.setAttribute('aria-label',typeof t==='function'?t('queued_cancel'):'Remove queued message');
    delBtn.setAttribute('draggable','false');
    delBtn.title='Remove from queue';
    delBtn.innerHTML=li('x',13);
    delBtn.onclick=()=>{
      const liveQ=_getSessionQueue(sid,false);
      const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
      if(idx!==-1) liveQ.splice(idx,1);
      if(!liveQ.length){delete SESSION_QUEUES[sid];_clearPersistedSessionQueue(sid);}
      else{SESSION_QUEUES[sid]=[...liveQ];_persistSessionQueueStorage(sid,liveQ);}
      delete _queueRenderKeys[sid];
      updateQueueBadge(sid);
    };
    row.appendChild(drag);
    row.appendChild(msgSpan);
    if(badges.childNodes.length) row.appendChild(badges);
    row.appendChild(delBtn);
    inner.appendChild(row);
  });
}

function _updateQueuePill(sid,count){
  const pill=document.getElementById('queuePill');
  if(!pill) return;
  const pillOuter=pill.parentElement;  // .queue-pill-outer — same wrapper as .queue-card
  const card=document.getElementById('queueCard');
  const flyoutVisible=card&&card.classList.contains('visible');
  if(count>0&&!flyoutVisible){
    const label=typeof t==='function'?t('queued_count',count):(count===1?'1 queued':`${count} queued`);
    pill.innerHTML=(typeof li==='function'?li('list-todo',12):'')+
      `<span class="queue-pill-count">${label}</span>`+
      `<span class="queue-pill-chevron">`+(typeof li==='function'?li('chevron-up',12):'▲')+`</span>`;
    pill.title='Show queued messages';
    if(pillOuter) pillOuter.classList.add('show');
    pill.onclick=()=>{
      delete _queueCollapsed[sid];
      const c=document.getElementById('queueCard');
      if(c){
        c.classList.add('visible');
        setTimeout(()=>{
          const firstFocusable=c.querySelector('.queue-card-text, .queue-card-icon-btn');
          if(firstFocusable) firstFocusable.focus();
        }, 360);
      }
      if(pillOuter) pillOuter.classList.remove('show');
      if(typeof scrollIfPinned==='function') scrollIfPinned();
    };
  } else {
    if(pillOuter) pillOuter.classList.remove('show');
    pill.onclick=null;
  }
}

function updateQueueBadge(sessionId){
  const sid=sessionId||(S.session&&S.session.session_id);
  const count=sid?getQueuedSessionCount(sid):0;
  if(count>0&&S.session&&sid===S.session.session_id){
    _renderQueueChips(sid);
    // If card is visible, hide pill. If card is collapsed, update pill count.
    const _cardEl=document.getElementById('queueCard');
    _updateQueuePill(sid,(_cardEl&&_cardEl.classList.contains('visible'))?0:count);
  } else {
    // Always clean up per-session data
    if(sid){delete _queueRenderKeys[sid];delete _queueCollapsed[sid];}
    // Only wipe global DOM if this is the currently active session
    const isActive=S.session&&sid===S.session.session_id;
    if(isActive){
      _clearQueueCardDisplay(sid);
    }
  }
}
const TOAST_DEFAULT_MS=2800;
const TOAST_ERROR_DEFAULT_MS=20000;
function clearToastDismissTimer(el){if(!el)return;clearTimeout(el._t);el._t=null;}
function setToastDismissTimer(el,duration){if(!el)return;clearToastDismissTimer(el);el._t=setTimeout(()=>{el.classList.remove('show');},duration);}
function dismissToast(btnOrEl){
  const el=btnOrEl&&btnOrEl.closest?btnOrEl.closest('#toast'):(btnOrEl&&btnOrEl.id==='toast'?btnOrEl:null);
  if(!el)return;
  clearToastDismissTimer(el);
  el.classList.remove('show');
}
function copyToastText(btn){
  const el=btn&&btn.closest?btn.closest('#toast'):null;
  const text=el?(el.dataset.toastMessage||el.textContent||''):'';
  const done=()=>{const old=btn.textContent;btn.textContent='Copied';setTimeout(()=>{btn.textContent=old;},1200);};
  _copyText(text).then(done).catch(()=>{});
}
function showToast(msg,ms,type){
  const el=$('toast');if(!el)return;
  const s=String(msg==null?'':msg);let t=type;
  if(!t){const low=s.toLowerCase();if(/fail|error|denied|invalid|unavailable|no active|no workspace match|no model match|no personalities/.test(low))t='error';else if(/warn|queued|takes effect|skipped|fallback/.test(low))t='warning';else if(/saved|created|imported|restored|switched|set to|updated|duplicated|moved to|renamed|deleted|complete|pinned|archived|cleared|stopped/.test(low))t='success';else t='info';}
  const duration=(ms==null)?(t==='error'?TOAST_ERROR_DEFAULT_MS:TOAST_DEFAULT_MS):ms;
  el.className='toast show '+t;
  el.dataset.toastMessage=s;
  if(t==='error') el.innerHTML=`<span class="toast-message">${esc(s)}</span><button class="toast-copy" type="button" data-toast-copy="1" onclick="copyToastText(this);event.stopPropagation()">Copy</button><button class="toast-dismiss" type="button" aria-label="Dismiss error toast" data-toast-dismiss="1" onclick="dismissToast(this);event.stopPropagation()">Dismiss</button>`;
  else el.textContent=s;
  el.onmouseenter=()=>clearToastDismissTimer(el);
  el.onmouseleave=()=>setToastDismissTimer(el,duration);
  el.onfocusin=()=>clearToastDismissTimer(el);
  el.onfocusout=()=>setToastDismissTimer(el,duration);
  el.onclick=t==='error'?null:()=>dismissToast(el);
  setToastDismissTimer(el,duration);
}



export {
  renderMd,
  _stripAttachedFilesMarkerForDisplay,
  setStatus,
  setComposerStatus,
  lockComposerForClarify,
  unlockComposerForClarify,
  _composerHasContent,
  _getExplicitBusyCommandAction,
  getComposerPrimaryAction,
  _applyBusyComposerPlaceholder,
  _setComposerPrimaryButtonIcon,
  updateSendBtn,
  setBusy,
  _clearQueueCardDisplay,
  _renderQueueChips,
  _updateQueuePill,
  updateQueueBadge,
  clearToastDismissTimer,
  setToastDismissTimer,
  dismissToast,
  copyToastText,
  showToast,
  handleComposerPrimaryAction,
  _queueRenderKeys,
  _queueCollapsed,
  TOAST_DEFAULT_MS,
  TOAST_ERROR_DEFAULT_MS,
  _composerLockState,
  _compressionPlaceholderSaved,
  _queueRenderEpoch,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderMd: { enumerable: true, get: () => renderMd, set: (value) => { renderMd = value; } },
  _stripAttachedFilesMarkerForDisplay: { enumerable: true, get: () => _stripAttachedFilesMarkerForDisplay, set: (value) => { _stripAttachedFilesMarkerForDisplay = value; } },
  setStatus: { enumerable: true, get: () => setStatus, set: (value) => { setStatus = value; } },
  setComposerStatus: { enumerable: true, get: () => setComposerStatus, set: (value) => { setComposerStatus = value; } },
  lockComposerForClarify: { enumerable: true, get: () => lockComposerForClarify, set: (value) => { lockComposerForClarify = value; } },
  unlockComposerForClarify: { enumerable: true, get: () => unlockComposerForClarify, set: (value) => { unlockComposerForClarify = value; } },
  _composerHasContent: { enumerable: true, get: () => _composerHasContent, set: (value) => { _composerHasContent = value; } },
  _getExplicitBusyCommandAction: { enumerable: true, get: () => _getExplicitBusyCommandAction, set: (value) => { _getExplicitBusyCommandAction = value; } },
  getComposerPrimaryAction: { enumerable: true, get: () => getComposerPrimaryAction, set: (value) => { getComposerPrimaryAction = value; } },
  _applyBusyComposerPlaceholder: { enumerable: true, get: () => _applyBusyComposerPlaceholder, set: (value) => { _applyBusyComposerPlaceholder = value; } },
  _setComposerPrimaryButtonIcon: { enumerable: true, get: () => _setComposerPrimaryButtonIcon, set: (value) => { _setComposerPrimaryButtonIcon = value; } },
  updateSendBtn: { enumerable: true, get: () => updateSendBtn, set: (value) => { updateSendBtn = value; } },
  setBusy: { enumerable: true, get: () => setBusy, set: (value) => { setBusy = value; } },
  _clearQueueCardDisplay: { enumerable: true, get: () => _clearQueueCardDisplay, set: (value) => { _clearQueueCardDisplay = value; } },
  _renderQueueChips: { enumerable: true, get: () => _renderQueueChips, set: (value) => { _renderQueueChips = value; } },
  _updateQueuePill: { enumerable: true, get: () => _updateQueuePill, set: (value) => { _updateQueuePill = value; } },
  updateQueueBadge: { enumerable: true, get: () => updateQueueBadge, set: (value) => { updateQueueBadge = value; } },
  clearToastDismissTimer: { enumerable: true, get: () => clearToastDismissTimer, set: (value) => { clearToastDismissTimer = value; } },
  setToastDismissTimer: { enumerable: true, get: () => setToastDismissTimer, set: (value) => { setToastDismissTimer = value; } },
  dismissToast: { enumerable: true, get: () => dismissToast, set: (value) => { dismissToast = value; } },
  copyToastText: { enumerable: true, get: () => copyToastText, set: (value) => { copyToastText = value; } },
  showToast: { enumerable: true, get: () => showToast, set: (value) => { showToast = value; } },
  handleComposerPrimaryAction: { enumerable: true, get: () => handleComposerPrimaryAction, set: (value) => { handleComposerPrimaryAction = value; } },
  _queueRenderKeys: { enumerable: true, get: () => _queueRenderKeys },
  _queueCollapsed: { enumerable: true, get: () => _queueCollapsed },
  TOAST_DEFAULT_MS: { enumerable: true, get: () => TOAST_DEFAULT_MS },
  TOAST_ERROR_DEFAULT_MS: { enumerable: true, get: () => TOAST_ERROR_DEFAULT_MS },
  _composerLockState: { enumerable: true, get: () => _composerLockState, set: (value) => { _composerLockState = value; } },
  _compressionPlaceholderSaved: { enumerable: true, get: () => _compressionPlaceholderSaved, set: (value) => { _compressionPlaceholderSaved = value; } },
  _queueRenderEpoch: { enumerable: true, get: () => _queueRenderEpoch, set: (value) => { _queueRenderEpoch = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
