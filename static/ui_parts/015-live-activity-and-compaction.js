// ── LiveFooter timer (module-level singleton) ──────────────────────────────
const _liveRunStatusTimers={};  // keyed by sessionId, max 1 active
let _liveRunStatusTokens=null;
let _liveRunStatusSessionId=null;
function _formatRunElapsed(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return'00:00';
  const total=Math.max(0,Math.floor(n));
  if(total>=3600){
    const h=Math.floor(total/3600);
    const m=Math.floor((total%3600)/60);
    return h+'h '+String(m).padStart(2,'0')+'m';
  }
  const m=Math.floor(total/60);
  const s=total%60;
  return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
}
function _moveLiveRunStatusToTurnEnd(el){
  el=el||$('liveRunStatus');
  if(!el) return null;
  const turn=$('liveAssistantTurn');
  const blocks=_assistantTurnBlocks(turn);
  if(blocks&&el.parentElement===blocks&&blocks.lastElementChild!==el) blocks.appendChild(el);
  return el;
}
function placeLiveRunStatusHost(){
  let el=$('liveRunStatus');
  if(!el){
    el=document.createElement('div');
    el.id='liveRunStatus';
    el.hidden=true;
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    const inner=$('msgInner');
    if(inner) inner.appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(blocks&&el.parentElement!==blocks) blocks.appendChild(el);
  el.className='live-run-status live-footer';
  return _moveLiveRunStatusToTurnEnd(el);
}
function showLiveRunStatus(sid,opts){
  if(typeof isCompactWorklogMode==='function'&&isCompactWorklogMode()){
    _liveRunStatusSessionId=sid;
    _liveRunStatusTokens=opts&&opts.tokens||null;
    const el=$('liveRunStatus');
    if(el){el.hidden=true;el.innerHTML='';}
    return;
  }
  const el=placeLiveRunStatusHost();
  if(!el)return;
  _liveRunStatusSessionId=sid;
  const startedAt=opts&&opts.startedAt||null;
  _liveRunStatusTokens=opts&&opts.tokens||null;
  el.hidden=false;
  _renderLiveRunStatusContent(el,startedAt);
  _startLiveRunStatusTimer(sid,startedAt);
}
function _renderLiveRunStatusContent(el,startedAt){
  if(!el)return;
  const now=Date.now()/1000;
  const elapsed=startedAt?Math.max(0,now-startedAt):0;
  const timeStr=_formatRunElapsed(elapsed);
  const tokens=_liveRunStatusTokens;
  el.innerHTML=`<span class="live-run-status-dot tool-card-running-dot"></span><span class="live-run-status-text lf-time">${timeStr}</span>${tokens?`<span class="lf-sep">·</span><span class="lf-tokens">${_fmtTokens(tokens)} tokens</span>`:''}<span class="lf-sep">·</span><span class="lf-status">Running</span>`;
}
function updateLiveRunStatus(opts){
  if(opts&&opts.sessionId&&_liveRunStatusSessionId&&opts.sessionId!==_liveRunStatusSessionId) return;
  if(opts&&opts.tokens!==undefined)_liveRunStatusTokens=opts.tokens;
  const el=$('liveRunStatus');
  if(el&&!el.hidden){
    _moveLiveRunStatusToTurnEnd(el);
    const timer=_liveRunStatusTimers[_liveRunStatusSessionId];
    const startedAt=timer&&timer.startedAt||null;
    _renderLiveRunStatusContent(el,startedAt);
  }
}
function _syncLiveRunStatusAfterRender(){
  const sid=S.session&&S.session.session_id;
  if(!sid||!S.activeStreamId||!S.busy) return;
  const timer=_liveRunStatusTimers[sid];
  const startedAt=(timer&&timer.startedAt)||((S.session&&S.session.pending_started_at)||Date.now()/1000);
  if(typeof isCompactWorklogMode==='function'&&isCompactWorklogMode()){
    const el=$('liveRunStatus');
    if(el){el.hidden=true;el.innerHTML='';}
    return;
  }
  const el=$('liveRunStatus');
  if(el&&el.isConnected&&!el.hidden){
    _moveLiveRunStatusToTurnEnd(el);
    _renderLiveRunStatusContent(el,startedAt);
    return;
  }
  showLiveRunStatus(sid,{startedAt,tokens:_liveRunStatusTokens});
}
function hideLiveRunStatus(sid){
  if(sid&&_liveRunStatusSessionId&&sid!==_liveRunStatusSessionId) return;
  const el=$('liveRunStatus');
  if(el){el.hidden=true;el.innerHTML='';}
  _clearLiveRunStatusTimer(sid||_liveRunStatusSessionId);
  _liveRunStatusTokens=null;
  _liveRunStatusSessionId=null;
}
function _startLiveRunStatusTimer(sid,startedAt){
  if(!sid)return;
  _clearLiveRunStatusTimer(sid);
  _liveRunStatusTimers[sid]={startedAt,interval:setInterval(()=>{
    const el=$('liveRunStatus');
    if(!el||el.hidden){_clearLiveRunStatusTimer(sid);return;}
    if(_liveRunStatusSessionId!==sid)return;
    _renderLiveRunStatusContent(el,startedAt);
  },1000)};
}
function _clearLiveRunStatusTimer(sid){
  const t=_liveRunStatusTimers[sid];
  if(t){clearInterval(t.interval);delete _liveRunStatusTimers[sid];}
}
function ensureRunActivityForCurrentTurn(){
  // Phase C: disabled — top live run Activity card removed
  return null;
}
function closeCurrentLiveActivityGroup(){
  const turn=$('liveAssistantTurn');
  if(!turn) return;
  turn.querySelectorAll('.tool-worklog-group[data-live-tool-call-group="1"][data-live-activity-current="1"],.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]').forEach(group=>{
    group.removeAttribute('data-live-activity-current');
    _finalizeLiveActivityDisclosureGroup(group);
  });
}
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
  _compressionPlaceholderSaved=null;
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
      _compressionPlaceholderSaved=_input.placeholder;
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
function _isHandoffSummaryToolPayload(value){
  if(!value||typeof value!=='object'||Array.isArray(value)) return false;
  return value._handoff_summary_card === true;
}
function _parseHandoffSummaryPayload(content){
  if(!content) return null;
  if(typeof content==='object' && !Array.isArray(content)) return _isHandoffSummaryToolPayload(content)?content:null;
  if(typeof content!=='string') return null;
  try {
    const parsed=JSON.parse(content);
    return _isHandoffSummaryToolPayload(parsed)?parsed:null;
  } catch (e) {
    return null;
  }
}
function _handoffSummaryStateFromMessage(m){
  if(!m||m.role!=='tool') return null;
  const payload = _parseHandoffSummaryPayload(m.content);
  if(!payload) return null;
  if(String(payload.session_id||'') && S.session && String(m.session_id||'') && String(payload.session_id)!==String(S.session.session_id||'')) {
    return null;
  }
  const summary = String(payload.summary||'').trim();
  if(!summary) return null;
  return {
    phase: 'done',
    channel: payload.channel || null,
    rounds: Number.isFinite(payload.rounds)?payload.rounds:null,
    summary,
    fallback: !!payload.fallback,
    generatedAt: Number(payload.generated_at) || null,
  };
}
function _collectHandoffSummaryStates(messages){
  const states=[];
  if(!Array.isArray(messages)) return states;
  for(let i=0;i<messages.length;i++){
    const state=_handoffSummaryStateFromMessage(messages[i]);
    if(state) states.push({state, rawIdx:i});
  }
  return states;
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
function _handoffStateForCurrentSession(){
  const state=window._handoffUi;
  if(!state||!S.session||state.sessionId!==S.session.session_id) return null;
  return state;
}
function clearHandoffUi(){
  window._handoffUi=null;
  _renderMessagesWithScrollSnapshot();
}
function setHandoffUi(state){
  if(!state){
    clearHandoffUi();
    return;
  }
  window._handoffUi={...state};
  _renderMessagesWithScrollSnapshot();
}
function _handoffCardsHtml(state){
  if(!state) return '';
  const channel=String(state.channel||'').trim();
  const label=channel?`${channel} handoff summary`:'Handoff summary';
  const isError=state.phase==='error';
  const isDone=state.phase==='done';
  const isFallback=!!state.fallback;
  const detail=isError
    ? String(state.errorText||'Could not generate summary. Please try again.')
    : isDone
      ? String(state.summary||'')
      : 'Generating handoff summary...';
  const meta=typeof state.rounds==='number'
    ? `${state.rounds} external conversation rounds`
    : '';
  const icon=isError
    ? li('x',13)
    : isDone
      ? li('check',13)
      : '<span class="tool-card-running-dot"></span>';
  const bodyHtml=isDone&&!isError
    ? (
      `${renderMd(detail)}${
        isFallback
          ? '<p class="handoff-summary-fallback-note">Fallback summary generated from recent turns; no model-based rewrite was used.</p>'
          : ''
      }`
    )
    : `<p>${esc(detail)}</p>`;
  return `
    <div class="tool-card-row compression-card-row handoff-card-row" data-compression-card="1" data-handoff-card="1">
      <div class="tool-card tool-card-handoff-summary${isError?' tool-card-compress-error':''} open">
        <div class="tool-card-header" onclick="this.closest('.tool-card').classList.toggle('open')">
          ${icon}
          <span class="tool-card-name">${esc(label)}</span>
          ${meta?`<span class="tool-card-preview">${esc(meta)}</span>`:''}
          <span class="tool-card-toggle">${li('chevron-right',12)}</span>
        </div>
        <div class="tool-card-detail">
          <div class="tool-card-result handoff-summary-body">${bodyHtml}</div>
        </div>
      </div>
    </div>`;
}
function _handoffCardsNode(state){
  const wrap=document.createElement('div');
  wrap.className='compression-turn handoff-turn';
  wrap.innerHTML=`<div class="compression-turn-blocks">${_handoffCardsHtml(state)}</div>`;
  return wrap;
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
// Session render cache: avoids full markdown+DOM rebuild when switching back
// to a session whose rendered transcript inputs are unchanged.
// Keyed by session_id. Only used on cross-session navigation, never for
// in-session updates (new messages, edits, stream events).
const _sessionHtmlCache=window.HermesSessionRenderCache.create({
  maxEntries:8,
  maxEntryBytes:2*1024*1024,
  maxTotalBytes:8*1024*1024,
});
let _sessionHtmlCacheSid=null; // session_id currently rendered in the DOM
// #5966 (Codex F3): persist which capped Transparent-Stream turns the user has
// revealed, keyed by `${session_id}:${ownerRawIdx}`, so a switch-away/back or a
// normal rebuild does NOT silently re-cap a turn the user already expanded. The
// DOM `data-transparent-earlier-revealed` flag alone is lost across the
// _sessionHtmlCache innerHTML round-trip; this survives it. Reveal also
// invalidates that session's cached HTML so the stored markup isn't stale-capped.
const _transparentRevealedTurns=new Set();
function _transparentRevealKey(sessionId, ownerIdx){
  return String(sessionId||(S.session&&S.session.session_id)||'')+':'+String(ownerIdx);
}
function clearMessageRenderCache(){
  _clearRenderCache();
  _sessionHtmlCache.clear();
  _sessionHtmlCacheSid=null;
  clearVisibleMessageRowCache();
  _clearMessageVirtualHeightCache();
}

function _messageRenderCacheSignature(){
  return window.HermesSessionRenderCache.signature({
    messages:S.messages,
    toolCalls:S.toolCalls,
    session:S.session,
    messageContent:msgContent,
    messageHasReasoningPayload:_messageHasReasoningPayload,
  });
}

function _clipCliToolSnippet(text, maxLen=20000){
  const s=String(text||'');
  if(s.length<=maxLen) return s;
  return `${s.slice(0,maxLen)}\n\n... truncated ${s.length-maxLen} chars ...`;
}

function _cliToolResultText(raw){
  const s=String(raw||'');
  try{
    const rd=JSON.parse(s);
    if(rd && typeof rd==='object'){
      for(const key of ['output','result','error','content','diff','patch']){
        if(Object.prototype.hasOwnProperty.call(rd,key)){
          const v=rd[key];
          if(v==null) return '';
          return typeof v==='string' ? v : JSON.stringify(v,null,2);
        }
      }
    }
  }catch(e){}
  return s;
}

function _cliLooksLikePatchDiff(text){
  const s=String(text||'');
  if(!s) return false;
  if(/\*\*\* Begin Patch/.test(s)) return true;
  if(/^diff --git /m.test(s)) return true;
  if(/^@@\s/m.test(s)) return true;
  if(/(^|\n)---\s+/.test(s) && /(^|\n)\+\+\+\s+/.test(s)) return true;
  return false;
}

function _cliToolResultSnippet(raw){
  const fullText=_cliToolResultText(raw);
  if(_cliLooksLikePatchDiff(fullText)) return _clipCliToolSnippet(fullText);
  return String(fullText||'').slice(0,4000);
}

function _prefixedCliDiffLines(prefix, value){
  return String(value||'').split('\n').map(line=>`${prefix}${line}`).join('\n');
}

function _firstOwnedValue(obj, keys){
  for(const key of keys){
    if(obj && Object.prototype.hasOwnProperty.call(obj,key)) return obj[key];
  }
  return undefined;
}

function _cliPatchSnippetFromArgs(name, args){
  if(!args || typeof args!=='object') return '';
  const toolName=String(name||'').toLowerCase();
  for(const key of ['patch','diff']){
    const v=args[key];
    if(typeof v==='string' && v.trim()) return _clipCliToolSnippet(v);
  }
  for(const key of ['input','content']){
    const v=args[key];
    if(typeof v==='string' && _cliLooksLikePatchDiff(v)) return _clipCliToolSnippet(v);
  }
  const isEditLike=toolName==='apply_patch'
    || toolName==='patch'
    || toolName.includes('edit')
    || toolName==='replace'
    || toolName==='str_replace';
  if(!isEditLike) return '';
  const oldValue=_firstOwnedValue(args,['old_string','old_str','old','before']);
  const newValue=_firstOwnedValue(args,['new_string','new_str','new','after']);
  if(oldValue!==undefined || newValue!==undefined){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const lines=[];
    if(path) lines.push(path);
    if(oldValue!==undefined) lines.push(_prefixedCliDiffLines('-', oldValue));
    if(newValue!==undefined) lines.push(_prefixedCliDiffLines('+', newValue));
    return _clipCliToolSnippet(lines.join('\n'));
  }
  if(Array.isArray(args.edits)){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const chunks=[];
    if(path) chunks.push(path);
    args.edits.slice(0,5).forEach(edit=>{
      if(!edit || typeof edit!=='object') return;
      const before=_firstOwnedValue(edit,['old_string','old_str','old','before']);
      const after=_firstOwnedValue(edit,['new_string','new_str','new','after']);
      if(before!==undefined) chunks.push(_prefixedCliDiffLines('-', before));
      if(after!==undefined) chunks.push(_prefixedCliDiffLines('+', after));
    });
    if(chunks.length) return _clipCliToolSnippet(chunks.join('\n'));
  }
  return '';
}

function _cliToolCardSnippet(resultSnippet, patchSnippet){
  if(_cliLooksLikePatchDiff(resultSnippet)) return resultSnippet;
  if(!patchSnippet) return resultSnippet || '';
  const result=String(resultSnippet||'').trim();
  if(!result) return patchSnippet;
  const generic=/^(success|ok|done|done\.|exit code: 0)$/i.test(result);
  if(generic) return patchSnippet;
  return `${resultSnippet}\n\n${patchSnippet}`;
}

function _cliToolCardHasDiffSnippet(resultSnippet, patchSnippet){
  return !!patchSnippet || _cliLooksLikePatchDiff(resultSnippet);
}

function _assistantToolAnchorIdxForMessage(messages, rawIdx){
  const list=Array.isArray(messages)?messages:[];
  const current=list[rawIdx];
  if(_assistantMessageHasVisibleContent(current)) return rawIdx;
  if(_assistantReasoningPayloadText(current)) return rawIdx;
  for(let idx=rawIdx-1;idx>=0;idx--){
    if(_assistantMessageHasVisibleContent(list[idx])) return idx;
  }
  return rawIdx;
}
function _toolArgsSnapshot(args, limit){
  if(!args||typeof args!=='object'||Array.isArray(args)) return {};
  const max=Math.max(1,Number(limit)||6);
  const priority=[
    'query','search_query','searchQuery','pattern','q','keyword','keywords','term',
    'url','uri','command','cmd','path','file','file_path','filename','file_glob',
    'glob','offset','limit',
  ];
  // Content / diff-reconstruction keys must not be capped to the short
  // incidental-arg limit, or long commands/paths get cut and recovery-rebuilt
  // diffs (built from old_string/new_string/patch) break (#4928). Mirrors the
  // backend _TOOL_ARG_CONTENT_KEYS / _TOOL_ARG_CONTENT_CAP.
  const contentKeys=new Set(['command','cmd','script','code','patch','diff','old_string','new_string','content','path','file_path']);
  const CONTENT_CAP=4000;
  const keys=[
    ...priority.filter(k=>Object.prototype.hasOwnProperty.call(args,k)),
    ...Object.keys(args).filter(k=>!priority.includes(k)),
  ].slice(0,max);
  const out={};
  keys.forEach(k=>{
    const v=String(args[k]);
    const cap=contentKeys.has(String(k).toLowerCase())?CONTENT_CAP:120;
    let val=v.slice(0,cap)+(v.length>cap?'...':'');
    // Now that content args are retained up to 4000 chars (#4928), a secret on
    // a non-first line / past char 120 would otherwise reach the args block,
    // the Full tab, and clipboard copy unredacted. Redact at the snapshot so
    // every downstream renderer receives already-masked args (#4928 gate).
    if(typeof _redactToolTargetLabel==='function'){ try{ val=_redactToolTargetLabel(val); }catch(e){} }
    out[k]=val;
  });
  return out;
}

function _captureMessageScrollSnapshot(){
  const el=$('messages');
  if(!el) return null;
  const bottom=Math.max(0,el.scrollHeight-el.scrollTop-el.clientHeight);
  const readerAwayFromBottom=bottom>250&&(
    _messageUserUnpinned ||
    _scrollPinned===false ||
    (typeof _recentMessageScrollIntent==='function'&&_recentMessageScrollIntent())
  );
  return {
    anchor:(typeof _captureMessageViewportAnchor==='function')?_captureMessageViewportAnchor():null,
    top:el.scrollTop,
    bottom,
    scrollHeight:el.scrollHeight,
    pinned:readerAwayFromBottom?false:_shouldFollowMessagesOnDomReplace(),
    userUnpinned:readerAwayFromBottom?true:_messageUserUnpinned,
  };
}
function _restorePinnedMessageScrollSnapshot(snapshot){
  const el=$('messages');
  if(!el||!snapshot||snapshot.pinned!==true||snapshot.userUnpinned===true) return false;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  const bottom=Number(snapshot.bottom);
  const target=Number.isFinite(bottom)?maxTop-Math.max(0,bottom):maxTop;
  _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
  el.scrollTop=Math.max(0,Math.min(target,maxTop));
  // Sync _lastScrollTop after programmatic restore so sticky-unpin does not false-trigger (#1731).
  _lastScrollTop=el.scrollTop;_lastMessageClientHeight=el.clientHeight;
  _messageUserUnpinned=false;
  _scrollPinned=true;
  _nearBottomCount=2;
  if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
  else requestAnimationFrame(()=>{ setTimeout(()=>{ _programmaticScroll=false; },0); });
  return true;
}
function _restoreMessageScrollSnapshot(snapshot){
  const el=$('messages');
  if(!el||!snapshot) return;
  const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
  // If the reader was following the live tail, preserve the tail-relative bottom
  // distance. Do not semantic-anchor to the first visible row: live Worklog/
  // activity rebuilds can remount an older top-of-viewport anchor and yank a
  // pinned streaming transcript upward. Semantic anchors remain for manual
  // unpinned reading positions below.
  if(_restorePinnedMessageScrollSnapshot(snapshot)) return;
  let restoredViaAnchor=(snapshot.anchor&&typeof _restoreMessageViewportAnchor==='function')
    ? _restoreMessageViewportAnchor(snapshot.anchor,0)
    : false;
  if(!restoredViaAnchor&&typeof _remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor)){
    restoredViaAnchor=(typeof _restoreMessageViewportAnchor==='function')
      ? _restoreMessageViewportAnchor(snapshot.anchor,0)
      : false;
  }
  if(!restoredViaAnchor){
    _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
    el.scrollTop=Math.max(0,Math.min(Number(snapshot.top)||0,maxTop));
  }
  // Sync _lastScrollTop after programmatic restore so sticky-unpin does not false-trigger (#1731).
  _lastScrollTop=el.scrollTop;_lastMessageClientHeight=el.clientHeight;
  if(snapshot.userUnpinned===true){
    _messageUserUnpinned=true;
    _scrollPinned=false;
    _nearBottomCount=0;
  }else if(snapshot.pinned===true){
    _messageUserUnpinned=false;
    _scrollPinned=true;
    _nearBottomCount=2;
  }else{
    const bottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;
    if(bottomDistance>250){
      _messageUserUnpinned=true;
      _scrollPinned=false;
      _nearBottomCount=0;
    }else if(bottomDistance<=120){
      _messageUserUnpinned=false;
      _scrollPinned=true;
      _nearBottomCount=2;
    }
  }
  if(!restoredViaAnchor){
    if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
    else requestAnimationFrame(()=>{ setTimeout(()=>{ _programmaticScroll=false; },0); });
  }
}
/**
 * Mobile scroll-jank guard: temporarily disable overflow-anchor so
 * Chromium cannot re-anchor to the topmost row during the innerHTML=''
 * wipe-and-rebuild gap. The rAF callback restores CSS default afterward.
 */
// Mobile scroll jump-back root fix. On touch devices #messages rests at
// overflow-anchor:auto, so the browser's native scroll-anchoring engine
// re-compensates scrollTop in the LAYOUT phase whenever content above the
// viewport changes height — worklog live→settled collapse, tool-card inserts,
// media/katex reflow, virtual-scroll topPad recompute, the STREAM_DONE
// multi-render sequence. That compensation happens in the browser's layout step,
// INDEPENDENT of which frame our JS wrote scrollTop in, so per-write suppression
// (a single-rAF guard) could not reach it: the collapse/reflow lands a frame or
// two later, after the guard already released. Real mobile flight-recorder data
// (captured jumps with dTop -101/+350/+748/-400, call stack = rAF sampler only =
// NO JS frame) confirmed the compensation is the browser engine, not our scroll
// writes.
//
// Fix: DEFER the restore AND track CSS animations. Each call re-arms suppression
// and cancels any pending release, so a burst of renders (STREAM_DONE fires
// several back-to-back) shares ONE suppression window. The base window is two
// animation frames + a settle timeout, which covers churn that is NOT a CSS
// animation (virtual topPad recompute, image-decode, katex measure). But the
// dominant churn is CSS max-height collapse/expand animations on worklog rows —
// .activity-body (.34s), .tool-group-body (.3s), .tool-card-detail (.26s) — which
// run LONGER than a fixed window; a fixed window lifts mid-animation and the rest
// of the animation still jumps. So we also bind transitionrun/transitionend on
// #messages: an animation start holds suppression (cancels the pending release);
// an animation end schedules a short settle after the LAST one. Hard-capped so a
// looping transition can't pin overflow-anchor:none forever. Desktop rests at
// none (predicate false) → the whole guard is a no-op.
const _MOBILE_ANCHOR_BASE_SETTLE_MS=400;
const _MOBILE_ANCHOR_POST_TRANSITION_MS=90;
const _MOBILE_ANCHOR_MAX_HOLD_MS=1200;
let _mobileAnchorSuppressReleaseTimer=null;
let _mobileAnchorSuppressRafId=0;
let _mobileAnchorTransitionListenerBound=false;
let _mobileAnchorSuppressArmedAt=0;
// Independent hard-cap timer. Unlike the settle/rAF release (which the
// transitionrun handler CANCELS to hold across an animation), this one is NEVER
// cancelled by re-arm or by onRun — it is only ever cleared when suppression is
// actually lifted, and re-armed to a fresh deadline on each _fixMobileScrollJank
// call. This guarantees overflow-anchor returns to the mobile resting 'auto'
// even if EVERY transitionend/transitioncancel is missed (animation interrupted,
// element detached mid-transition, etc.) — the #5338 contract that mobile rests
// at 'auto' must hold no matter what. (Gate-cert defect: the previous
// _MOBILE_ANCHOR_MAX_HOLD_MS was only a guard clause inside onRun, so a missed
// transitionend pinned 'none' forever.)
let _mobileAnchorMaxHoldTimer=null;
function _liftMobileAnchorSuppression(el){
  if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
  if(_mobileAnchorMaxHoldTimer){ clearTimeout(_mobileAnchorMaxHoldTimer); _mobileAnchorMaxHoldTimer=null; }
  if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
  _mobileAnchorSuppressRafId=0;
  // Only clear the inline value we set; a concurrent path may have legitimately
  // re-armed it (checked via the 'none' guard).
  if(el&&el.style&&el.style.overflowAnchor==='none') el.style.overflowAnchor='';
}
function _bindMobileAnchorTransitionExtender(el){
  if(_mobileAnchorTransitionListenerBound||!el||!el.addEventListener) return;
  _mobileAnchorTransitionListenerBound=true;
  // Only act while suppression is actually armed (inline 'none') and within the
  // hard cap, so we never pin overflow-anchor:none indefinitely.
  const onRun=(e)=>{
    if(!e||e.propertyName!=='max-height') return;
    if(el.style.overflowAnchor!=='none') return;
    if(_mobileAnchorSuppressArmedAt && (performance.now()-_mobileAnchorSuppressArmedAt)>_MOBILE_ANCHOR_MAX_HOLD_MS) return;
    // An animation is running — cancel the pending SETTLE release so we stay
    // suppressed until it ends (transitionend re-schedules the settle). The
    // independent max-hold timer is deliberately NOT cancelled here.
    if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
    if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
    _mobileAnchorSuppressRafId=0;
  };
  const onEnd=(e)=>{
    if(!e||e.propertyName!=='max-height') return;
    if(el.style.overflowAnchor!=='none') return;
    // This animation ended; settle shortly after (another may still be running,
    // in which case its own transitionrun already cancelled this timer).
    if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); }
    _mobileAnchorSuppressReleaseTimer=setTimeout(()=>{
      _mobileAnchorSuppressReleaseTimer=null;
      _liftMobileAnchorSuppression(el);
    },_MOBILE_ANCHOR_POST_TRANSITION_MS);
  };
  el.addEventListener('transitionrun',onRun,{passive:true});
  el.addEventListener('transitionstart',onRun,{passive:true});
  el.addEventListener('transitionend',onEnd,{passive:true});
  el.addEventListener('transitioncancel',onEnd,{passive:true});
}
window._fixMobileScrollJank=function _fixMobileScrollJank(){
  const el=document.getElementById('messages');
  if(!el) return;
  // Engage when the browser scroll-anchor layer is active (mobile auto), OR when
  // WE are already holding an inline suppression from a prior call in the same
  // burst. The predicate reads the COMPUTED value, which our own inline
  // overflow-anchor:none flips to 'none' — so on the 2nd..Nth call of a
  // STREAM_DONE burst the predicate would say false and short-circuit the re-arm
  // below, collapsing the whole "consecutive renders extend the window" behavior
  // to a single first-call window. Treat an inline 'none' WE set as still-armed
  // so re-arm actually runs. Desktop rests at computed 'none' with EMPTY inline,
  // so `alreadySuppressed` is false there and this stays a no-op. (Gate-cert
  // defect: re-arm was dead code without this.)
  const alreadySuppressed=el.style.overflowAnchor==='none';
  if(!alreadySuppressed && !_browserOverflowAnchorActive(el)) return;
  el.style.overflowAnchor='none';
  _bindMobileAnchorTransitionExtender(el);
  _mobileAnchorSuppressArmedAt=performance.now();
  // Re-arm: cancel any pending release so consecutive renders EXTEND, not shorten,
  // the suppression window (the STREAM_DONE settle fires renderMessages several
  // times back-to-back, plus a deferred postProcess reflow).
  if(_mobileAnchorSuppressReleaseTimer){ clearTimeout(_mobileAnchorSuppressReleaseTimer); _mobileAnchorSuppressReleaseTimer=null; }
  if(_mobileAnchorSuppressRafId&&typeof cancelAnimationFrame==='function'){ cancelAnimationFrame(_mobileAnchorSuppressRafId); }
  _mobileAnchorSuppressRafId=0;
  // Independent hard cap: (re)arm a release that NOTHING cancels except an actual
  // lift, so a missed transitionend can never pin 'none' past the cap.
  if(_mobileAnchorMaxHoldTimer){ clearTimeout(_mobileAnchorMaxHoldTimer); }
  _mobileAnchorMaxHoldTimer=setTimeout(()=>{
    _mobileAnchorMaxHoldTimer=null;
    _liftMobileAnchorSuppression(el);
  },_MOBILE_ANCHOR_MAX_HOLD_MS);
  const rafHop=(cb)=>{ if(typeof requestAnimationFrame==='function') return requestAnimationFrame(cb); return setTimeout(cb,16); };
  // Base window: two animation frames (paint + post-render reflow settle) THEN a
  // settle timeout. CSS max-height animations are covered by the transitionrun/
  // transitionend extender above; this floor covers non-animated churn.
  _mobileAnchorSuppressRafId=rafHop(()=>{
    _mobileAnchorSuppressRafId=rafHop(()=>{
      _mobileAnchorSuppressReleaseTimer=setTimeout(()=>{
        _mobileAnchorSuppressReleaseTimer=null;
        _liftMobileAnchorSuppression(el);
      },_MOBILE_ANCHOR_BASE_SETTLE_MS);
    });
  });
};

// Desktop stale-snapshot residue (issue #5637 follow-up). Reached only when
// _restoreMessageViewportAnchor already CONCEDED (anchor row unrecoverable by its
// per-tier lookup) and the desktop fallback would otherwise write the ABSOLUTE
// snapshot.top — which is stale once above-viewport content grew since capture,
// yanking a still reader backward. The correct hold is the app's own realign
// idiom: shift the CURRENT scrollTop by how far the anchor row moved since capture,
// `scrollTop += (currentOffset - capturedOffset)` (mirrors _restoreMessageViewportAnchor
// ui.js and _compensateScrollForMeasurementDelta). NOT `snapshot.top + delta`: a
// row's offset is scroll-relative (rect.top - containerRect.top = rowContentPos -
// scrollTop), so only a delta applied to the LIVE scrollTop holds the row put
// regardless of where scrollTop was carried to. Returns the realign delta (may be
// 0), or null when the anchor row can't be measured under the SAME per-tier guard
// _restoreMessageViewportAnchor uses (key -> sessionIdx, never the rawIdx
// degradation — rawIdx maps to a different message after a virtualization
// re-window, ui.js per-tier guard) so the caller can fall back to the topPad-delta
// idiom or keep raw rather than guessing.
function _desktopAnchorRealignDelta(container, anchor){
  if(!container||!anchor||typeof container.querySelector!=='function') return null;
  const capturedOffset=Number(anchor.topOffset);
  if(!Number.isFinite(capturedOffset)) return null;
  const anchorKey=String(anchor.key||'');
  let row=anchorKey
    ? Array.from(container.querySelectorAll('[data-message-anchor-key]')).find(el=>el&&el.dataset&&el.dataset.messageAnchorKey===anchorKey)
    : null;
  if(row&&row.getClientRects&&row.getClientRects().length===0) row=null;
  const sessionIdx=Number(anchor.sessionIdx);
  if(!row&&Number.isFinite(sessionIdx)) row=container.querySelector(`[data-session-msg-idx="${sessionIdx}"]`);
  // Per-tier guard mirror (ui.js _restoreMessageViewportAnchor): a genuinely-gone
  // anchor misses key AND sessionIdx -> concede (null). Do NOT degrade to rawIdx.
  if(!row) return null;
  if(typeof row.getBoundingClientRect!=='function') return null;
  if(row.getClientRects&&row.getClientRects().length===0) return null;
  const containerRect=container.getBoundingClientRect();
  const rect=row.getBoundingClientRect();
  const currentOffset=rect.top-containerRect.top;
  return currentOffset-capturedOffset;
}

window.HermesUI.register('liveActivity', {
  showLiveRunStatus,
  updateLiveRunStatus,
  renderCompressionUi,
  clearMessageRenderCache,
});
