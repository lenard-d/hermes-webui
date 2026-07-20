import { scrollIfPinned } from './activity-and-scroll.js';
import { _renderLiveAnchorActivitySceneForStream } from './anchor-scenes.js';
import { _clearCompressionElapsedTimer, _compressionElapsedStartedAt, _startCompressionElapsedTimer } from './composer-controls.js';
import { _compressionPlaceholderSaved, compatibilityBindings as composerStateBindings } from './composer-state.js';
import { _assistantTurnBlocks, _createAssistantTurn, msgContent } from './assistant-turn-presentation.js';
import { closeCurrentLiveActivityGroup, _moveLiveRunStatusToTurnEnd } from './live-run-status.js';
import { _captureMessageScrollSnapshot } from './message-scroll-snapshot.js';
import { _restoreMessageScrollSnapshotSameFrame } from './render-support.js';
import { $, S, _compressionSessionLock, _setCompressionSessionLock, esc } from './state.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './tool-worklog.js';
import { _activityKeyForLiveTurn, ensureLiveWorklogContainer, isLiveAnchorActivitySceneOwner } from './transparent-worklog.js';

function _compressionStateForCurrentSession(){
  const state=window._compressionUi;
  if(!state||!S.session||state.sessionId!==S.session.session_id) return null;
  return state;
}
function isCompressionUiRunning(){
  const state=_compressionStateForCurrentSession();
  const lock=_compressionSessionLock();
  return !!((state&&state.phase==='running') || (lock && S.session && lock===S.session.session_id));
}
// Restore the composer placeholder saved when auto-compaction started. Safe to
// call whenever compression leaves the running state, from any path (clear,
// non-running setCompressionUi, or a direct window._compressionUi=null in the
// SSE handler) — it no-ops when nothing was saved. (#3512)
function _restoreCompressionPlaceholder(){
  const _input=$('msg');
  if(_input&&typeof _compressionPlaceholderSaved==='string'){
    _input.placeholder=_compressionPlaceholderSaved;
  }
  composerStateBindings._compressionPlaceholderSaved=null;
}
function clearCompressionUi(){
  window._compressionUi=null;
  _clearCompressionElapsedTimer();
  _setCompressionSessionLock(null);
  _restoreCompressionPlaceholder();
  renderCompressionUi();
}
function setCompressionUi(state){
  if(!state){
    clearCompressionUi();
    return;
  }
  const nextState={...state};
  if(nextState.automatic&&nextState.phase==='running'&&!_compressionElapsedStartedAt(nextState)){
    nextState.startedAt=Date.now()/1000;
  }
  window._compressionUi=nextState;
  if(nextState.sessionId) _setCompressionSessionLock(nextState.sessionId);
  if(nextState.automatic&&nextState.phase==='running'){
    _startCompressionElapsedTimer();
    const _input=$('msg');
    if(_input&&_compressionPlaceholderSaved===null){
      composerStateBindings._compressionPlaceholderSaved=_input.placeholder;
      _input.placeholder=typeof t==='function'?t('composer_compression_will_queue')||'Type a message — it will queue and send after compression':'Type a message — it will queue and send after compression';
    }
  } else {
    _clearCompressionElapsedTimer();
    // Leaving the running state (e.g. setCompressionUi(done)) must restore the
    // placeholder too — not only clearCompressionUi(). (#3512 leak fix)
    _restoreCompressionPlaceholder();
  }
  renderCompressionUi();
}
function _compressionCardsHtml(state){
  if(!state) return '';
  if(state.automatic) return _autoCompressionCardsHtml(state);
  const cmdText=state.commandText||'/compress';
  const focusText=state.focusTopic?`${t('focus_label')}: ${state.focusTopic}`:'';
  const headerText=state.phase==='done'
    ? (state.summary?.headline||t('compress_complete_label'))
    : state.phase==='error'
      ? (state.errorText||t('compress_failed_label'))
      : (typeof state.beforeCount==='number' ? t('n_messages', state.beforeCount) : '');
  const statusBody=state.phase==='error'
    ? [state.errorText||t('compress_failed_label'), focusText].filter(Boolean).join('\n')
    : [t('compressing'), focusText].filter(Boolean).join('\n');
  const statusLabel=state.phase==='done'
    ? t('compress_complete_label')
    : state.phase==='error'
      ? t('compress_failed_label')
      : t('compress_running_label');
  const statusIcon=state.phase==='done'
    ? li('check',13)
    : state.phase==='error'
      ? li('x',13)
    : `<span class="tool-card-running-dot"></span>`;
  const doneCardHtml=state.phase==='done'
    ? _compressionStatusCardHtml({
        statusLabel,
        previewText: headerText,
        detail: [state.summary?.token_line, state.summary?.note, focusText].filter(Boolean).join('\n'),
        icon: statusIcon,
        open: true,
        variantClass: 'tool-card-compress-complete',
      })
    : '';
  const referenceHtml=(state.phase==='done'&&state.referenceText)
    ? _compressionReferenceCardHtml(state.referenceText, false)
    : '';
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      <div class="tool-card tool-card-compress-command">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          <span class="tool-card-icon">${li('settings',13)}</span>
          <span class="tool-card-name">${esc(t('command_label'))}</span>
          <span class="tool-card-preview">${esc(cmdText)}</span>
        </div>
      </div>
    </div>
    <div class="tool-card-row compression-card-row" data-compression-card="1">
      ${state.phase==='done'
        ? doneCardHtml
        : _compressionStatusCardHtml({
            statusLabel,
            previewText: headerText,
            detail: statusBody,
            icon: statusIcon,
            open: false,
            variantClass: state.phase==='error'
              ? 'tool-card-compress-error'
              : 'tool-card-compress-running',
          })
      }
    </div>
    ${referenceHtml}`;
}
function _autoCompressionBaseDetail(state){
  const running=state&&state.phase==='running';
  if(running)return 'Compressing context';
  if(state&&state.phase==='done')return 'Context auto-compressed';
  return '';
}
function _autoCompressionPreviewText(state){
  const running=state&&state.phase==='running';
  if(running)return 'Compressing context';
  if(state&&state.phase==='done')return 'Context auto-compressed';
  return '';
}
function _autoCompressionDetailText(state){
  const running=state&&state.phase==='running';
  if(running)return '';
  return '';
}
function _autoCompressionCardsHtml(state){
  const preview=_autoCompressionPreviewText(state);
  const done=state&&state.phase==='done';
  return `
    <div class="tool-card-row compression-card-row auto-compression-divider-row auto-compression-inline-row" data-compression-card="1">
      <div class="auto-compression-divider auto-compression-inline${done?' auto-compression-divider-done':''}" aria-label="${esc(preview)}">
        <span class="auto-compression-divider-label">${done?li('file-text',13):li('loader',13)}${esc(preview)}</span>
      </div>
    </div>`;
}
function _autoCompressionWorklogNode(state){
  const row=document.createElement('div');
  row.className='tool-card-row compression-card-row auto-compression-divider-row auto-compression-inline-row';
  row.setAttribute('data-compression-card','1');
  const label=_autoCompressionPreviewText(state);
  const done=state&&state.phase==='done';
  row.innerHTML=`
    <div class="auto-compression-divider auto-compression-inline${done?' auto-compression-divider-done':''}" aria-label="${esc(label)}">
      <span class="auto-compression-divider-label">${done?li('file-text',13):li('loader',13)}${esc(label)}</span>
    </div>`;
  return row;
}
function _compressionCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_compressionCardsHtml(state)}</div>`;
  return wrap;
}
function appendLiveCompressionCard(state){
  if(!S.session||!S.activeStreamId||!state) return false;
  if(isLiveAnchorActivitySceneOwner(S.activeStreamId)){
    return _renderLiveAnchorActivitySceneForStream(S.activeStreamId, S.session.session_id);
  }
  const scrollSnapshot=_captureMessageScrollSnapshot();
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    $('msgInner').appendChild(turn);
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return false;
  closeCurrentLiveActivityGroup();
  if(state.automatic){
    const group=ensureLiveWorklogContainer(inner,{activityKey:_activityKeyForLiveTurn()});
    const list=_toolWorklogListEl(group);
    if(!group||!list) return false;
    const node=_autoCompressionWorklogNode(state);
    node.setAttribute('data-live-compression-card','1');
    node.setAttribute('data-compression-phase',String(state.phase||''));
    if(state.phase==='running'){
      const started=_compressionElapsedStartedAt(state)||Date.now()/1000;
      node.setAttribute('data-compression-started-at',String(started));
      node.setAttribute('data-compression-message',String(state.message||'Compressing context'));
      _startCompressionElapsedTimer();
    } else {
      node.removeAttribute('data-compression-started-at');
      node.removeAttribute('data-compression-message');
      const _activeCompState = _compressionStateForCurrentSession();
      if (!_activeCompState || !_activeCompState.automatic || _activeCompState.phase !== 'running') {
        _clearCompressionElapsedTimer();
      }
    }
    const existingRunning=group.querySelector('[data-live-compression-card="1"][data-compression-started-at]');
    const existingDone=Array.from(group.querySelectorAll('[data-live-compression-card="1"][data-compression-phase="done"]')).pop();
    const existing=state.phase==='running'?existingRunning:(existingRunning||existingDone);
    if(existing) existing.replaceWith(node);
    else list.appendChild(node);
    _syncToolCallGroupSummary(group);
    _moveLiveRunStatusToTurnEnd();
    _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return true;
  }
  const node=_compressionCardsNode(state);
  if(!node) return false;
  node.setAttribute('data-live-compression-card','1');
  if(state.automatic&&state.phase==='running'){
    const started=_compressionElapsedStartedAt(state)||Date.now()/1000;
    node.setAttribute('data-compression-started-at',String(started));
    node.setAttribute('data-compression-message',String(state.message||'Auto-compressing context...'));
    _startCompressionElapsedTimer();
  } else {
    // Completion or error: clear the elapsed-timer attributes so the
    // interval reader (_compressionLiveCardState) doesn't keep treating
    // the replaced card as a running compression (#2973).
    node.removeAttribute('data-compression-started-at');
    node.removeAttribute('data-compression-message');
    // Only clear the global timer when the *active* session has no running
    // compression.  An SSE completion for a background session must not
    // kill the timer that's driving the current session's display.
    const _activeCompState = _compressionStateForCurrentSession();
    if (!_activeCompState || !_activeCompState.automatic || _activeCompState.phase !== 'running') {
      _clearCompressionElapsedTimer();
    }
  }
  const existing=inner.querySelector('[data-live-compression-card="1"]');
  if(existing) existing.replaceWith(node);
  else inner.appendChild(node);
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
  return true;
}

function _isContextCompactionMessage(m){
  if(!m||!m.role||m.role==='tool') return false;
  const text=msgContent(m)||String(m.content||'');
  return _isContextCompactionText(text);
}
function _isContextCompactionText(text){
  return /^\s*\[context compaction/i.test(String(text||'')) || /^\s*context compaction/i.test(String(text||''));
}
function _isPreservedCompressionTaskListMarkerText(text){
  return /^\s*\[your active task list was preserved across context compression\]/i.test(String(text||''));
}
function _isPreservedCompressionTaskListMarkerOnlyText(text){
  return _isPreservedCompressionTaskListMarkerText(text)
    && !String(text||'')
      .replace(/^\s*\[your active task list was preserved across context compression\]\s*/i,'')
      .trim();
}
function _isPreservedCompressionTaskListMessage(m){
  if(!m||m.role!=='user') return false;
  const text=msgContent(m)||String(m.content||'');
  return /^\s*\[your active task list was preserved across context compression\]/i.test(text);
}
function _isMarkerOnlyAssistantCompressionMessage(m){
  if(!m||m.role!=='assistant') return false;
  const text=msgContent(m)||String(m.content||'');
  return _isPreservedCompressionTaskListMarkerOnlyText(text);
}
function _preservedCompressionTaskListPreview(text){
  const body=String(text||'')
    .replace(/^\s*\[your active task list was preserved across context compression\]\s*/i,'')
    .trim();
  return (body.split(/\n+/).map(line=>line.trim()).filter(Boolean).slice(0,2).join(' ') || t('preserved_task_list_label'));
}
function _compressionMessageAnchorKey(m){
  if(!m||!m.role||m.role==='tool') return null;
  let content='';
  try{
    content=String(msgContent(m)||'');
  }catch(_){
    content=String(m.content||'');
  }
  const norm=content.replace(/\s+/g,' ').trim().slice(0,160);
  const ts=m._ts||m.timestamp||null;
  const attachments=Array.isArray(m.attachments)?m.attachments.length:0;
  if(!norm && !attachments && !ts) return null;
  return {role:String(m.role||''), ts, text:norm, attachments};
}
function _compressionAnchorIndex(visWithIdx, anchorKey, fallbackIdx=null){
  if(anchorKey&&Array.isArray(visWithIdx)){
    for(let i=visWithIdx.length-1;i>=0;i--){
      const candidate=_compressionMessageAnchorKey(visWithIdx[i].m);
      if(!candidate) continue;
      const anchorTs=String(anchorKey.ts??'');
      const candidateTs=String(candidate.ts??'');
      if(
        candidate.role===String(anchorKey.role||'') &&
        (!anchorTs||!candidateTs||candidateTs===anchorTs) &&
        String(candidate.text||'')===String(anchorKey.text||'') &&
        Number(candidate.attachments||0)===Number(anchorKey.attachments||0)
      ){
        return i;
      }
    }
  }
  return typeof fallbackIdx==='number' ? fallbackIdx : null;
}
function _latestCompressionReferenceMessage(messages, summaryText=''){
  if(!Array.isArray(messages)||!messages.length) return {message:null, rawIdx:-1};
  const summaryNorm=String(summaryText||'').replace(/\s+/g,' ').trim();
  for(let i=messages.length-1;i>=0;i--){
    const m=messages[i];
    if(!_isContextCompactionMessage(m)) continue;
    if(!summaryNorm) return {message:m, rawIdx:i};
    let content='';
    try{
      content=String(msgContent(m)||'');
    }catch(_){
      content=String((m&&m.content)||'');
    }
    const contentNorm=content.replace(/\s+/g,' ').trim();
    if(contentNorm.includes(summaryNorm)) return {message:m, rawIdx:i};
  }
  return {message:null, rawIdx:-1};
}
function _shouldShowSettledCompressionReference(referenceText){
  return !!String(referenceText||'').trim() && !_isContextCompactionText(referenceText);
}
function _compressionReferenceCardHtml(text, open=false){
  const copy=_engineAwareCompressionCopy();
  const preview=text.split(/\n+/).filter(Boolean).slice(0,2).join(' ');
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1" data-raw-text="${esc(text)}">
      <div class="tool-card tool-card-compress-reference${open?' open':''}">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          <span class="tool-card-icon">${li('star',13)}</span>
          <span class="tool-card-name">${esc(copy.label)}</span>
          <span class="tool-card-preview">${esc(copy.preview)} · ${esc(preview)}</span>
          <span class="tool-card-toggle">${li('chevron-right',12)}</span>
          <button class="msg-copy-btn msg-action-btn tool-card-copy compression-reference-copy" title="${t('copy')}" onclick="copyMsg(this);event.stopPropagation()">${li('copy',13)}</button>
        </div>
        <div class="tool-card-detail">
          <div class="tool-card-result">
          <pre>${esc(text)}</pre>
        </div>
        </div>
      </div>

    </div>`;
}
function _preservedCompressionTaskListCardHtml(m, open=false){
  const text=msgContent(m)||String(m.content||'');
  return `
    <div class="tool-card-row compression-card-row" data-compression-card="1" data-raw-text="${esc(text)}">
      ${_compressionStatusCardHtml({
        statusLabel: t('preserved_task_list_label'),
        previewText: _preservedCompressionTaskListPreview(text),
        detail: text,
        icon: li('list-todo',13),
        open,
        variantClass: 'tool-card-compress-reference',
      })}
    </div>`;
}
function _preservedCompressionTaskListCardsHtml(messages){
  return (messages||[]).map(m=>_preservedCompressionTaskListCardHtml(m, false)).join('');
}
function _latestTodoToolItems(messages){
  for(let i=(messages||[]).length-1;i>=0;i--){
    const m=messages[i];
    if(!m||m.role!=='tool') continue;
    try{
      const payload=typeof m.content==='string'?JSON.parse(m.content):m.content;
      if(payload&&Array.isArray(payload.todos)) return payload.todos;
    }catch(_){ }
  }
  return null;
}
function _hasActiveTodoItems(items){
  return Array.isArray(items) && items.some(item=>{
    const status=String(item&&item.status||'').trim().toLowerCase();
    return status==='pending'||status==='in_progress';
  });
}
function _latestPreservedCompressionTaskListMessages(messages){
  const latest=[...(messages||[])].reverse().find(m=>_isPreservedCompressionTaskListMessage(m));
  if(!latest) return [];
  const latestTodos=_latestTodoToolItems(messages);
  if(Array.isArray(latestTodos) && !_hasActiveTodoItems(latestTodos)) return [];
  return [latest];
}
function _isSameLocalDay(dateA, dateB){
  return dateA.getFullYear()===dateB.getFullYear()
    && dateA.getMonth()===dateB.getMonth()
    && dateA.getDate()===dateB.getDate();
}
function _formatMessageFooterTimestamp(tsVal){
  if(!tsVal) return '';
  const date=new Date(tsVal*1000);
  const now=new Date();
  // Use _formatInServerTz when available — it correctly handles fractional-hour
  // offsets like India +0530 that Etc/GMT cannot express. Falls back to plain
  // toLocaleString when sessions.js hasn't loaded yet.
  const fmt=(typeof _formatInServerTz==='function')?_formatInServerTz:null;
  if(_isSameLocalDay(date, now)){
    const opts={hour:'2-digit', minute:'2-digit'};
    return fmt?fmt(date,opts):date.toLocaleTimeString([], opts);
  }
  const opts={month:'short', day:'numeric', hour:'numeric', minute:'2-digit'};
  return fmt?fmt(date,opts):date.toLocaleString([], opts);
}
function _compressionEngineForSession(){
  return String(
    (S.session&&(
      S.session.compression_anchor_engine
      || S.session.context_engine
    )) || 'compressor'
  ).trim().toLowerCase() || 'compressor';
}
function _compressionModeForSession(){
  return String(
    (S.session&&S.session.compression_anchor_mode) || 'summary_compaction'
  ).trim().toLowerCase() || 'summary_compaction';
}
function _engineAwareCompressionCopy(engine=_compressionEngineForSession(), mode=_compressionModeForSession()){
  if(engine==='lcm'||mode==='lossless_retrieval'){
    return {
      label:t('retrieval_context_label'),
      preview:t('retrieval_context_preview'),
    };
  }
  return {
    label:t('context_compaction_label'),
    preview:t('reference_only_label'),
  };
}
function _compressionStatusCardHtml({
  statusLabel,
  previewText,
  detail,
  icon,
  open=false,
  variantClass='',
}){
  const statusDetail = String(detail || '').trim();
  const hasBody = !!statusDetail;
  const openClass = open ? ' open' : '';
  const statusIcon = icon;
  const bodyHtml = hasBody ? `<div class="tool-card-detail"><div class="tool-card-result"><pre>${esc(statusDetail)}</pre></div></div>` : '';
  const toggleHtml = hasBody ? `<span class="tool-card-toggle">${li('chevron-right',12)}</span>` : '';
  return `
    <div class="tool-card ${variantClass}${openClass}">
      <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
        ${statusIcon}
        <span class="tool-card-name">${esc(statusLabel)}</span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${toggleHtml}
      </div>
      ${bodyHtml}
    </div>`;
}

function _contextCompactionMessageHtml(m, tsTitle='', preservedMessages=[]){
  const text=msgContent(m)||String(m.content||'');
  return `<div class="compression-turn"><div class="compression-turn-blocks">${_compressionReferenceCardHtml(text, false, tsTitle)}${_preservedCompressionTaskListCardsHtml(preservedMessages)}</div></div>`;
}
function renderCompressionUi(){
  const el=$('liveCompressionCards');
  if(!el) return;
  el.innerHTML='';
  el.style.display='none';
}

export {
  _compressionStateForCurrentSession,
  isCompressionUiRunning,
  _restoreCompressionPlaceholder,
  clearCompressionUi,
  setCompressionUi,
  _compressionCardsHtml,
  _autoCompressionBaseDetail,
  _autoCompressionPreviewText,
  _autoCompressionDetailText,
  _autoCompressionCardsHtml,
  _autoCompressionWorklogNode,
  _compressionCardsNode,
  appendLiveCompressionCard,
  _isContextCompactionMessage,
  _isContextCompactionText,
  _isPreservedCompressionTaskListMarkerText,
  _isPreservedCompressionTaskListMarkerOnlyText,
  _isPreservedCompressionTaskListMessage,
  _isMarkerOnlyAssistantCompressionMessage,
  _preservedCompressionTaskListPreview,
  _compressionMessageAnchorKey,
  _compressionAnchorIndex,
  _latestCompressionReferenceMessage,
  _shouldShowSettledCompressionReference,
  _compressionReferenceCardHtml,
  _preservedCompressionTaskListCardHtml,
  _preservedCompressionTaskListCardsHtml,
  _latestTodoToolItems,
  _hasActiveTodoItems,
  _latestPreservedCompressionTaskListMessages,
  _isSameLocalDay,
  _formatMessageFooterTimestamp,
  _compressionEngineForSession,
  _compressionModeForSession,
  _engineAwareCompressionCopy,
  _compressionStatusCardHtml,
  _contextCompactionMessageHtml,
  renderCompressionUi,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _compressionStateForCurrentSession: { enumerable: true, get: () => _compressionStateForCurrentSession, set: (value) => { _compressionStateForCurrentSession = value; } },
  isCompressionUiRunning: { enumerable: true, get: () => isCompressionUiRunning, set: (value) => { isCompressionUiRunning = value; } },
  _restoreCompressionPlaceholder: { enumerable: true, get: () => _restoreCompressionPlaceholder, set: (value) => { _restoreCompressionPlaceholder = value; } },
  clearCompressionUi: { enumerable: true, get: () => clearCompressionUi, set: (value) => { clearCompressionUi = value; } },
  setCompressionUi: { enumerable: true, get: () => setCompressionUi, set: (value) => { setCompressionUi = value; } },
  _compressionCardsHtml: { enumerable: true, get: () => _compressionCardsHtml, set: (value) => { _compressionCardsHtml = value; } },
  _autoCompressionBaseDetail: { enumerable: true, get: () => _autoCompressionBaseDetail, set: (value) => { _autoCompressionBaseDetail = value; } },
  _autoCompressionPreviewText: { enumerable: true, get: () => _autoCompressionPreviewText, set: (value) => { _autoCompressionPreviewText = value; } },
  _autoCompressionDetailText: { enumerable: true, get: () => _autoCompressionDetailText, set: (value) => { _autoCompressionDetailText = value; } },
  _autoCompressionCardsHtml: { enumerable: true, get: () => _autoCompressionCardsHtml, set: (value) => { _autoCompressionCardsHtml = value; } },
  _autoCompressionWorklogNode: { enumerable: true, get: () => _autoCompressionWorklogNode, set: (value) => { _autoCompressionWorklogNode = value; } },
  _compressionCardsNode: { enumerable: true, get: () => _compressionCardsNode, set: (value) => { _compressionCardsNode = value; } },
  appendLiveCompressionCard: { enumerable: true, get: () => appendLiveCompressionCard, set: (value) => { appendLiveCompressionCard = value; } },
  _isContextCompactionMessage: { enumerable: true, get: () => _isContextCompactionMessage, set: (value) => { _isContextCompactionMessage = value; } },
  _isContextCompactionText: { enumerable: true, get: () => _isContextCompactionText, set: (value) => { _isContextCompactionText = value; } },
  _isPreservedCompressionTaskListMarkerText: { enumerable: true, get: () => _isPreservedCompressionTaskListMarkerText, set: (value) => { _isPreservedCompressionTaskListMarkerText = value; } },
  _isPreservedCompressionTaskListMarkerOnlyText: { enumerable: true, get: () => _isPreservedCompressionTaskListMarkerOnlyText, set: (value) => { _isPreservedCompressionTaskListMarkerOnlyText = value; } },
  _isPreservedCompressionTaskListMessage: { enumerable: true, get: () => _isPreservedCompressionTaskListMessage, set: (value) => { _isPreservedCompressionTaskListMessage = value; } },
  _isMarkerOnlyAssistantCompressionMessage: { enumerable: true, get: () => _isMarkerOnlyAssistantCompressionMessage, set: (value) => { _isMarkerOnlyAssistantCompressionMessage = value; } },
  _preservedCompressionTaskListPreview: { enumerable: true, get: () => _preservedCompressionTaskListPreview, set: (value) => { _preservedCompressionTaskListPreview = value; } },
  _compressionMessageAnchorKey: { enumerable: true, get: () => _compressionMessageAnchorKey, set: (value) => { _compressionMessageAnchorKey = value; } },
  _compressionAnchorIndex: { enumerable: true, get: () => _compressionAnchorIndex, set: (value) => { _compressionAnchorIndex = value; } },
  _latestCompressionReferenceMessage: { enumerable: true, get: () => _latestCompressionReferenceMessage, set: (value) => { _latestCompressionReferenceMessage = value; } },
  _shouldShowSettledCompressionReference: { enumerable: true, get: () => _shouldShowSettledCompressionReference, set: (value) => { _shouldShowSettledCompressionReference = value; } },
  _compressionReferenceCardHtml: { enumerable: true, get: () => _compressionReferenceCardHtml, set: (value) => { _compressionReferenceCardHtml = value; } },
  _preservedCompressionTaskListCardHtml: { enumerable: true, get: () => _preservedCompressionTaskListCardHtml, set: (value) => { _preservedCompressionTaskListCardHtml = value; } },
  _preservedCompressionTaskListCardsHtml: { enumerable: true, get: () => _preservedCompressionTaskListCardsHtml, set: (value) => { _preservedCompressionTaskListCardsHtml = value; } },
  _latestTodoToolItems: { enumerable: true, get: () => _latestTodoToolItems, set: (value) => { _latestTodoToolItems = value; } },
  _hasActiveTodoItems: { enumerable: true, get: () => _hasActiveTodoItems, set: (value) => { _hasActiveTodoItems = value; } },
  _latestPreservedCompressionTaskListMessages: { enumerable: true, get: () => _latestPreservedCompressionTaskListMessages, set: (value) => { _latestPreservedCompressionTaskListMessages = value; } },
  _isSameLocalDay: { enumerable: true, get: () => _isSameLocalDay, set: (value) => { _isSameLocalDay = value; } },
  _formatMessageFooterTimestamp: { enumerable: true, get: () => _formatMessageFooterTimestamp, set: (value) => { _formatMessageFooterTimestamp = value; } },
  _compressionEngineForSession: { enumerable: true, get: () => _compressionEngineForSession, set: (value) => { _compressionEngineForSession = value; } },
  _compressionModeForSession: { enumerable: true, get: () => _compressionModeForSession, set: (value) => { _compressionModeForSession = value; } },
  _engineAwareCompressionCopy: { enumerable: true, get: () => _engineAwareCompressionCopy, set: (value) => { _engineAwareCompressionCopy = value; } },
  _compressionStatusCardHtml: { enumerable: true, get: () => _compressionStatusCardHtml, set: (value) => { _compressionStatusCardHtml = value; } },
  _contextCompactionMessageHtml: { enumerable: true, get: () => _contextCompactionMessageHtml, set: (value) => { _contextCompactionMessageHtml = value; } },
  renderCompressionUi: { enumerable: true, get: () => renderCompressionUi, set: (value) => { renderCompressionUi = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
