import { _activityStatusNode } from './activity-and-scroll.js';
import { ensureActivityGroup } from './anchor-scenes.js';
import { _firstValidTimestampSeconds, _timestampSeconds } from './composer-controls.js';
import { renderMd } from './markdown-renderer.js';
import { _autoCompressionWorklogNode } from './compression-ui.js';
import { _decorateTransparentEventRow, _thinkingActivityNode, _transparentToolStatus } from './activity-presentation.js';
import { S, esc } from './state.js';
import { buildToolCard } from './tool-card-presentation.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './worklog-tool-groups.js';

function _anchorSceneToolRowLogicalKey(row){
  if(!row||row.role!=='tool') return '';
  const tool=(row.tool&&typeof row.tool==='object')?row.tool:{};
  const payload=(row.payload&&typeof row.payload==='object')?row.payload:{};
  const id=row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id||
    payload.tid||payload.id||payload.tool_call_id||payload.tool_use_id||payload.call_id||'';
  return id?`call:${id}`:'';
}
function _anchorSceneMergeToolRows(prev, row){
  if(!prev) return row;
  const prevTool=(prev.tool&&typeof prev.tool==='object')?prev.tool:{};
  const nextTool=(row&&row.tool&&typeof row.tool==='object')?row.tool:{};
  const prevPayload=(prev.payload&&typeof prev.payload==='object')?prev.payload:{};
  const nextPayload=(row&&row.payload&&typeof row.payload==='object')?row.payload:{};
  const prevArgs=(prevTool.args&&typeof prevTool.args==='object')?prevTool.args:
    ((prevPayload.args&&typeof prevPayload.args==='object')?prevPayload.args:{});
  const nextArgs=(nextTool.args&&typeof nextTool.args==='object')?nextTool.args:
    ((nextPayload.args&&typeof nextPayload.args==='object')?nextPayload.args:{});
  const mergedPayload=Object.assign({},prevPayload,nextPayload);
  const mergedTool=Object.assign({},prevTool,nextTool);
  if(!Object.keys(nextArgs).length&&Object.keys(prevArgs).length) mergedTool.args=prevArgs;
  const prevPreview=String(prevTool.preview||prevPayload.preview||'').trim();
  const nextText=String(row&&row.text||'').trim();
  const nextSnippet=String(nextTool.snippet||nextPayload.snippet||nextPayload.result||nextPayload.output||'').trim();
  const nextStatus=String(row&&row.status||'').toLowerCase();
  const nextLooksLikeResult=!!nextSnippet||(
    !!nextText&&nextStatus&&nextStatus!=='running'&&nextStatus!=='pending'
  );
  if(prevPreview&&nextLooksLikeResult){
    mergedTool.preview=prevTool.preview||prevPayload.preview||prevPreview;
    if(!mergedPayload.preview) mergedPayload.preview=prevPayload.preview||prevPreview;
  }
  if(nextSnippet) mergedTool.snippet=nextTool.snippet||nextPayload.snippet||nextPayload.result||nextPayload.output||nextSnippet;
  return Object.assign({},prev,row,{
    row_id:prev.row_id||row.row_id,
    order_index:prev.order_index??row.order_index,
    payload:mergedPayload,
    tool:mergedTool,
  });
}

function _anchorSceneRowsForRendering(scene, opts){
  const rows=Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[];
  const settled=!!(opts&&opts.settled);
  const live=!settled;
  const out=[];
  const byKey=new Map();
  const liveProseTextKeys=new Map();
  const proseTextKey=(value)=>String(value||'').replace(/\s+/g,' ').trim();
  const keyFor=(row)=>{
    if(!row) return '';
    if(row.role==='tool') return `tool:${_anchorSceneToolRowLogicalKey(row)||row.row_id||row.event_id||row.local_id||out.length}`;
    if(row.role==='prose') return `prose:${row.local_id||row.row_id||out.length}`;
    if(row.role==='thinking') return `thinking:${row.local_id||row.row_id||out.length}`;
    if(row.role==='lifecycle'){
      const source=String(row.source_event_type||'');
      if(source==='compressing'||source==='compressed') return 'lifecycle:compression';
      return `lifecycle:${source||row.local_id||row.row_id||out.length}`;
    }
    return `row:${row.row_id||out.length}`;
  };
  for(const row of rows){
    if(!row||typeof row!=='object') continue;
    if(row.role==='terminal'&&row.source_event_type==='done') continue;
    if(_anchorSceneIsSettledSuccessfulCompression(row,settled)) continue;
    const text=String(row.text||'').trim();
    if((row.role==='prose'||row.role==='thinking')&&!text) continue;
    const key=keyFor(row);
    if(byKey.has(key)){
      const index=byKey.get(key);
      if(live&&row.role==='prose'){
        const textKey=proseTextKey(text);
        const duplicateIndex=textKey?liveProseTextKeys.get(textKey):undefined;
        if(duplicateIndex!==undefined&&duplicateIndex!==index) continue;
        const previousTextKey=proseTextKey(out[index]&&out[index].text);
        if(previousTextKey&&previousTextKey!==textKey&&liveProseTextKeys.get(previousTextKey)===index){
          liveProseTextKeys.delete(previousTextKey);
        }
        if(textKey) liveProseTextKeys.set(textKey,index);
      }
      out[index]=row.role==='tool'?_anchorSceneMergeToolRows(out[index],row):row;
    }else{
      if(live&&row.role==='prose'){
        const textKey=proseTextKey(text);
        if(textKey&&liveProseTextKeys.has(textKey)) continue;
        if(textKey) liveProseTextKeys.set(textKey,out.length);
      }
      byKey.set(key,out.length);
      out.push(row);
    }
  }
  return out;
}
function _anchorSceneIsSettledSuccessfulCompression(row, settled){
  if(!settled||!row||row.role!=='lifecycle') return false;
  const source=String(row.source_event_type||'');
  if(source!=='compressing'&&source!=='compressed') return false;
  const status=String(row.status||'').toLowerCase();
  return !['error','failed','failure','compression_exhausted','degraded','interrupted','connection_lost'].includes(status);
}
function _anchorSceneToolCallFromRow(row, opts){
  const tool=(row&&row.tool&&typeof row.tool==='object')?row.tool:{};
  const payload=(row&&row.payload&&typeof row.payload==='object')?row.payload:{};
  const timestampSeconds=typeof _timestampSeconds==='function'?_timestampSeconds:function(value){
    const stamp=Number(value);
    return Number.isFinite(stamp)&&stamp>0?(stamp>1e12?stamp/1000:stamp):null;
  };
  const firstValidTimestampSeconds=typeof _firstValidTimestampSeconds==='function'
    ? _firstValidTimestampSeconds
    : function(...values){
        for(const value of values){
          const stamp=timestampSeconds(value);
          if(stamp) return stamp;
        }
        return null;
      };
  const rowTs=typeof _anchorSceneRowTimestampSeconds==='function'
    ? _anchorSceneRowTimestampSeconds(row)
    : firstValidTimestampSeconds(row&&row.created_at, row&&row.timestamp, row&&row.ts, row&&row.started_at, row&&row.completed_at);
  const id=tool.id||row.tool_call_id||payload.tid||payload.id||payload.tool_call_id||payload.tool_use_id||payload.call_id||'';
  const settled=!!(opts&&opts.settled);
  return {
    name:tool.name||payload.name||'tool',
    args:(tool.args&&typeof tool.args==='object')?tool.args:((payload.args&&typeof payload.args==='object')?payload.args:{}),
    command:tool.command||payload.command||payload.cmd||'',
    raw_command:tool.raw_command||payload.raw_command||'',
    preview:tool.preview||payload.preview||'',
    snippet:tool.snippet||payload.snippet||payload.result||payload.output||(
      row&&row.status!=='running'&&row.status!=='pending'?row.text:''
    )||'',
    done:settled?true:(tool.done!==null&&tool.done!==undefined?tool.done:(row.status!=='running'&&row.status!=='pending')),
    is_error:!!(tool.is_error||payload.is_error||row.status==='error'||row.status==='failed'),
    duration:tool.duration||payload.duration||payload.duration_seconds,
    started_at:firstValidTimestampSeconds(tool.started_at, payload.started_at, rowTs),
    created_at:firstValidTimestampSeconds(tool.created_at, payload.created_at, rowTs),
    timestamp:firstValidTimestampSeconds(tool.timestamp, payload.timestamp, rowTs),
    ts:firstValidTimestampSeconds(
      tool.ts,
      payload.ts,
      tool.timestamp,
      payload.timestamp,
      tool.created_at,
      payload.created_at,
      rowTs
    ),
    tid:id,
    id,
  };
}
function _anchorSceneRowTimestampSeconds(row){
  if(!row) return null;
  const timestampSeconds=typeof _timestampSeconds==='function'?_timestampSeconds:function(value){
    const stamp=Number(value);
    return Number.isFinite(stamp)&&stamp>0?(stamp>1e12?stamp/1000:stamp):null;
  };
  for(const key of ['created_at','timestamp','ts','started_at','completed_at']){
    const stamp=timestampSeconds(row[key]);
    if(stamp) return stamp;
  }
  return null;
}
function _anchorSceneNodeForRow(row, opts){
  const settled=!!(opts&&opts.settled);
  if(!row) return null;
  let node=null;
  if(row.role==='prose'){
    const text=String(row.text||'').trim();
    if(!text) return null;
    // Incremental live rendering: reuse a persistent smd node fed only the delta
    // instead of re-parsing the whole growing answer on every streamed frame
    // (O(n^2) -> O(n)). Settled rows and any failure fall through to the full
    // renderMd path below, which stays the source of truth for the final DOM.
    const proseKey=row.local_id||row.row_id||'';
    if(!settled && proseKey && typeof window.__anchorProseIncrementalNode==='function'){
      const inc=window.__anchorProseIncrementalNode(proseKey,text);
      // Route the incremental node through the shared row-decoration block below
      // (data-anchor-scene-row / -row-id / -row-role / -source-event-type) instead
      // of returning early — otherwise live incremental prose rows lose the
      // identity attributes the scene reconciler matches on. (Codex gate #5466)
      if(inc){ node=inc; }
    }
    if(!node){
      node=document.createElement('div');
      node.className='assistant-segment';
      node.setAttribute('data-anchor-scene-prose','1');
      node.dataset.rawText=text;
      node.innerHTML=`<div class="msg-body">${renderMd?renderMd(text):esc(text)}</div>`;
    }
  }else if(row.role==='thinking'){
    if(window._showThinking===false) return null;
    const text=String(row.text||row.thinking&&row.thinking.text||'').trim();
    if(!text) return null;
    node=_thinkingActivityNode(text, false, row.row_id||row.local_id||'anchor-thinking');
  }else if(row.role==='tool'){
    node=buildToolCard(_anchorSceneToolCallFromRow(row,opts));
  }else if(row.role==='lifecycle'){
    if(row.source_event_type==='compressing'||row.source_event_type==='compressed'){
      node=_autoCompressionWorklogNode({
        phase:settled||row.source_event_type==='compressed'?'done':'running',
        automatic:true,
        message:row.text||'Compressing context',
      });
    }else{
      node=_activityStatusNode({
        kind:settled?'done':'waiting',
        label:row.text||row.status||'Working',
        status:!settled&&row.status==='running'?'running':'done',
        id:row.row_id||row.local_id||'',
      });
    }
  }else if(row.role==='control'){
    node=_activityStatusNode({
      kind:settled?'done':'waiting',
      label:row.text||row.source_event_type||'Waiting',
      status:settled?'done':'running',
      id:row.row_id||row.local_id||'',
    });
  }else if(row.role==='terminal'){
    const status=String(row.status||row.source_event_type||'').trim();
    const isError=['error','failed','connection_lost','interrupted','compression_exhausted','tool_limit_reached','no_response'].includes(status);
    node=_activityStatusNode({
      kind:isError?'warning':'done',
      label:row.text||status||'Turn ended',
      status:settled?'done':(isError?'error':'done'),
      id:row.row_id||row.local_id||'',
    });
  }
  if(!node) return null;
  node.setAttribute('data-anchor-scene-row','1');
  node.setAttribute('data-anchor-row-id',String(row.row_id||row.local_id||''));
  if(row.local_id) node.setAttribute('data-anchor-local-id',String(row.local_id));
  node.setAttribute('data-anchor-row-role',String(row.role||'activity'));
  node.setAttribute('data-anchor-source-event-type',String(row.source_event_type||''));
  return node;
}
function _anchorSceneTransparentNodeForRow(row, opts){
  const settled=!!(opts&&opts.settled);
  const live=!!(opts&&opts.live);
  if(!row) return null;
  let node=null;
  const eventTs=typeof _anchorSceneRowTimestampSeconds==='function'?_anchorSceneRowTimestampSeconds(row):null;
  const meta={
    segmentSeq:row.segment_seq||row.segmentSeq||'',
    burstId:row.activity_burst_id||row.burst_id||row.burstId||'',
  };
  if(row.role==='prose'){
    // The settled assistant segment already owns the FINAL answer prose, so a
    // prose row whose text matches the final answer must be suppressed here to
    // avoid duplicating the answer. But INTERMEDIATE progress prose (the
    // between-tool narration from earlier rounds) is NOT the final answer and
    // belongs in the chronological transparent history — dropping it loses the
    // interleaving the user saw live (#4568: tools were restored but mid-turn
    // prose still vanished on reload). Render intermediate prose as an inline
    // assistant-segment (same shape _anchorSceneNodeForRow builds), skip only
    // the final-answer duplicate.
    const text=String(row.text||'').trim();
    if(!text) return null;
    const finalAnswer=String((opts&&opts.finalAnswer)||'').trim();
    if(opts&&opts.liveTokenFinalPrefixEligible&&_anchorSceneLiveTokenFinalPrefix(row,text,finalAnswer)) return null;
    if(finalAnswer&&_anchorSceneProseMatchesFinalAnswer(text,finalAnswer)) return null;
    node=_anchorSceneNodeForRow(row,{settled});
    if(!node) return null;
    node=_decorateTransparentEventRow(node,{type:'prose',text,preview:text,...meta});
  }else if(row.role==='thinking'){
    if(window._showThinking===false) return null;
    const text=String(row.text||row.thinking&&row.thinking.text||'').trim();
    if(!text) return null;
    node=_decorateTransparentEventRow(_thinkingActivityNode(text,false,row.row_id||row.local_id||'anchor-thinking'),{
      type:'thinking',
      text,
      preview:text,
      ts:eventTs,
      ...meta,
      live,
    });
  }else if(row.role==='tool'){
    const toolCall=_anchorSceneToolCallFromRow(row,{settled});
    node=_decorateTransparentEventRow(buildToolCard(toolCall),{
      type:'tool',
      name:toolCall&&toolCall.name,
      status:_transparentToolStatus(toolCall,settled),
      toolCall,
      ts:eventTs,
      ...meta,
      live,
      settled,
    });
  }else{
    node=_anchorSceneNodeForRow(row,{settled});
    node=_decorateTransparentEventRow(node,{
      type:String(row.role||'activity'),
      ...meta,
      live,
    });
  }
  if(!node) return null;
  node.setAttribute('data-anchor-scene-row','1');
  if(settled) node.setAttribute('data-anchor-settled-scene-row','1');
  if(live) node.setAttribute('data-anchor-live-scene-row','1');
  node.setAttribute('data-anchor-row-id',String(row.row_id||row.local_id||''));
  if(row.local_id) node.setAttribute('data-anchor-local-id',String(row.local_id));
  node.setAttribute('data-anchor-row-role',String(row.role||'activity'));
  node.setAttribute('data-anchor-source-event-type',String(row.source_event_type||''));
  if(opts&&opts.streamId) node.setAttribute('data-anchor-stream-id',String(opts.streamId));
  if(opts&&opts.sessionId) node.setAttribute('data-session-id',String(opts.sessionId));
  if(live) node.setAttribute('data-live-stream-owned','1');
  return node;
}
function _anchorSceneLiveTokenFinalPrefix(row, proseText, finalAnswer){
  if(!row||row.role!=='prose'||row.kind!=='process_prose') return false;
  if(String(row.source_event_type||'')!=='token') return false;
  if(!String(row.local_id||'').startsWith('live-prose:')) return false;
  const norm=(s)=>String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const rowKey=norm(proseText), finalKey=norm(finalAnswer);
  return !!(rowKey&&finalKey&&rowKey.length<finalKey.length&&finalKey.startsWith(rowKey));
}
function _anchorSceneLastNonTerminalWorkRowIndex(rows){
  if(!Array.isArray(rows)) return -1;
  return rows.reduce((last,row,idx)=>(row&&row.role==='tool')?idx:last,-1);
}
// Whitespace-insensitive compare so a scene prose row that IS the final answer
// (possibly re-wrapped) is recognized and not duplicated against the segment.
// Codex #4568: the prefix tolerance must NOT be able to suppress a DISTINCT
// intermediate progress row that merely happens to be a prefix of the final
// answer (e.g. "I found the issue and I'm applying the fix now." when the final
// answer starts with that sentence). So require exact normalized equality, with
// a prefix tolerance allowed ONLY when the two strings are near-equal length
// (>=0.9 ratio, shorter >=80 chars) — i.e. genuine re-wrap/truncation of the
// SAME text, never a short intermediate sentence that prefixes a long answer.
function _anchorSceneProseMatchesFinalAnswer(proseText, finalAnswer){
  const norm=(s)=>String(s||'').replace(/\s+/g,' ').trim();
  const a=norm(proseText), b=norm(finalAnswer);
  if(!a||!b) return false;
  if(a===b) return true;
  if(!(a.startsWith(b)||b.startsWith(a))) return false;
  const shorter=Math.min(a.length,b.length), longer=Math.max(a.length,b.length);
  return shorter>=80 && (shorter/longer)>=0.9;
}
function _anchorSceneWorklogGroup(blocks, opts){
  if(!blocks) return null;
  const live=!!(opts&&opts.live);
  const activityKey=(opts&&opts.activityKey)||'anchor-scene';
  let group=blocks.querySelector(`.tool-worklog-group[data-anchor-scene-owner="1"][data-tool-worklog-key="${CSS.escape(activityKey)}"]`);
  if(!group){
    group=ensureActivityGroup(blocks,{
      // Respect callers that need the settled activity group open. Round 6:
      // pinned followers keep the just-settled worklog open so STREAM_DONE does
      // not collapse hundreds of px of live worklog and visibly clamp the pane.
      collapsed:(opts&&opts.collapsed!==undefined)?opts.collapsed:!live,
      live,
      activityKey,
      beforeAnchor:!!(opts&&opts.beforeAnchor),
      anchor:(opts&&opts.anchor)||null,
      turnDuration:opts&&opts.turnDuration,
      turnStartedAt:opts&&opts.turnStartedAt,
      syncAnchorReason:false,
    });
  }
  if(!group) return null;
  group.setAttribute('data-anchor-scene-owner','1');
  if(live) group.setAttribute('data-live-anchor-scene-owner','1');
  group.setAttribute('data-tool-worklog-key',activityKey);
  if(opts&&opts.streamId) group.setAttribute('data-anchor-stream-id',String(opts.streamId));
  if(opts&&opts.turnDuration!==undefined&&opts.turnDuration!==null) group.setAttribute('data-turn-duration',String(opts.turnDuration));
  if(opts&&opts.turnStartedAt!==undefined&&opts.turnStartedAt!==null) group.setAttribute('data-turn-started-at',String(opts.turnStartedAt));
  return group;
}
function _renderAnchorSceneRowsIntoWorklog(group, rows, opts){
  const list=_toolWorklogListEl(group);
  if(!group||!list) return false;
  list.innerHTML='';
  let wrote=false;
  let currentTools=null;
  for(const row of rows){
    const node=_anchorSceneNodeForRow(row,opts);
    if(!node) continue;
    if(row.role==='tool'){
      if(!currentTools){
        currentTools=document.createElement('div');
        currentTools.className='wl-step-tools tool-worklog-tools';
        currentTools.setAttribute('data-worklog-tools','1');
        list.appendChild(currentTools);
      }
      currentTools.appendChild(node);
    }else{
      currentTools=null;
      list.appendChild(node);
    }
    wrote=true;
  }
  if(wrote){
    _syncToolCallGroupSummary(group);
  }
  return wrote;
}

export {
  _anchorSceneToolRowLogicalKey,
  _anchorSceneMergeToolRows,
  _anchorSceneRowsForRendering,
  _anchorSceneIsSettledSuccessfulCompression,
  _anchorSceneToolCallFromRow,
  _anchorSceneRowTimestampSeconds,
  _anchorSceneNodeForRow,
  _anchorSceneTransparentNodeForRow,
  _anchorSceneLiveTokenFinalPrefix,
  _anchorSceneLastNonTerminalWorkRowIndex,
  _anchorSceneProseMatchesFinalAnswer,
  _anchorSceneWorklogGroup,
  _renderAnchorSceneRowsIntoWorklog,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _anchorSceneToolRowLogicalKey: { enumerable: true, get: () => _anchorSceneToolRowLogicalKey, set: (value) => { _anchorSceneToolRowLogicalKey = value; } },
  _anchorSceneMergeToolRows: { enumerable: true, get: () => _anchorSceneMergeToolRows, set: (value) => { _anchorSceneMergeToolRows = value; } },
  _anchorSceneRowsForRendering: { enumerable: true, get: () => _anchorSceneRowsForRendering, set: (value) => { _anchorSceneRowsForRendering = value; } },
  _anchorSceneIsSettledSuccessfulCompression: { enumerable: true, get: () => _anchorSceneIsSettledSuccessfulCompression, set: (value) => { _anchorSceneIsSettledSuccessfulCompression = value; } },
  _anchorSceneToolCallFromRow: { enumerable: true, get: () => _anchorSceneToolCallFromRow, set: (value) => { _anchorSceneToolCallFromRow = value; } },
  _anchorSceneRowTimestampSeconds: { enumerable: true, get: () => _anchorSceneRowTimestampSeconds, set: (value) => { _anchorSceneRowTimestampSeconds = value; } },
  _anchorSceneNodeForRow: { enumerable: true, get: () => _anchorSceneNodeForRow, set: (value) => { _anchorSceneNodeForRow = value; } },
  _anchorSceneTransparentNodeForRow: { enumerable: true, get: () => _anchorSceneTransparentNodeForRow, set: (value) => { _anchorSceneTransparentNodeForRow = value; } },
  _anchorSceneLiveTokenFinalPrefix: { enumerable: true, get: () => _anchorSceneLiveTokenFinalPrefix, set: (value) => { _anchorSceneLiveTokenFinalPrefix = value; } },
  _anchorSceneLastNonTerminalWorkRowIndex: { enumerable: true, get: () => _anchorSceneLastNonTerminalWorkRowIndex, set: (value) => { _anchorSceneLastNonTerminalWorkRowIndex = value; } },
  _anchorSceneProseMatchesFinalAnswer: { enumerable: true, get: () => _anchorSceneProseMatchesFinalAnswer, set: (value) => { _anchorSceneProseMatchesFinalAnswer = value; } },
  _anchorSceneWorklogGroup: { enumerable: true, get: () => _anchorSceneWorklogGroup, set: (value) => { _anchorSceneWorklogGroup = value; } },
  _renderAnchorSceneRowsIntoWorklog: { enumerable: true, get: () => _renderAnchorSceneRowsIntoWorklog, set: (value) => { _renderAnchorSceneRowsIntoWorklog = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
