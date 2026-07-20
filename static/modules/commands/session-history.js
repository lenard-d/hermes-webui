function cmdClear(){
  if(!S.session)return;
  S.messages=[];S.toolCalls=[];
  clearLiveToolCards();
  if(typeof clearCompressionUi==='function') clearCompressionUi();
  renderMessages();
  $('emptyState').style.display='';
  showToast(t('conversation_cleared'));
}

async function cmdNew(){
  if(typeof clearCompressionUi==='function') clearCompressionUi();
  await newSession();
  await renderSessionList();
  $('msg').focus();
  showToast(t('new_session'));
}

async function cmdTitle(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const name=(args||'').trim();
  if(!name){
    S.messages.push({role:'assistant',content:`${t('title_current')}: **${S.session.title||t('untitled')}**\n\n${t('title_change_hint')}`});
    renderMessages();return;
  }
  try{
    const r=await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,title:name})});
    if(r&&r.error){showToast(r.error);return;}
    S.session.title=(r&&r.session&&r.session.title)||name;
    if(typeof syncTopbar==='function')syncTopbar();
    if(typeof renderSessionList==='function')renderSessionList();
    showToast(`${t('title_set')} "${S.session.title}"`);
    S.messages.push({role:'assistant',content:`${t('title_set')} **${S.session.title}**`});
    renderMessages();
  }catch(e){showToast(t('failed_colon')+e.message);}
}
async function cmdRetry(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid=S.session.session_id;
  // #5924: honor a genuine deliberate model pick across a recovery /retry without
  // forcing explicit_model_pick when there is no real pick. The single-shot marker
  // is already consumed by the failed send (messages.js clears it before
  // /api/chat/start regardless of outcome), so we can't read it back. Instead
  // derive the deliberate-pick signal the SAME way send()'s persistent path does:
  // the session's own model is a non-default pick vs the profile default. This is
  // NOT provider inference (no false positive) and survives marker consumption (no
  // false negative for an already-applied pick). Captured pre-await, scoped to
  // activeSid. No non-default pick → no re-arm → server compatible-model resolution runs.
  const _recoveryPick=_deliberateSessionModelPick(activeSid);
  try{
    const r=await api('/api/session/retry',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r&&r.error){showToast(r.error);return;}
    if(!S.session||S.session.session_id!==activeSid)return;
    const data=await api('/api/session?session_id='+encodeURIComponent(activeSid));
    // #5924 SILENT-race guard: a session switch during the GET await must not let
    // this recovery apply session A's intent to whatever session is now visible.
    if(!S.session||S.session.session_id!==activeSid)return;
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();if(typeof _messagesTruncated!=='undefined')_messagesTruncated=false;renderMessages();}
    $('msg').value=r.last_user_text||'';if(typeof autoResize==='function')autoResize();
    // Re-arm the single-shot explicit-pick marker from the captured non-default
    // pick — but only if it's still safe at fire time (session unchanged, current
    // model still matches the pick, and no newer onchange marker to clobber). See
    // _reArmRecoveryPick. Scoped to activeSid so it can't leak to another session.
    _reArmRecoveryPick(activeSid, _recoveryPick);
    await send();
  }catch(e){showToast(t('retry_failed')+e.message);}
}
async function cmdUndo(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(S.session.is_cli_session){showToast(t('cmd_webui_only_session'));return;}
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/session/undo',{method:'POST',body:JSON.stringify({session_id:activeSid})});
    if(r&&r.error){showToast(r.error);return;}
    if(!S.session||S.session.session_id!==activeSid)return;
    const data=await api('/api/session?session_id='+encodeURIComponent(activeSid));
    if(data&&data.session){S.messages=data.session.messages||[];S.toolCalls=[];if(typeof clearLiveToolCards==='function')clearLiveToolCards();if(typeof _messagesTruncated!=='undefined')_messagesTruncated=false;renderMessages();}
    showToast(`↩ ${t('undid_n_messages')} ${r.removed_count} ${t('undid_messages_suffix')}`);
  }catch(e){showToast(t('undo_failed')+e.message);}
}
async function undoLastExchange(){await cmdUndo();}
async function cmdBtw(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const question=(args||'').trim();
  if(!question){showToast(t('cmd_btw_usage'));return;}
  showToast(t('btw_asking'));
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/btw',{method:'POST',body:JSON.stringify({session_id:activeSid,question})});
    if(r&&r.error){showToast(r.error);return;}
    // Connect to the ephemeral SSE stream
    const streamId=r.stream_id;
    const parentSid=r.parent_session_id;
    if(typeof attachBtwStream==='function') attachBtwStream(parentSid,streamId,question);
  }catch(e){showToast(t('btw_failed')+e.message);}
}
async function cmdBackground(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const prompt=(args||'').trim();
  if(!prompt){showToast(t('cmd_background_usage'));return;}
  showToast(t('bg_running'));
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/background',{method:'POST',body:JSON.stringify({session_id:activeSid,prompt})});
    if(r&&r.error){showToast(r.error);return;}
    // Show background badge and start polling
    if(typeof showBackgroundBadge==='function') showBackgroundBadge(r.task_id);
    if(typeof startBackgroundPolling==='function') startBackgroundPolling(activeSid,r.task_id,prompt);
  }catch(e){showToast(t('bg_failed')+e.message);}
}
function _formatStatusTimestamp(value){
  if(value===undefined||value===null||value==='') return t('status_unknown');
  let date;
  if(typeof value==='number') date=new Date(value < 1000000000000 ? value*1000 : value);
  else date=new Date(value);
  if(Number.isNaN(date.getTime())) return t('status_unknown');
  return date.toLocaleString();
}
function _formatStatusTokens(s){
  const lastUsage=(typeof S!=='undefined'&&(S.lastUsage||s.last_usage))||{};
  const input=Number(s.input_tokens??lastUsage.input_tokens??0)||0;
  const output=Number(s.output_tokens??lastUsage.output_tokens??0)||0;
  const total=Number(s.total_tokens??lastUsage.total_tokens??(input+output))||0;
  const cost=Number(s.estimated_cost??lastUsage.estimated_cost??0)||0;
  if(!total&&!cost) return t('status_no_tokens');
  const fmtNum=n=>Number(n||0).toLocaleString();
  return `${fmtNum(input)} in / ${fmtNum(output)} out${cost?` (~$${cost.toFixed(4)})`:''}`;
}
function _statusProviderForSession(s){
  if(s.model_provider) return String(s.model_provider);
  if(window._activeProvider) return String(window._activeProvider);
  const model=String(s.model||'');
  return model.includes('/') ? model.split('/')[0] : '';
}
function _statusCardFromSession(s){
  const provider=_statusProviderForSession(s);
  const model=s.model||(($('modelSelect')&&$('modelSelect').value)||t('usage_default_model'));
  const running=!!(s.active_stream_id||S.activeStreamId||S.busy);
  const profile=s.profile||S.activeProfile||'default';
  const workspace=s.workspace||S.currentDir||t('status_unknown');
  const rows=[
    {label:t('status_session_id'), value:s.session_id||t('status_unknown')},
    {label:t('status_title'), value:s.title||t('untitled')},
    {label:t('status_model'), value:model},
    {label:t('status_provider'), value:provider||t('status_unknown')},
    {label:t('status_profile'), value:profile},
    {label:t('status_workspace'), value:workspace},
    {label:t('status_personality'), value:s.personality||t('usage_personality_none')},
    {label:t('status_started'), value:_formatStatusTimestamp(s.created_at)},
    {label:t('status_updated'), value:_formatStatusTimestamp(s.updated_at||s.last_message_at)},
    {label:t('status_tokens'), value:_formatStatusTokens(s)},
    {label:t('status_messages'), value:String(s.message_count??(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length)},
    {label:t('status_agent_running'), value:running?t('status_yes'):t('status_no')},
  ];
  return {
    title:t('status_heading'),
    subtitle:t('status_ephemeral'),
    sessionId:s.session_id||'',
    rows,
  };
}
function cmdStatus(){
  if(!S.session){showToast(t('no_active_session'));return;}
  S.messages.push({
    role:'assistant',
    content:'',
    _ephemeral:true,
    _statusCard:_statusCardFromSession(S.session),
    _ts:Date.now()/1000,
  });
  renderMessages();
}

// ── Branch / fork command ──
// Forks the current conversation into a new session (#465).
// /branch           → full history copy
// /branch My Name   → full history copy with custom title
async function cmdBranch(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  const readOnlySession=typeof _isReadOnlySession==='function'
    ? _isReadOnlySession(S.session)
    : !!(S.session&&(S.session.read_only||S.session.is_read_only));
  const branchableReadOnlySession=typeof _isBranchableReadOnlySession==='function'
    ? _isBranchableReadOnlySession(S.session)
    : false;
  if(readOnlySession&&!branchableReadOnlySession){showToast('Read-only sessions cannot be forked.',3000);return;}
  const customTitle=(args||'').trim()||null;
  try{
    const data=await api('/api/session/branch',{
      method:'POST',
      body:JSON.stringify({
        session_id:S.session.session_id,
        title:customTitle||undefined,
      }),
    });
    if(data&&data.session_id){
      await loadSession(data.session_id);
      if(typeof renderSessionList==='function') await renderSessionList();
      showToast(t('branch_forked'));
    }
  }catch(e){showToast(t('branch_failed')+e.message);}
}

// ── Fork from a specific message point ──
// Called from the "Fork from here" button on message hover actions.
// msgIdx is 1-based within the currently loaded tail window (rawIdx+1).
// When the session is truncated (_oldestIdx > 0), msgIdx alone would be
// a local-window count, but the backend expects an absolute message count
// from the beginning of the full transcript.  We capture the absolute
// count (_oldestIdx + msgIdx) BEFORE awaiting _ensureAllMessagesLoaded,
// which resets _oldestIdx to 0 after its wholesale replace.  See #2184.
async function forkFromMessage(msgIdx){
  if(!S.session)return;
  // During streaming, only block fork if the clicked message is the
  // currently-streaming (live) message itself.  Past messages that are
  // already committed server-side can be forked immediately without
  // waiting for the stream to finish.
  if(S.busy){
    const _msg=(Array.isArray(S.messages)&&S.messages[msgIdx-1])||null;
    const _isLastMsg=msgIdx>=(Array.isArray(S.messages)?S.messages.length:0);
    if((_msg&&(_msg._live||_msg._pending))||_isLastMsg){
      showToast('Cannot fork a message still being generated.',3000);
      return;
    }
  }
  const readOnlySession=typeof _isReadOnlySession==='function'
    ? _isReadOnlySession(S.session)
    : !!(S.session&&(S.session.read_only||S.session.is_read_only));
  const branchableReadOnlySession=typeof _isBranchableReadOnlySession==='function'
    ? _isBranchableReadOnlySession(S.session)
    : false;
  if(readOnlySession&&!branchableReadOnlySession){showToast('Read-only sessions cannot be forked.',3000);return;}
  const initialSid = S.session.session_id;
  // Capture the absolute keep_count before any async work that may
  // reset _oldestIdx.  _oldestIdx is 0 when the full transcript is
  // already loaded, so short/already-full sessions send msgIdx unchanged.
  const absoluteKeepCount = _oldestIdx + msgIdx;
  // Ensure the full transcript is loaded so the forked session renders
  // correctly and subsequent operations see the complete history.
  // Skip during streaming to avoid visual flicker — the fork data
  // (absoluteKeepCount) was already captured above and is unaffected.
  if(!S.busy && typeof _ensureAllMessagesLoaded==='function'){
    await _ensureAllMessagesLoaded();
  }
  if(!S.session || S.session.session_id !== initialSid) return;
  try{
    const data=await api('/api/session/branch',{
      method:'POST',
      body:JSON.stringify({
        session_id:initialSid,
        keep_count:absoluteKeepCount,
      }),
    });
    if(data&&data.session_id){
      await loadSession(data.session_id);
      if(typeof _ensureAllMessagesLoaded==='function') await _ensureAllMessagesLoaded();
      if(typeof renderSessionList==='function') await renderSessionList();
      showToast(t('branch_forked'));
    }
  }catch(e){showToast(t('branch_failed')+e.message);}
}

export {
  cmdBackground,
  cmdBranch,
  cmdBtw,
  cmdClear,
  cmdNew,
  cmdRetry,
  cmdStatus,
  cmdTitle,
  cmdUndo,
  forkFromMessage,
  undoLastExchange,
};
