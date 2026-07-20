import { _compressionMessageAnchorKey, _isContextCompactionMessage, _isPreservedCompressionTaskListMessage } from './compression-ui.js';
import { _assistantMessageHasVisibleContent, _isRecoveryControlMessage, _messageHasReasoningPayload, msgContent } from './assistant-turn-presentation.js';
import { $, S } from './state.js';

const _virtualizationLifecycle={
  clearRenderCache:()=>{},
  clearUserRowIntrinsicHeightCache:()=>{},
  currentRenderWindowSize:()=>MESSAGE_RENDER_WINDOW_DEFAULT,
  scheduleRender:()=>{},
};
function registerMessageVirtualizationLifecycle(adapter){
  if(!adapter||typeof adapter!=='object') return;
  for(const name of Object.keys(_virtualizationLifecycle)){
    if(typeof adapter[name]==='function') _virtualizationLifecycle[name]=adapter[name];
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
  if(typeof _virtualizationLifecycle!=='undefined'){
    _virtualizationLifecycle.clearUserRowIntrinsicHeightCache();
  }
}
function _resetMessageRenderWindow(sid){
  _messageRenderWindowSid=sid||null;
  _messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT;
  _cancelMessageVirtualizedRender();
  _virtualizationLifecycle.clearRenderCache();
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
  requestAnimationFrame(()=>{ _virtualizationLifecycle.scheduleRender(true); });
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
  const current=(typeof _virtualizationLifecycle!=='undefined')
    ? _virtualizationLifecycle.currentRenderWindowSize()
    : Math.max(MESSAGE_RENDER_WINDOW_DEFAULT,Number(_messageRenderWindowSize)||MESSAGE_RENDER_WINDOW_DEFAULT);
  return Math.min(current, MESSAGE_RENDER_WINDOW_DEFAULT);
}

export {
  registerMessageVirtualizationLifecycle,
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
  MESSAGE_RENDER_WINDOW_DEFAULT,
  MESSAGE_VIRTUAL_THRESHOLD_ROWS,
  MESSAGE_VIRTUAL_BUFFER_PX,
  MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS,
  MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS,
  _recycleStash,
  _recycleResetAttrs,
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

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  registerMessageVirtualizationLifecycle: { enumerable:true, get:()=>registerMessageVirtualizationLifecycle, set:value=>{ registerMessageVirtualizationLifecycle=value; } },
  _messageVirtualDefaultHeightForRole: { enumerable:true, get:()=>_messageVirtualDefaultHeightForRole, set:value=>{ _messageVirtualDefaultHeightForRole=value; } },
  _markMessageVirtualScrollActive: { enumerable:true, get:()=>_markMessageVirtualScrollActive, set:value=>{ _markMessageVirtualScrollActive=value; } },
  clearVisibleMessageRowCache: { enumerable:true, get:()=>clearVisibleMessageRowCache, set:value=>{ clearVisibleMessageRowCache=value; } },
  _clearMessageVirtualHeightCache: { enumerable:true, get:()=>_clearMessageVirtualHeightCache, set:value=>{ _clearMessageVirtualHeightCache=value; } },
  _resetMessageRenderWindow: { enumerable:true, get:()=>_resetMessageRenderWindow, set:value=>{ _resetMessageRenderWindow=value; } },
  _cancelMessageVirtualizedRender: { enumerable:true, get:()=>_cancelMessageVirtualizedRender, set:value=>{ _cancelMessageVirtualizedRender=value; } },
  _messageIsRenderable: { enumerable:true, get:()=>_messageIsRenderable, set:value=>{ _messageIsRenderable=value; } },
  _getVisibleMessagesWithIdx: { enumerable:true, get:()=>_getVisibleMessagesWithIdx, set:value=>{ _getVisibleMessagesWithIdx=value; } },
  _messageVirtualWindow: { enumerable:true, get:()=>_messageVirtualWindow, set:value=>{ _messageVirtualWindow=value; } },
  _messageVirtualSpacer: { enumerable:true, get:()=>_messageVirtualSpacer, set:value=>{ _messageVirtualSpacer=value; } },
  _messageVirtualWindowKeyFor: { enumerable:true, get:()=>_messageVirtualWindowKeyFor, set:value=>{ _messageVirtualWindowKeyFor=value; } },
  _messageVirtualMeasurementCycleKeyFor: { enumerable:true, get:()=>_messageVirtualMeasurementCycleKeyFor, set:value=>{ _messageVirtualMeasurementCycleKeyFor=value; } },
  _scheduleMessageVirtualMeasurementRefresh: { enumerable:true, get:()=>_scheduleMessageVirtualMeasurementRefresh, set:value=>{ _scheduleMessageVirtualMeasurementRefresh=value; } },
  _markMessageVirtualMeasurementsSettled: { enumerable:true, get:()=>_markMessageVirtualMeasurementsSettled, set:value=>{ _markMessageVirtualMeasurementsSettled=value; } },
  _messageVirtualHeightEntryMatches: { enumerable:true, get:()=>_messageVirtualHeightEntryMatches, set:value=>{ _messageVirtualHeightEntryMatches=value; } },
  _messageVirtualHeightPrefixEntryMatches: { enumerable:true, get:()=>_messageVirtualHeightPrefixEntryMatches, set:value=>{ _messageVirtualHeightPrefixEntryMatches=value; } },
  _syncMessageVirtualHeightCache: { enumerable:true, get:()=>_syncMessageVirtualHeightCache, set:value=>{ _syncMessageVirtualHeightCache=value; } },
  _messageVirtualRoleForEntry: { enumerable:true, get:()=>_messageVirtualRoleForEntry, set:value=>{ _messageVirtualRoleForEntry=value; } },
  _currentMessageVirtualWindow: { enumerable:true, get:()=>_currentMessageVirtualWindow, set:value=>{ _currentMessageVirtualWindow=value; } },
  _messageVirtualPrependedHeightDelta: { enumerable:true, get:()=>_messageVirtualPrependedHeightDelta, set:value=>{ _messageVirtualPrependedHeightDelta=value; } },
  _messageVisibleIndexForRawIdx: { enumerable:true, get:()=>_messageVisibleIndexForRawIdx, set:value=>{ _messageVisibleIndexForRawIdx=value; } },
  _safeEncodeURIComponent: { enumerable:true, get:()=>_safeEncodeURIComponent, set:value=>{ _safeEncodeURIComponent=value; } },
  _messageViewportAnchorKeyForMessage: { enumerable:true, get:()=>_messageViewportAnchorKeyForMessage, set:value=>{ _messageViewportAnchorKeyForMessage=value; } },
  _messageVisibleIndexForAnchorKey: { enumerable:true, get:()=>_messageVisibleIndexForAnchorKey, set:value=>{ _messageVisibleIndexForAnchorKey=value; } },
  _messageSessionIndexBase: { enumerable:true, get:()=>_messageSessionIndexBase, set:value=>{ _messageSessionIndexBase=value; } },
  _messageSessionIndexForRawIdx: { enumerable:true, get:()=>_messageSessionIndexForRawIdx, set:value=>{ _messageSessionIndexForRawIdx=value; } },
  _messageRawIdxForSessionIndex: { enumerable:true, get:()=>_messageRawIdxForSessionIndex, set:value=>{ _messageRawIdxForSessionIndex=value; } },
  _messageVirtualScrollTopForVisibleIdx: { enumerable:true, get:()=>_messageVirtualScrollTopForVisibleIdx, set:value=>{ _messageVirtualScrollTopForVisibleIdx=value; } },
  _messageVirtualKeepTailCount: { enumerable:true, get:()=>_messageVirtualKeepTailCount, set:value=>{ _messageVirtualKeepTailCount=value; } },
  MESSAGE_RENDER_WINDOW_DEFAULT: { enumerable:true, get:()=>MESSAGE_RENDER_WINDOW_DEFAULT },
  MESSAGE_VIRTUAL_THRESHOLD_ROWS: { enumerable:true, get:()=>MESSAGE_VIRTUAL_THRESHOLD_ROWS },
  MESSAGE_VIRTUAL_BUFFER_PX: { enumerable:true, get:()=>MESSAGE_VIRTUAL_BUFFER_PX },
  MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS: { enumerable:true, get:()=>MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS },
  MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS: { enumerable:true, get:()=>MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS },
  _recycleStash: { enumerable:true, get:()=>_recycleStash },
  _recycleResetAttrs: { enumerable:true, get:()=>_recycleResetAttrs },
  _messageRenderWindowSid: { enumerable:true, get:()=>_messageRenderWindowSid, set:value=>{ _messageRenderWindowSid=value; } },
  _messageRenderWindowSize: { enumerable:true, get:()=>_messageRenderWindowSize, set:value=>{ _messageRenderWindowSize=value; } },
  _messageVirtualHeightCache: { enumerable:true, get:()=>_messageVirtualHeightCache, set:value=>{ _messageVirtualHeightCache=value; } },
  _messageVirtualHeightCacheEntries: { enumerable:true, get:()=>_messageVirtualHeightCacheEntries, set:value=>{ _messageVirtualHeightCacheEntries=value; } },
  _messageVirtualHeightCacheLen: { enumerable:true, get:()=>_messageVirtualHeightCacheLen, set:value=>{ _messageVirtualHeightCacheLen=value; } },
  _messageVirtualHeightCacheSrc: { enumerable:true, get:()=>_messageVirtualHeightCacheSrc, set:value=>{ _messageVirtualHeightCacheSrc=value; } },
  _messageVirtualEstimatedRowHeight: { enumerable:true, get:()=>_messageVirtualEstimatedRowHeight, set:value=>{ _messageVirtualEstimatedRowHeight=value; } },
  _messageVirtualScrollRaf: { enumerable:true, get:()=>_messageVirtualScrollRaf, set:value=>{ _messageVirtualScrollRaf=value; } },
  _messageVirtualWindowKey: { enumerable:true, get:()=>_messageVirtualWindowKey, set:value=>{ _messageVirtualWindowKey=value; } },
  _messageVirtualMeasurementCycleKey: { enumerable:true, get:()=>_messageVirtualMeasurementCycleKey, set:value=>{ _messageVirtualMeasurementCycleKey=value; } },
  _messageVirtualMeasurementRetryCount: { enumerable:true, get:()=>_messageVirtualMeasurementRetryCount, set:value=>{ _messageVirtualMeasurementRetryCount=value; } },
  _messageVirtualScrollActive: { enumerable:true, get:()=>_messageVirtualScrollActive, set:value=>{ _messageVirtualScrollActive=value; } },
  _messageVirtualScrollSettleTimer: { enumerable:true, get:()=>_messageVirtualScrollSettleTimer, set:value=>{ _messageVirtualScrollSettleTimer=value; } },
  _messageVirtualDeferredMeasurement: { enumerable:true, get:()=>_messageVirtualDeferredMeasurement, set:value=>{ _messageVirtualDeferredMeasurement=value; } },
  _msgNodeRecycleEnabled: { enumerable:true, get:()=>_msgNodeRecycleEnabled, set:value=>{ _msgNodeRecycleEnabled=value; } },
  _scrollbarDragActive: { enumerable:true, get:()=>_scrollbarDragActive, set:value=>{ _scrollbarDragActive=value; } },
  _visWithIdxCache: { enumerable:true, get:()=>_visWithIdxCache, set:value=>{ _visWithIdxCache=value; } },
  _visWithIdxCacheLen: { enumerable:true, get:()=>_visWithIdxCacheLen, set:value=>{ _visWithIdxCacheLen=value; } },
  _visWithIdxCacheSrc: { enumerable:true, get:()=>_visWithIdxCacheSrc, set:value=>{ _visWithIdxCacheSrc=value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
