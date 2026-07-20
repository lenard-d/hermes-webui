import { _ensureLiveActivityBaseline, _setActivityElapsedStartedAt, _startActivityElapsedTimer, scrollIfPinned } from './activity-and-scroll.js';
import { _messageUserUnpinned, _syncTransparentEventTimestamp } from './composer-controls.js';
import { _captureMessageScrollSnapshot, _moveLiveRunStatusToTurnEnd, _sessionHtmlCache, _transparentRevealKey, _transparentRevealedTurns } from './live-activity.js';
import { _attachCopyButton, _captureWorklogDetailDisclosureState, _decorateTransparentEventRow, _restoreWorklogDetailDisclosureState, _setTransparentCardOpen, _syncTransparentEventControls, _wireTransparentHeaderToggle, chatActivityMode, isCompactWorklogMode, isSimplifiedToolCalling, isTransparentStream } from './activity-presentation.js';
import { _assistantAnchorSceneFinalAnswerText, _assistantTurnBlocks, _createAssistantTurn, msgContent } from './assistant-turn-presentation.js';
import { _restoreMessageScrollSnapshotSameFrame } from './render-support.js';
import { $, S, esc } from './state.js';
import { _findLatestVisibleLiveAssistant, _findLatestVisibleLiveAssistantByBurst, _findLiveAssistantAnchorForSegment, _syncToolCallGroupSummary, _toolWorklogListEl } from './tool-worklog.js';
import { _activityKeyForLiveTurn, _anchorSceneLastNonTerminalWorkRowIndex, _anchorSceneRowTimestampSeconds, _anchorSceneRowsForRendering, _anchorSceneTransparentNodeForRow, _anchorSceneWorklogGroup, _copyActivityDisclosureState, _dedupeLiveProcessedWorklogAnchors, _liveActivityUserExpanded, _prepareLiveAnchorScrollRebuildGuard, _projectLiveAnchorActivitySceneForStream, _readActivityDisclosureState, _renderAnchorSceneRowsIntoWorklog, _resetMismatchedLiveAssistantTurnForSession, _syncWorklogReasonFromAnchor, ensureLiveWorklogContainer, isLiveAnchorActivitySceneOwner } from './transparent-worklog.js';
import { compatibilityBindings as transparentWorklogBindings } from './transparent-worklog.js';

function renderLiveAnchorActivityScene(streamId, scene, opts){
  opts=opts||{};
  const requestedMode=opts.mode;
  const activeMode=chatActivityMode();
  // The USER's active activity-display mode is authoritative for what gets
  // painted. `requestedMode` (opts.mode) is only a fallback hint from callers
  // that hardcode {mode:'compact_worklog'} (appendLiveToolCard / ensureLiveWorklogShell
  // / appendLiveCompressionCard, etc.) — it must NEVER override the active mode, or a
  // transparent_stream turn gets a compact grouped-worklog frame forced onto it. That
  // regressed #5942 (grouped↔individual alternating) + #5943 (per-tick row rebuild /
  // flicker) when #5746's requestedMode-precedence landed: the good build always
  // checked isTransparentStream() FIRST and ignored the hint. Restore active-mode-wins:
  // honor requestedMode ONLY when there is no usable active mode.
  const knownMode=(m)=>m==='compact_worklog'||m==='transparent_stream'||m==='hide_all_activity';
  const sceneMode=knownMode(activeMode)?activeMode:(knownMode(requestedMode)?requestedMode:activeMode);
  if(sceneMode==='hide_all_activity') return false;
  const existingTurn=$('liveAssistantTurn');
  const requestedSessionId=String(opts.sessionId||'');
  const existingTurnSessionId=String(existingTurn&&existingTurn.dataset&&existingTurn.dataset.sessionId||'');
  if(existingTurn&&requestedSessionId&&existingTurnSessionId&&existingTurnSessionId!==requestedSessionId){
    if(!_resetMismatchedLiveAssistantTurnForSession(existingTurn, requestedSessionId)) return false;
  }
  if(sceneMode==='transparent_stream'){
    return _renderLiveAnchorActivitySceneTransparent(streamId,scene,opts);
  }
  if(typeof isSimplifiedToolCalling==='function'&&!isSimplifiedToolCalling()) return false;
  if(sceneMode!=='compact_worklog') return false;
  if(!S.session||!S.activeStreamId) return false;
  if(opts.sessionId&&S.session.session_id!==opts.sessionId) return false;
  if(streamId&&S.activeStreamId!==streamId) return false;
  const rows=_anchorSceneRowsForRendering(scene,{settled:false});
  $('emptyState').style.display='none';
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    $('msgInner').appendChild(turn);
  }
  turn.setAttribute('data-anchor-scene-live-owner','1');
  turn.setAttribute('data-anchor-stream-id',String(streamId||''));
  // Re-stamp when reusing a turn restored or previously rendered in another mode.
  if(S.session) turn.dataset.sessionId=S.session.session_id;
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return false;
  const liveDisclosureState=typeof _captureWorklogDetailDisclosureState==='function'
    ? _captureWorklogDetailDisclosureState(blocks)
    : null;
  const scrollSnapshot=_captureMessageScrollSnapshot();
  const scrollRebuildGuard=_prepareLiveAnchorScrollRebuildGuard(scrollSnapshot);
  blocks.querySelectorAll('[data-anchor-scene-owner="1"],[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  blocks.querySelectorAll('.live-worklog[data-live-worklog-shell="1"],.tool-worklog-group[data-live-tool-call-group="1"],.tool-call-group[data-live-tool-call-group="1"],.tool-card-row[data-live-tid]:not(.transparent-event-row),.agent-activity-thinking[data-live-thinking="1"],.interim-collapse-toggle').forEach(el=>el.remove());
  blocks.querySelectorAll('[data-live-assistant="1"]').forEach(el=>{
    el.classList.add('assistant-segment-worklog-source');
    el.setAttribute('aria-hidden','true');
    el.hidden=true;
  });
  const group=_anchorSceneWorklogGroup(blocks,{
    live:true,
    collapsed:false,
    activityKey:`live:${streamId||S.activeStreamId||'anchor'}`,
    streamId:streamId||S.activeStreamId||'',
    turnStartedAt:S.session&&S.session.pending_started_at,
  });
  const ok=_renderAnchorSceneRowsIntoWorklog(group,rows,{live:true,settled:false});
  if(!ok){
    const list=_toolWorklogListEl(group);
    if(list) list.innerHTML='';
    _syncToolCallGroupSummary(group);
  }
  if(typeof _restoreWorklogDetailDisclosureState==='function') _restoreWorklogDetailDisclosureState(blocks, liveDisclosureState);
  if(typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(group);
  _dedupeLiveProcessedWorklogAnchors(turn);
  if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
  if(scrollRebuildGuard&&scrollRebuildGuard.release){
    requestAnimationFrame(()=>{
      scrollRebuildGuard.release();
      // Only re-restore the unpinned snapshot if the reader is STILL unpinned at
      // rAF time. If they re-pinned between guard-engage and this frame, the
      // stale re-restore would yank them back off the bottom (Opus gate finding).
      if(_messageUserUnpinned) _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
    });
  }
  if(!scrollRebuildGuard.readerAwayFromBottom&&typeof scrollIfPinned==='function') scrollIfPinned();
  return true;
}
function _renderLiveAnchorActivitySceneTransparent(streamId, scene, opts){
  opts=opts||{};
  if(!S.session||!S.activeStreamId) return false;
  if(opts.sessionId&&S.session.session_id!==opts.sessionId) return false;
  if(streamId&&S.activeStreamId!==streamId) return false;
  const rows=_anchorSceneRowsForRendering(scene,{settled:false});
  if(!rows.length) return false;
  $('emptyState').style.display='none';
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    $('msgInner').appendChild(turn);
  }
  turn.setAttribute('data-anchor-scene-live-owner','1');
  turn.setAttribute('data-anchor-stream-id',String(streamId||''));
  turn.setAttribute('data-live-assistant-turn','1');
  if(S.session) turn.dataset.sessionId=S.session.session_id;
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return false;
  const scrollSnapshot=_captureMessageScrollSnapshot();
  const scrollRebuildGuard=_prepareLiveAnchorScrollRebuildGuard(scrollSnapshot);
  const activeStreamId = String(streamId || S.activeStreamId || '');
  const activeSessionId = String(S.session && S.session.session_id || '');
  const preserveByKey = new Map();
  blocks.querySelectorAll('.transparent-event-row[data-live-stream-owned="1"][data-anchor-row-id]').forEach(node=>{
    if(!node||!node.getAttribute) return;
    const rowStream = String(node.getAttribute('data-anchor-stream-id') || '');
    if(rowStream && rowStream !== activeStreamId) return;
    const rowSession = String(node.getAttribute('data-session-id') || '');
    if(rowSession && activeSessionId && rowSession !== activeSessionId) return;
    const key = _transparentLiveRowKey(node, activeStreamId);
    if(key && !preserveByKey.has(key)) preserveByKey.set(key, node);
  });
  blocks.querySelectorAll('[data-anchor-scene-owner="1"]').forEach(el=>el.remove());
  blocks.querySelectorAll('[data-anchor-scene-row="1"]').forEach(el=>{
    if(el.getAttribute('data-live-stream-owned') === '1'){
      const key = _transparentLiveRowKey(el, activeStreamId);
      if(key && preserveByKey.get(key) === el) return;
    }
    el.remove();
  });
  // Clear every legacy live activity surface this renderer can replace. The
  // anchor-scene rows are now the source of truth for visible live activity.
  blocks.querySelectorAll(
    '.live-worklog[data-live-worklog-shell="1"],'+
    '.tool-worklog-group[data-live-tool-call-group="1"],'+
    '.tool-call-group[data-live-tool-call-group="1"],'+
    '.tool-card-row[data-live-tid],'+
    '.agent-activity-thinking[data-live-thinking="1"],'+
    '.transparent-event-row[data-live-tid],'+
    '.interim-collapse-toggle'
  ).forEach(el=>el.remove());
  // Match the compact path: keep legacy live segments as hidden anchors so
  // stream-owned metadata survives while the anchor scene owns visible activity.
  blocks.querySelectorAll('[data-live-assistant="1"]').forEach(el=>{
    el.classList.add('assistant-segment-worklog-source');
    el.setAttribute('aria-hidden','true');
    el.hidden=true;
  });
  const liveFooter=blocks.querySelector('#liveRunStatus');
  const renderedRows=[];
  for(const row of rows){
    const rowEventTs=typeof _anchorSceneRowTimestampSeconds==='function'?_anchorSceneRowTimestampSeconds(row):null;
    const node=_anchorSceneTransparentNodeForRow(row,{
      live:true,
      settled:false,
      streamId:streamId||S.activeStreamId||'',
      sessionId:S.session&&S.session.session_id,
    });
    if(!node) continue;
    const key = _transparentLiveRowKey(node, activeStreamId);
    const existing = key ? preserveByKey.get(key) : null;
    const renderedNode = existing && _transparentLiveRowsCompatible(existing, node)
      ? _refreshTransparentLiveRow(existing, node, {
        preserveEventAt:!rowEventTs&&existing.getAttribute?existing.getAttribute('data-event-at'):null,
      })
      : node;
    if(existing) preserveByKey.delete(key);
    if(!renderedNode) continue;
    renderedRows.push(renderedNode);
  }
  const transparentLiveRowAlreadyPositioned=(node, expectedNextSibling)=>!!(
    node &&
    node.parentElement===blocks &&
    node.nextSibling===expectedNextSibling
  );
  let expectedNextSibling=(liveFooter&&liveFooter.parentElement===blocks) ? liveFooter : null;
  for(let i=renderedRows.length-1;i>=0;i--){
    const renderedNode=renderedRows[i];
    if(transparentLiveRowAlreadyPositioned(renderedNode,expectedNextSibling)){
      expectedNextSibling=renderedNode;
      continue;
    }
    if(expectedNextSibling&&expectedNextSibling.parentElement===blocks) blocks.insertBefore(renderedNode,expectedNextSibling);
    else blocks.appendChild(renderedNode);
    expectedNextSibling=renderedNode;
  }
  preserveByKey.forEach(stale=>stale.remove());
  if(renderedRows.length) _syncTransparentEventControls(turn);
  if(typeof _moveLiveRunStatusToTurnEnd==='function') _moveLiveRunStatusToTurnEnd();
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
  if(scrollRebuildGuard&&scrollRebuildGuard.release){
    requestAnimationFrame(()=>{
      scrollRebuildGuard.release();
      if(_messageUserUnpinned) _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
    });
  }
  if(!scrollRebuildGuard.readerAwayFromBottom&&typeof scrollIfPinned==='function') scrollIfPinned();
  return !!renderedRows.length;
}

function _transparentLiveRowKey(node, streamId){
  if(!node || !node.getAttribute) return '';
  const rowId = String(node.getAttribute('data-anchor-row-id') || '').trim();
  if(!rowId) return '';
  const rowStreamId = String(streamId || node.getAttribute('data-anchor-stream-id') || '').trim();
  const rowRole = String(node.getAttribute('data-anchor-row-role') || 'activity').trim();
  const rowSource = String(node.getAttribute('data-anchor-source-event-type') || '').trim();
  return `${rowStreamId}\u0000${rowId}\u0000${rowRole}\u0000${rowSource}`;
}

function _transparentLiveRowsCompatible(existing, candidate){
  if(!existing || !candidate) return false;
  return !!(
    existing.getAttribute('data-anchor-row-id') === candidate.getAttribute('data-anchor-row-id') &&
    existing.getAttribute('data-anchor-row-role') === candidate.getAttribute('data-anchor-row-role') &&
    existing.getAttribute('data-anchor-source-event-type') === candidate.getAttribute('data-anchor-source-event-type')
  );
}

function _transparentLiveRowAttributePairs(node){
  if(!node) return [];
  if(typeof node.getAttributeNames === 'function'){
    return node.getAttributeNames().map(name=>[name, node.getAttribute(name)]);
  }
  const attrs = node.attributes;
  if(!attrs || typeof attrs !== 'object') return [];
  if(typeof attrs.length === 'number'){
    const pairs = [];
    for(let i=0;i<attrs.length;i++){
      const attr = typeof attrs.item === 'function' ? attrs.item(i) : attrs[i];
      if(!attr || !attr.name) continue;
      pairs.push([attr.name, attr.value]);
    }
    return pairs;
  }
  return Object.keys(attrs).map(name=>[name, attrs[name]]);
}

function _transparentLiveRowInteractiveState(row){
  const card = row&&row.querySelector ? row.querySelector('.tool-card,.thinking-card') : null;
  const detail = row&&row.querySelector ? row.querySelector('.tool-card-detail') : null;
  return {
    expanded: !!((card&&card.classList&&card.classList.contains('open')) || (row&&row.getAttribute&&row.getAttribute('data-expanded')==='1')),
    detailMode: detail&&detail.getAttribute ? String(detail.getAttribute('data-transparent-detail-mode') || '') : '',
  };
}

function _rehydrateTransparentLiveRow(existing, node, preservedState){
  if(!existing) return;
  if(node && Object.prototype.hasOwnProperty.call(node, '_tcData')) existing._tcData = node._tcData;
  else if(Object.prototype.hasOwnProperty.call(existing, '_tcData')) delete existing._tcData;
  try{ delete node._tcData; }catch(_){}
  const header = existing.querySelector ? existing.querySelector('.tool-card-header,.thinking-card-header') : null;
  if(header){
    if(typeof _wireTransparentHeaderToggle === 'function') _wireTransparentHeaderToggle(header);
    if(typeof _attachCopyButton === 'function') _attachCopyButton(header);
  }
  const card = existing.querySelector ? existing.querySelector('.tool-card,.thinking-card') : null;
  if(card){
    if(typeof _setTransparentCardOpen === 'function') _setTransparentCardOpen(card, !!(preservedState&&preservedState.expanded));
    else if(card.classList&&typeof card.classList.toggle === 'function') card.classList.toggle('open', !!(preservedState&&preservedState.expanded));
  }
  const detail = existing.querySelector ? existing.querySelector('.tool-card-detail') : null;
  if(detail && preservedState && preservedState.detailMode){
    detail.setAttribute('data-transparent-detail-mode', preservedState.detailMode);
    detail.querySelectorAll('.transparent-detail-mode').forEach(el=>{
      const mode = String(el.getAttribute('data-mode') || '');
      if(el.classList && typeof el.classList.toggle === 'function') el.classList.toggle('active', mode===preservedState.detailMode);
    });
  }
}

function _refreshTransparentThinkingLiveRow(existing, node){
  if(!existing || !node || !existing.querySelector || !node.querySelector) return false;
  const existingType = String(existing.getAttribute('data-event-type') || '');
  const nodeType = String(node.getAttribute('data-event-type') || '');
  const existingIsThinking = existingType === 'thinking' || (existing.classList&&existing.classList.contains('transparent-thinking-event'));
  const nodeIsThinking = nodeType === 'thinking' || (node.classList&&node.classList.contains('transparent-thinking-event'));
  if(!existingIsThinking || !nodeIsThinking) return false;
  const existingPre = existing.querySelector('.thinking-card-body pre');
  const nodePre = node.querySelector('.thinking-card-body pre');
  if(!existingPre || !nodePre) return false;
  const nextText = String(nodePre.textContent || '');
  if(existingPre.textContent !== nextText) existingPre.textContent = nextText;
  const nodePreview = node.querySelector('.transparent-event-thinking-preview');
  const previewText = nodePreview ? String(nodePreview.textContent || '') : nextText;
  if(typeof _decorateTransparentEventRow === 'function'){
    const nodeStampSource = node.getAttribute ? String(node.getAttribute('data-event-at-source') || '') : '';
    const existingStamp = existing.getAttribute ? existing.getAttribute('data-event-at') : null;
    const nodeStamp = node.getAttribute ? node.getAttribute('data-event-at') : null;
    const nextStamp = nodeStampSource === 'event'
      ? (nodeStamp || existingStamp)
      : (existingStamp || nodeStamp);
    _decorateTransparentEventRow(existing,{
      type:'thinking',
      text:nextText,
      preview:previewText,
      ts:nextStamp||undefined,
      live:true,
    });
  }
  return true;
}

function _bindTransparentFadeCleanup(body){
  if(!body || body._transparentFadeCleanupBound || typeof body.addEventListener !== 'function') return;
  body._transparentFadeCleanupBound = true;
  body.addEventListener('animationend', e=>{
    const span = e.target;
    if(!span || !span.classList || !span.classList.contains('stream-fade-word')) return;
    span.replaceWith(document.createTextNode(span.textContent || ''));
  });
}

function _appendTransparentFadeText(body, text){
  if(!body) return;
  const value = String(text || '');
  if(!value) return;
  _bindTransparentFadeCleanup(body);
  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const frag = document.createDocumentFragment();
  const wordRe = /(\S+)(\s*)/g;
  let last = 0, match, changed = false;
  while((match = wordRe.exec(value))){
    if(match.index > last) frag.appendChild(document.createTextNode(value.slice(last, match.index)));
    if(reduceMotion){
      frag.appendChild(document.createTextNode(match[1]));
    }else{
      const span = document.createElement('span');
      span.className = 'stream-fade-word is-new';
      span.textContent = match[1];
      frag.appendChild(span);
    }
    if(match[2]) frag.appendChild(document.createTextNode(match[2]));
    last = match.index + match[0].length;
    changed = true;
  }
  if(!changed) frag.appendChild(document.createTextNode(value));
  else if(last < value.length) frag.appendChild(document.createTextNode(value.slice(last)));
  body.appendChild(frag);
}

function _refreshTransparentFadeProseRow(existing, node, preservedState){
  let body = existing.querySelector ? existing.querySelector('.msg-body') : null;
  const nextText = String((node.dataset && node.dataset.rawText) || (node.textContent || ''));
  const currentText = String(existing.getAttribute('data-stream-fade-text') || (body && body.textContent) || '');
  const pairs = _transparentLiveRowAttributePairs(node);
  const kept = Object.create(null);
  for(const pair of pairs){
    const [name, value] = pair;
    kept[String(name)] = String(value ?? '');
  }
  for(const [name] of _transparentLiveRowAttributePairs(existing)){
    if(!Object.prototype.hasOwnProperty.call(kept, name)) existing.removeAttribute(name);
  }
  for(const pair of pairs){
    const [name, value] = pair;
    existing.setAttribute(name, value);
  }
  existing.className = node.className || '';
  if(!body){
    body = document.createElement('div');
    body.className = 'msg-body';
    existing.appendChild(body);
  }
  if(body.classList) body.classList.add('stream-fade-active');
  if(!nextText.startsWith(currentText)){
    body.textContent = '';
    existing.setAttribute('data-stream-fade-text', '');
    _appendTransparentFadeText(body, nextText);
  }else{
    _appendTransparentFadeText(body, nextText.slice(currentText.length));
  }
  existing.setAttribute('data-stream-fade-text', nextText);
  _rehydrateTransparentLiveRow(existing, node, preservedState);
  return existing;
}

function _refreshTransparentLiveRow(existing, node, opts){
  opts=opts||{};
  if(!existing || !node || !existing.getAttribute) return node;
  if(existing===node) return existing;
  const preservedState = _transparentLiveRowInteractiveState(existing);
  const candidateIsFadeProse = node.getAttribute('data-anchor-row-role') === 'prose' &&
    node.querySelector &&
    !!node.querySelector('.msg-body.stream-fade-active,.stream-fade-word');
  if(candidateIsFadeProse){
    return _refreshTransparentFadeProseRow(existing, node, preservedState);
  }
  const pairs = _transparentLiveRowAttributePairs(node);
  const kept = Object.create(null);
  for(const pair of pairs){
    const [name, value] = pair;
    kept[String(name)] = String(value ?? '');
  }
  for(const [name] of _transparentLiveRowAttributePairs(existing)){
    if(!Object.prototype.hasOwnProperty.call(kept, name)) existing.removeAttribute(name);
  }
  for(const pair of pairs){
    const [name, value] = pair;
    existing.setAttribute(name, value);
  }
  existing.className = node.className || '';
  if(_refreshTransparentThinkingLiveRow(existing, node)){
    _rehydrateTransparentLiveRow(existing, node, preservedState);
    return existing;
  }
  const newHtml = node.innerHTML || '';
  const htmlChanged = existing.innerHTML !== newHtml;
  if(htmlChanged) existing.innerHTML = newHtml;
  _rehydrateTransparentLiveRow(existing, node, preservedState);
  if(opts.preserveEventAt){
    const header = existing.querySelector ? existing.querySelector('.tool-card-header,.thinking-card-header') : null;
    if(header) _syncTransparentEventTimestamp(existing, header, {ts:opts.preserveEventAt, live:false});
  }
  return existing;
}
function _renderLiveAnchorActivitySceneForStream(streamId, sessionId, opts){
  const requestedMode=opts&&opts.mode;
  const activeMode=chatActivityMode();
  const mode=activeMode==='hide_all_activity'
    ? 'hide_all_activity'
    : (requestedMode==='compact_worklog'||requestedMode==='transparent_stream'||requestedMode==='hide_all_activity'
    ? requestedMode
    : activeMode);
  const scene=_projectLiveAnchorActivitySceneForStream(streamId,mode);
  if(!scene) return false;
  return renderLiveAnchorActivityScene(streamId,scene,{...(opts||{}),sessionId});
}
function _renderLiveAnchorActivitySceneSnapshotForStream(streamId, scene, sessionId, opts){
  if(!scene||scene.version!=='activity_scene_v1') return false;
  return renderLiveAnchorActivityScene(streamId,scene,{...(opts||{}),sessionId});
}
if(typeof window!=='undefined'){
  // Direct assignment, NOT a same-name wrapper: these are top-level `function`
  // declarations, which in a classic script are already window properties.
  // Re-exporting via `window.X = function(){ return X() }` reassigns that same
  // global property to the wrapper, so the inner call resolves to the wrapper
  // itself → infinite recursion (RangeError: Maximum call stack size exceeded)
  // on every live render / reattach / snapshot-restore path (#2715/#2771 class).
  window._renderLiveAnchorActivitySceneForStream=_renderLiveAnchorActivitySceneForStream;
  window._renderLiveAnchorActivitySceneSnapshotForStream=_renderLiveAnchorActivitySceneSnapshotForStream;
  window._projectLiveAnchorActivitySceneForStream=_projectLiveAnchorActivitySceneForStream;
  window.isLiveAnchorActivitySceneOwner=isLiveAnchorActivitySceneOwner;
}
function _anchorSceneSceneHasWorklogWorthyRows(scene){
  // Mirror of messages.js _anchorSceneHasWorklogWorthyRows for the RENDER side:
  // a settled scene that was persisted (or hydrated from the backend) before the
  // generation-side guard existed can still be all-prose. Such a scene must NOT be
  // promoted to a collapsed worklog at render time (it would hide the whole answer
  // and shrink the transcript at settle → bottom-pinned jump-back). Require at least
  // one tool/thinking/compression row. (defense-in-depth for already-persisted scenes)
  const rows=Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[];
  for(const row of rows){
    if(!row||typeof row!=='object') continue;
    const role=String(row.role||'');
    if(role==='tool'||role==='thinking') return true;
    if(role==='lifecycle'){
      const source=String(row.source_event_type||'');
      if(source==='compressing'||source==='compressed') return true;
    }
  }
  return false;
}
// #5941: an errored/failed turn's terminal_state. A turn that ended in a
// provider/agent failure but which DID produce assistant content (tool calls,
// reasoning) still folds that content into a collapsed worklog above the error
// card — so the user reads a lone error bubble as "nothing came back", even
// though the real response is one click away. These are the terminal states
// that must keep the produced content VISIBLE by default. `completed` (normal
// turn) and null are deliberately excluded, and `cancelled`/`interrupted`
// (user-initiated stops with their own dedicated cards + #5224 transcript
// preservation) are left to their existing behavior — this is scoped to the
// error/failure family the report is about.
const _ANCHOR_SCENE_ERRORED_TERMINAL_STATES=new Set([
  'error','no_response','degraded','connection_lost','tool_limit_reached','compression_exhausted',
]);
function _anchorSceneHasErroredTerminalState(scene){
  const state=String(scene&&scene.terminal_state||'').trim().toLowerCase();
  return _ANCHOR_SCENE_ERRORED_TERMINAL_STATES.has(state);
}
function _renderSettledAnchorSceneTransparentForMessage(message, segment, rawIdx){
  if(!message||!message._anchor_activity_scene||!segment) return false;
  if(!_anchorSceneSceneHasWorklogWorthyRows(message._anchor_activity_scene)) return false;
  const blocks=_assistantTurnBlocks(segment.closest('.assistant-turn'));
  if(!blocks) return false;
  const scene=message._anchor_activity_scene;
  const rows=_anchorSceneRowsForRendering(scene,{settled:true});
  if(!rows.length) return false;
  const lastNonTerminalWorkRowIndex=_anchorSceneLastNonTerminalWorkRowIndex(rows);
  // The assistant segment owns the final answer; pass it so intermediate prose
  // rows render but the final-answer-duplicate prose row is suppressed.
  const finalAnswer=String(
    (scene&&typeof scene.final_answer==='string'&&scene.final_answer)
    || _assistantAnchorSceneFinalAnswerText(message)
    || (typeof msgContent==='function'?msgContent(message):'')
    || ''
  );
  blocks.querySelectorAll('[data-anchor-settled-scene-row="1"],.transparent-event-row[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  blocks.querySelectorAll('.transparent-earlier-steps[data-anchor-earlier-steps="1"]').forEach(el=>el.remove());
  blocks.querySelectorAll('.assistant-segment[data-msg-idx]').forEach(node=>{
    const idx=Number(node.getAttribute('data-msg-idx'));
    if(Number.isFinite(idx)&&idx<rawIdx){
      node.classList.add('assistant-segment-worklog-source');
      node.setAttribute('aria-hidden','true');
      node.hidden=true;
    }
  });
  // #5966: per-turn row cap. A reasoning-heavy settled turn can carry hundreds of
  // activity rows; rendering them all inline is the node-count half of the
  // Transparent-Stream memory blowup (detail-deferral above handles per-row
  // weight). Render only the last _TRANSPARENT_SETTLED_ROW_CAP rows and, when the
  // turn exceeds cap + slack, prepend a single "Show earlier steps (N)" affordance
  // that materializes the omitted prefix in place on click. Two exemptions keep
  // behavior identical where a cap would be wrong or unhelpful:
  //   • the JUST-SETTLED turn (its stream id matches the keep-open token) renders
  //     in full — capping it at STREAM_DONE would shrink the transcript and cause
  //     the backward-jump the keep-open token exists to prevent;
  //   • a turn already revealed this session (data flag) stays fully rendered
  //     across ordinary rebuilds / virtualize-out+in cycles.
  const turnEl=segment.closest('.assistant-turn');
  const streamId=String(message._anchor_stream_id||scene.stream_id||(scene.identity&&scene.identity.stream_id)||'');
  const justSettled=_shouldKeepSettledWorklogOpenForStreamSettle(streamId);
  // #5966 (Codex F3): revealed-state is authoritative from the persistent set
  // (survives cache round-trip / rebuild / switch-away), with the DOM flag as a
  // same-render fast path.
  const revealKey=_transparentRevealKey(S.session&&S.session.session_id, rawIdx);
  const alreadyRevealed=_transparentRevealedTurns.has(revealKey)
    || !!(turnEl&&turnEl.getAttribute('data-transparent-earlier-revealed')==='1');
  const cap=_TRANSPARENT_SETTLED_ROW_CAP;
  const slack=_TRANSPARENT_SETTLED_ROW_CAP_SLACK;
  let startIdx=0;
  if(!justSettled&&!alreadyRevealed&&rows.length>cap+slack){
    startIdx=rows.length-cap;
  }
  // Stash the TRUE tool-row count so "Trace: N tools" reflects the whole run even
  // while the prefix is capped; cleared on full reveal. (uncapped → remove it.)
  if(turnEl){
    if(startIdx>0){
      const totalTools=rows.filter(r=>String(r.role||'')==='tool').length;
      turnEl.setAttribute('data-transparent-total-tool-count',String(totalTools));
    }else{
      turnEl.removeAttribute('data-transparent-total-tool-count');
    }
  }
  const renderRowAt=(idx)=>{
    const row=rows[idx];
    const node=_anchorSceneTransparentNodeForRow(row,{settled:true,finalAnswer,liveTokenFinalPrefixEligible:idx>lastNonTerminalWorkRowIndex});
    if(!node) return null;
    // #5966 (Codex F2): stamp the OWNER message index so cache-round-trip recovery
    // resolves the correct scene in a multi-segment turn (the scene owner is often
    // NOT the turn's first assistant segment).
    node.setAttribute('data-anchor-owner-idx',String(rawIdx));
    if(segment.parentElement===blocks) blocks.insertBefore(node,segment);
    else blocks.appendChild(node);
    return node;
  };
  let wrote=false;
  // The "Show earlier steps" affordance sits ABOVE the retained rows (chronology:
  // the hidden steps came first). Insert it before rendering the retained tail so
  // it lands at the top of this turn's activity run.
  if(startIdx>0){
    const earlier=_buildTransparentEarlierStepsAffordance(startIdx);
    earlier.setAttribute('data-anchor-owner-idx',String(rawIdx));
    if(segment.parentElement===blocks) blocks.insertBefore(earlier,segment);
    else blocks.appendChild(earlier);
    // Reveal handler: materialize the omitted prefix in place, holding the reader's
    // viewport on the clicked affordance (insert grows content ABOVE it).
    earlier.addEventListener('click',()=>{
      _revealTransparentEarlierSteps(message,segment,rawIdx,earlier);
    });
    wrote=true;
  }
  for(let idx=startIdx;idx<rows.length;idx+=1){
    if(renderRowAt(idx)) wrote=true;
  }
  if(wrote){
    const turn=segment.closest('.assistant-turn');
    if(turn) _syncTransparentEventControls(turn);
  }
  return wrote;
}
// #5966 tunables. Cap chosen so a normal multi-tool turn (a handful to a couple
// dozen rows) is NEVER capped — only genuinely long reasoning runs are. Slack
// prevents a "Show 3 earlier steps" stub: only cap when the omitted prefix is
// worth its own row.
const _TRANSPARENT_SETTLED_ROW_CAP=30;
const _TRANSPARENT_SETTLED_ROW_CAP_SLACK=10;
// t() returns the key name itself for an unknown key, so `t(k)||literal` doesn't
// fall back. This resolves via t() only when the key is genuinely defined,
// otherwise uses the English literal — keeping the label correct before the
// locale keys are present in every bundle. (Fable UX i18n fast-follow.)
function _tOrDefault(key, literal, ...args){
  try{
    if(typeof t==='function'){
      const v=t(key, ...args);
      if(v && v!==key) return v;
    }
  }catch(_){ }
  return literal;
}
// A clean, in-flow affordance styled on the existing "Load earlier messages"
// pill (same visual language, so it reads as native). Shows the exact hidden
// count; the pill is the click target with a leading up-chevron.
function _buildTransparentEarlierStepsAffordance(hiddenCount){
  const el=document.createElement('div');
  el.className='transparent-earlier-steps';
  el.setAttribute('data-anchor-earlier-steps','1');
  el.setAttribute('data-anchor-scene-row','1');
  el.setAttribute('data-anchor-settled-scene-row','1');
  el.setAttribute('role','button');
  el.setAttribute('tabindex','0');
  el.setAttribute('data-earlier-count',String(hiddenCount));
  // i18n with English fallback, matching the sibling "Expand all"/"Collapse all"
  // controls' t() pattern. Keys live in the en locale (i18n.js); t() falls back to
  // en for other locales and to the key name if absent — so guard with a literal.
  const label=hiddenCount===1
    ? _tOrDefault('show_earlier_step_one','Show 1 earlier step')
    : _tOrDefault('show_earlier_steps','Show '+hiddenCount+' earlier steps',hiddenCount);
  el.setAttribute('aria-label',label);
  el.innerHTML=`<span class="transparent-earlier-steps-chevron">${li('chevron-up',13)}</span><span class="transparent-earlier-steps-label">${esc(label)}</span>`;
  el.addEventListener('keydown',(ev)=>{
    if(ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); el.click(); }
  });
  return el;
}
// Materialize the omitted prefix rows for a capped settled transparent turn,
// preserving the reader's viewport position (rows are inserted ABOVE the clicked
// affordance, so without compensation the content below would jump down).
function _revealTransparentEarlierSteps(message, segment, rawIdx, affordanceEl){
  const turnEl=segment.closest('.assistant-turn');
  // #5966 (Codex F3): record the reveal in the PERSISTENT set (survives rebuild /
  // switch-away / cache round-trip) and invalidate this session's cached HTML so
  // the stored markup isn't re-served stale-capped.
  const revealKey=_transparentRevealKey(S.session&&S.session.session_id, rawIdx);
  _transparentRevealedTurns.add(revealKey);
  try{
    const sid=S.session&&S.session.session_id;
    if(sid&&_sessionHtmlCache&&typeof _sessionHtmlCache.delete==='function') _sessionHtmlCache.delete(sid);
  }catch(_){ }
  if(turnEl){
    turnEl.setAttribute('data-transparent-earlier-revealed','1');
    // Full run now mounted → drop the capped-count stash so the Trace label
    // recomputes from the (now complete) DOM.
    turnEl.removeAttribute('data-transparent-total-tool-count');
  }
  const msgsEl=$('messages');
  const prevScrollTop=msgsEl?msgsEl.scrollTop:0;
  const prevScrollHeight=msgsEl?msgsEl.scrollHeight:0;
  const scene=message&&message._anchor_activity_scene;
  const blocks=_assistantTurnBlocks(turnEl);
  if(!scene||!blocks){ if(affordanceEl) affordanceEl.remove(); return; }
  const rows=_anchorSceneRowsForRendering(scene,{settled:true})||[];
  const lastNonTerminalWorkRowIndex=_anchorSceneLastNonTerminalWorkRowIndex(rows);
  const finalAnswer=String(
    (scene&&typeof scene.final_answer==='string'&&scene.final_answer)
    || _assistantAnchorSceneFinalAnswerText(message)
    || (typeof msgContent==='function'?msgContent(message):'')
    || ''
  );
  // The affordance's data-count tells us how many prefix rows to build (the rows
  // rendered on the initial pass are the tail after that index).
  const hidden=Number(affordanceEl&&affordanceEl.getAttribute('data-earlier-count'))||0;
  const stopIdx=hidden>0?hidden:_computeTransparentHiddenPrefixCount(rows);
  const frag=document.createDocumentFragment();
  for(let idx=0;idx<stopIdx;idx+=1){
    const node=_anchorSceneTransparentNodeForRow(rows[idx],{settled:true,finalAnswer,liveTokenFinalPrefixEligible:idx>lastNonTerminalWorkRowIndex});
    if(node){ node.setAttribute('data-earlier-revealed','1'); frag.appendChild(node); }
  }
  // Insert the prefix where the affordance sits, then drop the affordance.
  if(affordanceEl&&affordanceEl.parentElement===blocks){
    blocks.insertBefore(frag,affordanceEl);
    affordanceEl.remove();
  }else{
    blocks.appendChild(frag);
  }
  if(turnEl) _syncTransparentEventControls(turnEl);
  // Hold the reader's position: rows landed above the old affordance point, so
  // add the height delta to scrollTop (the app's own load-earlier idiom).
  if(msgsEl){
    const delta=msgsEl.scrollHeight-prevScrollHeight;
    msgsEl.scrollTop=prevScrollTop+delta;
  }
}
// The initial capped render omits rows[0 .. rows.length-cap-1]; recompute that
// prefix length from the current scene so the reveal is exact even if the count
// attribute is missing (cache round-trip).
function _computeTransparentHiddenPrefixCount(rows){
  const cap=_TRANSPARENT_SETTLED_ROW_CAP;
  const slack=_TRANSPARENT_SETTLED_ROW_CAP_SLACK;
  return (rows.length>cap+slack)?(rows.length-cap):0;
}
// One-shot token: the stream id of the turn that JUST settled at STREAM_DONE.
// The keep-open exception applies to ONLY this one turn's settled render, then
// is cleared so every other (historical) settled worklog renders compact even
// while the reader is pinned. Set right before the STREAM_DONE
// renderMessages({preserveScroll:true}) call and cleared after the settled-scene
// render pass; null at all other times.
let _keepSettledWorklogOpenForStreamId=null;
function _shouldKeepSettledWorklogOpenForStreamSettle(streamId){
  // Round 6 scroll-jump guard: collapsing the JUST-settled live worklog into a
  // compact summary at STREAM_DONE shrinks the transcript by hundreds of px. The
  // resulting backward jump hits readers in TWO positions, so keep that one
  // worklog open for BOTH on the settle render — the live->settled DOM swap is
  // then height-stable and there is no shrink for any scroll path to mishandle:
  //
  //  1. PINNED follower at the live tail: the shrink lowers scrollHeight, the
  //     browser clamps scrollTop to the new max, and the viewport snaps upward
  //     even though pin state is correct.
  //  2. UNPINNED reader who scrolled UP to read inside the just-settled turn
  //     (the mobile "往回大跳" report, #MOBILESCROLL follow-up): the worklog sits
  //     ABOVE their viewport, so collapsing it pulls their content up to the top
  //     of the turn. On desktop overflow-anchor:none + the JS snapshot restore
  //     keep them put, but on mobile the CSS resting value is overflow-anchor:
  //     auto AND _fixMobileScrollJank() flips an inline overflow-anchor:none over
  //     the settle render — which is exactly the wrong state: native anchoring is
  //     suppressed during the one frame the unpinned reader needs it to absorb
  //     the above-viewport shrink, so the content leaps to the turn's top. Keeping
  //     the worklog open removes the shrink entirely, which fixes it for every
  //     device/anchor-mode combination instead of fighting the anchor engine.
  //
  // SCOPING: the exception is gated on the one-shot token matching this turn's
  // stream id, so it applies ONLY to the turn that just settled — not to every
  // historical settled worklog on every re-render (which would defeat the
  // compact-worklog default for past turns).
  return !!(streamId&&_keepSettledWorklogOpenForStreamId===streamId);
}
// One-shot token set/clear API used by the STREAM_DONE handler (messages.js):
// arm the keep-open exception for exactly the turn that just settled, render,
// then disarm so subsequent re-renders collapse historical worklogs as normal.
function _armKeepSettledWorklogOpen(streamId){
  _keepSettledWorklogOpenForStreamId=streamId?String(streamId):null;
}
function _disarmKeepSettledWorklogOpen(){
  _keepSettledWorklogOpenForStreamId=null;
}
// True while a just-settled worklog is being force-rendered open (between
// _armKeepSettledWorklogOpen and _disarmKeepSettledWorklogOpen). renderMessages()
// consults this so it does NOT write the forced-open DOM into _sessionHtmlCache:
// the keep-open is a transient settle-frame device, and caching it would persist
// the forced-open worklog across session switches / restores, silently overriding
// a user-collapsed worklog. (#5260 gate-cert: keep-open must not leak into cache.)
function _isKeepSettledWorklogOpenArmed(){
  return _keepSettledWorklogOpenForStreamId!==null;
}
if(typeof window!=='undefined'){
  window._armKeepSettledWorklogOpen=_armKeepSettledWorklogOpen;
  window._disarmKeepSettledWorklogOpen=_disarmKeepSettledWorklogOpen;
}
function _renderSettledAnchorSceneForMessage(message, segment, rawIdx){
  if(!message||!message._anchor_activity_scene||!segment) return false;
  if(!_anchorSceneSceneHasWorklogWorthyRows(message._anchor_activity_scene)) return false;
  if(typeof isTransparentStream==='function'&&isTransparentStream()){
    return _renderSettledAnchorSceneTransparentForMessage(message,segment,rawIdx);
  }
  if(typeof isCompactWorklogMode==='function'&&!isCompactWorklogMode()) return false;
  const blocks=_assistantTurnBlocks(segment.closest('.assistant-turn'));
  if(!blocks) return false;
  const scene=message._anchor_activity_scene;
  const rows=_anchorSceneRowsForRendering(scene,{settled:true});
  if(!rows.length) return false;
  blocks.querySelectorAll('.assistant-segment[data-msg-idx]').forEach(node=>{
    const idx=Number(node.getAttribute('data-msg-idx'));
    if(Number.isFinite(idx)&&idx<rawIdx){
      node.classList.add('assistant-segment-worklog-source');
      node.setAttribute('aria-hidden','true');
      node.hidden=true;
    }
  });
  blocks.querySelectorAll('.tool-worklog-group:not([data-anchor-scene-owner="1"]),.tool-call-group:not([data-anchor-scene-owner="1"]),.agent-activity-thinking:not([data-anchor-scene-row="1"]),.wl-reason').forEach(el=>el.remove());
  const streamId=String(message._anchor_stream_id||scene.stream_id||scene.identity&&scene.identity.stream_id||'');
  const keepSettledWorklogOpen=_shouldKeepSettledWorklogOpenForStreamSettle(streamId);
  const activityKey=`anchor-scene:${rawIdx}`;
  if(streamId&&!_readActivityDisclosureState(activityKey)){
    _copyActivityDisclosureState(`live:${streamId}`, activityKey);
  }
  // #5941: an errored turn that produced assistant content (tool calls /
  // reasoning) must not hide that content behind a collapsed header — the user
  // reads a lone error card as "nothing came back". When the settled scene's
  // terminal_state is an error/failure (NOT a normal completion) keep the
  // worklog EXPANDED by default so the produced response stays visible. This
  // path is only reached for worklog-worthy scenes (the guard at the top
  // requires >=1 tool/thinking/compression row), so a genuinely-empty errored
  // turn — a real no_response with zero produced content — never gets here and
  // still shows only its error card, no phantom empty body. A user who has
  // explicitly collapsed THIS turn's worklog (saved 'closed' disclosure state)
  // is still respected, so the default-open never fights an intentional collapse.
  const erroredWorklogKeepOpen=_anchorSceneHasErroredTerminalState(scene)
    && _readActivityDisclosureState(activityKey)!=='closed';
  // keepSettledWorklogOpen forces collapsed:false for the ONE height-stable settle
  // render of the just-settled turn (no STREAM_DONE shrink jump) for both pinned
  // followers AND unpinned mid-turn readers. The keep-open is made genuinely
  // transient by the STREAM_DONE handler (messages.js): right after this render it
  // disarms the token and runs a scroll-PRESERVING collapse pass, so a worklog the
  // reader had manually collapsed returns to its copied disclosure state
  // (_copyActivityDisclosureState above) without the jump. While the token is
  // armed this forced-open DOM is also kept OUT of _sessionHtmlCache
  // (_isKeepSettledWorklogOpenArmed), so it never persists across restores.
  const group=_anchorSceneWorklogGroup(blocks,{
    live:false,
    collapsed:!(keepSettledWorklogOpen||erroredWorklogKeepOpen),
    beforeAnchor:true,
    anchor:segment,
    activityKey,
    streamId,
    turnDuration:message._turnDuration!==undefined&&message._turnDuration!==null?message._turnDuration:scene.turn_duration,
  });
  if(!group) return false;
  group.setAttribute('data-anchor-settled-scene-owner','1');
  // #5839: for a COLLAPSED settled worklog, defer building the row DOM until the
  // user first expands it. A reasoning-heavy turn can carry 80+ activity rows;
  // eagerly materializing them for every historical turn balloons the DOM and a
  // later synchronous layout (e.g. opening a dropdown) tips the tab into a
  // multi-GB freeze. The summary chip renders from data-turn-duration, not the
  // rows, so a deferred worklog still shows its "Processed in Xs" label. On
  // expand, _toggleActivityGroup materializes the stashed rows exactly once.
  const collapsed=group.classList.contains('tool-call-group-collapsed');
  if(collapsed){
    group._deferredWorklogRows=rows;
    group.setAttribute('data-worklog-rows-deferred','1');
    const list=_toolWorklogListEl(group);
    if(list) list.innerHTML='';
    _syncToolCallGroupSummary(group);
    return true;
  }
  group._deferredWorklogRows=null;
  group.removeAttribute('data-worklog-rows-deferred');
  return _renderAnchorSceneRowsIntoWorklog(group,rows,{settled:true});
}
function _syncLiveWorklogReasonsForAnchor(anchor, displayTextOverride){
  if(S.activeStreamId&&isLiveAnchorActivitySceneOwner(S.activeStreamId)) return;
  // Worklog reason-mirroring (folding intermediate prose into a top Worklog rail
  // and hiding the inline `assistant-segment` via `assistant-segment-worklog-source`
  // → display:none) is the Compact Worklog presentation (#3401). In Transparent
  // Stream mode prose must stay as visible, chronologically-placed inline segments
  // interleaved with tool rows — so do NOT build the worklog rail or hide the
  // inline segment here. Without this gate every round's prose mirror piles into
  // the single top rail while tool rows append at the bottom, so all prose bunches
  // above all tools during a live multi-round turn (#4096); it only self-heals when
  // the turn settles and renderMessages() rebuilds with the compact-only
  // `messageBelongsInWorklog` gate (which is already isCompactWorklogMode()-only).
  if(typeof isCompactWorklogMode==='function' && !isCompactWorklogMode()) return;
  if(!anchor||!anchor.matches||!anchor.matches('[data-live-assistant="1"]')) return;
  const blocks=anchor.parentElement;
  if(!blocks) return;
  const group=ensureLiveWorklogContainer(blocks,{
    activityKey:_activityKeyForLiveTurn(),
    anchor,
  });
  if(group) _syncWorklogReasonFromAnchor(group, anchor, displayTextOverride);
}
function _clearLiveActivityUserIntent(){
  transparentWorklogBindings._liveActivityUserExpanded = undefined;
}
function ensureActivityGroup(inner, opts){
  opts=opts||{};
  if(!inner) return null;
  const live=!!opts.live;
  const activityKey=opts.activityKey||(live?_activityKeyForLiveTurn():null);
  const burstId=opts.burstId!==undefined&&opts.burstId!==null?String(opts.burstId):'';
  const segmentSeq=opts.segmentSeq!==undefined&&opts.segmentSeq!==null?String(opts.segmentSeq):'';
  const liveSelectors=segmentSeq
    ? [
      `.tool-worklog-group[data-live-tool-worklog-group="1"][data-live-segment-seq="${CSS.escape(segmentSeq)}"]`,
      `.tool-call-group[data-live-tool-worklog-group="1"][data-live-segment-seq="${CSS.escape(segmentSeq)}"]`,
      `.tool-call-group[data-live-tool-call-group="1"][data-live-segment-seq="${CSS.escape(segmentSeq)}"]`,
    ]
    : burstId
    ? [
      `.tool-worklog-group[data-live-tool-worklog-group="1"][data-activity-burst-id="${CSS.escape(burstId)}"]`,
      `.tool-call-group[data-live-tool-worklog-group="1"][data-activity-burst-id="${CSS.escape(burstId)}"]`,
      `.tool-call-group[data-live-tool-call-group="1"][data-activity-burst-id="${CSS.escape(burstId)}"]`,
    ]
    : [
      '.tool-worklog-group[data-live-tool-worklog-group="1"][data-live-activity-current="1"]',
      '.tool-call-group[data-live-tool-worklog-group="1"][data-live-activity-current="1"]',
      '.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]',
    ];
  let group;
  if(live){
    if(activityKey){
      group=inner.querySelector(`.tool-worklog-group[data-tool-worklog-key="${CSS.escape(activityKey)}"],.tool-call-group[data-tool-worklog-key="${CSS.escape(activityKey)}"]`);
    }
    if(!group){
      for(const sel of liveSelectors){
        group=inner.querySelector(sel);
        if(group) break;
      }
    }
  }else{
    if(activityKey){
      group=inner.querySelector(`.tool-worklog-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-tool-worklog-key="${CSS.escape(activityKey)}"],.tool-call-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-tool-worklog-key="${CSS.escape(activityKey)}"]`);
    }
    if(!group&&segmentSeq){
      group=inner.querySelector(`.tool-worklog-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-live-segment-seq="${CSS.escape(segmentSeq)}"],.tool-call-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-live-segment-seq="${CSS.escape(segmentSeq)}"]`);
    }
    if(!group&&burstId){
      group=inner.querySelector(`.tool-worklog-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-activity-burst-id="${CSS.escape(burstId)}"],.tool-call-group[data-agent-activity-group="1"][data-tool-worklog-group="1"][data-activity-burst-id="${CSS.escape(burstId)}"]`);
    }
    if(!group&&activityKey){
      group=inner.querySelector(`.tool-worklog-group[data-tool-worklog-key="${CSS.escape(activityKey)}"],.tool-call-group[data-tool-worklog-key="${CSS.escape(activityKey)}"]`);
    }
    if(!group&&!activityKey){
      group=inner.querySelector('.tool-worklog-group[data-agent-activity-group="1"][data-tool-worklog-group="1"],.tool-call-group[data-agent-activity-group="1"][data-tool-worklog-group="1"],.tool-call-group[data-agent-activity-group="1"]:not([data-run-activity-group="1"])');
    }
  }
  if(!group && !activityKey && segmentSeq==="" && burstId){
    const candidates=live
      ? Array.from(inner.querySelectorAll('.tool-worklog-group[data-live-tool-worklog-group="1"],.tool-call-group[data-live-tool-worklog-group="1"],.tool-call-group[data-live-tool-call-group="1"]'))
      : Array.from(inner.querySelectorAll('.tool-worklog-group[data-agent-activity-group="1"],.tool-call-group[data-agent-activity-group="1"]:not([data-run-activity-group="1"])'));
    group=candidates.filter(el=>el.isConnected!==false).pop() || null;
  }
  if(!group){
    group=document.createElement('div');
    let collapsed=opts.collapsed!==false;
    if(window._worklogDetailsExpandedByDefault===true) collapsed=false;
    const savedState=_readActivityDisclosureState(activityKey);
    // Restore the user's explicit expand intent when recreating the live
    // activity group within the same turn (#1298), then let persisted chat/turn
    // state win across session switches and reloads. Saved closed-state should
    // override the default-expanded preference for settled groups the user has
    // explicitly collapsed.
    if(live && _liveActivityUserExpanded === true) collapsed=false;
    else if(live && _liveActivityUserExpanded === false) collapsed=true;
    if(live && savedState==='open') collapsed=false;
    else if(live && savedState==='closed') collapsed=true;
    group.className='agent-activity-group tool-worklog-group activity'+(collapsed?' tool-call-group-collapsed':'');
    group.setAttribute('data-tool-call-group','1');
    group.setAttribute('data-agent-activity-group','1');
    group.setAttribute('data-tool-worklog-group','1');
    group.setAttribute('data-tool-worklog-key',activityKey||'');
    if(activityKey) group.setAttribute('data-activity-disclosure-key',activityKey);
    if(live){
      group.setAttribute('data-live-tool-worklog-group','1');
      group.setAttribute('data-live-tool-call-group','1');
      group.setAttribute('data-live-activity-current','1');
    }
    if(burstId) group.setAttribute('data-activity-burst-id',burstId);
    if(segmentSeq) group.setAttribute('data-live-segment-seq',segmentSeq);
    group.classList.toggle('open',!collapsed);
    group.innerHTML=`<button type="button" class="tool-call-group-summary tool-worklog-summary activity-summary" aria-expanded="${collapsed?'false':'true'}" onclick="_toggleActivityGroup(this)"><span class="as-dot"></span><span class="tool-call-group-label tool-worklog-label as-text">Running</span><span class="tool-call-group-duration"></span><span class="tool-call-group-chevron as-caret">${li('chevron-right',12)}</span></button><div class="tool-call-group-body tool-worklog-body activity-body"><div class="worklog"><div class="tool-worklog-list"></div></div></div>`;
    const anchor=opts.anchor||null;
    if(anchor&&anchor.parentElement===inner){
      if(opts.beforeAnchor) inner.insertBefore(group, anchor);
      else anchor.insertAdjacentElement('afterend', group);
    }
    else inner.appendChild(group);
  }else if(activityKey&&!group.getAttribute('data-activity-disclosure-key')){
    group.setAttribute('data-activity-disclosure-key',activityKey);
  }
  if(burstId&&!group.getAttribute('data-activity-burst-id')) group.setAttribute('data-activity-burst-id',burstId);
  if(segmentSeq&&!group.getAttribute('data-live-segment-seq')) group.setAttribute('data-live-segment-seq',segmentSeq);
  if(!group.getAttribute('data-tool-worklog-key')&&activityKey) group.setAttribute('data-tool-worklog-key',activityKey);
  if(opts.turnDuration!==undefined&&opts.turnDuration!==null) group.setAttribute('data-turn-duration',String(opts.turnDuration));
  if(opts.turnStartedAt!==undefined&&opts.turnStartedAt!==null) group.setAttribute('data-turn-started-at',String(opts.turnStartedAt));
  const summary=group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
  if(summary){
    summary.removeAttribute('data-live-summary-static');
    summary.removeAttribute('aria-disabled');
    summary.disabled=false;
  }
  const anchor=opts.anchor||null;
  if(anchor&&anchor.parentElement===inner&&group.parentElement===inner){
    if(opts.beforeAnchor){
      if(group.nextElementSibling!==anchor) inner.insertBefore(group,anchor);
    }else if(group.previousElementSibling!==anchor){
      anchor.insertAdjacentElement('afterend',group);
    }
  }
  if(anchor&&opts.syncAnchorReason!==false) _syncWorklogReasonFromAnchor(group, anchor);
  _syncToolCallGroupSummary(group);
  return group;
}
function normalizeLiveActivityGroupPlacement(turn){
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  // Compact Worklog only: this reorders `.tool-call-group`/`.tool-worklog-group`
  // containers, which exist solely on the Compact Worklog live path. Transparent
  // Stream renders tool rows as flat `.transparent-event-row`s and never builds
  // these group containers (see appendLiveToolCard's transparent branch), and the
  // worklog prose-rail is gated off in transparent mode (#4096), so the selector
  // below matches nothing and this is a no-op there. Kept implicit (empty match)
  // rather than an early return so reconnect/restore behavior is unchanged.
  const groups=Array.from(
    blocks.querySelectorAll('.tool-worklog-group[data-live-tool-worklog-group="1"],.tool-call-group[data-live-tool-worklog-group="1"],.tool-call-group[data-live-tool-call-group="1"]')
  );
  groups.sort((a,b)=>{
    const as=Number(a.getAttribute('data-live-segment-seq'));
    const bs=Number(b.getAttribute('data-live-segment-seq'));
    if(Number.isFinite(as)&&Number.isFinite(bs)&&as!==bs) return as-bs;
    const av=Number(a.getAttribute('data-activity-burst-id'));
    const bv=Number(b.getAttribute('data-activity-burst-id'));
    if(Number.isFinite(av)&&Number.isFinite(bv)&&av!==bv) return av-bv;
    return 0;
  });
  for(const group of groups){
    const burstId=group.getAttribute('data-activity-burst-id')||'';
    const segmentSeq=group.getAttribute('data-live-segment-seq')||'';
    const anchor=segmentSeq
      ? _findLiveAssistantAnchorForSegment(blocks, segmentSeq)
      : burstId
      ? _findLatestVisibleLiveAssistantByBurst(blocks, burstId)
      : _findLatestVisibleLiveAssistant(blocks);
    if(!anchor) continue;
    if(anchor&&group.previousElementSibling!==anchor) anchor.insertAdjacentElement('afterend',group);
    _syncWorklogReasonFromAnchor(group, anchor);
  }
}
function ensureRunActivityGroup(inner, opts){
  opts=opts||{};
  if(!inner) return null;
  let group=inner.querySelector('.tool-call-group[data-run-activity-group="1"]');
  if(!group){
    group=document.createElement('div');
    const collapsed=opts.collapsed!==false;
    group.className='tool-call-group agent-activity-group run-activity-group'+(collapsed?' tool-call-group-collapsed':' open');
    group.setAttribute('data-tool-call-group','1');
    group.setAttribute('data-agent-activity-group','1');
    group.setAttribute('data-run-activity-group','1');
    group.innerHTML=`<button type="button" class="tool-call-group-summary" aria-expanded="${collapsed?'false':'true'}" onclick="_toggleActivityGroup(this)"><span class="tool-call-group-chevron">${li('chevron-right',12)}</span><span class="tool-call-group-label">Running</span><span class="tool-call-group-duration"></span></button><div class="tool-call-group-body"></div>`;
    if(inner.firstChild) inner.insertBefore(group, inner.firstChild);
    else inner.appendChild(group);
  }
  if(opts.turnDuration!==undefined&&opts.turnDuration!==null) group.setAttribute('data-turn-duration',String(opts.turnDuration));
  if(opts.turnStartedAt!==undefined&&opts.turnStartedAt!==null) group.setAttribute('data-turn-started-at',String(opts.turnStartedAt));
  _setActivityElapsedStartedAt(group);
  _ensureLiveActivityBaseline(group);
  _syncToolCallGroupSummary(group);
  if(opts.live!==false) _startActivityElapsedTimer(group);
  return group;
}


export {
  renderLiveAnchorActivityScene,
  _renderLiveAnchorActivitySceneTransparent,
  _transparentLiveRowKey,
  _transparentLiveRowsCompatible,
  _transparentLiveRowAttributePairs,
  _transparentLiveRowInteractiveState,
  _rehydrateTransparentLiveRow,
  _refreshTransparentThinkingLiveRow,
  _bindTransparentFadeCleanup,
  _appendTransparentFadeText,
  _refreshTransparentFadeProseRow,
  _refreshTransparentLiveRow,
  _renderLiveAnchorActivitySceneForStream,
  _renderLiveAnchorActivitySceneSnapshotForStream,
  _anchorSceneSceneHasWorklogWorthyRows,
  _anchorSceneHasErroredTerminalState,
  _renderSettledAnchorSceneTransparentForMessage,
  _tOrDefault,
  _buildTransparentEarlierStepsAffordance,
  _revealTransparentEarlierSteps,
  _computeTransparentHiddenPrefixCount,
  _shouldKeepSettledWorklogOpenForStreamSettle,
  _armKeepSettledWorklogOpen,
  _disarmKeepSettledWorklogOpen,
  _isKeepSettledWorklogOpenArmed,
  _renderSettledAnchorSceneForMessage,
  _syncLiveWorklogReasonsForAnchor,
  _clearLiveActivityUserIntent,
  ensureActivityGroup,
  normalizeLiveActivityGroupPlacement,
  ensureRunActivityGroup,
  _ANCHOR_SCENE_ERRORED_TERMINAL_STATES,
  _TRANSPARENT_SETTLED_ROW_CAP,
  _TRANSPARENT_SETTLED_ROW_CAP_SLACK,
  _keepSettledWorklogOpenForStreamId,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderLiveAnchorActivityScene: { enumerable: true, get: () => renderLiveAnchorActivityScene, set: (value) => { renderLiveAnchorActivityScene = value; } },
  _renderLiveAnchorActivitySceneTransparent: { enumerable: true, get: () => _renderLiveAnchorActivitySceneTransparent, set: (value) => { _renderLiveAnchorActivitySceneTransparent = value; } },
  _transparentLiveRowKey: { enumerable: true, get: () => _transparentLiveRowKey, set: (value) => { _transparentLiveRowKey = value; } },
  _transparentLiveRowsCompatible: { enumerable: true, get: () => _transparentLiveRowsCompatible, set: (value) => { _transparentLiveRowsCompatible = value; } },
  _transparentLiveRowAttributePairs: { enumerable: true, get: () => _transparentLiveRowAttributePairs, set: (value) => { _transparentLiveRowAttributePairs = value; } },
  _transparentLiveRowInteractiveState: { enumerable: true, get: () => _transparentLiveRowInteractiveState, set: (value) => { _transparentLiveRowInteractiveState = value; } },
  _rehydrateTransparentLiveRow: { enumerable: true, get: () => _rehydrateTransparentLiveRow, set: (value) => { _rehydrateTransparentLiveRow = value; } },
  _refreshTransparentThinkingLiveRow: { enumerable: true, get: () => _refreshTransparentThinkingLiveRow, set: (value) => { _refreshTransparentThinkingLiveRow = value; } },
  _bindTransparentFadeCleanup: { enumerable: true, get: () => _bindTransparentFadeCleanup, set: (value) => { _bindTransparentFadeCleanup = value; } },
  _appendTransparentFadeText: { enumerable: true, get: () => _appendTransparentFadeText, set: (value) => { _appendTransparentFadeText = value; } },
  _refreshTransparentFadeProseRow: { enumerable: true, get: () => _refreshTransparentFadeProseRow, set: (value) => { _refreshTransparentFadeProseRow = value; } },
  _refreshTransparentLiveRow: { enumerable: true, get: () => _refreshTransparentLiveRow, set: (value) => { _refreshTransparentLiveRow = value; } },
  _renderLiveAnchorActivitySceneForStream: { enumerable: true, get: () => _renderLiveAnchorActivitySceneForStream, set: (value) => { _renderLiveAnchorActivitySceneForStream = value; } },
  _renderLiveAnchorActivitySceneSnapshotForStream: { enumerable: true, get: () => _renderLiveAnchorActivitySceneSnapshotForStream, set: (value) => { _renderLiveAnchorActivitySceneSnapshotForStream = value; } },
  _anchorSceneSceneHasWorklogWorthyRows: { enumerable: true, get: () => _anchorSceneSceneHasWorklogWorthyRows, set: (value) => { _anchorSceneSceneHasWorklogWorthyRows = value; } },
  _anchorSceneHasErroredTerminalState: { enumerable: true, get: () => _anchorSceneHasErroredTerminalState, set: (value) => { _anchorSceneHasErroredTerminalState = value; } },
  _renderSettledAnchorSceneTransparentForMessage: { enumerable: true, get: () => _renderSettledAnchorSceneTransparentForMessage, set: (value) => { _renderSettledAnchorSceneTransparentForMessage = value; } },
  _tOrDefault: { enumerable: true, get: () => _tOrDefault, set: (value) => { _tOrDefault = value; } },
  _buildTransparentEarlierStepsAffordance: { enumerable: true, get: () => _buildTransparentEarlierStepsAffordance, set: (value) => { _buildTransparentEarlierStepsAffordance = value; } },
  _revealTransparentEarlierSteps: { enumerable: true, get: () => _revealTransparentEarlierSteps, set: (value) => { _revealTransparentEarlierSteps = value; } },
  _computeTransparentHiddenPrefixCount: { enumerable: true, get: () => _computeTransparentHiddenPrefixCount, set: (value) => { _computeTransparentHiddenPrefixCount = value; } },
  _shouldKeepSettledWorklogOpenForStreamSettle: { enumerable: true, get: () => _shouldKeepSettledWorklogOpenForStreamSettle, set: (value) => { _shouldKeepSettledWorklogOpenForStreamSettle = value; } },
  _armKeepSettledWorklogOpen: { enumerable: true, get: () => _armKeepSettledWorklogOpen, set: (value) => { _armKeepSettledWorklogOpen = value; } },
  _disarmKeepSettledWorklogOpen: { enumerable: true, get: () => _disarmKeepSettledWorklogOpen, set: (value) => { _disarmKeepSettledWorklogOpen = value; } },
  _isKeepSettledWorklogOpenArmed: { enumerable: true, get: () => _isKeepSettledWorklogOpenArmed, set: (value) => { _isKeepSettledWorklogOpenArmed = value; } },
  _renderSettledAnchorSceneForMessage: { enumerable: true, get: () => _renderSettledAnchorSceneForMessage, set: (value) => { _renderSettledAnchorSceneForMessage = value; } },
  _syncLiveWorklogReasonsForAnchor: { enumerable: true, get: () => _syncLiveWorklogReasonsForAnchor, set: (value) => { _syncLiveWorklogReasonsForAnchor = value; } },
  _clearLiveActivityUserIntent: { enumerable: true, get: () => _clearLiveActivityUserIntent, set: (value) => { _clearLiveActivityUserIntent = value; } },
  ensureActivityGroup: { enumerable: true, get: () => ensureActivityGroup, set: (value) => { ensureActivityGroup = value; } },
  normalizeLiveActivityGroupPlacement: { enumerable: true, get: () => normalizeLiveActivityGroupPlacement, set: (value) => { normalizeLiveActivityGroupPlacement = value; } },
  ensureRunActivityGroup: { enumerable: true, get: () => ensureRunActivityGroup, set: (value) => { ensureRunActivityGroup = value; } },
  _ANCHOR_SCENE_ERRORED_TERMINAL_STATES: { enumerable: true, get: () => _ANCHOR_SCENE_ERRORED_TERMINAL_STATES },
  _TRANSPARENT_SETTLED_ROW_CAP: { enumerable: true, get: () => _TRANSPARENT_SETTLED_ROW_CAP },
  _TRANSPARENT_SETTLED_ROW_CAP_SLACK: { enumerable: true, get: () => _TRANSPARENT_SETTLED_ROW_CAP_SLACK },
  _keepSettledWorklogOpenForStreamId: { enumerable: true, get: () => _keepSettledWorklogOpenForStreamId, set: (value) => { _keepSettledWorklogOpenForStreamId = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
