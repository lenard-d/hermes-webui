async function cmdStop(){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(!S.activeStreamId){showToast(t('no_active_task'));return;}
  if(typeof cancelStream==='function'){await cancelStream('slash-stop');showToast(t('stream_stopped'));}
  else showToast(t('cancel_unavailable'));
}

async function cmdGoal(args){
  if(!S.session){await newSession();await renderSessionList();}
  if(!S.session||!S.session.session_id){showToast(t('no_active_session'));return;}
  const activeSid=S.session.session_id;
  try{
    const r=await api('/api/goal',{method:'POST',body:JSON.stringify({
      session_id:activeSid,
      args:args||'',
      workspace:S.session.workspace,
      model:S.session.model||($('modelSelect')&&$('modelSelect').value)||'',
      model_provider:S.session.model_provider||null,
      profile:S.activeProfile||S.session.profile||'default',
    })});
    const msg = (() => {
      const raw = String((r && r.message) || '').trim();
      const key = String((r && r.message_key) || '').trim();
      const args = Array.isArray(r && r.message_args) ? r.message_args : [];
      if (raw.includes('\n')) return raw;
      if (key && typeof t === 'function') {
        const translated = String(t(key, ...args));
        if (translated && translated !== key) return translated;
      }
      return raw;
    })();
    if(msg){
      S.messages.push({role:'assistant',content:msg,_ts:Date.now()/1000,_goalStatus:true,_transient:true});
      renderMessages({preserveScroll:true});
      showToast(msg.split('\n')[0],2600);
    }
    if(!r||!r.stream_id)return;
    S.toolCalls=[];
    if(typeof clearLiveToolCards==='function')clearLiveToolCards();
    appendThinking();setBusy(true);
    setComposerStatus(t('goal_working_toward'));
    S.activeStreamId=r.stream_id;
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id=r.stream_id;
      if(typeof r.pending_started_at==='number')S.session.pending_started_at=r.pending_started_at;
      if(r.effective_model)S.session.model=r.effective_model;
      if(r.effective_model_provider)S.session.model_provider=r.effective_model_provider;
    }
    INFLIGHT[activeSid]={messages:[...S.messages],uploaded:[],toolCalls:[]};
    if(typeof markInflight==='function')markInflight(activeSid,r.stream_id);
    if(typeof saveInflightState==='function')saveInflightState(activeSid,{streamId:r.stream_id,messages:INFLIGHT[activeSid].messages,uploaded:[],toolCalls:[]});
    startApprovalPolling(activeSid);
    startClarifyPolling(activeSid);
    if(typeof _fetchYoloState==='function')_fetchYoloState(activeSid);
    attachLiveStream(activeSid,r.stream_id,[]);
    if(typeof renderSessionList==='function')void renderSessionList();
  }catch(e){
    const err=String((e&&e.message)||e||'Goal command failed');
    S.messages.push({role:'assistant',content:`**Goal command failed:** ${err}`,_ts:Date.now()/1000,_error:true});
    renderMessages({preserveScroll:true});
    showToast(err,3000);
  }
}

// ── Busy-input mode commands ──────────────────────────────────────────────
// These commands let users override the default message mode setting for a
// specific message.  They are only meaningful while the agent is running.

/**
 * /queue <message> — Explicitly queue a message for the next turn.
 * Works regardless of the default message mode setting.
 */
async function cmdQueue(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_queue_no_msg'));return;}
  // If nothing is running, /queue <msg> just sends like a normal message
  if(!S.busy){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  showToast(t('cmd_queue_confirm'),2000);
}

/**
 * /interrupt <message> — Cancel the current turn and send a new message.
 * Calls cancelStream() then queues the message so the drain picks it up.
 */
async function cmdInterrupt(args){
  const msg=(args||'').trim();
  if(!msg){showToast(t('cmd_interrupt_no_msg'));return;}
  // If nothing is running, /interrupt <msg> just sends like a normal message
  if(!S.busy||!S.activeStreamId){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  // Queue the message first (before cancel sets busy=false and drains)
  queueSessionMessage(S.session.session_id,{text:msg,files:[...S.pendingFiles],model:S.session&&S.session.model||($('modelSelect')&&$('modelSelect').value)||'',profile:S.activeProfile||'default'});
  updateQueueBadge(S.session.session_id);
  S.pendingFiles=[];renderTray();
  // Cancel the active stream; setBusy(false) will drain the queue
  if(typeof cancelStream==='function'){await cancelStream('slash-interrupt');}
  showToast(t('cmd_interrupt_confirm'),2000);
}

/**
 * /steer <message> — Inject a steering hint mid-task without interrupting.
 *
 * Calls POST /api/chat/steer which looks up the cached AIAgent for this
 * session and calls agent.steer(text). The agent's run loop appends the
 * steer text to the next tool-result message so the model sees it on its
 * next iteration — same pathway as the CLI's /steer command.
 *
 * Leaves the active stream alone when the agent isn't running, isn't cached,
 * or doesn't support steer (older hermes-agent versions). The failed steer text
 * is restored to the composer so the user can choose Queue or Interrupt
 * explicitly instead of WebUI silently cancelling the current run.
 */
async function cmdSteer(args){
  const msg=(args||'').trim();
  const hasPendingFiles=typeof S!=='undefined'&&Array.isArray(S.pendingFiles)&&S.pendingFiles.length>0;
  if(!msg&&!hasPendingFiles){showToast(t('cmd_steer_no_msg'));return;}
  // If nothing is running, /steer <msg> just sends like a normal message
  if(!S.busy||!S.activeStreamId){
    const inp=$('msg');
    if(inp){inp.value=msg;}
    if(typeof send==='function'){await send();}
    return;
  }
  if(!S.session){showToast(t('no_active_session'));return;}
  await _trySteer(msg, /*explicitSteer=*/true);
}

function _steerFailureMessageKey(fallback) {
  if(fallback==='gateway_steer_queued')return 'steer_fail_no_cached_agent';
  const key = 'steer_fail_' + (fallback || 'unknown');
  return (typeof LOCALES !== 'undefined' && LOCALES.en && LOCALES.en[key])
    ? key : 'steer_fail_unknown';
}

function _showSteerIndicator(text){
  const inner=document.getElementById('msgInner');
  if(!inner) return;
  // Remove any existing steer indicator
  const old=inner.querySelector('.steer-indicator');
  if(old) old.remove();
  const el=document.createElement('div');
  el.className='steer-indicator';
  const badge=document.createElement('span');
  badge.className='steer-badge';
  badge.textContent='Steer';
  const body=document.createElement('span');
  body.className='steer-body';
  body.textContent=text.length>120?text.slice(0,117)+'…':text;
  el.appendChild(badge);
  el.appendChild(body);
  inner.appendChild(el);
  if(typeof scrollToBottom==='function') scrollToBottom();
}

function _showSteerRecovery(msg, explicitSteer, fallback) {
  const inner = document.getElementById('msgInner');
  if (!inner) return;
  const old = inner.querySelector('.steer-recovery');
  if (old) old.remove();
  const el = document.createElement('div');
  el.className = 'steer-recovery';
  const label = document.createElement('span');
  label.className = 'steer-recovery-label';
  label.textContent = t(_steerFailureMessageKey(fallback));
  el.appendChild(label);
  const retryBtn = document.createElement('button');
  retryBtn.className = 'steer-recovery-retry';
  retryBtn.textContent = t(_steerFallbackIsDeadRun(fallback)?'clarify_send':'steer_recovery_retry');
  retryBtn.addEventListener('click', () => {
    el.remove();
    if(_steerFallbackIsDeadRun(fallback)&&typeof send==='function'){
      if(explicitSteer){
        const inp=$('msg');
        if(inp){
          inp.value=String(msg||'').trim();
          if(typeof autoResize==='function')autoResize();
        }
      }
      void send({literalSlash:true}).catch(console.error);
    }else{
      void _trySteer(msg, explicitSteer).catch(console.error);
    }
  });
  el.appendChild(retryBtn);
  const dismissBtn = document.createElement('button');
  dismissBtn.className = 'steer-recovery-dismiss';
  dismissBtn.textContent = t('steer_recovery_dismiss');
  dismissBtn.addEventListener('click', () => el.remove());
  el.appendChild(dismissBtn);
  inner.appendChild(el);
  if (typeof scrollToBottom === 'function') scrollToBottom();
}

/**
 * Shared implementation for /steer and the default_message_mode='steer' path.
 *
 * Tries the real steer endpoint first. On any non-accept response (no cached
 * agent, agent lacks steer, stream dead, etc.) it restores the draft and keeps
 * the active stream running. Steer belongs to the active run; a failed Steer
 * must not be silently upgraded into Queue, Interrupt, or Stop-and-send.
 *
 * @param {string} msg - The steer text.
 * @param {boolean} explicitSteer - True if the user explicitly invoked /steer
 *   (vs the busy-mode auto-fallback). Affects draft restore prefix only;
 *   toast wording is determined by the failure reason code.
 * @returns {Promise<boolean>} true when the steer was delivered, false when the
 *   draft was restored and the active stream was left untouched.
 */
function _steerUploadedAttachmentPaths(uploaded){
  if(!Array.isArray(uploaded))return[];
  return uploaded.map(u=>{
    if(!u)return'';
    if(typeof u==='string')return u;
    return u.path||u.name||u.filename||'';
  }).map(v=>String(v||'').trim()).filter(Boolean);
}

function _steerOwnerIsCurrent(ownerSid){
  return !!(ownerSid&&typeof S!=='undefined'&&S.session&&S.session.session_id===ownerSid);
}

function _steerFallbackIsDeadRun(fallback){
  return fallback==='stream_dead';
}

function _steerOwnerStreamIsCurrent(ownerSid, ownerStreamId){
  if(!_steerOwnerIsCurrent(ownerSid)||typeof S==='undefined'||!ownerStreamId)return false;
  const activeIds=[S.activeStreamId,S.session&&S.session.active_stream_id].filter(Boolean).map(String);
  return activeIds.length>0&&activeIds.every(id=>id===String(ownerStreamId));
}

function _steerClearCurrentOwnerDeadRun(ownerSid, ownerStreamId){
  if(!_steerOwnerStreamIsCurrent(ownerSid,ownerStreamId))return false;
  let changed=false;
  if(S.busy){S.busy=false;changed=true;}
  if(S.activeStreamId){S.activeStreamId=null;changed=true;}
  if(S.session&&S.session.active_stream_id){S.session.active_stream_id=null;changed=true;}
  if(typeof INFLIGHT!=='undefined'&&INFLIGHT&&INFLIGHT[ownerSid]){
    delete INFLIGHT[ownerSid];
    changed=true;
  }
  if(changed&&typeof clearInflightState==='function')clearInflightState(ownerSid);
  if(changed){
    // Finish the stale-busy cleanup idiom the rest of the app uses so a recovered
    // dead run leaves no lingering status line, elapsed timer, or streaming badge
    // (#5744 UX-gate). Deliberately does NOT call setBusy(false): its queue-drain
    // would clobber the draft text this recovery path restores.
    if(typeof setStatus==='function')setStatus('');
    if(typeof setComposerStatus==='function')setComposerStatus('');
    if(typeof _clearActivityElapsedTimer==='function')_clearActivityElapsedTimer();
    if(typeof clearOptimisticSessionStreaming==='function')clearOptimisticSessionStreaming(ownerSid);
  }
  if(changed&&typeof updateSendBtn==='function')updateSendBtn();
  return changed;
}

function _steerSetComposerStatusForOwner(ownerSid,text){
  if(_steerOwnerIsCurrent(ownerSid)&&typeof setComposerStatus==='function')setComposerStatus(text);
}

function _steerRestoreText(originalMsg, explicitSteer){
  return explicitSteer?`/steer ${originalMsg}`:originalMsg;
}

function _steerIndicatorText(originalMsg, filesSnapshot){
  const text=String(originalMsg||'').trim();
  if(text)return text;
  const names=(Array.isArray(filesSnapshot)?filesSnapshot:[])
    .map(f=>f&&(f.name||f.filename||f.path||''))
    .map(v=>String(v||'').trim())
    .filter(Boolean);
  return names.length?`Attached files: ${names.join(', ')}`:'Attached files';
}

async function _steerPersistDraftForOwner(ownerSid, originalMsg, explicitSteer, filesSnapshot){
  if(!ownerSid||typeof _saveComposerDraftNow!=='function')return;
  await _saveComposerDraftNow(ownerSid,_steerRestoreText(originalMsg,explicitSteer),filesSnapshot);
}

// #5459 gate: cache successful steer uploads by owner session so a failed-steer
// RETRY reuses the uploaded paths instead of re-uploading the same File objects.
// Keyed by ownerSid; invalidated when the staged file set changes or on accepted
// steer (see _steerUploadCacheMatches / clearing below).
let _steerUploadCache = null; // { sid, sig, paths }
function _steerFilesSignature(files){
  try{
    return (Array.isArray(files)?files:[]).map(f=>f&&(f.name+':'+(f.size||0)+':'+(f.lastModified||0))).join('|');
  }catch(_){return String(Date.now());}
}

async function _steerTextWithPendingFiles(msg, ownerSid, filesSnapshot){
  const base=String(msg||'').trim();
  const pendingFiles=Array.isArray(filesSnapshot)?filesSnapshot.filter(Boolean):[];
  if(!pendingFiles.length)return base;
  if(typeof uploadPendingFiles!=='function')return base;
  const sig=_steerFilesSignature(pendingFiles);
  // Reuse a prior successful upload for the same session + identical staged file
  // set (a steer that failed and is being retried) — don't upload twice.
  let paths=null;
  if(_steerUploadCache&&_steerUploadCache.sid===ownerSid&&_steerUploadCache.sig===sig&&Array.isArray(_steerUploadCache.paths)&&_steerUploadCache.paths.length){
    paths=_steerUploadCache.paths;
  }else{
    _steerSetComposerStatusForOwner(ownerSid,t('uploading')||'Uploading…');
    let uploaded=[];
    try{
      // Keep File objects staged until /api/chat/steer confirms acceptance. If
      // steer falls back, the draft and chips stay available for Queue/Interrupt.
      uploaded=await uploadPendingFiles({clearPending:false,sessionId:ownerSid,files:pendingFiles});
    }finally{
      _steerSetComposerStatusForOwner(ownerSid,'');
    }
    paths=_steerUploadedAttachmentPaths(uploaded);
    if(paths.length) _steerUploadCache={sid:ownerSid,sig,paths};
  }
  if(!paths||!paths.length)return base;
  const note=`[Attached files for this steer: ${paths.join(', ')}]\nUse the file tools/read_file to inspect these documents if needed.`;
  return base?`${base}\n\n${note}`:note;
}

async function _trySteer(msg, explicitSteer){
  let result=null;
  const originalMsg=String(msg||'').trim();
  const ownerSid=(typeof S!=='undefined'&&S.session&&S.session.session_id)||null;
  const ownerStreamId=(typeof S!=='undefined'&&(S.activeStreamId||(S.session&&S.session.active_stream_id)))||null;
  const pendingFilesSnapshot=typeof S!=='undefined'&&Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];
  const ownerProfile=typeof S!=='undefined'&&(S.activeProfile||'default');
  const ownerModelState=typeof _chatPayloadModelState==='function'
    ? _chatPayloadModelState()
    : {model:(typeof S!=='undefined'&&S.session&&S.session.model)||'',model_provider:(typeof S!=='undefined'&&S.session&&S.session.model_provider)||''};
  if(!ownerSid){showToast(t('no_active_session'));return false;}
  let steerText=originalMsg;
  try{
    steerText=await _steerTextWithPendingFiles(originalMsg,ownerSid,pendingFilesSnapshot);
  }catch(e){
    if(_steerOwnerIsCurrent(ownerSid)){
      const inp=$('msg');
      if(inp){
        inp.value=_steerRestoreText(originalMsg,explicitSteer);
        if(typeof autoResize==='function')autoResize();
      }
      if(typeof renderTray==='function')renderTray();
    }else{
      await _steerPersistDraftForOwner(ownerSid,originalMsg,explicitSteer,pendingFilesSnapshot);
    }
    _steerSetComposerStatusForOwner(ownerSid,'');
    showToast(`${t('upload_failed')}${e&&e.message?e.message:e}`,3500);
    return false;
  }
  if(!steerText){
    if(_steerOwnerIsCurrent(ownerSid)){
      const inp=$('msg');
      if(inp){
        inp.value=_steerRestoreText(originalMsg,explicitSteer);
        if(typeof autoResize==='function')autoResize();
      }
      if(typeof renderTray==='function')renderTray();
    }else{
      await _steerPersistDraftForOwner(ownerSid,originalMsg,explicitSteer,pendingFilesSnapshot);
    }
    showToast(t('cmd_steer_no_msg'));
    return false;
  }
  try{
    result=await api('/api/chat/steer',{
      method:'POST',
      body:JSON.stringify({session_id:ownerSid,text:steerText}),
    });
  }catch(e){
    // Network or server error — keep the active stream running and restore the draft.
    result={accepted:false, fallback:'network_error'};
  }
  if(result&&result.accepted){
    // The captured files+text were delivered to ownerSid — clear that session's
    // draft (it may not be the live session anymore if the user switched during
    // the upload/API await, which is fine: we're clearing the OWNER's draft).
    _steerUploadCache=null; // delivered — invalidate the retry cache
    if(ownerSid&&typeof _clearComposerDraft==='function') _clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot);
    // Show a transient steer indicator in the chat (NOT in S.messages — it must
    // survive the done event's S.messages=d.session.messages replacement).
    // The indicator self-removes when the turn completes (done/cancel/error
    // all call renderMessages which rebuilds msgInner). Only mutate the visible
    // tray/DOM if the user is still looking at the owning session.
    if(_steerOwnerIsCurrent(ownerSid)){
      // Remove ONLY the files we captured+delivered, by object identity, so any
      // files staged during the upload/API await are preserved (#5459 gate).
      if(typeof S!=='undefined'&&Array.isArray(S.pendingFiles)&&S.pendingFiles.length&&pendingFilesSnapshot.length){
        const _delivered=new Set(pendingFilesSnapshot);
        const _remaining=S.pendingFiles.filter(f=>!_delivered.has(f));
        if(_remaining.length!==S.pendingFiles.length){S.pendingFiles=_remaining;if(typeof renderTray==='function')renderTray();}
      }
      _showSteerIndicator(_steerIndicatorText(originalMsg,pendingFilesSnapshot));
    }
    showToast(t('cmd_steer_delivered'),2500);
    return true;
  }
  if(result&&result.fallback==='gateway_steer_queued'&&typeof queueSessionMessage==='function'){
    _steerUploadCache=null;
    queueSessionMessage(ownerSid,{
      text:originalMsg,
      files:pendingFilesSnapshot,
      model:ownerModelState.model,
      model_provider:ownerModelState.model_provider,
      profile:ownerProfile,
    });
    if(typeof updateQueueBadge==='function')updateQueueBadge(ownerSid);
    if(ownerSid&&typeof _clearComposerDraft==='function') _clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot);
    if(_steerOwnerIsCurrent(ownerSid)&&typeof S!=='undefined'&&Array.isArray(S.pendingFiles)&&S.pendingFiles.length&&pendingFilesSnapshot.length){
      const _queued=new Set(pendingFilesSnapshot);
      const _remaining=S.pendingFiles.filter(f=>!_queued.has(f));
      if(_remaining.length!==S.pendingFiles.length){S.pendingFiles=_remaining;if(typeof renderTray==='function')renderTray();}
    }
    showToast(t('steer_leftover_queued'),3000);
    return true;
  }
  // Do not fall back to interrupt: Steer failure is not permission to cancel
  // the active run. Restore the draft so the user can explicitly Queue or
  // Interrupt if that is what they want next. Pending files remain staged.
  const fallbackCode = result && result.fallback;
  const deadRunFallback = _steerFallbackIsDeadRun(fallbackCode);
  const applyCurrentFailure = !deadRunFallback||_steerOwnerStreamIsCurrent(ownerSid,ownerStreamId);
  if(_steerOwnerIsCurrent(ownerSid)&&applyCurrentFailure){
    const inp=$('msg');
    if(inp){
      inp.value=_steerRestoreText(originalMsg,explicitSteer);
      if(typeof autoResize==='function')autoResize();
    }
    if(typeof renderTray==='function')renderTray();
  }else{
    await _steerPersistDraftForOwner(ownerSid,originalMsg,explicitSteer,pendingFilesSnapshot);
  }
  const clearedDeadRun=deadRunFallback&&_steerClearCurrentOwnerDeadRun(ownerSid,ownerStreamId);
  if(!deadRunFallback||applyCurrentFailure)showToast(t(_steerFailureMessageKey(fallbackCode)), 3500);
  if(_steerOwnerIsCurrent(ownerSid)&&(!deadRunFallback||clearedDeadRun)) _showSteerRecovery(originalMsg, explicitSteer, fallbackCode);
  return false;
}

// ── YOLO mode toggle ──
// Session-scoped: skips all approval prompts for the current session.
// Toggles on/off; state is not persisted across page reloads.
async function cmdYolo(){
  const sid=S.session&&S.session.session_id;
  if(!sid){showToast(t('yolo_no_session'));return;}
  try{
    // Check current state first to toggle
    const status=await api('/api/session/yolo?session_id='+encodeURIComponent(sid));
    const enable=!status.yolo_enabled;
    await api('/api/session/yolo',{
      method:'POST',
      body:JSON.stringify({session_id:sid,enabled:enable}),
    });
    _yoloEnabled=enable;
    _updateYoloPill();
    showToast(enable?t('yolo_enabled'):t('yolo_disabled'));
    if(enable){
      // Dismiss any visible approval card
      hideApprovalCard(true);
    }
  }catch(e){showToast('YOLO: '+e.message);}
}

export {cmdGoal,cmdInterrupt,cmdQueue,cmdSteer,cmdStop,cmdYolo,_trySteer};
