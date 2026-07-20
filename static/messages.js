// Shared namespace for the split classic-script runtime. Historical globals
// remain available for index handlers, extensions, and Node harnesses.
var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

function _markSessionViewed(sid, messageCount) {
  if(typeof _setSessionViewedCount!=='function' || !sid) return;
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  _setSessionViewedCount(sid, next);
}

function _apiUrl(path) {
  return new URL(path, document.baseURI || location.href).href;
}

// Module-scope dedupe ring buffer for bg_task_complete events. Shared between
// the in-turn STREAMS path (per-turn EventSource inside the chat-stream wirer)
// and the persistent session-scoped path (/api/session/stream), so the
// frontend never double-fires a toast or ack for the same (session_id,
// event_id) regardless of which channel delivered it first. (Option X)
//
// Keyed by `${session_id}|${event_id}` → expiry timestamp (ms since epoch).
// Bounded by a 60-second TTL plus a 256-entry soft cap with insertion-order
// eviction on overflow. Events without `event_id` are ignored by the caller
// (the server contract guarantees `event_id` on every completion emit).
const _BG_TASK_COMPLETE_TTL_MS = 60000;
const _BG_TASK_COMPLETE_CAP = 256;
const _bgTaskCompleteSeenIds = new Map();

function _bgTaskCompleteRingBufferAdd(sid, evt_id) {
  // Missing key → treat as "seen/skip" (return true). The sole caller already
  // guards with `if (!evt_id) return;` before invoking this, so this branch is
  // defensive: returning true (skip) rather than false (proceed) means a
  // future call site that forgets that guard drops the un-keyable event
  // instead of processing a completion with no dedupe key.
  if (!sid || !evt_id) return true;
  const key = sid + '|' + evt_id;
  const now = Date.now();
  // Lazy purge: walk insertion-order; drop any entry whose expiry has passed.
  // Map iteration is insertion-order so this also surfaces the oldest entries
  // first when we need to evict for the soft cap below.
  for (const [k, exp] of _bgTaskCompleteSeenIds) {
    if (exp <= now) {
      _bgTaskCompleteSeenIds.delete(k);
    }
  }
  if (_bgTaskCompleteSeenIds.has(key)) return true;  // duplicate
  _bgTaskCompleteSeenIds.set(key, now + _BG_TASK_COMPLETE_TTL_MS);
  // Soft cap: insertion-order eviction.
  while (_bgTaskCompleteSeenIds.size > _BG_TASK_COMPLETE_CAP) {
    const firstKey = _bgTaskCompleteSeenIds.keys().next().value;
    if (firstKey === undefined) break;
    _bgTaskCompleteSeenIds.delete(firstKey);
  }
  return false;
}

function _isDocumentVisibleAndFocused() {
  if(typeof document!=='undefined' && document.visibilityState && document.visibilityState!=='visible') return false;
  if(typeof document!=='undefined' && typeof document.hasFocus==='function' && !document.hasFocus()) return false;
  return true;
}

let _desktopBackgroundedForNotifications=false;
// Desktop shells can background a visible document; keep that signal notification-only.
if(typeof window!=='undefined'){
  window.__hermesSetBackgrounded=(value)=>{
    _desktopBackgroundedForNotifications=!!value;
    if(_desktopBackgroundedForNotifications){
      for(const k in _STREAM_NOTIFICATION_BACKGROUND){
        const e=_STREAM_NOTIFICATION_BACKGROUND[k];
        if(e) e.wasBackgrounded=true;
      }
    }
  };
}
function _isBackgroundedForBrowserNotification(){
  return !!(typeof document!=='undefined'&&document.hidden)||_desktopBackgroundedForNotifications;
}

function _isSessionCurrentPane(sid) {
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  // During session switching, S.session still points at the previous row until
  // the next metadata request resolves. Do not let a just-finished old stream
  // update the chat pane while the user is moving to another session.
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}

function _isSessionActivelyViewed(sid) {
  if(!_isSessionCurrentPane(sid)) return false;
  if(!_isDocumentVisibleAndFocused()) return false;
  return true;
}

function _markActiveSessionViewedOnReturn() {
  if(!_isDocumentVisibleAndFocused() || !S.session || !S.session.session_id) return;
  _markSessionViewed(S.session.session_id, S.session.message_count || (S.messages&&S.messages.length) || 0);
  if(typeof _clearSessionCompletionUnread==='function') _clearSessionCompletionUnread(S.session.session_id);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
}

function _chatPayloadModel(){
  return S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'';
}

function _chatPayloadModelProvider(model){
  if(typeof _modelProviderForSend==='function') return _modelProviderForSend(model);
  if(S.session&&S.session.model_provider) return S.session.model_provider||null;
  return null;
}

function _chatPayloadModelState(){
  // Source-compat invariant: the starting precedence is still
  // model:S.session.model||$('modelSelect').value and
  // model_provider:S.session.model_provider||null. The helper only fills a
  // missing provider when it belongs to the same outgoing model.
  const model=_chatPayloadModel();
  return {model,model_provider:_chatPayloadModelProvider(model)};
}

function _deferStreamErrorIfOffline(){
  if(typeof isOfflineBannerVisible==='function' && isOfflineBannerVisible()){
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  if(typeof showOfflineBanner==='function' && navigator.onLine===false){
    showOfflineBanner('browser');
    setComposerStatus(t('offline_stream_waiting'));
    return true;
  }
  return false;
}

document.addEventListener('visibilitychange', _markActiveSessionViewedOnReturn);
window.addEventListener('focus', _markActiveSessionViewedOnReturn);

// Delegated click handler for the interim-progress-note collapse toggle (#2403).
// Delegation (not a per-element listener) is required because the live turn's
// DOM is snapshotted/restored via outerHTML/innerHTML on session switch
// (snapshotLiveTurnHtmlForSession / restoreLiveTurnHtmlForSession in ui.js),
// which strips element listeners. A document-level handler survives the
// restore so a restored toggle stays interactive and collapsed notes never
// become permanently unreachable. State lives in the DOM (presence of
// .interim-collapsed + data-threshold on the toggle), so the handler is
// stateless and works on freshly-created and restored toggles alike.
function _interimCollapseDelegatedClick(e){
  const toggle=e.target&&e.target.closest?e.target.closest('.interim-collapse-toggle'):null;
  if(!toggle) return;
  const blocks=toggle.parentElement;
  if(!blocks) return;
  const threshold=parseInt(toggle.dataset.threshold,10)||3;
  const hidden=blocks.querySelectorAll('.interim-collapsed');
  if(hidden.length){
    hidden.forEach(el=>el.classList.remove('interim-collapsed'));
    toggle.dataset.expanded='1';
    toggle.textContent='Collapse';
  } else {
    const all=Array.from(blocks.querySelectorAll('[data-interim="1"]'));
    const rehide=all.slice(0,all.length-threshold);
    rehide.forEach(el=>el.classList.add('interim-collapsed'));
    toggle.dataset.expanded='';
    toggle.textContent='Show '+rehide.length+' earlier update'+(rehide.length===1?'':'s');
  }
}
document.addEventListener('click', _interimCollapseDelegatedClick);

// TTS: pause speech synthesis when user focuses the composer (#499)
const _msgEl=document.getElementById('msg');
if(_msgEl) _msgEl.addEventListener('focus', ()=>{ if('speechSynthesis' in window && speechSynthesis.speaking) speechSynthesis.pause(); });
if(_msgEl) _msgEl.addEventListener('blur', ()=>{ if('speechSynthesis' in window && speechSynthesis.paused) speechSynthesis.resume(); });

let _selectedTextReplyBtn=null;
let _selectedTextReplyText='';
let _pendingSelections=[];  // [{id, name, text}] — named context blocks
let _selectionIdCounter=0;
// #4380: expose a pending-selection predicate so the composer's primary-action
// content check (_composerHasContent in ui.js) treats selection-only replies as
// sendable content even though they no longer live in the textarea.
if(typeof window!=='undefined'){
  window._hasPendingSelections=function(){return _pendingSelections.length>0;};
}
let _selectedTextReplyRaf=0;
const _persistentStateToastSeen=new Set();
const _thinkPairs=[
  {open:'<think>',close:'</think>'},
  {open:'<|channel>thought\n',close:'<channel|>'},
  {open:'<|turn|>thinking\n',close:'<turn|>'}
];

function _thinkingFenceMarkerAt(text, index){
  // A fenced code block opener may be indented up to 3 spaces in Markdown
  // (4+ spaces is an indented code block, handled separately). Only treat the
  // marker as a fence when it sits at a line start after optional 1-3 spaces.
  if(index>0&&text[index-1]!=='\n'){
    let back=index-1, spaces=0;
    while(back>=0&&text[back]===' '&&spaces<3){back--;spaces++;}
    if(!(back<0||text[back]==='\n')) return '';
  }
  if(text.startsWith('```',index)) return '```';
  if(text.startsWith('~~~',index)) return '~~~';
  return '';
}

function _nextThinkingOpener(text, start){
  // Index of the earliest complete thinking opener at/after `start`, or -1.
  // Cheap indexOf per opener — lets the scanner bulk-skip plain trailing content
  // instead of walking it char-by-char (#3633 Codex per-token perf catch).
  let best=-1;
  for(const p of _thinkPairs){
    const i=text.indexOf(p.open,start);
    if(i!==-1&&(best===-1||i<best)) best=i;
  }
  return best;
}

function _textTailIsPartialOpener(text){
  // True when the END of text is a non-empty proper prefix of some opener
  // (e.g. "<thi" for "<think>"). Decides whether a streaming tail might be a
  // forming block worth code-aware handling.
  for(const p of _thinkPairs){
    const m=Math.min(p.open.length-1,text.length);
    for(let n=m;n>0;n--){ if(p.open.startsWith(text.slice(text.length-n))) return true; }
  }
  return false;
}

function _lineIsIndentedCode(text, lineStart){
  // True when the line beginning at lineStart is a markdown indented code block
  // line (>=4 leading spaces or a leading tab, and not blank). lineStart must be
  // the first char of the line. Only inspects the line's leading chars, not the
  // whole document (the per-character variant was O(n^2) on long no-newline
  // content — #3633 Codex perf catch).
  if(lineStart>=text.length) return false;
  if(text[lineStart]==='\t'||text.startsWith('    ',lineStart)){
    let nl=text.indexOf('\n',lineStart);
    if(nl===-1) nl=text.length;
    return text.slice(lineStart,nl).trim()!=='';
  }
  return false;
}

function _mergeInlineThinkingReasoning(existingReasoning, extractedParts){
  let out=String(existingReasoning||'').trim();
  (Array.isArray(extractedParts)?extractedParts:[]).forEach(function(part){
    const item=String(part||'').trim();
    if(!item) return;
    if(!out){out=item;return;}
    if(out===item||out.split('\n\n').some(function(existing){return existing.trim()===item;})) return;
    out += '\n\n' + item;
  });
  return out;
}

function _extractInlineThinkingFromContent(rawContent, existingReasoning, options){
  // Code-aware extraction (must mirror api/streaming.py
  // _extract_inline_thinking_from_content): thinking tags inside a triple-fence,
  // an inline single-backtick code span, or an indented code block are LEFT
  // VISIBLE. options.streaming gates partial/unclosed handling — only during a
  // live stream does an unmatched open tag mean "still thinking"; on the
  // reload/render path an unclosed tag stays visible content (#3633 Codex catch).
  const streaming=!!(options&&options.streaming);
  const text=String(rawContent||'');
  if(!text){
    const reasoning=String(existingReasoning||'').trim();
    return {reasoning,content:text,thinkingText:reasoning,displayText:text,inThinking:false};
  }
  // Fast path (#3633 Codex perf catch — _parseStreamState / syncInflightAssistantMessage
  // call this on the FULL accumulator on every streamed token, so the common no-tag
  // case must not do the O(length) char walk per call). If no complete opener is
  // present AND — when streaming — the tail is not a prefix of an opener, there is
  // nothing to extract: return the text unchanged (two cheap substring scans).
  if(!_thinkPairs.some(p=>text.indexOf(p.open)!==-1)){
    let tailIsPartialOpener=false;
    if(streaming){
      for(const p of _thinkPairs){
        const maxPrefix=Math.min(p.open.length-1,text.length);
        for(let n=maxPrefix;n>0;n--){
          if(p.open.startsWith(text.slice(text.length-n))){tailIsPartialOpener=true;break;}
        }
        if(tailIsPartialOpener) break;
      }
    }
    if(!tailIsPartialOpener){
      const reasoning=String(existingReasoning||'').trim();
      return {reasoning,content:text,thinkingText:reasoning,displayText:text,inThinking:false};
    }
  }
  const visible=[];
  const extracted=[];
  let cursor=0;
  let index=0;
  let fence='';
  let inBacktick=false;
  let inThinking=false;
  // Incremental O(1)-per-iteration line state + seen-nonspace flag (the previous
  // per-character line scan + slice(0,index).trim() were O(n^2) on long
  // no-newline content — #3633 Codex perf catch).
  let lineIsIndentedCode=_lineIsIndentedCode(text,0);
  let seenNonspace=false;
  // Only lstrip the final content when a LEADING thinking block/prefix was
  // removed — a reply that legitimately starts with indented code / whitespace
  // and has no leading thinking wrapper keeps its leading whitespace (#3633
  // Codex catch).
  let leadingRemoved=false;
  // Index of the next complete opener at/after `index` — lets the scanner bulk-skip
  // plain trailing content instead of walking it char-by-char every streamed token
  // (#3633 Codex per-token perf catch).
  let nextOpener=_nextThinkingOpener(text,0);
  while(index<text.length){
    if(nextOpener===-1||index>nextOpener) nextOpener=_nextThinkingOpener(text,index);
    if(nextOpener===-1){
      // No further COMPLETE opener ahead — remaining tail is plain and is
      // appended in one slice, EXCEPT during streaming when the tail is a prefix
      // of an opener ("...<thi"): it may be a forming block and must be
      // suppressed, but ONLY if outside code context (a partial opener inside
      // inline-backtick / fenced / indented code stays visible — master parity).
      // Code state needs the char walk, so fall through in that case (bounded —
      // a partial tail is a transient single token) instead of bulk-skipping.
      if(streaming&&_textTailIsPartialOpener(text)){
        // fall through to the code-aware char walk for the tail
      } else {
        break;
      }
    }
    const ch=text[index];
    if(index>0&&text[index-1]==='\n') lineIsIndentedCode=_lineIsIndentedCode(text,index);
    const marker=_thinkingFenceMarkerAt(text,index);
    if(marker) fence=(fence===marker)?'':(fence||marker);
    if(!fence&&!marker&&ch==='`') inBacktick=!inBacktick;
    const inCode=!!fence||inBacktick||lineIsIndentedCode;
    if(!inCode){
      let pair=null;
      for(const candidate of _thinkPairs){
        if(text.startsWith(candidate.open,index)){pair=candidate;break;}
      }
      if(pair){
        const closeIndex=text.indexOf(pair.close,index+pair.open.length);
        if(closeIndex===-1){
          // Unclosed open tag. A LEADING unclosed block (nothing visible before
          // it) is a genuine thinking trace cut off mid-thought → reasoning
          // (master #3455 leading-only intent + live "still thinking"). An
          // unclosed tag AFTER visible content on the reload/render path is
          // almost always a literal typed tag — leave it (and following prose)
          // visible so nothing is silently truncated (#3633 Codex catch).
          const leading=!seenNonspace;
          if(!streaming&&!leading) break;
          if(leading) leadingRemoved=true;
          visible.push(text.slice(cursor,index));
          const partial=text.slice(index+pair.open.length);
          if(partial) extracted.push(partial);
          inThinking=true;
          cursor=text.length;
          index=text.length;
          break;
        }
        visible.push(text.slice(cursor,index));
        extracted.push(text.slice(index+pair.open.length,closeIndex));
        if(!seenNonspace) leadingRemoved=true;
        seenNonspace=true;
        index=closeIndex+pair.close.length;
        cursor=index;
        continue;
      }
      if(streaming){
        let matchedPartial=false;
        for(const candidate of _thinkPairs){
          const rest=text.slice(index);
          if(rest.length<candidate.open.length&&candidate.open.startsWith(rest)){
            if(!seenNonspace) leadingRemoved=true;
            visible.push(text.slice(cursor,index));
            inThinking=true;
            cursor=text.length;
            index=text.length;
            matchedPartial=true;
            break;
          }
        }
        if(matchedPartial||index>=text.length) break;
      }
    }
    if(ch.trim()!=='') seenNonspace=true;
    index++;
  }
  if(cursor<text.length) visible.push(text.slice(cursor));
  const content=leadingRemoved?visible.join('').replace(/^\s+/,''):visible.join('');
  const reasoning=_mergeInlineThinkingReasoning(existingReasoning,extracted);
  return {reasoning,content,thinkingText:reasoning,displayText:content,inThinking};
}

if(typeof window!=='undefined'){
  window._extractInlineThinkingFromContentForRender=function(rawContent, existingReasoning){
    return _extractInlineThinkingFromContent(rawContent, existingReasoning, {streaming:false});
  };
}

Object.assign(HermesMessages, {
  extractInlineThinkingFromContent: _extractInlineThinkingFromContent,
});
