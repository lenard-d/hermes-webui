var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

// ── Notifications and Sound ──────────────────────────────────────────────────

function _completionNotificationPreviewText(lastAssistantMessage, options){
  const opts=(options&&typeof options==='object')?options:{};
  const sessionId=String(opts.sessionId||'').trim();
  let text='';
  if(lastAssistantMessage&&typeof lastAssistantMessage==='object'){
    if(typeof _assistantTurnAnchorSettledFinalAnswer==='function'){
      const anchorFinal=_assistantTurnAnchorSettledFinalAnswer(
        lastAssistantMessage,
        lastAssistantMessage.content,
        {session_id:sessionId||undefined}
      );
      if(anchorFinal!==null&&anchorFinal!==undefined) text=String(anchorFinal||'').trim();
    }
    if(!text&&typeof msgContent==='function') text=String(msgContent(lastAssistantMessage)||'').trim();
    if(!text){
      let raw=lastAssistantMessage.content||'';
      if(Array.isArray(raw)) raw=raw.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
      text=String(raw||'').trim();
    }
    if(text&&typeof _extractInlineThinkingFromContent==='function'){
      const split=_extractInlineThinkingFromContent(text, lastAssistantMessage.reasoning, {streaming:false});
      if(split&&typeof split.content==='string') text=split.content.trim();
    }
  }
  if(!text&&typeof opts.liveDisplayText==='string') text=opts.liveDisplayText.trim();
  if(!text) return '';
  const normalized=text.replace(/\s+/g,' ').trim();
  return normalized.length>100?`${normalized.slice(0,100)}…`:normalized;
}

function playNotificationSound(){
  if(!window._soundEnabled) return;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(660,ctx.currentTime);
    osc.frequency.setValueAtTime(880,ctx.currentTime+0.1);
    gain.gain.setValueAtTime(0.3,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.3);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.3);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Notification sound failed:',e);}
}


function _attentionSoundKey(sid,kind,count){
  const safeSid=String(sid||'');
  const safeKind=String(kind||'attention');
  const safeCount=Math.max(1,Number(count)||1);
  return `${safeSid}:${safeKind}:${safeCount}`;
}

function playAttentionSound(key){
  if(!window._soundEnabled) return;
  const nowMs=Date.now();
  if(window._lastAttentionSoundAt&&nowMs-window._lastAttentionSoundAt<900) return;
  const dedupeKey=key?String(key):'';
  if(dedupeKey){
    const seen=window._attentionSoundSeenKeys instanceof Map?window._attentionSoundSeenKeys:new Map();
    window._attentionSoundSeenKeys=seen;
    for(const [seenKey,seenAt] of seen){
      if(nowMs-Number(seenAt||0)>300000) seen.delete(seenKey);
    }
    if(seen.has(dedupeKey)) return;
    seen.set(dedupeKey,nowMs);
  }
  window._lastAttentionSoundAt=nowMs;
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.type='sine';osc.frequency.setValueAtTime(880,ctx.currentTime);
    osc.frequency.setValueAtTime(660,ctx.currentTime+0.075);
    gain.gain.setValueAtTime(0.24,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+0.24);
    osc.start(ctx.currentTime);osc.stop(ctx.currentTime+0.24);
    osc.onended=()=>ctx.close();
  }catch(e){console.warn('Attention sound failed:',e);}
}

function _notificationOptions(body,options={}){
  const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);
  const url=sid?`${location.origin}${_sessionUrlForSid(sid)}`:location.href;
  return {body:body||'',tag:sid?`hermes-${sid}`:'hermes-webui',renotify:false,icon:'static/favicon-192.png',badge:'static/favicon-32.png',data:{url}};
}
function _showPwaNotification(title,body,options={}){
  const botName=assistantDisplayName();
  const opts=_notificationOptions(body,options);
  const direct=()=>new Notification(title||botName,opts);
  // Prefer the service worker (the only path that works in a standalone PWA,
  // notably iOS). Use getRegistration() + a short timeout race rather than
  // navigator.serviceWorker.ready, because `.ready` NEVER settles when no
  // registration ever activates for the scope (e.g. a reverse proxy serving
  // sw.js with the wrong MIME type, or SW disabled in the browser) — which
  // would silently drop every notification instead of falling back.
  if(navigator.serviceWorker&&navigator.serviceWorker.getRegistration){
    const reg$=Promise.race([
      navigator.serviceWorker.getRegistration().catch(()=>null),
      new Promise(res=>setTimeout(()=>res(null),2000))
    ]);
    return reg$.then(reg=>(reg&&reg.active&&reg.showNotification)
      ? reg.showNotification(title||botName,opts)
      : direct());
  }
  return Promise.resolve(direct());
}
function requestNotificationPermission(){
  if(!('Notification' in window)){
    if(typeof showToast==='function') showToast(t('notifications_unsupported'),3000,'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return Promise.resolve('unsupported');
  }
  if(Notification.permission==='granted'){
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    if(typeof showToast==='function') showToast(t('notifications_enabled_toast'),3000);
    return Promise.resolve('granted');
  }
  if(Notification.permission==='denied'){
    if(typeof showToast==='function') showToast(t('notifications_denied'),3500,'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return Promise.resolve('denied');
  }
  return Notification.requestPermission().then(p=>{
    if(typeof showToast==='function') showToast(p==='granted'?t('notifications_enabled_toast'):t('notifications_denied'),3000,p==='granted'?undefined:'error');
    if(typeof updateNotificationPermissionStatus==='function') updateNotificationPermissionStatus();
    return p;
  });
}
function sendBrowserNotification(title,body,options={}){
  const force=!!(options&&options.force);
  // #4416: `forceHidden` means the caller already determined the tab was hidden
  // during the relevant window (e.g. a stream that ran while backgrounded), so
  // the live `document.hidden` visibility gate — which a late, throttled SSE
  // makes unreliable — should be treated as satisfied. The user's
  // notifications-enabled SETTING is still honored (unlike `force`, which is the
  // explicit "Send test" override); only the visibility gate is bypassed.
  const forceHidden=!!(options&&options.forceHidden);
  if(!force&&!window._notificationsEnabled) return;
  if(!force&&!forceHidden&&!_isBackgroundedForBrowserNotification()) return;
  if(!('Notification' in window)) return;
  if(Notification.permission==='granted'){
    _showPwaNotification(title,body,options).catch(()=>{try{new Notification(title||assistantDisplayName(),_notificationOptions(body,options));}catch(_err){}});
  }else if(Notification.permission==='denied'){
    // Explicit "Send test" (force) deserves feedback instead of a silent no-op.
    if(force&&typeof showToast==='function') showToast(t('notifications_denied'),3500,'error');
  }else{
    requestNotificationPermission().then(p=>{if(p==='granted') _showPwaNotification(title,body,options).catch(()=>{try{new Notification(title||assistantDisplayName(),_notificationOptions(body,options));}catch(_err){}});});
  }
}

// ── /btw ephemeral stream ────────────────────────────────────────────────────
// Connects to the ephemeral SSE stream from /api/btw and renders the answer
// in a visually distinct bubble that is NOT persisted to session history.

function attachBtwStream(parentSid, streamId, question){
  if(!parentSid||!streamId) return;
  const src=new EventSource(new URL('api/chat/stream?stream_id='+encodeURIComponent(streamId), document.baseURI||location.href).href);
  let answer='';
  let btwRow=null;
  let _streamDone=false;
  function _ensureBtwRow(){
    if(btwRow&&btwRow.isConnected) return;
    const inner=$('msgInner');
    if(!inner) return;
    btwRow=document.createElement('div');
    btwRow.className='msg-row msg-row-btw';
    btwRow.dataset.role='assistant';
    btwRow.dataset.btw='1';
    const labelEl=document.createElement('div');
    labelEl.className='msg-btw-label';
    labelEl.textContent=t('btw_label');
    const qEl=document.createElement('div');
    qEl.className='msg-body';
    qEl.textContent=question;
    const ansEl=document.createElement('div');
    ansEl.className='msg-body msg-btw-answer';
    ansEl.textContent='...';
    btwRow.appendChild(labelEl);
    btwRow.appendChild(qEl);
    btwRow.appendChild(ansEl);
    inner.appendChild(btwRow);
    btwRow.scrollIntoView({behavior:'smooth',block:'end'});
  }
  src.addEventListener('token',e=>{
    try{answer+=JSON.parse(e.data).text||'';}catch(_){}
    _ensureBtwRow();
    const ansEl=btwRow&&btwRow.querySelector('.msg-btw-answer');
    if(ansEl) ansEl.innerHTML=renderMd(answer);
  });
  src.addEventListener('done',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      if(d.answer&&!answer) answer=d.answer;
    }catch(_){}
    if(S.session&&S.session.session_id===parentSid) _ensureBtwRow();
    if(btwRow&&btwRow.isConnected){
      const ansEl=btwRow.querySelector('.msg-btw-answer');
      if(ansEl) ansEl.innerHTML=renderMd(answer||t('btw_no_answer'));
    }
    showToast(t('btw_done'));
  });
  src.addEventListener('apperror',e=>{
    _streamDone=true;
    src.close();
    try{
      const d=JSON.parse(e.data);
      showToast(t('btw_failed')+(d.message||''));
    }catch(_){showToast(t('btw_failed'));}
    if(btwRow&&btwRow.isConnected) btwRow.remove();
  });
  src.addEventListener('stream_end',()=>{_streamDone=true;src.close();});
  src.onerror=()=>{src.close();if(!_streamDone&&btwRow&&btwRow.isConnected) btwRow.remove();};
}

// ── /background task tracking ────────────────────────────────────────────────

let _bgPollTimers={};
let _bgActiveTasks=new Set();

function showBackgroundBadge(taskId){
  _bgActiveTasks.add(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function hideBackgroundBadge(taskId){
  _bgActiveTasks.delete(taskId);
  const badge=$('bgBadge');
  if(badge){
    badge.textContent=String(_bgActiveTasks.size);
    badge.style.display=_bgActiveTasks.size?'':'none';
  }
}
function startBackgroundPolling(parentSid, taskId, prompt){
  if(_bgPollTimers[taskId]) return;
  async function _poll(){
    try{
      const r=await api('/api/background/status?session_id='+encodeURIComponent(parentSid));
      if(r&&r.results){
        for(const res of r.results){
          if(res.task_id===taskId){
            hideBackgroundBadge(taskId);
            delete _bgPollTimers[taskId];
            const msg={role:'assistant',content:`**${t('bg_label')}** ${prompt.slice(0,80)}\n\n${res.answer||t('bg_no_answer')}`,'_background':true,_ts:Date.now()/1000};
            S.messages.push(msg);
            renderMessages({preserveScroll:true});
            showToast(t('bg_complete'));
            return;
          }
        }
      }
    }catch(_){}
    _bgPollTimers[taskId]=setTimeout(_poll,3000);
  }
  _poll();
}

// ── Panel navigation (Chat / Tasks / Skills / Memory) ──

Object.assign(HermesMessages, {
  attachBtwStream,
  requestNotificationPermission,
  sendBrowserNotification,
  playNotificationSound,
});
