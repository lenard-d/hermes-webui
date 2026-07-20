import { esc } from './state.js';
import { _toolDisclosureIdentity } from './tool-identity.js';
import { _isMemorySave, _isSkillUpdate, _redactToolTargetLabel, _toolActionKind, _toolActionLabelText, _toolDisplayName, _toolFullCommandLabel, _toolTargetLabel } from './tool-call-presentation.js';

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

export {
  toolIcon,
  _toolArgPreviewValue,
  _toolArgPreviewKeyIsHidden,
  _formatToolArgPreview,
  _toolResultOneLiner,
  _toolCardPreviewText,
  _toolCardAllowsDetail,
  _toolDetailLeadLabel,
  _toolDetailLeadText,
  buildToolCard,
  _colorDiffLines,
  _snippetLooksLikeDiff,
  _toggleToolDiff,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  toolIcon: { enumerable: true, get: () => toolIcon, set: (value) => { toolIcon = value; } },
  _toolArgPreviewValue: { enumerable: true, get: () => _toolArgPreviewValue, set: (value) => { _toolArgPreviewValue = value; } },
  _toolArgPreviewKeyIsHidden: { enumerable: true, get: () => _toolArgPreviewKeyIsHidden, set: (value) => { _toolArgPreviewKeyIsHidden = value; } },
  _formatToolArgPreview: { enumerable: true, get: () => _formatToolArgPreview, set: (value) => { _formatToolArgPreview = value; } },
  _toolResultOneLiner: { enumerable: true, get: () => _toolResultOneLiner, set: (value) => { _toolResultOneLiner = value; } },
  _toolCardPreviewText: { enumerable: true, get: () => _toolCardPreviewText, set: (value) => { _toolCardPreviewText = value; } },
  _toolCardAllowsDetail: { enumerable: true, get: () => _toolCardAllowsDetail, set: (value) => { _toolCardAllowsDetail = value; } },
  _toolDetailLeadLabel: { enumerable: true, get: () => _toolDetailLeadLabel, set: (value) => { _toolDetailLeadLabel = value; } },
  _toolDetailLeadText: { enumerable: true, get: () => _toolDetailLeadText, set: (value) => { _toolDetailLeadText = value; } },
  buildToolCard: { enumerable: true, get: () => buildToolCard, set: (value) => { buildToolCard = value; } },
  _colorDiffLines: { enumerable: true, get: () => _colorDiffLines, set: (value) => { _colorDiffLines = value; } },
  _snippetLooksLikeDiff: { enumerable: true, get: () => _snippetLooksLikeDiff, set: (value) => { _snippetLooksLikeDiff = value; } },
  _toggleToolDiff: { enumerable: true, get: () => _toggleToolDiff, set: (value) => { _toggleToolDiff = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
