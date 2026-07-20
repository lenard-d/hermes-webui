// Activity-scene projection and renderer reconciliation for Assistant Turn Anchors.
// This factory is intentionally presentation-only: it receives immutable Anchor
// state and never owns registry identity, replay cursors, or settlement writes.
(function registerAssistantTurnAnchorScene(root){
  const parts=root.HermesAssistantTurnAnchorParts||(root.HermesAssistantTurnAnchorParts=Object.create(null));
  if(parts.createScene) throw new Error('assistant turn anchor scene already registered');

  parts.createScene=function createAssistantTurnAnchorScene(support){
    if(!support||typeof support!=='object') throw new Error('assistant turn anchor scene requires model support');
    const {
      hasOwn:_hasOwn,
      own:_own,
      firstOwn:_firstOwn,
      cleanString:_cleanString,
      activityDisplayMode:_activityDisplayMode,
      sanitizePayload:_sanitizePayload,
      copyObject:_copyObject,
      frozenIdentityCopy:_frozenIdentityCopy,
      firstTextValue:_firstTextValue,
      normalizeTerminalState:normalizeAssistantTurnAnchorTerminalState,
    }=support;

    function _anchorFromProjectionInput(input){
      if(!input||typeof input!=='object') return null;
      if(input.anchor&&typeof input.anchor==='object') return input.anchor;
      if(input.identity&&typeof input.identity==='object') return input;
      return null;
    }

    function _activityRowId(event, index){
      const eventId=_cleanString(_own(event,'event_id'));
      if(eventId) return eventId;
      const runId=_cleanString(_own(event,'run_id'));
      const seq=_own(event,'seq');
      if(runId&&seq!==undefined&&seq!==null&&seq!=='') return [runId,String(seq)].join(':');
      const localId=_cleanString(_own(event,'local_id'));
      if(localId){
        const sourceType=_cleanString(_own(event,'source_event_type'))||_cleanString(_own(event,'kind'))||'event';
        return [localId,sourceType,String(index)].join(':');
      }
      return 'activity:'+String(index);
    }

    function _activityRowText(event){
      const payload=_own(event,'payload')||{};
      return _firstTextValue(
        _own(payload,'text'),
        _own(payload,'content'),
        _own(payload,'message'),
        _own(payload,'summary'),
        _own(payload,'result'),
        _own(payload,'output')
      );
    }

    function _isToolActivityKind(kind){
      return kind==='tool_started'||kind==='tool_updated'||kind==='tool_completed';
    }

    function _activityRowToolId(event, kind){
      if(!_isToolActivityKind(kind)) return null;
      const payload=_own(event,'payload')||{};
      return _firstTextValue(
        _own(payload,'tool_call_id'),
        _own(payload,'tool_use_id'),
        _own(payload,'call_id'),
        _own(payload,'tid'),
        _own(payload,'id')
      )||null;
    }

    function _activityPayloadFirst(payload, keys){
      return _firstOwn(payload||{},keys);
    }

    function _activityRowToolDone(kind, status, payload){
      if(payload&&typeof _own(payload,'done')==='boolean') return _own(payload,'done');
      if(kind==='tool_completed') return true;
      if(kind==='tool_started'||kind==='tool_updated') return false;
      if(status==='completed'||status==='error'||status==='failed') return true;
      if(status==='running'||status==='pending') return false;
      return null;
    }

    function _activityRowToolIsError(status, payload){
      if(payload&&typeof _own(payload,'is_error')==='boolean') return _own(payload,'is_error');
      const raw=_cleanString(status).toLowerCase();
      if(raw==='error'||raw==='failed'||raw==='failure') return true;
      return false;
    }

    function _activityRowGroup(event, payload, index){
      const activitySegmentSeq=_activityPayloadFirst(payload,['activitySegmentSeq','activity_segment_seq','segmentSeq','segment_seq']);
      const activityBurstId=_activityPayloadFirst(payload,['activityBurstId','activity_burst_id','burstId','burst_id']);
      const assistantMsgIdx=_activityPayloadFirst(payload,['assistant_msg_idx','assistantMessageIndex','assistant_msg_index']);
      const cleanSegment=activitySegmentSeq!==undefined&&activitySegmentSeq!==null&&String(activitySegmentSeq)!==''
        ? activitySegmentSeq
        : null;
      const cleanBurst=activityBurstId!==undefined&&activityBurstId!==null&&String(activityBurstId)!==''
        ? activityBurstId
        : null;
      const cleanAssistant=assistantMsgIdx!==undefined&&assistantMsgIdx!==null&&String(assistantMsgIdx)!==''
        ? assistantMsgIdx
        : null;
      const fallbackSeq=_own(event,'seq');
      const fallbackKey=fallbackSeq!==undefined&&fallbackSeq!==null&&fallbackSeq!==''?`event:${String(fallbackSeq)}`:`activity:${String(index)}`;
      const groupKey=cleanSegment!==null
        ? `segment:${String(cleanSegment)}`
        : cleanBurst!==null
          ? `burst:${String(cleanBurst)}`
          : cleanAssistant!==null
            ? `assistant:${String(cleanAssistant)}`
            : fallbackKey;
      return Object.freeze({
        group_key:groupKey,
        activity_burst_id:cleanBurst,
        activity_segment_seq:cleanSegment,
        assistant_msg_idx:cleanAssistant,
      });
    }

    function _activityRowThinking(event, kind, text){
      if(kind!=='reasoning') return null;
      const payload=_own(event,'payload')||{};
      const thinkingText=_firstTextValue(
        _own(payload,'thinking'),
        _own(payload,'reasoning'),
        _own(payload,'text'),
        text
      );
      const preview=thinkingText?String(thinkingText).replace(/\s+/g,' ').trim():'';
      return Object.freeze({
        text:thinkingText||'',
        preview:preview.length>180?`${preview.slice(0,177)}...`:preview,
        dedupe_key:preview?`thinking:${preview.toLowerCase()}`:'',
      });
    }

    function _activityRowTool(event, kind, status, text, toolCallId){
      if(!_isToolActivityKind(kind)) return null;
      const payload=_own(event,'payload')||{};
      const toolName=_cleanString(
        _activityPayloadFirst(payload,['name','tool_name','function_name'])||
        (_own(payload,'function')&&_own(_own(payload,'function'),'name'))
      )||'tool';
      const args=_activityPayloadFirst(payload,['args','arguments','input','params']);
      const preview=_firstTextValue(
        _own(payload,'preview'),
        _own(payload,'summary'),
        text
      );
      const snippet=_firstTextValue(
        _own(payload,'snippet'),
        _own(payload,'result'),
        _own(payload,'output')
      );
      const done=_activityRowToolDone(kind,status,payload);
      const isError=_activityRowToolIsError(status,payload);
      const signatureParts=[
        toolName,
        toolCallId||'',
        JSON.stringify(_sanitizePayload(args||{})),
      ];
      return Object.freeze({
        id:toolCallId,
        name:toolName,
        args:_sanitizePayload(args||{}),
        preview:preview||'',
        snippet:snippet||'',
        result:_sanitizePayload(_own(payload,'result'))??null,
        output:_sanitizePayload(_own(payload,'output'))??null,
        done,
        is_error:isError,
        duration:_activityPayloadFirst(payload,['duration','duration_seconds','elapsed'])??null,
        started_at:_activityPayloadFirst(payload,['started_at','startedAt'])??null,
        signature:signatureParts.join('|'),
      });
    }

    function _activityRowRole(kind){
      if(kind==='process_prose') return 'prose';
      if(kind==='reasoning') return 'thinking';
      if(_isToolActivityKind(kind)) return 'tool';
      if(kind==='lifecycle_status') return 'lifecycle';
      if(kind==='control_boundary') return 'control';
      if(kind==='terminal_status') return 'terminal';
      return 'activity';
    }

    function _activityRowDisplayHint(kind, mode){
      if(mode==='transparent_stream') return 'chronological_activity';
      if(kind==='process_prose') return 'main_prose';
      if(kind==='reasoning') return 'collapsed_thinking';
      if(_isToolActivityKind(kind)) return 'tool_row';
      if(kind==='lifecycle_status') return 'quiet_lifecycle_row';
      if(kind==='control_boundary') return 'control_boundary_row';
      if(kind==='terminal_status') return 'terminal_status_row';
      return 'activity_row';
    }

    function _activityRowDisplayHints(kind){
      return Object.freeze({
        compact_worklog:_activityRowDisplayHint(kind,'compact_worklog'),
        transparent_stream:_activityRowDisplayHint(kind,'transparent_stream'),
      });
    }

    function _activitySceneRow(event, index, mode){
      const payload=_own(event,'payload');
      const kind=_cleanString(_own(event,'kind'))||'activity';
      const status=_cleanString(_own(event,'status'))||null;
      const text=_activityRowText(event);
      const toolCallId=_activityRowToolId(event,kind);
      const sanitizedPayload=_sanitizePayload(payload);
      return Object.freeze({
        row_id:_activityRowId(event,index),
        order_index:index,
        kind,
        role:_activityRowRole(kind),
        display_hint:_activityRowDisplayHint(kind,mode),
        display_hints:_activityRowDisplayHints(kind),
        source_event_type:_cleanString(_own(event,'source_event_type'))||null,
        event_id:_cleanString(_own(event,'event_id'))||null,
        local_id:_cleanString(_own(event,'local_id'))||null,
        run_id:_cleanString(_own(event,'run_id'))||null,
        stream_id:_cleanString(_own(event,'stream_id'))||null,
        seq:_own(event,'seq')??null,
        status,
        created_at:_own(event,'created_at')??null,
        identity:Object.freeze({
          event_id:_cleanString(_own(event,'event_id'))||null,
          local_id:_cleanString(_own(event,'local_id'))||null,
          run_id:_cleanString(_own(event,'run_id'))||null,
          stream_id:_cleanString(_own(event,'stream_id'))||null,
          seq:_own(event,'seq')??null,
        }),
        group:_activityRowGroup(event,payload||{},index),
        text,
        thinking:_activityRowThinking(event,kind,text),
        tool_call_id:toolCallId,
        tool:_activityRowTool(event,kind,status,text,toolCallId),
        payload:sanitizedPayload,
      });
    }

    function projectAssistantTurnAnchorActivityScene(input, options){
      const anchor=_anchorFromProjectionInput(input);
      const opts=(options&&typeof options==='object')?options:{};
      const requestedMode=_cleanString(_own(opts,'mode'));
      const mode=_activityDisplayMode(requestedMode);
      if(!anchor){
        return Object.freeze({
          version:'activity_scene_v1',
          mode,
          identity:Object.freeze({source_message_refs:Object.freeze([])}),
          lifecycle:Object.freeze({}),
          final_answer:'',
          final_message_ref:null,
          terminal_state:null,
          activity_rows:Object.freeze([]),
        });
      }
      const rows=(Array.isArray(anchor.activity_events)?anchor.activity_events:[])
        .map((event,index)=>_activitySceneRow(event,index,mode));
      const lifecycle=_copyObject(anchor.lifecycle);
      const content=anchor.content&&typeof anchor.content==='object'?anchor.content:{};
      return Object.freeze({
        version:'activity_scene_v1',
        mode,
        identity:_frozenIdentityCopy(anchor.identity||{}),
        lifecycle:Object.freeze(lifecycle),
        final_answer:typeof content.final_answer==='string'?content.final_answer:'',
        final_message_ref:typeof content.final_message_ref==='string'?content.final_message_ref:null,
        terminal_state:_cleanString(_own(lifecycle,'terminal_state'))||null,
        activity_rows:Object.freeze(rows),
      });
    }

    const ACTIVITY_RECONCILIATION_DEFAULT_FIELDS=Object.freeze([
      'kind',
      'role',
      'source_event_type',
      'status',
      'tool_call_id',
      'tool_name',
      'tool_done',
      'tool_is_error',
    ]);

    function _activityReconciliationInputScene(input, options){
      const opts=(options&&typeof options==='object')?options:{};
      const item=(input&&typeof input==='object')?input:{};
      if(_own(item,'version')==='activity_scene_v1') return item;
      const explicitScene=_own(item,'scene')||_own(item,'activity_scene')||_own(opts,'scene')||_own(opts,'activity_scene');
      if(explicitScene&&typeof explicitScene==='object'&&_own(explicitScene,'version')==='activity_scene_v1'){
        return explicitScene;
      }
      const projectionInput=_own(item,'registry')||_own(item,'anchor_registry')||_own(item,'anchor')||item;
      const requestedMode=_cleanString(_own(item,'mode'))||_cleanString(_own(opts,'mode'));
      return projectAssistantTurnAnchorActivityScene(projectionInput,{mode:requestedMode});
    }

    function _activityReconciliationRendererRows(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      for(const value of [
        _own(item,'renderer_rows'),
        _own(item,'actual_rows'),
        _own(item,'rows'),
        _own(opts,'renderer_rows'),
        _own(opts,'actual_rows'),
        _own(opts,'rows'),
      ]){
        if(Array.isArray(value)) return value;
      }
      return [];
    }

    function _activityReconciliationFields(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      const fields=_own(item,'compare_fields')||_own(item,'fields')||_own(opts,'compare_fields')||_own(opts,'fields');
      const list=Array.isArray(fields)?fields:ACTIVITY_RECONCILIATION_DEFAULT_FIELDS;
      const out=[];
      list.forEach((field)=>{
        const name=_cleanString(field);
        if(name&&out.indexOf(name)===-1) out.push(name);
      });
      return out;
    }

    function _activityReconciliationDataValue(row, keys){
      const item=(row&&typeof row==='object')?row:{};
      for(let i=0;i<keys.length;i+=1){
        const key=keys[i];
        const value=_own(item,key);
        if(value!==undefined&&value!==null&&value!=='') return value;
      }
      const dataset=item.dataset&&typeof item.dataset==='object'?item.dataset:null;
      if(dataset){
        for(let i=0;i<keys.length;i+=1){
          const key=keys[i];
          const camel=key.replace(/_([a-z])/g,(_,ch)=>ch.toUpperCase());
          const value=_own(dataset,camel)||_own(dataset,key);
          if(value!==undefined&&value!==null&&value!=='') return value;
        }
      }
      if(typeof item.getAttribute==='function'){
        for(let i=0;i<keys.length;i+=1){
          const key=keys[i];
          const dataKey=`data-${String(key).replace(/_/g,'-')}`;
          const value=item.getAttribute(dataKey)||item.getAttribute(key);
          if(value!==undefined&&value!==null&&value!=='') return value;
        }
      }
      return undefined;
    }

    function _activityReconciliationBool(value){
      if(typeof value==='boolean') return value;
      if(value===0||value==='0') return false;
      if(value===1||value==='1') return true;
      const raw=_cleanString(value).toLowerCase();
      if(!raw) return null;
      if(raw==='true'||raw==='yes'||raw==='done'||raw==='completed') return true;
      if(raw==='false'||raw==='no'||raw==='running'||raw==='pending') return false;
      return null;
    }

    function _activityReconciliationStatus(value){
      const raw=_cleanString(value).toLowerCase().replace(/[\s-]+/g,'_');
      if(!raw) return null;
      const terminal=normalizeAssistantTurnAnchorTerminalState(raw);
      if(terminal) return terminal;
      if(raw==='complete'||raw==='done') return 'completed';
      if(raw==='failed'||raw==='failure') return 'error';
      return raw;
    }

    function _activityReconciliationText(row){
      const item=(row&&typeof row==='object')?row:{};
      const value=_firstTextValue(
        _activityReconciliationDataValue(item,['text']),
        _activityReconciliationDataValue(item,['preview']),
        _activityReconciliationDataValue(item,['summary']),
        _activityReconciliationDataValue(item,['content'])
      );
      if(value) return value;
      return typeof item.textContent==='string'?item.textContent:'';
    }

    function _activityReconciliationRowSummary(row, index){
      const item=(row&&typeof row==='object')?row:{};
      const tool=item.tool&&typeof item.tool==='object'?item.tool:{};
      const rawKind=_cleanString(_activityReconciliationDataValue(item,['kind','event_type','type']));
      const rawRole=_cleanString(_activityReconciliationDataValue(item,['role']));
      const toolName=_cleanString(
        _activityReconciliationDataValue(item,['tool_name','name'])||
        _activityReconciliationDataValue(tool,['name','tool_name'])
      )||null;
      const toolCallId=_cleanString(
        _activityReconciliationDataValue(item,['tool_call_id','toolCallId','tid','tool_use_id','call_id'])||
        _activityReconciliationDataValue(tool,['id','tool_call_id','toolCallId','tid','tool_use_id','call_id'])
      )||null;
      const toolDone=_activityReconciliationBool(
        _activityReconciliationDataValue(item,['tool_done','toolDone','done']) ??
        _activityReconciliationDataValue(tool,['done'])
      );
      const toolError=_activityReconciliationBool(
        _activityReconciliationDataValue(item,['tool_is_error','toolError','is_error','isError']) ??
        _activityReconciliationDataValue(tool,['is_error','isError'])
      );
      const rowId=_cleanString(_activityReconciliationDataValue(item,['row_id','rowId','event_id','eventId','id']));
      const status=_activityReconciliationStatus(_activityReconciliationDataValue(item,['status','state']));
      return Object.freeze({
        row_id:rowId||null,
        order_index:index,
        declared_order_index:_activityReconciliationDataValue(item,['order_index','orderIndex'])??null,
        kind:rawKind||null,
        role:rawRole||null,
        source_event_type:_cleanString(_activityReconciliationDataValue(item,['source_event_type','sourceEventType','event_type','eventType']))||null,
        display_hint:_cleanString(_activityReconciliationDataValue(item,['display_hint','displayHint']))||null,
        status,
        text:_activityReconciliationText(item),
        tool_call_id:toolCallId,
        tool_name:toolName,
        tool_done:toolDone,
        tool_is_error:toolError,
      });
    }

    function _activityReconciliationRowsById(rows){
      const map=new Map();
      rows.forEach((row)=>{
        if(row&&row.row_id&&!map.has(row.row_id)) map.set(row.row_id,row);
      });
      return map;
    }

    function _activityReconciliationDuplicateRowIds(rows){
      const seen=new Set();
      const dupes=[];
      rows.forEach((row,index)=>{
        const rowId=row&&row.row_id;
        if(!rowId) return;
        if(seen.has(rowId)) dupes.push(Object.freeze({row_id:rowId,index,row}));
        else seen.add(rowId);
      });
      return dupes;
    }

    function _activityReconciliationMismatch(kind, detail){
      return Object.freeze({
        ..._copyObject(detail),
        kind,
      });
    }

    function reconcileAssistantTurnAnchorActivityScene(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      const scene=_activityReconciliationInputScene(item,opts);
      const expectedRows=(Array.isArray(scene.activity_rows)?scene.activity_rows:[])
        .map((row,index)=>_activityReconciliationRowSummary(row,index));
      const actualRows=_activityReconciliationRendererRows(item,opts)
        .map((row,index)=>_activityReconciliationRowSummary(row,index));
      const fields=_activityReconciliationFields(item,opts);
      const mismatches=[];
      if(expectedRows.length!==actualRows.length){
        mismatches.push(_activityReconciliationMismatch('row_count',{
          expected_count:expectedRows.length,
          actual_count:actualRows.length,
        }));
      }
      const expectedById=_activityReconciliationRowsById(expectedRows);
      const actualById=_activityReconciliationRowsById(actualRows);
      const useRowIds=expectedById.size===expectedRows.length&&actualById.size===actualRows.length;
      const matchedActualIds=new Set();
      _activityReconciliationDuplicateRowIds(actualRows).forEach((duplicate)=>{
        mismatches.push(_activityReconciliationMismatch('duplicate_actual_row',duplicate));
      });
      expectedRows.forEach((expected,index)=>{
        const actual=useRowIds&&expected.row_id?actualById.get(expected.row_id):actualRows[index];
        if(!actual){
          mismatches.push(_activityReconciliationMismatch('missing_actual_row',{
            row_id:expected.row_id,
            expected_index:index,
            expected,
          }));
          return;
        }
        if(useRowIds&&actual.row_id) matchedActualIds.add(actual.row_id);
        if(actual.order_index!==expected.order_index){
          mismatches.push(_activityReconciliationMismatch('order_mismatch',{
            row_id:expected.row_id,
            expected_index:expected.order_index,
            actual_index:actual.order_index,
          }));
        }
        fields.forEach((field)=>{
          if(!_hasOwn(expected,field)&&!_hasOwn(actual,field)) return;
          const expectedValue=expected[field]===undefined?null:expected[field];
          const actualValue=actual[field]===undefined?null:actual[field];
          if(JSON.stringify(expectedValue)!==JSON.stringify(actualValue)){
            mismatches.push(_activityReconciliationMismatch('field_mismatch',{
              row_id:expected.row_id,
              field,
              expected:expectedValue,
              actual:actualValue,
            }));
          }
        });
      });
      if(useRowIds){
        actualRows.forEach((actual,index)=>{
          if(actual.row_id&&expectedById.has(actual.row_id)) return;
          if(actual.row_id&&matchedActualIds.has(actual.row_id)) return;
          mismatches.push(_activityReconciliationMismatch('unexpected_actual_row',{
            row_id:actual.row_id,
            actual_index:index,
            actual,
          }));
        });
      }else if(actualRows.length>expectedRows.length){
        actualRows.slice(expectedRows.length).forEach((actual,offset)=>{
          mismatches.push(_activityReconciliationMismatch('unexpected_actual_row',{
            row_id:actual.row_id,
            actual_index:expectedRows.length+offset,
            actual,
          }));
        });
      }
      return Object.freeze({
        version:'activity_scene_reconciliation_v1',
        mode:scene.mode||(_cleanString(_own(item,'mode'))||_cleanString(_own(opts,'mode'))||'compact_worklog'),
        scene_version:scene.version||null,
        matched:mismatches.length===0,
        summary:Object.freeze({
          expected_count:expectedRows.length,
          actual_count:actualRows.length,
          mismatch_count:mismatches.length,
        }),
        identity:scene.identity||Object.freeze({source_message_refs:Object.freeze([])}),
        terminal_state:scene.terminal_state||null,
        fields:Object.freeze(fields),
        expected_rows:Object.freeze(expectedRows),
        actual_rows:Object.freeze(actualRows),
        mismatches:Object.freeze(mismatches),
      });
    }

    function _rendererSnapshotMode(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      const requested=_cleanString(_own(item,'mode'))||_cleanString(_own(opts,'mode'));
      return _activityDisplayMode(requested);
    }

    function _rendererSnapshotRowsInput(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      for(const value of [
        _own(item,'renderer_rows'),
        _own(item,'actual_rows'),
        _own(item,'rows'),
        _own(opts,'renderer_rows'),
        _own(opts,'actual_rows'),
        _own(opts,'rows'),
      ]){
        if(Array.isArray(value)) return value;
      }
      return null;
    }

    function _rendererSnapshotRoot(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      return _own(item,'root')||_own(item,'turn')||_own(item,'element')||
        _own(opts,'root')||_own(opts,'turn')||_own(opts,'element')||null;
    }

    function _rendererSnapshotDomRows(root, mode){
      if(!root||typeof root.querySelectorAll!=='function') return [];
      const selector=mode==='transparent_stream'
        ? '.transparent-event-row,[data-transparent-event-row="1"]'
        : [
          '.wl-reason[data-worklog-reason-source="reasoning"]',
          '.wl-reason[data-worklog-anchor-reason="1"]',
          '.agent-activity-thinking',
          '.thinking-card-row',
          '.tool-card-row',
        ].join(',');
      return Array.from(root.querySelectorAll(selector)||[]);
    }

    function _rendererSnapshotAttr(row, keys){
      return _activityReconciliationDataValue(row,keys);
    }

    function _rendererSnapshotQueryText(row, selector){
      if(!row||typeof row.querySelector!=='function') return '';
      const el=row.querySelector(selector);
      return el&&typeof el.textContent==='string'?el.textContent:'';
    }

    function _rendererSnapshotText(row){
      const explicit=_firstTextValue(
        _rendererSnapshotAttr(row,['text']),
        _rendererSnapshotAttr(row,['preview']),
        _rendererSnapshotAttr(row,['summary']),
        _rendererSnapshotAttr(row,['content'])
      );
      if(explicit) return explicit;
      return _firstTextValue(
        _rendererSnapshotQueryText(row,'.transparent-event-preview'),
        _rendererSnapshotQueryText(row,'.transparent-event-thinking-preview'),
        _rendererSnapshotQueryText(row,'.thinking-card-body pre'),
        _rendererSnapshotQueryText(row,'.tool-card-preview'),
        _rendererSnapshotQueryText(row,'.tool-card-result pre'),
        typeof row.textContent==='string'?row.textContent:''
      );
    }

    function _rendererSnapshotHasClass(row, className){
      return !!(row&&row.classList&&typeof row.classList.contains==='function'&&row.classList.contains(className));
    }

    function _rendererSnapshotHasAttribute(row, attrName){
      return !!(row&&typeof row.hasAttribute==='function'&&row.hasAttribute(attrName));
    }

    function _rendererSnapshotStatus(row){
      const raw=_rendererSnapshotAttr(row,['status','state','event_status','eventStatus']);
      if(raw!==undefined&&raw!==null&&raw!=='') return _activityReconciliationStatus(raw);
      if(_rendererSnapshotHasClass(row,'tool-card-running')) return 'running';
      return null;
    }

    function _rendererSnapshotToolDone(row, status){
      const explicit=_rendererSnapshotAttr(row,['tool_done','toolDone','done']);
      const parsed=_activityReconciliationBool(explicit);
      if(parsed!==null) return parsed;
      if(status==='completed'||status==='error'||status==='failed') return true;
      if(status==='running'||status==='pending'||status==='interrupted') return false;
      return null;
    }

    function _rendererSnapshotToolError(row, status){
      const explicit=_rendererSnapshotAttr(row,['tool_is_error','toolError','is_error','isError']);
      const parsed=_activityReconciliationBool(explicit);
      if(parsed!==null) return parsed;
      if(status==='error'||status==='failed') return true;
      return false;
    }

    function _rendererSnapshotToolName(row){
      return _firstTextValue(
        _rendererSnapshotAttr(row,['tool_name','toolName','name']),
        _rendererSnapshotQueryText(row,'.tool-card-name')
      )||null;
    }

    function _rendererSnapshotToolCallId(row){
      return _firstTextValue(
        _rendererSnapshotAttr(row,['tool_call_id','toolCallId','tool_use_id','toolUseId','call_id','callId','tid','live_tid','liveTid'])
      )||null;
    }

    function _rendererSnapshotRowId(row){
      return _firstTextValue(
        _rendererSnapshotAttr(row,[
          'row_id',
          'rowId',
          'activity_row_id',
          'activityRowId',
          'event_id',
          'eventId',
          'anchor_event_id',
          'anchorEventId',
          'id',
        ])
      )||null;
    }

    function _rendererSnapshotKindFromType(type, status, toolDone){
      if(type==='thinking'||type==='reasoning') return 'reasoning';
      if(type==='tool'||type==='tool_call'||type==='tool-card'){
        if(toolDone===true||status==='completed'||status==='error'||status==='failed') return 'tool_completed';
        return 'tool_started';
      }
      if(type==='compressing'||type==='compressed') return 'lifecycle_status';
      if(type==='done'||type==='cancel'||type==='error'||type==='apperror') return 'terminal_status';
      return type||null;
    }

    function _rendererSnapshotKind(row, mode, status, toolDone){
      const explicit=_cleanString(_rendererSnapshotAttr(row,['kind']));
      if(explicit) return explicit;
      const type=_cleanString(_rendererSnapshotAttr(row,['event_type','eventType','type']));
      if(type) return _rendererSnapshotKindFromType(type,status,toolDone);
      if(mode==='transparent_stream'){
        if(_rendererSnapshotAttr(row,['tool_name','toolName','name'])||_rendererSnapshotQueryText(row,'.tool-card-name')){
          return _rendererSnapshotKindFromType('tool',status,toolDone);
        }
        return 'reasoning';
      }
      if(_rendererSnapshotHasClass(row,'wl-reason')||_rendererSnapshotHasClass(row,'agent-activity-thinking')||_rendererSnapshotHasClass(row,'thinking-card-row')){
        return 'reasoning';
      }
      if(_rendererSnapshotHasClass(row,'tool-card-row')){
        const compressionCardValue=_rendererSnapshotAttr(row,['compression_card','compressionCard']);
        const hasCompressionCard=compressionCardValue!==undefined||
          _rendererSnapshotHasAttribute(row,'data-compression-card')||
          _rendererSnapshotHasAttribute(row,'compression-card')||
          _rendererSnapshotHasAttribute(row,'compression_card');
        if(hasCompressionCard&&_activityReconciliationBool(compressionCardValue)!==false) return 'lifecycle_status';
        return _rendererSnapshotKindFromType('tool',status,toolDone);
      }
      return null;
    }

    function _rendererSnapshotRole(kind){
      return _activityRowRole(kind||'activity');
    }

    function _rendererSnapshotSourceEventType(row, kind){
      const explicit=_cleanString(_rendererSnapshotAttr(row,['source_event_type','sourceEventType']));
      if(explicit) return explicit;
      const type=_cleanString(_rendererSnapshotAttr(row,['event_type','eventType','type']));
      if(type==='thinking') return 'reasoning';
      if(type==='tool') return kind==='tool_completed'?'tool_complete':'tool';
      if(kind==='reasoning') return 'reasoning';
      if(kind==='tool_started') return 'tool';
      if(kind==='tool_completed') return 'tool_complete';
      if(kind==='lifecycle_status') return type||'compressed';
      if(kind==='terminal_status') return type||'done';
      return type||null;
    }

    function _rendererSnapshotRow(row, index, mode){
      const status=_rendererSnapshotStatus(row);
      const provisionalDone=_rendererSnapshotToolDone(row,status);
      const kind=_rendererSnapshotKind(row,mode,status,provisionalDone);
      const toolDone=_isToolActivityKind(kind)?_rendererSnapshotToolDone(row,status):null;
      const toolError=_isToolActivityKind(kind)?_rendererSnapshotToolError(row,status):null;
      const sourceEventType=_rendererSnapshotSourceEventType(row,kind);
      return Object.freeze({
        row_id:_rendererSnapshotRowId(row),
        order_index:index,
        kind,
        role:_rendererSnapshotRole(kind),
        source_event_type:sourceEventType,
        status,
        text:_rendererSnapshotText(row),
        tool_call_id:_isToolActivityKind(kind)?_rendererSnapshotToolCallId(row):null,
        tool_name:_isToolActivityKind(kind)?_rendererSnapshotToolName(row):null,
        tool_done:toolDone,
        tool_is_error:toolError,
      });
    }

    function createAssistantTurnAnchorRendererSnapshot(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      const mode=_rendererSnapshotMode(item,opts);
      const renderer=_cleanString(_own(item,'renderer'))||_cleanString(_own(opts,'renderer'))||mode;
      const explicitRows=_rendererSnapshotRowsInput(item,opts);
      const rows=(explicitRows||_rendererSnapshotDomRows(_rendererSnapshotRoot(item,opts),mode))
        .map((row,index)=>_rendererSnapshotRow(row,index,mode));
      return Object.freeze({
        version:'renderer_snapshot_v1',
        mode,
        renderer,
        row_count:rows.length,
        rows:Object.freeze(rows),
      });
    }

    function reconcileAssistantTurnAnchorRendererSnapshot(input, options){
      const item=(input&&typeof input==='object')?input:{};
      const opts=(options&&typeof options==='object')?options:{};
      const snapshotInput=_own(item,'renderer_snapshot')||_own(item,'snapshot')||_own(opts,'renderer_snapshot')||_own(opts,'snapshot');
      const snapshot=(snapshotInput&&typeof snapshotInput==='object'&&_own(snapshotInput,'version')==='renderer_snapshot_v1')
        ? snapshotInput
        : createAssistantTurnAnchorRendererSnapshot(item,opts);
      const reconciliation=reconcileAssistantTurnAnchorActivityScene({
        ...item,
        mode:snapshot.mode,
        renderer_rows:snapshot.rows,
      },opts);
      return Object.freeze({
        version:'renderer_snapshot_reconciliation_v1',
        mode:snapshot.mode,
        renderer:snapshot.renderer,
        matched:reconciliation.matched,
        snapshot,
        reconciliation,
      });
    }


    return Object.freeze({
      projectAssistantTurnAnchorActivityScene,
      reconcileAssistantTurnAnchorActivityScene,
      createAssistantTurnAnchorRendererSnapshot,
      reconcileAssistantTurnAnchorRendererSnapshot,
    });
  };
})(typeof window!=='undefined'?window:globalThis);
