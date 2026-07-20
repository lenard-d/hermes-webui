var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

// Guard against concurrent send() calls.  Without this, two rapid sends
// (e.g. queue drain + user click) can both pass the S.busy check because
// setBusy(true) is only called after the first await inside send().
let _sendInProgress = false;
let _sendInProgressSid = null;  // session_id of the in-flight send
const _sessionTitleProvisionalBySid = new Map();
// Agent commands that are safe to execute directly in the WebUI even though
// their canonical command is registered on the backend (for example
// /reload-mcp). Keep this intentionally narrow and include underscore variants
// observed by users so typing either form still routes through executeAgentCommand.
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set(['reload-mcp', 'reload_mcp', 'reload-skills', 'reload_skills', 'codex-runtime', 'codex_runtime', 'credits']);

function _clearStaleBusyStateBeforeSend({compressionRunning=false}={}){
  if(!S||!S.busy||compressionRunning) return false;
  const session=S.session||{};
  const sid=session.session_id||'';
  const hasRuntimeConfirmation=Boolean(
    S.activeStreamId||
    session.active_stream_id||
    session.pending_user_message||
    session.pending_started_at
  );
  if(hasRuntimeConfirmation) return false;
  if(typeof INFLIGHT==='object'&&INFLIGHT&&sid&&INFLIGHT[sid]){
    delete INFLIGHT[sid];
    if(typeof clearInflightState==='function') clearInflightState(sid);
  }
  S.activeStreamId=null;
  if(session) session.active_stream_id=null;
  if(typeof setBusy==='function') setBusy(false);
  else S.busy=false;
  if(typeof setComposerStatus==='function') setComposerStatus('');
  if(typeof setStatus==='function') setStatus('');
  if(typeof updateSendBtn==='function') updateSendBtn();
  if(sid&&typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(sid);
  return true;
}

function _runOptionalPreStartUiStep(label, fn){
  try{
    return typeof fn==='function'?fn():undefined;
  }catch(e){
    const message=e&&e.message?e.message:String(e||'unknown error');
    try{console.warn('[webui] optional pre-start UI step failed', label, message);}catch(_){ }
    return undefined;
  }
}

function _runOptionalPostStartUiStep(label, fn){
  try{
    return typeof fn==='function'?fn():undefined;
  }catch(e){
    const message=e&&e.message?e.message:String(e||'unknown error');
    try{console.warn('[webui] optional post-start UI step failed', label, message);}catch(_){ }
    return undefined;
  }
}

function _sessionTitleLooksDefaultOrProvisional(titleText, provisionalText){
  const title=String(titleText||'').replace(/\s+/g,' ').trim();
  if(!title||title==='Untitled'||title==='New Chat')return true;
  const provisional=String(provisionalText||'').replace(/\s+/g,' ').trim().slice(0,64);
  return !!provisional&&title===provisional;
}

function _firstUserMessageTitleCandidate(){
  const first=(S.messages||[]).find(m=>m&&m.role==='user'&&m.content);
  return first?String(first.content||'').trim().slice(0,64):'';
}

function applySessionTitleUpdate(sid, titleText, options={}){
  const newTitle=String(titleText||'').trim();
  if(!sid||!newTitle)return false;
  const row=(typeof _allSessions!=='undefined'&&Array.isArray(_allSessions))
    ? _allSessions.find(s=>s&&s.session_id===sid)
    : null;
  const currentTitle=S.session&&S.session.session_id===sid
    ? S.session.title
    : row&&row.title;
  if(!options.force){
    const expected=String(options.expectedCurrent||'').trim();
    const remembered=_sessionTitleProvisionalBySid.get(sid)||'';
    const provisionalCandidates=[options.provisionalText,remembered,_firstUserMessageTitleCandidate()];
    const allowed=(expected&&String(currentTitle||'').trim()===expected)
      || String(currentTitle||'').trim()===newTitle
      || provisionalCandidates.some(p=>_sessionTitleLooksDefaultOrProvisional(currentTitle, p));
    if(!allowed)return false;
  }
  if(S.session&&S.session.session_id===sid){
    S.session.title=newTitle;
    if(typeof syncTopbar==='function') syncTopbar();
  }
  if(row) row.title=newTitle;
  if(options.rememberProvisional) _sessionTitleProvisionalBySid.set(sid,newTitle);
  if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
  else if(typeof renderSessionList==='function') renderSessionList();
  return true;
}

// #5472: when a provider/background error aborts a send, send() has already
// cleared the composer (`$('msg').value=''`), the persisted draft
// (`_clearComposerDraft`), and the staged files (uploadPendingFiles() sets
// `S.pendingFiles=[]`) before the turn was durably accepted server-side. On a
// start-time throw the turn is never persisted, so the user loses the entire
// typed message + attachments and must retype. Restore the ORIGINAL captured
// draft text + staged files so the user can re-send with one key press.
// Mirrors the draft-restore idiom already used by _trySteer (commands.js) and
// _stashClarifyDraft.
//
// `draftText` and `filesSnapshot` are immutable snapshots captured in send()
// BEFORE slash rewrites (/moa, bundles) mutate the payload and BEFORE
// uploadPendingFiles() drains S.pendingFiles — so we restore what the user
// actually typed, not the transformed send payload.
function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise){
  const restore=String(draftText||'');
  const files=Array.isArray(filesSnapshot)?filesSnapshot.filter(Boolean):[];
  if(!restore&&!files.length) return false;

  // Only mutate the VISIBLE composer / staged tray when the failed send belongs
  // to the session the user is currently looking at — otherwise a background
  // send failure would pollute another session's composer. (Codex #5484 catch.)
  const visibleSid=(S.session&&S.session.session_id)||null;
  const belongsToVisible=!(sid&&visibleSid&&sid!==visibleSid);
  let restoredVisible=false;
  if(belongsToVisible){
    const inp=$('msg');
    // Do not clobber a new message the user began typing during the async window.
    if(inp && !String(inp.value||'').trim()){
      inp.value=restore;
      if(typeof autoResize==='function') autoResize();
      if(typeof updateSendBtn==='function') updateSendBtn();
      // Re-stage the originally attached files so a one-key resend keeps them.
      if(files.length){
        S.pendingFiles=files;
        if(typeof renderTray==='function') renderTray();
      }
      restoredVisible=true;
    }
  }

  // Persist the failed session's draft so it survives a reload, ordered AFTER the
  // send-time _clearComposerDraft POST (text:'') resolves — otherwise the two
  // same-origin writes can be reordered under HTTP/2 multiplexing and leave the
  // server draft empty. (Opus #5484 NIT.) Because the persist is deferred, it
  // must be STALE-AWARE at fire time (Codex #5488 catch): if the failed session
  // is still visible, re-read the LIVE composer so a post-restore edit is
  // captured rather than clobbered by the original snapshot; if we restored the
  // visible session but the user has since switched away, skip entirely (the
  // session-switch save path already persisted this session's composer).
  if(sid&&typeof _saveComposerDraftNow==='function'){
    const _persist=()=>{
      try{
        const stillVisible=(S.session&&S.session.session_id)===sid;
        if(stillVisible){
          const inp=$('msg');
          const liveText=inp?String(inp.value||''):restore;
          _saveComposerDraftNow(sid, liveText, S.pendingFiles?[...S.pendingFiles]:[]);
        } else if(!restoredVisible){
          // Background failure (sid was never the visible session): no live
          // composer to read, so persist the captured snapshot — it's the only copy.
          _saveComposerDraftNow(sid, restore, []);
        }
        // else: restored the visible composer, then the user switched away — the
        // session-switch save path already saved sid's composer; skip stale write.
      }catch(_){ }
    };
    if(clearPromise&&typeof clearPromise.then==='function') clearPromise.then(_persist,_persist);
    else _persist();
  }

  return restoredVisible;
}

async function send(){
  // Static guards expect _defaultMessageMode to stay near send() while the actual
  // read remains in the S.busy branch below.
  // _defaultMessageMode
  // Reject concurrent invocations early — before any await yields control.
  // If a send is already in-flight (e.g. queue drain), re-queue the message
  // instead of silently dropping it.
  if (_sendInProgress) {
    const _text=_composerTextWithPendingSelections().trim();
    // Use the in-flight session's sid, not the currently viewed session,
    // so the queued message goes to the chat that owns the active stream.
    const _targetSid=_sendInProgressSid||(S.session&&S.session.session_id);
    if(_text && _targetSid){
      const _modelState=_chatPayloadModelState();
      queueSessionMessage(_targetSid,{text:_text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
      _clearComposerAfterQueuedSelectionSend();
      if(_targetSid&&typeof _clearComposerDraft==='function'&&_targetSid!==(S.session&&S.session.session_id)) _clearComposerDraft(_targetSid,_text,S.pendingFiles?[...S.pendingFiles]:[]);
      S.pendingFiles=[];renderTray();
      updateQueueBadge(_targetSid);
      showToast(`Queued: "${_text.slice(0,40)}${_text.length>40?'…':''}"`,2000);
    }
    return;
  }
  _sendInProgress = true;
  try{
  const options=arguments[0]||{};
  const literalSlash=!!(options&&options.literalSlash);
  let text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length&&!_pendingSelections.length){_sendInProgress=false;_sendInProgressSid=null;return;}
  // Don't send while an inline message edit is active
  if(document.querySelector('.msg-edit-area')){_sendInProgress=false;_sendInProgressSid=null;return;}
  _flushSelectionBlocksToComposer();
  text=$('msg').value.trim();
  if(!text&&!S.pendingFiles.length){_sendInProgress=false;_sendInProgressSid=null;return;}
  if(typeof shouldInterceptCompressionRecoveryContinuation==='function'&&shouldInterceptCompressionRecoveryContinuation(text,S.pendingFiles)){
    if(typeof showCompressionRecoveryContinuationHint==='function') showCompressionRecoveryContinuationHint();
    _sendInProgress=false;_sendInProgressSid=null;
    return;
  }

  // #5472: snapshot the ORIGINAL user-typed composer state now — before slash
  // rewrites (/moa, bundles) mutate `text` and before uploadPendingFiles()
  // clears S.pendingFiles. If /api/chat/start throws (turn never durably
  // started), _restoreComposerDraftAfterFailedSend() puts this exact text +
  // staged files back so the user can re-send without retyping. Captured as an
  // immutable snapshot so later reassignments to `text` don't leak into it.
  const _failedSendDraftText=text;
  const _failedSendFilesSnapshot=Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];

  // Dismiss handoff hint when user sends a message (resets seen_at).
  if(S.session&&S.session.session_id&&typeof _dismissHandoffHint==='function'){
    _dismissHandoffHint(S.session.session_id);
  }

  const compressionRunning=typeof isCompressionUiRunning==='function'&&isCompressionUiRunning();
  _clearStaleBusyStateBeforeSend({compressionRunning});
  // If busy or a manual compression is still running, handle based on default_message_mode
  if(S.busy||compressionRunning){
    if(text||S.pendingFiles.length){
      if(!S.session){await newSession();await renderSessionList();}
      // Busy-control slash commands must be intercepted HERE, before the
      // defaultMessageMode routing block, so the user can always type /steer, /interrupt,
      // /queue, /terminal, /goal, or /yolo while the agent is running and have
      // them execute immediately.
      // Without this intercept they fall through to the queue and execute after
      // the current turn ends — by which point there is no active stream and
      // cmdSteer / cmdInterrupt say "No active task to stop."
      if(text.startsWith('/')&&!literalSlash){
        const _pc=typeof parseCommand==='function'&&parseCommand(text);
        if(_pc&&['steer','interrupt','queue','terminal','goal','yolo'].includes(_pc.name)){
          const _bc=COMMANDS.find(c=>c.name===_pc.name);
          if(_bc){
            $('msg').value='';autoResize();
            await _bc.fn(_pc.args);
            return;
          }
        }
      }
    const defaultMessageMode=window._defaultMessageMode||'steer';
      if(defaultMessageMode==='steer'&&S.activeStreamId&&typeof _trySteer==='function'){
        // Real steer: clear the input first so the user gets immediate
        // feedback, then ship the steer payload via /api/chat/steer.
        // _trySteer captures the owner session/files before awaiting uploads,
        // restores/persists the draft on failure, and clears the owner draft
        // only after /api/chat/steer accepts.
        $('msg').value='';autoResize();
        // Do NOT clear pendingFiles yet — _trySteer uploads with clearPending=false,
        // and a failed steer must keep staged files available for the user's next explicit action.
        await _trySteer(text, /*explicitSteer=*/false);
        // _trySteer clears staged files only after /api/chat/steer accepts, and
        // only when the visible session still matches the captured owner sid.
      } else if(defaultMessageMode==='interrupt'){
        // Queue the message, then cancel so drain re-sends it.
        const _modelState=_chatPayloadModelState();
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
        updateQueueBadge(S.session.session_id);
        _clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);
        S.pendingFiles=[];renderTray();
        if(S.activeStreamId&&typeof cancelStream==='function'){
          showToast(t('busy_interrupt_confirm'),2000);
          await cancelStream('busy-interrupt');
        } else {
          showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
        }
      } else {
        // Default: queue mode (current behavior). Also the fallback for
        // 'steer' mode when no stream is active or _trySteer is unavailable.
        const _modelState=_chatPayloadModelState();
        queueSessionMessage(S.session.session_id,{text,files:[...S.pendingFiles],model:_modelState.model,model_provider:_modelState.model_provider,profile:S.activeProfile||'default'});
        _clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);
        S.pendingFiles=[];renderTray();
        updateQueueBadge(S.session.session_id);
        showToast(`Queued: "${text.slice(0,40)}${text.length>40?'…':''}"`,2000);
      }
    }
    return;
  }
  if(S.session&&(S.session.read_only||S.session.is_read_only)){
    if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000);
    return;
  }
  let _slashDisplayTextOverride=null;
  let _pendingMoaConfig=null;
  // Slash command intercept -- local commands handled without agent round-trip.
  // We push the user message BEFORE running the handler for echo-worthy
  // commands so chat order is correct: some handlers (e.g. cmdHelp) push
  // their assistant response synchronously.  If we pushed AFTER, S.messages
  // would be [assistant, user] and the chat would show the response above
  // the user's own input — reverse chronological order (#840 ordering bug).
  if(text.startsWith('/')&&!S.pendingFiles.length&&!literalSlash){
    const _parsedCmd=parseCommand(text);
    const _cmd=_parsedCmd?COMMANDS.find(c=>c.name===_parsedCmd.name):null;
    if(_cmd){
      let _pushedUser=false;
      if(!_cmd.noEcho){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        _pushedUser=true;
        renderMessages();
      }
      // Run the handler directly (we already looked it up).  If it returns
      // false it's opting out — e.g. /reasoning <level> falls through so the
      // agent sees the raw text.  Roll back the echo push in that case so
      // the normal send path doesn't duplicate it.
      if(_cmd.fn(_parsedCmd.args)===false){
        if(_pushedUser){S.messages.pop();renderMessages();}
        // Fall through to normal send path
      } else {
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
    }
    if(_parsedCmd&&!_cmd){
      if(_parsedCmd.name==='pet'){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _petOutput=null;
        try{
          _petOutput=typeof handlePetSlashCommand==='function'
            ? await handlePetSlashCommand(text,{name:'pet'})
            : {handled:false,message:'Desktop Companion is unavailable in WebUI.'};
        }catch(e){
          _petOutput={handled:false,message:`Desktop Companion command error: ${e&&e.message||e}`};
        }
        if(_petOutput&&_petOutput.message){
          S.messages.push({role:'assistant',content:String(_petOutput.message),_ts:Date.now()/1000});
        }
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      if(_parsedCmd.name==='sessions' || _parsedCmd.name==='resume'){
        // Open the native WebUI session browser rather than sending as chat text (#6224).
        // Use the mobile-aware opener so phone-width layouts (where expandSidebar is a
        // no-op) actually reveal the session drawer.
        if(typeof _openProfileSwitchSessionBrowser==='function') _openProfileSwitchSessionBrowser();
        else if(typeof expandSidebar==='function') expandSidebar();
        if(typeof renderSessionList==='function') await renderSessionList();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      const _agentCmd=typeof getAgentCommandMetadata==='function'
        ? await getAgentCommandMetadata(_parsedCmd.name)
        : null;
      if(_agentCmd&&_agentCmd.cli_only){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        S.messages.push({role:'assistant',content:cliOnlyCommandResponse(_parsedCmd.name,_agentCmd),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      const _agentCmdName=String(_agentCmd&&_agentCmd.name||_parsedCmd&&_parsedCmd.name||'').trim().toLowerCase();
      if(_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName)){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _agentOutput='(no output)';
        try{
          _agentOutput=typeof executeAgentCommand==='function'
            ? await executeAgentCommand(text,_agentCmd||{name:_agentCmdName})
            : 'Agent command runtime unavailable in WebUI.';
        }catch(e){
          _agentOutput=`Agent command error: ${e&&e.message||e}`;
        }
        S.messages.push({role:'assistant',content:String(_agentOutput||'(no output)'),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      if(_agentCmd&&_agentCmd.category==='Plugin'){
        if(!S.session){await newSession();await renderSessionList();}
        S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
        let _pluginOutput='(no output)';
        try{
          _pluginOutput=typeof executeAgentPluginCommand==='function'
            ? await executeAgentPluginCommand(text,_agentCmd)
            : 'Plugin command runtime unavailable in WebUI.';
        }catch(e){
          _pluginOutput=`Plugin command error: ${e&&e.message||e}`;
        }
        S.messages.push({role:'assistant',content:String(_pluginOutput||'(no output)'),_ts:Date.now()/1000});
        renderMessages();
        $('msg').value='';autoResize();hideCmdDropdown();return;
      }
      if(_agentCmdName==='moa'){
        const _moaArgs=(text.split(/\s+/).slice(1).join(' ')||'').trim();
        if(!S.session){await newSession();await renderSessionList();}
        if(!_moaArgs){
          let _moaUsage='/moa <prompt>';
          try{const _moaCfgU=await api('/api/commands/moa/resolve');_moaUsage=_moaCfgU.usage||_moaUsage;}catch(_eu){}
          S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
          S.messages.push({role:'assistant',content:_moaUsage,_ts:Date.now()/1000});
          renderMessages();$('msg').value='';autoResize();hideCmdDropdown();return;
        }
        try{
          await api('/api/commands/moa/resolve');
          _slashDisplayTextOverride=text;
          text=_moaArgs;
          _pendingMoaConfig=true;
        }catch(_e){
          S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
          S.messages.push({role:'assistant',content:'MoA unavailable: '+(_e&&_e.message||_e),_ts:Date.now()/1000});
          renderMessages();$('msg').value='';autoResize();hideCmdDropdown();return;
        }
      }
      const _bundleCmd=!_agentCmd&&typeof getBundleCommandMetadata==='function'
        ? await getBundleCommandMetadata(_parsedCmd.name)
        : null;
      if(_bundleCmd){
        try{
          const _bundleResolved=typeof resolveBundleCommand==='function'
            ? await resolveBundleCommand(text,_bundleCmd)
            : null;
          const _bundleMessage=String(_bundleResolved&&_bundleResolved.message||'').trim();
          if(!_bundleMessage) throw new Error('Bundle command runtime returned no invocation text.');
          _slashDisplayTextOverride=text;
          text=_bundleMessage;
        }catch(e){
          if(!S.session){await newSession();await renderSessionList();}
          S.messages.push({role:'user',content:text,_ts:Date.now()/1000});
          S.messages.push({role:'assistant',content:`Bundle command error: ${e&&e.message||e}`,_ts:Date.now()/1000});
          renderMessages();
          $('msg').value='';autoResize();hideCmdDropdown();return;
        }
      }
    }
  }
  if(!S.session){await newSession();await renderSessionList();}

  const activeSid=S.session.session_id;
  _sendInProgressSid=activeSid;

  // Salvage of #4750 (@harryazj): capture the composer text and clear the
  // textarea NOW — immediately after capture and BEFORE the uploadPendingFiles()
  // / forced-skill-directive awaits below. send() re-reads the LIVE composer when
  // it is re-entered while a send is in flight (the _sendInProgress guard at the
  // top of this function reads _composerTextWithPendingSelections()). If we
  // cleared only after the async work — as the pre-fix code did, down at the
  // _clearComposerDraft site — a re-entrant/interrupt-mode send during the upload
  // window would read the still-populated DOM and double-submit the same message.
  // _submittedDraftTextForClear is the sole authority for the send-time draft
  // signature from here down; no code path below re-reads $('msg').value on the
  // happy path.
  const _submittedDraftTextForClear=$('msg').value||'';
  $('msg').value='';autoResize();

  // #5912 gate CORE fix: snapshot the pending files that belong to THIS send
  // BEFORE the await, and upload exactly that snapshot. Otherwise a re-entrant /
  // interrupt-mode send during the upload window inherits the still-live
  // S.pendingFiles and later re-uploads the first send's attachment. Detach the
  // snapshot from S.pendingFiles now so files staged AFTER this point belong to
  // the next send only.
  const _submittedFiles=[...(S.pendingFiles||[])];
  const _submittedDraftFilesForClear=[..._submittedFiles];
  S.pendingFiles=[];
  if(typeof renderTray==='function')renderTray();

  // #5912 gate SILENT fix: clear the PERSISTED draft here — alongside the
  // textarea clear, BEFORE any await — so a new draft typed during the upload
  // window is not clobbered by a delayed text:'' post. Keep the promise so the
  // #5472 failed-send restore can chain its re-persist after this clear resolves.
  let _composerDraftClearPromise=null;
  if (activeSid && typeof _clearComposerDraft === 'function') _composerDraftClearPromise=_clearComposerDraft(activeSid,_submittedDraftTextForClear,_submittedDraftFilesForClear);

  setComposerStatus(_submittedFiles.length?'Uploading…':'');
  let uploaded=[];
  try{uploaded=await uploadPendingFiles({files:_submittedFiles, sessionId:activeSid, clearPending:false});}
  catch(e){if(!text){setComposerStatus(`Upload error: ${e.message}`);return;}}
  // Clear the uploading status now that upload is done — if we don't clear here
  // it stays visible for the entire duration of the agent stream, since
  // setComposerStatus('') is only called in setBusy(false), not setBusy(true).
  setComposerStatus('');

  const uploadedNames=uploaded.map(u=>u.name||u);
  const uploadedPaths=uploaded.map(u=>u&&u.path?u.path:(u&&u.name?u.name:(u&&u.filename?u.filename:u)));
  let msgText=text;
  if(uploaded.length&&!msgText)msgText=`I've uploaded ${uploaded.length} file(s): ${uploadedPaths.join(', ')}`;
  else if(uploaded.length)msgText=`${text}\n\n[Attached files: ${uploadedPaths.join(', ')}]`;
  if(_forcedSkillDirectivePending){
    const _pending=_forcedSkillDirectivePending;
    if(!_pending.sessionId||_pending.sessionId===activeSid){
      const _directivePayload = await _pending.promise;
      if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending = null;
      if(_directivePayload){
        const _directive = typeof _directivePayload==='string'
          ? _directivePayload
          : String(_directivePayload.directive||'').trim();
        const _forcedSkillName = typeof _directivePayload==='string'
          ? ''
          : String(_directivePayload.name||'').trim();
        const _forcedSkillContent = typeof _directivePayload==='string'
          ? ''
          : String(_directivePayload.content||'').trim();
        const _forcedSkillBlock = _forcedSkillName&&_forcedSkillContent
          ? `[FORCED SKILL CONTEXT: ${_forcedSkillName}]\n${_forcedSkillContent}\n[/FORCED SKILL CONTEXT]`
          : '';
        msgText=`${_directive}${_forcedSkillBlock?`\n\n${_forcedSkillBlock}`:''}\n\n${msgText||''}`.trim();
      }
    }
  }
  if(!msgText){setComposerStatus('Nothing to send');return;}
  // Composer textarea + persisted draft were already captured and cleared
  // immediately after capture (above, salvage of #4750 + #5912 gate fix) to close
  // the re-entrant double-send race AND avoid clobbering a draft typed during the
  // upload window. _composerDraftClearPromise / _submittedDraftFilesForClear are
  // set there; nothing to re-declare here.
  const displayText=_slashDisplayTextOverride||text||(uploaded.length?`Uploaded: ${uploadedNames.join(', ')}`:'(file upload)');
  const userMsg={role:'user',content:displayText,attachments:uploaded.length?uploadedNames:undefined,_ts:Date.now()/1000,_pending:true};
  S.toolCalls=[];  // clear tool calls from previous turn
  clearLiveToolCards();  // clear any leftover live cards from last turn
  let optimisticMessages;
  try{
    S.messages.push(userMsg);renderMessages();setBusy(true);
    if(S.session&&!S.session.pending_started_at) S.session.pending_started_at=Date.now()/1000;
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
    else appendThinking('',{pending:true});
    // First optimistic pass: make the local user turn visible before /api/chat/start
    // can save pending state on the server.
    _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.initial', ()=>{
      if(typeof upsertActiveSessionForLocalTurn==='function'){
        upsertActiveSessionForLocalTurn({title:displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
      }
    });
    optimisticMessages=[...S.messages];
    INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId:null,messages:INFLIGHT[activeSid].messages,uploaded:uploadedNames,toolCalls:[]});
    }
    _runOptionalPreStartUiStep('renderSessionListFromCache.initial', ()=>{
      if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
    });
    _runOptionalPreStartUiStep('startApprovalPolling.prestart', ()=>startApprovalPolling(activeSid));
    _runOptionalPreStartUiStep('startClarifyPolling.prestart', ()=>startClarifyPolling(activeSid));
    _runOptionalPreStartUiStep('fetchYoloState.prestart', ()=>_fetchYoloState(activeSid));  // sync YOLO pill with backend state
    S.activeStreamId = null;  // will be set after stream starts
    _runOptionalPreStartUiStep('updateSendBtn.prestart', ()=>{
      if(typeof updateSendBtn==='function') updateSendBtn();
    });

    // Set provisional title from user message immediately so session appears
    // in the sidebar right away with a meaningful name. /api/chat/start persists
    // the server-side provisional title and may refine this optimistic text.
    if(S.session&&(S.session.title==='Untitled'||!S.session.title)){
      const provisionalTitle=displayText.slice(0,64);
      _runOptionalPreStartUiStep('applySessionTitleUpdate.provisional', ()=>{
        applySessionTitleUpdate(activeSid, provisionalTitle, {force:true, rememberProvisional:true});
      });
      _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.provisional', ()=>{
        if(typeof upsertActiveSessionForLocalTurn==='function'){
          // Second optimistic pass: carry the provisional title into the cached row
          // without re-fetching /api/sessions before pending state exists server-side.
          upsertActiveSessionForLocalTurn({title:provisionalTitle,messageCount:S.messages.length,timestampMs:Date.now()});
        }
      });
    } else if(typeof upsertActiveSessionForLocalTurn==='function'){
      _runOptionalPreStartUiStep('upsertActiveSessionForLocalTurn.titled', ()=>{
        upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
      });
    } else {
      _runOptionalPreStartUiStep('renderSessionListFromCache.prestart', ()=>{
        renderSessionListFromCache();  // ensure it's visible even if already titled
      });
    }
  }catch(preStartError){
    // The user turn must reach /api/chat/start even if local optimistic UI
    // bookkeeping (render cache, storage quota, sidebar reconciliation, etc.)
    // throws. Otherwise the pane can show a user bubble + spinner while the
    // backend never receives the turn.
    const message=preStartError&&preStartError.message?preStartError.message:String(preStartError||'unknown error');
    try{console.warn('[webui] pre-start optimistic UI failed; continuing to /api/chat/start', message);}catch(_){ }
    if(!S.messages.includes(userMsg)) S.messages.push(userMsg);
    optimisticMessages=[...S.messages];
    INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    try{setBusy(true);}catch(_){S.busy=true;}
    if(S.session&&!S.session.pending_started_at) S.session.pending_started_at=Date.now()/1000;
    S.activeStreamId=null;
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
  }

  // Start the agent via POST, get a stream_id back
  let streamId;
  let postStartData;
  let modelStateForPostStart;
  let explicitPickForPostStart;
  try{
    const _modelState=_chatPayloadModelState();
    modelStateForPostStart=_modelState;
    const _pendingPick=(typeof _readPendingSessionModel==='function')
      ? _readPendingSessionModel(activeSid)
      : null;
    const _pendingPickMatch=_pendingPick
      && _pendingPick.model===_modelState.model
      && String(_pendingPick.model_provider||'')===String(_modelState.model_provider||'');
    // ── Persisted cross-provider pick (#3737 follow-up) ──
    // The onchange marker is consumed after the first send, so subsequent sends
    // lose explicit_model_pick and the server "repairs" the model back to the
    // profile default.  When the session has a non-default model from a different
    // provider than the profile's active provider, treat every send as explicit
    // so the server honors the user's choice across the entire conversation.
    const _defaultModel=(typeof window!=='undefined' && window._defaultModel)||'';
    const _activeProvider=(typeof window!=='undefined' && window._activeProvider)||null;
    const _isCrossProviderPick = _modelState.model
      && _modelState.model_provider
      && _defaultModel
      && _activeProvider
      && _modelState.model !== _defaultModel
      && String(_modelState.model_provider||'') !== String(_activeProvider||'');
    const _explicitPick = _pendingPickMatch || _isCrossProviderPick;
    // Consume the pending explicit-pick marker for THIS send only. The marker is
    // recorded on modelSelect.onchange and intentionally kept (not cleared on
    // session-update) so it survives the normal pick→update→send flow; clear it here
    // once read so a later send of an unchanged dropdown isn't treated as an explicit
    // pick. (#3739/#3737, Codex catch)
    if(_pendingPickMatch && typeof _clearPendingSessionModel==='function') _clearPendingSessionModel(activeSid);
    explicitPickForPostStart=_explicitPick;
    const startData=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
      session_id:activeSid,message:msgText,
      // S.session.model remains authoritative; the helper only resolves a
      // matching provider fallback for the same outgoing model.
      model:_modelState.model,workspace:S.session.workspace,
      model_provider:_modelState.model_provider,
      profile:S.activeProfile||S.session.profile||'default',
      explicit_model_pick:_explicitPick||undefined,
      attachments:uploaded.length?uploaded:undefined,
      moa_config:_pendingMoaConfig?true:undefined
    })});
    _pendingMoaConfig=null;
    postStartData = startData;
  }catch(e){
    const errMsg=String((e&&e.message)||'');
    // If /api/chat/start returns 404, the session was deleted server-side
    // (its sidecar is gone) while GET kept returning a CLI stub (#2782). Strip
    // the stale /session/<id> URL and clear localStorage so a reload does not
    // re-inject the dead id via _sessionIdFromLocation(), then reset to the
    // empty state instead of pushing a confusing error bubble into the chat.
    if(e&&e.status===404){
      try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
      try{
        if(typeof _appRootPath==='function') history.replaceState(null,'',_appRootPath());
        else history.replaceState(null,'',window.location.pathname.replace(/\/session\/[^/]+/,'')+window.location.search);
      }catch(_){ }
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);
      if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
      removeThinking();
      S.session=null;S.messages=[];
      setBusy(false);setComposerStatus('');
      if(typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(activeSid);
      if(typeof renderMessages==='function') renderMessages();
      if($('emptyState')) $('emptyState').style.display='';
      if($('msgInner')) $('msgInner').innerHTML='';
      if(typeof renderSessionList==='function') void renderSessionList();
      return;
    }
    const conflictActiveStream=/session already has an active stream/i.test(errMsg);
    if(conflictActiveStream){
      delete INFLIGHT[activeSid];
      if(typeof clearInflightState==='function') clearInflightState(activeSid);
      stopApprovalPolling();
      stopClarifyPolling();
      // Keep the user's attempted turn by queueing it for after the current run.
      const _retryModelState=_chatPayloadModelState();
      queueSessionMessage(activeSid,{text:msgText,files:[],model:_retryModelState.model,model_provider:_retryModelState.model_provider,profile:S.activeProfile||'default'});
      updateQueueBadge(activeSid);
      showToast('Current session is still running. Reconnected and queued your message.',2600);
      try{
        await loadSession(activeSid);
        setComposerStatus('');
        return;
      }catch(_){
        // Fall through to standard error handling if session reload fails.
      }
    }

    delete INFLIGHT[activeSid];
    stopApprovalPolling();
    stopClarifyPolling();
    // Only hide approval card if it belongs to the session that just finished
    if(!_approvalSessionId || _approvalSessionId===activeSid) hideApprovalCard(true);removeThinking();
    if(!_clarifySessionId || _clarifySessionId===activeSid) hideClarifyCard(true, 'terminal');
    S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});
    _queueDrainSid=activeSid;renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);
    // #5472: the send was rejected before the turn was durably started, so the
    // composer text + attachments (cleared at send time) would otherwise be
    // lost. Put back the ORIGINAL captured draft (not the mutated /moa/bundle
    // payload) and re-stage files so the user can re-send without retyping.
    _restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid, _composerDraftClearPromise);
    if(typeof clearOptimisticSessionStreaming==='function') clearOptimisticSessionStreaming(activeSid);
    // Reconcile with server truth after immediately clearing the optimistic spinner.
    if(typeof renderSessionList==='function') void renderSessionList();
    return;
  }

  const startData = postStartData || {};
  streamId = postStartData ? postStartData.stream_id : null;
  S.activeStreamId = streamId;
  // setBusy(true) already ran with activeStreamId=null; refresh now that we
  // have a stream id so the primary button can switch to Stop (see
  // getComposerPrimaryAction).
  if(typeof updateSendBtn==='function') updateSendBtn();
  _runOptionalPostStartUiStep('post-start ui/bookkeeping', ()=>{
    const _modelState=modelStateForPostStart || _chatPayloadModelState();
    const _explicitPick=explicitPickForPostStart;
    if(startData&&startData.title) applySessionTitleUpdate(activeSid, startData.title, {provisionalText:displayText.slice(0,64), rememberProvisional:true});

    if(startData&&startData.effective_model && S.session){
      const _sentModel=_modelState&&_modelState.model;
      if(_explicitPick && _sentModel && startData.effective_model!==_sentModel && typeof showToast==='function'){
        showToast('Model '+_sentModel+' changed to '+startData.effective_model+' — profile provider mismatch', 5000);
      }
      S.session.model=startData.effective_model;
      S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(startData.effective_model,S.session.model_provider||null);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'),S.session.model_provider||null);
      if(typeof syncTopbar==='function') syncTopbar();
    }else if(startData&&startData.effective_model_provider && S.session){
      S.session.model_provider=startData.effective_model_provider;
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(S.session.model||'',S.session.model_provider||null);
      if($('modelSelect')&&typeof _applyModelToDropdown==='function') _applyModelToDropdown(S.session.model||'', $('modelSelect'), S.session.model_provider||null);
      if(typeof syncModelChip==='function') syncModelChip();
      if(typeof syncTopbar==='function') syncTopbar();
    }

    if(S.session&&typeof startData.pending_started_at==='number'){
      S.session.pending_started_at=startData.pending_started_at;
    }
    if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();
    else if(typeof appendThinking==='function') appendThinking('',{pending:true});
    // setBusy(true) already ran with activeStreamId=null; refresh now that we
    // have a stream id so the primary button can switch to Stop (see
    // getComposerPrimaryAction).
    if(typeof updateSendBtn==='function') updateSendBtn();
    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
    if(S.session&&S.session.session_id===activeSid&&typeof showLiveRunStatus==='function'){
      const _startedAt=typeof startData?.pending_started_at==='number'
        ? startData.pending_started_at
        : (S.session.pending_started_at||Date.now()/1000);
      showLiveRunStatus(activeSid,{startedAt:_startedAt});
    }
    if(typeof upsertActiveSessionForLocalTurn==='function'){
      // Third optimistic pass: stream_id is now known, so the row can reconcile
      // against real active-stream metadata before the background refresh lands.
      upsertActiveSessionForLocalTurn({title:S.session&&S.session.title||displayText.slice(0,64),messageCount:S.messages.length,timestampMs:Date.now()});
    }
    if(!INFLIGHT[activeSid]){
      INFLIGHT[activeSid]={messages:optimisticMessages,uploaded:uploadedNames,toolCalls:[]};
    }
    const currentInflight=INFLIGHT[activeSid];
    markInflight(activeSid, streamId);
    if(typeof saveInflightState==='function'){
      saveInflightState(activeSid,{streamId,messages:currentInflight.messages||optimisticMessages,uploaded:uploadedNames,toolCalls:currentInflight.toolCalls||[]});
    }
    // Refresh session list so background streaming indicators appear immediately for the
    // session that was just started and any others that may already be running.
    if(typeof renderSessionList === 'function') {
      void renderSessionList();
    }
  });

  // Open SSE stream and render tokens live
  attachLiveStream(activeSid, streamId, uploadedNames);

  }finally{ _sendInProgress=false; _sendInProgressSid=null; }
}

Object.assign(HermesMessages, {
  send,
  applySessionTitleUpdate,
});
