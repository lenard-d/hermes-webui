import { _activityElapsedLabel, _activityLastObservedAge, _activityProcessedElapsedLabel, _activitySettledProcessedLabel, _formatActiveElapsedTimer, _formatTurnDuration } from './composer-controls.js';
import { _worklogDetailsExpandedDefault } from './activity-presentation.js';
import { esc } from './state.js';
import { _isMemorySave, _isSkillUpdate, _toolKindIcon, _toolWorklogSummary } from './tool-call-presentation.js';

function _toolWorklogListEl(group){
  if(!group) return null;
  return group.querySelector('.tool-worklog-list') || group.querySelector('.activity-body') || group.querySelector('.tool-call-group-body');
}
function _toolWorklogToolsEl(group){
  const list=_toolWorklogListEl(group);
  if(!list) return null;
  let tools=list.querySelector(':scope > .wl-step-tools[data-worklog-tools="1"]');
  if(!tools){
    tools=document.createElement('div');
    tools.className='wl-step-tools tool-worklog-tools';
    tools.setAttribute('data-worklog-tools','1');
    list.appendChild(tools);
  }
  return tools;
}
function _liveToolStepEl(group){
  const list=_toolWorklogListEl(group);
  if(!list) return null;
  const last=list.lastElementChild;
  if(last&&last.classList&&last.classList.contains('wl-step-tools')&&last.getAttribute('data-worklog-tools')==='1') return last;
  const tools=document.createElement('div');
  tools.className='wl-step-tools tool-worklog-tools';
  tools.setAttribute('data-worklog-tools','1');
  list.appendChild(tools);
  return tools;
}
function _directWorklogToolRows(list){
  if(!list) return [];
  const rows=[];
  Array.from(list.children).forEach(child=>{
    if(child.classList&&child.classList.contains('tool-card-row')) rows.push(child);
    else if(child.classList&&(child.classList.contains('tool-worklog-tool-group')||child.classList.contains('tool-group'))) rows.push(...Array.from(child.querySelectorAll('.tool-card-row')));
  });
  return rows;
}
function _unwrapNestedToolGroups(tools){
  if(!tools) return;
  tools.querySelectorAll(':scope > .tool-worklog-tool-group,:scope > .tool-group').forEach(el=>el.remove());
}
function _toolGroupPrimaryKind(rows){
  const counts=Object.create(null);
  Array.from(rows||[]).forEach(row=>{
    const kind=row&&row.dataset&&row.dataset.toolKind?row.dataset.toolKind:'unknown';
    counts[kind]=(counts[kind]||0)+1;
  });
  const order=['search','shell','read','write','skill','memory','web','list','delegate','unknown'];
  for(const kind of order){
    if(counts[kind]) return kind;
  }
  return 'unknown';
}
function _toolGroupIcon(rows){
  return _toolKindIcon(_toolGroupPrimaryKind(rows));
}
function _syncToolRowsContainer(tools, isLiveWorklog){
  if(!tools) return;
  const existingGroup=tools.querySelector(':scope > .tool-worklog-tool-group,:scope > .tool-group[data-tool-worklog-tool-group="1"]');
  const wasOpen=!!(existingGroup&&existingGroup.classList&&existingGroup.classList.contains('open'));
  const rows=_directWorklogToolRows(tools);
  _unwrapNestedToolGroups(tools);
  rows.forEach(row=>{ if(row.parentElement) row.remove(); });
  tools.querySelectorAll(':scope > .tool-card-row').forEach(row=>row.remove());
  const shouldGroup=tools.classList.contains('wl-step-tools') && rows.length>1;
  if(!shouldGroup){
    rows.forEach(row=>tools.appendChild(row));
    return;
  }
  const shouldOpen=wasOpen||_worklogDetailsExpandedDefault();
  const group=document.createElement('div');
  group.className='tool-group'+(shouldOpen?' open':' tool-worklog-tool-group-collapsed');
  group.setAttribute('data-tool-worklog-tool-group','1');
  let groupKey='group';
  if(tools.parentElement){
    const steps=Array.from(tools.parentElement.children).filter(child=>child.classList&&child.classList.contains('wl-step-tools')&&child.getAttribute('data-worklog-tools')==='1');
    const stepIdx=steps.indexOf(tools);
    if(stepIdx>=0) groupKey=`step:${stepIdx}`;
  }
  group.setAttribute('data-tool-group-disclosure-key',groupKey);
  const summary=_toolWorklogSummary(rows,{live:isLiveWorklog, toolCount:rows.length});
  group.innerHTML=`<button type="button" class="tool-group-head tool-worklog-tool-group-head" aria-expanded="${shouldOpen?'true':'false'}" onclick="_toggleToolWorklogGroup(this)"><span class="tool-worklog-tool-group-icon tg-icon">${_toolGroupIcon(rows)}</span><span class="tg-sum tool-worklog-tool-group-label">${esc(summary)}</span><span class="tool-call-group-chevron tg-caret">${li('chevron-right',12)}</span></button><div class="tool-group-body tool-worklog-tool-group-body"><div class="tg-rows tool-worklog-tool-group-rows"></div></div>`;
  const body=group.querySelector('.tg-rows');
  rows.forEach(row=>body.appendChild(row));
  tools.appendChild(group);
}
function _syncToolWorklogToolGroup(group){
  const list=_toolWorklogListEl(group);
  if(!list) return;
  const isLiveWorklog=!!(group.getAttribute('data-live-tool-worklog-group')==='1' || group.getAttribute('data-live-tool-call-group')==='1');
  const steps=Array.from(list.querySelectorAll(':scope > .wl-step-tools[data-worklog-tools="1"]'));
  if(!steps.length){
    const pendingRows=_directWorklogToolRows(list);
    if(!pendingRows.length) return;
    const tools=_toolWorklogToolsEl(group);
    if(!tools) return;
    pendingRows.forEach(row=>tools.appendChild(row));
    _syncToolRowsContainer(tools,isLiveWorklog);
    return;
  }
  steps.forEach(tools=>_syncToolRowsContainer(tools,isLiveWorklog));
}

function _syncToolCallGroupSummary(group){
  if(!group) return;
  if(group.getAttribute('data-tool-worklog-group')==='1') _syncToolWorklogToolGroup(group);
  const cards=Array.from((_toolWorklogListEl(group)||group).querySelectorAll('.tool-card-row .tool-card,.tool-card-row.tl'));
  const toolCount=cards.length;
  const label=group.querySelector('.tool-worklog-label') || group.querySelector('.tool-call-group-label');
  const isWorklogGroup=!!(group.getAttribute('data-tool-worklog-group')==='1');
  const isLiveWorklog=!!(group.getAttribute('data-live-tool-worklog-group')==='1' || group.getAttribute('data-live-tool-call-group')==='1');
  const hasRunningTool=cards.some(card=>card.classList.contains('tool-card-running'));
  if(isWorklogGroup){
    if(hasRunningTool) group.setAttribute('data-tool-worklog-running','1');
    else group.removeAttribute('data-tool-worklog-running');
  }
  const durationEl=group.querySelector('.tool-call-group-duration');
  if(label){
    if(group.getAttribute('data-run-activity-group')==='1'){
      label.textContent=toolCount?_toolWorklogSummary(cards,{live:isLiveWorklog, toolCount}):'Running';
    }else if(isWorklogGroup){
      const processedLabel=isLiveWorklog
        ? _activityProcessedElapsedLabel(group)
        : _activitySettledProcessedLabel(group);
      label.textContent=processedLabel||t('processed_elapsed','');
    }else{
      const rows=Array.from(group.querySelectorAll('.tool-card-row'));
      // Prefer the live _tcData classification; fall back to the durable data-*
      // flags for rows restored from an HTML snapshot (which drops JS properties).
      const isMem=r=>_isMemorySave(r._tcData)||r.getAttribute('data-memory-save')==='1';
      const isSkill=r=>_isSkillUpdate(r._tcData)||r.getAttribute('data-skill-update')==='1';
      const memCount=rows.filter(isMem).length;
      const skillCount=rows.filter(r=>!isMem(r)&&isSkill(r)).length;
      const otherCount=Math.max(0, toolCount-memCount-skillCount);
      let suffix='';
      if(memCount) suffix+=`, ${memCount} ${memCount===1?'memory':'memories'} saved`;
      if(skillCount) suffix+=`, ${skillCount} ${skillCount===1?'skill':'skills'} updated`;
      const toolsPart=otherCount?`${otherCount} tool${otherCount===1?'':'s'}`:'';
      if(group.getAttribute('data-live-tool-call-group')==='1'){
        if(toolsPart) label.textContent=`Activity: ${toolsPart}${suffix}`;
        else if(suffix) label.textContent=`Activity: ${suffix.slice(2)}`;
        else label.textContent='Running';
      }else if(toolsPart||suffix){
        label.textContent=toolsPart?`Activity: ${toolsPart}${suffix}`:`Activity: ${suffix.slice(2)}`;
      }else label.textContent='Activity';
    }
    label.setAttribute('data-sweep-label', label.textContent);
  }
  if(durationEl){
    if(group.getAttribute('data-run-activity-group')==='1'){
      const durationText=_formatTurnDuration(group.dataset.turnDuration);
      const label=durationText?'':_activityElapsedLabel(group);
      durationEl.textContent=durationText?` Done in ${durationText}`:(label?` Working for ${label}`:'');
      durationEl.style.display=durationEl.textContent?'':'none';
    }else if(group.getAttribute('data-live-tool-call-group')==='1'){
      const activeText=_activityElapsedLabel(group);
      if(activeText) group.setAttribute('data-active-turn-elapsed',activeText);
      else group.removeAttribute('data-active-turn-elapsed');
      durationEl.textContent='';
      durationEl.style.display='none';
    }else if(isWorklogGroup){
      durationEl.textContent='';
      durationEl.style.display='none';
    }else{
      const durationText=_formatTurnDuration(group.dataset.turnDuration);
      durationEl.textContent=durationText?` Done in ${durationText}`:'';
      durationEl.style.display=durationText?'':'none';
    }
  }
}

function _activityProgressLabelForToolName(name){
  const key=String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,'_');
  if(!key) return 'Working';
  if(key.includes('search')||key.includes('grep')) return 'Searching workspace';
  if(key.includes('read')||key.includes('view')||key.includes('open')) return 'Reading files';
  if(key.includes('write')||key.includes('patch')||key.includes('edit')) return 'Updating files';
  if(key.includes('terminal')||key.includes('shell')||key.includes('command')||key.includes('process')) return 'Running command';
  if(key.includes('web')||key.includes('fetch')||key.includes('curl')) return 'Checking web data';
  if(key.includes('todo')||key.includes('plan')) return 'Planning next steps';
  return 'Working';
}

function _toolCardVisibleNameText(nameEl){
  if(!nameEl) return '';
  const specific=nameEl.querySelector&&nameEl.querySelector('.tool-card-name-label');
  const generic=nameEl.querySelector&&nameEl.querySelector('.tool-card-name-generic');
  if(specific&&generic){
    const card=nameEl.closest&&nameEl.closest('.tool-card');
    const preferred=(card&&card.classList&&card.classList.contains('open'))?generic:specific;
    return String(preferred.textContent||'').trim();
  }
  return String(nameEl.textContent||'').trim();
}

function _activityLatestToolName(group){
  if(!group) return '';
  const running=group.querySelector('.tool-card.tool-card-running .tool-card-name');
  const latest=running || Array.from(group.querySelectorAll('.tool-card-name')).pop();
  return _toolCardVisibleNameText(latest);
}

function _activityWaitingDetail(group,label=''){
  const toolName=_activityLatestToolName(group);
  if(toolName){
    const action=_activityProgressLabelForToolName(toolName);
    if(group&&group.querySelector('.tool-card.tool-card-running')) return `${action}: ${toolName}. Results will appear here.`;
    return `Last step: ${action} (${toolName}); now choosing the next action or composing a response.`;
  }
  if(String(label||'').toLowerCase().includes('model')) return 'Reviewing the prompt and context, then choosing the next action or composing the response.';
  return 'The agent is running; tool results and response text will appear here.';
}

function _activityLiveProgressLabel(group){
  if(!group||group.getAttribute('data-live-tool-call-group')!=='1') return '';
  const idleAge=_activityLastObservedAge(group);
  if(idleAge!==null&&idleAge>=90) return `No recent activity for ${_formatActiveElapsedTimer(idleAge)}`;
  const running=group.querySelector('.tool-card.tool-card-running .tool-card-name');
  const latest=running?_toolCardVisibleNameText(running):_activityLatestToolName(group);
  const waiting=group.querySelector('.agent-activity-status-waiting .agent-activity-status-label');
  if(latest) return _activityProgressLabelForToolName(latest);
  if(waiting&&waiting.textContent&&String(waiting.textContent).toLowerCase().includes('model')) return 'Reviewing prompt and context';
  if(waiting&&waiting.textContent) return waiting.textContent;
  return 'Starting agent';
}

export {
  _toolWorklogListEl,
  _toolWorklogToolsEl,
  _liveToolStepEl,
  _directWorklogToolRows,
  _unwrapNestedToolGroups,
  _toolGroupPrimaryKind,
  _toolGroupIcon,
  _syncToolRowsContainer,
  _syncToolWorklogToolGroup,
  _syncToolCallGroupSummary,
  _activityProgressLabelForToolName,
  _toolCardVisibleNameText,
  _activityLatestToolName,
  _activityWaitingDetail,
  _activityLiveProgressLabel,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _toolWorklogListEl: { enumerable: true, get: () => _toolWorklogListEl, set: (value) => { _toolWorklogListEl = value; } },
  _toolWorklogToolsEl: { enumerable: true, get: () => _toolWorklogToolsEl, set: (value) => { _toolWorklogToolsEl = value; } },
  _liveToolStepEl: { enumerable: true, get: () => _liveToolStepEl, set: (value) => { _liveToolStepEl = value; } },
  _directWorklogToolRows: { enumerable: true, get: () => _directWorklogToolRows, set: (value) => { _directWorklogToolRows = value; } },
  _unwrapNestedToolGroups: { enumerable: true, get: () => _unwrapNestedToolGroups, set: (value) => { _unwrapNestedToolGroups = value; } },
  _toolGroupPrimaryKind: { enumerable: true, get: () => _toolGroupPrimaryKind, set: (value) => { _toolGroupPrimaryKind = value; } },
  _toolGroupIcon: { enumerable: true, get: () => _toolGroupIcon, set: (value) => { _toolGroupIcon = value; } },
  _syncToolRowsContainer: { enumerable: true, get: () => _syncToolRowsContainer, set: (value) => { _syncToolRowsContainer = value; } },
  _syncToolWorklogToolGroup: { enumerable: true, get: () => _syncToolWorklogToolGroup, set: (value) => { _syncToolWorklogToolGroup = value; } },
  _syncToolCallGroupSummary: { enumerable: true, get: () => _syncToolCallGroupSummary, set: (value) => { _syncToolCallGroupSummary = value; } },
  _activityProgressLabelForToolName: { enumerable: true, get: () => _activityProgressLabelForToolName, set: (value) => { _activityProgressLabelForToolName = value; } },
  _toolCardVisibleNameText: { enumerable: true, get: () => _toolCardVisibleNameText, set: (value) => { _toolCardVisibleNameText = value; } },
  _activityLatestToolName: { enumerable: true, get: () => _activityLatestToolName, set: (value) => { _activityLatestToolName = value; } },
  _activityWaitingDetail: { enumerable: true, get: () => _activityWaitingDetail, set: (value) => { _activityWaitingDetail = value; } },
  _activityLiveProgressLabel: { enumerable: true, get: () => _activityLiveProgressLabel, set: (value) => { _activityLiveProgressLabel = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
