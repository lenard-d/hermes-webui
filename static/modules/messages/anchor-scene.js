// This module owns settled assistant-turn scene
// projection and persistence; stream.js supplies one turn's identities and
// observations through createStreamAnchorSceneSettlement().
export function createStreamAnchorSceneSettlement(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const _anchorRegistry=options.anchorRegistry||null;
  const _projectLiveAnchorActivityScene=typeof options.projectLiveScene==='function'
    ? options.projectLiveScene
    : ()=>null;
  if(typeof options.activeMode!=='function'||typeof options.rowDisplayHintForMode!=='function'){
    throw new TypeError('stream anchor scene settlement requires display-mode adapters');
  }
  const _anchorSceneActiveMode=options.activeMode;
  const _anchorSceneRowDisplayHintForMode=options.rowDisplayHintForMode;
  const api=options.request;

  function _anchorSceneMessageRef(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    const payload={
      role:String(message.role||''),
      content:String(content||'').replace(/\s+/g,' ').trim(),
      timestamp:message._ts||message.timestamp||'',
    };
    return JSON.stringify(payload);
  }
  function _anchorSceneMessageText(message){
    if(!message||typeof message!=='object') return '';
    let content=message.content||'';
    if(Array.isArray(content)){
      try{
        content=content.map(part=>{
          if(part&&typeof part==='object') return part.text||part.content||part.input_text||'';
          return String(part||'');
        }).join('\n');
      }catch(_){ content=''; }
    }
    return typeof content==='string'?content:String(content||'');
  }
  function _anchorSceneContentText(part){
    if(part===undefined||part===null) return '';
    if(typeof part==='string') return part;
    if(typeof part!=='object') return String(part||'');
    return String(part.text||part.content||part.input_text||part.output_text||part.thinking||part.reasoning||part.summary||'');
  }
  function _anchorSceneContentVisibleText(part){
    if(part===undefined||part===null) return '';
    if(typeof part==='string') return part;
    if(typeof part!=='object') return String(part||'');
    const partType=String(part.type||'');
    if(partType==='thinking'||partType==='reasoning') return '';
    const contentText=(partType==='text'||partType==='input_text'||partType==='output_text')?part.content:'';
    return String(part.text||part.input_text||part.output_text||contentText||'');
  }
  function _anchorSceneMessageHasContentToolUse(message){
    return !!(message&&Array.isArray(message.content)&&message.content.some(part=>part&&typeof part==='object'&&part.type==='tool_use'));
  }
  function _anchorSceneFinalAnswerText(message){
    if(!_anchorSceneMessageHasContentToolUse(message)) return _anchorSceneMessageText(message);
    const content=Array.isArray(message.content)?message.content:[];
    let lastToolIndex=-1;
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(part&&typeof part==='object'&&part.type==='tool_use') lastToolIndex=i;
    }
    const tailText=content.slice(lastToolIndex+1)
      .map(part=>_anchorSceneContentVisibleText(part))
      .filter(text=>_anchorSceneCleanText(text))
      .join('\n');
    return _anchorSceneCleanText(tailText)?tailText:'';
  }
  function _anchorSceneCleanText(value){
    return String(value||'').replace(/\s+/g,' ').trim();
  }
  function _anchorSceneTextKey(value){
    return _anchorSceneCleanText(value).toLowerCase();
  }
  function _anchorSceneSafePayload(value){
    if(value===undefined) return undefined;
    if(value===null||typeof value!=='object') return value;
    try{
      return JSON.parse(JSON.stringify(value));
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneToolId(tool){
    return String(tool&&(tool.tid||tool.id||tool.tool_call_id||tool.tool_use_id||tool.call_id)||'').trim();
  }
  function _anchorSceneToolName(tool){
    const fn=tool&&tool.function&&typeof tool.function==='object'?tool.function:{};
    return String(tool&&(tool.name||tool.tool_name)||fn.name||'tool').trim()||'tool';
  }
  function _anchorSceneToolArgs(tool){
    if(!tool||typeof tool!=='object') return {};
    if(tool.args&&typeof tool.args==='object') return _anchorSceneSafePayload(tool.args)||{};
    if(tool.input&&typeof tool.input==='object') return _anchorSceneSafePayload(tool.input)||{};
    const fn=tool.function&&typeof tool.function==='object'?tool.function:{};
    if(typeof fn.arguments==='string'&&fn.arguments.trim()){
      try{
        const parsed=JSON.parse(fn.arguments);
        return parsed&&typeof parsed==='object'?_anchorSceneSafePayload(parsed):{};
      }catch(_){}
    }
    return {};
  }
  function _anchorSceneContentTool(part){
    if(!part||typeof part!=='object') return {};
    const fn=part.function&&typeof part.function==='object'?part.function:{};
    return {
      id:part.id||part.tid||part.tool_call_id||part.tool_use_id||part.call_id,
      tid:part.tid||part.id||part.tool_call_id||part.tool_use_id||part.call_id,
      tool_call_id:part.tool_call_id,
      tool_use_id:part.tool_use_id,
      call_id:part.call_id,
      name:part.name||part.tool_name||fn.name||'tool',
      tool_name:part.tool_name,
      args:part.args,
      input:part.input,
      function:part.function,
      command:part.command||part.raw_command||part.original_command||part.display_command,
      preview:part.preview||part.summary,
      snippet:part.snippet||part.result||part.output,
      result:part.result,
      output:part.output,
      is_error:part.is_error,
      error:part.error,
      duration:part.duration,
      started_at:part.started_at,
    };
  }
  function _anchorSceneStringPayload(value){
    if(value===undefined||value===null) return '';
    if(typeof value==='string') return value;
    try{
      return JSON.stringify(value);
    }catch(_){
      return String(value);
    }
  }
  function _anchorSceneRowBase(role, kind, sourceEventType, orderIndex, messageIndex){
    const groupKey=Number.isFinite(Number(messageIndex))?`assistant:${Number(messageIndex)}`:`activity:${orderIndex}`;
    return {
      row_id:`settled:${activeSid||'session'}:${streamId||'stream'}:${role}:${messageIndex}:${orderIndex}`,
      order_index:orderIndex,
      kind,
      role,
      display_hint:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
      display_hints:{
        compact_worklog:role==='prose'?'main_prose':role==='thinking'?'collapsed_thinking':role==='tool'?'tool_row':role==='terminal'?'terminal_status_row':'activity_row',
        transparent_stream:'chronological_activity',
      },
      source_event_type:sourceEventType,
      event_id:null,
      local_id:null,
      run_id:null,
      stream_id:streamId||null,
      seq:orderIndex,
      status:role==='terminal'?'completed':'completed',
      created_at:null,
      identity:{event_id:null,local_id:null,run_id:null,stream_id:streamId||null,seq:orderIndex},
      group:{
        group_key:groupKey,
        activity_burst_id:null,
        activity_segment_seq:null,
        assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null,
      },
      text:'',
      thinking:null,
      tool_call_id:null,
      tool:null,
      payload:{assistant_msg_idx:Number.isFinite(Number(messageIndex))?Number(messageIndex):null},
    };
  }
  function _anchorSceneProseRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('prose','process_prose','settled_message',orderIndex,messageIndex);
    row.text=String(text||'');
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneThinkingRow(text, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('thinking','reasoning','reasoning',orderIndex,messageIndex);
    row.text=String(text||'');
    const preview=_anchorSceneCleanText(text);
    row.thinking={
      text:row.text,
      preview:preview.length>180?`${preview.slice(0,177)}...`:preview,
      dedupe_key:preview?`thinking:${preview.toLowerCase()}`:'',
    };
    row.payload={...row.payload,text:row.text};
    return row;
  }
  function _anchorSceneToolRowFromCall(tool, orderIndex, messageIndex){
    const row=_anchorSceneRowBase('tool','tool_completed','tool_complete',orderIndex,messageIndex);
    const tid=_anchorSceneToolId(tool);
    const name=_anchorSceneToolName(tool);
    const args=_anchorSceneToolArgs(tool);
    const command=_anchorSceneStringPayload(tool&&(tool.command||tool.raw_command||tool.original_command||tool.display_command))||_anchorSceneStringPayload(args&&(args.cmd||args.command));
    const preview=_anchorSceneStringPayload(tool&&(tool.preview||tool.summary));
    const snippet=_anchorSceneStringPayload(tool&&(tool.snippet||tool.result||tool.output));
    const isError=!!(tool&&(tool.is_error||tool.error));
    row.row_id=tid?`settled:${activeSid||'session'}:${streamId||'stream'}:tool:${tid}`:row.row_id;
    row.tool_call_id=tid||null;
    row.tool={
      id:tid||null,
      name,
      args,
      command,
      preview,
      snippet,
      result:_anchorSceneSafePayload(tool&&tool.result)??null,
      output:_anchorSceneSafePayload(tool&&tool.output)??null,
      done:true,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:null,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:null,
      signature:[name,tid||'',JSON.stringify(args||{})].join('|'),
    };
    row.payload={
      ...row.payload,
      tid:tid||undefined,
      id:tid||undefined,
      name,
      args,
      command,
      preview,
      snippet,
      is_error:isError,
      duration:tool&&tool.duration!==undefined?tool.duration:undefined,
      started_at:tool&&tool.started_at!==undefined?tool.started_at:undefined,
    };
    return row;
  }
  function _anchorSceneToolRowName(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    return String(tool.name||payload.name||'tool').trim().toLowerCase();
  }
  function _anchorSceneToolRowId(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    return String(
      (row&&row.tool_call_id)||
      tool.id||
      tool.tid||
      tool.tool_call_id||
      tool.tool_use_id||
      tool.call_id||
      payload.tid||
      payload.id||
      ''
    ).trim();
  }
  function _anchorSceneToolRowsHaveNonConflictingIds(existing, incoming){
    const existingId=_anchorSceneToolRowId(existing);
    const incomingId=_anchorSceneToolRowId(incoming);
    return !existingId||!incomingId||existingId===incomingId;
  }
  function _anchorSceneToolRowsHaveDifferentExplicitIds(existing, incoming){
    const existingId=_anchorSceneToolRowId(existing);
    const incomingId=_anchorSceneToolRowId(incoming);
    return !!existingId&&!!incomingId&&existingId!==incomingId;
  }
  function _anchorSceneToolRowStartedAt(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const value=tool.started_at!==undefined&&tool.started_at!==null&&tool.started_at!==''?tool.started_at:payload.started_at;
    return value!==undefined&&value!==null&&value!==''?String(value):'';
  }
  function _anchorSceneToolRowsHaveSameStartedAt(existing, incoming){
    const existingStartedAt=_anchorSceneToolRowStartedAt(existing);
    const incomingStartedAt=_anchorSceneToolRowStartedAt(incoming);
    return !!existingStartedAt&&!!incomingStartedAt&&existingStartedAt===incomingStartedAt;
  }
  function _anchorSceneToolRowBodyText(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    for(const value of [tool.snippet,payload.snippet,tool.output,payload.output,tool.result,payload.result,tool.preview,payload.preview]){
      const text=_anchorSceneStringPayload(value).trim();
      if(text) return text;
    }
    return '';
  }
  function _anchorSceneToolRowsHaveCompatibleBody(existing, incoming){
    const existingBody=_anchorSceneToolRowBodyText(existing);
    const incomingBody=_anchorSceneToolRowBodyText(incoming);
    return !!existingBody&&!!incomingBody&&(
      existingBody===incomingBody||
      existingBody.startsWith(incomingBody)||
      incomingBody.startsWith(existingBody)
    );
  }
  function _anchorSceneToolRowsHaveCompatibleNames(existing, incoming){
    const existingName=_anchorSceneToolRowName(existing);
    const incomingName=_anchorSceneToolRowName(incoming);
    return !existingName||!incomingName||existingName==='tool'||incomingName==='tool'||existingName===incomingName;
  }
  function _anchorSceneToolRowArgs(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const args=(tool.args&&typeof tool.args==='object'&&!Array.isArray(tool.args))?tool.args:payload.args;
    return args&&typeof args==='object'&&!Array.isArray(args)?args:null;
  }
  function _anchorSceneObjectContainsSubset(base, subset){
    if(!base||!subset||typeof base!=='object'||typeof subset!=='object') return false;
    const stableStringify=(candidate)=>{
      const normalize=(value)=>{
        if(!value||typeof value!=='object') return value;
        if(Array.isArray(value)) return value.map(normalize);
        const normalized={};
        Object.keys(value).sort().forEach((key)=>{normalized[key]=normalize(value[key]);});
        return normalized;
      };
      try{return JSON.stringify(normalize(candidate));}catch(_){return JSON.stringify(candidate);}
    };
    for(const [key,value] of Object.entries(subset)){
      if(!Object.prototype.hasOwnProperty.call(base,key)) return false;
      if(stableStringify(base[key])!==stableStringify(value)) return false;
    }
    return true;
  }
  function _anchorSceneToolRowsHaveCompatibleInvocation(existing, incoming){
    const existingTool=existing&&existing.tool&&typeof existing.tool==='object'?existing.tool:{};
    const incomingTool=incoming&&incoming.tool&&typeof incoming.tool==='object'?incoming.tool:{};
    const existingPayload=existing&&existing.payload&&typeof existing.payload==='object'?existing.payload:{};
    const incomingPayload=incoming&&incoming.payload&&typeof incoming.payload==='object'?incoming.payload:{};
    const existingCommand=_anchorSceneStringPayload(existingTool.command||existingPayload.command).trim();
    const incomingCommand=_anchorSceneStringPayload(incomingTool.command||incomingPayload.command).trim();
    if(existingCommand&&incomingCommand) return existingCommand===incomingCommand;
    const existingArgs=_anchorSceneToolRowArgs(existing);
    const incomingArgs=_anchorSceneToolRowArgs(incoming);
    if(!existingArgs||!incomingArgs||!Object.keys(existingArgs).length||!Object.keys(incomingArgs).length) return false;
    return _anchorSceneObjectContainsSubset(existingArgs,incomingArgs)||_anchorSceneObjectContainsSubset(incomingArgs,existingArgs);
  }
  function _anchorSceneToolRowHasInvocationEvidence(row){
    const tool=row&&row.tool&&typeof row.tool==='object'?row.tool:{};
    const payload=row&&row.payload&&typeof row.payload==='object'?row.payload:{};
    const command=_anchorSceneStringPayload(tool.command||payload.command).trim();
    const args=_anchorSceneToolRowArgs(row);
    return !!command||!!(args&&Object.keys(args).length);
  }
  function _anchorSceneToolRowsCanNameMatch(existing, incoming){
    if(!_anchorSceneToolRowsHaveCompatibleNames(existing,incoming)) return false;
    if(_anchorSceneToolRowHasInvocationEvidence(existing)&&_anchorSceneToolRowHasInvocationEvidence(incoming)){
      return _anchorSceneToolRowsHaveCompatibleInvocation(existing,incoming);
    }
    return true;
  }
  function _anchorSceneMatchingContentToolRow(contentToolRows, incomingRow, ordinal, usedRows, incomingTotal, idFlexibleRows){
    if(!Array.isArray(contentToolRows)||!incomingRow) return null;
    const incomingTid=incomingRow.tool_call_id||(incomingRow.tool&&incomingRow.tool.id);
    for(const row of contentToolRows){
      if(!row||usedRows.has(row)) continue;
      const tid=row.tool_call_id||(row.tool&&row.tool.id);
      if(tid&&incomingTid&&tid===incomingTid) return row;
    }
    if(contentToolRows.length===1&&Number(incomingTotal)===1){
      const onlyRow=contentToolRows[0];
      if(onlyRow&&!usedRows.has(onlyRow)&&_anchorSceneToolRowsCanNameMatch(onlyRow,incomingRow)) return onlyRow;
    }
    const availableRows=contentToolRows.filter(row=>row&&!usedRows.has(row));
    if(availableRows.length===1){
      if(Number(incomingTotal)===1&&_anchorSceneToolRowsCanNameMatch(availableRows[0],incomingRow)) return availableRows[0];
      if(
        _anchorSceneToolRowsHaveCompatibleNames(availableRows[0],incomingRow)&&
        _anchorSceneToolRowsHaveCompatibleInvocation(availableRows[0],incomingRow)
      ) return availableRows[0];
    }
    const reusableRows=contentToolRows.filter(row=>row&&usedRows.has(row));
    if(
      reusableRows.length===1&&
      Number(incomingTotal)===1&&
      (
        (
          _anchorSceneToolRowId(reusableRows[0])&&
          _anchorSceneToolRowId(incomingRow)&&
          _anchorSceneToolRowId(reusableRows[0])===_anchorSceneToolRowId(incomingRow)
        )||
        (
          idFlexibleRows&&
          idFlexibleRows.has(reusableRows[0])&&
          _anchorSceneToolRowsHaveSameStartedAt(reusableRows[0],incomingRow)&&
          _anchorSceneToolRowsHaveCompatibleBody(reusableRows[0],incomingRow)
        )
      )&&
      _anchorSceneToolRowsHaveCompatibleNames(reusableRows[0],incomingRow)&&
      _anchorSceneToolRowsHaveCompatibleInvocation(reusableRows[0],incomingRow)
    ) return reusableRows[0];
    for(const row of contentToolRows){
      if(!row||usedRows.has(row)) continue;
      const tid=row.tool_call_id||(row.tool&&row.tool.id);
      if(!tid&&!incomingTid&&_anchorSceneToolRowsCanNameMatch(row,incomingRow)) return row;
    }
    return null;
  }
  function _anchorSceneMessageReasoningText(message){
    if(!message||typeof message!=='object') return '';
    return String(message.reasoning||message._reasoning||message.reasoning_content||message.thinking||'');
  }
  function _anchorSceneRowsFromContentParts(message, messageIndex, options){
    if(!_anchorSceneMessageHasContentToolUse(message)) return null;
    options=(options&&typeof options==='object')?options:{};
    const isFinalMessage=!!options.isFinalMessage;
    const rows=[];
    const content=Array.isArray(message.content)?message.content:[];
    let lastToolIndex=-1;
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(part&&typeof part==='object'&&part.type==='tool_use') lastToolIndex=i;
    }
    for(let i=0;i<content.length;i+=1){
      const part=content[i];
      if(!part||typeof part!=='object'){
        if(isFinalMessage&&i>lastToolIndex) continue;
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneProseRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='text'||part.type==='input_text'||part.type==='output_text'){
        if(isFinalMessage&&i>lastToolIndex&&_anchorSceneContentVisibleText(part)) continue;
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneProseRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='thinking'||part.type==='reasoning'){
        const text=_anchorSceneContentText(part);
        if(_anchorSceneCleanText(text)) rows.push(_anchorSceneThinkingRow(text,rows.length,messageIndex));
        continue;
      }
      if(part.type==='tool_use'){
        rows.push(_anchorSceneToolRowFromCall(_anchorSceneContentTool(part),rows.length,messageIndex));
      }
    }
    return rows;
  }
  // #4622: a settled tool row built from messages[].tool_calls (state.db/sidecar)
  // can lack the result body — terminal stdout, or the diff/output that a
  // patch/edit card renders — because the persisted row carries only a short
  // preview (or, on a cold/paginated load, nothing). The full body lives on the
  // live S.toolCalls entry at settle time. When a settled row and a live call
  // match by tool id, restore the missing body fields from the live call onto
  // the settled row's tool+payload (only when the settled value is empty — never
  // clobber a genuine persisted body), so the rebuilt card shows full output +
  // the Show-more expander + the rendered diff. Returns true if it enriched.
  function _enrichSettledToolRowBodyFromLive(row, live){
    if(!row||typeof row!=='object'||!live||typeof live!=='object') return false;
    const tool=(row.tool&&typeof row.tool==='object')?row.tool:(row.tool={});
    const payload=(row.payload&&typeof row.payload==='object')?row.payload:(row.payload={});
    let enriched=false;
    const _empty=v=>v===undefined||v===null||v==='';
    // Result body: _anchorSceneToolCallFromRow renders tool.snippet||payload.snippet
    // (||payload.result||payload.output) as the card output + diff source, so
    // restore the snippet onto both tool+payload when the settled row has none.
    const liveSnippet=_anchorSceneStringPayload(live.snippet||live.result||live.output);
    // Restore the live body when the settled snippet is missing OR is a bounded
    // preview of the live one. The backend persists a capped preview
    // (_TOOL_RESULT_SNIPPET_MAX = 4000 chars in api/streaming.py), so a long
    // terminal/tool output settles to that 4000-char prefix, not to empty —
    // #4622's actual symptom. Treat a settled snippet as restorable when the
    // live snippet is strictly longer AND the settled value is a prefix of it
    // AND the settled value is at/over the persistence cap (i.e. it's a
    // truncated preview, not a genuinely short real value we must not clobber).
    const _SETTLED_SNIPPET_CAP=4000;
    const _isBoundedPreview=(settled,full)=>(
      typeof settled==='string'&&typeof full==='string'&&
      full.length>settled.length&&settled.length>=_SETTLED_SNIPPET_CAP&&
      full.startsWith(settled)
    );
    const _settledSnippet=(!_empty(tool.snippet)?tool.snippet:(!_empty(payload.snippet)?payload.snippet:''));
    const _snippetRestorable=(_empty(tool.snippet)&&_empty(payload.snippet))||_isBoundedPreview(_settledSnippet,liveSnippet);
    if(liveSnippet&&_snippetRestorable){
      tool.snippet=liveSnippet; payload.snippet=liveSnippet; enriched=true;
    }
    // Command (shell detail-lead) + args (diff/input reconstruction, the "Full" tab).
    const liveCommand=_anchorSceneStringPayload(live.command||live.raw_command);
    if(liveCommand&&_empty(tool.command)&&_empty(payload.command)){
      tool.command=liveCommand; payload.command=liveCommand; enriched=true;
    }
    if(!_empty(live.started_at)&&_empty(tool.started_at)&&_empty(payload.started_at)){
      tool.started_at=live.started_at; payload.started_at=live.started_at; enriched=true;
    }
    const liveArgs=_anchorSceneToolArgs(live);
    if(liveArgs&&typeof liveArgs==='object'&&Object.keys(liveArgs).length){
      const mergeMissingArgs=(existing)=>{
        const base=(existing&&typeof existing==='object'&&!Array.isArray(existing))?{...existing}:{};
        let changed=!(existing&&typeof existing==='object'&&!Array.isArray(existing));
        for(const [key,value] of Object.entries(liveArgs)){
          if(!Object.prototype.hasOwnProperty.call(base,key)){
            base[key]=value;
            changed=true;
          }
        }
        return changed?base:existing;
      };
      const nextToolArgs=mergeMissingArgs(tool.args);
      const nextPayloadArgs=mergeMissingArgs(payload.args);
      if(nextToolArgs!==tool.args){ tool.args=nextToolArgs; enriched=true; }
      if(nextPayloadArgs!==payload.args){ payload.args=nextPayloadArgs; enriched=true; }
    }
    return enriched;
  }
  function _anchorSceneRowsByMessageIndex(messages, turnStart, lastAsstIndex, options){
    options=(options&&typeof options==='object')?options:{};
    const byIdx=new Map();
    const add=(idx,row)=>{
      if(!byIdx.has(idx)) byIdx.set(idx,[]);
      byIdx.get(idx).push(row);
    };
    // Pre-index S.toolCalls by assistant_msg_idx for O(m+n) lookup
    const toolsByIdx=new Map();
    if(S.toolCalls) for(const tc of S.toolCalls){
      const ti=typeof tc.toolIdx==='number'? tc.toolIdx : parseInt(tc.assistant_msg_idx,10);
      if(Number.isFinite(ti)){
        if(!toolsByIdx.has(ti)) toolsByIdx.set(ti,[]);
        toolsByIdx.get(ti).push(tc);
      }
    }
    let encounter=0;
    const endIndex=options&&options.includeFinal?lastAsstIndex+1:lastAsstIndex;
    for(let idx=turnStart+1;idx<endIndex;idx+=1){
      const message=messages[idx];
      if(!message||message.role!=='assistant') continue;
      const pool=[];
      const text=_anchorSceneMessageText(message);
      const contentRows=_anchorSceneRowsFromContentParts(message,idx,{isFinalMessage:idx===lastAsstIndex});
      const hasOrderedContentRows=Array.isArray(contentRows)&&contentRows.length>0;
      const contentToolRows=[];
      const usedContentToolRows=new Set();
      const idFlexibleContentToolRows=new Set();
      const seenToolIds=new Set();
      const rowByToolId=new Map();
      if(hasOrderedContentRows){
        for(const row of contentRows){
          pool.push({...row,_phase:1,_encounter:encounter++,_fromContent:true});
          const tid=row.tool_call_id||(row.tool&&row.tool.id);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
          if(row.role==='tool') contentToolRows.push(row);
        }
      }else if(_anchorSceneCleanText(text)){
        pool.push({..._anchorSceneProseRow(text,0,idx),_phase:2,_encounter:encounter++});
      }
      const reasoning=_anchorSceneMessageReasoningText(message);
      if(_anchorSceneCleanText(reasoning)&&_anchorSceneTextKey(reasoning)!==_anchorSceneTextKey(text)){
        pool.push({..._anchorSceneThinkingRow(reasoning,0,idx),_phase:0,_encounter:encounter++});
      }
      const messageTools=[];
      if(Array.isArray(message.tool_calls)) messageTools.push(...message.tool_calls);
      if(Array.isArray(message._partial_tool_calls)) messageTools.push(...message._partial_tool_calls);
      let messageToolOrdinal=0;
      for(const tool of messageTools){
        const row=_anchorSceneToolRowFromCall(tool,0,idx);
        const tid=row.tool_call_id||(row.tool&&row.tool.id);
        if(tid&&seenToolIds.has(tid)){
          const existing=rowByToolId.get(tid);
          if(existing){
            _enrichSettledToolRowBodyFromLive(existing, tool);
            if(contentToolRows.includes(existing)) usedContentToolRows.add(existing);
          }
          messageToolOrdinal+=1;
          continue;
        }
        const contentMatch=_anchorSceneMatchingContentToolRow(contentToolRows,row,messageToolOrdinal,usedContentToolRows,messageTools.length,idFlexibleContentToolRows);
        if(contentMatch){
          if(_anchorSceneToolRowsHaveDifferentExplicitIds(contentMatch,row)) idFlexibleContentToolRows.add(contentMatch);
          _enrichSettledToolRowBodyFromLive(contentMatch, tool);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,contentMatch); }
          usedContentToolRows.add(contentMatch);
          messageToolOrdinal+=1;
          continue;
        }
        pool.push({...row,_phase:1,_encounter:encounter++});
        if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
        messageToolOrdinal+=1;
      }
      // Merge S.toolCalls for this index, dedup by tool id. When a live call
      // matches a settled row already in the pool, don't just skip it —
      // restore any result body the settled row is missing (#4622): the live
      // S.toolCalls entry carries the full terminal output / patch diff that the
      // persisted state.db row may have dropped to a short preview or nothing.
      let liveToolOrdinal=0;
      for(const tool of (toolsByIdx.get(idx)||[])){
        if(!tool||typeof tool!=='object') continue;
        const toolIdx=Number(tool.assistant_msg_idx);
        if(!Number.isFinite(toolIdx)||toolIdx!==idx) continue;
        const row=_anchorSceneToolRowFromCall(tool,0,idx);
        const tid=row.tool_call_id||(row.tool&&row.tool.id);
        if(tid&&seenToolIds.has(tid)){
          const existing=rowByToolId.get(tid);
          if(existing){
            _enrichSettledToolRowBodyFromLive(existing, tool);
            if(contentToolRows.includes(existing)) usedContentToolRows.add(existing);
          }
          liveToolOrdinal+=1;
          continue;
        }
        const liveTools=toolsByIdx.get(idx)||[];
        const contentMatch=_anchorSceneMatchingContentToolRow(contentToolRows,row,liveToolOrdinal,usedContentToolRows,liveTools.length,idFlexibleContentToolRows);
        if(contentMatch){
          if(_anchorSceneToolRowsHaveDifferentExplicitIds(contentMatch,row)) idFlexibleContentToolRows.add(contentMatch);
          _enrichSettledToolRowBodyFromLive(contentMatch, tool);
          if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,contentMatch); }
          usedContentToolRows.add(contentMatch);
          liveToolOrdinal+=1;
          continue;
        }
        if(tid){ seenToolIds.add(tid); rowByToolId.set(tid,row); }
        pool.push({...row,_phase:1,_encounter:encounter++});
        liveToolOrdinal+=1;
      }
      // Stable sort by (phase, started_at, encounter). Once a message has an
      // ordered content[] scene, preserve that content bucket order exactly.
      const useStartedAt=!hasOrderedContentRows;
      pool.sort((a,b)=>{
        if(a._phase!==b._phase) return a._phase-b._phase;
        if(useStartedAt){
          const aTime=(a.tool&&a.tool.started_at!=null)?a.tool.started_at:Infinity;
          const bTime=(b.tool&&b.tool.started_at!=null)?b.tool.started_at:Infinity;
          if(aTime!==bTime) return aTime-bTime;
        }
        return a._encounter-b._encounter;
      });
      // Emit with sequential order_index values, strip temp props.
      // Rows were built with orderIndex=0, so their row_id/seq still encode 0.
      // Rewrite order_index AND regenerate the index-derived identity fields
      // (row_id/seq) from the final per-bucket position, so two anonymous rows
      // (no tool id) at the same message index don't collide on the same row_id
      // and get silently deduped by _completeSettledAnchorSceneForTurn().
      for(const row of pool){
        const {_phase,_encounter,_fromContent,...clean}=row;
        const oi=byIdx.has(idx)?byIdx.get(idx).length:0;
        clean.order_index=oi;
        clean.seq=oi;
        if(clean.identity&&typeof clean.identity==='object') clean.identity={...clean.identity,seq:oi};
        // Tool rows with a tool id carry a tid-based row_id (already unique) —
        // only regenerate the default index-based row_id form.
        const indexRowId=`settled:${activeSid||'session'}:${streamId||'stream'}:${clean.role}:${idx}:0`;
        if(clean.row_id===indexRowId){
          clean.row_id=`settled:${activeSid||'session'}:${streamId||'stream'}:${clean.role}:${idx}:${oi}`;
        }
        add(idx,clean);
      }
    }
    return byIdx;
  }
  function _anchorSceneExistingRowKey(row){
    if(!row||typeof row!=='object') return '';
    if(row.role==='tool'){
      const tool=row.tool&&typeof row.tool==='object'?row.tool:{};
      return `tool:${row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id||row.row_id||''}`;
    }
    if(row.role==='prose'||row.role==='thinking') return `${row.role}:${_anchorSceneTextKey(row.text)}`;
    return `${row.role||row.kind}:${row.source_event_type||''}:${row.status||''}:${row.row_id||''}`;
  }
  function _anchorSceneRowHasLiveIdentity(row){
    if(!row||typeof row!=='object') return false;
    const identity=row.identity&&typeof row.identity==='object'?row.identity:{};
    const values=[row.row_id,row.local_id,row.event_id,identity.local_id,identity.event_id];
    return values.some(value=>String(value||'').startsWith('live-'));
  }
  function _anchorSceneMessageRowsHaveThinking(messageRows){
    if(!(messageRows instanceof Map)) return false;
    for(const bucket of messageRows.values()){
      if(Array.isArray(bucket)&&bucket.some(row=>row&&row.role==='thinking')) return true;
    }
    return false;
  }
  function _anchorSceneSettleLiveRunningRow(row, hasSettledThinking){
    if(!row||typeof row!=='object') return row;
    if(row.role!=='thinking'&&row.role!=='prose'&&row.role!=='tool') return row;
    if(String(row.status||'').toLowerCase()!=='running') return row;
    if(!_anchorSceneRowHasLiveIdentity(row)) return row;
    if(row.role==='thinking'&&hasSettledThinking) return null;
    return {...row,status:'completed'};
  }
  function _anchorSceneRowLooksLikeFinalAnswer(rowTextKey, finalKey){
    if(!rowTextKey||!finalKey) return false;
    if(rowTextKey===finalKey) return true;
    // #4587: align with the renderer's _anchorSceneProseMatchesFinalAnswer — a
    // prefix-like overlap only counts as "the final answer" (and is dropped from
    // the scene) when it's a NEAR-complete match (ratio>=0.9). A shorter
    // intermediate-prose row that merely happens to be a prefix of the final
    // answer is legitimate progress narration and must be PRESERVED, not dropped.
    if(!(finalKey.startsWith(rowTextKey)||rowTextKey.startsWith(finalKey))) return false;
    const shorter=Math.min(rowTextKey.length,finalKey.length);
    const longer=Math.max(rowTextKey.length,finalKey.length);
    return shorter>=80&&longer>0&&(shorter/longer)>=0.9;
  }
  function _anchorSceneRowTextOverlapsExisting(rowTextKey, seenTextKeys){
    if(!rowTextKey||!Array.isArray(seenTextKeys)) return false;
    for(const existing of seenTextKeys){
      if(!existing) continue;
      if(rowTextKey===existing) return true;
      const minLen=Math.min(rowTextKey.length,existing.length);
      if(minLen>=80&&(rowTextKey.includes(existing)||existing.includes(rowTextKey))) return true;
    }
    return false;
  }
  function _anchorSceneTurnDurationForSettlement(lastAsst, base){
    if(lastAsst&&lastAsst._turnDuration!==undefined&&lastAsst._turnDuration!==null) return lastAsst._turnDuration;
    if(base&&base.turn_duration!==undefined&&base.turn_duration!==null) return base.turn_duration;
    const session=(typeof S!=='undefined'&&S&&S.session)?S.session:null;
    // The `pending_started_at` fallback below is the START of an IN-FLIGHT turn.
    // For a SETTLED turn that recorded no live duration, computing
    // `now - pending_started_at` is wrong: pending_started_at is either stale
    // (left over from an earlier turn / a session that sat idle) or belongs to a
    // different, still-pending turn — which rendered a bogus "Processed 15h 32m"
    // on fresh conversations (#4930). Only use it while a turn is actually in
    // flight; otherwise show no duration rather than a fabricated one.
    const turnInFlight=!!(session&&(session.active_stream_id||session.pending_user_message));
    if(!turnInFlight) return undefined;
    const candidates=[
      session&&session.pending_started_at,
      session&&session.active_started_at,
      session&&session.run_started_at,
      session&&session.started_at,
    ];
    for(const raw of candidates){
      const started=Number(raw);
      if(Number.isFinite(started)&&started>0){
        const elapsed=(Date.now()/1000)-started;
        if(Number.isFinite(elapsed)&&elapsed>=0) return elapsed;
      }
    }
    return undefined;
  }
  function _completeSettledAnchorSceneForTurn(messages, lastAsstIndex, projectedScene){
    if(!Array.isArray(messages)||lastAsstIndex<0) return projectedScene;
    const lastAsst=messages[lastAsstIndex];
    if(!lastAsst||lastAsst.role!=='assistant') return projectedScene;
    let turnStart=-1;
    for(let idx=lastAsstIndex-1;idx>=0;idx-=1){
      if(messages[idx]&&messages[idx].role==='user'){
        turnStart=idx;
        break;
      }
    }
    const base=(projectedScene&&typeof projectedScene==='object')?projectedScene:{};
    const sceneMode=base.mode==='transparent_stream'||base.mode==='hide_all_activity' ? base.mode : _anchorSceneActiveMode();
    const messageFinalAnswer=_anchorSceneFinalAnswerText(lastAsst);
    const finalAnswer=_anchorSceneCleanText(messageFinalAnswer)
      ? messageFinalAnswer
      : (typeof base.final_answer==='string'?base.final_answer:'');
    const finalKey=_anchorSceneTextKey(finalAnswer);
    const messageRows=_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex,{includeFinal:true});
    const hasSettledThinking=_anchorSceneMessageRowsHaveThinking(messageRows);
    const rows=[];
    const seen=new Set();
    const seenTextKeys=[];
    const projectedRows=Array.isArray(base.activity_rows)?base.activity_rows:[];
    const orderedRows=[];
    for(const row of projectedRows){
      if(row&&row.role==='terminal') continue;
      orderedRows.push(row);
    }
    for(let idx=turnStart+1;idx<=lastAsstIndex;idx+=1){
      const bucket=messageRows.get(idx)||[];
      for(const row of bucket) orderedRows.push(row);
    }
    for(const row of projectedRows){
      if(row&&row.role==='terminal') orderedRows.push(row);
    }
    // #5758 gap: final-segment eligibility must be judged against the LIVE
    // projection's own chronology. The settled per-message tool rows appended
    // into orderedRows above re-list tools that ran EARLIER in the turn, so an
    // index over the combined list pushes the "after the last tool row"
    // boundary past the final segment's live-prose accumulator — its stale
    // prefix snapshot then survives into the persisted scene and renders as a
    // duplicate of the answer's beginning. A live-prose row belongs to the
    // final segment iff no PROJECTED tool row follows it; pre-tool narration
    // that happens to prefix the final answer stays protected.
    const lastProjectedToolIndex=projectedRows.reduce((last,row,idx)=>(row&&row.role==='tool')?idx:last,-1);
    const finalSegmentLiveProseRows=new WeakSet();
    projectedRows.forEach((row,idx)=>{
      if(idx>lastProjectedToolIndex&&row&&row.role==='prose'&&row.kind==='process_prose'&&String(row.source_event_type||'')==='token'&&String(row.local_id||'').startsWith('live-prose:')) finalSegmentLiveProseRows.add(row);
    });
    const rowIsLiveTokenFinalPrefix=(row,textKey,finalSegmentEligible)=>finalSegmentEligible&&row&&row.role==='prose'&&row.kind==='process_prose'&&String(row.source_event_type||'')==='token'&&String(row.local_id||'').startsWith('live-prose:')&&textKey&&finalKey&&textKey.length<finalKey.length&&finalKey.startsWith(textKey);
    const pushRow=(row)=>{
      if(!row||typeof row!=='object') return;
      const finalSegmentEligible=finalSegmentLiveProseRows.has(row);
      row=_anchorSceneSettleLiveRunningRow(row,hasSettledThinking);
      if(!row||typeof row!=='object') return;
      const textKey=_anchorSceneTextKey(row.text);
      if(rowIsLiveTokenFinalPrefix(row,textKey,finalSegmentEligible)) return;
      const isTextual=row.role==='prose'||row.role==='thinking';
      if(isTextual&&_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)) return;
      if(isTextual&&_anchorSceneRowTextOverlapsExisting(textKey,seenTextKeys)) return;
      const key=_anchorSceneExistingRowKey(row);
      if(key&&seen.has(key)) return;
      if(key) seen.add(key);
      if(isTextual&&textKey) seenTextKeys.push(textKey);
      rows.push({
        ...row,
        display_hint:_anchorSceneRowDisplayHintForMode(row,sceneMode),
        order_index:rows.length,
        seq:rows.length,
      });
    };
    orderedRows.forEach((row)=>pushRow(row));
    const scene={
      ...base,
      version:'activity_scene_v1',
      mode:sceneMode,
      identity:{
        ...((base.identity&&typeof base.identity==='object')?base.identity:{}),
        source_message_refs:messages.slice(turnStart+1,lastAsstIndex+1)
          .filter(m=>m&&m.role==='assistant')
          .map(m=>_anchorSceneMessageRef(m)),
      },
      lifecycle:(base.lifecycle&&typeof base.lifecycle==='object')?{...base.lifecycle}:{},
      final_answer:_anchorSceneCleanText(finalAnswer)?finalAnswer:'',
      final_message_ref:_anchorSceneMessageRef(lastAsst),
      turn_duration:_anchorSceneTurnDurationForSettlement(lastAsst,base),
      terminal_state:base.terminal_state||((base.lifecycle&&base.lifecycle.terminal_state)||null),
      activity_rows:rows,
    };
    return scene;
  }
  let _persistAnchorSceneWarned=false;
  function _anchorSceneMessageOffsetForPersist(){
    const raw=(typeof options.getOldestMessageIndex==='function')?options.getOldestMessageIndex():0;
    const offset=Number(raw);
    return Number.isFinite(offset)&&offset>0?Math.floor(offset):0;
  }
  function _anchorSceneAbsoluteMessageIndexForPersist(messageIndex, offset){
    const idx=Number(messageIndex);
    const off=Number(offset);
    if(!Number.isFinite(idx)||idx<0) return messageIndex;
    return idx+(Number.isFinite(off)&&off>0?Math.floor(off):0);
  }
  function _persistSettledAnchorScene(message, scene, messageIndex){
    if(!activeSid||!message||!scene||typeof api!=='function') return;
    try{
      const messageOffset=_anchorSceneMessageOffsetForPersist();
      api('/api/session/anchor-scene',{
        method:'POST',
        timeoutMs:8000,
        timeoutToast:false,
        body:JSON.stringify({
          session_id:activeSid,
          stream_id:streamId,
          message_index:_anchorSceneAbsoluteMessageIndexForPersist(messageIndex,messageOffset),
          message_window_index:messageIndex,
          message_offset:messageOffset,
          message_ref:_anchorSceneMessageRef(message),
          scene,
        }),
      }).catch(err=>{
        if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
          _persistAnchorSceneWarned=true;
          console.warn('anchor activity scene persistence failed',err);
        }
      });
    }catch(err){
      if(!_persistAnchorSceneWarned&&typeof console!=='undefined'&&console.warn){
        _persistAnchorSceneWarned=true;
        console.warn('anchor activity scene persistence failed',err);
      }
    }
  }
  function _anchorSceneHasWorklogWorthyRows(scene){
    if(scene&&scene.mode==='hide_all_activity') return false;
    if(typeof window!=='undefined'&&typeof window.isFinalAnswerOnlyMode==='function'&&window.isFinalAnswerOnlyMode()) return false;
    // A worklog (the collapsible "已处理 …" rail) is only meaningful when the turn
    // actually DID worklog-worthy work — a tool call, a thinking/reasoning pass, or
    // a compression lifecycle card. A turn that only streamed prose (e.g. a long
    // plain-text answer, or a degeneration burst that flooded the body with repeated
    // tokens) projects an activity scene whose rows are ALL `prose`/`terminal`. Folding
    // such a turn into a collapsed worklog hides the whole answer and, at STREAM_DONE,
    // shrinks the transcript by the full streamed height → the browser clamps a
    // bottom-pinned viewport back to the top (the "jump back" report). Require at least
    // one genuinely worklog-worthy row before promoting the turn to a worklog.
    const rows=Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[];
    for(const row of rows){
      if(!row||typeof row!=='object') continue;
      const role=String(row.role||'');
      if(role==='tool'||role==='thinking') return true;
      if(role==='lifecycle'){
        const source=String(row.source_event_type||'');
        // compression cards are worklog-worthy; a bare terminal/done lifecycle is not.
        if(source==='compressing'||source==='compressed') return true;
      }
    }
    return false;
  }
  function _attachProjectedSceneToLastAssistant(messages){
    if(!_anchorRegistry||!Array.isArray(messages)) return false;
    let lastAsst=null;
    let lastAsstIndex=-1;
    for(let i=messages.length-1;i>=0;i--){
      const candidate=messages[i];
      if(candidate&&candidate.role==='assistant'){
        lastAsst=candidate;
        lastAsstIndex=i;
        break;
      }
    }
    if(!lastAsst) return false;
    const projectedScene=_projectLiveAnchorActivityScene();
    const scene=_completeSettledAnchorSceneForTurn(messages,lastAsstIndex,projectedScene);
    if(scene&&Array.isArray(scene.activity_rows)&&scene.activity_rows.length){
      const hasWorklogRows=_anchorSceneHasWorklogWorthyRows(scene);
      const shouldPersistScene=hasWorklogRows||scene.mode==='hide_all_activity';
      if(!shouldPersistScene) return false;
      lastAsst._anchor_stream_id=streamId;
      lastAsst._anchor_activity_scene=scene;
      _persistSettledAnchorScene(lastAsst, scene, lastAsstIndex);
      return hasWorklogRows;
    }
    return false;
  }
  return Object.freeze({
    attachProjectedSceneToLastAssistant: _attachProjectedSceneToLastAssistant,
  });
}
