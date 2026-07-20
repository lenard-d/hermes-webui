import { _inlineMediaHtmlForRef, _isSafeDataImageUri, _mdImageHtml } from './media-and-quota.js';
import { _isBacktickFenceClose, _matchBacktickFenceLine, esc } from './state.js';

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


export { renderMd };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderMd: { enumerable: true, get: () => renderMd, set: (value) => { renderMd = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
