import { _stripAttachedFilesMarker } from './session-display.js';

function _messageComparableText(m){
  if(!m) return '';
  if(typeof msgContent==='function'){
    try{return String(msgContent(m)||'').trim();}
    catch(_){}
  }
  return String(m.content||'').trim();
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

export const currentTurnTranscript=Object.freeze({compare:_sameTranscriptMessage,mergeInflight:_mergeInflightTailMessages,prepareRunningTail:_prepareRunningLiveTail,projectActivityBursts:_projectInflightMessagesForActivityBursts});

export { _dropCurrentTurnAssistantMessages, _ensureInflightLiveAssistantMessage, _hasCurrentTailUserDuplicate, _mergeInflightTailMessages, _messageComparableText, _prepareRunningLiveTail, _projectInflightMessagesForActivityBursts, _sameTranscriptMessage, _stripAttachedFilesMarker };
