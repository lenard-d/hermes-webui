// Loaded before stream.js. Owns live tool-call identity, matching and INFLIGHT
// projection for one attached stream.
var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

function createStreamLiveToolTracker(options={}){
  const activeSid=String(options.sessionId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const INFLIGHT=options.inflightStore&&typeof options.inflightStore==='object'?options.inflightStore:{};
  const uploaded=Array.isArray(options.uploaded)?options.uploaded:[];
  const persistInflightState=typeof options.persist==='function'?options.persist:()=>{};
  const getAssistantRow=typeof options.getAssistantRow==='function'?options.getAssistantRow:()=>null;
  const getAssistantSegmentSeq=typeof options.getAssistantSegmentSeq==='function'?options.getAssistantSegmentSeq:()=>0;
  const getCurrentLiveSegmentSeq=typeof options.getCurrentLiveSegmentSeq==='function'?options.getCurrentLiveSegmentSeq:()=>0;
  const getCurrentActivityBurstId=typeof options.getCurrentActivityBurstId==='function'?options.getCurrentActivityBurstId:()=>0;

  function _stableStringify(value){
    const normalize=(v)=>{
      if(v===null||typeof v!=='object') return v;
      if(Array.isArray(v)) return v.map(normalize);
      const obj={};
      const keys=Object.keys(v).sort();
      for(const key of keys){
        obj[key]=normalize(v[key]);
      }
      return obj;
    };
    try{
      return JSON.stringify(normalize(value));
    }catch(_){
      return String(value||'');
    }
  }

  function _hashString(value){
    let hash=2166136261;
    for(let i=0;i<String(value||'').length;i++){
      hash^=String(value||'').charCodeAt(i);
      hash=Math.imul(hash,16777619);
    }
    return (hash>>>0).toString(16);
  }

  function _toolCallSignature(d, activityBurstId, activitySegmentSeq){
    const name=String(d&&d.name||'').trim().toLowerCase();
    const bid=Number(activityBurstId);
    const seq=Number(activitySegmentSeq);
    const args=d&&d.args;
    return `${name}|${Number.isFinite(bid)?bid:0}|${Number.isFinite(seq)?seq:0}|${_stableStringify(args)}`;
  }

  function _liveToolTid(d, activityBurstId, activitySegmentSeq){
    const explicit=String(d&&(d.tid||d.id||d.tool_call_id||d.tool_use_id||d.call_id)||'').trim();
    if(explicit) return explicit;
    return `live-${activeSid}-${_hashString(_toolCallSignature(d,activityBurstId,activitySegmentSeq))}`;
  }

  function _coerceLiveToolCallSignature(tc, activityBurstId, activitySegmentSeq){
    if(tc&&typeof tc==='object' && !tc._liveToolCallSignature){
      tc._liveToolCallSignature=_toolCallSignature(tc,activityBurstId,activitySegmentSeq);
    }
    return tc&&tc._liveToolCallSignature||'';
  }

  function _findPendingLiveToolCallIndex(toolCalls, opts){
    if(!Array.isArray(toolCalls)) return -1;
    const wantedTid=opts&&opts.tid||'';
    const wantedName=String(opts&&opts.name||'');
    const wantedSig=opts&&opts.signature||'';
    const wantedBurst=Number(opts&&opts.activityBurstId);
    const wantedSeq=Number(opts&&opts.activitySegmentSeq);
    const allowDone=!!(opts&&opts.allowDone);
    const matchName=(candidate)=>{
      return !candidate||!candidate.name||!wantedName ? false : String(candidate.name)===wantedName;
    };
    if(wantedTid){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const candidateTid=String(candidate.tid||candidate.id||candidate.tool_call_id||candidate.tool_use_id||candidate.call_id||'');
        if(candidateTid&&candidateTid===wantedTid) return i;
      }
    }
    if(wantedSig){
      for(let i=toolCalls.length-1;i>=0;i--){
        const candidate=toolCalls[i];
        if(!candidate||typeof candidate!=='object') continue;
        if(!allowDone&&candidate.done===true) continue;
        const canonicalSig=_coerceLiveToolCallSignature(
          candidate,
          Number.isFinite(wantedBurst)?wantedBurst:activityBurstFallbackFromCandidate(candidate),
          Number.isFinite(wantedSeq)?wantedSeq:activitySegmentSeqFallbackFromCandidate(candidate),
        );
        if(canonicalSig&&canonicalSig===wantedSig) return i;
      }
    }
    for(let i=toolCalls.length-1;i>=0;i--){
      const candidate=toolCalls[i];
      if(!candidate||typeof candidate!=='object') continue;
      if(!allowDone&&candidate.done===true) continue;
      if(!matchName(candidate)) continue;
      const candidateSeq=Number(candidate.activitySegmentSeq);
      const candidateBid=Number(candidate.activityBurstId);
      if(Number.isFinite(wantedSeq)&&Number.isFinite(candidateSeq)&&candidateSeq!==wantedSeq) continue;
      if(Number.isFinite(wantedBurst)&&Number.isFinite(candidateBid)&&candidateBid!==wantedBurst) continue;
      return i;
    }
    return -1;
  }

  function activityBurstFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activityBurstId);
  }
  function activitySegmentSeqFallbackFromCandidate(candidate){
    return Number(candidate && candidate.activitySegmentSeq);
  }

  function _coerceLiveToolCallSeq(candidate){
    const raw=Number.isFinite(candidate)?candidate:Number(candidate&&candidate.activitySegmentSeq);
    return Number.isFinite(raw)&&raw>0?raw:undefined;
  }

  function _currentLiveToolAnchor(){
    const assistantRow=getAssistantRow();
    const _assistantSegmentSeq=getAssistantSegmentSeq();
    const _currentLiveSegmentSeq=getCurrentLiveSegmentSeq();
    const _currentActivityBurstId=getCurrentActivityBurstId();
    const segmentSeq=Number(
      assistantRow&&assistantRow.getAttribute('data-live-segment-seq')||
      _assistantSegmentSeq||
      _currentLiveSegmentSeq||
      0
    );
    const burst=Number(_currentActivityBurstId);
    return {
      segmentSeq:Number.isFinite(segmentSeq)&&segmentSeq>0?segmentSeq:undefined,
      burstId:Number.isFinite(burst)?burst:0,
    };
  }

  function upsertLiveToolCall(d, phase){
    if(!d||d.name==='clarify') return null;
    const name=String(d&&d.name||'').trim();
    if(!name) return null;
    const current=_currentLiveToolAnchor();
    const inflight=INFLIGHT[activeSid] || (INFLIGHT[activeSid]={
      messages:[...S.messages],
      uploaded:[...uploaded],
      toolCalls:[],
    });
    if(!Array.isArray(inflight.toolCalls)) inflight.toolCalls=[];
    if(!Array.isArray(inflight.messages)) inflight.messages=[...(inflight.messages||[])];

    const explicitTid=String(d&&d.tid||d&&d.id||d&&d.tool_call_id||d&&d.tool_use_id||d&&d.call_id||'').trim();
    const isComplete=phase==='complete';
    let signature=_toolCallSignature(d,current.burstId,current.segmentSeq);
    let index=-1;

    if(explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        tid:explicitTid,
        allowDone:isComplete,
      });
    }
    if(index<0){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        signature,
        name,
        activityBurstId:current.burstId,
        activitySegmentSeq:current.segmentSeq,
        allowDone:isComplete,
      });
    }
    if(index<0 && isComplete && !explicitTid){
      index=_findPendingLiveToolCallIndex(inflight.toolCalls,{
        name,
        activityBurstId:current.burstId,
        allowDone:true,
      });
    }

    let tc=null;
    if(index>=0&&inflight.toolCalls[index]){
      tc=inflight.toolCalls[index];
    }

    if(!tc){
      tc={
        name,
        preview:String(d.preview||''),
        args:d.args||{},
        snippet:'',
        done:isComplete,
        tid:explicitTid||_liveToolTid(d,current.burstId,current.segmentSeq),
        activityBurstId:current.burstId,
        activitySegmentSeq:_coerceLiveToolCallSeq(current.segmentSeq),
      };
      if(!isComplete){
        tc.started_at=Date.now()/1000;
      }
      if(isComplete) tc._createdByComplete=true;
      inflight.toolCalls.push(tc);
      if(!signature){
        signature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    } else {
      if(!tc.name) tc.name=name;
      if(!tc._liveToolCallSignature){
        tc._liveToolCallSignature=_toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
      }
    }

    if(isComplete){
      if(d.preview){
        tc.snippet=tc.snippet||String(d.preview||'');
        if(!tc.preview) tc.preview=String(d.preview||'');
      }
    } else {
      tc.preview=String(d.preview||tc.preview||'');
    }
    if(d.args!==undefined) tc.args=d.args;
    if(d.snippet!==undefined) tc.snippet=d.snippet;
    tc._liveToolCallSignature = _toolCallSignature(tc,tc.activityBurstId,tc.activitySegmentSeq);
    tc.activityBurstId = Number.isFinite(Number(tc.activityBurstId))
      ? Number(tc.activityBurstId)
      : current.burstId;

    const currentSegmentSeq=_coerceLiveToolCallSeq(current.segmentSeq);
    const startSeq=_coerceLiveToolCallSeq(tc._toolCallStartSeq);
    const inferredSeq=_coerceLiveToolCallSeq(tc.activitySegmentSeq);
    if(!isComplete){
      if(inferredSeq===undefined && currentSegmentSeq!==undefined){
        tc.activitySegmentSeq=currentSegmentSeq;
      } else if(inferredSeq!==undefined){
        tc.activitySegmentSeq=inferredSeq;
      }
      tc._toolCallStartSeq=tc.activitySegmentSeq;
    } else if(startSeq!==undefined){
      tc.activitySegmentSeq=startSeq;
    } else if(inferredSeq!==undefined){
      tc.activitySegmentSeq=inferredSeq;
    }

    if(isComplete){
      tc.done=true;
      if(typeof d.is_error==='boolean') tc.is_error=d.is_error;
      if(d.duration!==undefined) tc.duration=d.duration;
      if(tc.started_at===undefined||tc.started_at===null) tc.started_at=Date.now()/1000;
      if(!tc.tid) tc.tid=explicitTid||_liveToolTid(d,tc.activityBurstId,tc.activitySegmentSeq);
    } else {
      tc.done=false;
      tc.started_at=tc.started_at||Date.now()/1000;
    }

    S.toolCalls=inflight.toolCalls;
    persistInflightState();
    return tc;
  }

  return Object.freeze({
    upsert: upsertLiveToolCall,
  });
}

Object.assign(HermesMessages, {
  createStreamLiveToolTracker,
});
