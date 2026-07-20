// Loaded before stream.js. Owns the per-stream incremental markdown parser,
// MEDIA tail buffering, safe link projection, text fade and render scheduler.
var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

function createStreamRenderer(options={}){
  const readState=typeof options.readState==='function'?options.readState:()=>({});
  const _updateLiveThinkingCard=typeof options.updateLiveThinking==='function'?options.updateLiveThinking:()=>{};
  const _upsertAnchorProcessProse=typeof options.upsertAnchorProse==='function'?options.upsertAnchorProse:()=>null;
  const _syncLiveWorklogReasonsForAnchor=typeof options.syncWorklogReasons==='function'?options.syncWorklogReasons:()=>{};
  const scrollIfPinned=typeof options.scrollPinned==='function'?options.scrollPinned:()=>{};
  const _throttledSnapshotLiveTurn=typeof options.snapshotLiveTurn==='function'?options.snapshotLiveTurn:()=>{};
  const resetSegmentState=typeof options.resetSegmentState==='function'?options.resetSegmentState:()=>{};
  let _smdParser=null;
  let _smdWrittenLen=0;
  let _smdWrittenText='';
  let _streamingKatexTimer=null;
  let _smdReconnect=!!options.reconnecting;
  let _pendingRafHandle=null;
  let _renderPending=false;
  let _streamFadeVisibleText='';
  let _streamFadeLastTickMs=0;
  let _streamFadeWordCarry=0;
  let _streamFadeStartedAt=0;
  let _streamFadeLastTargetWords=0;
  let _streamFadeLastArrivalMs=0;
  let _streamFadeArrivalWps=0;
  let _streamFadeLatestAnimationEndAt=0;
  let _streamFadeVisibleWords=0;
  let _streamFadeHoldUntilMs=0;
  let _streamFadeCurrentMs=620;
  let _streamFadeDomText='';
  let _streamFadeReduceMotionMql=null;
  let _streamFadeReduceMotion=false;
  let _streamFadeReduceMotionOnChange=null;
  const _STREAM_FADE_MS=620;
  const _STREAM_FADE_MAX_MS=900;
  const _STREAM_FADE_DONE_MAX_MS=1000;
  const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400;

  function _stripXmlToolCalls(s){
    // Strip <function_calls>...</function_calls> blocks (DeepSeek XML tool syntax).
    // These are processed as tool calls server-side; showing them raw in the bubble
    // looks broken. Also handles orphaned opening tags mid-stream. (#702)
    // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
    // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
    if(!s) return s;
    // Case-insensitive presence check without allocating a full lowercased copy
    // of the (growing) text on every call — cuts per-token/per-frame GC pressure.
    // Equivalent to the previous toLowerCase()+indexOf gate. (#5455 WS2.3)
    if(!/function_calls|dsml/i.test(String(s))) return s;
    // Support both plain <function_calls> and DSML-prefixed variants.
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
    // Also remove truncated opening tags (missing closing ">" at stream tail).
    s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
    // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
    s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
    return s.trim();
  }
  function _streamDisplay(){
    const state=readState();
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(state.assistantText), state.liveReasoningText, {streaming:true}).content;
  }
  function _parseStreamState(){
    const state=readState();
    return _extractInlineThinkingFromContent(_stripXmlToolCalls(state.assistantText), state.liveReasoningText, {streaming:true});
  }
  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      _updateLiveThinkingCard(text||'Thinking…');
      return;
    }
    // Only remove thinking if we're not in an active reasoning phase.
    // When reasoningText is set but liveReasoningText was just reset (post-tool),
    // don't wipe the finalized thinking card — it has no id anymore so
    // removeThinking() won't find it anyway, but guard explicitly.
    if(!readState().reasoningText) removeThinking();
  }
  // Helper: create (or recreate) the smd parser bound to a given DOM element.
  // Called when assistantBody is first created and after each tool-call segment reset.
  function _smdNewParser(el, fade=false){
    _smdWrittenLen=0;
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    const baseRenderer=fade ? _streamFadeRenderer(el) : _safeSmdRenderer(el);
    const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
    _smdParser=window.smd.parser(renderer);
    _smdBindParserIdentity(renderer,_smdParser,el);
  }
  function _smdRendererWithoutUnderscoreEmphasis(renderer){
    if(!renderer||!window.smd) return renderer;
    const baseAddToken=renderer.add_token;
    const baseEndToken=renderer.end_token;
    const baseAddText=renderer.add_text;
    const tokenStack=[];
    renderer.add_token=(data,token)=>{
      if(token===window.smd.ITALIC_UND||token===window.smd.STRONG_UND){
        const marker=token===window.smd.STRONG_UND?'__':'_';
        tokenStack.push(marker);
        baseAddText(data,marker);
        return;
      }
      tokenStack.push(null);
      baseAddToken(data,token);
    };
    renderer.end_token=(data)=>{
      const marker=tokenStack.pop();
      if(marker){
        baseAddText(data,marker);
        return;
      }
      baseEndToken(data);
    };
    return renderer;
  }
  // Helper: end the current smd parser (flushes remaining state) and null it out.
  function _smdEndParser(){
    const assistantBody=readState().assistantBody;
    if(_streamingKatexTimer){clearTimeout(_streamingKatexTimer);_streamingKatexTimer=null;}
    if(_smdParser&&window.smd){
      try{window.smd.parser_end(_smdParser);}catch(_){}
    }
    // parser_end may emit one final add_text chunk; flush MEDIA tails after it
    // so a final extensionless URL is rendered before the settled re-render.
    if(typeof _smdMediaTailFlush==='function') _smdMediaTailFlush(_smdParser);
    if(typeof _smdMediaTailFlush==='function') _smdMediaTailFlush(__SMD_PARSER_FALLBACK);
    // parser_end / tail flush may create new links/images — re-sanitize the
    // body before the DOM is handed off to highlightCode / renderMessages.
    if(assistantBody){_sanitizeSmdLinks(assistantBody);enhanceMarkdownTables(assistantBody);}
    // Clear the per-parser MEDIA tail buffer — any incomplete MEDIA
    // prefix the parser was holding is no longer relevant.
    if(typeof _smdMediaTailClear==='function') _smdMediaTailClear(_smdParser);
    if(typeof _smdClearParserIdentity==='function') _smdClearParserIdentity(assistantBody,_smdParser);
    _smdParser=null;
    _smdWrittenLen=0;
    _smdWrittenText='';
    // Clear the fallback MEDIA tail buffer too; fallback chunks are keyed
    // by __SMD_PARSER_FALLBACK, not null.
    if(typeof _smdMediaTailClear==='function') _smdMediaTailClear(__SMD_PARSER_FALLBACK);
  }
  function _scheduleStreamingKatex(){
    if(_streamingKatexTimer) return;
    _streamingKatexTimer=setTimeout(()=>{
      _streamingKatexTimer=null;
      const assistantBody=readState().assistantBody;
      if(assistantBody&&typeof renderKatexBlocks==='function') renderKatexBlocks(assistantBody,{streaming:true});
    },150);
  }
  // Helper: feed new displayText delta to the smd parser.
  // Only feeds chars beyond what has already been written (_smdWrittenLen).
  function _smdWrite(displayText, fade=false){
    const assistantBody=readState().assistantBody;
    if(!_smdParser||!window.smd) return;
    displayText=String(displayText||'');
    // Self-heal desyncs: if displayText no longer starts with what we've already
    // written (e.g. due to stream sanitization/tag stripping), incremental slicing
    // can skip characters. Rebuild parser from the full current displayText.
    if(_smdWrittenText && !displayText.startsWith(_smdWrittenText)){
      _smdParser=null;
      _smdWrittenLen=0;
      _smdWrittenText='';
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody,fade);
      if(!_smdParser) return;
    }
    const delta=displayText.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenLen=displayText.length;
    _smdWrittenText=displayText;
    // URL scheme safety is handled by the renderer's set_attr hook
    // (_safeSmdRenderer or _streamFadeRenderer), applied inline as smd
    // creates each DOM node — no post-hoc full-DOM scan needed.
    _scheduleStreamingKatex();
  }
  // Allowed URL schemes for anchors and images rendered from agent-streamed markdown.
  // Raw file:// anchors are rewritten to /api/media before the user can click them.
  const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|\/|#|\?|\.|api|session\/)/i;
  // ui.js owns the image-only data URI policy. It loads before this script;
  // fail closed if that contract is unavailable rather than inventing a second
  // allowlist that can drift from settled rendering.
  const _SMD_SAFE_IMG_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;
  function _smdImgSrcAllowed(v){
    const s=String(v||'');
    if(/^data:/i.test(s)) return typeof _isSafeDataImageUri==='function'&&_isSafeDataImageUri(s);
    return _SMD_SAFE_IMG_URL_RE.test(s);
  }
  function _smdLinkHref(raw){
    const href=String(raw||'');
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
    if(!/^file:\/\//i.test(href)) return href;
    try{
      const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
      return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
    }catch(_){
      return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
    }
  }
  function _smdFileHref(raw){
    return _smdLinkHref(raw);
  }
  function _sanitizeSmdLinks(root){
    if(!root||!root.querySelectorAll) return;
    const _a=root.querySelectorAll('a[href]');
    for(let i=0;i<_a.length;i++){
      const n=_a[i],v=n.getAttribute('href')||'';
      if(/^(file|workspace|session):\/\//i.test(v)){n.setAttribute('href',_smdLinkHref(v));n.classList&&/^session:\/\//i.test(v)&&n.classList.add('session-link');continue;}
      if(!_SMD_SAFE_URL_RE.test(v)){n.removeAttribute('href');n.setAttribute('data-blocked-scheme','1');}
    }
    const _im=root.querySelectorAll('img[src]');
    for(let i=0;i<_im.length;i++){
      const n=_im[i],v=n.getAttribute('src')||'';
      if(!_smdImgSrcAllowed(v)){n.removeAttribute('src');n.setAttribute('data-blocked-scheme','1');}
    }
  }

  function _resetStreamFadeState(){
    _streamFadeVisibleText='';
    _streamFadeLastTickMs=0;
    _streamFadeWordCarry=0;
    _streamFadeStartedAt=0;
    _streamFadeLastTargetWords=0;
    _streamFadeLastArrivalMs=0;
    _streamFadeArrivalWps=0;
    _streamFadeLatestAnimationEndAt=0;
    _streamFadeVisibleWords=0;
    _streamFadeHoldUntilMs=0;
    _streamFadeCurrentMs=_STREAM_FADE_MS;
    _streamFadeDomText='';
  }
  function _cancelAnimationFramePendingStreamRender(){
    if(_pendingRafHandle===null) return;
    cancelAnimationFrame(_pendingRafHandle);
    clearTimeout(_pendingRafHandle);
    _pendingRafHandle=null;
    _renderPending=false;
  }
  function _shouldUseStreamFade(){
    return window._fadeTextEffect===true;
  }
  function _shouldUseTransparentStreamFade(){
    return typeof isTransparentStream==='function'&&isTransparentStream();
  }
  function _shouldUseLiveProseFade(){
    return !_streamFadeReduceMotionEnabled() && (_shouldUseStreamFade() || _shouldUseTransparentStreamFade());
  }
  function _streamFadeSkipNode(node){
    if(!node||node.nodeType!==1) return false;
    const tag=(node.tagName||'').toLowerCase();
    return tag==='pre'||tag==='code'||tag==='script'||tag==='style'||tag==='textarea'||tag==='svg'||tag==='math';
  }
  function _streamFadeReduceMotionEnabled(){
    if(!window.matchMedia) return false;
    if(!_streamFadeReduceMotionMql){
      _streamFadeReduceMotionMql=window.matchMedia('(prefers-reduced-motion: reduce)');
      _streamFadeReduceMotion=!!_streamFadeReduceMotionMql.matches;
      _streamFadeReduceMotionOnChange=e=>{_streamFadeReduceMotion=!!e.matches;};
      try{_streamFadeReduceMotionMql.addEventListener('change',_streamFadeReduceMotionOnChange);}
      catch(_){try{_streamFadeReduceMotionMql.addListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    }
    return _streamFadeReduceMotion;
  }
  function _streamFadeCleanupReduceMotionListener(){
    if(!_streamFadeReduceMotionMql||!_streamFadeReduceMotionOnChange) return;
    try{_streamFadeReduceMotionMql.removeEventListener('change',_streamFadeReduceMotionOnChange);}
    catch(_){try{_streamFadeReduceMotionMql.removeListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    _streamFadeReduceMotionMql=null;
    _streamFadeReduceMotionOnChange=null;
  }
  function _streamFadeBindCleanup(el){
    if(!el||el._streamFadeCleanupBound) return;
    el._streamFadeCleanupBound=true;
    el.addEventListener('animationend',e=>{
      const span=e.target;
      if(!span||!span.classList||!span.classList.contains('stream-fade-word')) return;
      span.replaceWith(document.createTextNode(span.textContent||''));
    });
  }
  function _streamFadeRenderer(el){
    _streamFadeBindCleanup(el);
    const renderer=window.smd.default_renderer(el);
    const baseAddText=renderer.add_text;
    const baseSetAttr=renderer.set_attr;
    const parserFor = (data)=>{
      return _smdParserKey(data, el);
    };
    const writeFadeText=(writeParent, writeData, writeText)=>{
      if(!writeParent||_streamFadeSkipNode(writeParent)){
        _smdAppendPlainText(writeParent, writeData, writeText, baseAddText);
        return;
      }
      _streamFadeAppendText(writeParent, writeText);
    };
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      if(!parent||_streamFadeSkipNode(parent)){baseAddText(data,text);return;}
      // MEDIA-in-stream: if this chunk carries a MEDIA:<ref> token, defer to
      // the shared interceptor so the token becomes a real media element
      // instead of plain text. The fade renderer would otherwise wrap every
      // word in a stream-fade-word span, leaving MEDIA: paths visible.
      const parser=parserFor(data);
      const hasMediaTail=!!(_SMD_MEDIA_TAIL&&parser&&_SMD_MEDIA_TAIL.has&&_SMD_MEDIA_TAIL.has(parser));
      const value=String(text||'');
      const hasMediaPrefixTail=!!_smdMediaPrefixTail(value);
      if(/MEDIA:/.test(value)||hasMediaTail||hasMediaPrefixTail){
        _smdMediaAwareAddText(baseAddText, parent, data, text, _SMD_MEDIA_TAIL, parser, writeFadeText);
        return;
      }
      const frag=document.createDocumentFragment();
      const wordRe=/(\S+)(\s*)/g;
      const reduceMotion=_streamFadeReduceMotionEnabled();
      const appendStartedAt=performance.now();
      let last=0, match, changed=false;
      while((match=wordRe.exec(value))){
        if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
        if(reduceMotion){
          frag.appendChild(document.createTextNode(match[1]));
          if(match[2]) frag.appendChild(document.createTextNode(match[2]));
          last=match.index+match[0].length;
          changed=true;
          continue;
        }
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+fadeMs);
        if(match[2]) frag.appendChild(document.createTextNode(match[2]));
        last=match.index+match[0].length;
        changed=true;
      }
      if(!changed){baseAddText(data,text);return;}
      if(last<value.length) frag.appendChild(document.createTextNode(value.slice(last)));
      parent.appendChild(frag);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      const allowed=isSrc?_smdImgSrcAllowed(value):_SMD_SAFE_URL_RE.test(String(value||''));
      if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
        baseSetAttr(data,attr,_smdLinkHref(value));
        if(/^session:\/\//i.test(String(value||''))){
          const node=data&&data.nodes&&data.nodes[data.index];
          if(node&&node.classList) node.classList.add('session-link');
        }
        return;
      }
      if((isHref||isSrc)&&!allowed){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  // Safe renderer: wraps default_renderer with a set_attr hook that validates
  // href/src URL schemes inline — no post-hoc DOM-wide querySelectorAll needed.
  // Unlike _streamFadeRenderer, this does NOT wrap add_text, so smd adds new
  // DOM nodes as plain text nodes (no animation spans). Used on the non-fade
  // streaming path to eliminate _sanitizeSmdLinks(assistantBody) O(DOM) scans
  // on every token event (#WebUI-perf).
  // MEDIA-in-stream fix: also wraps add_text so MEDIA:<ref> tokens that arrive
  // mid-turn are converted to inline media elements at insert time, matching
  // what the full renderMd() pipeline does on the settled assistant message.
  // Without this, streamed prose shows MEDIA:C:\... as literal text until the
  // turn settles and the full re-render swaps it for the real <img>.
  // SAFETY & CROSS-CHUNK SPLITS (Greptile #1 + #2):
  //   1. Prose slices go back to the owning text writer (text nodes or
  //      fade spans), NOT through DOMParser — mixed prose with HTML entities /
  //      malicious <img onerror> stays as literal text.
  //   2. Each MEDIA token's HTML (from _inlineMediaHtmlForRef) is handed
  //      to DOMParser one at a time — only trusted markup is parsed.
  //   3. A MEDIA prefix split across smd flushes (e.g. "MEDIA:" then
  //      "foo.png") is buffered in a per-parser tail buffer and completed
  //      on the next add_text call.
  const _MEDIA_TAIL_MAX = 4096; // bytes; defensive cap on per-parser buffer
  const _SMD_MEDIA_PREFIX = 'MEDIA:';
  function _smdMediaPrefixTail(value){
    const text=String(value||'');
    const max=Math.min(_SMD_MEDIA_PREFIX.length,text.length);
    for(let len=max;len>0;len-=1){
      const suffix=text.slice(text.length-len);
      if(_SMD_MEDIA_PREFIX.startsWith(suffix)) return suffix;
    }
    return '';
  }
  function _smdAppendPlainText(parent, data, text, baseAddText){
    const value=String(text||'');
    if(parent&&parent.appendChild&&typeof document!=='undefined'&&document.createTextNode){
      parent.appendChild(document.createTextNode(value));
      return;
    }
    if(baseAddText) baseAddText(data,value);
  }
  function _smdMediaWriteText(parent, data, baseAddText, writeText, text){
    if(writeText){
      writeText(parent, data, String(text||''));
      return;
    }
    if(baseAddText) baseAddText(data,String(text||''));
  }
  function _smdMediaTailSet(tailMap, parser, chunk, parent, baseAddText, data, writeText){
    if(!tailMap||!parser) return;
    if(chunk) tailMap.set(parser, {chunk, parent, baseAddText, data, writeText});
    else tailMap.delete(parser);
  }
  function _smdMediaTailEntryChunk(entry){
    return entry && typeof entry==='object' && Object.prototype.hasOwnProperty.call(entry,'chunk') ? entry.chunk : entry;
  }
  function _smdMediaTailSameOwner(entry, parent, baseAddText, writeText){
    return !!entry && entry.parent===parent && entry.baseAddText===baseAddText && entry.writeText===writeText;
  }
  function _smdMediaRefHasReliableBoundary(rawRef){
    const raw=String(rawRef||'');
    if(/[?#]$/.test(raw)) return false;
    const ref=raw.split(/[?#]/,1)[0];
    return /\.(?:png|jpe?g|gif|webp|bmp|ico|svg|avif|mp4|webm|mov|m4v|mkv|avi|ogv|mp3|wav|ogg|m4a|aac|wma|opus|flac|oga|pdf|html?|csv|diff|patch|excalidraw)$/i.test(ref);
  }
  function _smdMediaTailFlushEntry(entry){
    const chunk=_smdMediaTailEntryChunk(entry);
    if(!chunk) return;
    const m=/^MEDIA:([^\s\)\]]+)$/.exec(String(chunk));
    const emitted=!!(m && entry && entry.parent && _smdAppendMediaNode(entry.parent, m[1]));
    if(!emitted && entry) _smdMediaWriteText(entry.parent, entry.data, entry.baseAddText, entry.writeText, chunk);
  }
  function _smdMediaTailFlush(parser){
    if(!_SMD_MEDIA_TAIL||!parser||!_SMD_MEDIA_TAIL.get) return;
    const entry=_SMD_MEDIA_TAIL.get(parser);
    if(!entry) return;
    _SMD_MEDIA_TAIL.delete(parser);
    _smdMediaTailFlushEntry(entry);
  }
  function _smdMediaAwareAddText(baseAddText, parent, data, text, tailMap, parser, writeText){
    const value=String(text||'');
    const tails=tailMap||(typeof _SMD_MEDIA_TAIL!=='undefined'&&_SMD_MEDIA_TAIL)||null;
    const writeCurrent=(chunk)=>_smdMediaWriteText(parent, data, baseAddText, writeText, chunk);
    if(!value){
      writeCurrent('');
      return;
    }
    // Pull any pending tail from a previous (split) chunk, then clear it;
    // this call will either complete it, re-buffer it, or flush it as text.
    let leadEntry = tails && parser && tails.get ? tails.get(parser) : null;
    let lead = _smdMediaTailEntryChunk(leadEntry);
    if(lead && !_smdMediaTailSameOwner(leadEntry, parent, baseAddText, writeText)){
      if(tails && parser && tails.delete) tails.delete(parser);
      _smdMediaTailFlushEntry(leadEntry);
      leadEntry=null;
      lead='';
    }else if(lead && tails && parser && tails.delete){
      tails.delete(parser);
    }
    const combined = lead ? lead + value : value;
    // Fast path: no MEDIA tokens in the (possibly combined) string.
    if(!/MEDIA:/.test(combined)){
      const prefixTail=_smdMediaPrefixTail(combined);
      if(prefixTail && tails && parser && prefixTail.length < _MEDIA_TAIL_MAX){
        const stable=combined.slice(0, combined.length-prefixTail.length);
        if(stable) writeCurrent(stable);
        _smdMediaTailSet(tails, parser, prefixTail, parent, baseAddText, data, writeText);
        return;
      }
      writeCurrent(combined);
      return;
    }
    // Walk the combined string, slicing into prose + MEDIA token runs.
    // Prose runs go through the owning text writer. MEDIA tokens go through
    // the single-token DOMParser helper only after a delimiter or
    // reliable filename suffix proves the ref is complete.
    const re=/MEDIA:([^\s\)\]]+)/g;
    let last=0, m;
    let unmatchedTail=null;
    while((m=re.exec(combined))){
      const matchEnd = m.index + m[0].length;
      if(m.index>last){
        const slice = combined.slice(last, m.index);
        writeCurrent(slice);
      }
      if(matchEnd===combined.length && !_smdMediaRefHasReliableBoundary(m[1])){
        const candidate = combined.slice(m.index);
        if(candidate.length < _MEDIA_TAIL_MAX){
          unmatchedTail = candidate;
        } else {
          writeCurrent(candidate);
        }
        last = combined.length;
        break;
      }
      if(!_smdAppendMediaNode(parent, m[1])) writeCurrent(m[0]);
      last = matchEnd;
    }
    // Tail buffer — hold trailing bytes that look like an unterminated
    // MEDIA prefix; flush any prose before the partial MEDIA suffix.
    const rest = combined.slice(last);
    if(rest){
      const tailMatch = /MEDIA:[^\s\)\]]*$/.exec(rest);
      const prefixTail = tailMatch ? '' : _smdMediaPrefixTail(rest);
      const tailValue = tailMatch ? tailMatch[0] : prefixTail;
      if(tailValue && rest.length < _MEDIA_TAIL_MAX){
        const tailStart = tailMatch ? tailMatch.index : rest.length-prefixTail.length;
        if(tailStart>0) writeCurrent(rest.slice(0, tailStart));
        unmatchedTail = tailValue;
      } else {
        writeCurrent(rest);
      }
    }
    if(tails && parser){
      _smdMediaTailSet(tails, parser, unmatchedTail, parent, baseAddText, data, writeText);
    }
  }
  // Single-token DOM splice. Only ever fed the output of
  // _inlineMediaHtmlForRef (trusted markup fragment). Plain text
  // goes through baseAddText → createTextNode — NEVER here.
  function _smdAppendMediaNode(parent, rawRef){
    if(!parent||!rawRef) return false;
    const mediaHtml = (typeof _inlineMediaHtmlForRef==='function')
      ? _inlineMediaHtmlForRef(String(rawRef))
      : '';
    if(!mediaHtml) return false;
    let host=null;
    try{
      const doc=new DOMParser().parseFromString('<div>'+mediaHtml+'</div>','text/html');
      host=doc.body&&doc.body.firstChild;
    }catch(_){ host=null; }
    if(!host||!host.childNodes||!host.childNodes.length) return false;
    const frag=document.createDocumentFragment();
    while(host.firstChild) frag.appendChild(host.firstChild);
    parent.appendChild(frag);
    _smdScheduleMediaPostProcess(parent);
    return true;
  }
  function _smdScheduleMediaPostProcess(root){
    if(!root) return;
    if(typeof _postProcessWithAnchorSuppression!=='function'
      && typeof postProcessRenderedMessages!=='function'
      && typeof _applyMediaPlaybackPreferences!=='function') return;
    const run=()=>{
      try{
        if(typeof _postProcessWithAnchorSuppression==='function') _postProcessWithAnchorSuppression(root);
        else if(typeof postProcessRenderedMessages==='function') postProcessRenderedMessages(root);
        if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(root);
      }catch(_){}
    };
    if(typeof requestAnimationFrame==='function') requestAnimationFrame(run);
    else if(typeof setTimeout==='function') setTimeout(run,0);
    else run();
  }
  // Per-parser tail buffer keyed by parser instance so concurrent
  // smd parsers (live prose + anchor-scene rows + tool-card streams)
  // keep their own pending bytes. Cleared inside _smdEndParser /
  // _clearAnchorProseIncrementalNode on stream end.
  const _SMD_MEDIA_TAIL = (typeof WeakMap!=='undefined') ? new WeakMap() : new Map();
  // Sentinel for parserFor fallback — a dedicated object instead of
  // a string, so WeakMap.set doesn't throw TypeError when all three
  // parser-identity sources are unavailable (Greptile #3).
  const __SMD_PARSER_FALLBACK = {};
  function _smdParserKey(data, el){
    return (data && data.parser) || (el && el.__smdParser) || __SMD_PARSER_FALLBACK;
  }
  function _smdBindParserIdentity(renderer, parser, el){
    if(renderer&&renderer.data) renderer.data.parser=parser;
    if(el) el.__smdParser=parser;
  }
  function _smdClearParserIdentity(el, parser){
    if(!el || (parser && el.__smdParser!==parser)) return;
    try{delete el.__smdParser;}catch(_){el.__smdParser=null;}
  }
  function _smdMediaTailClear(parser){
    if(_SMD_MEDIA_TAIL && parser) _SMD_MEDIA_TAIL.delete(parser);
    // Also clear the fallback key if it was ever set
    if(_SMD_MEDIA_TAIL && parser === __SMD_PARSER_FALLBACK) _SMD_MEDIA_TAIL.delete(parser);
  }
  function _safeSmdRenderer(el){
    const renderer=window.smd.default_renderer(el);
    const baseSetAttr=renderer.set_attr;
    const baseAddText=renderer.add_text;
    const writePlainText=(writeParent, writeData, writeText)=>{
      _smdAppendPlainText(writeParent, writeData, writeText, baseAddText);
    };
    const parserFor = (data)=>{
      return _smdParserKey(data, el);
    };
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      _smdMediaAwareAddText(baseAddText, parent, data, text, _SMD_MEDIA_TAIL, parserFor(data), writePlainText);
    };
    renderer.set_attr=(data,attr,value)=>{
      const isHref=window.smd&&attr===window.smd.HREF;
      const isSrc=window.smd&&attr===window.smd.SRC;
      const allowed=isSrc?_smdImgSrcAllowed(value):_SMD_SAFE_URL_RE.test(String(value||''));
      if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
        baseSetAttr(data,attr,_smdLinkHref(value));
        if(/^session:\/\//i.test(String(value||''))){
          const node=data&&data.nodes&&data.nodes[data.index];
          if(node&&node.classList) node.classList.add('session-link');
        }
        return;
      }
      if((isHref||isSrc)&&!allowed){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
        return;
      }
      baseSetAttr(data,attr,value);
    };
    return renderer;
  }
  function _streamFadeWordCountOf(text){
    const m=String(text||'').match(/\S+/g);
    return m?m.length:0;
  }
  function _streamFadeAppendText(el, text){
    if(!el) return;
    const value=String(text||'');
    if(!value) return;
    const reduceMotion=_streamFadeReduceMotionEnabled();
    const frag=document.createDocumentFragment();
    const wordRe=/(\S+)(\s*)/g;
    const appendStartedAt=performance.now();
    let last=0, match, changed=false;
    while((match=wordRe.exec(value))){
      if(match.index>last) frag.appendChild(document.createTextNode(value.slice(last,match.index)));
      if(reduceMotion){
        frag.appendChild(document.createTextNode(match[1]));
      }else{
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        frag.appendChild(span);
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+fadeMs);
      }
      if(match[2]) frag.appendChild(document.createTextNode(match[2]));
      last=match.index+match[0].length;
      changed=true;
    }
    if(!changed){
      frag.appendChild(document.createTextNode(value));
    }else if(last<value.length){
      frag.appendChild(document.createTextNode(value.slice(last)));
    }
    el.appendChild(frag);
  }
  function _streamFadePauseAfter(text, paragraphBreakIndex){
    if(paragraphBreakIndex>=0) return 90;
    const trimmed=String(text||'').trimEnd();
    if(/[.!?]["\x27)\]]*$/.test(trimmed)) return 45;
    if(/[:;]["\x27)\]]*$/.test(trimmed)) return 30;
    return 0;
  }
  function _streamFadeNextText(targetText){
    targetText=String(targetText||'');
    const now=performance.now();
    if(!targetText){
      const hadVisible=!!_streamFadeVisibleText;
      _resetStreamFadeState();
      return {text:'', caughtUp:true, changed:hadVisible};
    }
    if(!_streamFadeVisibleText||!targetText.startsWith(_streamFadeVisibleText)){
      // Markdown/tool stripping can rewrite the visible prefix. Reset safely rather than
      // trying to animate across incompatible strings or stale word birth timestamps.
      _resetStreamFadeState();
    }
    if(!_streamFadeLastTickMs){
      _streamFadeLastTickMs=now;
      _streamFadeStartedAt=now;
    }
    if(_streamFadeVisibleText===targetText) return {text:_streamFadeVisibleText,caughtUp:true,changed:false};

    const remaining=targetText.slice(_streamFadeVisibleText.length);
    const backlogWords=_streamFadeWordCountOf(remaining);
    const targetWords=_streamFadeVisibleWords+backlogWords;
    const elapsedMs=Math.max(16,Math.min(120,now-_streamFadeLastTickMs));
    _streamFadeLastTickMs=now;

    // OpenWebUI fades the actual arriving tokens, so long/fast responses naturally
    // appear to accelerate. Hermes has a playout buffer, so track incoming word
    // velocity and play out faster than it instead of using a metronomic cadence.
    // LLM telemetry is usually tokens/sec, but the UI reveals words. A fixed word
    // cadence can look stuck even when token throughput is high, so combine:
    //   1) live target-word arrival velocity, 2) backlog pressure, 3) time ramp.
    if(!_streamFadeLastArrivalMs){
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords>_streamFadeLastTargetWords){
      const arrivalElapsedMs=Math.max(16, now-_streamFadeLastArrivalMs);
      const instantArrivalWps=(targetWords-_streamFadeLastTargetWords)*1000/arrivalElapsedMs;
      // EWMA smooths bursty token chunks without hiding sustained fast output.
      _streamFadeArrivalWps=_streamFadeArrivalWps
        ? (_streamFadeArrivalWps*0.65 + instantArrivalWps*0.35)
        : instantArrivalWps;
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    } else if(targetWords<_streamFadeLastTargetWords){
      _streamFadeLastTargetWords=targetWords;
      _streamFadeLastArrivalMs=now;
      _streamFadeArrivalWps=0;
    }

    if(now<_streamFadeHoldUntilMs){
      return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    }

    const streamAgeSeconds=Math.max(0, (now-(_streamFadeStartedAt||now))/1000);
    const baseWps=22 + Math.min(streamAgeSeconds*2.5, 28); // 22 → 50 wps over long answers
    const arrivalWps=_streamFadeArrivalWps ? Math.min(_streamFadeArrivalWps*1.05 + 8, 160) : 0;
    const backlogWps=backlogWords>0 ? Math.min(22 + backlogWords*1.1, 160) : 0;
    const wordsPerSecond=Math.min(160, Math.max(baseWps, arrivalWps, backlogWps));
    const speedFadeRatio=Math.max(0,Math.min(1,(wordsPerSecond-50)/(160-50)));
    _streamFadeCurrentMs=Math.round(_STREAM_FADE_MS+(_STREAM_FADE_MAX_MS-_STREAM_FADE_MS)*speedFadeRatio);

    _streamFadeWordCarry+=elapsedMs*wordsPerSecond/1000;
    if(!_streamFadeVisibleText) _streamFadeWordCarry=Math.max(_streamFadeWordCarry,1);
    let wordsToReveal=Math.floor(_streamFadeWordCarry);
    // At very high throughput, cap each frame to a small readable wave. Sustained
    // playback still catches up, but whole paragraphs no longer pop in at once.
    const waveCap=backlogWords>=160?3:2;
    wordsToReveal=Math.min(wordsToReveal,waveCap,backlogWords);
    if(wordsToReveal<1) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    _streamFadeWordCarry=Math.max(0,_streamFadeWordCarry-wordsToReveal);

    let cut=0;
    const wordRe=/(\s*\S+\s*)/g;
    let match;
    while(wordsToReveal>0&&(match=wordRe.exec(remaining))){
      cut=wordRe.lastIndex;
      wordsToReveal-=1;
    }
    if(cut<=0) cut=Math.min(remaining.length,4);
    const chunk=remaining.slice(0,cut);
    const paragraphMatch=chunk.match(/\n\s*\n/);
    const paragraphBreak=paragraphMatch ? paragraphMatch.index : -1;
    if(paragraphMatch) cut=paragraphBreak+paragraphMatch[0].length;
    const revealed=remaining.slice(0,cut);
    _streamFadeVisibleText+=revealed;
    _streamFadeVisibleWords+=_streamFadeWordCountOf(revealed);
    const pauseMs=_streamFadePauseAfter(revealed,paragraphBreak);
    if(pauseMs) _streamFadeHoldUntilMs=now+pauseMs;
    if(_streamFadeVisibleText.length>targetText.length) _streamFadeVisibleText=targetText;
    return {text:_streamFadeVisibleText,caughtUp:_streamFadeVisibleText===targetText,changed:true};
  }
  function _renderStreamingFadeMarkdown(displayText){
    const assistantBody=readState().assistantBody;
    if(!assistantBody) return true;
    const next=_streamFadeNextText(displayText);
    if(!next.changed) return next.caughtUp;
    assistantBody.classList.add('stream-fade-active');
    if(!_shouldUseTransparentStreamFade()){
      if(!_smdParser&&window.smd){
        if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
        _smdNewParser(assistantBody,true);
      }
      if(_smdParser){
        _smdWrite(next.text,true);
      }else{
        assistantBody.innerHTML=renderMd ? renderMd(next.text||'') : esc(next.text||'');
        _sanitizeSmdLinks(assistantBody);
      }
      _streamFadeDomText=String(next.text||'');
      return next.caughtUp;
    }
    if(_smdParser){
      _smdEndParser();
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    _smdReconnect=false;
    if(!_streamFadeDomText&&assistantBody.textContent){
      assistantBody.textContent='';
    }
    if(!String(next.text||'').startsWith(_streamFadeDomText)){
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    const delta=String(next.text||'').slice(_streamFadeDomText.length);
    if(delta) assistantBody.appendChild(document.createTextNode(delta));
    _streamFadeDomText=String(next.text||'');
    return next.caughtUp;
  }
  function _streamFadeCurrentDisplayText(){
    const state=readState();
    const parsed=_parseStreamState();
    return state.segmentStart===0
      ? parsed.displayText
      : _stripXmlToolCalls(state.assistantText.slice(state.segmentStart));
  }
  function _drainStreamFadeBeforeDone(onDone){
    const drainStartedAt=performance.now();
    let forcedDone=false;
    const step=()=>{
      const assistantBody=readState().assistantBody;
      if(!assistantBody){onDone();return;}
      const target=_streamFadeCurrentDisplayText();
      const caughtUp=_renderStreamingFadeMarkdown(target);
      const anchorProcessText=_streamFadeDomText||target;
      if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText);
      scrollIfPinned();
      if(caughtUp){
        // parser_end can flush pending markdown text; include that final text in
        // the fade wait instead of replacing it immediately in renderMessages().
        if(_smdParser) _smdEndParser();
        // Let the last released words visibly finish their stagger + fade before
        // the final renderMessages() DOM replacement removes the live spans.
        const remainingAnimationMs=Math.max(_STREAM_FADE_MS, _streamFadeLatestAnimationEndAt-performance.now());
        setTimeout(onDone, Math.min(remainingAnimationMs, _STREAM_FADE_DONE_MAX_MS));
        return;
      }
      // Final SSE `done` means the canonical completed session is available.
      // The optional word-fade playout must not keep that completed answer
      // hidden behind the live Thinking state for large/bursty responses.
      if(!forcedDone&&performance.now()-drainStartedAt>=_STREAM_FADE_DONE_DRAIN_MAX_MS){
        forcedDone=true;
        if(_smdParser) _smdEndParser();
        onDone();
        return;
      }
      setTimeout(()=>requestAnimationFrame(step), 33);
    };
    step();
  }
  function _flushPendingSegmentRender(options={}){
    const force=!!(options&&options.force);
    const skipAnchorProcessProse=!!(options&&options.skipAnchorProcessProse);
    const state=readState();
    const assistantBody=state.assistantBody;
    const assistantRow=state.assistantRow;
    const assistantText=state.assistantText;
    const segmentStart=state.segmentStart;
    if(!assistantBody||(!force&&!_renderPending)) return;
    if(_renderPending) _cancelAnimationFramePendingStreamRender();
    const displayText=segmentStart===0
      ? _parseStreamState().displayText
      : _stripXmlToolCalls(assistantText.slice(segmentStart));
    if(_smdParser){
      _smdWrite(displayText);
    } else if(window.smd){
      // Parser was nulled out (e.g. by a prior segment end) but smd is
      // available — recreate it on the existing element. Uses the non-fade
      // renderer to match standard rendering, avoiding O(n²) innerHTML
      // churn on long responses (#4704). Clear any content the renderMd()
      // fallback already wrote first: _smdNewParser resets _smdWrittenText to
      // '' but does NOT clear the element, so a following _smdWrite(displayText)
      // would append the full accumulated segment ON TOP of the existing
      // fallback render and duplicate the live text.
      assistantBody.innerHTML='';
      _smdNewParser(assistantBody, false);
      if(_smdParser) _smdWrite(displayText);
    } else if(renderMd){
      assistantBody.innerHTML=renderMd(displayText);
    } else {
      assistantBody.innerHTML=esc(displayText);
    }
    if(!skipAnchorProcessProse) _upsertAnchorProcessProse(displayText,{sealed:force});
    _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
  }
  function _resetAssistantSegment(){
    resetSegmentState();
    _smdEndParser();
    _resetStreamFadeState();
  }

  // Persistent incremental renderer for anchor-scene live prose rows. The compact
  // worklog re-renders the whole scene each frame; rendering the growing prose via
  // renderMd(fullText) every frame is O(n^2) over a long answer. Instead keep a
  // per-segment smd parser + node (the SAME safe renderer as the main live body)
  // and feed only the delta, then hand the persistent node back to the ui.js scene
  // builder. Returns null whenever smd or a stable key is unavailable so the caller
  // falls back to the full renderMd path — identical structure, just not
  // incremental. (#5455 WS2.1)
  const _anchorProseSmdCache = new Map();
  function _anchorProseIncrementalNode(key, text){
    if(!window.smd || !key || typeof _safeSmdRenderer!=='function') return null;
    const value=String(text||'');
    const fade=typeof _shouldUseLiveProseFade==='function'&&_shouldUseLiveProseFade();
    try{
      let st=_anchorProseSmdCache.get(key);
      // Self-heal desyncs (edit/sanitize made the text no longer a pure append):
      // rebuild the parser+node from scratch, mirroring the _smdWrite guard.
      if(st && st.writtenText && !value.startsWith(st.writtenText)) st=null;
      if(st && st.fade!==fade) st=null;
      if(!st){
        const node=document.createElement('div');
        node.className='assistant-segment';
        node.setAttribute('data-anchor-scene-prose','1');
        const body=document.createElement('div');
        body.className='msg-body';
        if(body.classList) body.classList.toggle('stream-fade-active',fade);
        node.appendChild(body);
        const baseRenderer=fade?_streamFadeRenderer(body):_safeSmdRenderer(body);
        const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
        st={node,parser:window.smd.parser(renderer),writtenText:'',fade};
        _smdBindParserIdentity(renderer,st.parser,body);
        _anchorProseSmdCache.set(key,st);
        // Bound memory across turns: keys embed the stream id, so stale entries
        // from finished streams age out here.
        if(_anchorProseSmdCache.size>32){
          const oldest=_anchorProseSmdCache.keys().next().value;
          if(oldest!==key) _anchorProseSmdCache.delete(oldest);
        }
      }
      const body=st.node&&st.node.querySelector&&st.node.querySelector('.msg-body');
      if(body&&body.classList) body.classList.toggle('stream-fade-active',fade);
      const delta=value.slice(st.writtenText.length);
      if(delta){
        window.smd.parser_write(st.parser,delta);
        st.writtenText=value;
      }
      st.node.dataset.rawText=value;
      return st.node;
    }catch(_){
      _anchorProseSmdCache.delete(key);
      return null;
    }
  }
  window.__anchorProseIncrementalNode=_anchorProseIncrementalNode;
  function _clearAnchorProseIncrementalNode(){
    if(typeof window!=='undefined'&&window.__anchorProseIncrementalNode===_anchorProseIncrementalNode) window.__anchorProseIncrementalNode=null;
    // Clear the per-parser MEDIA tail for each cached smd parser.
    // _anchorProseSmdCache is a Map<key, {parser, ...}>; we can't
    // iterate a WeakMap to clean up, but WeakMap keys become eligible
    // for GC once the parser objects are released by the cache clear
    // below, so the WeakMap entries are automatically removed. The
    // explicit _smdMediaTailClear per-parser is a best-effort guard
    // for cache entries that may hold the last strong reference.
    if(typeof _anchorProseSmdCache!=='undefined'&&_anchorProseSmdCache.size){
      _anchorProseSmdCache.forEach(function(st){
        if(st&&st.parser&&typeof _smdMediaTailFlush==='function'){
          _smdMediaTailFlush(st.parser);
        }
        if(st&&st.parser&&typeof _smdMediaTailClear==='function'){
          _smdMediaTailClear(st.parser);
        }
      });
    }
    _anchorProseSmdCache.clear();
  }

  let _lastRenderMs=0;
  // Parse-result cache: _scheduleRender can accept a pre-computed _parseStreamState()
  // from the token event handler, avoiding a duplicate O(n) scan inside _doRender
  // when the rAF fires before the next token arrives.
  let _cachedParsed=null;
  let _cachedParsedText='';
  let _cachedParsedReasoning='';
  function _scheduleRender(parsed){
    const initialState=readState();
    const _streamFinalized=!!initialState.streamFinalized;
    // If caller provides a pre-computed parse result, cache it for _doRender.
    if(parsed){
      _cachedParsed=parsed;
      _cachedParsedText=initialState.assistantText;
      _cachedParsedReasoning=initialState.liveReasoningText;
    }
    if(_renderPending) return;
    if(initialState.streamFinalized) return; // Bug A: don't schedule new rAF after stream finalized
    _renderPending=true;
    // Cap render rate to ~15fps. The browser's rAF fires at 60fps, but each DOM
    // update takes 50-150ms on large sessions. During GC pauses, rAF callbacks
    // accumulate and then execute all at once, blocking the main thread for
    // multi-second stretches and crashing the renderer (Chrome error code 4/5).
    // Throttling to 66ms intervals prevents this pileup without noticeable
    // visual degradation — streaming text updates still feel immediate.
    // performance.now() is monotonic so tab suspend/resume and NTP adjustments
    // cannot produce negative or enormous deltas.
    const sinceLastMs=performance.now()-_lastRenderMs;
    const _doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      // Guard: a pending setTimeout+rAF can outlive stream finalization.
      const state=readState();
      const _streamFinalized=!!state.streamFinalized;
      if(_streamFinalized) return;
      const assistantText=String(state.assistantText||'');
      const liveReasoningText=String(state.liveReasoningText||'');
      const segmentStart=Number(state.segmentStart)||0;
      const assistantBody=state.assistantBody;
      const assistantRow=state.assistantRow;
      // Mobile scroll-jank guard: temporarily disable overflow-anchor before DOM
      // writes to suppress Chromium scroll re-anchoring during streaming growth.
      if(typeof window._fixMobileScrollJank==='function') window._fixMobileScrollJank();
      _lastRenderMs=performance.now();
      const parsed=_cachedParsed&&_cachedParsedText===assistantText&&_cachedParsedReasoning===liveReasoningText ? _cachedParsed : _parseStreamState();
      _cachedParsed=null;
      _renderLiveThinking(parsed);
      const displayText = segmentStart===0
        ? parsed.displayText                          // first segment: uses think-tag stripping
        : _stripXmlToolCalls(assistantText.slice(segmentStart));
      let anchorProcessText=displayText;
      if(assistantBody){
        if(_shouldUseLiveProseFade()){
          const caughtUp=_renderStreamingFadeMarkdown(displayText);
          anchorProcessText=_streamFadeDomText||'';
          if(!caughtUp&&!readState().streamFinalized){
            setTimeout(()=>_scheduleRender(), 33);
          }
        } else {
          assistantBody.classList.remove('stream-fade-active');
          _resetStreamFadeState();
          if(!_smdParser&&window.smd){
            // On reconnect: prior content in assistantBody came from a different smd parser run.
            // Clear it and start fresh — renderMessages() on done will restore the full content.
            if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}
            _smdNewParser(assistantBody);
          }
        if(_smdParser){
          _smdWrite(displayText);
        } else {
            // Fallback: smd not loaded yet, reconnect session, or smd unavailable — use renderMd
            // for every live segment. Without this, the first segment inserts raw
            // parsed.displayText and users see unformatted markdown until done.
            const fallbackText = segmentStart===0
              ? parsed.displayText
              : _stripXmlToolCalls(assistantText.slice(segmentStart));
            assistantBody.innerHTML = renderMd ? renderMd(fallbackText) : esc(fallbackText);
          }
        }
        _syncLiveWorklogReasonsForAnchor(assistantRow, displayText);
      }
      if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText);
      scrollIfPinned();
      _throttledSnapshotLiveTurn();
    };
    const frameIntervalMs=_shouldUseLiveProseFade()?33:66;
    if(sinceLastMs>=frameIntervalMs){
      _pendingRafHandle=requestAnimationFrame(_doRender);
    } else {
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(_doRender), frameIntervalMs-sinceLastMs);
    }
  }


  return Object.freeze({
    stripXmlToolCalls: _stripXmlToolCalls,
    streamDisplay: _streamDisplay,
    parseStreamState: _parseStreamState,
    renderLiveThinking: _renderLiveThinking,
    endParser: _smdEndParser,
    resetFadeState: _resetStreamFadeState,
    cancelPendingRender: _cancelAnimationFramePendingStreamRender,
    shouldUseLiveProseFade: _shouldUseLiveProseFade,
    cleanupReduceMotion: _streamFadeCleanupReduceMotionListener,
    drainFadeBeforeDone: _drainStreamFadeBeforeDone,
    flushPendingSegment: _flushPendingSegmentRender,
    resetAssistantSegment: _resetAssistantSegment,
    scheduleRender: _scheduleRender,
    clearAnchorProseIncrementalNode: _clearAnchorProseIncrementalNode,
  });
}

Object.assign(HermesMessages, {
  createStreamRenderer,
});
