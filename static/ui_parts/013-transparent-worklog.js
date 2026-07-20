// ── Transparent turn-level collapse (Hermes chat name tag) ───────────────
// In transparent_stream mode the assistant role label is the turn's "name
// tag". Clicking it collapses the entire event stack underneath so the
// transcript shows only the final answer (Output only). A chevron on the
// role telegraph the affordance; the blocks body animates with the same
// max-height transition used by individual event cards. Persisted via
// data-attribute only — the turn's render path reads it on rebuild.
const _transparentTurnCollapsedStates={}; // key: `${sid}:${turnMsgIdx}` → boolean
function _wireTransparentTurnToggle(turn){
  if(!turn) return;
  if(!isTransparentStream()) return;
  const role=turn.querySelector('.msg-role.assistant');
  if(!role) return;
  turn.setAttribute('data-transparent-turn-toggle-bound','1');
  // Add chevron if missing.
  if(!role.querySelector('.transparent-turn-chevron')){
    const chev=document.createElement('span');
    chev.className='transparent-turn-chevron';
    chev.innerHTML=li('chevron-down',10);
    role.appendChild(chev);
  }
  role.setAttribute('role','button');
  role.setAttribute('tabindex','0');
  role.setAttribute('aria-expanded',turn.getAttribute('data-transparent-turn-collapsed')==='1'?'false':'true');
  const toggle=function(ev){
    if(ev&&ev.target&&ev.target.closest&&ev.target.closest('.msg-tps-inline')) return;
    const collapsed=turn.getAttribute('data-transparent-turn-collapsed')==='1';
    turn.setAttribute('data-transparent-turn-collapsed',collapsed?'0':'1');
    role.setAttribute('aria-expanded',collapsed?'true':'false');
    // Persist state across DOM rebuilds.
    if(S.session){
      const seg=turn.querySelector('.assistant-segment');
      if(seg){
        const mi=seg.getAttribute('data-msg-idx');
        if(mi!=null) _transparentTurnCollapsedStates[`${S.session.session_id}:${mi}`]=!collapsed;
      }
    }
  };
  role.onclick=toggle;
  role.onkeydown=function(ev){
    if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();toggle(ev);}
  };
}
// ── Transparent old-event fading (medium → low) ───────────────────────────
// In long streams the earliest rows fade to a lower opacity so the user's
// eye lands on the most recent activity. The fade is per-turn: the newest
// event stays at full opacity, each earlier event drops one step. Floors
// at 0.32 so labels stay readable.
function _applyTransparentRowFading(turn){
  if(!turn||!isTransparentStream()) return;
  // Recency-fading only makes sense on the LIVE turn (draw the eye to the most
  // recent activity). On settled/historical turns it permanently dims the trace
  // below readable contrast (floor .32) — the opposite of a transparent record.
  // So clear any fade on non-live turns and only fade the live turn.
  // (Trifecta finding V8.)
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const rows=Array.from(blocks.querySelectorAll(':scope > .transparent-event-row'));
  const isLive=turn.id==='liveAssistantTurn'||turn.getAttribute('data-live-assistant-turn')==='1';
  if(!isLive){
    rows.forEach(row=>row.removeAttribute('data-transparent-fade'));
    return;
  }
  const total=rows.length;
  for(let i=0;i<total;i++){
    const row=rows[i];
    // Newest = full opacity; each step back drops by 1 (floors at 5).
    const stepsFromEnd=total-1-i;
    if(stepsFromEnd<=0){row.removeAttribute('data-transparent-fade');continue;}
    const step=Math.min(5,stepsFromEnd);
    row.setAttribute('data-transparent-fade',String(step));
  }
}
// ── Transparent turn footer (elapsed · tokens · TTFT · status) ───────────
// Mirrors the live run-status line for settled turns in transparent
// mode. Shows duration, first-token time, token usage, and final status.
// Only rendered for turns that have transparent event rows.
function _transparentTurnFooterHtml(durationText, ttftText, tokensText, statusText){
  const parts=[];
  if(durationText) parts.push(`<span class="lf-time">${esc(durationText)}</span>`);
  if(ttftText) parts.push(`<span class="lf-ttft" title="${esc(t('first_token_time')||'Time to first token')}">TTFT ${esc(ttftText)}</span>`);
  if(tokensText) parts.push(`<span class="lf-tokens">${esc(tokensText)}</span>`);
  if(statusText) parts.push(`<span class="lf-status">${esc(statusText)}</span>`);
  if(!parts.length) return '';
  return `<div class="transparent-turn-footer">${parts.join('<span class="lf-sep">·</span>')}</div>`;
}
function _renderTransparentTurnFooter(turn, opts){
  if(!turn||!isTransparentStream()) return;
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const hasRows=blocks.querySelector(':scope > .transparent-event-row');
  if(!hasRows){
    // No events → no footer (the answer itself carries the duration).
    const existing=turn.querySelector('.transparent-turn-footer');
    if(existing) existing.remove();
    return;
  }
  const durationText=opts&&opts.durationText||'';
  const ttftText=opts&&opts.ttftText||'';
  const tokensText=opts&&opts.tokensText||'';
  const statusText=opts&&opts.statusText||(t('done')||'Done');
  const html=_transparentTurnFooterHtml(durationText, ttftText, tokensText, statusText);
  let footer=turn.querySelector('.transparent-turn-footer');
  if(!html){
    if(footer) footer.remove();
    return;
  }
  if(!footer){
    footer=document.createElement('div');
    footer.className='transparent-turn-footer';
    const blocks=turn.querySelector('.assistant-turn-blocks');
    // Guard: nextSibling may be null (blocks is last child) or orphaned from
    // a prior DOM rebuild. Only insertBefore when it is still a child of turn.
    if(blocks&&blocks.nextSibling&&blocks.nextSibling.parentNode===turn){
      turn.insertBefore(footer, blocks.nextSibling);
    }else{
      turn.appendChild(footer);
    }
  }
  footer.innerHTML=html.replace(/^<div class="transparent-turn-footer">|<\/div>$/g,'');
}
// ── Activity-group user expand intent (#1298) ──────────────────────────────
// When the user manually expands the live "Activity" dropdown during streaming,
// preserve that intent across the destroy/recreate cycle that fires on every
// thinking/tool event. Without this, ensureActivityGroup() re-creates the group
// with the default collapsed state and finalizeThinkingCard() force-collapses
// it whenever the assistant transitions from thinking → tool → thinking, so
// the panel snaps shut every few seconds while the user is trying to read it.
//
// The tracker is a singleton boolean: there is at most one live activity group
// at a time (selector .tool-call-group[data-live-tool-call-group="1"]). It is
// set to true when the user clicks the summary to expand, false when they
// click to collapse, and cleared back to undefined when the live group is
// finalized into a settled assistant turn (the live attribute is removed in
// _convertLiveActivityGroupToSettled / when liveAssistantTurn loses its id).
let _liveActivityUserExpanded;
const _activityDisclosureStoragePrefix='hermes-activity-disclosure:';
function _activityDisclosureStorageKey(activityKey){
  if(!activityKey||!S.session||!S.session.session_id) return null;
  return _activityDisclosureStoragePrefix+S.session.session_id+':'+activityKey;
}
function _readActivityDisclosureState(activityKey){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return null;
  try{
    const saved=localStorage.getItem(key);
    return saved==='open'||saved==='closed'?saved:null;
  }catch(_){return null;}
}
function _writeActivityDisclosureState(activityKey, open){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return;
  try{localStorage.setItem(key, open?'open':'closed');}catch(_){}
}
function _copyActivityDisclosureState(fromActivityKey, toActivityKey){
  const state=_readActivityDisclosureState(fromActivityKey);
  if(state) _writeActivityDisclosureState(toActivityKey, state==='open');
}
function _activityKeyForLiveTurn(){
  return S.activeStreamId?'live:'+S.activeStreamId:null;
}
function _onLiveActivityToggle(group){
  if(!group) return;
  // Only track explicit user clicks on the live group, not programmatic toggles.
  if(group.getAttribute('data-live-tool-call-group')!=='1') return;
  _liveActivityUserExpanded = !group.classList.contains('tool-call-group-collapsed');
}
function _materializeDeferredWorklogRows(group){
  // #5839: build the row DOM for a settled worklog whose rows were deferred at
  // render time (collapsed). Idempotent — clears the marker so it runs once.
  if(!group||group.getAttribute('data-worklog-rows-deferred')!=='1') return false;
  let rows=group._deferredWorklogRows;
  // The JS-property stash is dropped when the transcript is restored from the
  // HTML cache (innerHTML round-trip). Recover the rows from the owning message
  // via the disclosure key (anchor-scene:<rawIdx>) so a post-restore expand
  // still fills the worklog. (#5839)
  if((!rows||!rows.length)&&typeof _deferredWorklogRowsFromGroup==='function'){
    rows=_deferredWorklogRowsFromGroup(group);
  }
  group.removeAttribute('data-worklog-rows-deferred');
  group._deferredWorklogRows=null;
  if(!rows||!rows.length) return false;
  const ok=_renderAnchorSceneRowsIntoWorklog(group,rows,{settled:true});
  if(!ok) return false;
  // #5839 fix: the eager render path post-processes its rows (syntax highlight,
  // copy buttons, mermaid, katex, structured trees) and restores detail-disclosure
  // state; a lazily-materialized group must do the same or expanded rows render
  // un-enhanced and any captured open/scroll state is lost. Post-process on the
  // next frame (matching the eager rebuild paths), then re-apply the disclosure
  // state stashed with the group at defer time.
  const disclosure=group._deferredWorklogDisclosure;
  group._deferredWorklogDisclosure=null;
  if(typeof _postProcessWithAnchorSuppression==='function'
     && typeof requestAnimationFrame==='function'){
    requestAnimationFrame(()=>{
      _postProcessWithAnchorSuppression(group);
      if(disclosure&&disclosure.size&&typeof _restoreWorklogDetailDisclosureState==='function'){
        _restoreWorklogDetailDisclosureState(group, disclosure);
      }
    });
  }else if(disclosure&&disclosure.size&&typeof _restoreWorklogDetailDisclosureState==='function'){
    _restoreWorklogDetailDisclosureState(group, disclosure);
  }
  return true;
}
function _deferredWorklogRowsFromGroup(group){
  // Recover a settled worklog's rows from S.messages using the group's
  // disclosure key `anchor-scene:<rawIdx>`. Used after an HTML-cache restore
  // where the _deferredWorklogRows JS property was dropped. (#5839)
  const key=group&&group.getAttribute&&group.getAttribute('data-activity-disclosure-key');
  const m=key&&/^anchor-scene:(\d+)$/.exec(key);
  if(!m) return null;
  const msg=S.messages&&S.messages[Number(m[1])];
  const scene=msg&&msg._anchor_activity_scene;
  if(!scene) return null;
  return _anchorSceneRowsForRendering(scene,{settled:true});
}
function _rehydrateDeferredWorklogsFromCache(root){
  // After restoring a transcript from _sessionHtmlCache, deferred settled
  // worklogs carry data-worklog-rows-deferred="1" but lost their stashed rows
  // (JS properties don't survive innerHTML). Re-stash from the owning message so
  // the first expand materializes correctly. (#5839)
  if(!root||!root.querySelectorAll) return;
  root.querySelectorAll('[data-worklog-rows-deferred="1"]').forEach(group=>{
    if(group._deferredWorklogRows&&group._deferredWorklogRows.length) return;
    const rows=_deferredWorklogRowsFromGroup(group);
    if(rows&&rows.length) group._deferredWorklogRows=rows;
    else group.removeAttribute('data-worklog-rows-deferred'); // nothing to defer
  });
}
function _toggleActivityGroup(summary){
  const group=summary&&summary.closest?summary.closest('.agent-activity-group,.tool-call-group'):null;
  if(!group) return;
  const collapsed=group.classList.toggle('tool-call-group-collapsed');
  group.classList.toggle('open',!collapsed);
  summary.setAttribute('aria-expanded',String(!collapsed));
  // #5839: materialize deferred settled rows on first expand (lazy render).
  if(!collapsed) _materializeDeferredWorklogRows(group);
  _writeActivityDisclosureState(group.getAttribute('data-activity-disclosure-key'), !collapsed);
  if(typeof _onLiveActivityToggle==='function') _onLiveActivityToggle(group);
}
function _toggleToolWorklogGroup(summary){
  const group=summary&&summary.closest?summary.closest('.tool-worklog-tool-group,.tool-group'):null;
  if(group){
    const collapsed=group.classList.toggle('tool-worklog-tool-group-collapsed');
    group.classList.toggle('open',!collapsed);
    summary.setAttribute('aria-expanded',String(!collapsed));
    return;
  }
  return _toggleActivityGroup(summary);
}
function _finalizeLiveActivityDisclosureGroup(group){
  if(!group) return;
  const keepOpen=!!(
    group.querySelector&&group.querySelector('.tool-card.open,.thinking-card.open,.tool-group.open,.tool-worklog-tool-group.open')
  );
  const disclosureKey=group.getAttribute('data-activity-disclosure-key')||group.getAttribute('data-tool-worklog-key')||'';
  group.removeAttribute('data-live-activity-current');
  group.removeAttribute('data-live-tool-call-group');
  group.removeAttribute('data-live-tool-worklog-group');
  group.removeAttribute('data-live-anchor-scene-owner');
  group.classList.toggle('tool-call-group-collapsed', !keepOpen);
  group.classList.toggle('open', keepOpen);
  if(keepOpen&&disclosureKey) _writeActivityDisclosureState(disclosureKey, true);
  const summary=group.querySelector&&group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
  if(summary){
    summary.removeAttribute('data-live-summary-static');
    summary.removeAttribute('aria-disabled');
    summary.disabled=false;
    summary.setAttribute('aria-expanded',keepOpen?'true':'false');
  }
  if(typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(group);
}
function _worklogReasonHtmlFromAnchor(anchor, textOverride){
  if(!anchor||!anchor.matches||!anchor.matches('.assistant-segment')) return '';
  const body=anchor.querySelector&&anchor.querySelector('.msg-body');
  const hasOverride=arguments.length>1;
  const text=hasOverride?String(textOverride||''):((body?body.textContent:anchor.textContent)||'');
  if(!String(text||'').trim()) return '';
  if(String(text||'').trim()==='(empty)') return '';
  if(hasOverride) return _worklogReasonHtmlFromText(text);
  return body?body.innerHTML:esc(String(text||'').trim());
}
function _worklogReasonHtmlFromText(text){
  const clean=_sanitizeThinkingDisplayText(text);
  if(!String(clean||'').trim()) return '';
  if(String(clean||'').trim()==='(empty)') return '';
  return renderMd?renderMd(clean):esc(clean);
}
function _renderWorklogReasonInto(row, text){
  if(!row) return;
  const html=_worklogReasonHtmlFromText(text);
  row.innerHTML=html;
}
function _worklogReasonNodeFromText(text, attrs){
  if(window._showThinking===false) return null;
  const html=_worklogReasonHtmlFromText(text);
  if(!html) return null;
  const row=document.createElement('div');
  row.className='wl-reason';
  row.setAttribute('data-worklog-reason-source','reasoning');
  if(attrs&&attrs.active) row.setAttribute('data-worklog-reason-active','1');
  row.innerHTML=html;
  return row;
}
let _worklogAnchorKeySeq=0;
function _worklogReasonAnchorKey(anchor){
  if(!anchor||!anchor.dataset) return '';
  if(anchor.dataset.worklogAnchorKey) return anchor.dataset.worklogAnchorKey;
  const segmentSeq=anchor.getAttribute('data-live-segment-seq')||'';
  const burstId=anchor.getAttribute('data-activity-burst-id')||'';
  const msgIdx=anchor.getAttribute('data-msg-idx')||'';
  const raw=String(anchor.getAttribute('data-raw-text')||anchor.textContent||'').trim().slice(0,80);
  const key=segmentSeq
    ? `segment:${segmentSeq}`
    : msgIdx
    ? `msg:${msgIdx}`
    : burstId&&raw
    ? `burst:${burstId}:${raw}`
    : burstId
    ? `burst:${burstId}`
    : `node:${++_worklogAnchorKeySeq}`;
  anchor.dataset.worklogAnchorKey=key;
  return key;
}
function _syncWorklogReasonFromAnchor(group, anchor, displayTextOverride){
  const list=_toolWorklogListEl(group);
  if(!group||!list) return;
  const anchorKey=_worklogReasonAnchorKey(anchor);
  const selector=anchorKey?`:scope > .wl-reason[data-worklog-anchor-key="${CSS.escape(anchorKey)}"]`:':scope > .wl-reason[data-worklog-anchor-reason="1"]';
  // When reasoning/thinking display is turned off (#3903), do not render Worklog
  // reasoning rows on the live OR settled path — remove any existing one and bail
  // before building. (The gate must live here, in the actual render path, not in
  // the unused _worklogReasonNodeFromText helper.)
  if(window._showThinking===false){
    const existing=list.querySelector(selector);
    if(existing) existing.remove();
    return;
  }
  const html=arguments.length>2
    ? _worklogReasonHtmlFromAnchor(anchor, displayTextOverride)
    : _worklogReasonHtmlFromAnchor(anchor);
  let reason=list.querySelector(selector);
  if(!html){
    if(reason) reason.remove();
    return;
  }
  if(!reason){
    reason=document.createElement('div');
    reason.className='wl-reason';
    reason.setAttribute('data-worklog-anchor-reason','1');
    if(anchorKey) reason.setAttribute('data-worklog-anchor-key',anchorKey);
    list.appendChild(reason);
  }
  reason.innerHTML=html;
  if(anchor){
    anchor.classList.add('assistant-segment-worklog-source');
    anchor.setAttribute('aria-hidden','true');
    anchor.hidden=true;
  }
}
function ensureLiveWorklogContainer(blocks, opts){
  opts=opts||{};
  if(!blocks) return null;
  const activityKey=opts.activityKey||_activityKeyForLiveTurn();
  let worklog=activityKey
    ? blocks.querySelector(`.live-worklog[data-live-worklog-shell="1"][data-tool-worklog-key="${CSS.escape(activityKey)}"]`)
    : null;
  if(!worklog) worklog=blocks.querySelector('.live-worklog[data-live-worklog-shell="1"][data-live-activity-current="1"]');
  if(!worklog){
    worklog=document.createElement('div');
    worklog.className='live-worklog worklog';
    worklog.setAttribute('data-live-worklog-shell','1');
    worklog.setAttribute('data-live-tool-worklog-group','1');
    worklog.setAttribute('data-live-tool-call-group','1');
    worklog.setAttribute('data-live-activity-current','1');
    worklog.setAttribute('data-tool-worklog-group','1');
    worklog.setAttribute('data-tool-worklog-key',activityKey||'');
    worklog.innerHTML='<div class="tool-worklog-list"></div>';
    const anchor=opts.anchor||null;
    const footer=blocks.querySelector('#liveRunStatus');
    if(anchor&&anchor.parentElement===blocks) anchor.insertAdjacentElement('afterend',worklog);
    else if(footer&&footer.parentElement===blocks) blocks.insertBefore(worklog,footer);
    else blocks.appendChild(worklog);
  }else if(activityKey&&!worklog.getAttribute('data-tool-worklog-key')){
    worklog.setAttribute('data-tool-worklog-key',activityKey);
  }
  if(opts.anchor) _syncWorklogReasonFromAnchor(worklog, opts.anchor);
  _migrateLegacyLiveActivityGroupsToWorklog(blocks, worklog);
  _syncToolCallGroupSummary(worklog);
  return worklog;
}
function _migrateLegacyLiveActivityGroupsToWorklog(blocks, worklog){
  if(!blocks||!worklog) return;
  const list=_toolWorklogListEl(worklog);
  if(!list) return;
  const legacy=Array.from(blocks.querySelectorAll('.tool-worklog-group[data-live-tool-call-group="1"],.tool-call-group[data-live-tool-call-group="1"]'))
    .filter(group=>group!==worklog && !group.classList.contains('live-worklog'));
  for(const group of legacy){
    const oldList=_toolWorklogListEl(group);
    if(oldList){
      while(oldList.firstChild) list.appendChild(oldList.firstChild);
    }
    group.remove();
  }
}
function _appendWorklogReason(list, anchor){
  if(!list) return null;
  // Reasoning display off (#3903): never append a Worklog reasoning row.
  if(window._showThinking===false) return null;
  const html=_worklogReasonHtmlFromAnchor(anchor);
  if(!html) return null;
  const reason=document.createElement('div');
  reason.className='wl-reason';
  reason.setAttribute('data-worklog-anchor-reason','1');
  const anchorKey=_worklogReasonAnchorKey(anchor);
  if(anchorKey) reason.setAttribute('data-worklog-anchor-key',anchorKey);
  reason.innerHTML=html;
  list.appendChild(reason);
  if(anchor){
    anchor.classList.add('assistant-segment-worklog-source');
    anchor.setAttribute('aria-hidden','true');
    anchor.hidden=true;
  }
  return reason;
}
function _toolIdentity(tc){
  if(!tc) return '';
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  if(tid) return `id:${tid}`;
  const args=tc.args&&typeof tc.args==='object'?tc.args:{};
  return [
    tc.assistant_msg_idx!==undefined?`a:${tc.assistant_msg_idx}`:'',
    tc.name||'tool',
    JSON.stringify(args),
    String(tc.snippet||tc.preview||'').slice(0,160),
  ].join('|');
}
function _toolDisclosureIdentity(tc){
  if(!tc) return '';
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  if(tid) return `id:${tid}`;
  const stable=[
    tc.assistant_msg_idx!==undefined?`a:${tc.assistant_msg_idx}`:'',
    tc.name||'tool',
  ].join('\x1f');
  return stable.trim()?`derived:${_worklogDetailHashKey(stable)}`:'';
}
function _filterNewWorklogTools(cards, seenTools){
  const out=[];
  for(const tc of Array.from(cards||[]).filter(Boolean)){
    const key=_toolIdentity(tc);
    if(key&&seenTools&&seenTools.has(key)) continue;
    if(key&&seenTools) seenTools.add(key);
    out.push(tc);
  }
  return out;
}
function _anchorSceneToolRowLogicalKey(row){
  if(!row||row.role!=='tool') return '';
  const tool=(row.tool&&typeof row.tool==='object')?row.tool:{};
  const payload=(row.payload&&typeof row.payload==='object')?row.payload:{};
  const id=row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id||
    payload.tid||payload.id||payload.tool_call_id||payload.tool_use_id||payload.call_id||'';
  return id?`call:${id}`:'';
}
function _anchorSceneMergeToolRows(prev, row){
  if(!prev) return row;
  const prevTool=(prev.tool&&typeof prev.tool==='object')?prev.tool:{};
  const nextTool=(row&&row.tool&&typeof row.tool==='object')?row.tool:{};
  const prevPayload=(prev.payload&&typeof prev.payload==='object')?prev.payload:{};
  const nextPayload=(row&&row.payload&&typeof row.payload==='object')?row.payload:{};
  const prevArgs=(prevTool.args&&typeof prevTool.args==='object')?prevTool.args:
    ((prevPayload.args&&typeof prevPayload.args==='object')?prevPayload.args:{});
  const nextArgs=(nextTool.args&&typeof nextTool.args==='object')?nextTool.args:
    ((nextPayload.args&&typeof nextPayload.args==='object')?nextPayload.args:{});
  const mergedPayload=Object.assign({},prevPayload,nextPayload);
  const mergedTool=Object.assign({},prevTool,nextTool);
  if(!Object.keys(nextArgs).length&&Object.keys(prevArgs).length) mergedTool.args=prevArgs;
  const prevPreview=String(prevTool.preview||prevPayload.preview||'').trim();
  const nextText=String(row&&row.text||'').trim();
  const nextSnippet=String(nextTool.snippet||nextPayload.snippet||nextPayload.result||nextPayload.output||'').trim();
  const nextStatus=String(row&&row.status||'').toLowerCase();
  const nextLooksLikeResult=!!nextSnippet||(
    !!nextText&&nextStatus&&nextStatus!=='running'&&nextStatus!=='pending'
  );
  if(prevPreview&&nextLooksLikeResult){
    mergedTool.preview=prevTool.preview||prevPayload.preview||prevPreview;
    if(!mergedPayload.preview) mergedPayload.preview=prevPayload.preview||prevPreview;
  }
  if(nextSnippet) mergedTool.snippet=nextTool.snippet||nextPayload.snippet||nextPayload.result||nextPayload.output||nextSnippet;
  return Object.assign({},prev,row,{
    row_id:prev.row_id||row.row_id,
    order_index:prev.order_index??row.order_index,
    payload:mergedPayload,
    tool:mergedTool,
  });
}
function _appendWorklogStep(group, anchor, cards, thinkingText, opts){
  const list=_toolWorklogListEl(group);
  if(!group||!list) return;
  let wroteProse=false;
  const seenReasons=opts&&opts.seenReasons;
  if(!opts||opts.includeAnchorReason!==false){
    const anchorKey=anchor&&anchor.dataset&&anchor.dataset.msgIdx?`anchor:${anchor.dataset.msgIdx}`:'';
    if(!anchorKey||!seenReasons||!seenReasons.has(anchorKey)){
      const reason=_appendWorklogReason(list, anchor);
      if(reason){
        wroteProse=true;
        if(anchorKey&&seenReasons) seenReasons.add(anchorKey);
      }
    }
  }
  if(thinkingText){
    const thinkingKey=(opts&&opts.thinkingKey)||`reason:${String(thinkingText).trim()}`;
    const thinkingDisclosureKey=(opts&&opts.thinkingDisclosureKey)||thinkingKey;
    if(!seenReasons||!seenReasons.has(thinkingKey)){
      const thinking=_thinkingActivityNode(thinkingText, false, thinkingDisclosureKey);
      if(thinking){
        list.appendChild(thinking);
        wroteProse=true;
        if(seenReasons) seenReasons.add(thinkingKey);
      }
    }
  }
  const toolCards=_filterNewWorklogTools(cards, opts&&opts.seenTools);
  if(toolCards.length){
    const last=list.lastElementChild;
    let tools=(!wroteProse&&last&&last.classList&&last.classList.contains('wl-step-tools')&&last.getAttribute('data-worklog-tools')==='1')
      ? last
      : null;
    if(!tools){
      tools=document.createElement('div');
      tools.className='wl-step-tools tool-worklog-tools';
      tools.setAttribute('data-worklog-tools','1');
      list.appendChild(tools);
    }
    for(const tc of toolCards) tools.appendChild(buildToolCard(tc));
    _syncToolRowsContainer(tools, !!(opts&&opts.live));
  }
}
function _anchorSceneRowsForRendering(scene, opts){
  const rows=Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[];
  const settled=!!(opts&&opts.settled);
  const live=!settled;
  const out=[];
  const byKey=new Map();
  const liveProseTextKeys=new Map();
  const proseTextKey=(value)=>String(value||'').replace(/\s+/g,' ').trim();
  const keyFor=(row)=>{
    if(!row) return '';
    if(row.role==='tool') return `tool:${_anchorSceneToolRowLogicalKey(row)||row.row_id||row.event_id||row.local_id||out.length}`;
    if(row.role==='prose') return `prose:${row.local_id||row.row_id||out.length}`;
    if(row.role==='thinking') return `thinking:${row.local_id||row.row_id||out.length}`;
    if(row.role==='lifecycle'){
      const source=String(row.source_event_type||'');
      if(source==='compressing'||source==='compressed') return 'lifecycle:compression';
      return `lifecycle:${source||row.local_id||row.row_id||out.length}`;
    }
    return `row:${row.row_id||out.length}`;
  };
  for(const row of rows){
    if(!row||typeof row!=='object') continue;
    if(row.role==='terminal'&&row.source_event_type==='done') continue;
    if(_anchorSceneIsSettledSuccessfulCompression(row,settled)) continue;
    const text=String(row.text||'').trim();
    if((row.role==='prose'||row.role==='thinking')&&!text) continue;
    const key=keyFor(row);
    if(byKey.has(key)){
      const index=byKey.get(key);
      if(live&&row.role==='prose'){
        const textKey=proseTextKey(text);
        const duplicateIndex=textKey?liveProseTextKeys.get(textKey):undefined;
        if(duplicateIndex!==undefined&&duplicateIndex!==index) continue;
        const previousTextKey=proseTextKey(out[index]&&out[index].text);
        if(previousTextKey&&previousTextKey!==textKey&&liveProseTextKeys.get(previousTextKey)===index){
          liveProseTextKeys.delete(previousTextKey);
        }
        if(textKey) liveProseTextKeys.set(textKey,index);
      }
      out[index]=row.role==='tool'?_anchorSceneMergeToolRows(out[index],row):row;
    }else{
      if(live&&row.role==='prose'){
        const textKey=proseTextKey(text);
        if(textKey&&liveProseTextKeys.has(textKey)) continue;
        if(textKey) liveProseTextKeys.set(textKey,out.length);
      }
      byKey.set(key,out.length);
      out.push(row);
    }
  }
  return out;
}
function _anchorSceneIsSettledSuccessfulCompression(row, settled){
  if(!settled||!row||row.role!=='lifecycle') return false;
  const source=String(row.source_event_type||'');
  if(source!=='compressing'&&source!=='compressed') return false;
  const status=String(row.status||'').toLowerCase();
  return !['error','failed','failure','compression_exhausted','degraded','interrupted','connection_lost'].includes(status);
}
function _anchorSceneToolCallFromRow(row, opts){
  const tool=(row&&row.tool&&typeof row.tool==='object')?row.tool:{};
  const payload=(row&&row.payload&&typeof row.payload==='object')?row.payload:{};
  const timestampSeconds=typeof _timestampSeconds==='function'?_timestampSeconds:function(value){
    const stamp=Number(value);
    return Number.isFinite(stamp)&&stamp>0?(stamp>1e12?stamp/1000:stamp):null;
  };
  const firstValidTimestampSeconds=typeof _firstValidTimestampSeconds==='function'
    ? _firstValidTimestampSeconds
    : function(...values){
        for(const value of values){
          const stamp=timestampSeconds(value);
          if(stamp) return stamp;
        }
        return null;
      };
  const rowTs=typeof _anchorSceneRowTimestampSeconds==='function'
    ? _anchorSceneRowTimestampSeconds(row)
    : firstValidTimestampSeconds(row&&row.created_at, row&&row.timestamp, row&&row.ts, row&&row.started_at, row&&row.completed_at);
  const id=tool.id||row.tool_call_id||payload.tid||payload.id||payload.tool_call_id||payload.tool_use_id||payload.call_id||'';
  const settled=!!(opts&&opts.settled);
  return {
    name:tool.name||payload.name||'tool',
    args:(tool.args&&typeof tool.args==='object')?tool.args:((payload.args&&typeof payload.args==='object')?payload.args:{}),
    command:tool.command||payload.command||payload.cmd||'',
    raw_command:tool.raw_command||payload.raw_command||'',
    preview:tool.preview||payload.preview||'',
    snippet:tool.snippet||payload.snippet||payload.result||payload.output||(
      row&&row.status!=='running'&&row.status!=='pending'?row.text:''
    )||'',
    done:settled?true:(tool.done!==null&&tool.done!==undefined?tool.done:(row.status!=='running'&&row.status!=='pending')),
    is_error:!!(tool.is_error||payload.is_error||row.status==='error'||row.status==='failed'),
    duration:tool.duration||payload.duration||payload.duration_seconds,
    started_at:firstValidTimestampSeconds(tool.started_at, payload.started_at, rowTs),
    created_at:firstValidTimestampSeconds(tool.created_at, payload.created_at, rowTs),
    timestamp:firstValidTimestampSeconds(tool.timestamp, payload.timestamp, rowTs),
    ts:firstValidTimestampSeconds(
      tool.ts,
      payload.ts,
      tool.timestamp,
      payload.timestamp,
      tool.created_at,
      payload.created_at,
      rowTs
    ),
    tid:id,
    id,
  };
}
function _anchorSceneRowTimestampSeconds(row){
  if(!row) return null;
  const timestampSeconds=typeof _timestampSeconds==='function'?_timestampSeconds:function(value){
    const stamp=Number(value);
    return Number.isFinite(stamp)&&stamp>0?(stamp>1e12?stamp/1000:stamp):null;
  };
  for(const key of ['created_at','timestamp','ts','started_at','completed_at']){
    const stamp=timestampSeconds(row[key]);
    if(stamp) return stamp;
  }
  return null;
}
function _anchorSceneNodeForRow(row, opts){
  const settled=!!(opts&&opts.settled);
  if(!row) return null;
  let node=null;
  if(row.role==='prose'){
    const text=String(row.text||'').trim();
    if(!text) return null;
    // Incremental live rendering: reuse a persistent smd node fed only the delta
    // instead of re-parsing the whole growing answer on every streamed frame
    // (O(n^2) -> O(n)). Settled rows and any failure fall through to the full
    // renderMd path below, which stays the source of truth for the final DOM.
    const proseKey=row.local_id||row.row_id||'';
    if(!settled && proseKey && typeof window.__anchorProseIncrementalNode==='function'){
      const inc=window.__anchorProseIncrementalNode(proseKey,text);
      // Route the incremental node through the shared row-decoration block below
      // (data-anchor-scene-row / -row-id / -row-role / -source-event-type) instead
      // of returning early — otherwise live incremental prose rows lose the
      // identity attributes the scene reconciler matches on. (Codex gate #5466)
      if(inc){ node=inc; }
    }
    if(!node){
      node=document.createElement('div');
      node.className='assistant-segment';
      node.setAttribute('data-anchor-scene-prose','1');
      node.dataset.rawText=text;
      node.innerHTML=`<div class="msg-body">${renderMd?renderMd(text):esc(text)}</div>`;
    }
  }else if(row.role==='thinking'){
    if(window._showThinking===false) return null;
    const text=String(row.text||row.thinking&&row.thinking.text||'').trim();
    if(!text) return null;
    node=_thinkingActivityNode(text, false, row.row_id||row.local_id||'anchor-thinking');
  }else if(row.role==='tool'){
    node=buildToolCard(_anchorSceneToolCallFromRow(row,opts));
  }else if(row.role==='lifecycle'){
    if(row.source_event_type==='compressing'||row.source_event_type==='compressed'){
      node=_autoCompressionWorklogNode({
        phase:settled||row.source_event_type==='compressed'?'done':'running',
        automatic:true,
        message:row.text||'Compressing context',
      });
    }else{
      node=_activityStatusNode({
        kind:settled?'done':'waiting',
        label:row.text||row.status||'Working',
        status:!settled&&row.status==='running'?'running':'done',
        id:row.row_id||row.local_id||'',
      });
    }
  }else if(row.role==='control'){
    node=_activityStatusNode({
      kind:settled?'done':'waiting',
      label:row.text||row.source_event_type||'Waiting',
      status:settled?'done':'running',
      id:row.row_id||row.local_id||'',
    });
  }else if(row.role==='terminal'){
    const status=String(row.status||row.source_event_type||'').trim();
    const isError=['error','failed','connection_lost','interrupted','compression_exhausted','tool_limit_reached','no_response'].includes(status);
    node=_activityStatusNode({
      kind:isError?'warning':'done',
      label:row.text||status||'Turn ended',
      status:settled?'done':(isError?'error':'done'),
      id:row.row_id||row.local_id||'',
    });
  }
  if(!node) return null;
  node.setAttribute('data-anchor-scene-row','1');
  node.setAttribute('data-anchor-row-id',String(row.row_id||row.local_id||''));
  if(row.local_id) node.setAttribute('data-anchor-local-id',String(row.local_id));
  node.setAttribute('data-anchor-row-role',String(row.role||'activity'));
  node.setAttribute('data-anchor-source-event-type',String(row.source_event_type||''));
  return node;
}
function _anchorSceneTransparentNodeForRow(row, opts){
  const settled=!!(opts&&opts.settled);
  const live=!!(opts&&opts.live);
  if(!row) return null;
  let node=null;
  const eventTs=typeof _anchorSceneRowTimestampSeconds==='function'?_anchorSceneRowTimestampSeconds(row):null;
  const meta={
    segmentSeq:row.segment_seq||row.segmentSeq||'',
    burstId:row.activity_burst_id||row.burst_id||row.burstId||'',
  };
  if(row.role==='prose'){
    // The settled assistant segment already owns the FINAL answer prose, so a
    // prose row whose text matches the final answer must be suppressed here to
    // avoid duplicating the answer. But INTERMEDIATE progress prose (the
    // between-tool narration from earlier rounds) is NOT the final answer and
    // belongs in the chronological transparent history — dropping it loses the
    // interleaving the user saw live (#4568: tools were restored but mid-turn
    // prose still vanished on reload). Render intermediate prose as an inline
    // assistant-segment (same shape _anchorSceneNodeForRow builds), skip only
    // the final-answer duplicate.
    const text=String(row.text||'').trim();
    if(!text) return null;
    const finalAnswer=String((opts&&opts.finalAnswer)||'').trim();
    if(opts&&opts.liveTokenFinalPrefixEligible&&_anchorSceneLiveTokenFinalPrefix(row,text,finalAnswer)) return null;
    if(finalAnswer&&_anchorSceneProseMatchesFinalAnswer(text,finalAnswer)) return null;
    node=_anchorSceneNodeForRow(row,{settled});
    if(!node) return null;
    node=_decorateTransparentEventRow(node,{type:'prose',text,preview:text,...meta});
  }else if(row.role==='thinking'){
    if(window._showThinking===false) return null;
    const text=String(row.text||row.thinking&&row.thinking.text||'').trim();
    if(!text) return null;
    node=_decorateTransparentEventRow(_thinkingActivityNode(text,false,row.row_id||row.local_id||'anchor-thinking'),{
      type:'thinking',
      text,
      preview:text,
      ts:eventTs,
      ...meta,
      live,
    });
  }else if(row.role==='tool'){
    const toolCall=_anchorSceneToolCallFromRow(row,{settled});
    node=_decorateTransparentEventRow(buildToolCard(toolCall),{
      type:'tool',
      name:toolCall&&toolCall.name,
      status:_transparentToolStatus(toolCall,settled),
      toolCall,
      ts:eventTs,
      ...meta,
      live,
      settled,
    });
  }else{
    node=_anchorSceneNodeForRow(row,{settled});
    node=_decorateTransparentEventRow(node,{
      type:String(row.role||'activity'),
      ...meta,
      live,
    });
  }
  if(!node) return null;
  node.setAttribute('data-anchor-scene-row','1');
  if(settled) node.setAttribute('data-anchor-settled-scene-row','1');
  if(live) node.setAttribute('data-anchor-live-scene-row','1');
  node.setAttribute('data-anchor-row-id',String(row.row_id||row.local_id||''));
  if(row.local_id) node.setAttribute('data-anchor-local-id',String(row.local_id));
  node.setAttribute('data-anchor-row-role',String(row.role||'activity'));
  node.setAttribute('data-anchor-source-event-type',String(row.source_event_type||''));
  if(opts&&opts.streamId) node.setAttribute('data-anchor-stream-id',String(opts.streamId));
  if(opts&&opts.sessionId) node.setAttribute('data-session-id',String(opts.sessionId));
  if(live) node.setAttribute('data-live-stream-owned','1');
  return node;
}
function _anchorSceneLiveTokenFinalPrefix(row, proseText, finalAnswer){
  if(!row||row.role!=='prose'||row.kind!=='process_prose') return false;
  if(String(row.source_event_type||'')!=='token') return false;
  if(!String(row.local_id||'').startsWith('live-prose:')) return false;
  const norm=(s)=>String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const rowKey=norm(proseText), finalKey=norm(finalAnswer);
  return !!(rowKey&&finalKey&&rowKey.length<finalKey.length&&finalKey.startsWith(rowKey));
}
function _anchorSceneLastNonTerminalWorkRowIndex(rows){
  if(!Array.isArray(rows)) return -1;
  return rows.reduce((last,row,idx)=>(row&&row.role==='tool')?idx:last,-1);
}
// Whitespace-insensitive compare so a scene prose row that IS the final answer
// (possibly re-wrapped) is recognized and not duplicated against the segment.
// Codex #4568: the prefix tolerance must NOT be able to suppress a DISTINCT
// intermediate progress row that merely happens to be a prefix of the final
// answer (e.g. "I found the issue and I'm applying the fix now." when the final
// answer starts with that sentence). So require exact normalized equality, with
// a prefix tolerance allowed ONLY when the two strings are near-equal length
// (>=0.9 ratio, shorter >=80 chars) — i.e. genuine re-wrap/truncation of the
// SAME text, never a short intermediate sentence that prefixes a long answer.
function _anchorSceneProseMatchesFinalAnswer(proseText, finalAnswer){
  const norm=(s)=>String(s||'').replace(/\s+/g,' ').trim();
  const a=norm(proseText), b=norm(finalAnswer);
  if(!a||!b) return false;
  if(a===b) return true;
  if(!(a.startsWith(b)||b.startsWith(a))) return false;
  const shorter=Math.min(a.length,b.length), longer=Math.max(a.length,b.length);
  return shorter>=80 && (shorter/longer)>=0.9;
}
function _anchorSceneWorklogGroup(blocks, opts){
  if(!blocks) return null;
  const live=!!(opts&&opts.live);
  const activityKey=(opts&&opts.activityKey)||'anchor-scene';
  let group=blocks.querySelector(`.tool-worklog-group[data-anchor-scene-owner="1"][data-tool-worklog-key="${CSS.escape(activityKey)}"]`);
  if(!group){
    group=ensureActivityGroup(blocks,{
      // Respect callers that need the settled activity group open. Round 6:
      // pinned followers keep the just-settled worklog open so STREAM_DONE does
      // not collapse hundreds of px of live worklog and visibly clamp the pane.
      collapsed:(opts&&opts.collapsed!==undefined)?opts.collapsed:!live,
      live,
      activityKey,
      beforeAnchor:!!(opts&&opts.beforeAnchor),
      anchor:(opts&&opts.anchor)||null,
      turnDuration:opts&&opts.turnDuration,
      turnStartedAt:opts&&opts.turnStartedAt,
      syncAnchorReason:false,
    });
  }
  if(!group) return null;
  group.setAttribute('data-anchor-scene-owner','1');
  if(live) group.setAttribute('data-live-anchor-scene-owner','1');
  group.setAttribute('data-tool-worklog-key',activityKey);
  if(opts&&opts.streamId) group.setAttribute('data-anchor-stream-id',String(opts.streamId));
  if(opts&&opts.turnDuration!==undefined&&opts.turnDuration!==null) group.setAttribute('data-turn-duration',String(opts.turnDuration));
  if(opts&&opts.turnStartedAt!==undefined&&opts.turnStartedAt!==null) group.setAttribute('data-turn-started-at',String(opts.turnStartedAt));
  return group;
}
function _renderAnchorSceneRowsIntoWorklog(group, rows, opts){
  const list=_toolWorklogListEl(group);
  if(!group||!list) return false;
  list.innerHTML='';
  let wrote=false;
  let currentTools=null;
  for(const row of rows){
    const node=_anchorSceneNodeForRow(row,opts);
    if(!node) continue;
    if(row.role==='tool'){
      if(!currentTools){
        currentTools=document.createElement('div');
        currentTools.className='wl-step-tools tool-worklog-tools';
        currentTools.setAttribute('data-worklog-tools','1');
        list.appendChild(currentTools);
      }
      currentTools.appendChild(node);
    }else{
      currentTools=null;
      list.appendChild(node);
    }
    wrote=true;
  }
  if(wrote){
    _syncToolCallGroupSummary(group);
  }
  return wrote;
}
function _liveProcessedWorklogAnchorScore(group, index){
  if(!group) return -1;
  const hasRows=!!group.querySelector('.tool-card-row,.wl-reason,.agent-activity-thinking,[data-anchor-scene-row="1"]');
  const label=group.querySelector('.tool-worklog-label,.tool-call-group-label');
  const text=String(label&&label.textContent||'').trim();
  const hasElapsed=!!(group.getAttribute('data-active-turn-elapsed')||/\d/.test(text));
  let score=index;
  if(hasElapsed) score+=400;
  if(hasRows) score+=200;
  if(group.getAttribute('data-live-activity-current')==='1') score+=80;
  if(group.getAttribute('data-anchor-scene-owner')==='1') score+=40;
  if(group.getAttribute('data-live-tool-call-group')==='1') score+=20;
  return score;
}
function _dedupeLiveProcessedWorklogAnchors(turn){
  const blocks=_assistantTurnBlocks(turn||$('liveAssistantTurn'));
  if(!blocks) return null;
  const groups=Array.from(blocks.querySelectorAll(
    '.tool-worklog-group[data-tool-worklog-group="1"],'+
    '.tool-call-group[data-tool-worklog-group="1"],'+
    '.live-worklog[data-live-worklog-shell="1"]'
  )).filter(group=>group&&group.isConnected!==false);
  if(groups.length<=1) return groups[0]||null;
  let keep=groups[0];
  let keepScore=_liveProcessedWorklogAnchorScore(keep,0);
  groups.forEach((group,index)=>{
    const score=_liveProcessedWorklogAnchorScore(group,index);
    if(score>=keepScore){
      keep=group;
      keepScore=score;
    }
  });
  groups.forEach(group=>{
    if(group!==keep) group.remove();
  });
  if(keep&&typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(keep);
  return keep;
}
function isLiveAnchorActivitySceneOwner(streamId){
  const turn=$('liveAssistantTurn');
  if(!turn) return false;
  const owner=turn.getAttribute('data-anchor-scene-live-owner')==='1'||
    !!turn.querySelector('[data-live-anchor-scene-owner="1"],[data-anchor-scene-row="1"]');
  if(!owner) return false;
  const current=turn.getAttribute('data-anchor-stream-id')||'';
  return !streamId||!current||String(streamId)===current;
}
function _projectLiveAnchorActivitySceneForStream(streamId, mode){
  const api=(typeof window!=='undefined')?window.HermesAssistantTurnAnchors:null;
  const map=(typeof window!=='undefined')?window._liveAnchorRegistries:null;
  const registry=map&&streamId?map.get(streamId):null;
  if(!api||!registry||typeof api.projectAssistantTurnAnchorActivityScene!=='function') return null;
  try{
    return api.projectAssistantTurnAnchorActivityScene(registry,{mode:mode||'compact_worklog'});
  }catch(err){
    if(typeof console!=='undefined'&&console.warn) console.warn('assistant turn anchor scene projection failed',err);
    return null;
  }
}
function _prepareLiveAnchorScrollRebuildGuard(scrollSnapshot){
  const messagesEl=$('messages');
  if(!messagesEl||!scrollSnapshot) return {readerAwayFromBottom:false,release:null};
  const beforeBottomDistance=Math.max(0,messagesEl.scrollHeight-messagesEl.scrollTop-messagesEl.clientHeight);
  // Only treat the reader as away if they were ALREADY in a non-follow state.
  // A pinned follower can transiently have bottomDistance>250 mid-render (the
  // assistant body grows before the anchor scene re-renders), so keying on a
  // raw scrollTop>0 here would mis-classify a pinned reader as unpinned and kill
  // auto-follow. Require an explicit unpin/non-pinned signal instead.
  const readerAwayFromBottom=beforeBottomDistance>250&&(_messageUserUnpinned||_scrollPinned===false);
  if(!readerAwayFromBottom) return {readerAwayFromBottom:false,release:null};
  scrollSnapshot.pinned=false;
  scrollSnapshot.userUnpinned=true;
  scrollSnapshot.bottom=beforeBottomDistance;
  _messageUserUnpinned=true;
  _scrollPinned=false;
  _nearBottomCount=0;
  const msgInner=$('msgInner');
  if(!msgInner||!msgInner.style) return {readerAwayFromBottom:true,release:null};
  const guardPreviousKey='liveAnchorScrollGuardPreviousMinHeight';
  let previousMinHeight=msgInner.style.minHeight||'';
  if(msgInner.dataset&&Object.prototype.hasOwnProperty.call(msgInner.dataset,guardPreviousKey)){
    previousMinHeight=msgInner.dataset[guardPreviousKey]||'';
  }else if(msgInner.dataset){
    msgInner.dataset[guardPreviousKey]=previousMinHeight;
  }
  const guardHeight=Math.max(messagesEl.scrollHeight,Number(scrollSnapshot.scrollHeight)||0);
  if(guardHeight>0) msgInner.style.minHeight=`${guardHeight}px`;
  return {
    readerAwayFromBottom:true,
    release:()=>{
      msgInner.style.minHeight=previousMinHeight;
      if(msgInner.dataset&&msgInner.dataset[guardPreviousKey]===previousMinHeight){
        delete msgInner.dataset[guardPreviousKey];
      }
    },
  };
}
function _resetMismatchedLiveAssistantTurnForSession(turn, sessionId){
  const sid=String(sessionId||'');
  if(!turn||!sid||!turn.dataset) return false;
  const existingSid=String(turn.dataset.sessionId||'');
  if(!existingSid||existingSid===sid) return false;
  const blocks=typeof _assistantTurnBlocks==='function' ? _assistantTurnBlocks(turn) : turn;
  if(blocks){
    try{
      blocks.innerHTML='';
    }catch(_){
      while(blocks.firstChild) blocks.removeChild(blocks.firstChild);
    }
  }
  turn.dataset.sessionId=sid;
  return true;
}
function _liveAnchorReasoningRowForFallback(turn, opts){
  opts=opts||{};
  const blocks=typeof _assistantTurnBlocks==='function' ? _assistantTurnBlocks(turn) : turn;
  if(!turn||!blocks||!blocks.querySelectorAll) return null;
  const streamId=String(opts.streamId||S.activeStreamId||'');
  const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');
  const localId=String(opts.anchorReasoningLocalId||opts.localId||'').trim();
  if(!localId) return null;
  const rows=blocks.querySelectorAll(
    '[data-anchor-scene-row="1"][data-anchor-local-id]'
  );
  for(const row of Array.from(rows)){
    const anchorLocalId=String(row.getAttribute&&row.getAttribute('data-anchor-local-id')||'');
    if(anchorLocalId!==localId) continue;
    const rowRole=String(row.getAttribute&&row.getAttribute('data-anchor-row-role')||'');
    const rowSource=String(row.getAttribute&&row.getAttribute('data-anchor-source-event-type')||'');
    if(rowRole!=='thinking'&&rowSource!=='reasoning') continue;
    const rowStreamId=String(row.getAttribute&&row.getAttribute('data-anchor-stream-id')||'');
    if(rowStreamId&&streamId&&rowStreamId!==streamId) continue;
    const rowSessionId=String(row.getAttribute&&row.getAttribute('data-session-id')||'');
    if(rowSessionId&&sessionId&&rowSessionId!==sessionId) continue;
    return row;
  }
  return null;
}
function _updateLiveAnchorReasoningRowForFallback(turn, text, opts){
  const clean=_sanitizeThinkingDisplayText(text);
  if(!clean||window._showThinking===false) return false;
  const row=_liveAnchorReasoningRowForFallback(turn, opts);
  if(!row) return false;
  if(row.classList&&row.classList.contains('wl-reason')){
    if(typeof _renderWorklogReasonInto==='function') _renderWorklogReasonInto(row, clean);
    else row.textContent=clean;
    const group=row.closest&&row.closest('.tool-worklog-group,.tool-call-group,.live-worklog');
    if(group&&typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(group);
  }else if(row.classList&&row.classList.contains('transparent-event-row')){
    _renderThinkingInto(row, clean);
    const eventAt=row.getAttribute&&row.getAttribute('data-event-at');
    const nextTs=typeof _firstValidTimestampSeconds==='function'
      ? _firstValidTimestampSeconds(opts&&opts.ts, opts&&opts.timestamp, opts&&opts.created_at, eventAt)
      : null;
    if(typeof _decorateTransparentEventRow==='function'){
      _decorateTransparentEventRow(row,{
        type:'thinking',
        text:clean,
        preview:clean,
        ts:nextTs||undefined,
        live:true,
        segmentSeq:opts&&opts.segmentSeq,
        burstId:opts&&opts.burstId,
      });
    }
  }else{
    _renderThinkingInto(row, clean);
  }
  if(turn&&typeof _syncTransparentEventControls==='function') _syncTransparentEventControls(turn);
  if(typeof scrollIfPinned==='function') scrollIfPinned();
  return true;
}

window.HermesUI.register('transparentWorklog', {
  ensureLiveWorklogContainer,
  ensureRunActivityGroup,
});
