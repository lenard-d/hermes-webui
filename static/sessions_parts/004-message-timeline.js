window.HermesSessions=window.HermesSessions||{};
window.HermesSessions.parts=window.HermesSessions.parts||{};
function _messageComparableText(m){
  if(!m) return '';
  if(typeof msgContent==='function'){
    try{return String(msgContent(m)||'').trim();}
    catch(_){}
  }
  return String(m.content||'').trim();
}

function _stripAttachedFilesMarker(text){
  return String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
}

function _stripForcedSkillEnvelope(text){
  let value=String(text||'').trim();
  // `/use <skill>` augments the model-facing prompt with a directive and a
  // hidden skill-content envelope, while the optimistic UI row keeps the human
  // prompt.  Treat them as the same submitted turn for active reload/reconnect
  // dedupe without rewriting the persisted pending prompt.
  value=value.replace(/^\[USER OVERRIDE\][^\n]*\n*/,'').trim();
  value=value.replace(/\[FORCED SKILL CONTEXT:[^\]]+\][\s\S]*?\[\/FORCED SKILL CONTEXT\]\s*/g,'').trim();
  return value;
}

function _normalizeUserTranscriptText(text){
  const value=_stripAttachedFilesMarker(_stripForcedSkillEnvelope(text));
  // ui.js is loaded before sessions.js in index.html and owns the canonical
  // workspace-sentinel parser used by rendering.  Keep a small fallback for
  // static/helper tests and defensive partial loads, but prefer the renderer's
  // parser whenever it is available.
  if(typeof _stripWorkspaceDisplayPrefix==='function'){
    return _stripWorkspaceDisplayPrefix(value);
  }
  const raw=String(value||'');
  const strippedV1=raw.replace(/^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*/,'');
  if(strippedV1!==raw) return strippedV1.trim();
  return raw.replace(/^\s*\[Workspace:[^\]]+\]\s*/,'').trim();
}

function _sameTranscriptMessage(a,b){
  if(!(a&&b)) return false;
  const role=String(a.role||'');
  if(role!==String(b.role||'')) return false;
  const aText=_messageComparableText(a);
  const bText=_messageComparableText(b);
  if(aText===bText) return true;
  if(role==='user'){
    return _normalizeUserTranscriptText(aText)===_normalizeUserTranscriptText(bText);
  }
  return false;
}

function _currentTailUserMessage(messages){
  const list=Array.isArray(messages)?messages:[];
  for(let i=list.length-1;i>=0;i--){
    const msg=list[i];
    if(!msg) continue;
    if(String(msg.role||'')==='user'){
      // Compaction rows are synthetic user-role markers, not submitted turns.
      if(typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(msg)) continue;
      return msg;
    }
    if(msg._live||String(msg.role||'')==='tool') continue;
    return null;
  }
  return null;
}

function _hasCurrentTailUserDuplicate(messages,candidate){
  if(!candidate||String(candidate.role||'')!=='user') return false;
  const existing=_currentTailUserMessage(messages);
  return !!(existing&&_sameTranscriptMessage(existing,candidate));
}

function _currentTurnAssistantText(messages){
  const list=Array.isArray(messages)?messages:[];
  let start=-1;
  for(let i=list.length-1;i>=0;i--){
    if(list[i]&&list[i].role==='user'){start=i;break;}
  }
  const parts=[];
  for(let i=start+1;i<list.length;i++){
    const msg=list[i];
    if(!msg||msg.role!=='assistant'||msg._live) continue;
    const text=_messageComparableText(msg);
    if(text) parts.push(text);
  }
  return parts.join('\n\n').trim();
}

function _compactTranscriptText(text){
  return String(text||'').replace(/\s+/g,' ').trim();
}

function _dropCurrentTurnAssistantMessages(messages){
  const list=Array.isArray(messages)?messages:[];
  let start=-1;
  for(let i=list.length-1;i>=0;i--){
    if(list[i]&&list[i].role==='user'){start=i;break;}
  }
  if(start<0) return list;
  return list.filter((msg,idx)=>idx<=start||!(msg&&msg.role==='assistant'));
}

function _ensureInflightLiveAssistantMessage(inflight){
  if(!inflight) return false;
  const text=String(inflight.lastAssistantText||'').trim();
  const reasoning=String(inflight.lastReasoningText||'').trim();
  if(!text&&!reasoning) return false;
  if(!Array.isArray(inflight.messages)) inflight.messages=[];
  let live=null;
  for(let i=inflight.messages.length-1;i>=0;i--){
    const msg=inflight.messages[i];
    if(msg&&msg.role==='assistant'&&msg._live){live=msg;break;}
  }
  if(live){
    const liveText=_messageComparableText(live);
    if(text&&(!liveText||text.startsWith(liveText)||text.length>liveText.length)){
      live.content=text;
    }
    if(reasoning&&!live.reasoning) live.reasoning=reasoning;
    return true;
  }
  inflight.messages.push({
    role:'assistant',
    content:text,
    reasoning:reasoning||undefined,
    _live:true,
    _ts:Date.now()/1000,
  });
  return true;
}

function _projectInflightMessagesForActivityBursts(inflight){
  const messages=Array.isArray(inflight&&inflight.messages)?inflight.messages:[];
  const anchors=Array.isArray(inflight&&inflight.activityBurstAnchors)?inflight.activityBurstAnchors:[];
  if(!anchors.length) return messages;
  let liveIdx=-1;
  for(let i=messages.length-1;i>=0;i--){
    const msg=messages[i];
    if(msg&&msg.role==='assistant'&&msg._live){liveIdx=i;break;}
  }
  if(liveIdx<0) return messages;
  let liveTailStartIdx=liveIdx;
  while(liveTailStartIdx>0){
    const prev=messages[liveTailStartIdx-1];
    if(!(prev&&prev.role==='assistant'&&prev._live)) break;
    liveTailStartIdx-=1;
  }
  const live=messages[liveIdx];
  const text=_messageComparableText(live);
  if(!text) return messages;
  const priorLiveTexts=messages.slice(liveTailStartIdx,liveIdx)
    .filter(m=>m&&m.role==='assistant'&&m._live)
    .map(m=>_messageComparableText(m))
    .filter(Boolean);
  const liveTailIsAccumulator=priorLiveTexts.length>0&&priorLiveTexts.every(part=>
    _compactTranscriptText(text).includes(_compactTranscriptText(part))
  );
  const replaceStartIdx=liveTailIsAccumulator?liveTailStartIdx:liveIdx;
  if(priorLiveTexts.length&&!liveTailIsAccumulator) return messages;
  const cleanAnchors=anchors
    .map(a=>({id:Number(a&&a.id),textEnd:Number(a&&a.textEnd)}))
    .filter(a=>Number.isFinite(a.id)&&Number.isFinite(a.textEnd)&&a.textEnd>0)
    .sort((a,b)=>a.textEnd-b.textEnd||a.id-b.id);
  const aliasBurstIds=new Map();
  const fallbackBurstId = Number(inflight.currentActivityBurstId||0)||0;
  aliasBurstIds.set(0,fallbackBurstId);
  let lastVisibleBurstId=null;
  let lastVisibleTextEnd=0;
  const visibleAnchors=[];
  for(const anchor of cleanAnchors){
    const end=Math.min(text.length,anchor.textEnd);
    if(end<=lastVisibleTextEnd){
      if(lastVisibleBurstId!==null) aliasBurstIds.set(anchor.id,lastVisibleBurstId);
      continue;
    }
    visibleAnchors.push(anchor);
    lastVisibleBurstId=anchor.id;
    lastVisibleTextEnd=end;
  }

  if(visibleAnchors.length&&Number.isFinite(visibleAnchors[0].id)) aliasBurstIds.set(0,visibleAnchors[0].id);
  if(!visibleAnchors.length){
    const firstVisibleBurstId=Number(cleanAnchors[0]&&cleanAnchors[0].id);
    const fallbackAnchorId=Number.isFinite(firstVisibleBurstId)?firstVisibleBurstId:fallbackBurstId;
    if(fallbackAnchorId!==fallbackBurstId) aliasBurstIds.set(0,fallbackAnchorId);
    const projected=[{...live,content:text,_activityBurstId:fallbackAnchorId}];

    const baselineSeq=Number(inflight.currentLiveSegmentSeq);
    const existingSeqs=messages
      .filter(m=>m&&m._live&&Number.isFinite(Number(m._liveSegmentSeq)))
      .map(m=>Number(m._liveSegmentSeq));
    const baseFromMessages=existingSeqs.length
      ? existingSeqs.reduce((acc,n)=>Math.max(acc,n),-Infinity)
      : 0;
    const firstSeq=(Number.isFinite(baselineSeq)&&baselineSeq>0)
      ? baselineSeq
      : (Number.isFinite(baseFromMessages)&&baseFromMessages>0)
        ? baseFromMessages
        : 1;
    projected.forEach((seg,i)=>{
      seg._liveSegmentSeq=i===0?firstSeq:1+i;
    });
    if(Array.isArray(inflight.toolCalls)){
      const segmentSeqByBurstId=new Map();
      segmentSeqByBurstId.set(String(fallbackAnchorId),firstSeq);
      projected.forEach(seg=>{
        const bid=Number(seg&&seg._activityBurstId);
        const seq=Number(seg&&seg._liveSegmentSeq);
        if(!Number.isFinite(bid)||!Number.isFinite(seq)) return;
        const key=String(bid);
        const current=segmentSeqByBurstId.get(key);
        if(current===undefined||seq>current) segmentSeqByBurstId.set(key,seq);
      });

      const validSeqs=new Set(segmentSeqByBurstId.values());
      const canonicalBurstId=(value)=>{
        const bid=Number(value);
        if(!Number.isFinite(bid)) return null;
        if(aliasBurstIds.has(bid)) return aliasBurstIds.get(bid);
        return bid;
      };

      inflight.toolCalls.forEach(tc=>{
        if(!tc) return;
        if(tc.activityBurstId!==undefined&&tc.activityBurstId!==null){
          const current=Number(tc.activityBurstId);
          if(aliasBurstIds.has(current)) tc.activityBurstId=aliasBurstIds.get(current);
        }
        const segSeq=Number(tc.activitySegmentSeq);
        if(Number.isFinite(segSeq)&&validSeqs.has(segSeq)) return;
        const canonical=canonicalBurstId(tc.activityBurstId);
        if(!Number.isFinite(canonical)){
          if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
          return;
        }
        const mappedSeq=segmentSeqByBurstId.get(String(canonical));
        if(Number.isFinite(mappedSeq)) tc.activitySegmentSeq=mappedSeq;
        else if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
      });
    }
    return [...messages.slice(0,replaceStartIdx),...projected,...messages.slice(liveIdx+1)];
  }
  const projected=[];
  let prev=0;
  for(let i=0;i<visibleAnchors.length;i++){
    const anchor=visibleAnchors[i];
    const end=Math.max(prev,Math.min(text.length,anchor.textEnd));
    const part=text.slice(prev,end).trim();
    if(part) projected.push({...live,content:part,_activityBurstId:anchor.id});
    else{
      const fallbackAnchor = visibleAnchors[i+1] || visibleAnchors[i-1];
      if(fallbackAnchor && Number.isFinite(anchor.id)&&Number.isFinite(fallbackAnchor.id)){
        aliasBurstIds.set(anchor.id,fallbackAnchor.id);
      }
    }
    prev=end;
  }
  const tail=text.slice(prev).trim();
  if(tail) projected.push({...live,content:tail,_activityBurstId:Number(inflight.currentActivityBurstId||0)||0});
  if(!projected.length) return messages;

  const baselineSeq=Number(inflight.currentLiveSegmentSeq);
  const existingSeqs=messages
    .filter(m=>m&&m._live&&Number.isFinite(Number(m._liveSegmentSeq)))
    .map(m=>Number(m._liveSegmentSeq));
  const baseFromMessages=existingSeqs.length
    ? existingSeqs.reduce((acc,n)=>Math.max(acc,n),-Infinity)
    : 0;
  const endSeq=(Number.isFinite(baselineSeq)&&baselineSeq>0)
    ? baselineSeq
    : (Number.isFinite(baseFromMessages)&&baseFromMessages>0)
      ? baseFromMessages
      : projected.length;
  let firstSeq=endSeq-projected.length+1;
  if(!Number.isFinite(firstSeq)||firstSeq<1) firstSeq=1;
  projected.forEach((seg,i)=>{
    const seq=firstSeq+i;
    seg._liveSegmentSeq=seq;
  });
  if(Number.isFinite(firstSeq) && projected.length){
    inflight.currentLiveSegmentSeq=projected[projected.length-1]._liveSegmentSeq;
  }

  const segmentSeqByBurstId=new Map();
  projected.forEach(seg=>{
    const bid=Number(seg&&seg._activityBurstId);
    const seq=Number(seg&&seg._liveSegmentSeq);
    if(!Number.isFinite(bid)||!Number.isFinite(seq)) return;
    const key=String(bid);
    const current=segmentSeqByBurstId.get(key);
    if(current===undefined||seq>current) segmentSeqByBurstId.set(key,seq);
  });

  const canonicalBurstId = (value)=>{
    const bid=Number(value);
    if(!Number.isFinite(bid)) return null;
    if(aliasBurstIds.has(bid)) return aliasBurstIds.get(bid);
    return bid;
  };

  const validSeqs=new Set(segmentSeqByBurstId.values());

  if(Array.isArray(inflight.toolCalls)){
    inflight.toolCalls.forEach(tc=>{
      if(!tc) return;
      if(tc.activityBurstId!==undefined&&tc.activityBurstId!==null){
        const current=Number(tc.activityBurstId);
        if(aliasBurstIds.has(current)) tc.activityBurstId=aliasBurstIds.get(current);
      }
      const segSeq=Number(tc.activitySegmentSeq);
      if(Number.isFinite(segSeq)&&validSeqs.has(segSeq)) return;
      const canonical=canonicalBurstId(tc.activityBurstId);
      if(!Number.isFinite(canonical)){
        if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
        return;
      }
      const mappedSeq=segmentSeqByBurstId.get(String(canonical));
      if(Number.isFinite(mappedSeq)) tc.activitySegmentSeq=mappedSeq;
      else if(Number.isFinite(segSeq)) tc.activitySegmentSeq=undefined;
    });
  }
  return [...messages.slice(0,replaceStartIdx),...projected,...messages.slice(liveIdx+1)];
}

function _prepareRunningLiveTail(baseMessages,inflightMessages){
  const inflight=Array.isArray(inflightMessages)?inflightMessages:[];
  const liveMessages=inflight.filter(m=>m&&m.role==='assistant'&&m._live);
  if(liveMessages.length>1) return liveMessages.some(m=>!!_messageComparableText(m));
  const live=liveMessages[0]||null;
  if(!live) return false;
  const liveText=_messageComparableText(live);
  const persistedText=_currentTurnAssistantText(baseMessages);
  if(persistedText){
    const compactPersisted=_compactTranscriptText(persistedText);
    const compactLive=_compactTranscriptText(liveText);
    if(!liveText || persistedText.startsWith(liveText)){
      live.content=persistedText;
    }else if(liveText.startsWith(persistedText)){
      const extra=liveText.slice(persistedText.length).trim();
      if(extra&&compactPersisted.includes(_compactTranscriptText(extra))){
        live.content=persistedText;
      }
    }else if(compactPersisted===compactLive){
      live.content=persistedText;
    }
  }
  return !!_messageComparableText(live);
}

function _mergeInflightTailMessages(baseMessages, inflightMessages){
  const base=Array.isArray(baseMessages)?baseMessages:[];
  const inflight=Array.isArray(inflightMessages)?inflightMessages:[];
  let firstLiveIdx=-1;
  for(let i=0;i<inflight.length;i++){
    if(inflight[i]&&inflight[i]._live){firstLiveIdx=i;break;}
  }
  if(firstLiveIdx<0) return base;
  let start=firstLiveIdx;
  if(firstLiveIdx>0&&inflight[firstLiveIdx-1]&&inflight[firstLiveIdx-1].role==='user') start=firstLiveIdx-1;
  const tail=inflight.slice(start).filter(m=>m&&m.role);
  const merged=[...base];
  for(const msg of tail){
    let candidate=msg;
    if(!candidate) continue;
    const duplicate=String(candidate.role||'')==='user'
      ? _hasCurrentTailUserDuplicate(merged,candidate)
      : merged.slice(-Math.max(5,tail.length+2)).some(existing=>_sameTranscriptMessage(existing,candidate));
    if(!duplicate) merged.push(candidate);
  }
  return merged;
}

// Load older messages when the user scrolls to the top of the conversation.
// Prepends them to S.messages and re-renders, preserving scroll position.
let _loadingOlder = false;
// _oldestIdx tracks the index (in the server's full message array) of the
// oldest message currently loaded in S.messages. Starts at 0 when all
// messages are loaded, or > 0 when truncated by msg_limit.
let _oldestIdx = 0;
// Generation token bumped every time S.messages is wholesale-replaced
// (rather than incrementally extended). _loadOlderMessages snapshots it
// before its `await` and re-checks after, so a late-resolving prefetch
// does not prepend onto a transcript that was rebuilt under it
// (e.g. by _ensureAllMessagesLoaded after a Start-jump). See #1937.
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {
  // Wrap to keep the counter bounded; the only operation that matters is
  // strict inequality between the snapshot and the post-await read, so any
  // monotonic bump is sufficient.
  _messagesGeneration = (_messagesGeneration + 1) | 0;
  return _messagesGeneration;
}

async function _loadOlderMessages() {
  if (_loadingOlder || !_messagesTruncated) return;
  const sid = S.session ? S.session.session_id : null;
  if (!sid || !S.messages.length) return;
  if (_oldestIdx <= 0) { _messagesTruncated = false; return; }
  _loadingOlder = true;
  // Snapshot the generation BEFORE we await. If S.messages is wholesale
  // replaced while the request is in flight, the post-await check below
  // bails out so we never prepend stale older messages onto a freshly
  // rebuilt transcript (#1937).
  const startGeneration = _messagesGeneration;
  try {
    // Two strategies, chosen by whether the growing tail window still fits under
    // the server's msg_limit ceiling (_MSG_LIMIT_MAX, mirroring backend
    // _MAX_MSG_LIMIT):
    //
    //  - Below the ceiling: ask for a larger authoritative tail window
    //    (currentLoaded + _INITIAL_MSG_LIMIT). Post-#2716 the backend runs the
    //    full append-only merge, so a larger msg_limit produces the same merged
    //    transcript we'd get by stitching pages, without client-side index
    //    bookkeeping. The newly exposed head is what we expose to the user.
    //
    //  - At/above the ceiling: the server clamps msg_limit, so the tail window
    //    stops growing and this strategy would stall (the same clamped tail is
    //    returned, olderMsgs -> 0). Switch to msg_before paging — a fixed
    //    _INITIAL_MSG_LIMIT backward page keyed off _oldestIdx — which is
    //    bounded and never hits the ceiling, so the head stays reachable for
    //    arbitrarily long transcripts. (This is the same paging request the
    //    race-fallback below uses, proven correct there.)
    const requestedLimit = Math.max(_INITIAL_MSG_LIMIT, (S.messages || []).length + _INITIAL_MSG_LIMIT);
    const useBeforePaging = requestedLimit >= _msgLimitMax;
    const data = useBeforePaging
      ? await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`,
          {timeoutMs:120000}
        )
      : await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_limit=${requestedLimit}`,
          {timeoutMs:120000}
        );
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) { _loadingOlder = false; return; }
    //  - response shape sane
    //  - the active session is still the one we issued the request for.
    //    Compare against S.session.session_id, NOT _loadingSessionId — the
    //    latter is null between session loads, leaving a window where a
    //    stale response could prepend onto the new session's S.messages.
    if (!data || !data.session) return;
    if (!S.session || S.session.session_id !== sid) return;
    if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
    // Generation guard: another code path (typically jumpToSessionStart →
    // _ensureAllMessagesLoaded) may have replaced S.messages while we were
    // awaiting. Prepending older messages onto that replacement would
    // duplicate the head of the transcript. Detect via the generation
    // counter and abort cleanly. _oldestIdx and _messagesTruncated were
    // already reset by the wholesale-replace path, so no rollback needed.
    if (_messagesGeneration !== startGeneration) return;
    let responseSession = data.session;
    let expandedMsgs = (responseSession.messages || []).filter(m => m && m.role);
    const currentMsgs = (S.messages || []).filter(m => m && m.role);
    const currentLen = currentMsgs.length;
    // Suffix-continuity check: the cumulative tail is only safe to wholesale-
    // replace when our currently-displayed messages are still its suffix. If
    // the server appended new messages (or merge filtered something) while we
    // were awaiting, the suffix won't line up — fall back to the legacy
    // msg_before page so we never drop visible older messages on the floor.
    // When useBeforePaging is true, `data` is a bounded msg_before OLDER page,
    // not a cumulative tail. A raw-row-heavy older page whose visible text
    // repeats the current tail could otherwise pass the suffix check below and
    // be wholesale-replaced AS IF it were the full tail — silently discarding
    // the current (newer) rows and marking history complete. Gate the suffix
    // heuristic on !useBeforePaging so every msg_before page is always treated
    // as an older page and prepended (Codex gate #6154, silent row-loss).
    let tailMatches = !useBeforePaging && expandedMsgs.length >= currentLen;
    if (tailMatches && currentLen > 0) {
      const start = expandedMsgs.length - currentLen;
      for (let i = 0; i < currentLen; i++) {
        if (!_sameTranscriptMessage(expandedMsgs[start + i], currentMsgs[i])) {
          tailMatches = false;
          break;
        }
      }
    }
    let olderCount = Math.max(0, expandedMsgs.length - currentLen);
    let olderMsgs = expandedMsgs.slice(0, olderCount);
    let nextMessages = expandedMsgs;
    if (!tailMatches) {
      // Race fallback (or the over-ceiling msg_before primary path): keep the
      // legacy index-page request as the correctness-preserving alternative.
      // When useBeforePaging is true we already fetched a msg_before page as
      // the primary `data`, so reuse it instead of re-fetching. Same guards
      // reapplied because we just awaited again (skipped for the reuse case).
      if (!useBeforePaging) {
        const fallback = await api(
          `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0&msg_before=${_oldestIdx}&msg_limit=${_INITIAL_MSG_LIMIT}`,
          {timeoutMs:120000}
        );
        if (!fallback || !fallback.session) { _loadingOlder = false; return; }
        if (!S.session || S.session.session_id !== sid) return;
        if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
        if (_messagesGeneration !== startGeneration) return;
        responseSession = fallback.session;
      }
      olderMsgs = (responseSession.messages || []).filter(m => m && m.role);
      nextMessages = [...olderMsgs, ...S.messages];
    }
    if (!olderMsgs.length) { _messagesTruncated = !!responseSession._messages_truncated; return; }
    // Replace with the larger tail window and preserve scroll as if older
    // messages were prepended. When the suffix check fails, nextMessages
    // already encodes the legacy prepend fallback so the visible behavior
    // matches the old msg_before page path exactly.
    // Use $('messages') — the scrollable container (#msgInner is not scrollable).
    const container = $('messages');
    const prevScrollH = container ? container.scrollHeight : 0;
    const oldTop = container ? container.scrollTop : 0;
    const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')
      ? _captureMessageViewportAnchor()
      : null;
    // Carry forward ephemeral turn fields (_turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard/_anchor_stream_id) before the wholesale replace so the badge
    // does not briefly appear and disappear during older-message expansion.
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      nextMessages = window._carryForwardEphemeralTurnFields(S.messages || [], nextMessages);
    }
    S.messages = nextMessages;
    _syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);
    // renderMessages() windows long transcripts from the end. If we do not
    // expand that window before rendering, the newly prepended page stays
    // hidden and the "hidden" counter rises while the viewport appears stuck.
    // Count by the same visible-message rules used by renderMessages(); the
    // virtual fallback below uses this as a pixel-height prefix length.
    const addedRenderable = olderMsgs.filter(m=>{
      if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);
      if(!m||!m.role||m.role==='tool') return false;
      if(typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m)) return false;
      if(typeof _isPreservedCompressionTaskListMessage==='function'&&_isPreservedCompressionTaskListMessage(m)) return false;
      if(typeof _isRecoveryControlMessage==='function'&&_isRecoveryControlMessage(m)) return false;
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
      return !!(msgContent(m)||m._statusCard||m.attachments?.length||(m.role==='assistant'&&(hasTc||hasTu||hasPartialTc||(typeof _messageHasReasoningPayload==='function'&&_messageHasReasoningPayload(m))||(typeof _assistantMessageHasVisibleContent==='function'&&_assistantMessageHasVisibleContent(m)))));
    }).length;
    _messageRenderWindowSize=_currentMessageRenderWindowSize()+Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT);
    _messagesTruncated = !!responseSession._messages_truncated;
    _oldestIdx = responseSession._messages_offset || 0;
    renderMessages({ preserveScroll: true });
    if (container) {
      // Prepending older messages must not teleport the reader. Anchor to the
      // first visible rendered row and restore that row's top offset after the
      // prepend so synthetic virtual spacer heights cannot skew the delta.
      const restoredViaAnchor = (viewportAnchor && typeof _restoreMessageViewportAnchor === 'function')
        ? _restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)
        : false;
      if (!restoredViaAnchor) {
        const virtualAddedHeight = (typeof _messageVirtualPrependedHeightDelta === 'function')
          ? _messageVirtualPrependedHeightDelta(addedRenderable)
          : null;
        const newScrollH = container.scrollHeight;
        const addedHeight = Number.isFinite(virtualAddedHeight)
          ? virtualAddedHeight
          : Math.max(0, newScrollH - prevScrollH);
        _programmaticScroll = true;
        container.scrollTop = oldTop + addedHeight;
        requestAnimationFrame(()=>{ _programmaticScroll = false; });
      }
    }
    _scrollPinned = false;
  } catch(e) {
    console.warn('_loadOlderMessages failed:', e);
  } finally {
    // Always clear the loading lock. If the user switched sessions while
    // this request was in flight, loadSession() already set _loadingOlder=false
    // (see line ~122), so this is a harmless double-reset.
    _loadingOlder = false;
  }
}

// Ensure the full message history is loaded (for undo, export, etc).
// If the session was loaded with msg_limit, this fetches all messages.
//
// Race-safety (#1937): with the endless-scroll opt-in, _loadOlderMessages
// may be in flight when this runs (e.g. user scrolled near the top, then
// hit the Start jump pill). Two coordinated guards prevent the prefetch
// from prepending duplicate messages onto our wholesale replacement:
//   1. Hold the _loadingOlder mutex around the body so a NEW prefetch
//      cannot start mid-replace (entry-gate check at line ~1003 returns
//      early). The mutex is also self-protecting against concurrent
//      ensure-all calls from rapid double-clicks on Start.
//   2. Bump _messagesGeneration before mutating S.messages so any
//      in-flight prefetch's post-await generation check bails out.
async function _ensureAllMessagesLoaded() {
  if (!_messagesTruncated || !S.session) return;
  if (_loadingOlder) {
    // A prefetch is mid-flight (between the `_loadingOlder = true` line
    // and its post-await guards). Bumping the generation token now
    // poisons that prefetch's continuation, but we still need to claim
    // the mutex AFTER it releases. Yield until the prefetch finishes
    // (its finally-block clears _loadingOlder) before fetching the full
    // history ourselves. The generation bump below ensures any other
    // future race against this same continuation also fails closed.
    _bumpMessagesGeneration();
    while (_loadingOlder) {
      await new Promise(resolve => setTimeout(resolve, 16));
    }
    if (!_messagesTruncated || !S.session) return;
  }
  _loadingOlder = true;
  try {
    const sid = S.session.session_id;
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0`, {timeoutMs:120000});
    // Guard: api() may have redirected (401) and returned undefined.
    if (!data || !data.session) return;
    // Session may have been switched while we awaited. Bail rather than
    // overwrite the new session's messages.
    if (!S.session || S.session.session_id !== sid) return;
    if (_loadingSessionId !== null && _loadingSessionId !== sid) return;
    const msgs = (data.session.messages || []).filter(m => m && m.role);
    // Bump the generation BEFORE the wholesale replace so any racing
    // prefetch (whose snapshot was taken before this call's mutex
    // acquisition) sees the new value and aborts.
    _bumpMessagesGeneration();
    // #3306: Same ephemeral-field carry-forward as _ensureMessagesLoaded.
    // Loading older messages also does a wholesale replace of S.messages
    // and would otherwise drop _turnUsage/_turnDuration/_turnTps/
    // _gatewayRouting/_statusCard/_anchor_stream_id on the existing turns.
    let _msgsToAssign = msgs;
    if (typeof window._carryForwardEphemeralTurnFields === 'function') {
      _msgsToAssign = window._carryForwardEphemeralTurnFields(S.messages || [], msgs);
    }
    S.messages = _msgsToAssign;
    _messagesTruncated = false;
    _oldestIdx = 0;
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
    if (S.session && S.session.session_id === sid) {
      S.session.message_count = Number(data.session.message_count || msgs.length);
    }
  } finally {
    _loadingOlder = false;
  }
}

window.HermesSessions.parts.messageTimeline=Object.freeze({mergeInflight:_mergeInflightTailMessages,prepareRunningTail:_prepareRunningLiveTail,loadOlder:_loadOlderMessages,ensureAllLoaded:_ensureAllMessagesLoaded});
