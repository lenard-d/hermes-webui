function _toolDisplayName(tc){
  const name=(tc&&tc.name)||'tool';
  if(name==='subagent_progress') return 'Subagent';
  if(name==='delegate_task') return 'Delegate task';
  if(name==='skill_view') return 'Skill';
  if(name==='skill_manage') return 'Skill';
  return name;
}

// Activity-summary detection for persisted memory/skill writes (#3340, #3544).
// Action vocabularies match the real agent tool enums:
//   memory.action      = add | replace | remove   (add/replace persist content → "saved")
//   skill_manage.action= create | patch | edit | delete | write_file | remove_file
//                        (create/patch/edit/write_file mutate a skill → "updated")
// Deletions (memory 'remove', skill 'delete'/'remove_file') are intentionally
// excluded so the "saved"/"updated" label verbs stay accurate; running/errored
// calls are excluded so only completed writes are counted.
const _MEMORY_SAVE_ACTIONS=new Set(['add','replace']);
const _SKILL_UPDATE_ACTIONS=new Set(['create','patch','edit','write_file']);
function _tcAction(tc){
  return String((tc&&tc.args&&tc.args.action)||'').toLowerCase();
}
function _isMemorySave(tc){
  if(!tc||tc.name!=='memory'||tc.done===false||tc.is_error) return false;
  return _MEMORY_SAVE_ACTIONS.has(_tcAction(tc));
}
function _isSkillUpdate(tc){
  if(!tc||tc.name!=='skill_manage'||tc.done===false||tc.is_error) return false;
  return _SKILL_UPDATE_ACTIONS.has(_tcAction(tc));
}
// ── Tool action label helpers ──────────────────────────────────────────────
function _decodeToolLabelEntities(value){
  return String(value||'')
    .replace(/&quot;/g,'"')
    .replace(/&#39;|&apos;/g,"'")
    .replace(/&lt;/g,'<')
    .replace(/&gt;/g,'>')
    .replace(/&amp;/g,'&');
}
function _redactToolTargetLabel(value){
  return String(value||'')
    .replace(/\bsshpass\s+-p\s+(?:"[^"]*"|'[^']*'|\S+)/gi,'sshpass -p "[redacted]"')
    .replace(/(--password(?:=|\s+))(?:"[^"]*"|'[^']*'|\S+)/gi,'$1[redacted]')
    .replace(/(password(?:=|\s+))(?:"[^"]*"|'[^']*'|\S+)/gi,'$1[redacted]')
    // Env-assignment / flag secrets, masked across the full (multi-line) text so
    // the expanded shell card can't leak a key on a non-first line (#4926). Keys
    // matched case-insensitively: *(TOKEN|API_KEY|APIKEY|SECRET|PASSWD|PASSWORD|
    // ACCESS_KEY|PRIVATE_KEY|AUTH|CREDENTIAL|SESSION_KEY|CLIENT_SECRET)*.
    .replace(/(^|[\s;|(])([A-Za-z0-9_]*(?:TOKEN|API[_-]?KEY|SECRET|PASSWD|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIALS?|CLIENT[_-]?SECRET|SESSION[_-]?KEY)[A-Za-z0-9_]*\s*=\s*)(?:"[^"]*"|'[^']*'|\S+)/gi,'$1$2[redacted]')
    // AUTH-family env assignment, but only the `=` form (the `Authorization:`
    // header colon form is handled separately below, and must not be eaten here).
    .replace(/(^|[\s;|(])([A-Za-z0-9_]*AUTH[A-Za-z0-9_]*\s*=\s*)(?:"[^"]*"|'[^']*'|\S+)/gi,'$1$2[redacted]')
    // --token / --api-key / --secret style flags.
    .replace(/(--(?:token|api[_-]?key|secret|access[_-]?key|client[_-]?secret|auth[_-]?token)(?:=|\s+))(?:"[^"]*"|'[^']*'|\S+)/gi,'$1[redacted]')
    // Authorization: Bearer/Bot/Token <token> (header or curl -H form):
    // redact everything after the scheme keyword up to the closing quote/space.
    .replace(/(authorization\s*:?\s*(?:bearer|bot|token)\s+)(?:"[^"]*"|'[^']*'|[^\s'"]+)/gi,'$1[redacted]')
    .replace(/((?:authorization|x-api-key)\s*:\s+)(?:"[^"]*"|'[^']*'|[^\s'"]{12,})/gi,'$1[redacted]')
    // Secret-looking URL query params (?token=... &api_key=... &access_token=...).
    .replace(/([?&](?:token|api[_-]?key|access[_-]?token|secret|sig|signature|key)=)(?:[^&\s"']+)/gi,'$1[redacted]');
}
function _shortToolLabel(value, limit){
  const text=String(value||'').replace(/\s+/g,' ').trim();
  const max=limit||112;
  if(text.length<=max) return text;
  const head=Math.max(24, Math.floor(max*.68));
  const tail=Math.max(12, max-head-3);
  return text.slice(0,head).trimEnd()+'...'+text.slice(-tail).trimStart();
}
function _toolI18n(key, fallback){
  const args=Array.prototype.slice.call(arguments,2);
  if(typeof t==='function'){
    const value=t.apply(null,[key].concat(args));
    if(value&&value!==key) return value;
  }
  return typeof fallback==='function'?fallback.apply(null,args):String(fallback||'');
}
function _toolPathBasename(value){
  const text=String(value||'').trim();
  if(!text) return '';
  const normalized=text.replace(/[\\/]+$/,'');
  const parts=normalized.split(/[\\/]+/);
  return parts.pop()||normalized;
}
function _toolActionKind(tc){
  const n=String(tc&&tc.name||'').toLowerCase().replace(/[^a-z0-9]+/g,'_');
  if(!n) return 'unknown';
  if(n==='subagent_progress'||n==='delegate_task') return 'delegate';
  if(n.includes('skill')) return 'skill';
  if(n.includes('memory')) return 'memory';
  if(n.includes('terminal')||n.includes('shell')||n.includes('command')||n.includes('process')||n==='execute_code') return 'shell';
  if(n.includes('read')||n.includes('view')||n.includes('open')||n==='vision_analyze') return 'read';
  if(n.includes('list')||n==='todo') return 'list';
  if(n.includes('web')||n.includes('fetch')||n.includes('curl')||n.includes('extract')||n.includes('browse')||n.includes('navigate')) return 'web';
  if(n.includes('search')||n.includes('grep')||n.includes('find')) return 'search';
  if(n.includes('write')||n.includes('patch')||n.includes('edit')) return 'write';
  return 'unknown';
}
function _toolKindIcon(kind){
  const icons={
    shell:'terminal',
    read:'file-text',
    list:'list',
    search:'search',
    web:'globe',
    write:'file-pen',
    skill:'book-open',
    memory:'brain',
    delegate:'bot',
    unknown:'wrench',
  };
  return li(icons[kind]||icons.unknown,14);
}
function _toolTargetLabel(tc){
  const a=tc&&tc.args||{};
  const kind=_toolActionKind(tc);
  let raw='';
  if(kind==='shell') raw=a.cmd||a.command||tc.command||tc.raw_command||tc.original_command||tc.display_command||'';
  else if(kind==='skill') raw=a.name||a.skill||'';
  else if(kind==='memory') raw=a.target||a.name||a.action||'';
  else if(kind==='read'||kind==='write') raw=a.path||a.file_path||a.file||a.target||a.name||'';
  else if(kind==='search'||kind==='web') raw=a.query||a.pattern||a.url||a.uri||'';
  else raw=a.cmd||a.command||a.path||a.file_path||a.file||a.uri||a.url||a.query||a.pattern||a.dir||a.task||a.name||'';
  return _redactToolTargetLabel(_decodeToolLabelEntities(String(raw).split('\n')[0].trim()));
}
function _toolReadRangeLabel(tc){
  const name=String(tc&&tc.name||'').toLowerCase().replace(/[^a-z0-9]+/g,'_');
  if(name!=='read_file') return '';
  const args=tc&&tc.args||{};
  const offset=args.offset;
  if(!Number.isSafeInteger(offset)||offset<=0) return '';
  const limit=args.limit;
  if(limit===undefined) return `L${offset}`;
  if(!Number.isSafeInteger(limit)||limit<=0) return '';
  if(limit===1) return `L${offset}`;
  const span=limit-1;
  if(offset>Number.MAX_SAFE_INTEGER-span) return '';
  return `L${offset}-${offset+span}`;
}
function _toolFullCommandLabel(tc){
  // Full (multi-line) shell command for the EXPANDED detail lead. Mirrors the
  // shell raw-extraction in _toolTargetLabel but WITHOUT the .split('\n')[0]
  // first-line collapse, so a multi-line script shows every line when the card
  // is expanded (#4926). Redaction + entity-decode still applied to the whole.
  const a=tc&&tc.args||{};
  const raw=a.cmd||a.command||tc.command||tc.raw_command||tc.original_command||tc.display_command||'';
  return _redactToolTargetLabel(_decodeToolLabelEntities(String(raw).replace(/\s+$/,'')));
}
function _toolVisibleTargetLabel(tc, opts){
  opts=opts||{};
  const target=_toolTargetLabel(tc);
  if(!target) return '';
  const kind=_toolActionKind(tc);
  if(kind==='read'||kind==='write'){
    let text=_toolPathBasename(target)||target;
    const range=kind==='read'?_toolReadRangeLabel(tc):'';
    if(range) text=opts.rangeFirst?`${range} · ${text}`:`${text} · ${range}`;
    return _shortToolLabel(text, opts.limit||112);
  }
  if(kind==='skill'){
    const suffix=_toolI18n('tool_target_skill_suffix', 'skill');
    const text=target.toLowerCase().endsWith(String(suffix).toLowerCase())?target:`${target} ${suffix}`;
    return _shortToolLabel(text, opts.limit||112);
  }
  return _shortToolLabel(target, opts.limit||112);
}
function _toolCommandTitle(command){
  const normalized=String(command||'').replace(/\s+/g,' ').trim();
  if(!normalized) return '';
  if(/^git\s+fetch\b/i.test(normalized)) return 'git fetch';
  if(/^git\s+(?:status|rev-list|branch)\b/i.test(normalized)) return 'git ahead/behind';
  if(/^git\s+log\b/i.test(normalized)) return 'git log';
  if(/\bcurl\b/i.test(normalized)&&/\/health\b/i.test(normalized)) return 'health check';
  if(/\b(?:ps|pgrep)\b/i.test(normalized)) return 'process check';
  const m=normalized.match(/\blsof\b.*(?:-i|:)(\d{2,5})\b/i);
  if(m) return `port ${m[1]} check`;
  if(/\blaunchctl\b/i.test(normalized)) return 'launchctl';
  return _shortToolLabel(normalized,72);
}
function _toolQueryTitle(query){
  const normalized=String(query||'').replace(/\s+/g,' ').trim();
  return _shortToolLabel(normalized,72);
}
function _toolActionLabelText(tc, opts){
  opts=opts||{};
  const kind=_toolActionKind(tc);
  const done=tc&&tc.done!==false;
  const isErr=tc&&tc.is_error;
  const state=done?'done':'running';
  let target=opts.generic?'':_toolVisibleTargetLabel(tc, opts);
  if((kind==='search'||kind==='web')&&target) target=_toolQueryTitle(target);
  const display=_toolDisplayName(tc);
  return _toolI18n('tool_action_label',(k,s,tgt,disp,err)=>{
    const verbs={
      shell:{running:'Running',done:'Ran',fallback:'a command'},
      read:{running:'Reading',done:'Read',fallback:'a file'},
      list:{running:'Listing',done:'Listed',fallback:'files'},
      search:{running:'Searching for',done:'Searched for',fallback:'workspace'},
      web:{running:'Checking',done:'Checked',fallback:'web data'},
      write:{running:'Updating',done:'Updated',fallback:'a file'},
      skill:{running:'Loading',done:'Loaded',fallback:'a skill'},
      memory:{running:'Saving',done:'Saved',fallback:'memory'},
      delegate:{running:'Delegating',done:'Delegated',fallback:'a task'},
      unknown:{running:'Running',done:'Ran',fallback:disp||'a tool'},
    };
    const v=verbs[k]||verbs.unknown;
    const verb=v[s]||v.running;
    const object=tgt||v.fallback||disp||'tool';
    if(err) return `Failed ${String(v.running||verb).toLowerCase()} ${object}`;
    return `${verb} ${object}`;
  },kind,state,target,display,isErr);
}
function _toolActionLabel(tc){
  return esc(_toolActionLabelText(tc,{limit:112}));
}
const _toolWorklogSummaries={shell:{},read:{},list:{},search:{},web:{},write:{},skill:{},memory:{},delegate:{},unknown:{}};
function _toolWorklogSummaryLine(kind, state, count){
  const n=Math.max(1,Number(count)||1);
  return _toolI18n('tool_worklog_summary',(k,s,c)=>{
    const forms={
      shell:{running:['Running a command','Running {n} commands'],done:['Ran a command','Ran {n} commands']},
      read:{running:['Reading a file','Reading {n} files'],done:['Read a file','Read {n} files']},
      list:{running:['Listing files','Listing {n} items'],done:['Listed files','Listed {n} files']},
      search:{running:['Searching workspace','Searching workspace {n} times'],done:['Searched workspace','Searched workspace {n} times']},
      web:{running:['Checking web','Checking web {n} times'],done:['Checked the web','Checked the web {n} times']},
      write:{running:['Updating a file','Updating {n} files'],done:['Updated a file','Updated {n} files']},
      skill:{running:['Loading a skill','Loading {n} skills'],done:['Loaded a skill','Loaded {n} skills']},
      memory:{running:['Saving memory','Saving {n} memory updates'],done:['Saved memory','Saved {n} memory updates']},
      delegate:{running:['Delegating a task','Delegating {n} tasks'],done:['Delegated a task','Delegated {n} tasks']},
      unknown:{running:['Running a tool','Running {n} tools'],done:['Ran a tool','Ran {n} tools']},
    };
    const pair=((forms[k]||forms.unknown)[s]||forms.unknown.running);
    return (c===1?pair[0]:pair[1]).replace('{n}',String(c));
  },kind,state,n);
}
function _toolWorklogJoin(lines){
  const parts=Array.from(lines||[]).filter(Boolean);
  if(parts.length<=1) return parts[0]||'';
  return _toolI18n('tool_summary_join',(items)=>items.join(', '),parts);
}
function _toolWorklogActionParts(tc){
  if(tc&&tc.nodeType===1){
    const row=tc.classList&&tc.classList.contains('tool-card-row')?tc:tc.closest&&tc.closest('.tool-card-row');
    const card=tc.classList&&tc.classList.contains('tool-card')?tc:(row&&row.querySelector('.tool-card'));
    const actionLabel=(row&&row.dataset.toolActionLabel)||(card&&card.querySelector('.tool-card-name')&&card.querySelector('.tool-card-name').textContent.trim())||'';
    const kind=(row&&row.dataset.toolKind)||'unknown';
    const isDone=!((row&&row.dataset.toolDone)==='false'||(card&&card.classList.contains('tool-card-running')));
    const isErr=(row&&row.dataset.toolError)==='true'||(card&&card.classList.contains('tool-card-error'));
    return {kind,isDone,isErr,target:'',actionLabel};
  }
  const kind=_toolActionKind(tc);
  return {
    kind,
    isDone:tc&&tc.done!==false,
    isErr:tc&&tc.is_error,
    target:_toolTargetLabel(tc),
    actionLabel:_toolActionLabelText(tc),
  };
}
function _toolWorklogSummary(toolCalls, opts){
  const cards=Array.from(toolCalls||[]).filter(tc=>tc);
  if(!cards.length) return (opts&&opts.live)?'Running':'Worklog';
  if(cards.length===1){
    const part=_toolWorklogActionParts(cards[0]);
    const line=_toolWorklogSummaryLine(part.kind,part.isDone?'done':'running',1);
    return part.isErr?`${line}, 1 failed`:line;
  }
  const order=['shell','read','search','write','skill','memory','web','list','delegate','unknown'];
  const runningCounts={}, doneCounts={};
  let failed=0;
  for(const tc of cards){
    const part=_toolWorklogActionParts(tc);
    const counts=part.isDone?doneCounts:runningCounts;
    counts[part.kind]=(counts[part.kind]||0)+1;
    if(part.isErr) failed+=1;
  }
  const emit=(counts,state)=>{
    const out=[];
    for(const kind of order){
      const n=counts[kind]||0;
      if(!n) continue;
      out.push(_toolWorklogSummaryLine(kind,state,n));
    }
    return out;
  };
  const lines=[...emit(runningCounts,'running'),...emit(doneCounts,'done')];
  if(failed) lines.push(`${failed} failed`);
  return lines.length?_toolWorklogJoin(lines):_toolActionLabel(cards[0]);
}
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
function toolIcon(name){
  const raw=String(name||'');
  if(raw.startsWith('mcp__')||raw.startsWith('mcp.')) return li('plug');
  const icons={
    terminal:        li('terminal'),
    read_file:       li('file-text'),
    write_file:      li('file-pen'),
    search_files:    li('search'),
    web_search:      li('globe'),
    web_extract:     li('globe'),
    execute_code:    li('play'),
    patch:           li('wrench'),
    memory:          li('brain'),
    skill_view:      li('book-open'),
    skill_manage:    li('book-open'),
    todo:            li('list-todo'),
    cronjob:         li('clock'),
    delegate_task:   li('bot'),
    send_message:    li('message-square'),
    browser_navigate:li('globe'),
    vision_analyze:  li('eye'),
    subagent_progress:li('shuffle'),
  };
  return icons[name]||li('wrench');
}

function _toolArgPreviewValue(value){
  if(value===null||value===undefined) return '';
  if(Array.isArray(value)){
    if(!value.length) return '[]';
    if(value.length<=3&&value.every(v=>v===null||['string','number','boolean'].includes(typeof v))){
      return value.map(v=>String(v)).join(', ');
    }
    return `${value.length} items`;
  }
  if(typeof value==='object') return 'object';
  return String(value).replace(/\s+/g,' ').trim();
}
// Secret/sensitive-arg guard for collapsed tool-card previews. Exact-name hiding
// alone misses camelCase / variant spellings (apiKey, access_token, clientSecret,
// Authorization, …), so a normalized substring check runs first so secret-shaped
// argument names are never surfaced in the always-visible collapsed header (#3267).
function _toolArgPreviewKeyIsHidden(key){
  const k=String(key||'').toLowerCase().replace(/[^a-z0-9]/g,'');
  // verbose-but-not-secret bodies we keep out of the compact preview
  const verbose=['content','filecontent','newstring','oldstring','patch','text','message','prompt','code','script','cookies','headers'];
  if(verbose.includes(k)) return true;
  // secret-shaped substrings (covers api_key/apiKey, access_token/auth_token/bearer,
  // client_secret, password, credential, private_key, authorization, etc.)
  return /(apikey|token|secret|password|passwd|credential|authorization|\bauth\b|auth$|^auth|bearer|privatekey|accesskey|sessionkey|signingkey|cookie)/.test(k)
    || k==='auth' || k==='key' || k==='pat';
}
function _formatToolArgPreview(args){
  if(!args||typeof args!=='object') return '';
  const preferred=['path','file_path','target','pattern','query','url','urls','name','ref','command','action','mode','schedule','workdir'];
  const keys=[];
  for(const key of preferred){
    if(Object.prototype.hasOwnProperty.call(args,key)&&!_toolArgPreviewKeyIsHidden(key)) keys.push(key);
  }
  for(const key of Object.keys(args)){
    if(keys.length>=3) break;
    if(keys.includes(key)||_toolArgPreviewKeyIsHidden(key)) continue;
    keys.push(key);
  }
  const parts=[];
  for(const key of keys){
    const raw=_toolArgPreviewValue(args[key]);
    if(!raw) continue;
    const val=raw.length>96?`${raw.slice(0,93)}…`:raw;
    parts.push(`${key}=${val}`);
    if(parts.join(' · ').length>=150) break;
  }
  const out=parts.join(' · ');
  return out.length>180?`${out.slice(0,177)}…`:out;
}
function _toolResultOneLiner(preview){
  if(!preview) return '';
  const first=preview.split('\n').find(l=>l.trim())||'';
  const trimmed=first.trim();
  if(!trimmed) return '';
  if(trimmed[0]==='{') return '';
  if(trimmed[0]==='['){try{JSON.parse(trimmed);return '';}catch(e){/* not JSON */}}
  return trimmed.length>180?trimmed.slice(0,177)+'…':trimmed;
}
function _toolCardPreviewText(tc, displaySnippet){
  const explicitPreview=String(tc&&tc.preview||'').trim();
  if(tc&&tc.done===false&&explicitPreview) return explicitPreview;
  const resultSource=explicitPreview||String(tc&&tc.snippet||'').trim();
  const resultLine=_toolResultOneLiner(resultSource);
  if(tc&&tc.done!==false&&resultLine) return resultLine;
  const argPreview=_formatToolArgPreview(tc&&tc.args);
  if(argPreview) return argPreview;
  if(tc&&tc.done===false) return 'Running';
  if(tc&&tc.is_error) return 'Failed';
  return 'Completed';
}
function _toolCardAllowsDetail(kind, tc){
  const infoKinds={read:1,search:1,list:1,web:1};
  if(infoKinds[kind]&&!(tc&&tc.is_error)) return false;
  return true;
}
function _toolDetailLeadLabel(kind){
  if(kind==='shell') return 'Shell';
  if(kind==='write') return 'Target';
  return 'Input';
}
function _toolDetailLeadText(kind, tc){
  const target=_toolTargetLabel(tc);
  if(kind==='shell'){
    // Expanded card shows the FULL multi-line command, not just the header's
    // first line (#4926). Fall back to the first-line target if full is empty.
    const full=_toolFullCommandLabel(tc);
    const cmd=full||target;
    return cmd?`$ ${cmd}`:'';
  }
  if(!target) return '';
  return target;
}
function buildToolCard(tc){
  const row=document.createElement('div');
  row.className='tool-card-row';
  if(!row.dataset) row.dataset={};
  row.dataset.toolName=String(tc&&tc.name||'tool');
  const toolKind=typeof _toolActionKind==='function'?_toolActionKind(tc):'unknown';
  row.dataset.toolKind=toolKind;
  row.dataset.toolDone=String(tc&&tc.done!==false);
  row.dataset.toolError=String(!!(tc&&tc.is_error));
  row.dataset.toolActionLabel=typeof _toolActionLabelText==='function'?_toolActionLabelText(tc):_toolDisplayName(tc);
  const disclosureKey=typeof _toolDisclosureIdentity==='function'?_toolDisclosureIdentity(tc):'';
  if(disclosureKey) row.setAttribute('data-tool-disclosure-key', disclosureKey);
  const icon=toolIcon(tc.name);
  const hasRawDetail=!!(tc.snippet)||(tc.args&&Object.keys(tc.args).length>0);
  const allowsDetail=typeof _toolCardAllowsDetail==='function'?_toolCardAllowsDetail(toolKind,tc):true;
  const hasDetail=hasRawDetail&&allowsDetail;
  let displaySnippet='';
  if(tc.snippet){
    const s=tc.snippet;
    if(s.length<=800){displaySnippet=s;}
    else{
      const cutoff=s.slice(0,800);
      const lastBreak=Math.max(cutoff.lastIndexOf('. '),cutoff.lastIndexOf('\n'),cutoff.lastIndexOf('; '));
      displaySnippet=lastBreak>80?s.slice(0,lastBreak+1):cutoff;
    }
  }
  const hasMore=tc.snippet&&tc.snippet.length>displaySnippet.length;
  const moreLabel=tc.is_diff?'Show diff':'Show more';
  const lessLabel=tc.is_diff?'Hide diff':'Show less';
  const runIndicator=tc.done===false?'<span class="tool-card-running-dot"></span>':'';
  const isSubagent=tc.name==='subagent_progress';
  const isDelegation=tc.name==='delegate_task';
  const openClass='';
  const cardClass='tool-card'+(tc.done===false?' tool-card-running':'')+(isSubagent?' tool-card-subagent':'')+(hasDetail?'':' tool-card-no-detail')+openClass;
  const headerClick=hasDetail?' onclick="this.closest(\'.tool-card\').classList.toggle(\'open\')"':'';
  // Clean up legacy subagent prefixes since the Lucide icon already shows it
  let displayName=typeof _toolActionLabelText==='function'?_toolActionLabelText(tc,{limit:112}):_toolDisplayName(tc);
  let genericName=typeof _toolActionLabelText==='function'?_toolActionLabelText(tc,{generic:true,limit:112}):_toolDisplayName(tc);
  let previewText=_toolCardPreviewText(tc, displaySnippet);
  const argPreview=_formatToolArgPreview(tc&&tc.args);
  if(toolKind==='shell'||previewText===argPreview||previewText==='Completed'||previewText==='Running'||previewText==='Failed') previewText='';
  if(isSubagent) previewText=previewText.replace(/^(?:\u{1F500}|↳)\s*/u,'');
  const detailLeadText=hasDetail&&typeof _toolDetailLeadText==='function'?_toolDetailLeadText(toolKind,tc):'';
  const detailLeadLabel=typeof _toolDetailLeadLabel==='function'?_toolDetailLeadLabel(toolKind):(toolKind==='shell'?'Shell':'Input');
  const detailLead=detailLeadText?`<div class="tool-card-detail-lead"><div class="tool-card-detail-lead-label">${esc(detailLeadLabel)}</div><pre>${esc(detailLeadText)}</pre></div>`:'';
  const argsEntries=tc.args&&Object.keys(tc.args).length?Object.entries(tc.args):[];
  const visibleArgs=(detailLeadText&&toolKind==='shell')?[]:argsEntries;
  row.innerHTML=`
    <div class="${cardClass}">
      <div class="tool-card-header"${headerClick}>
        ${runIndicator}
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-name"><span class="tool-card-name-label">${esc(displayName)}</span><span class="tool-card-name-generic">${esc(genericName)}</span></span>
        <span class="tool-card-preview">${esc(previewText)}</span>
        ${hasDetail?`<span class="tool-card-toggle">${li('chevron-right',12)}</span>`:''}
      </div>
      ${hasDetail?`<div class="tool-card-detail">
        ${detailLead}
        ${visibleArgs.length?`<div class="tool-card-args">${
          visibleArgs.map(([k,v])=>{
            let sv=String(v);
            if(typeof _redactToolTargetLabel==='function'){ try{ sv=_redactToolTargetLabel(sv); }catch(e){} }
            return `<div class="tool-arg-pair"><span class="tool-arg-key">${esc(k)}</span><span class="tool-arg-val">${esc(sv)}</span></div>`;
          }).join('')
        }</div>`:''}
        ${displaySnippet?`<div class="tool-card-result">
          <pre>${tc.is_diff||_snippetLooksLikeDiff(displaySnippet)?`<code class="diff-block" data-highlighted="1">${_colorDiffLines(displaySnippet)}</code>`:esc(displaySnippet)}</pre>
          ${hasMore?`<button class="tool-card-more" data-full="${esc(tc.snippet||'').replace(/"/g,'&quot;')}" data-short="${esc(displaySnippet||'').replace(/"/g,'&quot;')}" data-is-diff="${tc.is_diff||_snippetLooksLikeDiff(displaySnippet)?1:0}" data-more-label="${esc(moreLabel)}" data-less-label="${esc(lessLabel)}" onclick="event.stopPropagation();_toggleToolDiff(this)">${esc(moreLabel)}</button>`:''}
        </div>`:''}
      </div>`:''}
    </div>`;
  row._tcData = tc;
  // Durable classification flags: _tcData (a JS property) does NOT survive the
  // outerHTML/innerHTML snapshot+restore the live tool-call group uses on session
  // switch/restore, which would make _syncToolCallGroupSummary re-count restored
  // memory/skill rows as generic tools and silently drop the suffix. Mirror the
  // classification onto data-* attributes so it survives serialization. (#3544)
  if(_isMemorySave(tc)){row.setAttribute('data-memory-save','1');row.removeAttribute('data-skill-update');}
  else if(_isSkillUpdate(tc)){row.setAttribute('data-skill-update','1');row.removeAttribute('data-memory-save');}
  else {row.removeAttribute('data-memory-save');row.removeAttribute('data-skill-update');}
  return row;
}

function _colorDiffLines(text){
  if(typeof text !== 'string') return esc(String(text||''));
  return esc(text).split('\n').map(line=>{
    if(line.startsWith('@@')) return `<span class="diff-line diff-hunk">${line}</span>`;
    if(line.startsWith('+')&&!line.startsWith('+++')) return `<span class="diff-line diff-plus">${line}</span>`;
    if(line.startsWith('-')&&!line.startsWith('---')) return `<span class="diff-line diff-minus">${line}</span>`;
    return `<span class="diff-line">${line}</span>`;
  }).join('\n');
}

// Detect if text looks like a unified diff (has @@ hunk headers and +/- lines).
function _snippetLooksLikeDiff(text){
  if(typeof text!=='string'||text.length<10) return false;
  if(!/^@@\s/.test(text)) return false;
  const lines=text.split('\n');
  let plusMinus=0;
  for(let i=0;i<lines.length&&i<50;i++){
    const l=lines[i];
    if(l.startsWith('+')||l.startsWith('-')) plusMinus++;
  }
  return plusMinus>=2;
}

function _toggleToolDiff(btn){
  const pre=btn.closest('.tool-card-result')?.querySelector('pre');
  if(!pre) return;
  const isDiff=btn.dataset.isDiff==='1';
  const expanded=btn.textContent===btn.dataset.moreLabel;
  const raw=expanded?btn.dataset.full:btn.dataset.short;
  if(isDiff){
    let code=pre.querySelector('code');
    if(!code){code=document.createElement('code');code.className='diff-block';pre.textContent='';pre.appendChild(code);}
    code.innerHTML=_colorDiffLines(raw);
  }else{
    pre.textContent=raw;
  }
  btn.textContent=expanded?btn.dataset.lessLabel:btn.dataset.moreLabel;
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

// ── Live tool card helpers (called during SSE streaming) ──
// Live cards are inserted INLINE inside #msgInner (tagged with data-live-tid)
// so the streaming layout matches the settled layout produced by renderMessages
// (user → thinking → tool cards → response). The legacy #liveToolCards
// sibling container is no longer used for placement — keeping the cards in the
// message column eliminates the visible "jump" users saw when renderMessages
// fired on the done event.
function appendLiveToolCard(tc){
  // Guard: ignore if session was switched. Prevents stale tool events from
  // a previous session's SSE stream from manipulating the new session's DOM.
  if(!S.session||!S.activeStreamId) return;
  const opts=arguments[1]||{};
  if(opts.sessionId&&S.session.session_id!==opts.sessionId) return;
  if(opts.streamId&&S.activeStreamId!==opts.streamId) return;
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return;
  if(isLiveAnchorActivitySceneOwner(opts.streamId||S.activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(opts.streamId||S.activeStreamId, opts.sessionId||S.session.session_id);
    return;
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;  // see #1366
    $('msgInner').appendChild(turn);
  }
  const inner=_assistantTurnBlocks(turn);
  if(!inner) return;
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  const children=Array.from(inner.children);
  const burstId=tc.activityBurstId!==undefined&&tc.activityBurstId!==null&&String(tc.activityBurstId)!=='0'?String(tc.activityBurstId):'';
  const segmentSeq=tc.activitySegmentSeq!==undefined&&tc.activitySegmentSeq!==null&&String(tc.activitySegmentSeq)!=='0'?String(tc.activitySegmentSeq):'';
  const segmentAnchor=segmentSeq?_findLiveAssistantAnchorForSegment(inner, segmentSeq):null;
  const burstAnchor=burstId?_findLatestVisibleLiveAssistantByBurst(inner, burstId):null;
  const anchor=segmentAnchor||burstAnchor||_findLatestVisibleLiveAssistant(inner)||children.filter(el=>el.matches('[data-live-assistant="1"]')).pop();
  const effectiveSegmentSeq=anchor&&anchor.getAttribute?anchor.getAttribute('data-live-segment-seq')||segmentSeq:segmentSeq;
  if(isTransparentStream()){
    const insertTransparentRow=(row)=>{
      const liveFooter=inner.querySelector('#liveRunStatus');
      if(liveFooter&&liveFooter.parentElement===inner){
        inner.insertBefore(row,liveFooter);
      }else{
        inner.appendChild(row);
      }
    };
    if(tid){
      const existing=inner.querySelector(`.transparent-event-row[data-live-tid="${CSS.escape(tid)}"],.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
      if(existing){
        const replacementTs=_transparentEventTimestampSeconds(existing,{toolCall:tc});
        const replacement=_decorateTransparentEventRow(buildToolCard(tc),{
          type:'tool',
          name:tc&&tc.name,
          status:_transparentToolStatus(tc),
          toolCall:tc,
          ts:replacementTs,
          live:true,
          segmentSeq:effectiveSegmentSeq,
          burstId,
        });
        replacement.dataset.liveTid=tid;
        // Preserve the user's expand state + detail tab across tool completion:
        // the running row is rebuilt fresh on toolComplete, which would otherwise
        // snap an expanded row shut and reset its Full/Output tab. The detail-mode
        // is preserved regardless of open state (a user who picked Output then
        // collapsed should still get Output on re-open). (Trifecta O-Bug2 + r2.)
        try{
          const _oldCard=existing.querySelector('.tool-card,.thinking-card');
          const _newCard=replacement.querySelector('.tool-card,.thinking-card');
          const _oldDetail=existing.querySelector('.tool-card-detail');
          const _newDetail=replacement.querySelector('.tool-card-detail');
          const _mode=_oldDetail&&_oldDetail.getAttribute('data-transparent-detail-mode');
          if(_newDetail&&_mode){
            const _tab=_newDetail.querySelector(`.transparent-detail-mode[data-mode="${_mode}"]`);
            if(_tab) _setTransparentDetailMode(_tab,_mode);
          }
          if(_oldCard&&_newCard&&_oldCard.classList.contains('open')){
            _setTransparentCardOpen(_newCard,true);
          }
        }catch(_){ /* non-fatal: completion still renders, just collapsed */ }
        existing.replaceWith(replacement);
        _syncTransparentEventControls(turn);
        _moveLiveRunStatusToTurnEnd();
        if(typeof scrollIfPinned==='function') scrollIfPinned();
        return;
      }
    }
    const row=_decorateTransparentEventRow(buildToolCard(tc),{
      type:'tool',
      name:tc&&tc.name,
      status:_transparentToolStatus(tc),
      toolCall:tc,
      live:true,
      segmentSeq:effectiveSegmentSeq,
      burstId,
    });
    if(tid) row.dataset.liveTid=tid;
    insertTransparentRow(row);
    _syncTransparentEventControls(turn);
    _moveLiveRunStatusToTurnEnd();
    if(typeof scrollIfPinned==='function') scrollIfPinned();
    return;
  }
  if(anchor) _removeEmptyLiveWorklogShells(inner);
  const group=ensureLiveWorklogContainer(inner,{
    anchor,
    activityKey:_activityKeyForLiveTurn(),
    segmentSeq:effectiveSegmentSeq,
    burstId,
  });
  const list=_liveToolStepEl(group);
  if(!list) return;
  // toolComplete can replace the existing live card with the same tid.
  if(tid){
    const existing=group.querySelector(`.tool-card-row[data-live-tid="${CSS.escape(tid)}"]`);
    if(existing){
      const replacement=buildToolCard(tc);
      replacement.dataset.liveTid=tid;
      existing.replaceWith(replacement);
      _syncToolCallGroupSummary(group);
      _moveLiveRunStatusToTurnEnd();
      if(typeof scrollIfPinned==='function') scrollIfPinned();
      return;
    }
  }
  const worklog=_toolWorklogListEl(group) || list;
  const waiting=worklog.querySelector('.agent-activity-status[data-activity-event-id="thinking-placeholder"] .agent-activity-status-label');
  if(waiting&&tc.done===false) waiting.textContent='Waiting on tool result';
  const row=buildToolCard(tc);
  if(tid) row.dataset.liveTid=tid;
  list.appendChild(row);
  _syncToolCallGroupSummary(group);
  _moveLiveRunStatusToTurnEnd();
  if(typeof scrollIfPinned==='function') scrollIfPinned();
}

function _findLatestLiveAssistantByBurst(inner, burstId){
  if(!inner || !burstId) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-activity-burst-id="${CSS.escape(String(burstId))}"]`))
    .filter(el=>el.isConnected!==false);
  return candidates[candidates.length-1] || null;
}
function _findLatestLiveAssistantBySegment(inner, segmentSeq){
  if(!inner || !segmentSeq) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-live-segment-seq="${CSS.escape(String(segmentSeq))}"]`)).filter(el=>el.isConnected!==false);
  return candidates[candidates.length-1] || null;
}
function _liveAssistantHasVisibleText(el){
  if(!el||!el.matches||!el.matches('[data-live-assistant="1"]')) return false;
  const body=el.querySelector&&el.querySelector('.msg-body');
  const text=(body?body.textContent:el.textContent)||el.dataset&&el.dataset.rawText||'';
  return !!String(text||'').trim();
}
function _findPreviousVisibleLiveAssistant(inner, beforeNode){
  if(!inner) return null;
  let node=beforeNode&&beforeNode.previousElementSibling;
  while(node){
    if(_liveAssistantHasVisibleText(node)) return node;
    node=node.previousElementSibling;
  }
  return null;
}
function _findLatestVisibleLiveAssistant(inner){
  if(!inner) return null;
  const candidates=Array.from(inner.querySelectorAll('[data-live-assistant="1"]')).filter(el=>el.isConnected!==false&&_liveAssistantHasVisibleText(el));
  return candidates[candidates.length-1] || null;
}
function _findLatestVisibleLiveAssistantByBurst(inner, burstId){
  if(!inner || !burstId) return null;
  const candidates=Array.from(inner.querySelectorAll(`[data-live-assistant="1"][data-activity-burst-id="${CSS.escape(String(burstId))}"]`))
    .filter(el=>el.isConnected!==false&&_liveAssistantHasVisibleText(el));
  return candidates[candidates.length-1] || null;
}
function _findLiveAssistantAnchorForSegment(inner, segmentSeq){
  const exact=_findLatestLiveAssistantBySegment(inner, segmentSeq);
  if(exact&&_liveAssistantHasVisibleText(exact)) return exact;
  return _findPreviousVisibleLiveAssistant(inner, exact) || _findLatestVisibleLiveAssistant(inner) || exact;
}

function clearLiveToolCards(){
  if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
  const inner=_assistantTurnBlocks($('liveAssistantTurn'));
  if(inner) inner.querySelectorAll('.live-worklog[data-live-worklog-shell],.tool-worklog-group[data-live-tool-call-group],.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid]:not(.transparent-event-row),[data-anchor-scene-owner="1"],[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  // Reset the per-turn user expand intent so the next turn starts at the
  // default collapsed state (#1298).
  if(typeof _clearLiveActivityUserIntent==='function') _clearLiveActivityUserIntent();
  // Legacy #liveToolCards container cleanup — kept for safety in case any
  // leftover cards were inserted there before this refactor took effect.
  const container=$('liveToolCards');
  if(container){container.innerHTML='';container.style.display='none';}
}
function _hideLiveActivityForFinalAnswerOnly(){
  clearLiveToolCards();
  if(typeof removeThinking==='function') removeThinking();
  const turn=$('liveAssistantTurn');
  const inner=_assistantTurnBlocks(turn);
  if(inner){
    inner.querySelectorAll('.transparent-event-row,.agent-activity-thinking,.wl-reason,#liveRunStatus,.live-worklog[data-live-worklog-shell],.tool-worklog-group[data-live-tool-call-group],.tool-call-group[data-live-tool-call-group],.tool-card-row[data-live-tid],[data-anchor-scene-owner="1"],[data-anchor-scene-row="1"]').forEach(el=>el.remove());
  }
  const legacyThinking=$('thinkingRow');
  if(legacyThinking) legacyThinking.remove();
  if(turn&&inner&&!inner.children.length) turn.remove();
}
if(typeof window!=='undefined') window._hideLiveActivityForFinalAnswerOnly=_hideLiveActivityForFinalAnswerOnly;
function _removeEmptyLiveWorklogShells(inner){
  if(!inner) return;
  inner.querySelectorAll('.live-worklog[data-live-worklog-shell="1"],.tool-worklog-group[data-live-worklog-shell="1"],.tool-call-group[data-live-worklog-shell="1"]').forEach(group=>{
    if(!group.querySelector('.tool-card-row,.wl-reason,.agent-activity-thinking')) group.remove();
  });
}
function _setLiveWorklogThinkingPlaceholder(group){
  if(!group) return;
  group.setAttribute('data-prestart-thinking','1');
  const label=group.querySelector&&(
    group.querySelector('.tool-worklog-label') || group.querySelector('.tool-call-group-label')
  );
  if(label){
    const text=typeof t==='function'?t('worklog_thinking'):'Thinking';
    label.textContent=text;
    label.setAttribute('data-sweep-label', text);
  }
  const durationEl=group.querySelector&&group.querySelector('.tool-call-group-duration');
  if(durationEl){
    durationEl.textContent='';
    durationEl.style.display='none';
  }
}
function ensureLiveWorklogShell(){
  if(!S.session) return null;
  if(typeof isFinalAnswerOnlyMode==='function'&&isFinalAnswerOnlyMode()) return null;
  const activeStreamId=S.activeStreamId||'';
  if(activeStreamId&&typeof _renderLiveAnchorActivitySceneForStream==='function'&&_renderLiveAnchorActivitySceneForStream(activeStreamId, S.session.session_id)){
    _dedupeLiveProcessedWorklogAnchors($('liveAssistantTurn'));
    return $('liveAssistantTurn');
  }
  if(activeStreamId&&isLiveAnchorActivitySceneOwner(activeStreamId)){
    _renderLiveAnchorActivitySceneForStream(activeStreamId, S.session.session_id);
    _dedupeLiveProcessedWorklogAnchors($('liveAssistantTurn'));
    return $('liveAssistantTurn');
  }
  $('emptyState').style.display='none';
  const compactWorklog=typeof isCompactWorklogMode==='function'&&isCompactWorklogMode();
  if(!compactWorklog&&!isSimplifiedToolCalling()){
    appendThinking();
    return $('thinkingRow');
  }
  let turn=$('liveAssistantTurn');
  if(!turn){
    turn=_createAssistantTurn();
    turn.id='liveAssistantTurn';
    if(S.session) turn.dataset.sessionId=S.session.session_id;
    $('msgInner').appendChild(turn);
  }
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return null;
  if(isTransparentStream()){
    _moveLiveRunStatusToTurnEnd();
    scrollIfPinned();
    return blocks;
  }
  const group=ensureActivityGroup(blocks,{
    live:true,
    collapsed:false,
    activityKey:_activityKeyForLiveTurn(),
    turnStartedAt:S.session&&S.session.pending_started_at,
  });
  if(!group) return null;
  if(activeStreamId){
    group.removeAttribute('data-prestart-thinking');
    if(typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(group);
  }else{
    _setLiveWorklogThinkingPlaceholder(group);
  }
  _moveLiveRunStatusToTurnEnd();
  _dedupeLiveProcessedWorklogAnchors(turn);
  scrollIfPinned();
  return group;
}

// ── Edit + Regenerate ──


window.HermesUI.register('toolWorklog', {
  buildToolCard,
  appendLiveToolCard,
  clearLiveToolCards,
  ensureLiveWorklogShell,
});
