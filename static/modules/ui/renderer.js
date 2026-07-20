import { _formatGatewayModelLabel, _gatewayModelWarningText, _gatewayRoutingFailoverText, _normalizeThinkingEchoCompare } from './activity-and-scroll.js';
import { _isKeepSettledWorklogOpenArmed, _renderSettledAnchorSceneForMessage, ensureActivityGroup } from './anchor-scenes.js';
import { _clearCompressionElapsedTimer, _deferClearProgrammaticScroll, _fmtTokens, _formatFirstToken, _formatTurnDuration, _lastMessageRenderAt, _messageUserUnpinned, _programmaticScroll, _programmaticScrollSetAt } from './composer-controls.js';
import { _stripAttachedFilesMarkerForDisplay } from './composer.js';
import { _postProcessWithAnchorSuppression } from './content-postprocessing.js';
import { _liveAssistantSegmentTextLength } from './dialogs-and-reconnect.js';
import { _assistantToolAnchorIdxForMessage, _captureMessageScrollSnapshot, _cliPatchSnippetFromArgs, _cliToolCardHasDiffSnippet, _cliToolCardSnippet, _cliToolResultSnippet, _collectHandoffSummaryStates, _compressionAnchorIndex, _compressionCardsNode, _compressionReferenceCardHtml, _compressionStateForCurrentSession, _formatMessageFooterTimestamp, _handoffCardsNode, _handoffStateForCurrentSession, _isContextCompactionMessage, _isMarkerOnlyAssistantCompressionMessage, _isPreservedCompressionTaskListMessage, _latestCompressionReferenceMessage, _latestPreservedCompressionTaskListMessages, _messageRenderCacheSignature, _preservedCompressionTaskListCardsHtml, _sessionHtmlCache, _sessionHtmlCacheSid, _shouldShowSettledCompressionReference, _syncLiveRunStatusAfterRender, _toolArgsSnapshot, clearCompressionUi, renderCompressionUi } from './live-activity.js';
import { _initMediaPlaybackObserver, _renderAttachmentHtml } from './media-and-quota.js';
import { _applySessionNavigationPrefs, _applyUserRowIntrinsicHeight, _getCachedRender, _questionJumpButtonHtml, _rememberRenderedUserRowIntrinsicHeights, _updateMessageVirtualMeasurements, _userMessageDomId, _wireMessageWindowLoadEarlierButton } from './navigation.js';
import { _ERR_MSG_RE, _assistantMessageBelongsInWorklog, _assistantReasoningPayloadText, _assistantRoleHtml, _assistantThinkingBelongsInWorklog, _assistantTurnBlocks, _assistantTurnFinalVisibleContentMap, _assistantTurnVisibleContentMap, _captureWorklogDetailDisclosureState, _createAssistantTurn, _decorateTransparentEventRow, _fmtDateSep, _formatTurnTps, _isAssistantEmptyPlaceholderContent, _rehydrateTransparentStreamDom, _restoreWorklogDetailDisclosureState, _setLatestAssistantTurnLandmark, _syncTransparentEventControls, _thinkingActivityNode, _thinkingCardHtml, _transparentToolStatus, _worklogReasoningTextFromMessage, isCompactWorklogMode, isSimplifiedToolCalling, isTpsDisplayEnabled, isTransparentStream, msgContent } from './presentation.js';
import { _assistantTurnAnchorSettledFinalAnswer, _collectToolResultSnippetsByTid, _legacySettledFallbackHasToolMetadata, _maybeRecoverVirtualizedBlankViewport, _reanchorPinnedTailAfterRender, _scrollAfterMessageRender, _transparentOrderedDisplayText, _transparentOrderedToolCall, _transparentStreamOrderedParts } from './render-support.js';
import { $, INFLIGHT, S, _activeCompressionRecoveryPayload, _compressionRecoveryHtml, _currentMessageVirtualWindow, _getVisibleMessagesWithIdx, _messageRenderWindowSid, _messageSessionIndexForRawIdx, _messageViewportAnchorKeyForMessage, _messageVirtualKeepTailCount, _messageVirtualSpacer, _messageVirtualWindowKey, _messageVirtualWindowKeyFor, _msgNodeRecycleEnabled, _recycleResetAttrs, _recycleStash, _resetMessageRenderWindow, _setCompressionSessionLock, _statusCardHtml, _stripWorkspaceDisplayPrefix, esc } from './state.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl, buildToolCard } from './tool-worklog.js';
import { _appendWorklogStep, _applyTransparentRowFading, _materializeDeferredWorklogRows, _rehydrateDeferredWorklogsFromCache, _renderTransparentTurnFooter, _transparentTurnCollapsedStates, _wireTransparentTurnToggle, _worklogReasonHtmlFromAnchor } from './transparent-worklog.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';
import { compatibilityBindings as liveActivityBindings } from './live-activity.js';
import { compatibilityBindings as stateBindings } from './state.js';

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
      liveActivityBindings._sessionHtmlCacheSid=sid;
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
  // Mid-stream flicker fix (#3877): when a renderMessages() rebuild is reached
  // while THIS session is actively streaming (e.g. the clarify-response echo at
  // messages.js, or a CLI-import refresh), the `inner.innerHTML=''` below detaches
  // the live `#liveAssistantTurn` node — and the smd parser keeps writing into
  // that now-orphaned node, so the streamed text vanishes until the next stream
  // event rebuilds the turn ("disappears, then reappears"). Capture the live
  // turn's actual DOM node (not its HTML — the parser holds a live reference into
  // it) so it can be re-attached after the rebuild, keeping the parser target
  // connected and the streamed text visible. Only for the streaming session's own
  // live turn; never affects settled transcripts.
  let _preservedLiveTurn=null;
  if(sid&&INFLIGHT[sid]){
    const _lt=document.getElementById('liveAssistantTurn');
    if(_lt&&(!_lt.dataset||!_lt.dataset.sessionId||_lt.dataset.sessionId===sid)){
      // Blank-turn fix (对话消失): only preserve the live turn across the DOM
      // wipe if it is GENUINELY live — either an active stream is still running
      // (S.activeStreamId set: the #3877 mid-stream flicker case this preserve
      // was written for), or the turn already holds real rendered content (a
      // visible answer body, a tool card, or a reasoning row). A DEAD shell —
      // an interrupted turn whose stream dropped (S.activeStreamId cleared to
      // null) but whose INFLIGHT[sid] entry was not cleaned, leaving only an
      // empty worklog group ("Processed Ns" with no body/tool rows) — must NOT
      // be preserved: re-attaching it on a session-updated swap re-render pins
      // an avatar-only empty turn OVER the settled transcript, hiding the real
      // (already-persisted) answer. That is the reported blank. Reproduced +
      // fix verified on an isolated debug instance (8710): stale INFLIGHT +
      // empty live-turn survived the swap → blank; gating on real-content /
      // active-stream clears it while a genuine live turn still renders.
      const _hasRealLiveContent=!!_lt.querySelector('.msg-body, .tool-card-row, .wl-reason');
      if(_hasRealLiveContent || S.activeStreamId){
        _preservedLiveTurn=_lt;
      }
    }
  }
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
  const anchorOwnedAssistantRawIdxs=new Set();
  for(const [rawIdx,seg] of assistantSegments){
    const msg=S.messages[rawIdx];
    if(!msg||!msg._anchor_activity_scene||!seg) continue;
    const turn=seg.closest('.assistant-turn');
    if(!turn) continue;
    turn.querySelectorAll('.assistant-segment[data-msg-idx]').forEach(node=>{
      const idx=Number(node.getAttribute('data-msg-idx'));
      if(Number.isFinite(idx)) anchorOwnedAssistantRawIdxs.add(idx);
    });
  }
  // Insert settled tool call cards (history view only).
  // During live streaming, tool cards are rendered in #liveToolCards by the
  // tool SSE handler and never mixed into the message list until done fires.
  //
  // Fallback: if S.toolCalls is empty (sessions that predate session-level tool
  // tracking, or runs that didn't go through the normal streaming path), build
  // a display list from per-message tool_calls (OpenAI format) stored in each
  // assistant message. This covers the reload case described in issue #140.
  const hasMessageToolMetadata=!S.busy&&Array.isArray(S.messages)&&S.messages.some((m,rawIdx)=>
    !anchorOwnedAssistantRawIdxs.has(rawIdx)&&_legacySettledFallbackHasToolMetadata(m)
  );
  if(!S.busy && (hasMessageToolMetadata||!S.toolCalls||!S.toolCalls.length)){
    // Index tool outputs by tool_call_id / tool_use_id so the
    // fallback-built cards carry their result snippet (not just the command).
    // Without this step CLI-origin sessions reload with empty tool cards.
    const resultsByTid={};
    const fallbackToolSources=[];
    // Durable fallback: the persisted compact summary (session.tool_calls, built
    // by _extract_tool_calls_from_messages) carries a bounded result `snippet`
    // keyed by tid. On a cold/paginated load where the role:tool result-message
    // join below misses (id mismatch, recovery-rebuilt turn), use this so the
    // terminal output / diff body still renders instead of vanishing (#4927).
    const persistedSnippetByTid={};
    try{
      const persisted=(S.session&&Array.isArray(S.session.tool_calls))?S.session.tool_calls:[];
      persisted.forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const ptid=tc.tid||tc.id||tc.tool_call_id||tc.call_id||'';
        const psnip=tc.snippet||tc.result||tc.output||tc.preview||'';
        if(ptid&&psnip&&!persistedSnippetByTid[ptid]) persistedSnippetByTid[ptid]=String(psnip);
      });
    }catch(e){}
    S.messages.forEach((m,rawIdx)=>{
      if(!m) return;
      // OpenAI / Hermes CLI format: role=tool with tool_call_id
      if(m.role==='tool'){
        const tid=m.tool_call_id||m.tool_use_id||'';
        if(tid) resultsByTid[tid]=_cliToolResultSnippet(m.content);
        return;
      }
      // Anthropic format: tool_result blocks inside a user message content array
      if(Array.isArray(m.content)){
        m.content.forEach(p=>{
          if(!p||typeof p!=='object'||p.type!=='tool_result') return;
          const tid=p.tool_use_id||'';
          if(!tid) return;
          const raw=typeof p.content==='string'?p.content
                   :Array.isArray(p.content)?p.content.map(c=>c&&c.text?c.text:'').join('')
                   :'';
          resultsByTid[tid]=_cliToolResultSnippet(raw);
        });
      }
      if(m.role==='assistant'){
        if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;
        if(_legacySettledFallbackHasToolMetadata(m)) fallbackToolSources.push({m,rawIdx});
      }
    });
    const derived=[];
    const liveToolMetadata=Array.isArray(S._settledLiveToolMetadata)
      ? S._settledLiveToolMetadata
      : (Array.isArray(S.toolCalls)?S.toolCalls:[]);
    const liveMetadataByTid=new Map();
    liveToolMetadata.forEach((tc,idx)=>{
      if(!tc||typeof tc!=='object') return;
      const tid=tc.tid||tc.id||tc.tool_call_id||tc.call_id||'';
      if(tid&&!liveMetadataByTid.has(tid)) liveMetadataByTid.set(tid,{tc,idx});
    });
    const usedLiveToolMetadata=new Set();
    const copyLiveToolMetadata=(next,name,tid)=>{
      let matchEntry=tid?liveMetadataByTid.get(tid):null;
      if(!matchEntry){
        const matchIdx=liveToolMetadata.findIndex((tc,i)=>tc&&!usedLiveToolMetadata.has(i)&&(!name||tc.name===name));
        if(matchIdx>=0) matchEntry={tc:liveToolMetadata[matchIdx],idx:matchIdx};
      }
      if(matchEntry){
        usedLiveToolMetadata.add(matchEntry.idx);
        const live=matchEntry.tc||{};
        for(const key of ['activityBurstId','duration','started_at']){
          if((next[key]===undefined||next[key]===null)&&live[key]!==undefined&&live[key]!==null) next[key]=live[key];
        }
      }
      return next;
    };
    fallbackToolSources.forEach(({m,rawIdx})=>{
      const assistantToolAnchorIdx=_assistantToolAnchorIdxForMessage(S.messages,rawIdx);
      // OpenAI format: top-level tool_calls field on the assistant message
      (m.tool_calls||[]).forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const fn=tc.function||{};
        const name=fn.name||tc.name||'tool';
        let args={};
        try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){}
        const tid=tc.id||tc.call_id||'';
        const patchSnippet=_cliPatchSnippetFromArgs(name,args);
        const resultSnippet=resultsByTid[tid]||persistedSnippetByTid[tid]||'';
        let argsSnap=_toolArgsSnapshot(args);
        derived.push(copyLiveToolMetadata({
          name,
          snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
          is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
          tid,
          assistant_msg_idx:assistantToolAnchorIdx,
          args:argsSnap,
          done:true,
        }, name, tid));
      });
      // WebUI partial/live format: _partial_tool_calls snapshots survive
      // interrupted or adapter-shaped settles even when session.tool_calls is empty.
      const partialToolCalls=Array.isArray(m._partial_tool_calls)?m._partial_tool_calls:[];
      partialToolCalls.forEach(tc=>{
        if(!tc||typeof tc!=='object') return;
        const fn=tc.function||{};
        const name=tc.name||fn.name||'tool';
        let args=tc.args||tc.input||{};
        if(!args||typeof args!=='object'){
          try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){ args={}; }
        }else if(!Object.keys(args).length&&fn.arguments){
          try{ args=JSON.parse(fn.arguments||'{}'); }catch(e){}
        }
        const tid=tc.tid||tc.id||tc.tool_call_id||tc.call_id||'';
        const patchSnippet=_cliPatchSnippetFromArgs(name,args);
        const resultSnippet=resultsByTid[tid]||tc.snippet||tc.preview||persistedSnippetByTid[tid]||'';
        const argsSnap=_toolArgsSnapshot(args);
        derived.push(copyLiveToolMetadata({
          name,
          snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
          is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
          tid,
          assistant_msg_idx:assistantToolAnchorIdx,
          args:argsSnap,
          done:true,
        }, name, tid));
      });
      // Anthropic format: tool_use blocks inside assistant content array
      if(Array.isArray(m.content)){
        m.content.forEach(p=>{
          if(!p||typeof p!=='object'||p.type!=='tool_use') return;
          const name=p.name||'tool';
          const args=p.input||{};
          const tid=p.id||'';
          const patchSnippet=_cliPatchSnippetFromArgs(name,args);
          const resultSnippet=resultsByTid[tid]||persistedSnippetByTid[tid]||'';
          const argsSnap=_toolArgsSnapshot(args);
          derived.push(copyLiveToolMetadata({
            name,
            snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
            is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
            tid,
            assistant_msg_idx:assistantToolAnchorIdx,
            args:argsSnap,
            done:true,
          }, name, tid));
        });
      }
      // WebUI-internal partial tool calls captured on cancel/stop
      // (private shape: name/args/done/preview/snippet, no OpenAI envelope).
      if(Array.isArray(m._partial_tool_calls)){
        m._partial_tool_calls.forEach(tc=>{
          if(!tc||typeof tc!=='object') return;
          const name=tc.name||'tool';
          const args=tc.args||{};
          const tid=tc.id||tc.call_id||tc.tool_call_id||tc.tid||'';
          const patchSnippet=_cliPatchSnippetFromArgs(name,args);
          const resultSnippet=_cliToolResultSnippet(tc.snippet||tc.result||tc.output||tc.preview||'');
          const argsSnap=_toolArgsSnapshot(args,4);
          derived.push(copyLiveToolMetadata({
            name,
            snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
            is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
            tid,
            assistant_msg_idx:assistantToolAnchorIdx,
            args:argsSnap,
            done:true,
          }, name, tid));
        });
      }
    });
    if(derived.length) S.toolCalls=derived;
    if(S._settledLiveToolMetadata) S._settledLiveToolMetadata=null;
  }
  if(!S.busy || (S.toolCalls&&S.toolCalls.length)){
    // Rebuild settled tool/worklog/thinking nodes. The `|| (S.toolCalls.length)`
    // arm is REQUIRED, not just `!S.busy`: when renderMessages re-runs during an
    // active stream (e.g. switching back to an in-progress session, busy=true),
    // the earlier innerHTML wipe removed every settled turn's worklog above the
    // live turn. Gating purely on `!S.busy` skipped this rebuild while busy and
    // left those prior turns' tool cards gone until the stream finished (#3401
    // regression vs master; same content-loss-on-switch class as #3668). The
    // `:not([data-live-thinking="1"])` / live-card guards below keep the active
    // turn's own live nodes from being double-built.
    inner.querySelectorAll('.tool-worklog-group:not([data-compression-card]),.tool-call-group:not([data-compression-card]),.tool-card-row:not([data-compression-card]):not([data-event-type="tool"]),.agent-activity-thinking:not([data-live-thinking="1"]):not([data-event-type="thinking"]),.wl-reason[data-worklog-anchor-reason="1"],.wl-reason[data-worklog-reason-source="reasoning"]').forEach(el=>el.remove());
    const byActivity = new Map();
    const assistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    const _assistantAnchorForActivity=(aIdx,segmentSeq,burstId)=>{
      if(segmentSeq){
        for(const seg of assistantSegments.values()){
          if(seg&&seg.getAttribute('data-live-segment-seq')===String(segmentSeq)) return seg;
        }
      }
      const wantedBurst=burstId!==undefined&&burstId!==null&&String(burstId)!==''&&String(burstId)!=='0'?String(burstId):'';
      if(wantedBurst){
        for(const seg of assistantSegments.values()){
          if(seg&&seg.getAttribute('data-activity-burst-id')===wantedBurst) return seg;
        }
      }
      let anchorRow=assistantSegments.get(aIdx)||null;
      if(!anchorRow&&assistantIdxs.length){
        if(aIdx<assistantIdxs[0]) return null;
        const fallbackIdx=[...assistantIdxs].reverse().find(idx=>idx<=aIdx);
        anchorRow=fallbackIdx!==undefined?assistantSegments.get(fallbackIdx):assistantSegments.get(assistantIdxs[assistantIdxs.length-1]);
      }
      return anchorRow;
    };
    const _turnDurationForAnchor=(anchorRow)=>{
      if(!anchorRow) return undefined;
      const turn=anchorRow.closest('.assistant-turn');
      const blocks=_assistantTurnBlocks(turn);
      if(!blocks) return undefined;
      let duration;
      for(const seg of blocks.querySelectorAll('.assistant-segment')){
        const idx=Number(seg.dataset&&seg.dataset.msgIdx);
        const msg=Number.isFinite(idx)?S.messages[idx]:null;
        if(msg&&msg._turnDuration!==undefined) duration=msg._turnDuration;
      }
      return duration;
    };
    const durationAssignedTurns = new Set();
    const activityByTurn = new Map();
    const activityOrder = [];
    const ensureActivityBucket=(key,aIdx,segmentSeq,burstId)=>{
      if(!byActivity.has(key)){
        const entry={key,aIdx,segmentSeq:segmentSeq||'',burstId:burstId||'',cards:[],thinkingIdx:null,includeAnchorReason:false};
        byActivity.set(key,entry);
        activityOrder.push(entry);
      }
      return byActivity.get(key);
    };
    const normalizeToken=(value)=>{
      const hasValue=value!==undefined&&value!==null&&String(value)!==''&&String(value)!=='0';
      return hasValue?String(value):'';
    };
    const knownBurstIds=new Set();
    for(const s of assistantSegments.values()) if(s){const b=s.getAttribute('data-activity-burst-id');if(b)knownBurstIds.add(b);}
    for(const tc of (S.toolCalls||[])){
      if(!tc) continue;
      const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
      if(tid&&transparentOrderedToolIds.has(tid)) continue;
      const aIdx=tc.assistant_msg_idx!==undefined?parseInt(tc.assistant_msg_idx):-1;
      if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;
      if(virtualWindow.virtualized&&renderableRawIdxs.has(aIdx)&&!renderedRawIdxs.has(aIdx)) continue;
      const segmentSeq=normalizeToken(tc.activitySegmentSeq);
      const burstId=normalizeToken(tc.activityBurstId);
      const burstResolvable=burstId&&knownBurstIds.has(burstId);
      const key=segmentSeq?`segment:${segmentSeq}`:(burstResolvable?`burst:${burstId}`:`assistant:${aIdx}`);
      const entry=ensureActivityBucket(key,aIdx,segmentSeq,burstId);
      entry.cards.push(tc);
      entry.includeAnchorReason=true;
    }
    for(const aIdx of assistantThinking.keys()){
      if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;
      if(virtualWindow.virtualized&&renderableRawIdxs.has(aIdx)&&!renderedRawIdxs.has(aIdx)) continue;
      const seg=assistantSegments.get(aIdx);
      const segmentSeq=seg&&seg.getAttribute('data-live-segment-seq')||'';
      const burstId=seg&&seg.getAttribute('data-activity-burst-id')||'';
      const key=segmentSeq?`segment:${segmentSeq}`:(burstId?`burst:${burstId}`:`assistant:${aIdx}`);
      const entry=ensureActivityBucket(key,aIdx,segmentSeq,burstId);
      if(entry.thinkingIdx===null) entry.thinkingIdx=aIdx;
    }
    for(const [aIdx,seg] of assistantSegments){
      if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;
      if(!seg||!seg.classList||!seg.classList.contains('assistant-segment-worklog-source')) continue;
      if(virtualWindow.virtualized&&renderableRawIdxs.has(aIdx)&&!renderedRawIdxs.has(aIdx)) continue;
      if(!_worklogReasonHtmlFromAnchor(seg)) continue;
      const segmentSeq=seg&&seg.getAttribute('data-live-segment-seq')||'';
      const burstId=seg&&seg.getAttribute('data-activity-burst-id')||'';
      const key=segmentSeq?`segment:${segmentSeq}`:(burstId?`burst:${burstId}`:`assistant:${aIdx}`);
      const entry=ensureActivityBucket(key,aIdx,segmentSeq,burstId);
      entry.includeAnchorReason=true;
    }
    activityOrder.sort((a,b)=>{
      const anchorA=_assistantAnchorForActivity(a.aIdx,a.segmentSeq,a.burstId);
      const anchorB=_assistantAnchorForActivity(b.aIdx,b.segmentSeq,b.burstId);
      const idxA=(anchorA&&anchorA.parentElement)?Array.prototype.indexOf.call(anchorA.parentElement.children,anchorA):Number.MAX_SAFE_INTEGER;
      const idxB=(anchorB&&anchorB.parentElement)?Array.prototype.indexOf.call(anchorB.parentElement.children,anchorB):Number.MAX_SAFE_INTEGER;
      if(idxA!==idxB) return idxA-idxB;
      const seqA=a.segmentSeq!==''?Number(a.segmentSeq):Number.MAX_SAFE_INTEGER;
      const seqB=b.segmentSeq!==''?Number(b.segmentSeq):Number.MAX_SAFE_INTEGER;
      if(Number.isFinite(seqA)&&Number.isFinite(seqB)&&seqA!==seqB) return seqA-seqB;
      const burstA=a.burstId!==''?Number(a.burstId):Number.MAX_SAFE_INTEGER;
      const burstB=b.burstId!==''?Number(b.burstId):Number.MAX_SAFE_INTEGER;
      if(Number.isFinite(burstA)&&Number.isFinite(burstB)&&burstA!==burstB) return burstA-burstB;
      return a.aIdx-b.aIdx;
    });
    if(!isTransparentStream()){
      for(const entry of activityOrder){
        const {aIdx,segmentSeq,burstId,cards,thinkingIdx,includeAnchorReason}=entry;
        if(aIdx<assistantIdxs[0]) continue;
        const anchorRow=_assistantAnchorForActivity(aIdx,segmentSeq,burstId);
        if(!anchorRow) continue;
        const anchorParent=anchorRow.parentElement;
        const anchorReasonHtml=_worklogReasonHtmlFromAnchor(anchorRow);
        const thinkingText=thinkingIdx!==null?assistantThinking.get(thinkingIdx):'';
        if(!cards.length&&!anchorReasonHtml&&!thinkingText) continue;
        const anchorTurn=anchorRow.closest('.assistant-turn');
        if(!anchorTurn) continue;
        let state=activityByTurn.get(anchorTurn);
        if(!state){
          const includeTurnDuration=!durationAssignedTurns.has(anchorTurn);
          if(includeTurnDuration) durationAssignedTurns.add(anchorTurn);
          const activityKey=`assistant:${aIdx}`;
          const anchorIsWorklogSource=anchorRow.classList&&anchorRow.classList.contains('assistant-segment-worklog-source');
          const group=ensureActivityGroup(anchorParent,{
            collapsed:true,
            anchor:anchorRow,
            beforeAnchor:!!thinkingText&&!anchorIsWorklogSource,
            syncAnchorReason:anchorIsWorklogSource,
            activityKey,
            burstId:burstId||'',
            segmentSeq:segmentSeq||'',
            turnDuration:includeTurnDuration?_turnDurationForAnchor(anchorRow):undefined,
          });
          const list=_toolWorklogListEl(group);
          if(!list) continue;
          list.innerHTML='';
          state={group,cards:[],seenReasons:new Set(),seenTools:new Set()};
          activityByTurn.set(anchorTurn,state);
        }
        state.cards.push(...cards);
        _appendWorklogStep(state.group, anchorRow, cards, thinkingText, {
          live:false,
          includeAnchorReason:!!includeAnchorReason&&!!anchorReasonHtml,
          thinkingKey:thinkingText?`thinking:${_normalizeThinkingEchoCompare(thinkingText)}`:'',
          thinkingDisclosureKey:thinkingText?`thinking:${entry.key}`:'',
          seenReasons:state.seenReasons,
          seenTools:state.seenTools,
        });
      }
      activityByTurn.forEach(state=>{
        _syncToolCallGroupSummary(state.group);
      });
    }else{
      // ── transparent_stream path: individual expandable event rows ──
      const transparentInsertCursors=new Map();
      // Per-turn dedup of echoed thinking text — mirrors the compact-worklog
      // path's `seenReasons` Set (the transparent branch previously had none,
      // so the same echoed reasoning rendered twice, once out of chronological
      // position). Keyed by the assistant turn element. (Trifecta finding O-Bug1.)
      const transparentSeenThinking=new Map();
      for(const entry of activityOrder){
        const {aIdx,segmentSeq,burstId,cards,thinkingIdx,includeAnchorReason}=entry;
        const sourceMsg=aIdx>=0?S.messages[aIdx]:null;
        const event={
          ...entry,
          ts:sourceMsg&&((sourceMsg._ts!==undefined&&sourceMsg._ts!==null)?sourceMsg._ts:sourceMsg.timestamp),
          thinkingText:thinkingIdx!==null?assistantThinking.get(thinkingIdx):'',
        };
        if(aIdx<assistantIdxs[0]) continue;
        const anchorRow=_assistantAnchorForActivity(aIdx,segmentSeq,burstId);
        if(!anchorRow) continue;
        const anchorTurn=anchorRow.closest('.assistant-turn');
        const turn=anchorTurn;
        const blocks=_assistantTurnBlocks(anchorTurn);
        if(!anchorTurn||!blocks) continue;
        const anchorIsWorklogSource=anchorRow.classList&&anchorRow.classList.contains('assistant-segment-worklog-source');
        const insertAfterCursor=(row)=>{
          const cursor=transparentInsertCursors.get(anchorRow)||anchorRow;
          const ref=cursor&&cursor.parentElement===blocks?cursor.nextElementSibling:null;
          if(ref&&ref.parentElement===blocks) blocks.insertBefore(row,ref);
          else blocks.appendChild(row);
          transparentInsertCursors.set(anchorRow,row);
        };
        const insertBeforeAnchor=(row)=>{
          if(anchorRow&&anchorRow.parentElement===blocks) blocks.insertBefore(row,anchorRow);
          else blocks.appendChild(row);
        };
        if(event.thinkingText){
          const _thinkKey=typeof _normalizeThinkingEchoCompare==='function'
            ? _normalizeThinkingEchoCompare(event.thinkingText)
            : String(event.thinkingText).trim();
          let _seen=transparentSeenThinking.get(anchorTurn);
          if(!_seen){_seen=new Set();transparentSeenThinking.set(anchorTurn,_seen);}
          if(_thinkKey&&_seen.has(_thinkKey)){
            // Echoed reasoning already rendered for this turn — skip the duplicate.
          }else{
            if(_thinkKey)_seen.add(_thinkKey);
            const thinkingRow=_decorateTransparentEventRow(_thinkingActivityNode(event.thinkingText,false),{
              type:'thinking',
              text:event.thinkingText,
              preview:event.thinkingText,
              ts:event.ts,
              segmentSeq,
              burstId,
            });
            if(!anchorIsWorklogSource) insertBeforeAnchor(thinkingRow);
            else insertAfterCursor(thinkingRow);
          }
        }
        for(const toolCall of cards){
          event.toolCall=toolCall;
          const toolRow=_decorateTransparentEventRow(buildToolCard(event.toolCall),{
            type:'tool',
            name:event.toolCall&&event.toolCall.name,
            status:_transparentToolStatus(event.toolCall,true),
            toolCall:event.toolCall,
            ts:event.ts,
            segmentSeq,
            burstId,
          });
          insertAfterCursor(toolRow);
        }
        _syncTransparentEventControls(turn);
      }
    }
  }
  for(const [rawIdx,seg] of assistantSegments){
    const msg=S.messages[rawIdx];
    if(msg&&msg._anchor_activity_scene){
      _renderSettledAnchorSceneForMessage(msg, seg, rawIdx);
    }
  }
  _restoreWorklogDetailDisclosureState(inner, worklogDetailDisclosureState);
  // #5839 fix: deferred settled worklogs have no rows yet at restore time, so
  // the disclosure restore above can't reach their detail elements. Stash the
  // captured state on each still-deferred group; _materializeDeferredWorklogRows
  // re-applies it (key-scoped + idempotent) once the rows exist on expand.
  if(worklogDetailDisclosureState&&worklogDetailDisclosureState.size){
    inner.querySelectorAll('[data-worklog-rows-deferred="1"]').forEach(group=>{
      group._deferredWorklogDisclosure=worklogDetailDisclosureState;
    });
  }
  // Render per-turn duration and optional token usage on assistant messages.
  // Duration stays visible even when token usage is disabled, because it answers
  // the basic "how long did that turn take?" UX question. Only walk rendered
  // assistant segments so hidden messages above the DOM window cannot skew the
  // footer-to-message mapping.
  {
    const renderedAssistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);
    for(const mi of renderedAssistantIdxs){
      const msg=S.messages[mi]||{};
      if(msg.role!=='assistant') continue;
      const routing=msg._gatewayRouting||null;
      const gatewayText=_formatGatewayModelLabel(S.session&&S.session.model||'', '', routing);
      const failoverText=_gatewayRoutingFailoverText(routing);
      const modelWarningText=_gatewayModelWarningText(routing);
      const hasTurnUsage=!!msg._turnUsage;
      // The Worklog summary owns the "Done in …" duration whenever this
      // assistant message contributes tool or thinking detail to a folded
      // Worklog above the final answer.
      const compactWorklogForMessage=isCompactWorklogMode()&&(toolCallAssistantIdxs.has(mi)||assistantThinking.has(mi));
      const durationText=compactWorklogForMessage?'':_formatTurnDuration(msg._turnDuration);
      if(!hasTurnUsage&&!durationText&&!gatewayText&&!failoverText&&!modelWarningText) continue;
      const seg=assistantSegments.get(mi);
      const row=seg?seg.closest('.assistant-turn'):null;
      const footerRows=row?row.querySelectorAll('.msg-foot'):[];
      const targetFoot=footerRows.length?footerRows[footerRows.length-1]:null;
      if(!targetFoot||targetFoot.querySelector('.msg-usage-inline,.msg-duration-inline,.msg-gateway-inline,.gateway-failover-inline,.msg-model-warning-inline')) continue;
      const fragments=[];
      if(modelWarningText){
        const warning=document.createElement('span');
        warning.className='msg-model-warning-inline';
        warning.textContent=modelWarningText;
        fragments.push(warning);
      }
      if(failoverText){
        const failover=document.createElement('span');
        failover.className='gateway-failover-inline';
        failover.textContent=failoverText;
        fragments.push(failover);
      }
      if(gatewayText){
        const gateway=document.createElement('span');
        gateway.className='msg-gateway-inline';
        gateway.textContent=gatewayText;
        fragments.push(gateway);
      }
      if(durationText){
        const duration=document.createElement('span');
        duration.className='msg-duration-inline';
        duration.textContent=`Done in ${durationText}`;
        fragments.push(duration);
      }
      if(window._showTokenUsage&&hasTurnUsage){
        const usage=document.createElement('span');
        usage.className='msg-usage-inline';
        const inTok=msg._turnUsage.input_tokens||0;
        const outTok=msg._turnUsage.output_tokens||0;
        const cost=msg._turnUsage.estimated_cost;
        let text=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
        if(cost) text+=` · ~$${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
        const cacheHitPct=msg._turnUsage.cache_hit_percent;
        if(cacheHitPct!=null) text+=` · ${t('usage_cached_percent',cacheHitPct)}`;
        usage.textContent=text;
        fragments.push(usage);
      }
      if(fragments.length){
        targetFoot.classList.add('msg-foot-with-usage');
        for(let i=fragments.length-1;i>=0;i--){
          // Guard: firstChild may be null (empty foot) or orphaned.
          const firstChild=targetFoot.firstChild;
          if(firstChild&&firstChild.parentNode===targetFoot) targetFoot.insertBefore(fragments[i], firstChild);
          else targetFoot.appendChild(fragments[i]);
        }
      }
    }
  }
  // Transparent mode per-turn wiring: collapsible Hermes chat name tag, old-event
  // fading, and the bottom-of-turn footer (elapsed · tokens · TTFT · status).
  // Runs after the per-turn duration block above so the footer can reuse the
  // computed durationText / tokens / TTFT for each settled assistant turn.
  if(isTransparentStream()){
    for(const turn of inner.querySelectorAll('.assistant-turn')){
      if(turn.id==='liveAssistantTurn') continue;
      const blocks=_assistantTurnBlocks(turn);
      if(!blocks) continue;
      const hasTransparentRows=blocks.querySelector(':scope > .transparent-event-row');
      _wireTransparentTurnToggle(turn);
      // Restore collapse state from the map (survives DOM rebuild).
      const seg=turn.querySelector('.assistant-segment');
      if(seg&&sid){
        const mi=seg.getAttribute('data-msg-idx');
        if(mi!=null&&_transparentTurnCollapsedStates[`${sid}:${mi}`]){
          turn.setAttribute('data-transparent-turn-collapsed','1');
          const role=turn.querySelector('.msg-role.assistant');
          if(role) role.setAttribute('aria-expanded','false');
        }
      }
      _applyTransparentRowFading(turn);
      if(hasTransparentRows){
        // Find the corresponding message to read duration/usage.
        const seg=turn.querySelector('.assistant-segment');
        let durationText='';
        let ttftText='';
        let tokensText='';
        if(seg){
          const mi=seg.getAttribute('data-msg-idx');
          if(mi!=null){
            const msg=S.messages[Number(mi)]||{};
            if(msg._turnDuration!=null) durationText=_formatTurnDuration(msg._turnDuration);
            if(msg._firstTokenMs!=null) ttftText=_formatFirstToken(msg._firstTokenMs);
            if(msg._turnUsage){
              const inTok=msg._turnUsage.input_tokens||0;
              const outTok=msg._turnUsage.output_tokens||0;
              tokensText=`${_fmtTokens(inTok)} in · ${_fmtTokens(outTok)} out`;
            }
          }
        }
        _renderTransparentTurnFooter(turn,{
          durationText,
          ttftText,
          tokensText,
          statusText: t('done')||'Done',
        });
      }else{
        // No transparent rows → no footer needed.
        _renderTransparentTurnFooter(turn,{});
      }
    }
  }
  // Fail-safe invariant (#3875): a settled assistant turn must never render with
  // ZERO visible content. The Worklog redesign (#3401) folds intermediate
  // assistant segments into a collapsed Worklog card and hides the source segment
  // (`assistant-segment-worklog-source` → display:none). That is correct WHEN the
  // turn also has a visible final answer. But when a turn's ONLY content is folded
  // into a collapsed Worklog (e.g. an autonomous/interrupted run whose final
  // assistant message is empty, or a reload where S.toolCalls didn't hydrate so the
  // worklog card built with no expandable tool steps), every segment is hidden and
  // the turn paints as nothing — leaving the transcript a bare stack of date
  // separators (#3875 brick). Reveal such turns so their content is never silently
  // swallowed: expand the turn's Worklog group(s) when the turn has no other
  // visible content. This NEVER touches a turn that has any visible segment, so the
  // intended collapsed-Worklog UX is preserved whenever a visible answer exists.
  // The live turn is excluded by its `liveAssistantTurn` id (it drives its own
  // state during a stream), so this sweep is safe to run even while busy — a
  // historical blank turn must not re-paint blank during a follow-up stream
  // (Opus advisor, stage-342).
  {
    const _turnHasVisibleContent=(turn)=>{
      const segs=turn.querySelectorAll('.assistant-segment');
      for(const seg of segs){
        // A segment shows real content only when it is NOT worklog-folded AND its
        // body/files/status actually painted (the anchor-only placeholder class
        // carries no visible body).
        if(seg.classList.contains('assistant-segment-worklog-source')) continue;
        if(seg.classList.contains('assistant-segment-anchor')) continue;
        if((seg.textContent||'').trim()) return true;
      }
      return false;
    };
    for(const turn of inner.querySelectorAll('.assistant-turn')){
      if(turn.id==='liveAssistantTurn') continue; // live turn drives its own state
      if(_turnHasVisibleContent(turn)) continue;
      // No visible content — surface the folded Worklog so the turn isn't blank.
      const groups=turn.querySelectorAll('.tool-worklog-group,.tool-call-group');
      let revealed=false;
      for(const group of groups){
        if(!(group.textContent||'').trim()) continue; // empty group can't help
        if(group.classList.contains('tool-call-group-collapsed')){
          group.classList.remove('tool-call-group-collapsed');
          group.classList.add('open');
          const summary=group.querySelector('.tool-call-group-summary,.activity-summary');
          if(summary) summary.setAttribute('aria-expanded','true');
          // #5839: this turn is otherwise blank, so materialize any deferred
          // settled rows now that we're force-expanding the worklog to fill it.
          if(typeof _materializeDeferredWorklogRows==='function') _materializeDeferredWorklogRows(group);
        }
        // `revealed` means "this turn has a non-empty Worklog group that the user
        // can see" — NOT "we just expanded something". An already-open non-empty
        // group is itself visible (it slips past _turnHasVisibleContent only
        // because that check inspects .assistant-segment nodes, not group bodies),
        // so the turn isn't truly blank and the last-resort un-hide below is
        // unnecessary. Keep this assignment OUTSIDE the if(collapsed) branch.
        revealed=true;
      }
      // Last resort: no usable worklog group either, but hidden worklog-source
      // segments carry the real text — un-hide them so nothing is lost.
      if(!revealed){
        for(const seg of turn.querySelectorAll('.assistant-segment-worklog-source')){
          if(!(seg.textContent||'').trim()) continue;
          seg.classList.remove('assistant-segment-worklog-source');
          seg.removeAttribute('aria-hidden');
          seg.hidden=false;
        }
      }
    }
  }
  // Re-attach the preserved live turn (#3877). The rebuild above recreated a
  // live turn from S.messages, but the live assistant message's content lags the
  // stream (it is only persisted to S.messages on a throttled write-back) — so the
  // fresh node often shows LESS streamed text than the ORIGINAL node, which is
  // still referenced by the smd parser and holds the real in-progress reply. Swap
  // the preserved (parser) node back in so the parser target stays connected and
  // the visible text never blanks.
  //
  // The swap fires when the preserved node carries at least as much streamed text
  // as the rebuilt one (`_rebuiltLen <= _preservedLen`). The `<=` (not `<`) is
  // load-bearing: at the throttled-persist boundary the rebuilt turn's live
  // content can EQUAL the preserved length, and the old `<` guard then skipped the
  // swap — leaving the smd parser writing into the detached original node, which
  // is exactly the residual "disappears, then reappears" frame (#3877 reopen). On
  // a tie the preserved node is strictly preferable (it holds the live parser
  // reference; identical length means nothing is lost). When the rebuilt turn
  // genuinely has MORE content (e.g. a reconnect where S.messages caught up past
  // the parser), the guard correctly skips and lets the parser re-resolve to the
  // fuller node.
  //
  // Swap at the SEGMENT level — replace only the rebuilt live segment with the
  // preserved one — so a multi-segment turn (earlier settled segments + tool/
  // worklog groups built by the rebuild) keeps that rebuilt-only structure; a
  // whole-turn replaceWith would discard it when the preserved snapshot predates
  // those segments. Fall back to whole-turn replace only when the rebuilt turn has
  // no live segment to swap into. No-op for a settled turn or when nothing was
  // streaming.
  if(_preservedLiveTurn){
    const _rebuilt=document.getElementById('liveAssistantTurn');
    // Pick the PARSER-OWNED live segment, not just the first one. On reconnect /
    // post-tool activity boundaries a live turn can carry MULTIPLE
    // [data-live-assistant="1"] segments, and the smd parser writes into the
    // LAST (tail) one (see ensureAssistantRow in messages.js — it re-attaches to
    // the last live segment). Prefer the preserved segment whose
    // data-live-segment-seq matches the rebuilt tail (same logical segment), then
    // fall back to the last preserved live segment. Using querySelector() (first)
    // here would move the wrong segment and leave the parser-owned tail detached
    // in a multi-segment turn.
    const _rebuiltSegs=_rebuilt?_rebuilt.querySelectorAll('[data-live-assistant="1"]'):null;
    const _rebuiltSeg=(_rebuiltSegs&&_rebuiltSegs.length)?_rebuiltSegs[_rebuiltSegs.length-1]:null;
    const _preservedSegs=_preservedLiveTurn.querySelectorAll('[data-live-assistant="1"]');
    let _preservedSeg=_preservedSegs.length?_preservedSegs[_preservedSegs.length-1]:null;
    const _rebuiltSeq=_rebuiltSeg?_rebuiltSeg.getAttribute('data-live-segment-seq'):null;
    if(_rebuiltSeq){
      for(const _seg of _preservedSegs){
        if(_seg.getAttribute('data-live-segment-seq')===_rebuiltSeq){_preservedSeg=_seg;break;}
      }
    }
    const _preservedLen=_liveAssistantSegmentTextLength(_preservedSeg||_preservedLiveTurn);
    // Structural-block counts: a live turn can be AHEAD of S.messages with
    // Activity/tool/worklog blocks that haven't persisted yet — even with ZERO
    // streamed text (e.g. an Activity-only turn mid-tool-call). The text-length
    // gate alone would skip preservation in that case, so a scroll-triggered
    // rebuild on a long (virtualized) transcript could blink those live-only
    // blocks for a frame. Also restore when the preserved turn carries more
    // structure than the rebuilt (lagging-S.messages) turn. (#3714 ship-review)
    const _structuralCount=(turn)=> turn?turn.querySelectorAll(
      '[data-live-assistant="1"],.tool-call-group,.tool-card-row,'+
      '.tool-worklog-group,.live-worklog[data-live-worklog-shell="1"],'+
      '.wl-reason,.agent-activity-thinking,.thinking-card-row'
    ).length:0;
    const _preservedStructure=_structuralCount(_preservedLiveTurn);
    const _rebuiltStructure=_structuralCount(_rebuilt);
    if(_preservedLen>0 || _preservedStructure>_rebuiltStructure){
      const _rebuiltLen=_rebuilt?_liveAssistantSegmentTextLength(_rebuiltSeg||_rebuilt):-1;
      if(_rebuiltLen<=_preservedLen){
        // Decide segment-level vs whole-turn restore. Segment-level keeps the
        // rebuilt turn's structure (good when the rebuild is the structural
        // superset). But the whole premise here is that the live DOM can be
        // AHEAD of S.messages: a tool/worklog group can land in the live turn
        // between the last throttled persist and this rebuild, so the rebuilt
        // turn (built from the lagging S.messages) may have FEWER structural
        // blocks. In that case a segment-only swap would drop those live-only
        // blocks for a frame — so restore the WHOLE preserved turn instead.
        // Otherwise (rebuild has >= the preserved turn's structural blocks) do
        // the precise segment swap so rebuilt-only structure is kept.
        if(_rebuilt&&_rebuiltSeg&&_preservedSeg&&_rebuiltStructure>=_preservedStructure){
          // Rebuild is the structural superset — swap only the parser-owned
          // (tail) live segment, keeping rebuilt-only segments / tool groups.
          // (No dataset.sessionId stamp here: only the segment enters the DOM;
          // the rebuilt turn was already stamped at build time, see above.)
          _rebuiltSeg.replaceWith(_preservedSeg);
        }else if(_rebuilt){
          // Rebuilt turn lacks structure the live turn already has (live-only
          // tool card not yet persisted), or has no live segment to target —
          // restore the whole preserved turn so nothing the user saw vanishes.
          if(S.session) _preservedLiveTurn.dataset.sessionId=S.session.session_id;
          _rebuilt.replaceWith(_preservedLiveTurn);
        }else{
          if(S.session) _preservedLiveTurn.dataset.sessionId=S.session.session_id;
          inner.appendChild(_preservedLiveTurn);
        }
      }
    }
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
  liveActivityBindings._sessionHtmlCacheSid=sid;
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
