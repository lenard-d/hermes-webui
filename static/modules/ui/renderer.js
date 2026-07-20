import { _isKeepSettledWorklogOpenArmed } from './anchor-scenes.js';
import { _clearCompressionElapsedTimer, _deferClearProgrammaticScroll, _messageUserUnpinned } from './composer-controls.js';
import { _stripAttachedFilesMarkerForDisplay } from './composer.js';
import { _postProcessWithAnchorSuppression } from './content-postprocessing.js';
import { _liveAssistantSegmentTextLength } from './live-turn-recovery.js';
import { _assistantToolAnchorIdxForMessage, _cliPatchSnippetFromArgs, _cliToolCardHasDiffSnippet, _cliToolCardSnippet, _cliToolResultSnippet, _toolArgsSnapshot } from './cli-tool-presentation.js';
import { _compressionAnchorIndex, _compressionCardsNode, _compressionReferenceCardHtml, _compressionStateForCurrentSession, _formatMessageFooterTimestamp, _isContextCompactionMessage, _isMarkerOnlyAssistantCompressionMessage, _isPreservedCompressionTaskListMessage, _latestCompressionReferenceMessage, _latestPreservedCompressionTaskListMessages, _preservedCompressionTaskListCardsHtml, _shouldShowSettledCompressionReference, clearCompressionUi, renderCompressionUi } from './compression-ui.js';
import { _collectHandoffSummaryStates, _handoffCardsNode, _handoffStateForCurrentSession } from './handoff-ui.js';
import { _syncLiveRunStatusAfterRender } from './live-run-status.js';
import { _messageRenderCacheSignature, _sessionHtmlCache, _sessionHtmlCacheSid } from './message-render-cache.js';
import { _captureMessageScrollSnapshot } from './message-scroll-snapshot.js';
import { _initMediaPlaybackObserver, _renderAttachmentHtml } from './media-and-quota.js';
import { _applySessionNavigationPrefs, _applyUserRowIntrinsicHeight, _getCachedRender, _questionJumpButtonHtml, _rememberRenderedUserRowIntrinsicHeights, _updateMessageVirtualMeasurements, _userMessageDomId, _wireMessageWindowLoadEarlierButton } from './navigation.js';
import { _captureWorklogDetailDisclosureState, _decorateTransparentEventRow, _rehydrateTransparentStreamDom, _thinkingCardHtml, _transparentToolStatus, isCompactWorklogMode, isSimplifiedToolCalling, isTransparentStream } from './activity-presentation.js';
import { _ERR_MSG_RE, _assistantMessageBelongsInWorklog, _assistantReasoningPayloadText, _assistantRoleHtml, _assistantThinkingBelongsInWorklog, _assistantTurnBlocks, _assistantTurnFinalVisibleContentMap, _assistantTurnVisibleContentMap, _createAssistantTurn, _fmtDateSep, _formatTurnTps, _isAssistantEmptyPlaceholderContent, _setLatestAssistantTurnLandmark, _worklogReasoningTextFromMessage, isTpsDisplayEnabled, msgContent } from './assistant-turn-presentation.js';
import { _assistantTurnAnchorSettledFinalAnswer, _collectToolResultSnippetsByTid, _maybeRecoverVirtualizedBlankViewport, _reanchorPinnedTailAfterRender, _scrollAfterMessageRender, _transparentOrderedDisplayText, _transparentOrderedToolCall, _transparentStreamOrderedParts } from './render-support.js';
import { $, INFLIGHT, S, _activeCompressionRecoveryPayload, _compressionRecoveryHtml, _currentMessageVirtualWindow, _getVisibleMessagesWithIdx, _messageRenderWindowSid, _messageSessionIndexForRawIdx, _messageViewportAnchorKeyForMessage, _messageVirtualKeepTailCount, _messageVirtualSpacer, _messageVirtualWindowKey, _messageVirtualWindowKeyFor, _msgNodeRecycleEnabled, _recycleResetAttrs, _recycleStash, _resetMessageRenderWindow, _setCompressionSessionLock, _statusCardHtml, _stripWorkspaceDisplayPrefix, esc } from './state.js';
import { buildToolCard } from './tool-worklog.js';
import { _rehydrateDeferredWorklogsFromCache } from './transparent-worklog.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';
import { compatibilityBindings as messageRenderCacheBindings } from './message-render-cache.js';
import { compatibilityBindings as stateBindings } from './state.js';
import { captureLiveAssistantTurn, restoreLiveAssistantTurn } from './live-turn-preservation.js';
import { rebuildSettledActivity } from './settled-activity-renderer.js';
import { finalizeSettledTurns } from './settled-turn-finalization.js';

// Coordinates the ordered transcript rebuild transaction. Domain owners handle
// live parser-node preservation, persisted Activity reconstruction, and settled
// turn finalization; this function retains lifecycle ordering and row creation.
function renderMessages(options){
  composerControlsBindings._lastMessageRenderAt=performance.now();
  const preserveScroll=!!(options&&options.preserveScroll);
  const virtualFallback=!!(options&&options._virtualFallback);
  // Capture the pre-wipe scroll position when preserving OR when the reader has
  // manually unpinned; both need to restore the reader's position after the DOM
  // rebuild rather than snap to the bottom. (Codex #4006 r3 follow-up.)
  const scrollSnapshot=(preserveScroll||_messageUserUnpinned)?_captureMessageScrollSnapshot():null;
  const inner=$('msgInner');
  const sid=S.session?S.session.session_id:null;
  const msgCount=S.messages.length;
  // During session switch, S.messages is intentionally cleared while the full
  // message fetch is still in flight. Other async updates can still call
  // renderMessages() in this window. Keep the existing loading placeholder.
  if(_loadingSessionId===sid&&msgCount===0&&inner) return;
  if(sid!==_messageRenderWindowSid) _resetMessageRenderWindow(sid);
  let cachedRenderSignature=null;
  const hasTransientTranscriptUi=!!(
    (window._compressionUi&&(!window._compressionUi.sessionId||window._compressionUi.sessionId===sid)) ||
    (window._handoffUi&&(!window._handoffUi.sessionId||window._handoffUi.sessionId===sid))
  );

  const preservedCompressionTaskMessages=_latestPreservedCompressionTaskListMessages(S.messages);
  const visWithIdx=_getVisibleMessagesWithIdx();
  $('emptyState').style.display=(visWithIdx.length||preservedCompressionTaskMessages.length)?'none':'';
  const virtualWindow=virtualFallback
    ? {virtualized:false,start:0,end:visWithIdx.length,topPad:0,bottomPad:0,total:visWithIdx.length,tailStart:visWithIdx.length}
    : _currentMessageVirtualWindow(visWithIdx,_messageVirtualKeepTailCount());
  const renderWindowKey=_messageVirtualWindowKeyFor(virtualWindow);
  const windowStart=virtualWindow.start;
  const windowEnd=virtualWindow.end;
  const renderHeadVisWithIdx=visWithIdx.slice(windowStart, windowEnd);
  const renderTailStart=virtualWindow.virtualized?Math.max(windowEnd, virtualWindow.tailStart):windowEnd;
  const renderTailVisWithIdx=virtualWindow.virtualized&&renderTailStart<visWithIdx.length
    ? visWithIdx.slice(renderTailStart)
    : [];
  const renderVisWithIdx=renderHeadVisWithIdx.concat(renderTailVisWithIdx);
  const renderVisibleIdxs=[
    ...renderHeadVisWithIdx.map((_,idx)=>windowStart+idx),
    ...renderTailVisWithIdx.map((_,idx)=>renderTailStart+idx),
  ];
  const headRenderCount=renderHeadVisWithIdx.length;

  // Fast path: switching back to a previously rendered session with same count.
  // Guard: sid !== _sessionHtmlCacheSid ensures in-session updates (edits,
  // new messages, tool_complete) always get a fresh rebuild.
  // Skip cache if this session is still streaming — the live smd parser writes
  // into a DOM node inside the cached subtree; serving cached HTML detaches it.
  // Also skip cache for transient transcript cards such as /compress and
  // cross-channel handoff summaries; otherwise the cached transcript returns
  // before those cards can be inserted.
  if(sid&&sid!==_sessionHtmlCacheSid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi){
    const renderSignature=_messageRenderCacheSignature();
    cachedRenderSignature=renderSignature;
    const cached=_sessionHtmlCache.get(sid);
    if(cached&&cached.msgCount===msgCount&&cached.renderWindowKey===renderWindowKey&&cached.signature===renderSignature){
      inner.innerHTML=cached.html;
      stateBindings._messageVirtualWindowKey=renderWindowKey;
      messageRenderCacheBindings._sessionHtmlCacheSid=sid;
      _rehydrateTransparentStreamDom(inner);
      _rehydrateDeferredWorklogsFromCache(inner);
      _wireMessageWindowLoadEarlierButton();
      if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
      _scrollAfterMessageRender(preserveScroll, scrollSnapshot);
      if(_maybeRecoverVirtualizedBlankViewport(options, preserveScroll, virtualWindow)) return;
      _updateMessageVirtualMeasurements(renderVisWithIdx, renderVisibleIdxs, virtualWindow);
      requestAnimationFrame(()=>_postProcessWithAnchorSuppression(inner));
      if(typeof _initMediaPlaybackObserver==='function') _initMediaPlaybackObserver();
      if(typeof loadTodos==='function'&&document.getElementById('panelTodos')&&document.getElementById('panelTodos').classList.contains('active')){loadTodos();}
      return;
    }
  }
  const preservedLiveTurn=typeof captureLiveAssistantTurn==='function'
    ? captureLiveAssistantTurn(sid)
    : null;
  const compressionState=(()=>{
    let compressionState=_compressionStateForCurrentSession();
    if(!S.busy && compressionState && compressionState.automatic){
      window._compressionUi=null;
      _clearCompressionElapsedTimer();
      _setCompressionSessionLock(null);
      compressionState=null;
    }
    return compressionState;
  })();
  if(window._compressionUi && !compressionState) clearCompressionUi();
  const handoffState=_handoffStateForCurrentSession();
  if(window._handoffUi && !handoffState) window._handoffUi=null;
  const sessionCompressionAnchor=(
    S.session && typeof S.session.compression_anchor_visible_idx==='number'
  ) ? S.session.compression_anchor_visible_idx : null;
  const sessionCompressionAnchorKey=(
    S.session && S.session.compression_anchor_message_key && typeof S.session.compression_anchor_message_key==='object'
  ) ? S.session.compression_anchor_message_key : null;
  const sessionCompressionSummary=(
    S.session && typeof S.session.compression_anchor_summary==='string'
  ) ? S.session.compression_anchor_summary.trim() : '';
  const worklogDetailDisclosureState=_captureWorklogDetailDisclosureState(inner);
  _recycleStash.clear();
  if(_msgNodeRecycleEnabled){
    for(const child of Array.from(inner.children)){
      const key=child.dataset&&(child.dataset.recycleKey||child.dataset.msgIdx);
      if(!key) continue;
      if(child.id==='liveAssistantTurn'||child.querySelector&&child.querySelector('#liveAssistantTurn')) continue;
      _recycleStash.set(Number(key), child);
    }
  }
  // Mobile scroll-jank fix: temporarily disable overflow-anchor so Chromium
  // cannot re-anchor to the topmost row during the DOM wipe-and-rebuild gap.
  if(window._fixMobileScrollJank) window._fixMobileScrollJank();
  // Capture whether the reader was at/near the tail BEFORE the wipe. A tail-follower
  // hit by a mid-stream re-render gets a one-frame jitter: the wipe+rebuild lands the
  // sync scrollTop write against a transient layout whose above-viewport height is a
  // few px short of the settled value, so the browser clamps scrollTop a little high;
  // the settle rAF corrects it the next frame, producing a fast ~1-row back-and-forth
  // bounce. We remember the pre-wipe near-tail state here (geometry, not closure pin
  // flags — the wipe's clamp scroll event can transiently perturb those) so the tail
  // of renderMessages can re-anchor to the settled bottom before the intermediate is
  // painted. See _reanchorPinnedTailAfterRender + its queueMicrotask call site.
  const _preWipeNearTail=(()=>{
    const _m=$('messages');
    if(!_m) return false;
    return (_m.scrollHeight-_m.scrollTop-_m.clientHeight)<=8;
  })();
  // Pre-wipe capture: read the still-laid-out user rows' REAL heights before the wipe below
  // destroys them, and persist so the rebuild reserves the real off-screen height. This is
  // the non-virtualized analog of #5638's virtualized measure pass (which never runs when
  // _virtualizeTranscript===false). Without it, a fresh off-screen tall user row reserves
  // only the flat contain-intrinsic-size estimate, scrollHeight shrinks, and the browser
  // clamps scrollTop → the jump-back. Reading pre-wipe (not post-render) is what makes the
  // measurement reliable — the old elements have painted, so their rect height is real even
  // off-screen; a post-render read of a fresh off-screen row returns its collapsed reserve.
  if(typeof _rememberRenderedUserRowIntrinsicHeights==='function') _rememberRenderedUserRowIntrinsicHeights();
  // The DOM wipe can briefly collapse #msgInner to zero height, causing the
  // browser to clamp #messages.scrollTop to 0 and emit a scroll event.  That
  // event is a render artifact, not user intent; if the scroll listener sees it
  // with _programmaticScroll=false, it marks the reader manually unpinned and
  // the live reply stops following / appears to jump backward.
  composerControlsBindings._programmaticScroll=true;
  composerControlsBindings._programmaticScrollSetAt=performance.now();
  inner.innerHTML='';
  const compressionNode=compressionState?_compressionCardsNode(compressionState):null;
  const {message:referenceMessage, rawIdx:referenceMessageRawIdx}=_latestCompressionReferenceMessage(
    S.messages,
    sessionCompressionSummary
  );
  const referenceText=referenceMessage
    ? msgContent(referenceMessage)||String(referenceMessage.content||'')
    : sessionCompressionSummary;
  const referenceNode=(!compressionState && _shouldShowSettledCompressionReference(referenceText) && (sessionCompressionAnchor!==null || sessionCompressionAnchorKey || sessionCompressionSummary))
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(referenceText,false)}${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  let preservedCompressionTaskCardsAttached=!!referenceNode;
  const preservedCompressionRawIdxs=[];
  let rawIdx=0;
  for(const m of S.messages){
    if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
    if(_isPreservedCompressionTaskListMessage(m)){preservedCompressionRawIdxs.push(rawIdx);rawIdx++;continue;}
    rawIdx++;
  }
  const firstRenderedRawIdx=renderVisWithIdx.length?renderVisWithIdx[0].rawIdx:Infinity;
  const assistantTurnFinalVisibleContentByRawIdx=_assistantTurnFinalVisibleContentMap(visWithIdx);
  const assistantTurnVisibleContentByRawIdx=_assistantTurnVisibleContentMap(visWithIdx);
  const hasServerOlder=!!(typeof _messagesTruncated!=='undefined' && _messagesTruncated && S.messages.length>0);
  const serverOlderCount=hasServerOlder&&Number.isFinite(Number(_oldestIdx))?Math.max(0,Number(_oldestIdx)):0;
  if(typeof _applySessionNavigationPrefs==='function') _applySessionNavigationPrefs();
  if(virtualWindow.virtualized&&virtualWindow.topPad>0){
    inner.appendChild(_messageVirtualSpacer(virtualWindow.topPad,'before'));
  }
  if(hasServerOlder){
    const indicator=document.createElement('button');
    indicator.type='button';
    indicator.id='loadOlderIndicator';
    indicator.className='load-older-indicator message-window-load-earlier';
    indicator.textContent=serverOlderCount>0
      ? `Load earlier messages (${serverOlderCount} older)`
      : (typeof t==='function'?t('load_older_messages'):'Load earlier messages');
    inner.appendChild(indicator);
    _wireMessageWindowLoadEarlierButton();
  }
  let lastUserRawIdx=-1;
  for(let i=visWithIdx.length-1;i>=0;i--){
    if(visWithIdx[i].m&&visWithIdx[i].m.role==='user'){
      lastUserRawIdx=visWithIdx[i].rawIdx;
      break;
    }
  }
  const insertionAnchorFull=_compressionAnchorIndex(
    visWithIdx,
    compressionState ? compressionState.anchorMessageKey : sessionCompressionAnchorKey,
    compressionState
      ? (typeof compressionState.anchorVisibleIdx==='number' ? compressionState.anchorVisibleIdx : compressionState.anchorRawIdx)
      : sessionCompressionAnchor
  );
  let insertionAnchor=null;
  if(typeof insertionAnchorFull==='number'){
    const hasVirtualRenderGap=renderVisibleIdxs.some((idx,pos)=>idx!==windowStart+pos);
    if(!hasVirtualRenderGap){
      if(insertionAnchorFull<windowStart) insertionAnchor=renderVisWithIdx.length?0:null;
      else if(insertionAnchorFull<windowStart+renderVisWithIdx.length) insertionAnchor=insertionAnchorFull-windowStart;
      else insertionAnchor=renderVisWithIdx.length?renderVisWithIdx.length-1:null;
    }else if(renderVisibleIdxs.length){
      let previousVisibleIdx=-1;
      for(let i=0;i<renderVisibleIdxs.length;i++){
        if(renderVisibleIdxs[i]<=insertionAnchorFull) previousVisibleIdx=i;
        else break;
      }
      insertionAnchor=previousVisibleIdx>=0?previousVisibleIdx:0;
    }else{
      insertionAnchor=null;
    }
  }
  let _prevSepKey=null;
  let currentAssistantTurn=null;
  // Only build question→assistant mapping for the visible window, not the
  // full visWithIdx.  The jump-to-question button is only rendered for
  // assistant messages that appear in the current render window anyway.
  const questionRawIdxByAssistantRawIdx=new Map();
  let lastQuestionRawIdx=-1;
  const renderedRawIdxs=new Set(renderVisWithIdx.map(e=>e.rawIdx));
  const renderableRawIdxs=new Set(visWithIdx.map(e=>e.rawIdx));
  for(const entry of visWithIdx){
    const role=entry&&entry.m&&entry.m.role;
    if(role==='user') lastQuestionRawIdx=entry.rawIdx;
    else if(role==='assistant'&&renderedRawIdxs.has(entry.rawIdx)) questionRawIdxByAssistantRawIdx.set(entry.rawIdx,lastQuestionRawIdx);
  }
  const assistantRawIdxByQuestionRawIdx=new Map();
  for(const [aIdx,qIdx] of questionRawIdxByAssistantRawIdx){
    if(!assistantRawIdxByQuestionRawIdx.has(qIdx)) assistantRawIdxByQuestionRawIdx.set(qIdx,aIdx);
  }
  // #3709 (defect B): build a per-turn combined visible-answer text so the
  // thinking echo-strip can de-dupe a thinking-only message (whose own visible
  // body is empty) against the answer prose carried by a SIBLING message in the
  // same turn. A turn = the run of assistant messages between two user messages.
  // Map every assistant rawIdx in a run to the run's combined visible text.
  const _turnVisibleTextByRawIdx=new Map();
  {
    let _run=[]; let _runText=[];
    const _flush=()=>{
      if(_run.length){
        const combined=_runText.join('\n\n');
        for(const ri of _run) _turnVisibleTextByRawIdx.set(ri, combined);
      }
      _run=[]; _runText=[];
    };
    for(const entry of renderVisWithIdx){
      const em=entry&&entry.m; const role=em&&em.role;
      if(role==='assistant'){
        _run.push(entry.rawIdx);
        // Visible prose = content with any leading <think>…</think> /channel-thought
        // block stripped (the same blocks the per-message extractor removes below).
        let vis=typeof em.content==='string'?em.content:'';
        vis=vis.replace(/^\s*<think>[\s\S]*?<\/think>\s*/,'')
               .replace(/^\s*<\|channel\|?>thought\n?[\s\S]*?<channel\|>\s*/,'')
               .replace(/^\s*<\|turn\|>thinking\n[\s\S]*?<turn\|>\s*/,'').trim();
        if(vis) _runText.push(vis);
      }else{
        _flush();
      }
    }
    _flush();
  }

  const assistantSegments=new Map();
  const assistantThinking=new Map();
  const userRows=new Map();
  // Only collect tool-call assistant indices for messages that are actually
  // rendered in the current window.  S.toolCalls can grow large in long turns,
  // but we only need the ones whose assistant_msg_idx falls inside the visible
  // range.
  const toolCallAssistantIdxs=new Set();
  if(Array.isArray(S.toolCalls)){
    for(const tc of S.toolCalls){
      if(!tc) continue;
      const idx=tc.assistant_msg_idx;
      if(idx!==undefined && renderedRawIdxs.has(idx)){
        toolCallAssistantIdxs.add(idx);
      }
    }
  }
  const transparentOrderedToolIds=new Set();
  const transparentOrderedToolCallsByTid=new Map();
  // These scans only feed the transparent-stream ordered render path; skip the
  // O(messages×parts) work entirely in other modes (Opus perf finding #4932).
  const _transparentModeActive=(typeof isTransparentStream==='function')&&isTransparentStream();
  const transparentPersistedSnippetByTid={};
  if(_transparentModeActive){
    if(Array.isArray(S.toolCalls)){
      for(const tc of S.toolCalls){
        if(!tc||typeof tc!=='object') continue;
        const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
        if(tid&&!transparentOrderedToolCallsByTid.has(tid)) transparentOrderedToolCallsByTid.set(tid,tc);
      }
    }
    // #4927 durable fallback: the ordered path must consult the persisted
    // session.tool_calls snippet by tid too, or a cold/paginated load where the
    // S.messages tool_result join misses renders an empty body — and its inline
    // card then suppresses the post-loop derived card that WOULD have recovered.
    try{
      const persisted=(S.session&&Array.isArray(S.session.tool_calls))?S.session.tool_calls:[];
      persisted.forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const ptid=tc.tid||tc.id||tc.tool_call_id||tc.call_id||'';
        const psnip=tc.snippet||tc.result||tc.output||tc.preview||'';
        if(ptid&&psnip&&!transparentPersistedSnippetByTid[ptid]) transparentPersistedSnippetByTid[ptid]=String(psnip);
      });
    }catch(e){}
  }
  const transparentToolResultsByTid=_transparentModeActive?_collectToolResultSnippetsByTid(S.messages):{};
  const latestRenderedAssistantRawIdx=(()=>{
    for(let i=renderVisWithIdx.length-1;i>=0;i--){
      const entry=renderVisWithIdx[i];
      if(entry&&entry.m&&entry.m.role==='assistant'&&!entry.m._live) return entry.rawIdx;
    }
    return -1;
  })();
  // Windowed render loop replaces the legacy full loop:
  // for(let vi=0;vi<visWithIdx.length;vi++)
  for(let vi=0;vi<renderVisWithIdx.length;vi++){
    if(virtualWindow.virtualized&&virtualWindow.bottomPad>0&&vi===headRenderCount){
      // The virtual gap breaks assistant-turn adjacency. Reset the current
      // turn before rendering the always-visible tail so assistant segments do
      // not merge across the spacer boundary.
      currentAssistantTurn=null;
      inner.appendChild(_messageVirtualSpacer(virtualWindow.bottomPad,'after'));
    }
    const {m,rawIdx}=renderVisWithIdx[vi];
    const _tsSep=m._ts||m.timestamp;
    if(_tsSep){
      const _d=new Date(_tsSep*1000);
      const _key=_d.toDateString();
      if(_prevSepKey && _prevSepKey!==_key){
        const sep=document.createElement('div');
        sep.className='msg-date-sep';
        sep.textContent=_fmtDateSep(_d);
        inner.appendChild(sep);
      }
      _prevSepKey=_key;
    }
    let content=m.content||'';
    let thinkingText='';
    let orderedTransparentParts=_transparentStreamOrderedParts(m);
    if(Array.isArray(content)){
      content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
    }
    if(m.role==='assistant'&&!m._live&&typeof content==='string'){
      const anchorFinal=_assistantTurnAnchorSettledFinalAnswer(m, content, {
        session_id:sid,
        raw_idx:rawIdx,
      });
      if(anchorFinal!==null){
        content=anchorFinal;
        if(Array.isArray(orderedTransparentParts)){
          for(let i=orderedTransparentParts.length-1;i>=0;i--){
            if(orderedTransparentParts[i]&&orderedTransparentParts[i].kind==='text'){
              orderedTransparentParts[i]={...orderedTransparentParts[i], text:anchorFinal};
              break;
            }
          }
        }
      }
    }
    if(typeof content==='string'){
      if(typeof window!=='undefined'&&typeof window._extractInlineThinkingFromContentForRender==='function'){
        const split=window._extractInlineThinkingFromContentForRender(content, thinkingText);
        thinkingText=split.reasoning||thinkingText;
        content=split.content;
      }else if(!thinkingText){
        const thinkMatch=content.match(/^\s*<think>([\s\S]*?)<\/think>\s*/);
        if(thinkMatch){
          thinkingText=thinkMatch[1].trim();
          content=content.replace(/^\s*<think>[\s\S]*?<\/think>\s*/,'').trimStart();
        }
        if(!thinkingText){
          const gemmaMatch=content.match(/^\s*<\|channel\|?>thought\n?([\s\S]*?)<channel\|>\s*/);
          if(gemmaMatch){
            thinkingText=gemmaMatch[1].trim();
            content=content.replace(/^\s*<\|channel\|?>thought\n?[\s\S]*?<channel\|>\s*/,'').trimStart();
          }
        }
        if(!thinkingText){
          const gemmaTurnMatch=content.match(/^\s*<\|turn\|>thinking\n([\s\S]*?)<turn\|>\s*/);
          if(gemmaTurnMatch){
            thinkingText=gemmaTurnMatch[1].trim();
            content=content.replace(/^\s*<\|turn\|>thinking\n[\s\S]*?<turn\|>\s*/,'').trimStart();
          }
        }
      }
    }
    const isProcessWakeup=m&&m._source==='process_wakeup';
    const isUser=m.role==='user';
    if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m)){
      content='**Error:** No response received after context compression. Please retry.';
    }
    const displayContent=isUser?_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content)):content;
    const rowDisplayContent=displayContent;
    if(!isUser&&_isAssistantEmptyPlaceholderContent(m, displayContent)){
      content='';
    }
    if(!isUser&&(isCompactWorklogMode()||isTransparentStream())&&!thinkingText){
      const turnFinalVisibleContent=assistantTurnFinalVisibleContentByRawIdx.get(rawIdx)||'';
      const turnVisibleContents=assistantTurnVisibleContentByRawIdx.get(rawIdx)||[];
      thinkingText=_worklogReasoningTextFromMessage(m, rawIdx, toolCallAssistantIdxs, displayContent, turnFinalVisibleContent, turnVisibleContents);
    }
    const isLastAssistant=!isUser&&vi===renderVisWithIdx.length-1;
    const nextRendered=renderVisWithIdx[vi+1];
    const isTurnFinalAssistant=!isUser&&(!nextRendered||!nextRendered.m||nextRendered.m.role!=='assistant');
    let filesHtml='';
    if(m.attachments&&m.attachments.length){
      // Static regression tests intentionally look for msg-media-img/msg-file-badge near this branch.
      const _attachSid=(S.session&&S.session.session_id)||'';
      filesHtml=`<div class="msg-files">${m.attachments.map(f=>{
        const fLabel=typeof f==='string'?f:(f&&(f.name||f.filename||f.path))||'';
        const fname=String(fLabel).split('/').pop()||String(fLabel);
        // Use api/file/raw which resolves filename relative to the session workspace.
        const fileUrl='api/file/raw?session_id='+encodeURIComponent(_attachSid)+'&path='+encodeURIComponent(fname);
        return _renderAttachmentHtml(fname,fileUrl);
      }).join('')}</div>`;
    }
    let bodyHtml = _getCachedRender(displayContent, isUser);
    if(!isUser&&m.provider_details){
      const summary=m.provider_details_label||'Provider details';
      bodyHtml += `<details class="provider-error-details"><summary>${esc(String(summary))}</summary><pre><code>${esc(String(m.provider_details))}</code></pre></details>`;
    }
    const recoveryPayload=(!isUser&&m._compressionRecovery)
      ? m._compressionRecovery
      : (!isUser&&isLastAssistant&&isTurnFinalAssistant&&typeof _activeCompressionRecoveryPayload==='function' ? _activeCompressionRecoveryPayload() : null);
    const recoveryHtml=recoveryPayload ? _compressionRecoveryHtml(recoveryPayload, (S.session&&S.session.session_id)||'') : '';
    if(recoveryHtml) bodyHtml += recoveryHtml;
    const statusHtml = (!isUser&&m._statusCard) ? _statusCardHtml(m._statusCard) : '';
    const isEditableUser=isUser&&rawIdx===lastUserRawIdx;
    const editBtn  = isEditableUser ? `<button class="msg-action-btn" title="${t('edit_message')}" onclick="editMessage(this)">${li('pencil',13)}</button>` : '';
    const undoBtn  = isLastAssistant ? `<button class="msg-action-btn" title="${t('undo_exchange')}" onclick="undoLastExchange()">${li('undo',13)}</button>` : '';
    const retryBtn = isLastAssistant ? `<button class="msg-action-btn" title="${t('regenerate')}" onclick="regenerateResponse(this)">${li('rotate-ccw',13)}</button>` : '';
    const copyBtn  = `<button class="msg-copy-btn msg-action-btn" title="${t('copy')}" onclick="copyMsg(this)">${li('copy',13)}</button>`;
    const readOnlySession=typeof _isReadOnlySession==='function'
      ? _isReadOnlySession(S.session)
      : !!(S.session&&(S.session.read_only||S.session.is_read_only));
    const branchableReadOnlySession=typeof _isBranchableReadOnlySession==='function'
      ? _isBranchableReadOnlySession(S.session)
      : false;
    const forkBtn  = (readOnlySession&&!branchableReadOnlySession) ? '' : `<button class="msg-action-btn" title="${t('fork_from_here')}" onclick="forkFromMessage(${rawIdx+1})">${li('git-branch',13)}</button>`;
    const ttsBtn   = !isUser ? `<button class="msg-action-btn msg-tts-btn" title="${t('tts_listen')||'Listen'}" onclick="speakMessage(this)">${li('volume-2',13)}</button>` : '';
    const tsVal=m._ts||m.timestamp;
    // _formatInServerTz handles fractional-hour offsets (India +0530 etc.)
    // correctly via offset arithmetic; bare toLocaleString is the browser-tz fallback.
    const _fmtSv=(typeof _formatInServerTz==='function')?_formatInServerTz:null;
    const tsTitle=tsVal?(_fmtSv?_fmtSv(new Date(tsVal*1000),{}):new Date(tsVal*1000).toLocaleString()):'';
    const tsTime=_formatMessageFooterTimestamp(tsVal);
    const timeHtml = tsTime ? `<span class="msg-time" title="${esc(tsTitle)}">${tsTime}</span>` : '';
    // #3114: show jump-to-question on every assistant message that has a
    // resolvable question target, not just the turn-final one. Multi-step
    // turns (tool_call -> assistant -> tool_call -> assistant) otherwise
    // strip the button from every intermediate assistant bubble and the
    // user loses the navigation affordance.
    const _qJumpTarget=(!isUser&&!m._live)?questionRawIdxByAssistantRawIdx.get(rawIdx):undefined;
    const questionJumpBtn = (_qJumpTarget!==undefined&&_qJumpTarget!==null)
      ? _questionJumpButtonHtml(_qJumpTarget, assistantRawIdxByQuestionRawIdx.get(_qJumpTarget)??rawIdx)
      : '';
    const footHtml = `<div class="msg-foot">${timeHtml}<span class="msg-actions">${editBtn}${ttsBtn}${forkBtn}${copyBtn}${retryBtn}</span>${questionJumpBtn}</div>`;

    if(_isContextCompactionMessage(m)){
      continue;
    }

    if(isProcessWakeup){
      currentAssistantTurn=null;
      let row=_msgNodeRecycleEnabled?_recycleStash.get(rawIdx):null;
      if(row&&(!row.classList.contains('msg-row')||row.classList.contains('assistant-turn'))) row=null;
      const processText=String(rowDisplayContent||'').trim();
      const processFootHtml=`<div class="msg-foot">${timeHtml}<span class="msg-actions">${copyBtn}</span></div>`;
      const processTextHtml=processText?`<pre class="process-wakeup-text">${esc(processText)}</pre>`:'';
      const nextRowHtml=`<div class="process-wakeup-notice"><div class="process-wakeup-label">${li('terminal',13)}<span>${esc(t('process_wakeup_label'))}</span></div>${filesHtml}<div class="msg-body process-wakeup-body">${processTextHtml}</div>${processFootHtml}</div>`;
      if(row){
        row.className='msg-row process-wakeup-row';
        row.id=_userMessageDomId(rawIdx);
        row.dataset.msgIdx=rawIdx;
        row.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
        row.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
        row.dataset.role='process_wakeup';
        delete row.dataset.editing;
        if(row.dataset.rawText!==processText||row.innerHTML!==nextRowHtml){
          row.dataset.rawText=processText;
          row.innerHTML=nextRowHtml;
        }
      }else{
        row=document.createElement('div');
        row.className='msg-row process-wakeup-row';
        row.id=_userMessageDomId(rawIdx);
        row.dataset.msgIdx=rawIdx;
        row.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
        row.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
        row.dataset.role='process_wakeup';
        row.dataset.rawText=processText;
        row.innerHTML=nextRowHtml;
      }
      inner.appendChild(row);
      userRows.set(rawIdx, row);
      continue;
    }

    if(isUser){
      currentAssistantTurn=null;
      let row=_msgNodeRecycleEnabled?_recycleStash.get(rawIdx):null;
      if(row&&(!row.classList.contains('msg-row')||row.classList.contains('assistant-turn'))) row=null;
      const newRawText=String(displayContent).trim();
      const nextRowHtml=`${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`;
      if(row){
        row.className='msg-row';
        row.id=_userMessageDomId(rawIdx);
        row.dataset.msgIdx=rawIdx;
        row.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
        row.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
        row.dataset.role='user';
        delete row.dataset.editing;
        if(row.dataset.rawText!==newRawText||row.innerHTML!==nextRowHtml){
          row.dataset.rawText=newRawText;
          row.innerHTML=nextRowHtml;
        }
      }else{
        row=document.createElement('div');
        row.className='msg-row';
        row.id=_userMessageDomId(rawIdx);
        row.dataset.msgIdx=rawIdx;
        row.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
        row.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
        row.dataset.role='user';
        row.dataset.rawText=newRawText;
        row.innerHTML=nextRowHtml;
      }
      // Reserve this user row's real off-screen height up front so a wipe-and-rebuild
      // does not collapse scrollHeight to the flat 96px estimate (the collapse that
      // clamps/re-anchors the viewport on mobile — #5637/#5638, both jump classes). Uses
      // the remembered measured height when this row has been measured before, else a
      // content-length estimate; the measure pass refines it exactly next frame. The
      // typeof guard keeps renderMessages runnable in the node test harnesses that
      // extract it without this helper (they stub every collaborator by name).
      if(typeof _applyUserRowIntrinsicHeight==='function') _applyUserRowIntrinsicHeight(row, newRawText);
      inner.appendChild(row);
      userRows.set(rawIdx, row);
      continue;
    }

    if(!currentAssistantTurn){
      let recycled=_msgNodeRecycleEnabled?_recycleStash.get(rawIdx):null;
      if(recycled&&!recycled.classList.contains('assistant-turn')) recycled=null;
      if(recycled){
        const blocks=_assistantTurnBlocks(recycled);
        if(blocks) blocks.innerHTML='';
        for(const attr of _recycleResetAttrs) recycled.removeAttribute(attr);
        const role=recycled.querySelector('.msg-role.assistant');
        if(role) role.outerHTML=_assistantRoleHtml(tsTitle, isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):'');
        currentAssistantTurn=recycled;
      }else{
        currentAssistantTurn=_createAssistantTurn(tsTitle, isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):'');
      }
      currentAssistantTurn.dataset.role='assistant';
      if(S.session) currentAssistantTurn.dataset.sessionId=S.session.session_id;
      currentAssistantTurn.dataset.recycleKey=rawIdx;
      inner.appendChild(currentAssistantTurn);
    }
    _setLatestAssistantTurnLandmark(currentAssistantTurn, !m._live&&rawIdx===latestRenderedAssistantRawIdx);
    const seg=document.createElement('div');
    if(Array.isArray(orderedTransparentParts)&&orderedTransparentParts.length){
      const blocks=_assistantTurnBlocks(currentAssistantTurn);
      const sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
      const messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
      const lastTextPartIdx=(()=>{
        for(let i=orderedTransparentParts.length-1;i>=0;i--){
          if(
            orderedTransparentParts[i]&&
            orderedTransparentParts[i].kind==='text'&&
            String(_transparentOrderedDisplayText(orderedTransparentParts[i].text)).trim()
          ) return i;
        }
        return -1;
      })();
      let firstSeg=null;
      if(thinkingText&&window._showThinking!==false){
        if((isCompactWorklogMode()||isTransparentStream())&&_assistantThinkingBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs)) assistantThinking.set(rawIdx, thinkingText);
      }
      orderedTransparentParts.forEach((part, partIdx)=>{
        if(!part) return;
        if(part.kind==='tool'){
        const toolCall=_transparentOrderedToolCall(part, rawIdx, transparentOrderedToolCallsByTid, transparentToolResultsByTid, transparentPersistedSnippetByTid);
          const toolRow=_decorateTransparentEventRow(buildToolCard(toolCall),{
            type:'tool',
            name:toolCall&&toolCall.name,
            status:_transparentToolStatus(toolCall,true),
            toolCall,
            segmentSeq:toolCall&&toolCall.activitySegmentSeq,
            burstId:(toolCall&&toolCall.activityBurstId)||m._activityBurstId,
          });
          blocks.appendChild(toolRow);
          if(part.toolUseId) transparentOrderedToolIds.add(part.toolUseId);
          return;
        }
        const orderedSeg=document.createElement('div');
        const partDisplayText=_transparentOrderedDisplayText(part.text);
        if(!String(partDisplayText).trim()) return;
        orderedSeg.className='assistant-segment';
        orderedSeg.dataset.msgIdx=rawIdx;
        orderedSeg.dataset.sessionMsgIdx=sessionMsgIdx;
        orderedSeg.dataset.messageAnchorKey=messageAnchorKey;
        orderedSeg.dataset.rawText=String(partDisplayText||'').trim();
        if(m._activityBurstId!==undefined&&m._activityBurstId!==null) orderedSeg.setAttribute('data-activity-burst-id',String(m._activityBurstId));
        if(Number.isFinite(Number(m._liveSegmentSeq))) orderedSeg.setAttribute('data-live-segment-seq',String(Number(m._liveSegmentSeq)));
        if(_ERR_MSG_RE.test(String(partDisplayText||'').trim())) orderedSeg.dataset.error='1';
        if(!firstSeg&&thinkingText&&window._showThinking!==false&&!((isCompactWorklogMode()||isTransparentStream())&&_assistantThinkingBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs))) orderedSeg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText));
        const isLastTextPart=partIdx===lastTextPartIdx;
        const partBodyHtml=_getCachedRender(partDisplayText,false);
        if(isLastTextPart&&statusHtml){
          orderedSeg.insertAdjacentHTML('beforeend', statusHtml);
        }
        orderedSeg.insertAdjacentHTML('beforeend', `${isLastTextPart?filesHtml:''}<div class="msg-body">${partBodyHtml}</div>${isLastTextPart?footHtml:''}`);
        blocks.appendChild(orderedSeg);
        if(!firstSeg) firstSeg=orderedSeg;
      });
      assistantSegments.set(rawIdx, firstSeg||null);
      continue;
    }
    seg.className='assistant-segment';
    seg.dataset.msgIdx=rawIdx;
    seg.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);
    seg.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);
    seg.dataset.rawText=String(content).trim();
    if(m._activityBurstId!==undefined&&m._activityBurstId!==null) seg.setAttribute('data-activity-burst-id',String(m._activityBurstId));
    if(Number.isFinite(Number(m._liveSegmentSeq))) seg.setAttribute('data-live-segment-seq',String(Number(m._liveSegmentSeq)));
    const messageBelongsInWorklog=!S.busy&&isCompactWorklogMode()&&_assistantMessageBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs, displayContent, {isTurnFinalAssistant});
    if(messageBelongsInWorklog){
      seg.classList.add('assistant-segment-worklog-source');
      seg.setAttribute('aria-hidden','true');
      seg.hidden=true;
    }
    if(m._live){
      currentAssistantTurn.id='liveAssistantTurn';
      // Stamp the session id on the live turn so finalizeThinkingCard()
      // and other late callbacks can verify they're operating on the
      // right session's DOM (the user may have switched tabs/sessions
      // while this stream is still streaming). See #1366.
      if(S.session) currentAssistantTurn.dataset.sessionId=S.session.session_id;
      seg.setAttribute('data-live-assistant','1');
    }
    if(_ERR_MSG_RE.test(String(content||'').trim())) seg.dataset.error='1';
    // A turn whose visible content is empty but which carries a separate
    // `reasoning` field (e.g. a run-journal-recovered anchor: empty content +
    // reasoning + `_recovered_from_run_journal`) extracts NO inline thinkingText
    // and would render no Thinking Card at all — collapsing to an empty hidden
    // anchor. A session made entirely of such rows then paints blank (only date
    // separators) — the #3875 reporter's exact case (Compact tool activity OFF,
    // i.e. legacy mode). Surface the message's reasoning payload as the Thinking
    // Card source for these empty-content turns so the turn is never blank.
    //
    // LEGACY-MODE ONLY (!isSimplifiedToolCalling()): the simplified/Worklog path
    // already derives reasoning above (line ~8149 via
    // _worklogReasoningTextFromMessage, which strips an exact visible-answer echo
    // so reasoning duplicating a sibling answer is not re-shown). Repopulating the
    // raw reasoning here would bypass that echo-strip and re-render the duplicate
    // as a Worklog Thinking card (Codex gate catch). In legacy mode there is no
    // Worklog folding, so the raw payload is the correct Thinking-card source.
    // Stays OUT of the inline-content `thinkingText` extraction block (#2565) and
    // only fires for empty-content/no-inline-thinking turns, so answer-bearing
    // messages are unchanged.
    if(!isUser&&!m._live&&!isSimplifiedToolCalling()&&!thinkingText&&!String(content||'').trim()&&!filesHtml&&!statusHtml){
      const _reasoningPayload=_assistantReasoningPayloadText(m);
      if(_reasoningPayload) thinkingText=_reasoningPayload;
    }
    if(thinkingText&&window._showThinking!==false){
      if((isCompactWorklogMode()||isTransparentStream())&&_assistantThinkingBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs)) assistantThinking.set(rawIdx, thinkingText);
      else if(window._showThinking!==false) seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText));
    }
    const hasVisibleBody=!!(String(content||'').trim()||filesHtml||statusHtml||recoveryHtml);
    if(statusHtml){
      seg.insertAdjacentHTML('beforeend', statusHtml);
    }else if(hasVisibleBody){
      seg.insertAdjacentHTML('beforeend', `${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`);
    }else if(!(thinkingText&&window._showThinking!==false&&!isSimplifiedToolCalling())){
      seg.classList.add('assistant-segment-anchor');
    }
    _assistantTurnBlocks(currentAssistantTurn).appendChild(seg);
    assistantSegments.set(rawIdx, seg);
  }

  function _insertCompressionLikeNode(node, anchorIndex){
    if(!node) return;
    const anchorIdx=anchorIndex===undefined?insertionAnchor:anchorIndex;
    if(anchorIdx!==null && renderVisWithIdx[anchorIdx]){
      const anchorRawIdx=renderVisWithIdx[anchorIdx].rawIdx;
      const anchorSeg=assistantSegments.get(anchorRawIdx);
      if(anchorSeg){
        const turn=anchorSeg.closest('.assistant-turn');
        const blocks=_assistantTurnBlocks(turn);
        if(blocks){
          blocks.appendChild(node);
          return;
        }
      }
      const userRow=userRows.get(anchorRawIdx);
      if(userRow && userRow.parentElement){
        userRow.parentElement.insertBefore(node, userRow.nextSibling);
        return;
      }
    }
    inner.appendChild(node);
  }
  function _insertCompressionLikeNodeByRawIdx(node, rawIdx){
    if(!node) return;
    if(rawIdx<firstRenderedRawIdx) return;
    if(!renderVisWithIdx.length){
      inner.appendChild(node);
      return;
    }
    let anchorIdx=null;
    for(let i=0;i<renderVisWithIdx.length;i++){
      if(renderVisWithIdx[i].rawIdx > rawIdx){
        anchorIdx=i;
        break;
      }
    }
    if(anchorIdx===null){
      inner.appendChild(node);
      return;
    }
    const anchorRawIdx=renderVisWithIdx[anchorIdx].rawIdx;
    const anchorSeg=assistantSegments.get(anchorRawIdx);
    if(anchorSeg){
      const turn=anchorSeg.closest('.assistant-turn');
      const blocks=_assistantTurnBlocks(turn);
      if(blocks){
        blocks.insertBefore(node, anchorSeg);
        return;
      }
      const turnParent=turn && turn.parentElement;
      if(turnParent){
        turnParent.insertBefore(node, turn);
        return;
      }
    }
    const userRow=userRows.get(anchorRawIdx);
    if(userRow && userRow.parentElement){
      userRow.parentElement.insertBefore(node, userRow);
      return;
    }
    inner.appendChild(node);
  }
  const preservedOnlyNode=(!preservedCompressionTaskCardsAttached&&(!referenceNode||compressionState)&&preservedCompressionTaskMessages.length)
    ? (()=>{const row=document.createElement('div');row.innerHTML=`<div class="compression-turn"><div class="compression-turn-blocks">${_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)}</div></div>`;return row.firstElementChild;})()
    : null;
  const preservedOnlyAnchor=preservedCompressionRawIdxs.length
    ? (()=>{let idx=null;for(let i=0;i<renderVisWithIdx.length;i++){if(renderVisWithIdx[i].rawIdx<preservedCompressionRawIdxs[0]) idx=i;}return idx;})()
    : null;
  const handoffSummaryStates=_collectHandoffSummaryStates(S.messages);

  _insertCompressionLikeNode(compressionNode);
  if(referenceNode&&referenceMessageRawIdx>=0) _insertCompressionLikeNodeByRawIdx(referenceNode, referenceMessageRawIdx);
  else _insertCompressionLikeNode(referenceNode);
  _insertCompressionLikeNode(preservedOnlyNode, preservedOnlyAnchor);
  _insertCompressionLikeNode(handoffState?_handoffCardsNode(handoffState):null, renderVisWithIdx.length?renderVisWithIdx.length-1:null);
  for(const entry of handoffSummaryStates){
    if(!entry||!entry.state) continue;
    if(entry.rawIdx<firstRenderedRawIdx) continue;
    _insertCompressionLikeNodeByRawIdx(_handoffCardsNode(entry.state), entry.rawIdx);
  }
  renderCompressionUi();
  if(typeof rebuildSettledActivity==='function'){
    rebuildSettledActivity({
      inner,
      virtualWindow,
      renderableRawIdxs,
      renderedRawIdxs,
      assistantSegments,
      assistantThinking,
      transparentOrderedToolIds,
      worklogDetailDisclosureState,
    });
  }
  if(typeof finalizeSettledTurns==='function'){
    finalizeSettledTurns({
      inner,
      sid,
      assistantSegments,
      assistantThinking,
      toolCallAssistantIdxs,
    });
  }
  if(typeof restoreLiveAssistantTurn==='function'){
    restoreLiveAssistantTurn(preservedLiveTurn, inner);
  }
  // Only force-scroll when not actively streaming — mid-stream re-renders
  // (tool completion, session switch) must not override the user's scroll position.
  // scrollIfPinned() respects _scrollPinned, so it's a no-op if user scrolled up.
  if(typeof _syncLiveRunStatusAfterRender==='function') _syncLiveRunStatusAfterRender();
  _scrollAfterMessageRender(preserveScroll, scrollSnapshot);
  if(_maybeRecoverVirtualizedBlankViewport(options, preserveScroll, virtualWindow)) return;
  // Apply syntax highlighting after DOM is built
  requestAnimationFrame(()=>_postProcessWithAnchorSuppression(inner));
  // Refresh todo panel if it's currently open
  if(typeof loadTodos==='function' && document.getElementById('panelTodos') && document.getElementById('panelTodos').classList.contains('active')){
    loadTodos();
  }
  // Apply persisted playback speed after media nodes are rendered.
  if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(inner);
  // Populate session cache so switching back here skips a full rebuild.
  messageRenderCacheBindings._sessionHtmlCacheSid=sid;
  // Skip caching while the just-settled keep-open token is armed: that render
  // force-opens the settled worklog for height-stability, and caching it would
  // persist the forced-open DOM across session switches / restores, overriding a
  // user-collapsed worklog. The follow-up collapse pass (after disarm) produces
  // the correct cacheable DOM on its own render. (#5260 gate-cert.) The typeof
  // guard keeps standalone renderMessages() test harnesses (which don't define
  // the helper) working — absent helper == not armed == cache normally.
  const _keepOpenArmed=(typeof _isKeepSettledWorklogOpenArmed==='function')&&_isKeepSettledWorklogOpenArmed();
  if(sid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi&&!_keepOpenArmed){
    const _html=inner.innerHTML;
    const renderSignature=cachedRenderSignature===null?_messageRenderCacheSignature():cachedRenderSignature;
    _sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature});
  }
  _updateMessageVirtualMeasurements(renderVisWithIdx, renderVisibleIdxs, virtualWindow);
  // Kill the pinned/tail-follower mid-stream jitter. Schedule the re-anchor in a MICROTASK,
  // not synchronously: inside this render sync stack the browser still reports a transient
  // scrollHeight (layout is batched), so a synchronous re-anchor would read the SAME short
  // value scrollToBottom already clamped against and be a no-op. The microtask runs after the
  // stack unwinds (scrollHeight has flushed to the settled value) but before the browser
  // paints this frame, so writing the settled max lands the tail exactly and the ~1-row high
  // intermediate never reaches the screen. Only re-anchors a pre-wipe tail-follower left short
  // of the settled max — an unpinned reader parked in history is never moved (orthogonal to
  // the unpinned jump-back class). See _reanchorPinnedTailAfterRender for the full rationale.
  // (typeof guards mirror the _deferClearProgrammaticScroll call below so standalone
  // renderMessages() test harnesses that don't define these helpers still run.)
  if(typeof queueMicrotask==='function' && typeof _reanchorPinnedTailAfterRender==='function'){
    queueMicrotask(()=>_reanchorPinnedTailAfterRender(_preWipeNearTail));
  }
  _recycleStash.clear();
  if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll(160);
}



export {
  renderMessages,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderMessages: { enumerable: true, get: () => renderMessages, set: (value) => { renderMessages = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
