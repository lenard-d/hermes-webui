import { esc } from './state.js';

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

export {
  _toolDisplayName,
  _tcAction,
  _isMemorySave,
  _isSkillUpdate,
  _decodeToolLabelEntities,
  _redactToolTargetLabel,
  _shortToolLabel,
  _toolI18n,
  _toolPathBasename,
  _toolActionKind,
  _toolKindIcon,
  _toolTargetLabel,
  _toolReadRangeLabel,
  _toolFullCommandLabel,
  _toolVisibleTargetLabel,
  _toolCommandTitle,
  _toolQueryTitle,
  _toolActionLabelText,
  _toolActionLabel,
  _toolWorklogSummaryLine,
  _toolWorklogJoin,
  _toolWorklogActionParts,
  _toolWorklogSummary,
  _MEMORY_SAVE_ACTIONS,
  _SKILL_UPDATE_ACTIONS,
  _toolWorklogSummaries,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _toolDisplayName: { enumerable: true, get: () => _toolDisplayName, set: (value) => { _toolDisplayName = value; } },
  _tcAction: { enumerable: true, get: () => _tcAction, set: (value) => { _tcAction = value; } },
  _isMemorySave: { enumerable: true, get: () => _isMemorySave, set: (value) => { _isMemorySave = value; } },
  _isSkillUpdate: { enumerable: true, get: () => _isSkillUpdate, set: (value) => { _isSkillUpdate = value; } },
  _decodeToolLabelEntities: { enumerable: true, get: () => _decodeToolLabelEntities, set: (value) => { _decodeToolLabelEntities = value; } },
  _redactToolTargetLabel: { enumerable: true, get: () => _redactToolTargetLabel, set: (value) => { _redactToolTargetLabel = value; } },
  _shortToolLabel: { enumerable: true, get: () => _shortToolLabel, set: (value) => { _shortToolLabel = value; } },
  _toolI18n: { enumerable: true, get: () => _toolI18n, set: (value) => { _toolI18n = value; } },
  _toolPathBasename: { enumerable: true, get: () => _toolPathBasename, set: (value) => { _toolPathBasename = value; } },
  _toolActionKind: { enumerable: true, get: () => _toolActionKind, set: (value) => { _toolActionKind = value; } },
  _toolKindIcon: { enumerable: true, get: () => _toolKindIcon, set: (value) => { _toolKindIcon = value; } },
  _toolTargetLabel: { enumerable: true, get: () => _toolTargetLabel, set: (value) => { _toolTargetLabel = value; } },
  _toolReadRangeLabel: { enumerable: true, get: () => _toolReadRangeLabel, set: (value) => { _toolReadRangeLabel = value; } },
  _toolFullCommandLabel: { enumerable: true, get: () => _toolFullCommandLabel, set: (value) => { _toolFullCommandLabel = value; } },
  _toolVisibleTargetLabel: { enumerable: true, get: () => _toolVisibleTargetLabel, set: (value) => { _toolVisibleTargetLabel = value; } },
  _toolCommandTitle: { enumerable: true, get: () => _toolCommandTitle, set: (value) => { _toolCommandTitle = value; } },
  _toolQueryTitle: { enumerable: true, get: () => _toolQueryTitle, set: (value) => { _toolQueryTitle = value; } },
  _toolActionLabelText: { enumerable: true, get: () => _toolActionLabelText, set: (value) => { _toolActionLabelText = value; } },
  _toolActionLabel: { enumerable: true, get: () => _toolActionLabel, set: (value) => { _toolActionLabel = value; } },
  _toolWorklogSummaryLine: { enumerable: true, get: () => _toolWorklogSummaryLine, set: (value) => { _toolWorklogSummaryLine = value; } },
  _toolWorklogJoin: { enumerable: true, get: () => _toolWorklogJoin, set: (value) => { _toolWorklogJoin = value; } },
  _toolWorklogActionParts: { enumerable: true, get: () => _toolWorklogActionParts, set: (value) => { _toolWorklogActionParts = value; } },
  _toolWorklogSummary: { enumerable: true, get: () => _toolWorklogSummary, set: (value) => { _toolWorklogSummary = value; } },
  _MEMORY_SAVE_ACTIONS: { enumerable: true, get: () => _MEMORY_SAVE_ACTIONS },
  _SKILL_UPDATE_ACTIONS: { enumerable: true, get: () => _SKILL_UPDATE_ACTIONS },
  _toolWorklogSummaries: { enumerable: true, get: () => _toolWorklogSummaries },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
