import { createStreamAnchorSceneSettlement } from './anchor-scene.js';

// One live assistant turn owns one Anchor registry. This module keeps event
// normalization, idempotent activity projection, hydration, echo removal, and
// registry cleanup behind that per-turn interface.
export function createStreamAnchorLiveRuntime(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const api=options.request;
  const _runJournalReplayAfterSeq=typeof options.runJournalReplayAfterSeq==='function'
    ? options.runJournalReplayAfterSeq
    : ()=>0;
  const _isActiveSession=typeof options.isActiveSession==='function'
    ? options.isActiveSession
    : ()=>false;
  const _liveThinkingPlacement=typeof options.liveThinkingPlacement==='function'
    ? options.liveThinkingPlacement
    : ()=>({activityKey:null,segmentSeq:1,burstId:0});
  const syncInflightAssistantMessage=typeof options.syncInflightAssistantMessage==='function'
    ? options.syncInflightAssistantMessage
    : ()=>{};
  const _placement=typeof options.readPlacement==='function'
    ? options.readPlacement
    : ()=>({assistantSegmentSeq:0,currentLiveSegmentSeq:0,currentActivityBurstId:0});
  const _readReasoning=typeof options.readReasoning==='function'
    ? options.readReasoning
    : ()=>({reasoningText:'',liveReasoningText:''});
  const _writeReasoning=typeof options.writeReasoning==='function'
    ? options.writeReasoning
    : ()=>{};
  const _getInflight=typeof options.getInflight==='function'
    ? options.getInflight
    : ()=>null;
  const _getOldestMessageIndex=typeof options.getOldestMessageIndex==='function'
    ? options.getOldestMessageIndex
    : ()=>0;
  const _anchorApi=(typeof window!=='undefined'&&window.HermesAssistantTurnAnchors)
    ? window.HermesAssistantTurnAnchors
    : null;
  const _anchorRegistryMap=(typeof window!=='undefined')
    ? (window._liveAnchorRegistries=window._liveAnchorRegistries||new Map())
    : null;
  const _existingAnchorRegistry=_anchorRegistryMap?_anchorRegistryMap.get(streamId):null;
  const _anchorRegistry=_existingAnchorRegistry||(_anchorApi&&typeof _anchorApi.createAssistantTurnAnchorRegistry==='function'
    ? _anchorApi.createAssistantTurnAnchorRegistry({
      session_id:activeSid,
      stream_id:streamId,
      run_id:null,
    })
    : null);
  let _anchorShadowWarned=false;
  let _anchorReasoningFlushed=false;
  let _anchorLocalSeq=0;
  if(_anchorRegistryMap&&_anchorRegistry) _anchorRegistryMap.set(streamId,_anchorRegistry);
  function _scheduleAnchorRegistryCleanup(delayMs=600000){
    if(!_anchorRegistryMap||!_anchorRegistry) return;
    setTimeout(()=>{
      if(_anchorRegistryMap.get(streamId)===_anchorRegistry) _anchorRegistryMap.delete(streamId);
    },delayMs);
  }
  // Backstop: schedule an identity-guarded cleanup at creation so this shadow
  // registry self-expires no matter which teardown path the stream takes
  // (incl. external ones like sidebar cancelSessionStream() that bypass the
  // in-closure SSE handlers). Explicit terminal-path calls above just expire it
  // sooner; this guarantees window._liveAnchorRegistries can't grow unbounded.
  _scheduleAnchorRegistryCleanup(600000);
  // Applying an event and painting it are separate outcomes. Reasoning uses the
  // optional holder to decide whether a temporary visible fallback is needed.
  function _applyToAnchor(sourceEventType, rawEventData, sseEvent, renderOutcome){
    if(renderOutcome&&typeof renderOutcome==='object') renderOutcome.rendered=false;
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return null;
    const raw=(rawEventData&&typeof rawEventData==='object')?rawEventData:{};
    const eventId=(sseEvent&&sseEvent.lastEventId)||raw.event_id||raw.lastEventId||raw.last_event_id||'';
    const sourceEvent={
      ...raw,
      source_event_type:sourceEventType,
      // Persist a creation timestamp the FIRST time we see this source event, so
      // the worklog event timestamp (#5700/#5739) survives settlement. Reasoning
      // events carry no server timestamp; without this, the live DOM shows a
      // fallback time but the settled scene row rebuilds with created_at:null and
      // the timestamp disappears. Prefer any real server-supplied stamp; fall back
      // to now only when none exists. (#5739 gate finding.)
      created_at:raw.created_at??raw.timestamp??raw.ts??(Date.now()/1000),
      activitySegmentSeq:raw.activitySegmentSeq??raw.activity_segment_seq??_placement().assistantSegmentSeq,
      activityBurstId:raw.activityBurstId??raw.activity_burst_id??_placement().currentActivityBurstId,
    };
    if(eventId) sourceEvent.event_id=eventId;
    try{
      const result=_anchorApi.applyAssistantTurnAnchorSourceEvent(
        _anchorRegistry,
        sourceEvent,
        {session_id:activeSid,stream_id:streamId}
      );
      const rendered=_renderAnchorLiveScene();
      if(renderOutcome&&typeof renderOutcome==='object') renderOutcome.rendered=rendered;
      return result;
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live shadow feed failed',err);
      }
      return null;
    }
  }
  function _anchorActivityEvents(){
    const anchor=_anchorRegistry&&_anchorRegistry.anchor;
    return anchor&&Array.isArray(anchor.activity_events)?anchor.activity_events:null;
  }
  function _findAnchorActivityEventByLocalId(localId, sourceEventType){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      return event;
    }
    return null;
  }
  function _latestAnchorCompressionEventIndex(sourceEventType){
    const events=_anchorActivityEvents();
    if(!events) return -1;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(event&&event.source_event_type===sourceEventType) return i;
    }
    return -1;
  }
  function _anchorCompressionCompletedAfter(index){
    const events=_anchorActivityEvents();
    if(!events) return false;
    for(let i=events.length-1;i>index;i--){
      const event=events[i];
      if(event&&event.source_event_type==='compressed') return true;
    }
    return false;
  }
  function _ensureAnchorCompressionCompletedOnLiveProgress(sessionId){
    if(!_anchorRegistry||!_anchorApi) return false;
    const sid=String(sessionId||activeSid||'');
    const events=_anchorActivityEvents();
    const runningIndex=_latestAnchorCompressionEventIndex('compressing');
    if(runningIndex>=0&&_anchorCompressionCompletedAfter(runningIndex)) return true;
    const runningEvent=(events&&runningIndex>=0)?events[runningIndex]:null;
    const basis=String((runningEvent&&(runningEvent.local_id||runningEvent.event_id))||streamId||sid||'compression');
    const localId=`live-compression-complete:${basis}`;
    if(_findAnchorActivityEventByLocalId(localId,'compressed')) return true;
    const eventId=`synthetic:${localId}`;
    const result=_applyToAnchor('compressed',{
      event_id:eventId,
      local_id:localId,
      session_id:sid,
      old_session_id:sid,
      automatic:true,
      synthetic:true,
      status:'completed',
      phase:'done',
      message:'Context auto-compressed',
    },{lastEventId:eventId});
    return !!(result&&(result.applied||result.reason==='duplicate'));
  }
  function _replaceAnchorActivityEventByLocalId(localId, sourceEventType, patch){
    const events=_anchorActivityEvents();
    if(!events||!localId) return null;
    for(let i=events.length-1;i>=0;i--){
      const event=events[i];
      if(!event||event.local_id!==localId) continue;
      if(sourceEventType&&event.source_event_type!==sourceEventType) continue;
      const next={
        ...event,
        ...(patch||{}),
        payload:{
          ...(event.payload&&typeof event.payload==='object'?event.payload:{}),
          ...((patch&&patch.payload&&typeof patch.payload==='object')?patch.payload:{}),
        },
      };
      events[i]=next;
      return next;
    }
    return null;
  }
  function _nextAnchorLocalSeq(){
    _anchorLocalSeq+=1;
    const cursor=Number(_runJournalReplayAfterSeq&&_runJournalReplayAfterSeq());
    return (Number.isFinite(cursor)?cursor:0)+_anchorLocalSeq;
  }
  function _anchorSegmentSeq(){
    const seq=Number(_placement().assistantSegmentSeq||_placement().currentLiveSegmentSeq||0);
    return Number.isFinite(seq)&&seq>0?seq:1;
  }
  function _anchorSceneActiveMode(){
    const normalize=value=>value==='transparent_stream'||value==='compact_worklog'||value==='hide_all_activity'?value:'';
    if(typeof window!=='undefined'){
      if(typeof window.chatActivityMode==='function'){
        try{
          const mode=normalize(window.chatActivityMode());
          if(mode) return mode;
        }catch(_){}
      }
      const displayMode=normalize(window._chatActivityDisplayMode);
      if(displayMode) return displayMode;
      if(window._transparentStream) return 'transparent_stream';
    }
    return 'compact_worklog';
  }
  function _anchorSceneRowDisplayHintForMode(row, sceneMode){
    const hints=row&&typeof row==='object'&&row.display_hints&&typeof row.display_hints==='object'
      ? row.display_hints
      : null;
    if(sceneMode==='transparent_stream') return (hints&&hints.transparent_stream)||'chronological_activity';
    if(sceneMode==='compact_worklog') return (hints&&hints.compact_worklog)||row.display_hint||'activity_row';
    if(sceneMode==='hide_all_activity') return (hints&&hints.hidden_activity)||'hidden_activity';
    return row&&row.display_hint||'activity_row';
  }
  function _renderAnchorLiveScene(){
    if(!_anchorRegistry||!_isActiveSession()) return false;
    if(typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneForStream!=='function') return false;
    try{
      return !!window._renderLiveAnchorActivitySceneForStream(streamId, activeSid, {
        mode:_anchorSceneActiveMode(),
      });
    }catch(err){
      if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
        _anchorShadowWarned=true;
        console.warn('assistant turn anchor live scene render failed',err);
      }
      return false;
    }
  }
  function _projectLiveAnchorActivityScene(){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.projectAssistantTurnAnchorActivityScene!=='function') return null;
    try{
      return _anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:_anchorSceneActiveMode()});
    }catch(_){
      return null;
    }
  }
  const _anchorSceneSettlement=createStreamAnchorSceneSettlement({
    sessionId:activeSid,
    streamId,
    state:S,
    anchorRegistry:_anchorRegistry,
    projectLiveScene:_projectLiveAnchorActivityScene,
    activeMode:_anchorSceneActiveMode,
    rowDisplayHintForMode:_anchorSceneRowDisplayHintForMode,
    request:api,
    getOldestMessageIndex:_getOldestMessageIndex,
  });
  function _attachProjectedAnchorSceneToLastAssistant(messages){
    return _anchorSceneSettlement.attachProjectedSceneToLastAssistant(messages);
  }
  function _upsertAnchorProcessProse(displayText, options={}){
    const text=String(displayText||'').trim();
    if(!text||!_anchorRegistry) return null;
    const segmentSeq=Number(options.segmentSeq||_anchorSegmentSeq());
    const localId=`live-prose:${streamId}:${segmentSeq}`;
    const existing=_findAnchorActivityEventByLocalId(localId,'token');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'token',{
        status:options.sealed?'completed':'running',
        payload:{text,activitySegmentSeq:segmentSeq,activityBurstId:_placement().currentActivityBurstId},
      });
      _renderAnchorLiveScene();
      return replaced;
    }
    _applyToAnchor('token',{
      text,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_placement().currentActivityBurstId,
    },null);
    return _findAnchorActivityEventByLocalId(localId,'token');
  }
  function _anchorHasReasoningEvents(){
    const events=_anchorActivityEvents();
    return !!(events&&events.some(event=>event&&event.source_event_type==='reasoning'));
  }
  function _upsertAnchorReasoning(text, options={}){
    const clean=String(text||'').trim();
    const placement=_liveThinkingPlacement();
    const segmentSeq=Number(options.segmentSeq||placement.segmentSeq||_anchorSegmentSeq());
    const localId=String(options.localId||`live-reasoning:${streamId}:${segmentSeq}`);
    if(options&&typeof options==='object'){
      options.anchorReasoningLocalId=localId;
      options.segmentSeq=segmentSeq;
      if(options.burstId===undefined) options.burstId=_placement().currentActivityBurstId;
    }
    if(!clean||!_anchorRegistry||window._showThinking===false) return null;
    const existing=_findAnchorActivityEventByLocalId(localId,'reasoning');
    if(existing){
      const replaced=_replaceAnchorActivityEventByLocalId(localId,'reasoning',{
        status:options.sealed?'completed':'running',
        payload:{text:clean,activitySegmentSeq:segmentSeq,activityBurstId:_placement().currentActivityBurstId},
      });
      return _renderAnchorLiveScene()?replaced:null;
    }
    const renderOutcome={rendered:false};
    _applyToAnchor('reasoning',{
      text:clean,
      local_id:localId,
      seq:_nextAnchorLocalSeq(),
      status:options.sealed?'completed':'running',
      activitySegmentSeq:segmentSeq,
      activityBurstId:_placement().currentActivityBurstId,
    },null,renderOutcome);
    return renderOutcome.rendered?_findAnchorActivityEventByLocalId(localId,'reasoning'):null;
  }
  function _compactVisibleEchoText(value){
    return String(value||'').replace(/\s+/g,'');
  }
  function _stripCompactEchoSuffix(value, suffix){
    const raw=String(value||'');
    const candidate=_compactVisibleEchoText(suffix);
    if(!raw||!candidate) return {text:raw,removed:false};
    const windowSize=Math.max(String(suffix||'').length*3,4096);
    const offset=Math.max(0,raw.length-windowSize);
    const tail=raw.slice(offset);
    for(let idx=0;idx<=tail.length;idx+=1){
      if(_compactVisibleEchoText(tail.slice(idx))===candidate){
        return {text:raw.slice(0,offset+idx).trimEnd(),removed:true};
      }
    }
    return {text:raw,removed:false};
  }
  function _stripAnchorReasoningEcho(visible){
    const events=_anchorActivityEvents();
    if(!events||!visible) return false;
    for(let i=events.length-1;i>=0;i-=1){
      const event=events[i];
      if(!event||event.source_event_type!=='reasoning') continue;
      const payload=(event.payload&&typeof event.payload==='object')?event.payload:{};
      const rawText=String(payload.text||payload.reasoning||payload.thinking||'');
      const stripped=_stripCompactEchoSuffix(rawText, visible);
      if(!stripped.removed) continue;
      const nextText=String(stripped.text||'').trim();
      if(nextText){
        _replaceAnchorActivityEventByLocalId(event.local_id,'reasoning',{
          payload:{text:nextText},
        });
      }else{
        events.splice(i,1);
      }
      _renderAnchorLiveScene();
      return true;
    }
    return false;
  }
  function _removeLiveReasoningEchoRows(visible){
    const turn=$('liveAssistantTurn');
    const blocks=turn&&typeof _assistantTurnBlocks==='function'?_assistantTurnBlocks(turn):null;
    if(!blocks||!visible) return false;
    let removed=false;
    const selector=[
      '.agent-activity-thinking[data-anchor-scene-row="1"]',
      '.agent-activity-thinking[data-live-thinking="1"]',
      '.wl-reason[data-worklog-anchor-reason="1"]',
      '.wl-reason[data-worklog-reason-source="reasoning"]'
    ].join(',');
    blocks.querySelectorAll(selector).forEach(row=>{
      const textNode=row.querySelector&&(
        row.querySelector('.thinking-card-body pre') ||
        row.querySelector('.thinking-card-body')
      );
      const text=String((textNode&&textNode.textContent)||row.textContent||'');
      if(!_stripCompactEchoSuffix(text, visible).removed) return;
      row.remove();
      removed=true;
    });
    if(removed&&typeof _syncToolCallGroupSummary==='function'){
      blocks.querySelectorAll('.tool-worklog-group,.tool-call-group').forEach(group=>{
        _syncToolCallGroupSummary(group);
      });
    }
    return removed;
  }
  function _stripLiveReasoningEcho(visible){
    let removed=false;
    const reasoningState=_readReasoning();
    const durable=_stripCompactEchoSuffix(reasoningState.reasoningText, visible);
    const live=_stripCompactEchoSuffix(reasoningState.liveReasoningText, visible);
    const nextReasoning=durable.removed?durable.text:reasoningState.reasoningText;
    const nextLiveReasoning=live.removed?live.text:reasoningState.liveReasoningText;
    if(durable.removed||live.removed){
      _writeReasoning({
        reasoningText:nextReasoning,
        liveReasoningText:nextLiveReasoning,
      });
      removed=true;
    }
    const anchorRemoved=_stripAnchorReasoningEcho(visible);
    const domRemoved=_removeLiveReasoningEchoRows(visible);
    if(removed) syncInflightAssistantMessage();
    if((removed||anchorRemoved||domRemoved)&&!String(nextLiveReasoning||'').trim()&&typeof removeThinking==='function'){
      removeThinking();
    }
    return removed||anchorRemoved||domRemoved;
  }
  function _flushReasoningToAnchor(){
    const reasoningText=_readReasoning().reasoningText;
    if(_anchorReasoningFlushed||!reasoningText) return;
    _anchorReasoningFlushed=true;
    if(_anchorHasReasoningEvents()) return;
    _upsertAnchorReasoning(reasoningText,{sealed:true,localId:`live-reasoning:${streamId}:final`});
  }
  function _sourceEventTypeForSnapshotAnchorRow(row){
    const source=String(row&&row.source_event_type||'').trim();
    if(source&&source!=='runtime_journal_snapshot') return source;
    const role=String(row&&row.role||'').trim();
    const kind=String(row&&row.kind||'').trim();
    if(role==='prose'||kind==='process_prose') return 'token';
    if(role==='thinking'||kind==='reasoning') return 'reasoning';
    if(role==='tool') return row&&row.status==='running'?'tool':'tool_complete';
    // Terminal statuses are done/cancel/error/apperror — never invent a
    // compression start from a running terminal row (false "Compressing context").
    if(role==='terminal'||kind==='terminal_status'){
      const termStatus=String(row&&row.status||'').trim().toLowerCase();
      if(termStatus==='cancelled'||termStatus==='canceled'||termStatus==='interrupted') return 'cancel';
      if(termStatus==='error'||termStatus==='failed'||termStatus==='errored') return 'error';
      if(termStatus==='running') return '';
      return 'done';
    }
    // lifecycle_status is shared by compressing + compressed. Prefer explicit
    // cues; do not default every lifecycle row to a running compress divider.
    if(role==='lifecycle'||kind==='lifecycle_status'){
      const phase=String(row&&(row.phase||row.status)||'').trim().toLowerCase();
      const text=String(row&&(row.text||row.message||row.label)||'').trim().toLowerCase();
      if(
        phase==='done'||phase==='completed'||phase==='compressed'
        || text.includes('auto-compressed')
        || text.includes('compression finished')
        || (text.includes('compressed')&&!text.includes('compressing'))
      ) return 'compressed';
      if(
        phase==='running'||phase==='compressing'
        || text.includes('compressing context')
        || text.includes('compacting context')
        || text.includes('preflight compression')
        || text.includes('pre-api compression')
        || text.includes('context too large')
        || text.includes('compression attempt')
        || (text.includes('compressing')&&!text.includes('skipping'))
      ) return 'compressing';
      return '';
    }
    return '';
  }
  function _hydrateAnchorRegistryFromActivityScene(scene){
    if(!_anchorRegistry||!_anchorApi||typeof _anchorApi.applyAssistantTurnAnchorSourceEvent!=='function') return false;
    if(!scene||scene.version!=='activity_scene_v1'||!Array.isArray(scene.activity_rows)||!scene.activity_rows.length) return false;
    const sceneKey=[
      scene.identity&&scene.identity.stream_id||streamId||'',
      scene.activity_rows.length,
      scene.activity_rows.map(row=>row&&row.row_id||row&&row.local_id||'').join('|'),
    ].join(':');
    if(_anchorRegistry._hydrated_activity_scene_key===sceneKey) return true;
    const rows=scene.activity_rows;
    for(let i=0;i<rows.length;i+=1){
      const row=rows[i];
      if(!row||typeof row!=='object') continue;
      const sourceType=_sourceEventTypeForSnapshotAnchorRow(row);
      if(!sourceType) continue;
      const payload={
        ...((row.payload&&typeof row.payload==='object')?row.payload:{}),
      };
      if(row.text&&!payload.text) payload.text=row.text;
      if(row.tool&&typeof row.tool==='object'){
        payload.name=payload.name||row.tool.name;
        payload.args=payload.args||row.tool.args;
        payload.preview=payload.preview||row.tool.preview;
        payload.snippet=payload.snippet||row.tool.snippet;
        payload.tid=payload.tid||row.tool.tid||row.tool.id;
        payload.id=payload.id||row.tool.id||row.tool.tid;
        payload.is_error=payload.is_error||row.tool.is_error;
        payload.duration=payload.duration||row.tool.duration;
      }
      if(row.group&&typeof row.group==='object'){
        payload.activitySegmentSeq=payload.activitySegmentSeq||row.group.activity_segment_seq;
        payload.activityBurstId=payload.activityBurstId||row.group.activity_burst_id;
      }
      const sourceEvent={
        ...payload,
        source_event_type:sourceType,
        local_id:row.local_id||row.row_id||`snapshot:${streamId}:${i}`,
        event_id:row.event_id||null,
        seq:row.seq??undefined,
        status:row.status||undefined,
        stream_id:row.stream_id||streamId,
        run_id:row.run_id||streamId,
        // Carry the row's persisted creation timestamp through hydration so the
        // worklog event timestamp (#5700/#5739) survives a settled-snapshot rebuild
        // (payload may not carry created_at even when the row does). (#5739 gate.)
        created_at:payload.created_at??row.created_at??undefined,
      };
      try{
        _anchorApi.applyAssistantTurnAnchorSourceEvent(_anchorRegistry,sourceEvent,{session_id:activeSid,stream_id:streamId,run_id:streamId});
      }catch(err){
        if(!_anchorShadowWarned&&typeof console!=='undefined'&&console.warn){
          _anchorShadowWarned=true;
          console.warn('assistant turn anchor snapshot hydration failed',err);
        }
        return false;
      }
    }
    _anchorRegistry._hydrated_activity_scene_key=sceneKey;
    return true;
  }
  _hydrateAnchorRegistryFromActivityScene(_getInflight()&&_getInflight().anchorActivityScene);
  return Object.freeze({
    registry:_anchorRegistry,
    apply:_applyToAnchor,
    ensureCompressionCompletedOnLiveProgress:_ensureAnchorCompressionCompletedOnLiveProgress,
    attachProjectedSceneToLastAssistant:_attachProjectedAnchorSceneToLastAssistant,
    upsertProcessProse:_upsertAnchorProcessProse,
    upsertReasoning:_upsertAnchorReasoning,
    stripReasoningEcho:_stripLiveReasoningEcho,
    flushReasoning:_flushReasoningToAnchor,
    hydrateFromActivityScene:_hydrateAnchorRegistryFromActivityScene,
    scheduleCleanup:_scheduleAnchorRegistryCleanup,
  });
}
