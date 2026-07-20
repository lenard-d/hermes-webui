import { _normalizeThinkingEchoCompare } from './activity-and-scroll.js';
import { _renderSettledAnchorSceneForMessage, ensureActivityGroup } from './anchor-scenes.js';
import { _decorateTransparentEventRow, _restoreWorklogDetailDisclosureState, _syncTransparentEventControls, _thinkingActivityNode, _transparentToolStatus, isTransparentStream } from './activity-presentation.js';
import { _assistantTurnBlocks } from './assistant-turn-presentation.js';
import { _assistantToolAnchorIdxForMessage, _cliPatchSnippetFromArgs, _cliToolCardHasDiffSnippet, _cliToolCardSnippet, _cliToolResultSnippet, _toolArgsSnapshot } from './cli-tool-presentation.js';
import { _legacySettledFallbackHasToolMetadata } from './render-support.js';
import { S } from './state.js';
import { buildToolCard } from './tool-card-presentation.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './worklog-tool-groups.js';
import { _appendWorklogStep } from './worklog-step-presentation.js';
import { _worklogReasonHtmlFromAnchor } from './worklog-reasoning.js';

// Rebuilds durable Activity/Worklog history from persisted messages and tool
// metadata after the transcript rows have been recreated. This module owns the
// fallback normalization and chronological attachment rules as one interface.
function rebuildSettledActivity(context) {
  const {
    inner,
    virtualWindow,
    renderableRawIdxs,
    renderedRawIdxs,
    assistantSegments,
    assistantThinking,
    transparentOrderedToolIds,
    worklogDetailDisclosureState,
  }=context;
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
}

const compatibilityBindings=Object.freeze({});

export {
  rebuildSettledActivity,
  compatibilityBindings,
};
