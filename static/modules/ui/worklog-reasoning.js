import { _sanitizeThinkingDisplayText } from './activity-and-scroll.js';
import { renderMd } from './markdown-renderer.js';
import { S, esc } from './state.js';
import { _syncToolCallGroupSummary, _toolWorklogListEl } from './worklog-tool-groups.js';
import { _activityKeyForLiveTurn } from './worklog-disclosure.js';

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

export {
  _worklogReasonHtmlFromAnchor,
  _worklogReasonHtmlFromText,
  _renderWorklogReasonInto,
  _worklogReasonNodeFromText,
  _worklogReasonAnchorKey,
  _syncWorklogReasonFromAnchor,
  ensureLiveWorklogContainer,
  _migrateLegacyLiveActivityGroupsToWorklog,
  _appendWorklogReason,
  _worklogAnchorKeySeq,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _worklogReasonHtmlFromAnchor: { enumerable: true, get: () => _worklogReasonHtmlFromAnchor, set: (value) => { _worklogReasonHtmlFromAnchor = value; } },
  _worklogReasonHtmlFromText: { enumerable: true, get: () => _worklogReasonHtmlFromText, set: (value) => { _worklogReasonHtmlFromText = value; } },
  _renderWorklogReasonInto: { enumerable: true, get: () => _renderWorklogReasonInto, set: (value) => { _renderWorklogReasonInto = value; } },
  _worklogReasonNodeFromText: { enumerable: true, get: () => _worklogReasonNodeFromText, set: (value) => { _worklogReasonNodeFromText = value; } },
  _worklogReasonAnchorKey: { enumerable: true, get: () => _worklogReasonAnchorKey, set: (value) => { _worklogReasonAnchorKey = value; } },
  _syncWorklogReasonFromAnchor: { enumerable: true, get: () => _syncWorklogReasonFromAnchor, set: (value) => { _syncWorklogReasonFromAnchor = value; } },
  ensureLiveWorklogContainer: { enumerable: true, get: () => ensureLiveWorklogContainer, set: (value) => { ensureLiveWorklogContainer = value; } },
  _migrateLegacyLiveActivityGroupsToWorklog: { enumerable: true, get: () => _migrateLegacyLiveActivityGroupsToWorklog, set: (value) => { _migrateLegacyLiveActivityGroupsToWorklog = value; } },
  _appendWorklogReason: { enumerable: true, get: () => _appendWorklogReason, set: (value) => { _appendWorklogReason = value; } },
  _worklogAnchorKeySeq: { enumerable: true, get: () => _worklogAnchorKeySeq, set: (value) => { _worklogAnchorKeySeq = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
