function _compressionAnchorMessageKey(m){
  if(!m||!m.role||m.role==='tool') return null;
  let content='';
  try{
    content=typeof msgContent==='function' ? String(msgContent(m)||'') : String(m.content||'');
  }catch(_){
    content=String(m.content||'');
  }
  const norm=content.replace(/\s+/g,' ').trim().slice(0,160);
  const ts=m._ts||m.timestamp||null;
  const attachments=Array.isArray(m.attachments)?m.attachments.length:0;
  if(!norm && !attachments && !ts) return null;
  return {role:String(m.role||''), ts, text:norm, attachments};
}

function _manualCompressionVisibleMessages(){
  return (S.messages||[]).filter(m=>{
    if(!m||!m.role||m.role==='tool') return false;
    if(m.role==='assistant'){
      const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
      const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
      if(hasTc||hasTu|| (typeof _messageHasReasoningPayload==='function' && _messageHasReasoningPayload(m))) return true;
    }
    return typeof msgContent==='function' ? !!msgContent(m) || !!m.attachments?.length : !!m.content || !!m.attachments?.length;
  });
}

function _manualCompressionSleep(ms){
  return new Promise(resolve=>setTimeout(resolve, ms));
}

async function _pollManualCompressionResult(sid){
  let delay=700;
  while(true){
    const data=await api(`/api/session/compress/status?session_id=${encodeURIComponent(sid)}`);
    if(data&&data.status==='done') return data;
    if(data&&data.status==='error'){
      const err=new Error(data.error||'Compression failed');
      err.status=data.error_status||400;
      throw err;
    }
    if(data&&data.status==='idle') throw new Error('Compression job is no longer available');
    await _manualCompressionSleep(delay);
    delay=Math.min(2000, delay+300);
  }
}

async function _applyManualCompressionResult(data, focusTopic, visibleCount, commandText){
  if(data&&data.session){
    const currentSid=S.session&&S.session.session_id;
    if(data.session.session_id&&data.session.session_id!==currentSid){
      await loadSession(data.session.session_id);
    }else{
      S.session=data.session;
      S.messages=data.session.messages||[];
      S.toolCalls=data.session.tool_calls||[];
      clearLiveToolCards();
      try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
      if(typeof _setActiveSessionUrl==='function') _setActiveSessionUrl(S.session.session_id);
      syncTopbar();
      renderMessages();
      await renderSessionList();
      updateQueueBadge(S.session.session_id);
    }
  }
  const summary=data&&data.summary;
  if(typeof setCompressionUi==='function'&&S.session){
    const referenceMsg=(S.messages||[]).find(m=>typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m));
    const messageRef=referenceMsg?msgContent(referenceMsg)||String(referenceMsg.content||''):'';
    const summaryRef=summary&&typeof summary.reference_message==='string' ? String(summary.reference_message||'').trim() : '';
    // Prefer the persisted compaction handoff when it already exists in session state.
    // The short summary fallback is only for environments where that message is unavailable.
    const referenceText=messageRef || summaryRef;
    const effectiveFocus=(data&&data.focus_topic)||focusTopic||'';
    setCompressionUi({
      sessionId:S.session.session_id,
      phase:'done',
      focusTopic:effectiveFocus,
      commandText:effectiveFocus?`/compress ${effectiveFocus}`:(commandText||'/compress'),
      beforeCount:visibleCount,
      summary:summary||null,
      referenceText,
      anchorVisibleIdx: data?.session?.compression_anchor_visible_idx,
      anchorMessageKey: data?.session?.compression_anchor_message_key||null,
    });
  }
  if(typeof setComposerStatus==='function') setComposerStatus('');
  renderMessages();
  if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
}

async function resumeManualCompressionForSession(sid){
  if(!sid) return;
  try{
    const status=await api(`/api/session/compress/status?session_id=${encodeURIComponent(sid)}`);
    if(!status||status.status!=='running') return;
    const visibleMessages=_manualCompressionVisibleMessages();
    const visibleCount=visibleMessages.length;
    const anchorMessageKey=_compressionAnchorMessageKey(visibleMessages[visibleMessages.length-1]||null);
    if(typeof setBusy==='function') setBusy(true);
    if(typeof setComposerStatus==='function') setComposerStatus(t('compressing'));
    if(typeof setCompressionUi==='function'){
      setCompressionUi({
        sessionId:sid,
        phase:'running',
        focusTopic:status.focus_topic||'',
        commandText:status.focus_topic?`/compress ${status.focus_topic}`:'/compress',
        beforeCount:visibleCount,
        anchorVisibleIdx:Math.max(0, visibleCount-1),
        anchorMessageKey,
      });
    }
    renderMessages();
    const done=await _pollManualCompressionResult(sid);
    if(!S.session||S.session.session_id!==sid) return;
    await _applyManualCompressionResult(done, status.focus_topic||'', visibleCount, status.focus_topic?`/compress ${status.focus_topic}`:'/compress');
  }catch(e){
    // No active compression job or transient server error — not a real failure.
    // 404: route missed or session gone; 5xx: backend exception during status check.
    if(e&&(!e.status||e.status===404||e.status>=500)) return;
    if(S.session&&S.session.session_id===sid&&typeof setCompressionUi==='function'){
      const visibleMessages=_manualCompressionVisibleMessages();
      setCompressionUi({
        sessionId:sid,
        phase:'error',
        focusTopic:'',
        commandText:'/compress',
        beforeCount:visibleMessages.length,
        errorText:`Compression failed: ${e.message}`,
        anchorVisibleIdx:Math.max(0, visibleMessages.length-1),
        anchorMessageKey:null,
      });
      renderMessages();
    }
  }finally{
    if(S.session&&S.session.session_id===sid){
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(typeof setBusy==='function') setBusy(false);
      if(typeof setComposerStatus==='function') setComposerStatus('');
    }
  }
}

async function _runManualCompression(focusTopic){
  if(!S.session){showToast(t('no_active_session'));return;}
  let visibleCount=0;
  try{
    const sid=S.session.session_id;
    // Preflight: verify the viewed session still exists before compressing.
    // This avoids a confusing "not found" toast when the UI is stale.
    try{
      const live=await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
      if(!live||!live.session||live.session.session_id!==sid){
        throw new Error('session no longer available');
      }
      S.session=live.session;
      S.messages=live.session.messages||[];
      S.toolCalls=live.session.tool_calls||[];
      if(typeof _messagesTruncated!=='undefined') _messagesTruncated=false;
    }catch(preflightErr){
      if(typeof clearCompressionUi==='function') clearCompressionUi();
      if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
      if(typeof setBusy==='function') setBusy(false);
      if(typeof setComposerStatus==='function') setComposerStatus('');
      renderMessages();
      showToast('Compression failed: '+(preflightErr.message||'session no longer available'));
      return;
    }
    if(typeof setBusy==='function') setBusy(true);
    const body={session_id:sid};
    if(focusTopic) body.focus_topic=focusTopic;
    const visibleMessages=_manualCompressionVisibleMessages();
    visibleCount=visibleMessages.length;
    const anchorVisibleIdx=Math.max(0, visibleCount - 1);
    const anchorMessageKey=_compressionAnchorMessageKey(visibleMessages[visibleMessages.length-1]||null);
    const commandText=focusTopic?`/compress ${focusTopic}`:'/compress';
    if(typeof setCompressionUi==='function'){
      setCompressionUi({
        sessionId:S.session.session_id,
        phase:'running',
        focusTopic:focusTopic||'',
        commandText,
        beforeCount:visibleCount,
        anchorVisibleIdx,
        anchorMessageKey,
      });
    }
    if(typeof setComposerStatus==='function') setComposerStatus(t('compressing'));
    renderMessages();
    const started=await api('/api/session/compress/start',{method:'POST',body:JSON.stringify(body)});
    if(started&&started.status==='error'){
      const err=new Error(started.error||'Compression failed');
      err.status=started.error_status||400;
      throw err;
    }
    const data=(started&&started.status==='done')?started:await _pollManualCompressionResult(sid);
    await _applyManualCompressionResult(data, focusTopic, visibleCount, commandText);
  }catch(e){
    if(typeof setCompressionUi==='function'){
      const currentSid=S.session&&S.session.session_id;
      setCompressionUi({
        sessionId:currentSid||'',
        phase:'error',
        focusTopic:(focusTopic||'').trim(),
        commandText:focusTopic?`/compress ${focusTopic}`:'/compress',
        beforeCount:(S.messages||[]).filter(m=>m&&m.role&&m.role!=='tool').length,
        errorText:`Compression failed: ${e.message}`,
        anchorVisibleIdx: Math.max(0, visibleCount - 1),
        anchorMessageKey:null,
      });
    }
    if(typeof _setCompressionSessionLock==='function') _setCompressionSessionLock(null);
    if(typeof setBusy==='function') setBusy(false);
    if(typeof setComposerStatus==='function') setComposerStatus('');
    renderMessages();
    showToast('Compression failed: '+e.message);
    return;
  }
  if(typeof setBusy==='function') setBusy(false);
}

async function cmdCompress(args){
  await _runManualCompression((args||'').trim());
}

async function cmdCompact(args){
  await _runManualCompression((args||'').trim());
}

export {cmdCompact,cmdCompress,resumeManualCompressionForSession};
