import { showToast } from './composer.js';
import { refreshSession } from './session-recovery.js';
import { _compressionMessageAnchorKey, _isContextCompactionMessage, _isPreservedCompressionTaskListMessage } from './compression-ui.js';
import { _clearRenderCache, _clearUserRowIntrinsicHeightCache, _currentMessageRenderWindowSize, _scheduleMessageVirtualizedRender } from './navigation.js';
import { _assistantMessageHasVisibleContent, _isRecoveryControlMessage, _messageHasReasoningPayload, msgContent } from './assistant-turn-presentation.js';
import { syncTopbar } from './topbar-presentation.js';
import { renderMessages } from './renderer.js';

// `todos` is the single source of truth for the Todos panel.  Any update
// goes through the `todo_state` SSE event (live) or session.todo_state
// (cold-load).  `todoStateMeta` doubles as a sentinel: while it is null
// no explicit signal has been seen, so loadTodos() falls back to the
// legacy reverse-scan over S.messages — that keeps new clients working
// against old servers (Phase 1 may not yet be deployed everywhere).
// See api/todo_state.py for the wire contract.
const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default',activeProfileIsDefault:true,showHiddenWorkspaceFiles:false,todos:[],todoStateMeta:null,_pendingSessionToolsets:null};

function assistantDisplayName(){
  if(S.activeProfile&&S.activeProfile!=='default') return S.activeProfile.charAt(0).toUpperCase()+S.activeProfile.slice(1);
  return window._botName||'Hermes';
}
const INFLIGHT={};  // keyed by session_id while request in-flight
const SESSION_QUEUES={};  // keyed by session_id for queued follow-up turns
const MAX_UPLOAD_BYTES=(window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.maxUploadBytes)||20*1024*1024;
const MAX_UPLOAD_MB=Math.round(MAX_UPLOAD_BYTES/1024/1024);
// Tracks which session's queue to drain in setBusy(false).
// Set to activeSid just before setBusy(false) in done/error handlers so the
// queue drains the session that *finished*, not the one currently viewed.
// Single-shot: setBusy() reads and clears this on every call. Concurrent
// back-to-back stream completions would overwrite it, but HTTPServer is
// single-threaded so only one done event fires at a time in practice.
let _queueDrainSid=null;
const $=id=>document.getElementById(id);
const OFFLINE_RECHECK_MS=2500;
const OFFLINE_HEALTH_TIMEOUT_MS=10000;
const OFFLINE_FETCH_FAILURES_BEFORE_BANNER=2;
let _offlineVisible=false;
let _offlineReason='browser';
let _offlineProbeTimer=null;
let _offlineChecking=false;
let _offlineProbePromise=null;
let _offlineHealthProbePromise=null;
let _offlineFetchProbeFailures=0;
let _offlineRawFetch=null;
let _offlineFetchPatched=false;
function _browserReportsOnline(){return !('onLine' in navigator)||navigator.onLine!==false;}
function _offlineHealthUrl(){const url=new URL('health',document.baseURI||location.href);url.searchParams.set('offline_probe',String(Date.now()));return url.href;}
function _setOfflineChecking(checking){
  _offlineChecking=!!checking;
  const btn=$('offlineCheckNow');
  if(btn){btn.disabled=_offlineChecking;btn.textContent=_offlineChecking?t('offline_checking'):t('offline_check_now');}
}
function _renderOfflineBanner(){
  const banner=$('offlineBanner');
  if(!banner)return;
  const detail=$('offlineDetails');
  if(detail)detail.textContent=t(_offlineReason==='browser'?'offline_browser_detail':'offline_network_detail');
  const title=$('offlineTitle');
  if(title)title.textContent=t('offline_title');
  const auto=$('offlineAutorefresh');
  if(auto)auto.textContent=t('offline_autorefresh');
  _setOfflineChecking(_offlineChecking);
  banner.hidden=false;
  banner.classList.add('visible');
}
function _startOfflineProbeTimer(){
  if(_offlineProbeTimer)return;
  _offlineProbeTimer=setInterval(()=>{checkOfflineRecoveryNow();},OFFLINE_RECHECK_MS);
}
function _stopOfflineProbeTimer(){
  if(_offlineProbeTimer){clearInterval(_offlineProbeTimer);_offlineProbeTimer=null;}
}
function showOfflineBanner(reason){
  _offlineVisible=true;
  _offlineReason=reason||(_browserReportsOnline()?'network':'browser');
  _renderOfflineBanner();
  _startOfflineProbeTimer();
}
function isOfflineBannerVisible(){return _offlineVisible;}
function _hideOfflineBanner(){
  _offlineVisible=false;
  _stopOfflineProbeTimer();
  _setOfflineChecking(false);
  const banner=$('offlineBanner');
  if(banner){banner.classList.remove('visible');banner.hidden=true;}
}
async function _probeOfflineRecovery(){
  if(_offlineHealthProbePromise)return _offlineHealthProbePromise;
  _offlineHealthProbePromise=(async()=>{
    const fetcher=_offlineRawFetch||window.fetch.bind(window);
    // Bound the probe so a black-hole network (connected, server hung, packets
    // dropped) can't delay the banner past a few seconds — the probe now gates
    // the initial banner display on the offline-event/startup paths.
    let ctrl=null,timer=null;
    try{ctrl=(typeof AbortController!=='undefined')?new AbortController():null;}catch(_){ctrl=null;}
    if(ctrl)timer=setTimeout(()=>{try{ctrl.abort();}catch(_){}},OFFLINE_HEALTH_TIMEOUT_MS);
    try{
      const opts={cache:'no-store',credentials:'include'};
      if(ctrl)opts.signal=ctrl.signal;
      const res=await fetcher(_offlineHealthUrl(),opts);
      return !!(res&&res.ok);
    }catch(_){return false;}
    finally{if(timer)clearTimeout(timer);}
  })();
  try{return await _offlineHealthProbePromise;}
  finally{_offlineHealthProbePromise=null;}
}
async function _showOfflineBannerIfProbeFails(reason,opts){
  opts=opts||{};
  const visibleAtStart=_offlineVisible;
  const requireConsecutiveFailures=opts.requireConsecutiveFailures!==false;
  if(visibleAtStart)_setOfflineChecking(true);
  const ok=await _probeOfflineRecovery();
  if(visibleAtStart)_setOfflineChecking(false);
  if(ok){
    _offlineFetchProbeFailures=0;
    if(_offlineVisible){_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();}
    return true;
  }
  if(!visibleAtStart&&requireConsecutiveFailures){
    _offlineFetchProbeFailures+=1;
    if(_offlineFetchProbeFailures<OFFLINE_FETCH_FAILURES_BEFORE_BANNER)return false;
  }
  showOfflineBanner(reason||(_browserReportsOnline()?'network':'browser'));
  return false;
}
async function checkOfflineRecoveryNow(){
  if(_offlineProbePromise)return _offlineProbePromise;
  _offlineProbePromise=(async()=>{
    if(!_offlineVisible)return false;
    _setOfflineChecking(true);
    const ok=await _probeOfflineRecovery();
    _setOfflineChecking(false);
    if(ok){_offlineFetchProbeFailures=0;if(!_offlineVisible)return true;_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();return true;}
    showOfflineBanner(_browserReportsOnline()?'network':'browser');
    return false;
  })();
  try{return await _offlineProbePromise;}
  finally{_offlineProbePromise=null;}
}
// Recover from a transient "Connection lost" without a full page reload.
//
// The offline banner fires whenever a fetch/SSE errors — which Android does
// aggressively every time the PWA is backgrounded, even for a second. The old
// behaviour here was `window.location.reload()`: a hard cold boot that re-runs
// the whole app and re-pulls /api/sessions + /api/session, producing the
// multi-second "reload to see the conversation I was just in" flash on every
// resume. The reload was also intermittent (only when a request actually
// errored that time), matching the reported "sometimes it reloads, sometimes
// it doesn't".
//
// The server keeps the agent running and buffers stream events while no
// subscriber is attached (#2307), so a hard reload is never required to
// recover — we just need to reattach. This does the soft path: hide the
// banner, restart the gateway SSE (bfcache/background kills the connection),
// and re-fetch the active session so any messages that landed while we were
// away appear. A full reload is the fallback only if the soft path throws.
async function _recoverFromOfflineSoftly(){
  try{
    _hideOfflineBanner();
    if(typeof startGatewaySSE==='function') startGatewaySSE();
    if(S.session && typeof refreshSession==='function'){
      await refreshSession();
    }
    // After refreshSession() sets S.activeStreamId, reattach if a stream is live.
    // The server buffers events while no subscriber is attached (#2307/#3863).
    const sid=S.session&&S.session.session_id;
    const streamId=S.session&&S.session.active_stream_id;
    if(sid&&streamId&&typeof attachLiveStream==='function'){
      let status=null;
      try{
        status=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
      }catch(_){/* stream status check failed — leave session refreshed but don't reattach */}
      // Outside the probe's catch so an attachLiveStream throw reaches the
      // outer fallback (hard reload) instead of being silently swallowed.
      if(status&&status.active) attachLiveStream(sid,streamId,S.session.pending_attachments||[],{reconnecting:true});
    }
    return true;
  }catch(_){
    // Soft reattach failed (server mid-restart, session gone, etc.) — fall
    // back to the original hard reload so the user is never stuck offline.
    window.location.reload();
    return false;
  }
}
function _isAbortError(e){return !!(e&&(e.name==='AbortError'||e.code===20));}
function _patchOfflineFetch(){
  if(_offlineFetchPatched||typeof window.fetch!=='function')return;
  _offlineFetchPatched=true;
  _offlineRawFetch=window.fetch.bind(window);
  window.fetch=async function(...args){
    try{return await _offlineRawFetch(...args);}
    catch(e){
      if(!_isAbortError(e)&&(e instanceof TypeError||!_browserReportsOnline())){
        void _showOfflineBannerIfProbeFails(_browserReportsOnline()?'network':'browser');
      }
      throw e;
    }
  };
}
function initOfflineMonitor(){
  _patchOfflineFetch();
  window.addEventListener('offline',()=>{void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});});
  window.addEventListener('online',()=>{if(_offlineVisible)checkOfflineRecoveryNow();});
  if(!_browserReportsOnline())void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initOfflineMonitor,{once:true});
else initOfflineMonitor();
// Redirect to login when the server responds with 401 (auth session expired).
// Handles iOS PWA standalone mode and keeps subpath mounts like /hermes/ from
// escaping to the personal site root /login.
// #5578: on a login-shaped page, reload 'login' WITHOUT a next (avoid self-nesting).
function _redirectIfUnauth(res){if(res&&res.status===401){var _p=(window.location.pathname||'').replace(/\/+$/,'');if(/(?:^|\/)login$/.test(_p)){window.location.href='login';}else{window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);}return true;}return false;}
function _getSessionQueue(sid, create=false){
  if(!sid) return [];
  if(!SESSION_QUEUES[sid]&&create) SESSION_QUEUES[sid]=[];
  return SESSION_QUEUES[sid]||[];
}
function _queueStorageKey(sid){
  return 'hermes-queue-'+sid;
}
function _clearPersistedSessionQueue(sid){
  if(!sid) return;
  const key=_queueStorageKey(sid);
  try{sessionStorage.removeItem(key);}catch(_){}
  try{localStorage.removeItem(key);}catch(_){}
}
function _persistSessionQueueStorage(sid, queue){
  if(!sid) return;
  const q=Array.isArray(queue)?queue:[];
  if(!q.length){_clearPersistedSessionQueue(sid);return;}
  const key=_queueStorageKey(sid);
  let payload='[]';
  try{payload=JSON.stringify(q);}catch(_){return;}
  try{sessionStorage.setItem(key,payload);}catch(_){}
  try{localStorage.setItem(key,payload);}catch(_){}
}
function _readPersistedSessionQueue(sid){
  if(!sid) return [];
  const key=_queueStorageKey(sid);
  const read=(store)=>{
    try{
      const raw=store&&store.getItem?store.getItem(key):null;
      if(!raw) return null;
      const parsed=JSON.parse(raw);
      return Array.isArray(parsed)?parsed:null;
    }catch(_){return null;}
  };
  const sessionValue=read(sessionStorage);
  if(sessionValue&&sessionValue.length) return sessionValue;
  const localValue=read(localStorage);
  if(localValue&&localValue.length){
    try{sessionStorage.setItem(key,JSON.stringify(localValue));}catch(_){}
    return localValue;
  }
  return [];
}
function queueSessionMessage(sid, payload){
  if(!sid||!payload) return 0;
  const q=_getSessionQueue(sid,true);
  // Stamp created_at so the restore path can detect stale entries (agent already responded)
  const entry={...payload, _queued_at: Date.now()};
  q.push(entry);
  _persistSessionQueueStorage(sid,q);
  return q.length;
}
function shiftQueuedSessionMessage(sid){
  const q=_getSessionQueue(sid,false);
  if(!q.length) return null;
  const next=q.shift();
  if(!q.length){
    delete SESSION_QUEUES[sid];
    _clearPersistedSessionQueue(sid);
  } else {
    _persistSessionQueueStorage(sid,q);
  }
  return next;
}
function getQueuedSessionCount(sid){
  return _getSessionQueue(sid,false).length;
}
function _compressionSessionLock(){
  return window._compressionLockSid||null;
}
function _setCompressionSessionLock(sid){
  window._compressionLockSid=sid||null;
}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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

function _compressionRecoveryHtml(recovery, sessionId){
  if(!recovery||typeof recovery!=='object') return '';
  if(String(recovery.terminal_state||'')!=='compression_exhausted') return '';
  const action=String(recovery.recommended_action||'');
  if(action!=='start_focused_continuation') return '';
  const sid=String(recovery.source_session_id||sessionId||'');
  const title=String(recovery.title||'Context compression exhausted');
  const summary=String(recovery.summary||'Start a focused continuation, then describe the next narrow task.');
  const actionLabel=String(recovery.action_label||'Start focused continuation');
  const icon=(typeof li==='function')?li('git-branch',14):'';
  return `<div class="compression-recovery-card" data-compression-recovery-card="1">
    <div class="compression-recovery-copy">
      <div class="compression-recovery-title">${esc(title)}</div>
      <div class="compression-recovery-summary">${esc(summary)}</div>
    </div>
    <button class="compression-recovery-action" type="button" data-recovery-session-id="${esc(sid)}" onclick="startCompressionRecovery(this);event.stopPropagation()">${icon}<span>${esc(actionLabel)}</span></button>
  </div>`;
}

function _activeCompressionRecoveryPayload(){
  if(!S||!S.session) return null;
  const recovery=S.session.compression_recovery;
  if(recovery&&typeof recovery==='object'&&String(recovery.terminal_state||'')==='compression_exhausted') return recovery;
  // A cleared session-level recovery payload is authoritative. Only scan
  // message metadata for older sessions that never exposed this field.
  if(Object.prototype.hasOwnProperty.call(S.session,'compression_recovery')) return null;
  const messages=Array.isArray(S.messages)?S.messages:[];
  for(let i=messages.length-1;i>=0;i--){
    const msg=messages[i];
    const msgRecovery=msg&&msg._compressionRecovery;
    if(msgRecovery&&typeof msgRecovery==='object'&&String(msgRecovery.terminal_state||'')==='compression_exhausted') return msgRecovery;
  }
  return null;
}

function isGenericCompressionContinuationIntent(text){
  const raw=String(text||'').trim().toLowerCase();
  if(!raw) return false;
  const normalized=raw.replace(/[^\p{L}\p{N}]+/gu,' ').trim();
  const generic=new Set(['continue','continue please','go on','keep going','resume','proceed','carry on','继续','继续吧','接着','接着做','继续做','继续执行']);
  if(generic.has(normalized)) return true;
  const parts=normalized.split(/\s+/).filter(Boolean);
  return !!parts.length&&parts.length<=2&&parts.every(part=>generic.has(part));
}

function shouldInterceptCompressionRecoveryContinuation(text, files){
  const hasFiles=Array.isArray(files)&&files.length>0;
  if(hasFiles||!isGenericCompressionContinuationIntent(text)) return false;
  const recovery=_activeCompressionRecoveryPayload();
  return !!(recovery&&String(recovery.recommended_action||'')==='start_focused_continuation');
}

function showCompressionRecoveryContinuationHint(){
  const card=document.querySelector('[data-compression-recovery-card="1"]');
  if(card&&typeof card.scrollIntoView==='function'){
    try{card.scrollIntoView({block:'center',behavior:'smooth'});}catch(_){card.scrollIntoView();}
    const btn=card.querySelector('.compression-recovery-action');
    if(btn&&typeof btn.focus==='function') setTimeout(()=>btn.focus(),120);
  }
  if(typeof showToast==='function') showToast('This session exhausted context compression. Start a focused continuation, then describe the next narrow task.',4500,'warning');
}

async function startCompressionRecovery(btn){
  const sourceSid=String((btn&&btn.dataset&&btn.dataset.recoverySessionId)||(S.session&&S.session.session_id)||'').trim();
  if(!sourceSid) return;
  let retiredRecoveryCard=false;
  if(btn){btn.disabled=true;btn.classList.add('loading');}
  try{
    const data=await api('/api/session/compression-recovery/start',{method:'POST',body:JSON.stringify({session_id:sourceSid})});
    const sid=data&&data.session&&data.session.session_id;
    if(!sid) throw new Error('Compression recovery did not return a session.');
    try{localStorage.setItem('hermes-webui-session',sid);}catch(_){}
    if(typeof loadSession==='function') await loadSession(sid,{preserveActiveInput:false});
    else if(data.session){S.session=data.session;S.messages=data.session.messages||[];syncTopbar();renderMessages();}
    if(typeof renderSessionList==='function') await renderSessionList();
    if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(sid);
    if(typeof showToast==='function') showToast((data&&data.message)||'Started focused continuation.',3000,'success');
    const composer=$('msg');
    if(composer&&typeof composer.focus==='function') composer.focus();
  }catch(e){
    // A 409 means this session no longer has an active recovery action (the
    // session already moved on — e.g. a substantive prompt cleared it). The
    // persisted card in the transcript is stale, so retire it and show a neutral
    // note instead of a raw error. The server is authoritative on availability.
    if(e&&e.status===409){
      const staleCard=(btn&&btn.closest&&btn.closest('.compression-recovery-card'))
        ||document.querySelector('[data-compression-recovery-card="1"]');
      if(staleCard){
        staleCard.setAttribute('data-compression-recovery-consumed','1');
        const staleBtn=staleCard.querySelector('.compression-recovery-action');
        if(staleBtn){staleBtn.disabled=true;staleBtn.classList.remove('loading');}
        retiredRecoveryCard=true;
      }
      if(typeof showToast==='function') showToast('This conversation already moved on — the focused-continuation action is no longer available.',4000,'info');
      return;
    }
    if(typeof showToast==='function') showToast('Compression recovery failed: '+(e&&e.message||e),5000,'error');
  }finally{
    // Do NOT re-enable a button we deliberately retired in the 409 branch.
    if(btn){if(!retiredRecoveryCard) btn.disabled=false;btn.classList.remove('loading');}
  }
}

const MESSAGE_RENDER_WINDOW_DEFAULT=50;
const MESSAGE_VIRTUAL_THRESHOLD_ROWS=80;
const MESSAGE_VIRTUAL_BUFFER_PX=900;
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS={
  user:120,
  process_wakeup:96,
  assistant:160,
  tool_call:400,
  default:140,
};
function _messageVirtualDefaultHeightForRole(role){
  return MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS[
    role&&Object.prototype.hasOwnProperty.call(MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS,role)?role:'default'
  ];
}
const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS=2;
let _messageRenderWindowSid=null;
let _messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT;
let _messageVirtualHeightCache=[];
let _messageVirtualHeightCacheEntries=[];
let _messageVirtualHeightCacheLen=0;
let _messageVirtualHeightCacheSrc=null;
let _messageVirtualEstimatedRowHeight=_messageVirtualDefaultHeightForRole('default');
let _messageVirtualScrollRaf=0;
let _messageVirtualWindowKey='';
let _messageVirtualMeasurementCycleKey='';
let _messageVirtualMeasurementRetryCount=0;
let _messageVirtualScrollActive=false;
let _messageVirtualScrollSettleTimer=0;
let _messageVirtualDeferredMeasurement=null;
let _msgNodeRecycleEnabled=false;
const _recycleStash=new Map();
const _recycleResetAttrs=[
  'data-transparent-turn-collapsed',
  'data-transparent-turn-toggle-bound',
  'data-anchor-scene-live-owner',
  'data-anchor-stream-id',
  'data-latest-assistant-response',
  'role',
  'aria-label',
  // Defensive reset for legacy/restored shells that may still carry the fallback live-turn marker.
  'data-live-assistant-turn',
];
let _scrollbarDragActive=false;
function _markMessageVirtualScrollActive(){
  _messageVirtualScrollActive=true;
  clearTimeout(_messageVirtualScrollSettleTimer);
  _messageVirtualScrollSettleTimer=setTimeout(()=>{
    _messageVirtualScrollActive=false;
    if(_messageVirtualDeferredMeasurement){
      const deferred=_messageVirtualDeferredMeasurement;
      _messageVirtualDeferredMeasurement=null;
      _scheduleMessageVirtualMeasurementRefresh(deferred);
    }
  },150);
}
// Cached visWithIdx array — invalidated when S.messages.length changes.
let _visWithIdxCache=null;
let _visWithIdxCacheLen=0;
let _visWithIdxCacheSrc=null;  // S.messages reference — detects wholesale replacement with same length
function clearVisibleMessageRowCache(){
  _visWithIdxCache=null;
  _visWithIdxCacheLen=0;
  _visWithIdxCacheSrc=null;
}
function _clearMessageVirtualHeightCache(){
  _messageVirtualHeightCache=[];
  _messageVirtualHeightCacheEntries=[];
  _messageVirtualHeightCacheLen=0;
  _messageVirtualHeightCacheSrc=null;
  _messageVirtualEstimatedRowHeight=_messageVirtualDefaultHeightForRole('default');
  _messageVirtualWindowKey='';
  _messageVirtualMeasurementCycleKey='';
  _messageVirtualMeasurementRetryCount=0;
  _messageVirtualScrollActive=false;
  clearTimeout(_messageVirtualScrollSettleTimer);
  _messageVirtualScrollSettleTimer=0;
  _messageVirtualDeferredMeasurement=null;
  if(typeof _clearUserRowIntrinsicHeightCache==='function') _clearUserRowIntrinsicHeightCache();
}
function _resetMessageRenderWindow(sid){
  _messageRenderWindowSid=sid||null;
  _messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT;
  _cancelMessageVirtualizedRender();
  _clearRenderCache();
  clearVisibleMessageRowCache();
  _clearMessageVirtualHeightCache();
}
function _cancelMessageVirtualizedRender(){
  if(_messageVirtualScrollRaf){
    cancelAnimationFrame(_messageVirtualScrollRaf);
    _messageVirtualScrollRaf=0;
  }
}
function _messageIsRenderable(m){
  if(!m||!m.role||m.role==='tool') return false;
  if(m._source === 'process_wakeup') return !!(msgContent(m)||m.attachments?.length);
  if(_isContextCompactionMessage(m)||_isPreservedCompressionTaskListMessage(m)) return false;
  if(_isRecoveryControlMessage(m)) return false;
  const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
  const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
  const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
  const hasReasoningAnchor=hasTc||hasTu||_messageHasReasoningPayload(m);
  const hasAssistantVisibleAnchor=hasTc||hasTu||hasPartialTc||_messageHasReasoningPayload(m)||_assistantMessageHasVisibleContent(m);
  return !!(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasReasoningAnchor||hasAssistantVisibleAnchor)));
}
function _getVisibleMessagesWithIdx(){
  if(!_visWithIdxCache || _visWithIdxCacheLen !== S.messages.length || _visWithIdxCacheSrc !== S.messages){
    const rebuilt=[];
    let rawIdx=0;
    for(const m of (S.messages||[])){
      if(_messageIsRenderable(m)) rebuilt.push({m,rawIdx});
      rawIdx++;
    }
    _visWithIdxCache=rebuilt;
    _visWithIdxCacheLen=S.messages.length;
    _visWithIdxCacheSrc=S.messages;
  }
  return _visWithIdxCache;
}
function _messageVirtualWindow(opts){
  const total=Math.max(0, Number(opts&&opts.total)||0);
  const threshold=Math.max(1, Number(opts&&opts.threshold)||MESSAGE_VIRTUAL_THRESHOLD_ROWS);
  const defaultHeight=Math.max(1, Number(opts&&opts.defaultHeight)||_messageVirtualDefaultHeightForRole('default'));
  const bufferPx=Math.max(0, Number(opts&&opts.bufferPx)||MESSAGE_VIRTUAL_BUFFER_PX);
  const viewportHeight=Math.max(defaultHeight, Number(opts&&opts.viewportHeight)||defaultHeight*6);
  const keepTailCount=Math.max(0, Number(opts&&opts.keepTailCount)||0);
  const tailStart=Math.max(0, total-keepTailCount);
  const heights=Array.isArray(opts&&opts.heights)?opts.heights:[];
  const roleForIdx=typeof (opts&&opts.roleForIdx)==='function'?opts.roleForIdx:null;
  const rowHeightFor=(idx)=>{
    const cached=Number(heights[idx]);
    if(Number.isFinite(cached)&&cached>0) return cached;
    return roleForIdx?Math.max(1,_messageVirtualDefaultHeightForRole(roleForIdx(idx))):defaultHeight;
  };
  if(total<=Math.max(threshold, keepTailCount)){
    return {virtualized:false,start:0,end:total,topPad:0,bottomPad:0,total,tailStart};
  }
  const scrollTop=Math.max(0, Number(opts&&opts.scrollTop)||0);
  const targetTop=Math.max(0, scrollTop-bufferPx);
  const targetBottom=scrollTop+viewportHeight+bufferPx;
  let start=0;
  let offset=0;
  while(start<tailStart&&offset+rowHeightFor(start)<=targetTop){
    offset+=rowHeightFor(start);
    start++;
  }
  if(start>=tailStart){
    return {virtualized:true,start:tailStart,end:tailStart,topPad:offset,bottomPad:0,total,tailStart};
  }
  let end=start;
  let cursor=offset;
  while(end<tailStart&&cursor<targetBottom){
    cursor+=rowHeightFor(end);
    end++;
  }
  if(end<=start) end=Math.min(total, start+1);
  let bottomPad=0;
  for(let i=end;i<tailStart;i++) bottomPad+=rowHeightFor(i);
  return {
    virtualized:true,
    start,
    end,
    topPad:offset,
    bottomPad,
    total,
    tailStart,
  };
}
function _messageVirtualSpacer(height, where){
  const spacer=document.createElement('div');
  spacer.className='message-virtual-spacer';
  spacer.dataset.virtualSpacer=where||'gap';
  spacer.setAttribute('aria-hidden','true');
  spacer.style.height=Math.max(0,Math.round(height||0))+'px';
  spacer.style.flex='0 0 auto';
  return spacer;
}
function _messageVirtualWindowKeyFor(windowMetrics){
  if(!windowMetrics) return '';
  return [
    windowMetrics.virtualized?1:0,
    windowMetrics.start,
    windowMetrics.end,
    Math.round(windowMetrics.topPad||0),
    Math.round(windowMetrics.bottomPad||0),
    windowMetrics.tailStart||0,
  ].join(':');
}
function _messageVirtualMeasurementCycleKeyFor(windowMetrics){
  if(!windowMetrics) return '';
  return [
    windowMetrics.virtualized?1:0,
    windowMetrics.start,
    windowMetrics.end,
    windowMetrics.tailStart||0,
  ].join(':');
}
function _scheduleMessageVirtualMeasurementRefresh(windowMetrics){
  if(_messageVirtualScrollActive){
    _messageVirtualDeferredMeasurement=windowMetrics;
    return;
  }
  const cycleKey=_messageVirtualMeasurementCycleKeyFor(windowMetrics);
  if(_messageVirtualMeasurementCycleKey!==cycleKey){
    _messageVirtualMeasurementCycleKey=cycleKey;
    _messageVirtualMeasurementRetryCount=0;
  }
  if(_messageVirtualMeasurementRetryCount>=MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS) return;
  _messageVirtualMeasurementRetryCount++;
  requestAnimationFrame(()=>{ _scheduleMessageVirtualizedRender(true); });
}
function _markMessageVirtualMeasurementsSettled(windowMetrics){
  _messageVirtualMeasurementCycleKey=_messageVirtualMeasurementCycleKeyFor(windowMetrics);
  _messageVirtualMeasurementRetryCount=0;
}
function _messageVirtualHeightEntryMatches(previousEntry, nextEntry){
  return !!(
    previousEntry&&nextEntry&&
    previousEntry.m===nextEntry.m
  );
}
function _messageVirtualHeightPrefixEntryMatches(previousEntry, nextEntry){
  return !!(
    previousEntry&&nextEntry&&
    previousEntry.rawIdx===nextEntry.rawIdx&&
    _messageVirtualHeightEntryMatches(previousEntry, nextEntry)
  );
}
function _syncMessageVirtualHeightCache(visWithIdx){
  const nextEntries=Array.isArray(visWithIdx)
    ? visWithIdx.map(entry=>entry?{rawIdx:entry.rawIdx,m:entry.m}:entry)
    : [];
  if(
    _messageVirtualHeightCacheLen===S.messages.length &&
    _messageVirtualHeightCacheSrc===S.messages &&
    _messageVirtualHeightCacheEntries.length===nextEntries.length
  ) return;
  const previousEntries=Array.isArray(_messageVirtualHeightCacheEntries)?_messageVirtualHeightCacheEntries:[];
  const previousHeights=Array.isArray(_messageVirtualHeightCache)?_messageVirtualHeightCache.slice():[];
  let nextHeights=null;
  if(!previousEntries.length){
    nextHeights=new Array(nextEntries.length);
  }else if(!nextEntries.length){
    _clearMessageVirtualHeightCache();
    _messageVirtualHeightCacheLen=S.messages.length;
    _messageVirtualHeightCacheSrc=S.messages;
    return;
  }else{
    const sharedPrefix=Math.min(previousEntries.length,nextEntries.length);
    let prefixMatches=true;
    for(let i=0;i<sharedPrefix;i++){
      if(!_messageVirtualHeightPrefixEntryMatches(previousEntries[i], nextEntries[i])){
        prefixMatches=false;
        break;
      }
    }
    if(prefixMatches){
      nextHeights=previousHeights.slice(0, sharedPrefix);
      nextHeights.length=nextEntries.length;
    }else if(nextEntries.length>=previousEntries.length){
      const prependedCount=nextEntries.length-previousEntries.length;
      let suffixMatches=true;
      for(let i=0;i<previousEntries.length;i++){
        if(!_messageVirtualHeightEntryMatches(previousEntries[i], nextEntries[i+prependedCount])){
          suffixMatches=false;
          break;
        }
      }
      if(suffixMatches){
        nextHeights=new Array(nextEntries.length);
        for(let i=0;i<previousEntries.length;i++){
          nextHeights[prependedCount+i]=previousHeights[i];
        }
      }
    }
  }
  if(nextHeights===null){
    _clearMessageVirtualHeightCache();
    _messageVirtualHeightCache=new Array(nextEntries.length);
  }else{
    _messageVirtualHeightCache=nextHeights;
    _messageVirtualWindowKey='';
  }
  _messageVirtualHeightCacheEntries=nextEntries;
  _messageVirtualHeightCacheLen=S.messages.length;
  _messageVirtualHeightCacheSrc=S.messages;
}
function _messageVirtualRoleForEntry(entry){
  const m=entry&&entry.m;
  if(!m) return 'default';
  if(m._source === 'process_wakeup') return 'process_wakeup';
  if(m.role==='user') return 'user';
  if(m.role==='assistant'){
    if((Array.isArray(m.tool_calls)&&m.tool_calls.length>0)||
       (Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use'))||
       (Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0))
      return 'tool_call';
    return 'assistant';
  }
  return 'default';
}
function _currentMessageVirtualWindow(visWithIdx, keepTailCount){
  _syncMessageVirtualHeightCache(visWithIdx);
  const container=$('messages');
  // #4325 opt-out: when the user disables transcript virtualization, always
  // render the full transcript (no windowing). Mirrors the <=threshold path so
  // every downstream consumer (render, anchor, prepend-delta) treats it as a
  // plain non-virtualized list.
  if(typeof window!=='undefined' && window._virtualizeTranscript===false){
    const total=visWithIdx.length;
    const tailStart=Math.max(0, total-Math.max(0, Number(keepTailCount)||0));
    return {virtualized:false,start:0,end:total,topPad:0,bottomPad:0,total,tailStart};
  }
  return _messageVirtualWindow({
    total:visWithIdx.length,
    scrollTop:container?container.scrollTop:0,
    viewportHeight:container?container.clientHeight:(_messageVirtualEstimatedRowHeight*6),
    heights:_messageVirtualHeightCache,
    defaultHeight:_messageVirtualEstimatedRowHeight,
    roleForIdx:idx=>_messageVirtualRoleForEntry(visWithIdx[idx]),
    keepTailCount,
  });
}
function _messageVirtualPrependedHeightDelta(prependedRenderableCount){
  const count=Math.max(0, Number(prependedRenderableCount)||0);
  if(count<=0) return null;
  const visWithIdx=_getVisibleMessagesWithIdx();
  const virtualWindow=_currentMessageVirtualWindow(visWithIdx,_messageVirtualKeepTailCount());
  if(!virtualWindow||!virtualWindow.virtualized) return null;
  const limit=Math.min(count,_messageVirtualHeightCache.length);
  let total=0;
  for(let i=0;i<limit;i++){
    const cached=Number(_messageVirtualHeightCache[i]);
    total+=(Number.isFinite(cached)&&cached>0)?cached:_messageVirtualDefaultHeightForRole(_messageVirtualRoleForEntry(visWithIdx[i]));
  }
  return Math.max(0,Math.round(total));
}
function _messageVisibleIndexForRawIdx(rawIdx, visWithIdx){
  const list=Array.isArray(visWithIdx)?visWithIdx:_getVisibleMessagesWithIdx();
  for(let i=0;i<list.length;i++){
    if(list[i]&&list[i].rawIdx===rawIdx) return i;
  }
  return -1;
}
function _safeEncodeURIComponent(v){
  try{return encodeURIComponent(String(v));}
  catch(e){
    // encodeURIComponent threw URIError -> one or more lone UTF-16 surrogates.
    // Walk the string as UTF-16 code units: keep valid high(D800-DBFF) +
    // low(DC00-DFFF) pairs intact (so emoji survive) and drop lone surrogates.
    // No regex lookbehind/lookahead so this parses on every browser engine
    // (some older WebViews / Safari <16.4 don't support lookbehind in regex
    // literals, which would otherwise brick ui.js at parse time).
    const s=String(v);
    let cleaned='';
    for(let i=0;i<s.length;i++){
      const c=s.charCodeAt(i);
      if(c>=0xD800&&c<=0xDBFF){
        const n=(i+1<s.length)?s.charCodeAt(i+1):0;
        if(n>=0xDC00&&n<=0xDFFF){cleaned+=s[i]+s[i+1];i++;}
      }else if(c<0xDC00||c>0xDFFF){
        cleaned+=s[i];
      }
    }
    return encodeURIComponent(cleaned);
  }
}

function _messageViewportAnchorKeyForMessage(m){
  if(typeof _compressionMessageAnchorKey!=='function') return '';
  const key=_compressionMessageAnchorKey(m);
  if(!key) return '';
  return [key.role||'',key.ts??'',key.attachments??0,key.text||''].map(v=>_safeEncodeURIComponent(v)).join('|');
}
function _messageVisibleIndexForAnchorKey(anchorKey, visWithIdx){
  const key=String(anchorKey||'');
  if(!key) return -1;
  const list=Array.isArray(visWithIdx)?visWithIdx:_getVisibleMessagesWithIdx();
  for(let i=0;i<list.length;i++){
    if(list[i]&&_messageViewportAnchorKeyForMessage(list[i].m)===key) return i;
  }
  return -1;
}
function _messageSessionIndexBase(){
  const n=Number(typeof _oldestIdx!=='undefined'?_oldestIdx:0);
  return Number.isFinite(n)?Math.max(0,n):0;
}
function _messageSessionIndexForRawIdx(rawIdx){
  const n=Number(rawIdx);
  if(!Number.isFinite(n)) return null;
  return _messageSessionIndexBase()+n;
}
function _messageRawIdxForSessionIndex(sessionIdx){
  const n=Number(sessionIdx);
  if(!Number.isFinite(n)) return null;
  return n-_messageSessionIndexBase();
}
function _messageVirtualScrollTopForVisibleIdx(visWithIdx, visibleIdx, container){
  const idx=Math.max(0,Number(visibleIdx)||0);
  _syncMessageVirtualHeightCache(visWithIdx);
  const limit=Math.min(idx,_messageVirtualHeightCache.length);
  let offset=0;
  for(let i=0;i<limit;i++){
    const cached=Number(_messageVirtualHeightCache[i]);
    offset+=(Number.isFinite(cached)&&cached>0)?cached:_messageVirtualDefaultHeightForRole(_messageVirtualRoleForEntry(visWithIdx[i]));
  }
  const viewport=container?Math.max(0,Number(container.clientHeight)||0):0;
  return Math.max(0,Math.round(offset-(viewport*0.35)));
}
function _messageVirtualKeepTailCount(){
  return Math.min(_currentMessageRenderWindowSize(), MESSAGE_RENDER_WINDOW_DEFAULT);
}


export {
  assistantDisplayName,
  _browserReportsOnline,
  _offlineHealthUrl,
  _setOfflineChecking,
  _renderOfflineBanner,
  _startOfflineProbeTimer,
  _stopOfflineProbeTimer,
  showOfflineBanner,
  isOfflineBannerVisible,
  _hideOfflineBanner,
  _isAbortError,
  _patchOfflineFetch,
  initOfflineMonitor,
  _redirectIfUnauth,
  _getSessionQueue,
  _queueStorageKey,
  _clearPersistedSessionQueue,
  _persistSessionQueueStorage,
  _readPersistedSessionQueue,
  queueSessionMessage,
  shiftQueuedSessionMessage,
  getQueuedSessionCount,
  _compressionSessionLock,
  _setCompressionSessionLock,
  _matchBacktickFenceLine,
  _isBacktickFenceClose,
  _stripWorkspaceDisplayPrefix,
  _renderUserFencedBlocks,
  _statusCardHtml,
  _compressionRecoveryHtml,
  _activeCompressionRecoveryPayload,
  isGenericCompressionContinuationIntent,
  shouldInterceptCompressionRecoveryContinuation,
  showCompressionRecoveryContinuationHint,
  _messageVirtualDefaultHeightForRole,
  _markMessageVirtualScrollActive,
  clearVisibleMessageRowCache,
  _clearMessageVirtualHeightCache,
  _resetMessageRenderWindow,
  _cancelMessageVirtualizedRender,
  _messageIsRenderable,
  _getVisibleMessagesWithIdx,
  _messageVirtualWindow,
  _messageVirtualSpacer,
  _messageVirtualWindowKeyFor,
  _messageVirtualMeasurementCycleKeyFor,
  _scheduleMessageVirtualMeasurementRefresh,
  _markMessageVirtualMeasurementsSettled,
  _messageVirtualHeightEntryMatches,
  _messageVirtualHeightPrefixEntryMatches,
  _syncMessageVirtualHeightCache,
  _messageVirtualRoleForEntry,
  _currentMessageVirtualWindow,
  _messageVirtualPrependedHeightDelta,
  _messageVisibleIndexForRawIdx,
  _safeEncodeURIComponent,
  _messageViewportAnchorKeyForMessage,
  _messageVisibleIndexForAnchorKey,
  _messageSessionIndexBase,
  _messageSessionIndexForRawIdx,
  _messageRawIdxForSessionIndex,
  _messageVirtualScrollTopForVisibleIdx,
  _messageVirtualKeepTailCount,
  _probeOfflineRecovery,
  _showOfflineBannerIfProbeFails,
  checkOfflineRecoveryNow,
  _recoverFromOfflineSoftly,
  startCompressionRecovery,
  S,
  INFLIGHT,
  SESSION_QUEUES,
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_MB,
  $,
  OFFLINE_RECHECK_MS,
  OFFLINE_HEALTH_TIMEOUT_MS,
  OFFLINE_FETCH_FAILURES_BEFORE_BANNER,
  esc,
  MESSAGE_RENDER_WINDOW_DEFAULT,
  MESSAGE_VIRTUAL_THRESHOLD_ROWS,
  MESSAGE_VIRTUAL_BUFFER_PX,
  MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS,
  MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS,
  _recycleStash,
  _recycleResetAttrs,
  _queueDrainSid,
  _offlineVisible,
  _offlineReason,
  _offlineProbeTimer,
  _offlineChecking,
  _offlineProbePromise,
  _offlineHealthProbePromise,
  _offlineFetchProbeFailures,
  _offlineRawFetch,
  _offlineFetchPatched,
  _messageRenderWindowSid,
  _messageRenderWindowSize,
  _messageVirtualHeightCache,
  _messageVirtualHeightCacheEntries,
  _messageVirtualHeightCacheLen,
  _messageVirtualHeightCacheSrc,
  _messageVirtualEstimatedRowHeight,
  _messageVirtualScrollRaf,
  _messageVirtualWindowKey,
  _messageVirtualMeasurementCycleKey,
  _messageVirtualMeasurementRetryCount,
  _messageVirtualScrollActive,
  _messageVirtualScrollSettleTimer,
  _messageVirtualDeferredMeasurement,
  _msgNodeRecycleEnabled,
  _scrollbarDragActive,
  _visWithIdxCache,
  _visWithIdxCacheLen,
  _visWithIdxCacheSrc,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  assistantDisplayName: { enumerable: true, get: () => assistantDisplayName, set: (value) => { assistantDisplayName = value; } },
  _browserReportsOnline: { enumerable: true, get: () => _browserReportsOnline, set: (value) => { _browserReportsOnline = value; } },
  _offlineHealthUrl: { enumerable: true, get: () => _offlineHealthUrl, set: (value) => { _offlineHealthUrl = value; } },
  _setOfflineChecking: { enumerable: true, get: () => _setOfflineChecking, set: (value) => { _setOfflineChecking = value; } },
  _renderOfflineBanner: { enumerable: true, get: () => _renderOfflineBanner, set: (value) => { _renderOfflineBanner = value; } },
  _startOfflineProbeTimer: { enumerable: true, get: () => _startOfflineProbeTimer, set: (value) => { _startOfflineProbeTimer = value; } },
  _stopOfflineProbeTimer: { enumerable: true, get: () => _stopOfflineProbeTimer, set: (value) => { _stopOfflineProbeTimer = value; } },
  showOfflineBanner: { enumerable: true, get: () => showOfflineBanner, set: (value) => { showOfflineBanner = value; } },
  isOfflineBannerVisible: { enumerable: true, get: () => isOfflineBannerVisible, set: (value) => { isOfflineBannerVisible = value; } },
  _hideOfflineBanner: { enumerable: true, get: () => _hideOfflineBanner, set: (value) => { _hideOfflineBanner = value; } },
  _isAbortError: { enumerable: true, get: () => _isAbortError, set: (value) => { _isAbortError = value; } },
  _patchOfflineFetch: { enumerable: true, get: () => _patchOfflineFetch, set: (value) => { _patchOfflineFetch = value; } },
  initOfflineMonitor: { enumerable: true, get: () => initOfflineMonitor, set: (value) => { initOfflineMonitor = value; } },
  _redirectIfUnauth: { enumerable: true, get: () => _redirectIfUnauth, set: (value) => { _redirectIfUnauth = value; } },
  _getSessionQueue: { enumerable: true, get: () => _getSessionQueue, set: (value) => { _getSessionQueue = value; } },
  _queueStorageKey: { enumerable: true, get: () => _queueStorageKey, set: (value) => { _queueStorageKey = value; } },
  _clearPersistedSessionQueue: { enumerable: true, get: () => _clearPersistedSessionQueue, set: (value) => { _clearPersistedSessionQueue = value; } },
  _persistSessionQueueStorage: { enumerable: true, get: () => _persistSessionQueueStorage, set: (value) => { _persistSessionQueueStorage = value; } },
  _readPersistedSessionQueue: { enumerable: true, get: () => _readPersistedSessionQueue, set: (value) => { _readPersistedSessionQueue = value; } },
  queueSessionMessage: { enumerable: true, get: () => queueSessionMessage, set: (value) => { queueSessionMessage = value; } },
  shiftQueuedSessionMessage: { enumerable: true, get: () => shiftQueuedSessionMessage, set: (value) => { shiftQueuedSessionMessage = value; } },
  getQueuedSessionCount: { enumerable: true, get: () => getQueuedSessionCount, set: (value) => { getQueuedSessionCount = value; } },
  _compressionSessionLock: { enumerable: true, get: () => _compressionSessionLock, set: (value) => { _compressionSessionLock = value; } },
  _setCompressionSessionLock: { enumerable: true, get: () => _setCompressionSessionLock, set: (value) => { _setCompressionSessionLock = value; } },
  _matchBacktickFenceLine: { enumerable: true, get: () => _matchBacktickFenceLine, set: (value) => { _matchBacktickFenceLine = value; } },
  _isBacktickFenceClose: { enumerable: true, get: () => _isBacktickFenceClose, set: (value) => { _isBacktickFenceClose = value; } },
  _stripWorkspaceDisplayPrefix: { enumerable: true, get: () => _stripWorkspaceDisplayPrefix, set: (value) => { _stripWorkspaceDisplayPrefix = value; } },
  _renderUserFencedBlocks: { enumerable: true, get: () => _renderUserFencedBlocks, set: (value) => { _renderUserFencedBlocks = value; } },
  _statusCardHtml: { enumerable: true, get: () => _statusCardHtml, set: (value) => { _statusCardHtml = value; } },
  _compressionRecoveryHtml: { enumerable: true, get: () => _compressionRecoveryHtml, set: (value) => { _compressionRecoveryHtml = value; } },
  _activeCompressionRecoveryPayload: { enumerable: true, get: () => _activeCompressionRecoveryPayload, set: (value) => { _activeCompressionRecoveryPayload = value; } },
  isGenericCompressionContinuationIntent: { enumerable: true, get: () => isGenericCompressionContinuationIntent, set: (value) => { isGenericCompressionContinuationIntent = value; } },
  shouldInterceptCompressionRecoveryContinuation: { enumerable: true, get: () => shouldInterceptCompressionRecoveryContinuation, set: (value) => { shouldInterceptCompressionRecoveryContinuation = value; } },
  showCompressionRecoveryContinuationHint: { enumerable: true, get: () => showCompressionRecoveryContinuationHint, set: (value) => { showCompressionRecoveryContinuationHint = value; } },
  _messageVirtualDefaultHeightForRole: { enumerable: true, get: () => _messageVirtualDefaultHeightForRole, set: (value) => { _messageVirtualDefaultHeightForRole = value; } },
  _markMessageVirtualScrollActive: { enumerable: true, get: () => _markMessageVirtualScrollActive, set: (value) => { _markMessageVirtualScrollActive = value; } },
  clearVisibleMessageRowCache: { enumerable: true, get: () => clearVisibleMessageRowCache, set: (value) => { clearVisibleMessageRowCache = value; } },
  _clearMessageVirtualHeightCache: { enumerable: true, get: () => _clearMessageVirtualHeightCache, set: (value) => { _clearMessageVirtualHeightCache = value; } },
  _resetMessageRenderWindow: { enumerable: true, get: () => _resetMessageRenderWindow, set: (value) => { _resetMessageRenderWindow = value; } },
  _cancelMessageVirtualizedRender: { enumerable: true, get: () => _cancelMessageVirtualizedRender, set: (value) => { _cancelMessageVirtualizedRender = value; } },
  _messageIsRenderable: { enumerable: true, get: () => _messageIsRenderable, set: (value) => { _messageIsRenderable = value; } },
  _getVisibleMessagesWithIdx: { enumerable: true, get: () => _getVisibleMessagesWithIdx, set: (value) => { _getVisibleMessagesWithIdx = value; } },
  _messageVirtualWindow: { enumerable: true, get: () => _messageVirtualWindow, set: (value) => { _messageVirtualWindow = value; } },
  _messageVirtualSpacer: { enumerable: true, get: () => _messageVirtualSpacer, set: (value) => { _messageVirtualSpacer = value; } },
  _messageVirtualWindowKeyFor: { enumerable: true, get: () => _messageVirtualWindowKeyFor, set: (value) => { _messageVirtualWindowKeyFor = value; } },
  _messageVirtualMeasurementCycleKeyFor: { enumerable: true, get: () => _messageVirtualMeasurementCycleKeyFor, set: (value) => { _messageVirtualMeasurementCycleKeyFor = value; } },
  _scheduleMessageVirtualMeasurementRefresh: { enumerable: true, get: () => _scheduleMessageVirtualMeasurementRefresh, set: (value) => { _scheduleMessageVirtualMeasurementRefresh = value; } },
  _markMessageVirtualMeasurementsSettled: { enumerable: true, get: () => _markMessageVirtualMeasurementsSettled, set: (value) => { _markMessageVirtualMeasurementsSettled = value; } },
  _messageVirtualHeightEntryMatches: { enumerable: true, get: () => _messageVirtualHeightEntryMatches, set: (value) => { _messageVirtualHeightEntryMatches = value; } },
  _messageVirtualHeightPrefixEntryMatches: { enumerable: true, get: () => _messageVirtualHeightPrefixEntryMatches, set: (value) => { _messageVirtualHeightPrefixEntryMatches = value; } },
  _syncMessageVirtualHeightCache: { enumerable: true, get: () => _syncMessageVirtualHeightCache, set: (value) => { _syncMessageVirtualHeightCache = value; } },
  _messageVirtualRoleForEntry: { enumerable: true, get: () => _messageVirtualRoleForEntry, set: (value) => { _messageVirtualRoleForEntry = value; } },
  _currentMessageVirtualWindow: { enumerable: true, get: () => _currentMessageVirtualWindow, set: (value) => { _currentMessageVirtualWindow = value; } },
  _messageVirtualPrependedHeightDelta: { enumerable: true, get: () => _messageVirtualPrependedHeightDelta, set: (value) => { _messageVirtualPrependedHeightDelta = value; } },
  _messageVisibleIndexForRawIdx: { enumerable: true, get: () => _messageVisibleIndexForRawIdx, set: (value) => { _messageVisibleIndexForRawIdx = value; } },
  _safeEncodeURIComponent: { enumerable: true, get: () => _safeEncodeURIComponent, set: (value) => { _safeEncodeURIComponent = value; } },
  _messageViewportAnchorKeyForMessage: { enumerable: true, get: () => _messageViewportAnchorKeyForMessage, set: (value) => { _messageViewportAnchorKeyForMessage = value; } },
  _messageVisibleIndexForAnchorKey: { enumerable: true, get: () => _messageVisibleIndexForAnchorKey, set: (value) => { _messageVisibleIndexForAnchorKey = value; } },
  _messageSessionIndexBase: { enumerable: true, get: () => _messageSessionIndexBase, set: (value) => { _messageSessionIndexBase = value; } },
  _messageSessionIndexForRawIdx: { enumerable: true, get: () => _messageSessionIndexForRawIdx, set: (value) => { _messageSessionIndexForRawIdx = value; } },
  _messageRawIdxForSessionIndex: { enumerable: true, get: () => _messageRawIdxForSessionIndex, set: (value) => { _messageRawIdxForSessionIndex = value; } },
  _messageVirtualScrollTopForVisibleIdx: { enumerable: true, get: () => _messageVirtualScrollTopForVisibleIdx, set: (value) => { _messageVirtualScrollTopForVisibleIdx = value; } },
  _messageVirtualKeepTailCount: { enumerable: true, get: () => _messageVirtualKeepTailCount, set: (value) => { _messageVirtualKeepTailCount = value; } },
  _probeOfflineRecovery: { enumerable: true, get: () => _probeOfflineRecovery, set: (value) => { _probeOfflineRecovery = value; } },
  _showOfflineBannerIfProbeFails: { enumerable: true, get: () => _showOfflineBannerIfProbeFails, set: (value) => { _showOfflineBannerIfProbeFails = value; } },
  checkOfflineRecoveryNow: { enumerable: true, get: () => checkOfflineRecoveryNow, set: (value) => { checkOfflineRecoveryNow = value; } },
  _recoverFromOfflineSoftly: { enumerable: true, get: () => _recoverFromOfflineSoftly, set: (value) => { _recoverFromOfflineSoftly = value; } },
  startCompressionRecovery: { enumerable: true, get: () => startCompressionRecovery, set: (value) => { startCompressionRecovery = value; } },
  S: { enumerable: true, get: () => S },
  INFLIGHT: { enumerable: true, get: () => INFLIGHT },
  SESSION_QUEUES: { enumerable: true, get: () => SESSION_QUEUES },
  MAX_UPLOAD_BYTES: { enumerable: true, get: () => MAX_UPLOAD_BYTES },
  MAX_UPLOAD_MB: { enumerable: true, get: () => MAX_UPLOAD_MB },
  $: { enumerable: true, get: () => $ },
  OFFLINE_RECHECK_MS: { enumerable: true, get: () => OFFLINE_RECHECK_MS },
  OFFLINE_HEALTH_TIMEOUT_MS: { enumerable: true, get: () => OFFLINE_HEALTH_TIMEOUT_MS },
  OFFLINE_FETCH_FAILURES_BEFORE_BANNER: { enumerable: true, get: () => OFFLINE_FETCH_FAILURES_BEFORE_BANNER },
  esc: { enumerable: true, get: () => esc },
  MESSAGE_RENDER_WINDOW_DEFAULT: { enumerable: true, get: () => MESSAGE_RENDER_WINDOW_DEFAULT },
  MESSAGE_VIRTUAL_THRESHOLD_ROWS: { enumerable: true, get: () => MESSAGE_VIRTUAL_THRESHOLD_ROWS },
  MESSAGE_VIRTUAL_BUFFER_PX: { enumerable: true, get: () => MESSAGE_VIRTUAL_BUFFER_PX },
  MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS: { enumerable: true, get: () => MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS },
  MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS: { enumerable: true, get: () => MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS },
  _recycleStash: { enumerable: true, get: () => _recycleStash },
  _recycleResetAttrs: { enumerable: true, get: () => _recycleResetAttrs },
  _queueDrainSid: { enumerable: true, get: () => _queueDrainSid, set: (value) => { _queueDrainSid = value; } },
  _offlineVisible: { enumerable: true, get: () => _offlineVisible, set: (value) => { _offlineVisible = value; } },
  _offlineReason: { enumerable: true, get: () => _offlineReason, set: (value) => { _offlineReason = value; } },
  _offlineProbeTimer: { enumerable: true, get: () => _offlineProbeTimer, set: (value) => { _offlineProbeTimer = value; } },
  _offlineChecking: { enumerable: true, get: () => _offlineChecking, set: (value) => { _offlineChecking = value; } },
  _offlineProbePromise: { enumerable: true, get: () => _offlineProbePromise, set: (value) => { _offlineProbePromise = value; } },
  _offlineHealthProbePromise: { enumerable: true, get: () => _offlineHealthProbePromise, set: (value) => { _offlineHealthProbePromise = value; } },
  _offlineFetchProbeFailures: { enumerable: true, get: () => _offlineFetchProbeFailures, set: (value) => { _offlineFetchProbeFailures = value; } },
  _offlineRawFetch: { enumerable: true, get: () => _offlineRawFetch, set: (value) => { _offlineRawFetch = value; } },
  _offlineFetchPatched: { enumerable: true, get: () => _offlineFetchPatched, set: (value) => { _offlineFetchPatched = value; } },
  _messageRenderWindowSid: { enumerable: true, get: () => _messageRenderWindowSid, set: (value) => { _messageRenderWindowSid = value; } },
  _messageRenderWindowSize: { enumerable: true, get: () => _messageRenderWindowSize, set: (value) => { _messageRenderWindowSize = value; } },
  _messageVirtualHeightCache: { enumerable: true, get: () => _messageVirtualHeightCache, set: (value) => { _messageVirtualHeightCache = value; } },
  _messageVirtualHeightCacheEntries: { enumerable: true, get: () => _messageVirtualHeightCacheEntries, set: (value) => { _messageVirtualHeightCacheEntries = value; } },
  _messageVirtualHeightCacheLen: { enumerable: true, get: () => _messageVirtualHeightCacheLen, set: (value) => { _messageVirtualHeightCacheLen = value; } },
  _messageVirtualHeightCacheSrc: { enumerable: true, get: () => _messageVirtualHeightCacheSrc, set: (value) => { _messageVirtualHeightCacheSrc = value; } },
  _messageVirtualEstimatedRowHeight: { enumerable: true, get: () => _messageVirtualEstimatedRowHeight, set: (value) => { _messageVirtualEstimatedRowHeight = value; } },
  _messageVirtualScrollRaf: { enumerable: true, get: () => _messageVirtualScrollRaf, set: (value) => { _messageVirtualScrollRaf = value; } },
  _messageVirtualWindowKey: { enumerable: true, get: () => _messageVirtualWindowKey, set: (value) => { _messageVirtualWindowKey = value; } },
  _messageVirtualMeasurementCycleKey: { enumerable: true, get: () => _messageVirtualMeasurementCycleKey, set: (value) => { _messageVirtualMeasurementCycleKey = value; } },
  _messageVirtualMeasurementRetryCount: { enumerable: true, get: () => _messageVirtualMeasurementRetryCount, set: (value) => { _messageVirtualMeasurementRetryCount = value; } },
  _messageVirtualScrollActive: { enumerable: true, get: () => _messageVirtualScrollActive, set: (value) => { _messageVirtualScrollActive = value; } },
  _messageVirtualScrollSettleTimer: { enumerable: true, get: () => _messageVirtualScrollSettleTimer, set: (value) => { _messageVirtualScrollSettleTimer = value; } },
  _messageVirtualDeferredMeasurement: { enumerable: true, get: () => _messageVirtualDeferredMeasurement, set: (value) => { _messageVirtualDeferredMeasurement = value; } },
  _msgNodeRecycleEnabled: { enumerable: true, get: () => _msgNodeRecycleEnabled, set: (value) => { _msgNodeRecycleEnabled = value; } },
  _scrollbarDragActive: { enumerable: true, get: () => _scrollbarDragActive, set: (value) => { _scrollbarDragActive = value; } },
  _visWithIdxCache: { enumerable: true, get: () => _visWithIdxCache, set: (value) => { _visWithIdxCache = value; } },
  _visWithIdxCacheLen: { enumerable: true, get: () => _visWithIdxCacheLen, set: (value) => { _visWithIdxCacheLen = value; } },
  _visWithIdxCacheSrc: { enumerable: true, get: () => _visWithIdxCacheSrc, set: (value) => { _visWithIdxCacheSrc = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
