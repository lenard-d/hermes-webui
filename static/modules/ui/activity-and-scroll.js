import { _activityClockLabel, _activityElapsedLabel, _activityElapsedStartedAt, _activityElapsedTimer, _activityElapsedTimerGroup, _activityMarkObserved, _activityNowSeconds, _activityProcessedElapsedLabel, _bottomSettleToken, _clearNewMessageScrollCue, _deferClearProgrammaticScroll, _fmtTokens, _lastMessageClientHeight, _lastScrollTop, _messageUserUnpinned, _nearBottomCount, _programmaticScroll, _programmaticScrollSetAt, _recentMessageKeyScrollIntent, _recentMessageScrollIntent, _recentMessageTouchScrollIntent, _recentMessageWheelIntent, _recentNonMessageScrollIntent, _scrollPinned, _settleFinalTimer, _settleRAF, _settleRO, _settleTimer, _syncScrollToBottomCue, openComposerContextMenu } from './composer-controls.js';
import { _dynamicModelLabels } from './media-and-quota.js';
import { _compactComposerModelChipLabel } from './model-catalog.js';
import { _updateSessionStartJumpButton } from './navigation.js';
import { $, S, esc } from './state.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';

function _activityStatusNode({kind='info',label='',detail='',status='done',ts=null,id=''}){
  const row=document.createElement('div');
  row.className=`agent-activity-status agent-activity-status-${kind} agent-activity-status-${status}`;
  if(id) row.setAttribute('data-activity-event-id',id);
  if(ts) row.setAttribute('data-activity-at',String(ts));
  const iconMap={run:li('play',13),model:li('bot',13),waiting:'<span class="tool-card-running-dot"></span>',thinking:li('lightbulb',13),tool:li('wrench',13),done:li('check',13),warning:li('alert-triangle',13)};
  row.innerHTML=`<span class="agent-activity-status-icon">${iconMap[kind]||li('clock',13)}</span><span class="agent-activity-status-copy"><span class="agent-activity-status-label">${esc(label)}</span>${detail?`<span class="agent-activity-status-detail">${esc(detail)}</span>`:''}</span><span class="agent-activity-status-time">${esc(_activityClockLabel(ts))}</span>`;
  return row;
}
function _appendActivityEvent(group, event){
  if(!group)return null;
  const body=group.querySelector('.tool-call-group-body');
  if(!body)return null;
  const eventId=event&&event.id;
  let row=eventId?body.querySelector(`.agent-activity-status[data-activity-event-id="${CSS.escape(eventId)}"]`):null;
  const next=_activityStatusNode(event||{});
  if(row){row.replaceWith(next);row=next;}
  else{body.appendChild(next);row=next;}
  _activityMarkObserved(group,event&&event.ts);
  return row;
}
function _ensureLiveActivityBaseline(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const started=_activityElapsedStartedAt(group)||_activityNowSeconds();
  if(!group.getAttribute('data-turn-started-at')) group.setAttribute('data-turn-started-at',String(started));
  if(!group.getAttribute('data-last-activity-at')) group.setAttribute('data-last-activity-at',String(started));
  _appendActivityEvent(group,{id:'run-started',kind:'run',label:'Run started',detail:'Observable activity will appear here as the agent works.',status:'done',ts:started});
  const modelLabel=(S.session&&S.session.model)?getModelLabel(S.session.model):'';
  if(modelLabel)_appendActivityEvent(group,{id:'run-model',kind:'model',label:`Model: ${modelLabel}`,detail:S.activeProfile&&S.activeProfile!=='default'?`Profile: ${S.activeProfile}`:'',status:'done',ts:started});
}
function _setActivityElapsedStartedAt(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  const started=_activityElapsedStartedAt(group);
  if(started)group.setAttribute('data-turn-started-at',String(started));
}
function _updateActiveActivityElapsedTimer(){
  const group=_activityElapsedTimerGroup;
  if(!group||!group.isConnected||group.getAttribute('data-live-tool-call-group')!=='1'||group.getAttribute('data-live-activity-current')!=='1'){
    _clearActivityElapsedTimer();
    return;
  }
  const durationEl=group.querySelector('.tool-call-group-duration');
  const label=_activityElapsedLabel(group);
  const processedLabel=_activityProcessedElapsedLabel(group);
  if(label){
    group.setAttribute('data-active-turn-elapsed',label);
  }else{
    group.removeAttribute('data-active-turn-elapsed');
  }
  const labelEl=group.querySelector('.tool-worklog-label') || group.querySelector('.tool-call-group-label');
  if(labelEl&&processedLabel){
    labelEl.textContent=processedLabel;
    labelEl.setAttribute('data-sweep-label', processedLabel);
  }
  if(durationEl){
    durationEl.textContent='';
    durationEl.style.display='none';
  }
}
function _startActivityElapsedTimer(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1')return;
  _setActivityElapsedStartedAt(group);
  // Last-resort fallback for recovered live renders that arrive before session metadata.
  if(!group.getAttribute('data-turn-started-at')) group.setAttribute('data-turn-started-at',String(_activityNowSeconds()));
  if(_activityElapsedTimerGroup&&_activityElapsedTimerGroup!==group)_clearActivityElapsedTimer();
  composerControlsBindings._activityElapsedTimerGroup=group;
  _updateActiveActivityElapsedTimer();
  if(!composerControlsBindings._activityElapsedTimer)composerControlsBindings._activityElapsedTimer=setInterval(_updateActiveActivityElapsedTimer,1000);
}
function _clearActivityElapsedTimer(){
  if(_activityElapsedTimer){
    clearInterval(_activityElapsedTimer);
    composerControlsBindings._activityElapsedTimer=null;
  }
  if(_activityElapsedTimerGroup&&_activityElapsedTimerGroup.isConnected){
    _activityElapsedTimerGroup.removeAttribute('data-active-turn-elapsed');
    const durationEl=_activityElapsedTimerGroup.querySelector('.tool-call-group-duration');
    if(durationEl){durationEl.textContent='';durationEl.style.display='none';}
  }
  composerControlsBindings._activityElapsedTimerGroup=null;
}

const _MOBILE_CONFIG_BASE_LABEL='Workspace, model, quota, reasoning, and context settings';

function _setCtxCompressButton(btn,text){
  if(!btn)return;
  if(text){
    btn.style.display='';
    btn.textContent=text;
    btn.onclick=function(e){
      if(e)e.stopPropagation();
      const ta=$('msg');
      if(ta){ta.value='/compress ';ta.focus();autoResize();}
    };
  }else{
    btn.style.display='none';
    btn.textContent='';
    btn.onclick=null;
  }
}

function _syncMobileCtxDisplay(state){
  const mobileConfigBtn=$('composerMobileConfigBtn');
  const row=$('composerMobileContextAction');
  const usageLine=$('composerMobileContextUsage');
  const tokensLine=$('composerMobileContextTokens');
  const thresholdLine=$('composerMobileContextThreshold');
  const costLine=$('composerMobileContextCost');
  const compressBtn=$('composerMobileCtxCompressBtn');
  if(!state||!state.visible){
    if(row)row.style.display='none';
    if(mobileConfigBtn){
      mobileConfigBtn.setAttribute('aria-label',_MOBILE_CONFIG_BASE_LABEL);
      mobileConfigBtn.setAttribute('title',_MOBILE_CONFIG_BASE_LABEL);
    }
    _setCtxCompressButton(compressBtn,'');
    // Reset context ring to 0% to clear any stale values from previous sessions
    var arc = document.getElementById('ctx-arc');
    var num = document.getElementById('ctx-num');
    if (arc && num) {
      var circumference = 87.96;
      arc.setAttribute('stroke-dashoffset', circumference);
      num.textContent = '0';
      arc.setAttribute('stroke', '#22c55e');
    }
    return;
  }
  (function updateCtxRing(pct) {
    var arc = document.getElementById('ctx-arc');
    var num = document.getElementById('ctx-num');
    if (!arc || !num) return;
    var offset = 87.96 * (1 - Math.min(pct, 100) / 100);
    arc.setAttribute('stroke-dashoffset', offset);
    num.textContent = Math.round(pct);
    arc.setAttribute('stroke',
      pct <= 50 ? '#22c55e' : pct <= 85 ? '#f97316' : '#ef4444'
    );
  })(state.pct);
  if(mobileConfigBtn){
    mobileConfigBtn.setAttribute('aria-label',`${_MOBILE_CONFIG_BASE_LABEL}; ${state.label}`);
    mobileConfigBtn.setAttribute('title',`${_MOBILE_CONFIG_BASE_LABEL} \u00b7 ${state.label}`);
  }
  if(row){
    row.style.display='';
    row.setAttribute('aria-label',state.label);
    row.classList.toggle('ctx-mid',state.pct>50&&state.pct<=75);
    row.classList.toggle('ctx-high',state.pct>75);
  }
  if(usageLine)usageLine.textContent=state.usageText||'';
  if(tokensLine)tokensLine.textContent=state.tokensText||'';
  if(thresholdLine){
    if(state.thresholdText){
      thresholdLine.style.display='';
      thresholdLine.textContent=state.thresholdText;
    }else{
      thresholdLine.style.display='none';
      thresholdLine.textContent='';
    }
  }
  if(costLine){
    if(state.costText){
      costLine.style.display='';
      costLine.textContent=state.costText;
    }else{
      costLine.style.display='none';
      costLine.textContent='';
    }
  }
  _setCtxCompressButton(compressBtn,state.compressText||'');
}

function _mergeUsageForCtxIndicator(latest, fallback){
  const latestObj=(latest&&typeof latest==='object')?latest:{};
  const fallbackObj=(fallback&&typeof fallback==='object')?fallback:{};
  const merged={...latestObj};
  for(const field of [
    'input_tokens','output_tokens','estimated_cost',
    'cache_read_tokens','cache_write_tokens','cache_hit_percent',
    'turn_cache_hit_percent','duration_seconds','tps','gateway_routing',
  ]){
    if(merged[field]==null&&fallbackObj[field]!=null){
      merged[field]=fallbackObj[field];
    }
  }
  if(!(Number(latestObj.context_length)>0)&&Number(fallbackObj.context_length)>0){
    merged.context_length=fallbackObj.context_length;
  }
  for(const field of ['threshold_tokens','last_prompt_tokens']){
    if(latestObj[field]==null&&fallbackObj[field]!=null){
      merged[field]=fallbackObj[field];
    }
  }
  if(!Object.hasOwn(latestObj,'post_compression_context_tokens_estimate')&&fallbackObj.post_compression_context_tokens_estimate!=null){
    merged.post_compression_context_tokens_estimate=fallbackObj.post_compression_context_tokens_estimate;
  }
  return merged;
}

// Context usage indicator in composer footer
function _syncCtxIndicator(usage){
  const wrap=$('ctxIndicatorWrap');
  const el=$('ctxIndicator');
  if(!el)return;
  const ctxHidden=!!(window._composerControlVisibility&&window._composerControlVisibility.hide_composer_context);
  if(ctxHidden){
    if(wrap) wrap.style.display='none';
    _syncMobileCtxDisplay({visible:false});
    return;
  }
  // #1436: Use last_prompt_tokens only — NEVER fall back to cumulative
  // input_tokens for the "context window % used" calculation.  input_tokens
  // is summed across all turns, so dividing it by the context window gives a
  // nonsense percentage (often >100%) on long sessions.  When we have no
  // last-prompt data we render "·" + "tokens used" via the !hasPromptTok
  // branch below — honest "no data" instead of misleading "890% used".
  const postCompressionEstimate=Number(usage.post_compression_context_tokens_estimate)||0;
  const hasPostCompressionEstimate=postCompressionEstimate>0;
  const promptTok=usage.last_prompt_tokens||0;
  const contextPromptTok=hasPostCompressionEstimate?postCompressionEstimate:promptTok;
  const totalTok=(usage.input_tokens||0)+(usage.output_tokens||0);
  const cacheReadTok=usage.cache_read_tokens||0;
  const cacheWriteTok=usage.cache_write_tokens||0;
  // Default context window to 128K when not provided by backend
  const DEFAULT_CTX=128*1024;
  const ctxWindow=usage.context_length||DEFAULT_CTX;
  const cost=usage.estimated_cost;
  // Show indicator whenever we have any usage data (tokens or cost)
  if(!promptTok&&!totalTok&&!cost&&!cacheReadTok&&!cacheWriteTok){
    if(wrap) wrap.style.display='none';
    _syncMobileCtxDisplay({visible:false});
    return;
  }
  if(wrap){
    // Defensive reset: keep dynamic context display from being stuck hidden.
    wrap.classList.remove('composer-control-hidden');
    wrap.removeAttribute('aria-hidden');
    wrap.style.display='';
  }
  let hasPromptTok=!!promptTok;
  if(hasPostCompressionEstimate) hasPromptTok=true;
  const rawPct=hasPromptTok?Math.round((contextPromptTok/ctxWindow)*100):0;
  const pct=Math.min(100,rawPct);
  const overflowed=rawPct>100;
  const ring=$('ctxRingValue');
  const center=$('ctxPercent');
  const usageLine=$('ctxTooltipUsage');
  const tokensLine=$('ctxTooltipTokens');
  const thresholdLine=$('ctxTooltipThreshold');
  const costLine=$('ctxTooltipCost');
  if(ring){
    const circumference=61.261056745;
    ring.style.strokeDasharray=String(circumference);
    ring.style.strokeDashoffset=String(circumference*(1-pct/100));
  }
  if(center) center.textContent=hasPromptTok?String(pct):'\u00b7';
  const hasExplicitCtx=!!usage.context_length;
  el.classList.toggle('ctx-mid',pct>50&&pct<=75);
  el.classList.toggle('ctx-high',pct>75);
  // ── Compress affordance (#524) ──
  // Show a hint in the tooltip when context usage is high so users
  // discover /compress without having to know the slash command.
  const compressWrap=$('ctxTooltipCompress');
  const compressBtn=$('ctxCompressBtn');
  const compressText=pct>=75?t('ctx_compress_action'):(pct>=50?t('ctx_compress_hint'):'');
  if(compressWrap) compressWrap.style.display=compressText?'':'none';
  _setCtxCompressButton(compressBtn,compressText);
  const cacheHitPct=usage.cache_hit_percent;
  const cacheText=cacheHitPct!=null?t('usage_cache_hit_detail',cacheHitPct,_fmtTokens(cacheReadTok),_fmtTokens(cacheWriteTok)):'';
  const contextLabel=hasPostCompressionEstimate?'Estimated next model context':'Context window';
  let label=hasPromptTok?`${contextLabel} ${pct}% used`:`${_fmtTokens(totalTok)} tokens used`;
  if(!hasExplicitCtx&&hasPromptTok) label+=' (est. 128K)';
  if(cost) label+=` \u00b7 $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
  if(cacheText) label+=` \u00b7 ${cacheText}`;
  el.setAttribute('aria-label',label);
  const usageText=hasPromptTok?(overflowed?`${contextLabel}: ${rawPct}% used (context exceeded)`:`${contextLabel}: ${pct}% used (${100-pct}% left)`):`${_fmtTokens(totalTok)} tokens used`;
  const tokensText=hasPromptTok?`${contextLabel}: ${_fmtTokens(contextPromptTok)} / ${_fmtTokens(ctxWindow)} tokens used`:`In: ${_fmtTokens(usage.input_tokens||0)} \u00b7 Out: ${_fmtTokens(usage.output_tokens||0)}`;
  if(usageLine) usageLine.textContent=usageText;
  if(tokensLine) tokensLine.textContent=tokensText;
  const threshold=usage.threshold_tokens||0;
  let thresholdText='';
  if(thresholdLine){
    if(threshold&&ctxWindow){
      thresholdText=`Auto-compress at ${_fmtTokens(threshold)} (${Math.round(threshold/ctxWindow*100)}%)`;
      thresholdLine.style.display='';
      thresholdLine.textContent=thresholdText;
    }else{
      thresholdLine.style.display='none';
      thresholdLine.textContent='';
    }
  }
  let costText='';
  if(costLine){
    if(cost){
      costText=`Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}`;
      if(cacheText) costText+=` \u00b7 ${cacheText}`;
      costLine.style.display='';
      costLine.textContent=costText;
    }else if(cacheText){
      costText=cacheText;
      costLine.style.display='';
      costLine.textContent=costText;
    }else{
      costLine.style.display='none';
      costLine.textContent='';
    }
  }
  _syncMobileCtxDisplay({
    visible:true,
    hasPromptTok,
    pct,
    label,
    usageText,
    tokensText,
    thresholdText,
    costText,
    compressText
  });
}

// ── Touch support: toggle context tooltip on tap (#524) ──
// Hover/focus still exposes the compact tooltip, but a click/tap now opens the
// shared composer config menu used by the phone footer so the richer context
// details and compress action have one interaction path.
document.addEventListener('DOMContentLoaded',function(){
  const wrap=document.getElementById('ctxIndicatorWrap');
  const tooltip=document.getElementById('ctxTooltip');
  if(!wrap||!tooltip)return;
  const btn=document.getElementById('ctxIndicator');
  if(!btn)return;
  btn.addEventListener('click',openComposerContextMenu);
  // Close on outside tap
  document.addEventListener('click',function(){
    tooltip.classList.remove('ctx-tooltip-active');
    tooltip.setAttribute('aria-hidden','true');
  },{passive:true});
  // Prevent tooltip click from closing itself
  tooltip.addEventListener('click',function(e){e.stopPropagation();});
});

function _setMessageScrollToBottom(){
  const el=$('messages');
  if(!el) return;
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  el.scrollTop=el.scrollHeight;
  composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
  composerControlsBindings._nearBottomCount=2;
  composerControlsBindings._scrollPinned=true;
  requestAnimationFrame(()=>{
    // Retry the bottom write on the next layout frame so a DOM rebuild that
    // grows the transcript after the first write doesn't strand a pinned
    // conversation mid-scroll (#3319). But by this frame the user may have
    // scrolled up — under the sticky-unpin model (#3343) _messageUserUnpinned
    // is the authoritative "user scrolled away" signal, so DON'T snap them back
    // or re-pin if so; only release the programmatic-scroll latch.
    if(_messageUserUnpinned || !_scrollPinned || _recentNonMessageScrollIntent()){
      _deferClearProgrammaticScroll();
      return;
    }
    el.scrollTop=el.scrollHeight;
    composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
    composerControlsBindings._nearBottomCount=2;
    composerControlsBindings._scrollPinned=true;
    _deferClearProgrammaticScroll();
  });
}
function _isMessagePaneNearBottom(threshold=250){
  const el=$('messages');
  if(!el) return false;
  return el.scrollHeight-el.scrollTop-el.clientHeight<=threshold;
}
function _messageBottomDistance(){
  const el=$('messages');
  if(!el) return 0;
  return el.scrollHeight-el.scrollTop-el.clientHeight;
}
// #5514/#5515: when the composer grows (typing multiple rows, Shift+Enter, a
// multi-line paste / WisprFlow), the flex:1 `.messages` viewport shrinks by the
// same delta. A reader pinned to the bottom is then stranded Δpx above it — the
// transcript appears to "scroll up" one row per composer row, and (the #5515
// half) it reads as a random upward jump during normal use. autoResize() only
// resized the textarea; nothing re-pinned the transcript. Re-pin the bottom, but
// ONLY when the reader is genuinely still pinned (sticky-unpin model: honor
// _messageUserUnpinned so we never yank a reader who scrolled away, and never
// fight a stream that already unpinned). Cheap no-op when not pinned.
function _repinMessagesAfterComposerResize(){
  if(_messageUserUnpinned || !_scrollPinned) return;
  const el=$('messages');
  if(!el) return;
  // Already at/very near the bottom? nothing to do (avoids needless writes while
  // idle-reading a short conversation that isn't scrollable).
  if(_messageBottomDistance()<=1) return;
  if(typeof _setMessageScrollToBottom==='function') _setMessageScrollToBottom();
  else { el.scrollTop=el.scrollHeight; }
}
if(typeof window!=='undefined') window._repinMessagesAfterComposerResize=_repinMessagesAfterComposerResize;
function _shouldFollowMessagesOnDomReplace(){
  // Final stream settlement replaces the live DOM with persisted messages. Keep
  // following only for users who are still pinned or effectively at the tail.
  // A broad near-bottom window causes long answers/mobile readers who scroll up
  // a little to read mid-stream to get snapped back to the bottom on completion.
  return _autoScrollFollow && !_messageUserUnpinned && (_scrollPinned || _isMessagePaneNearBottom(120));
}
function _followMessagesAfterDomReplace(){
  if(_shouldFollowMessagesOnDomReplace()){
    scrollToBottom();
    return true;
  }
  return false;
}
function _settleMessageScrollToBottom(force, explicit){
  // `explicit` = a user-invoked scroll-to-bottom (End button / scrollToBottom()).
  // When explicit, late-layout settling runs even if Auto-follow is OFF — the
  // setting only suppresses AUTOMATIC streaming follow, not a deliberate jump
  // to the bottom. (Codex #4006 r3.)
  // can grow the transcript after the first scroll write. Re-apply the bottom
  // position when content settles so late layout does not leave the viewport
  // above the real end. User scroll increments _bottomSettleToken and cancels.
  //
  // Firefox paints each scrollTop write as a visible reflow step. The old
  // rAF-polling approach read scrollHeight across frames — the read itself
  // forced a reflow in Firefox, causing visible jitter.
  //
  // ResizeObserver approach: the browser notifies us when the container
  // resizes (no scrollHeight polling needed). On each notification we write
  // scrollTop once via rAF (batches multiple resize callbacks per frame into
  // a single write). After 300ms of no resize events, the observer disconnects.
  const token=++composerControlsBindings._bottomSettleToken;
  cancelAnimationFrame(_settleRAF);
  if(composerControlsBindings._settleRO){ composerControlsBindings._settleRO.disconnect(); composerControlsBindings._settleRO=null; }
  clearTimeout(_settleTimer);
  clearTimeout(_settleFinalTimer);

  // Sync write anchors the viewport immediately.
  _setMessageScrollToBottom();

  if(force) return;

  const el=document.getElementById('messages');
  if(!el) return;
  // Observe the GROWING content node, not the scroll container. #messages is the
  // scroller but its box is fixed by the flex layout, so it never resizes — the
  // transcript grows inside #msgInner (.messages-inner). Observing #messages
  // would mean the callback never fires. (Codex review #2.)
  const observed=document.getElementById('msgInner')||el;

  // Instance-owned cleanup: close over THIS observer so a stale callback (from a
  // superseded settle) only ever disconnects its own observer, never the newer
  // active one that may now be in the global _settleRO. (Codex review #3.)
  const ro=new ResizeObserver(()=>{
    if(token!==_bottomSettleToken){ ro.disconnect(); if(composerControlsBindings._settleRO===ro) composerControlsBindings._settleRO=null; return; }
    if((!_autoScrollFollow&&!explicit)||!_scrollPinned||_messageUserUnpinned||_recentNonMessageScrollIntent()){
      ro.disconnect(); if(composerControlsBindings._settleRO===ro) composerControlsBindings._settleRO=null;
      composerControlsBindings._programmaticScroll=false;
      return;
    }
    // Write scrollTop once per frame — ResizeObserver batches multiple
    // notifications per frame, so this is at most one write per frame.
    cancelAnimationFrame(_settleRAF);
    composerControlsBindings._settleRAF=requestAnimationFrame(()=>{
      if(token!==_bottomSettleToken) return;
      _setMessageScrollToBottom();
    });
    // After 300ms of quiet, disconnect — layout is stable.
    clearTimeout(_settleTimer);
    composerControlsBindings._settleTimer=setTimeout(()=>{
      if(token!==_bottomSettleToken) return;
      ro.disconnect(); if(composerControlsBindings._settleRO===ro) composerControlsBindings._settleRO=null;
      _setMessageScrollToBottom();
    },300);
  });
  composerControlsBindings._settleRO=ro;
  ro.observe(observed);
  // #4702: for an explicit (user/open) settle, also observe the SCROLLER itself.
  // On iOS the transcript content (#msgInner) may not resize, but the scroller
  // grows when the portrait toolbar collapses after first paint — observing both
  // re-anchors the bottom after that late viewport settle. Desktop never resizes
  // here, so this is a no-op off-mobile.
  if(explicit&&observed!==el){ try{ ro.observe(el); }catch(_){ } }

  // Static-content safety net: a fully-static response (no Prism/KaTeX/Mermaid/
  // late images) never resizes after the initial sync write, so the
  // ResizeObserver callback above never fires and its 300ms quiet-timer is never
  // armed. Arm a single 2s top-level fallback so a late settle still runs for
  // that case. The token check inside _settleFinalScroll makes this a no-op if a
  // newer settle started, and it self-skips if the user unpinned. (Review #2/#3.)
  clearTimeout(_settleFinalTimer);
  composerControlsBindings._settleFinalTimer=setTimeout(()=>{
    if(token!==_bottomSettleToken) return;
    ro.disconnect(); if(composerControlsBindings._settleRO===ro) composerControlsBindings._settleRO=null;
    if((!_autoScrollFollow&&!explicit)||!_scrollPinned||_messageUserUnpinned||_recentNonMessageScrollIntent()){ composerControlsBindings._programmaticScroll=false; return; }
    _settleFinalScroll(token);
  },2000);
}

function _settleFinalScroll(token){
  if(token!==_bottomSettleToken) return;
  const el=document.getElementById('messages');
  if(!el){ composerControlsBindings._programmaticScroll=false; return; }
  if(_messageUserUnpinned||!_scrollPinned||_recentNonMessageScrollIntent()||_recentMessageTouchScrollIntent()){
    composerControlsBindings._programmaticScroll=false;
    return;
  }
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  el.scrollTop=el.scrollHeight;
  composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
  composerControlsBindings._nearBottomCount=2;
  composerControlsBindings._scrollPinned=true;
  _deferClearProgrammaticScroll();
}
function scrollIfPinned(){
  if(!_autoScrollFollow) return;
  if(_messageUserUnpinned){
    // Only scrollToBottom() cleared this flag, so one scroll-up permanently
    // killed auto-follow. Re-pin ONLY when the reader has genuinely returned to
    // the true bottom tail (<=80px), NOT on mere near-bottom proximity — the
    // #4295 invariant is that proximity alone (inside the ~250px band) must not
    // re-pin, or a reader scanning the last few lines gets yanked to the bottom
    // mid-stream. Also bail on ANY recent message-pane scroll intent (wheel,
    // key, touch) and non-message intent, so an active scroll-up near the tail
    // is never overridden. Uses the same _nearBottomCount debounce as the
    // scroll listener (~4859-4866).
    if(_recentNonMessageScrollIntent()||_recentMessageScrollIntent()||_recentMessageTouchScrollIntent()||_recentMessageWheelIntent()||_recentMessageKeyScrollIntent()){ composerControlsBindings._nearBottomCount=0; return; }
    if(_messageBottomDistance()>80){ composerControlsBindings._nearBottomCount=0; return; }
    composerControlsBindings._nearBottomCount=composerControlsBindings._nearBottomCount+1;
    if(_nearBottomCount<2) return;
    composerControlsBindings._nearBottomCount=0;
    composerControlsBindings._messageUserUnpinned=false;
    composerControlsBindings._scrollPinned=true;
  }
  if(!_scrollPinned) return;
  if(_recentNonMessageScrollIntent()) return;
  if(_messageBottomDistance()>500) _setMessageScrollToBottom();
  _settleMessageScrollToBottom(false);
}
function scrollToBottom(){
  _clearNewMessageScrollCue();
  composerControlsBindings._scrollPinned=true;
  composerControlsBindings._messageUserUnpinned=false;
  // Write scrollTop once synchronously to anchor the viewport, then let
  // ResizeObserver settle handle any late layout growth (Prism, KaTeX,
  // Mermaid, images).  Using force=false so the observer runs — force=true
  // was skipping the observer and causing Firefox paint jumps when
  // renderMessages({preserveScroll:true}) + scrollToBottom() fired back-to-back.
  _setMessageScrollToBottom();
  _settleMessageScrollToBottom(false, true);
  _syncScrollToBottomCue(false,{newMessage:false});
  if(typeof _updateSessionStartJumpButton==='function') _updateSessionStartJumpButton();
  if(typeof _flushDeferredActiveSessionExternalRefresh==='function') _flushDeferredActiveSessionExternalRefresh();
}

function _fmtOllamaLabel(mid){
  const [namePart, ...variantParts] = mid.split(':');
  const variant = variantParts.join(':');
  const _fmt = (s) => {
    const tokens = s.replace(/[-_]/g, ' ').split(' ');
    return tokens.map(t => {
      const alphaOnly = t.replace(/\./g, '');
      if (t.length <= 3 && /^[a-zA-Z.]+$/.test(t)) return t.toUpperCase();
      if (/^\d/.test(alphaOnly)) return t.toUpperCase();
      return t.charAt(0).toUpperCase() + t.slice(1);
    }).join(' ');
  };
  let label = _fmt(namePart);
  if (variant) label += ' (' + _fmt(variant) + ')';
  return label;
}

function getModelLabel(modelId){
  if(!modelId) return 'Unknown';
  const rawId=String(modelId||'');
  // Preserve custom gateway model IDs exactly as configured.
  // Examples:
  //   @custom:ai_gateway:Qwen3.6-35B-A3B -> Qwen3.6-35B-A3B
  //   @custom:qwen397b-64k               -> qwen397b-64k
  if(rawId.startsWith('@custom:')){
    const rest=rawId.slice('@custom:'.length);
    if(rest.includes(':')) return rest.slice(rest.lastIndexOf(':')+1)||rawId;
    if(rest.includes('/')) return rest.slice(rest.indexOf('/')+1)||rawId;
    return rest||rawId;
  }
  // Check dynamic labels first, then fall back to splitting the ID
  if(_dynamicModelLabels[modelId]) return _dynamicModelLabels[modelId];
  // Static fallback for common models
  const STATIC_LABELS={'openai/gpt-5.4-mini':'GPT-5.4 Mini','openai/gpt-4o':'GPT-4o','openai/o3':'o3','openai/o4-mini':'o4-mini','anthropic/claude-sonnet-4.6':'Sonnet 4.6','anthropic/claude-sonnet-4-5':'Sonnet 4.5','anthropic/claude-haiku-3-5':'Haiku 3.5','google/gemini-3.1-pro-preview':'Gemini 3.1 Pro','google/gemini-3-flash-preview':'Gemini 3 Flash','google/gemini-3.1-flash-lite-preview':'Gemini 3.1 Flash Lite','google/gemini-2.5-pro':'Gemini 2.5 Pro','google/gemini-2.5-flash':'Gemini 2.5 Flash','deepseek/deepseek-v4-flash':'DeepSeek V4 Flash','deepseek/deepseek-v4-pro':'DeepSeek V4 Pro','deepseek/deepseek-chat-v3-0324':'DeepSeek V3 (legacy)','meta-llama/llama-4-scout':'Llama 4 Scout'};
  if(STATIC_LABELS[modelId]) return STATIC_LABELS[modelId];
  // Safe Ollama-tag fallback: strip only the first slash-segment (provider
  // prefix) so multi-slash IDs preserve their vendor hierarchy (#3360).
  // URI-scheme ids (e.g. `gpt://${FOLDER}/deepseek-v4-flash/latest`, provider
  // `yandex:gpt`) must NOT be first-segment-stripped — `indexOf('/')` would
  // land inside the `://` and leave `/${FOLDER}/...` path junk (#3429). For a
  // `scheme://authority/path...` id, drop the scheme AND the authority, then
  // pick the model name from the PATH segments only. A version/channel tail
  // (`latest`/`stable`/numeric) is skipped only when a real model segment
  // precedes it — never promoting the authority or a container folder (#3429).
  let _last;
  const _uriMatch = /^[a-z][a-z0-9+.-]*:\/\/(.+)$/i.exec(modelId);
  if (_uriMatch) {
    const _all = _uriMatch[1].split('/').filter(Boolean);
    // _all[0] is the authority (folder/host); the model lives in the path tail.
    const _path = _all.slice(1);
    // A pure version/channel tail: named channels, or a bare version number
    // (`v4`, `1.2`, `20231231`) — NOT a mixed model name that merely starts
    // with a digit (`2026-model`, `4o-mini`), which must be kept as the label.
    const _isVersionTail = (s) => /^(latest|stable|current|default|v\d[\d.]*|\d[\d.]*)$/i.test(s);
    const _isPlaceholder = (s) => /\$\{[^}]*\}/.test(s);
    // Walk path segments right-to-left; the model name is the LAST segment that
    // is neither a version/channel tail (`latest`, `v4`, `1.2`) nor a `${...}`
    // env-var placeholder. Fall back to the last non-placeholder segment, then
    // the literal last segment. Never returns the authority (`_all[0]`).
    let _pick = '';
    let _lastUsable = '';
    for (let _i = _path.length - 1; _i >= 0; _i--) {
      const _seg = _path[_i];
      if (_isPlaceholder(_seg)) continue;
      if (!_lastUsable) _lastUsable = _seg;
      if (!_isVersionTail(_seg)) { _pick = _seg; break; }
    }
    // Fallbacks: the chosen non-version segment, else the last non-placeholder
    // path segment. NEVER the authority and NEVER a `${...}` placeholder — for
    // a degenerate id (`gpt://folder123`, `gpt://folder123/${MODEL}`) fall all
    // the way back to the raw id rather than leak the folder/host or env var.
    const _lastPath = _path[_path.length - 1] || '';
    _last = _pick || _lastUsable || (_lastPath && !_isPlaceholder(_lastPath) ? _lastPath : '') || modelId;
  } else {
    _last = modelId.includes('/') ? (modelId.slice(modelId.indexOf('/')+1) || modelId) : modelId;
  }
  // Strip @provider: prefix if present (e.g. @ollama-cloud:kimi-k2.6)
  if (_last.startsWith('@') && _last.includes(':')) _last = _last.split(':').slice(1).join(':');
  const looksLikeOllamaTag = /^[a-z0-9][\w.-]*:[\w.-]+$/i.test(_last);
  const atProvider=(rawId.startsWith('@')&&rawId.includes(':'))
    ? rawId.slice(1,rawId.indexOf(':')).toLowerCase()
    : '';
  const allowOllamaFormat=!atProvider||atProvider.startsWith('ollama');
  // Narrow: only apply Ollama formatter to IDs with explicit @ollama prefix or colon-tag format.
  // Avoids reformatting bare provider model IDs like claude-sonnet-4-6 or gpt-4o.
  const looksLikeBareOllamaId = modelId.startsWith('@ollama') || looksLikeOllamaTag;
  const ollamaLabel = _fmtOllamaLabel(_last);
  if (allowOllamaFormat && (modelId.startsWith('ollama/') || modelId.startsWith('@ollama') || looksLikeOllamaTag || looksLikeBareOllamaId) && ollamaLabel !== _last) {
    return ollamaLabel;
  }
  return _last || 'Unknown';
}

function _gatewayProviderName(provider){
  const text=String(provider||'').trim();
  if(!text)return'';
  return text.replace(/^custom:/,'').replace(/[-_]/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
}
function _gatewayRoutingLabel(routing){
  if(!routing)return'';
  const provider=_gatewayProviderName(routing.used_provider||routing.provider);
  return provider?`via ${provider}`:'';
}
function _formatGatewayModelLabel(modelId,labelText,routing){
  if(!routing)return'';
  const usedModel=String(routing.used_model||'').trim();
  const base=usedModel
    ?_compactComposerModelChipLabel(usedModel,getModelLabel(usedModel))
    :_compactComposerModelChipLabel(modelId,labelText||getModelLabel(modelId));
  const via=_gatewayRoutingLabel(routing);
  return via?`${base} ${via}`:base;
}
function _gatewayRoutingFailoverText(routing){
  if(!routing||!routing.has_failover)return'';
  const attempts=Array.isArray(routing.routing)?routing.routing:[];
  const providers=attempts.map(a=>_gatewayProviderName(a&&a.provider)).filter(Boolean);
  const unique=[];providers.forEach(p=>{if(!unique.includes(p))unique.push(p);});
  if(unique.length>=2)return`Failover: ${unique[0]} → ${unique[unique.length-1]}`;
  const from=_gatewayProviderName(routing.requested_provider);
  const to=_gatewayProviderName(routing.used_provider);
  if(from&&to&&from!==to)return`Failover: ${from} → ${to}`;
  return'Gateway failover detected';
}
function _gatewayModelWarningText(routing){
  if(!routing||!routing.model_changed)return'';
  const requested=getModelLabel(routing.requested_model||'requested model');
  const used=getModelLabel(routing.used_model||'served model');
  return`Model switched: ${requested} → ${used}`;
}
function _latestGatewayRoutingForSession(session){
  if(!session)return null;
  if(session.gateway_routing)return session.gateway_routing;
  const history=Array.isArray(session.gateway_routing_history)?session.gateway_routing_history:[];
  return history.length?history[history.length-1]:null;
}

function _stripXmlToolCallsDisplay(s){
  // Strip <function_calls>...</function_calls> blocks emitted by DeepSeek and
  // similar models in their raw response text.  These are processed separately
  // as tool calls; leaving them in the content causes them to render visibly
  // in the settled chat bubble.  (#702)
  // Also handles DSML-prefixed variants from DeepSeek/Bedrock, including
  // spacing variants like "<｜DSML |function_calls" and truncated prefixes.
  if(!s) return s;
  const lo=String(s).toLowerCase();
  if(lo.indexOf('function_calls')===-1 && lo.indexOf('dsml')===-1) return s;
  // Support both plain <function_calls> and DSML-prefixed variants.
  s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
  // Also remove truncated opening tags (missing closing ">" at stream tail).
  s=s.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
  // Remove malformed DSML tag fragments like "<｜DSML |" that can leak in tokens.
  s=s.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
  return s.trim();
}

function _sanitizeThinkingDisplayText(text){
  const stripped=_stripXmlToolCallsDisplay(String(text||''));
  return stripped.trim();
}

function _normalizeThinkingEchoCompare(text){
  return String(text||'').replace(/\s+/g,' ').trim();
}

function _stripVisibleAssistantEchoFromThinking(thinkingText, ...visibleTexts){
  const clean=_sanitizeThinkingDisplayText(thinkingText);
  const thinkingNorm=_normalizeThinkingEchoCompare(clean);
  if(!thinkingNorm) return '';
  for(const visibleText of visibleTexts){
    const visibleNorm=_normalizeThinkingEchoCompare(visibleText);
    if(visibleNorm&&visibleNorm===thinkingNorm) return '';
  }
  return clean;
}



export {
  _activityStatusNode,
  _appendActivityEvent,
  _ensureLiveActivityBaseline,
  _setActivityElapsedStartedAt,
  _updateActiveActivityElapsedTimer,
  _startActivityElapsedTimer,
  _clearActivityElapsedTimer,
  _setCtxCompressButton,
  _syncMobileCtxDisplay,
  _mergeUsageForCtxIndicator,
  _syncCtxIndicator,
  _setMessageScrollToBottom,
  _isMessagePaneNearBottom,
  _messageBottomDistance,
  _repinMessagesAfterComposerResize,
  _shouldFollowMessagesOnDomReplace,
  _followMessagesAfterDomReplace,
  _settleMessageScrollToBottom,
  _settleFinalScroll,
  scrollIfPinned,
  scrollToBottom,
  _fmtOllamaLabel,
  getModelLabel,
  _gatewayProviderName,
  _gatewayRoutingLabel,
  _formatGatewayModelLabel,
  _gatewayRoutingFailoverText,
  _gatewayModelWarningText,
  _latestGatewayRoutingForSession,
  _stripXmlToolCallsDisplay,
  _sanitizeThinkingDisplayText,
  _normalizeThinkingEchoCompare,
  _stripVisibleAssistantEchoFromThinking,
  _MOBILE_CONFIG_BASE_LABEL,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _activityStatusNode: { enumerable: true, get: () => _activityStatusNode, set: (value) => { _activityStatusNode = value; } },
  _appendActivityEvent: { enumerable: true, get: () => _appendActivityEvent, set: (value) => { _appendActivityEvent = value; } },
  _ensureLiveActivityBaseline: { enumerable: true, get: () => _ensureLiveActivityBaseline, set: (value) => { _ensureLiveActivityBaseline = value; } },
  _setActivityElapsedStartedAt: { enumerable: true, get: () => _setActivityElapsedStartedAt, set: (value) => { _setActivityElapsedStartedAt = value; } },
  _updateActiveActivityElapsedTimer: { enumerable: true, get: () => _updateActiveActivityElapsedTimer, set: (value) => { _updateActiveActivityElapsedTimer = value; } },
  _startActivityElapsedTimer: { enumerable: true, get: () => _startActivityElapsedTimer, set: (value) => { _startActivityElapsedTimer = value; } },
  _clearActivityElapsedTimer: { enumerable: true, get: () => _clearActivityElapsedTimer, set: (value) => { _clearActivityElapsedTimer = value; } },
  _setCtxCompressButton: { enumerable: true, get: () => _setCtxCompressButton, set: (value) => { _setCtxCompressButton = value; } },
  _syncMobileCtxDisplay: { enumerable: true, get: () => _syncMobileCtxDisplay, set: (value) => { _syncMobileCtxDisplay = value; } },
  _mergeUsageForCtxIndicator: { enumerable: true, get: () => _mergeUsageForCtxIndicator, set: (value) => { _mergeUsageForCtxIndicator = value; } },
  _syncCtxIndicator: { enumerable: true, get: () => _syncCtxIndicator, set: (value) => { _syncCtxIndicator = value; } },
  _setMessageScrollToBottom: { enumerable: true, get: () => _setMessageScrollToBottom, set: (value) => { _setMessageScrollToBottom = value; } },
  _isMessagePaneNearBottom: { enumerable: true, get: () => _isMessagePaneNearBottom, set: (value) => { _isMessagePaneNearBottom = value; } },
  _messageBottomDistance: { enumerable: true, get: () => _messageBottomDistance, set: (value) => { _messageBottomDistance = value; } },
  _repinMessagesAfterComposerResize: { enumerable: true, get: () => _repinMessagesAfterComposerResize, set: (value) => { _repinMessagesAfterComposerResize = value; } },
  _shouldFollowMessagesOnDomReplace: { enumerable: true, get: () => _shouldFollowMessagesOnDomReplace, set: (value) => { _shouldFollowMessagesOnDomReplace = value; } },
  _followMessagesAfterDomReplace: { enumerable: true, get: () => _followMessagesAfterDomReplace, set: (value) => { _followMessagesAfterDomReplace = value; } },
  _settleMessageScrollToBottom: { enumerable: true, get: () => _settleMessageScrollToBottom, set: (value) => { _settleMessageScrollToBottom = value; } },
  _settleFinalScroll: { enumerable: true, get: () => _settleFinalScroll, set: (value) => { _settleFinalScroll = value; } },
  scrollIfPinned: { enumerable: true, get: () => scrollIfPinned, set: (value) => { scrollIfPinned = value; } },
  scrollToBottom: { enumerable: true, get: () => scrollToBottom, set: (value) => { scrollToBottom = value; } },
  _fmtOllamaLabel: { enumerable: true, get: () => _fmtOllamaLabel, set: (value) => { _fmtOllamaLabel = value; } },
  getModelLabel: { enumerable: true, get: () => getModelLabel, set: (value) => { getModelLabel = value; } },
  _gatewayProviderName: { enumerable: true, get: () => _gatewayProviderName, set: (value) => { _gatewayProviderName = value; } },
  _gatewayRoutingLabel: { enumerable: true, get: () => _gatewayRoutingLabel, set: (value) => { _gatewayRoutingLabel = value; } },
  _formatGatewayModelLabel: { enumerable: true, get: () => _formatGatewayModelLabel, set: (value) => { _formatGatewayModelLabel = value; } },
  _gatewayRoutingFailoverText: { enumerable: true, get: () => _gatewayRoutingFailoverText, set: (value) => { _gatewayRoutingFailoverText = value; } },
  _gatewayModelWarningText: { enumerable: true, get: () => _gatewayModelWarningText, set: (value) => { _gatewayModelWarningText = value; } },
  _latestGatewayRoutingForSession: { enumerable: true, get: () => _latestGatewayRoutingForSession, set: (value) => { _latestGatewayRoutingForSession = value; } },
  _stripXmlToolCallsDisplay: { enumerable: true, get: () => _stripXmlToolCallsDisplay, set: (value) => { _stripXmlToolCallsDisplay = value; } },
  _sanitizeThinkingDisplayText: { enumerable: true, get: () => _sanitizeThinkingDisplayText, set: (value) => { _sanitizeThinkingDisplayText = value; } },
  _normalizeThinkingEchoCompare: { enumerable: true, get: () => _normalizeThinkingEchoCompare, set: (value) => { _normalizeThinkingEchoCompare = value; } },
  _stripVisibleAssistantEchoFromThinking: { enumerable: true, get: () => _stripVisibleAssistantEchoFromThinking, set: (value) => { _stripVisibleAssistantEchoFromThinking = value; } },
  _MOBILE_CONFIG_BASE_LABEL: { enumerable: true, get: () => _MOBILE_CONFIG_BASE_LABEL },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
