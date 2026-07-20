import { _sanitizeThinkingDisplayText } from './activity-and-scroll.js';
import { _revealTransparentEarlierSteps } from './anchor-scenes.js';
import { _syncTransparentEventTimestamp } from './composer-controls.js';
import { showToast } from './composer.js';
import { _postProcessWithAnchorSuppression, _renderThinkingInto } from './content-postprocessing.js';
import { S, esc } from './state.js';
import { _redactToolTargetLabel, _shortToolLabel, _toolActionKind, _toolCardAllowsDetail, _toolVisibleTargetLabel, buildToolCard } from './tool-worklog.js';
import { _anchorSceneRowsForRendering, _anchorSceneToolCallFromRow, _applyTransparentRowFading, _wireTransparentTurnToggle } from './transparent-worklog.js';

function _worklogDetailsExpandedDefault(){
  return window._worklogDetailsExpandedByDefault===true;
}
function _applyWorklogDetailsExpandedDefault(root){
  const scope=root&&root.querySelectorAll?root:document;
  const open=_worklogDetailsExpandedDefault();
  scope.querySelectorAll('.thinking-card').forEach(card=>{
    card.classList.toggle('open', open);
  });
  scope.querySelectorAll('.tool-group[data-tool-worklog-tool-group="1"],.tool-worklog-tool-group').forEach(group=>{
    group.classList.toggle('open', open);
    group.classList.toggle('tool-worklog-tool-group-collapsed', !open);
    const summary=group.querySelector('.tool-group-head,.tool-worklog-tool-group-head');
    if(summary) summary.setAttribute('aria-expanded', String(open));
  });
}
const _worklogDetailDisclosureSelector='.thinking-card,.tool-card,.tool-group[data-tool-worklog-tool-group="1"],.tool-worklog-tool-group';
function _worklogDetailTextKey(text, maxLen){
  return String(text||'').replace(/\s+/g,' ').trim().slice(0,maxLen||160);
}
function _worklogDetailHashKey(value){
  const s=String(value||'');
  let hash=2166136261;
  for(let i=0;i<s.length;i++){
    hash^=s.charCodeAt(i);
    hash=Math.imul(hash,16777619)>>>0;
  }
  return hash.toString(36);
}
function _worklogDetailBaseKey(el){
  if(!el||!el.classList) return '';
  const activity=el.closest&&el.closest('.agent-activity-group,.tool-worklog-group[data-tool-worklog-group="1"],.tool-call-group[data-tool-call-group="1"],.live-worklog[data-live-worklog-shell="1"]');
  const scope=activity?[
    activity.getAttribute('data-anchor-stream-id')?`stream:${activity.getAttribute('data-anchor-stream-id')}`:'',
    activity.getAttribute('data-activity-disclosure-key')||'',
    activity.getAttribute('data-tool-worklog-key')||'',
    activity.getAttribute('data-live-segment-seq')||'',
    activity.getAttribute('data-activity-burst-id')||'',
  ].filter(Boolean).join('|'):'';
  if(el.classList.contains('thinking-card')){
    const row=el.closest('.agent-activity-thinking,.thinking-card-row');
    const stable=row&&(
      row.getAttribute('data-thinking-key')||
      row.getAttribute('data-live-thinking-key')||
      row.getAttribute('data-live-segment-seq')||
      row.getAttribute('data-activity-burst-id')||
      row.id||
      ''
    );
    return `thinking:${scope}:${stable||'ordinal'}`;
  }
  if(el.classList.contains('tool-card')){
    const row=el.closest('.tool-card-row');
    const tid=row&&(
      row.getAttribute('data-tool-disclosure-key')||
      row.getAttribute('data-live-tid')||
      row.getAttribute('data-tool-call-id')||
      row.getAttribute('data-tool-id')||
      ''
    );
    const label=row&&(row.dataset&&row.dataset.toolActionLabel)||'';
    const name=el.querySelector('.tool-card-name');
    return `tool:${scope}:${tid||label||_worklogDetailTextKey(name?name.textContent:'tool',80)}`;
  }
  if(el.matches&&el.matches('.tool-group[data-tool-worklog-tool-group="1"],.tool-worklog-tool-group')){
    const stable=
      el.getAttribute('data-tool-group-disclosure-key')||
      el.getAttribute('data-activity-disclosure-key')||
      el.getAttribute('data-tool-worklog-key')||
      el.getAttribute('data-live-segment-seq')||
      el.getAttribute('data-activity-burst-id')||
      'group';
    return `tool-group:${scope}:${stable}`;
  }
  return '';
}
function _worklogDetailDisclosureIsOpen(el){
  return !!(el&&el.classList&&el.classList.contains('open'));
}
function _worklogDetailScrollableBody(el){
  if(!el||!el.querySelector) return null;
  return el.querySelector('.thinking-card-body,.tool-card-detail');
}
function _setWorklogDetailDisclosureOpen(el, open){
  if(!el||!el.classList) return;
  // #5966 (Codex F2 r2): restoring an OPEN state on a settled Transparent Stream
  // tool row whose detail was deferred must MATERIALIZE the body first, or the
  // card restores open-but-empty after an in-session renderMessages() rebuild
  // (e.g. the next send re-defers it, then this toggles .open with no content).
  if(open){
    const drow=(el.matches&&el.matches('.transparent-event-row[data-transparent-detail-deferred="1"]'))
      ? el
      : (el.closest&&el.closest('.transparent-event-row[data-transparent-detail-deferred="1"]'));
    if(drow&&typeof _materializeTransparentToolDetail==='function') _materializeTransparentToolDetail(drow);
  }
  el.classList.toggle('open', !!open);
  if(el.matches&&el.matches('.tool-group[data-tool-worklog-tool-group="1"],.tool-worklog-tool-group')){
    el.classList.toggle('tool-worklog-tool-group-collapsed', !open);
    const summary=el.querySelector('.tool-group-head,.tool-worklog-tool-group-head');
    if(summary) summary.setAttribute('aria-expanded', String(!!open));
  }
}
function _worklogDetailDisclosureKeyForElement(el, counts){
  const base=_worklogDetailBaseKey(el);
  if(!base) return '';
  const idx=counts[base]||0;
  counts[base]=idx+1;
  return `${base}#${idx}`;
}
function _captureWorklogDetailDisclosureState(root){
  const state=new Map();
  if(!root||!root.querySelectorAll) return state;
  // Stamp the capturing session so a restore can't replay one session's
  // disclosure state onto another. Cross-session switches currently wipe
  // #msgInner (sessions.js loading placeholder) so the capture is normally
  // empty, but the ordinal/derived keys carry no session id — this stamp makes
  // the isolation explicit instead of depending on that wipe invariant. (Opus #4063.)
  try{ state._sid=S.session?S.session.session_id:null; }catch(_){ state._sid=null; }
  const counts=Object.create(null);
  root.querySelectorAll(_worklogDetailDisclosureSelector).forEach(el=>{
    const key=_worklogDetailDisclosureKeyForElement(el, counts);
    if(!key) return;
    const body=_worklogDetailScrollableBody(el);
    state.set(key,{
      open:_worklogDetailDisclosureIsOpen(el),
      scrollTop:body?Math.max(0,Number(body.scrollTop)||0):0,
    });
  });
  return state;
}
function _restoreWorklogDetailDisclosureState(root, state){
  if(!root||!root.querySelectorAll||!state||!state.size) return;
  // Don't restore a snapshot captured under a different session.
  try{ if(state._sid!==undefined && state._sid!==(S.session?S.session.session_id:null)) return; }catch(_){ /* fall through */ }
  const counts=Object.create(null);
  root.querySelectorAll(_worklogDetailDisclosureSelector).forEach(el=>{
    const key=_worklogDetailDisclosureKeyForElement(el, counts);
    if(!key||!state.has(key)) return;
    const saved=state.get(key);
    const open=(saved&&typeof saved==='object'&&'open' in saved)?saved.open:saved;
    _setWorklogDetailDisclosureOpen(el, open);
    const scrollTop=(saved&&typeof saved==='object')?Number(saved.scrollTop):0;
    if(open&&Number.isFinite(scrollTop)&&scrollTop>0){
      const body=_worklogDetailScrollableBody(el);
      if(body) body.scrollTop=Math.min(scrollTop, Math.max(0, body.scrollHeight-body.clientHeight));
    }
  });
}
function _thinkingCardHtml(text, open){
  const clean=_sanitizeThinkingDisplayText(text);
  const copyBtn=`<button class="thinking-copy-btn" onclick="event.stopPropagation();_copyThinkingText(this)" title="${t('copy')}" aria-label="${t('copy')}">${li('copy',12)}</button>`;
  const shouldOpen=!!open||_worklogDetailsExpandedDefault();
  const classes=`thinking-card${shouldOpen?' open':''}`;
  return `<div class="${classes}"><div class="thinking-card-header" onclick="this.parentElement.classList.toggle('open')"><span class="thinking-card-icon">${li('lightbulb',14)}</span><span class="thinking-card-label">${t('thinking')}</span><span class="thinking-card-btn-row">${copyBtn}<span class="thinking-card-toggle">${li('chevron-right',12)}</span></span></div><div class="thinking-card-body"><pre>${esc(clean)}</pre></div></div>`;
}
function isSimplifiedToolCalling(){
  return window._simplifiedToolCalling!==false;
}
function _thinkingActivityNode(text, open, disclosureKey){
  const row=document.createElement('div');
  row.className='agent-activity-thinking';
  row.setAttribute('data-worklog-thinking-card','1');
  if(disclosureKey) row.setAttribute('data-thinking-key', String(disclosureKey));
  row.innerHTML=_thinkingCardHtml(text, open);
  _renderThinkingInto(row,text);
  return row;
}
function chatActivityMode(){
  if(typeof window==='undefined') return 'compact_worklog';
  const mode=window._chatActivityDisplayMode;
  if(mode==='compact_worklog'||mode==='transparent_stream'||mode==='hide_all_activity') return mode;
  return window._transparentStream ? 'transparent_stream' : 'compact_worklog';
}
function isTransparentStream(){
  return chatActivityMode()==='transparent_stream';
}
function isFinalAnswerOnlyMode(){
  return chatActivityMode()==='hide_all_activity';
}
function isCompactWorklogMode(){
  return isSimplifiedToolCalling()&&chatActivityMode()==='compact_worklog';
}
if(typeof window!=='undefined'){
  window.chatActivityMode=chatActivityMode;
  window.isTransparentStream=isTransparentStream;
  window.isFinalAnswerOnlyMode=isFinalAnswerOnlyMode;
  window.isCompactWorklogMode=isCompactWorklogMode;
}
function _toolShortName(name){
  const raw=String(name||'').trim();
  if(!raw) return 'tool';
  if(raw.startsWith('mcp__')){
    const parts=raw.split('__').filter(Boolean);
    if(parts.length>1) return parts.slice(1).join('/');
  }
  if(raw.startsWith('mcp.')){
    const parts=raw.split('.').filter(Boolean);
    if(parts.length>1) return parts.slice(1).join('/');
  }
  return raw;
}
function _transparentEventPreview(text){
  const clean=_sanitizeThinkingDisplayText(String(text||'')).replace(/\s+/g,' ').trim();
  if(!clean) return '';
  return clean.length>180?`${clean.slice(0,177)}...`:clean;
}
function _transparentToolStatus(tc, settled){
  if(tc&&tc.is_error) return 'Failed';
  if(tc&&tc.done===false) return settled?'Interrupted':'Running';
  return 'Completed';
}
// Quiet one-line summary for a collapsed transparent tool row (#4658).
// The transparent view overrides the row name to the bare tool name
// (_toolShortName, e.g. "read_file"/"terminal"), so — unlike the worklog view —
// it has no action-label carrying the target, and buildToolCard's collapsed
// preview is blanked for the common arg/shell case (the #4411 suppression that
// assumes the name carries the target). That left transparent rows showing only
// the bare tool name with no hint of what each call did. Rebuild a summary from
// the call's TARGET (path/command/query/skill/...) — NOT the raw result JSON —
// so it stays consistent with the "keep collapsed previews quiet" intent
// (test_tool_card_preview_summary.py) while restoring "understand the call
// without expanding it".
function _transparentToolSummary(tc){
  if(!tc||typeof tc!=='object') return '';
  // Explicit progress text (e.g. subagent_progress) wins while still running.
  const explicit=String(tc.preview||'').trim();
  if(tc.done===false&&explicit) return _shortToolLabel(explicit,160);
  // Target-based summary only (path/command/query/skill). Deliberately NO generic
  // arg-preview fallback: a call with args but no real target (e.g. `terminal`
  // with only {workdir} or an unknown tool with {mode:"dry-run"}) must yield an
  // EMPTY collapsed preview rather than dumping a raw arg snippet — that keeps the
  // collapsed row quiet and consistent with the no-args case (#4658 review).
  const target=typeof _toolVisibleTargetLabel==='function'?_toolVisibleTargetLabel(tc,{limit:160,rangeFirst:true}):'';
  if(target) return target;
  return '';
}
function _copyEventToClipboard(row){
  if(!row) return;
  const type=row.getAttribute('data-event-type');
  let text='';
  let label='event';
  if(type==='tool'){
    const tc=row._tcData||{};
    const fallbackName=row.getAttribute('data-event-name')||row.getAttribute('data-tool-name')||'tool';
    label=`tool ${tc.name||fallbackName}`;
    const parts=[`tool: ${tc.name||fallbackName}`];
    if(tc.args&&Object.keys(tc.args).length){
      // Redact secret-bearing arg values before copying to clipboard, mirroring
      // the Full-tab render — content args can be long commands with secrets
      // past the first line (#4928 gate).
      let argsForCopy=tc.args;
      if(typeof _redactToolTargetLabel==='function'){
        try{
          argsForCopy={};
          Object.entries(tc.args).forEach(([k,v])=>{
            argsForCopy[k]=typeof v==='string'?_redactToolTargetLabel(v):v;
          });
        }catch(e){ argsForCopy=tc.args; }
      }
      parts.push('args: '+JSON.stringify(argsForCopy,null,2));
    }
    if(tc.snippet) parts.push('output:\n'+String(tc.snippet));
    if(parts.length===1){
      const argsText=Array.from(row.querySelectorAll('.tool-card-args .tool-arg-pair'))
        .map(pair=>String(pair.textContent||'').trim())
        .filter(Boolean)
        .join('\n');
      const outputText=String((row.querySelector('.tool-card-result pre')||{}).textContent||'').trim();
      if(argsText) parts.push('args:\n'+argsText);
      if(outputText) parts.push('output:\n'+outputText);
    }
    text=parts.join('\n');
  }else if(type==='thinking'){
    const pre=row.querySelector('.thinking-card-body pre');
    text=pre?pre.textContent:(row.textContent||'').replace(/^\s*Thinking\s*/i,'');
    label='thinking';
  }else{
    text=row.textContent||'';
  }
  const fallback=()=>{
    try{
      const ta=document.createElement('textarea');
      ta.value=text;
      ta.setAttribute('readonly','');
      ta.style.position='absolute';
      ta.style.left='-9999px';
      document.body.appendChild(ta);
      ta.select();
      const ok=document.execCommand('copy');
      document.body.removeChild(ta);
      if(typeof showToast==='function') showToast(ok?(t('copied')||'Copied'):(t('copy_failed')||'Copy failed'),1600);
    }catch(_){
      if(typeof showToast==='function') showToast(t('copy_failed')||'Copy failed',2000,'error');
    }
  };
  if(navigator&&navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(()=>{
      if(typeof showToast==='function') showToast(`${t('copied')||'Copied'} ${label}`,1600);
    }).catch(fallback);
  }else{
    fallback();
  }
}
function _attachCopyButton(header){
  if(!header) return null;
  const bindCopyButton=(btn)=>{
    if(!btn) return null;
    btn.classList.add('transparent-event-copy');
    btn.setAttribute('role','button');
    btn.setAttribute('tabindex','0');
    btn.setAttribute('aria-label',t('copy')||'Copy');
    btn.setAttribute('data-transparent-copy','1');
    btn.title=t('copy')||'Copy';
    const handler=function(ev){
      ev.stopPropagation();
      ev.preventDefault();
      _copyEventToClipboard(header.closest('.transparent-event-row'));
    };
    btn.onclick=handler;
    btn.onkeydown=function(ev){
      if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();handler(ev);}
    };
    return btn;
  };
  // Reuse ANY existing copy button (handles both .transparent-event-copy
  // added by this function AND the legacy .thinking-copy-btn baked into the
  // thinking-card template). Returning the existing one prevents the
  // duplicate copy buttons that appeared in thinking boxes.
  const existing=header.querySelector('.transparent-event-copy,.thinking-copy-btn');
  if(existing){
    // Normalise the class so CSS treats them identically.
    return bindCopyButton(existing);
  }
  const btn=document.createElement('span');
  btn.className='transparent-event-copy';
  btn.innerHTML=`<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`;
  bindCopyButton(btn);
  // Place the copy button at a FIXED flexbox position regardless of
  // whether a toggle or status badge is present: always right before
  // the toggle. The CSS uses flexbox order to keep it visually stable
  // even if other elements are inserted between header and toggle.
  const toggle=header.querySelector('.tool-card-toggle,.thinking-card-toggle');
  if(toggle&&toggle.parentNode===header) header.insertBefore(btn,toggle);
  else header.appendChild(btn);
  return btn;
}
function _transparentEventCountLabel(toolCount){
  return toolCount?`Trace: ${toolCount} ${toolCount===1?'tool':'tools'}`:'Trace';
}
function _setTransparentDetailMode(tab, mode){
  const row=tab&&tab.closest?tab.closest('.transparent-event-row'):null;
  const detail=row&&row.querySelector('.tool-card-detail');
  if(!detail) return;
  const next=mode==='output'?'output':'full';
  detail.setAttribute('data-transparent-detail-mode',next);
  detail.querySelectorAll('.transparent-detail-mode').forEach(el=>{
    el.classList.toggle('active', el===tab || el.getAttribute('data-mode')===next);
  });
}
function _setTransparentCardOpen(card, open){
  if(!card) return;
  const expanded=!!open;
  const row=card.closest&&card.closest('.transparent-event-row');
  // #5966: a settled tool row whose detail body was deferred at render must
  // materialize it the first time it's opened (before we flip .open so the
  // detail exists for the same paint). No-op on live/already-materialized rows.
  if(expanded&&row&&row.getAttribute('data-transparent-detail-deferred')==='1'){
    _materializeTransparentToolDetail(row);
  }
  card.classList.toggle('open',expanded);
  if(row) row.setAttribute('data-expanded',expanded?'1':'0');
  const header=card.querySelector('.tool-card-header,.thinking-card-header');
  if(header) header.setAttribute('aria-expanded',expanded?'true':'false');
}
// #5966: build the deferred `.tool-card-detail` for a settled transparent tool
// row on first expand — the lazy counterpart of the eager build in
// _decorateTransparentEventRow. Recovers the tool call from the in-memory stash
// or, after a _sessionHtmlCache innerHTML round-trip drops the JS property, from
// the row's data-anchor-row-id → S.messages scene (the #5839 recovery pattern).
// Idempotent: clears the deferred flag and runs anchor-suppressed post-processing
// (Prism / copy buttons / KaTeX / Mermaid / trees) on just the new subtree so an
// expanded deferred row is byte-identical to the eager path.
// #5966: does this tool call have a detail body worth deferring? Mirrors
// buildToolCard's `hasDetail` (snippet OR args, gated by _toolCardAllowsDetail)
// so we only mark a row deferred/expandable when there's genuinely something to
// build — a detail-less tool row keeps its `tool-card-no-detail` (no chevron).
function _transparentToolRowHasDetail(tc){
  if(!tc||typeof tc!=='object') return false;
  const hasRaw=!!tc.snippet||(tc.args&&typeof tc.args==='object'&&Object.keys(tc.args).length>0);
  if(!hasRaw) return false;
  if(typeof _toolActionKind==='function'&&typeof _toolCardAllowsDetail==='function'){
    try{ return !!_toolCardAllowsDetail(_toolActionKind(tc), tc); }catch(_){ return true; }
  }
  return true;
}
function _materializeTransparentToolDetail(row){
  if(!row||row.getAttribute('data-transparent-detail-deferred')!=='1') return false;
  const card=row.querySelector('.tool-card');
  if(!card){ row.removeAttribute('data-transparent-detail-deferred'); return false; }
  let tc=row._deferredToolCall;
  if(!tc){
    tc=_transparentToolCallFromRowDataset(row);
  }
  if(!tc){ row.removeAttribute('data-transparent-detail-deferred'); return false; }
  const status=String(row.getAttribute('data-event-status')||_transparentToolStatus(tc,true));
  row.removeAttribute('data-transparent-detail-deferred');
  row._deferredToolCall=null;
  if(!card.querySelector('.tool-card-detail')){
    // Codex F1(r2): rebuild through the CANONICAL buildToolCard() detail path, not
    // the thinner _transparentToolDetailHtml() — the latter drops diff coloring,
    // "Show diff/Show more", and canonical shell-command detail that buildToolCard
    // produces, so an expanded deferred row must transplant buildToolCard's own
    // `.tool-card-detail` to stay byte-identical to the eager path.
    let sourceDetail=null;
    try{
      const rebuilt=buildToolCard(tc);
      sourceDetail=rebuilt&&rebuilt.querySelector('.tool-card-detail');
    }catch(_){ sourceDetail=null; }
    if(sourceDetail){
      card.appendChild(sourceDetail);   // move the canonical detail node onto the live card
    }else{
      // Fallback (e.g. buildToolCard unavailable): the lighter detail is better than none.
      card.insertAdjacentHTML('beforeend',_transparentToolDetailHtml(tc,status));
    }
    const detail=card.querySelector('.tool-card-detail');
    if(detail&&!detail.querySelector('.transparent-detail-modes')){
      const modes=document.createElement('div');
      modes.className='transparent-detail-modes';
      modes.setAttribute('role','tablist');
      modes.innerHTML=`<span class="transparent-detail-mode active" role="tab" tabindex="0" data-mode="full" onclick="_setTransparentDetailMode(this,'full')">Full</span><span class="transparent-detail-mode" role="tab" tabindex="0" data-mode="output" onclick="_setTransparentDetailMode(this,'output')">Output</span>`;
      const firstChild=detail.firstChild;
      if(firstChild&&firstChild.parentNode===detail) detail.insertBefore(modes, firstChild);
      else detail.appendChild(modes);
      detail.setAttribute('data-transparent-detail-mode','full');
    }
    // Match the eager path's post-processing so highlight/copy/KaTeX/Mermaid land.
    if(typeof _postProcessWithAnchorSuppression==='function'){
      requestAnimationFrame(()=>{ try{ _postProcessWithAnchorSuppression(card); }catch(_){ } });
    }
  }
  return true;
}
// Recover a tool call for a deferred row whose _deferredToolCall JS property was
// dropped by an innerHTML cache round-trip: walk data-anchor-row-id back to the
// owning message's anchor scene and rebuild the tool call from the matching row.
function _transparentToolCallFromRowDataset(row){
  try{
    const rowId=row.getAttribute('data-anchor-row-id')||'';
    // #5966 (Codex F2): resolve the OWNER message by its stamped index, not the
    // turn's first assistant segment — a multi-segment turn's scene is owned by a
    // later segment, so the first-segment lookup recovered the wrong (or no) scene.
    const ownerIdxAttr=row.getAttribute('data-anchor-owner-idx');
    let msg=null;
    if(ownerIdxAttr!==null&&ownerIdxAttr!==''){
      const oi=Number(ownerIdxAttr);
      if(Number.isFinite(oi)) msg=S.messages[oi];
    }
    if(!msg){
      // Fallback: find the assistant segment in this turn that actually owns a scene.
      const turn=row.closest&&row.closest('.assistant-turn');
      const segs=turn?Array.from(turn.querySelectorAll('.assistant-segment[data-msg-idx]')):[];
      for(const seg of segs){
        const i=Number(seg.getAttribute('data-msg-idx'));
        if(Number.isFinite(i)&&S.messages[i]&&S.messages[i]._anchor_activity_scene){ msg=S.messages[i]; break; }
      }
    }
    const scene=msg&&msg._anchor_activity_scene;
    if(!scene||!rowId) return null;
    const rows=_anchorSceneRowsForRendering(scene,{settled:true})||[];
    const match=rows.find(r=>String(r.row_id||r.local_id||'')===rowId&&String(r.role||'')==='tool');
    return match?_anchorSceneToolCallFromRow(match,{settled:true}):null;
  }catch(_){ return null; }
}
function _wireTransparentHeaderToggle(header){
  if(!header) return;
  header.setAttribute('data-transparent-toggle-bound','1');
  header.onclick=function(ev){
    const target=ev&&ev.target;
    if(target&&target.closest&&target.closest('.transparent-event-copy,.transparent-detail-mode,.tool-card-more')) return;
    const card=this.closest('.tool-card,.thinking-card');
    _setTransparentCardOpen(card,!(card&&card.classList.contains('open')));
  };
  header.onkeydown=function(ev){
    if(ev.key!=='Enter'&&ev.key!==' ') return;
    ev.preventDefault();
    const card=this.closest('.tool-card,.thinking-card');
    _setTransparentCardOpen(card,!(card&&card.classList.contains('open')));
  };
  header.setAttribute('role','button');
  header.setAttribute('tabindex','0');
}
function _transparentToolDetailHtml(tc, status){
  const args=tc&&tc.args&&typeof tc.args==='object'?tc.args:{};
  const argEntries=Object.entries(args);
  // The tool name is already shown in the row header and the status is shown as
  // a badge, so don't repeat them as pseudo-args in the body. Only surface a
  // duration meta when present. (Trifecta finding V6 — reduce redundancy.)
  const meta=[];
  if(tc&&tc.duration!==undefined&&tc.duration!==null) meta.push(['duration', String(tc.duration)]);
  const preview=String((tc&&(tc.snippet||tc.preview||tc.result||tc.output))||'').trim();
  const argHtml=[...meta,...argEntries].map(([k,v])=>{
    let sv=typeof v==='string'?v:JSON.stringify(v,null,2);
    // Redact secret-bearing arg values before rendering the transparent Full
    // tab — content args can be long multi-line commands (#4928) whose later
    // lines may carry secrets the short label never showed (#4928 gate).
    if(typeof _redactToolTargetLabel==='function'){ try{ sv=_redactToolTargetLabel(sv); }catch(e){} }
    return `<div class="tool-arg-pair"><span class="tool-arg-key">${esc(String(k))}</span><span class="tool-arg-val">${esc(sv)}</span></div>`;
  }).join('');
  return `<div class="tool-card-detail" data-transparent-detail-mode="full"><div class="transparent-detail-modes" role="tablist"><span class="transparent-detail-mode active" role="tab" tabindex="0" data-mode="full" onclick="_setTransparentDetailMode(this,'full')">Full</span><span class="transparent-detail-mode" role="tab" tabindex="0" data-mode="output" onclick="_setTransparentDetailMode(this,'output')">Output</span></div><div class="tool-card-args">${argHtml}</div>${preview?`<div class="tool-card-result"><pre>${esc(preview)}</pre></div>`:''}</div>`;
}
function _syncTransparentEventControls(turn){
  if(!turn||!isTransparentStream()) return;
  const blocks=_assistantTurnBlocks(turn);
  if(!blocks) return;
  const rows=Array.from(blocks.querySelectorAll(':scope > .transparent-event-row,[data-transparent-event-row="1"]'));
  const mountedToolCount=rows.filter(row=>row.getAttribute('data-event-type')==='tool').length;
  // #5966: when this turn's earlier steps are capped (some prefix rows are not
  // mounted yet), the true tool count is stashed on the turn so the "Trace: N
  // tools" label reflects the whole run, not just what's currently in the DOM.
  // Falls back to the mounted count for uncapped turns and the live path.
  const stashedTotal=Number(turn.getAttribute('data-transparent-total-tool-count'));
  const toolCount=(Number.isFinite(stashedTotal)&&stashedTotal>mountedToolCount)?stashedTotal:mountedToolCount;
  let bar=blocks.querySelector(':scope > .transparent-event-controls');
  if(!rows.length){
    if(bar) bar.remove();
    return;
  }
  if(!bar){
    bar=document.createElement('div');
    bar.className='transparent-event-controls';
    const label=document.createElement('span');
    label.className='transparent-event-controls-label';
    label.setAttribute('data-transparent-tool-count','1');
    const expand=document.createElement('span');
    expand.className='transparent-event-control';
    expand.setAttribute('role','button');
    expand.setAttribute('tabindex','0');
    expand.setAttribute('data-transparent-expand-all','1');
    expand.textContent=t('expand_all')||'Expand all';
    expand.onclick=function(ev){ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),true);};
    expand.onkeydown=function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),true);}};
    const collapse=document.createElement('span');
    collapse.className='transparent-event-control';
    collapse.setAttribute('role','button');
    collapse.setAttribute('tabindex','0');
    collapse.setAttribute('data-transparent-collapse-all','1');
    collapse.textContent=t('collapse_all')||'Collapse all';
    collapse.onclick=function(ev){ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),false);};
    collapse.onkeydown=function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),false);}};
    bar.appendChild(label);
    bar.appendChild(expand);
    bar.appendChild(collapse);
    // Guard: firstChild may be null (empty blocks) or orphaned from a prior
    // DOM rebuild. Only insertBefore when it is still a child of blocks.
    if(blocks.firstChild&&blocks.firstChild.parentNode===blocks) blocks.insertBefore(bar, blocks.firstChild);
    else blocks.appendChild(bar);
  }
  const expand=bar.querySelector('[data-transparent-expand-all]');
  if(expand){
    expand.onclick=function(ev){ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),true);};
    expand.onkeydown=function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),true);}};
  }
  const collapse=bar.querySelector('[data-transparent-collapse-all]');
  if(collapse){
    collapse.onclick=function(ev){ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),false);};
    collapse.onkeydown=function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();ev.stopPropagation();_setTransparentRowsExpanded(this.closest('.assistant-turn'),false);}};
  }
  const label=bar.querySelector('.transparent-event-controls-label');
  if(label){
    label.textContent=_transparentEventCountLabel(toolCount);
    label.setAttribute('data-transparent-tool-count',String(toolCount));
  }
  bar.setAttribute('data-tool-count',String(toolCount));
  // Wire the Hermes chat name tag toggle for the live turn.
  _wireTransparentTurnToggle(turn);
  // Apply recency fade so the newest activity stands out while streaming. The
  // fade helper is internally gated to the live turn, so this no-ops on settled
  // turns — without this call the fade feature stayed dormant (only ever cleared
  // from the settled render loop). (Trifecta r2 follow-up.)
  _applyTransparentRowFading(turn);
}
function _rehydrateTransparentStreamDom(root){
  if(!root||!isTransparentStream()) return;
  // Handle BOTH a container root and a root that IS itself an assistant turn
  // (the live-turn restore path passes the #liveAssistantTurn element directly,
  // which querySelectorAll('.assistant-turn') would not match). (Trifecta C1 r2.)
  const turns=[];
  if(root.matches&&root.matches('.assistant-turn')) turns.push(root);
  root.querySelectorAll('.assistant-turn').forEach(turn=>turns.push(turn));
  turns.forEach(turn=>{
    _wireTransparentTurnToggle(turn);
    _syncTransparentEventControls(turn);
  });
  root.querySelectorAll('.transparent-event-row').forEach(row=>{
    const card=row.querySelector('.tool-card,.thinking-card');
    const header=row.querySelector('.tool-card-header,.thinking-card-header');
    if(header){
      _wireTransparentHeaderToggle(header);
      _attachCopyButton(header);
    }
    if(card) _setTransparentCardOpen(card,card.classList.contains('open'));
  });
  // #5966: re-wire the "Show earlier steps" affordance. Its click handler was
  // added via addEventListener and is lost when the session HTML-cache restores
  // innerHTML; the DOM + data-earlier-count survive, so rebind by walking back to
  // the owning message/segment. _revealTransparentEarlierSteps recovers the rows
  // from the scene, so no JS-property stash is needed.
  root.querySelectorAll('.transparent-earlier-steps[data-anchor-earlier-steps="1"]').forEach(el=>{
    if(el.getAttribute('data-earlier-rewired')==='1') return;
    el.setAttribute('data-earlier-rewired','1');
    // #5966 (Codex F2): rebind to the OWNER message by stamped index (multi-segment
    // turns own the scene on a later segment, not the first).
    const turn=el.closest&&el.closest('.assistant-turn');
    const ownerIdxAttr=el.getAttribute('data-anchor-owner-idx');
    let idx=(ownerIdxAttr!==null&&ownerIdxAttr!=='')?Number(ownerIdxAttr):NaN;
    let msg=Number.isFinite(idx)?S.messages[idx]:null;
    let seg=(turn&&Number.isFinite(idx))?turn.querySelector('.assistant-segment[data-msg-idx="'+idx+'"]'):null;
    if(!msg||!msg._anchor_activity_scene||!seg){
      // Fallback: the scene-owning segment in this turn.
      const segs=turn?Array.from(turn.querySelectorAll('.assistant-segment[data-msg-idx]')):[];
      for(const s of segs){
        const i=Number(s.getAttribute('data-msg-idx'));
        if(Number.isFinite(i)&&S.messages[i]&&S.messages[i]._anchor_activity_scene){ idx=i; msg=S.messages[i]; seg=s; break; }
      }
    }
    if(!msg||!seg){ return; }
    const handler=()=>_revealTransparentEarlierSteps(msg,seg,idx,el);
    el.addEventListener('click',handler);
    el.addEventListener('keydown',(ev)=>{ if(ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); el.click(); } });
  });
}
function _decorateTransparentEventRow(row, opts){
  if(!row) return row;
  opts=opts||{};
  const type=String(opts.type||row.getAttribute('data-event-type')||'event');
  row.classList.add('transparent-event-row');
  row.setAttribute('data-transparent-event-row','1');
  row.setAttribute('data-transparent-stream','1');
  row.setAttribute('data-event-type',type);
  if(opts.name) row.setAttribute('data-event-name',String(opts.name));
  if(opts.segmentSeq) row.setAttribute('data-live-segment-seq',String(opts.segmentSeq));
  if(opts.burstId) row.setAttribute('data-activity-burst-id',String(opts.burstId));
  if(type==='tool'){
    const tc=opts.toolCall||row._tcData||{};
    const name=String(opts.name||tc.name||'tool');
    row.setAttribute('data-tool-name',name);
    const status=String(opts.status||_transparentToolStatus(tc));
    row.setAttribute('data-event-status',status);
    const header=row.querySelector('.tool-card-header');
    const card=row.querySelector('.tool-card');
    if(card) card.classList.add('transparent-event-card');
    if(header){
      const nameEl=header.querySelector('.tool-card-name');
      // The status badge (now legible per V2) already carries "Running", so don't
      // also prefix the name with "Running:" — that's the same redundancy class
      // V6 removed from the detail body. (Trifecta r2 #4.)
      if(nameEl) nameEl.textContent=_toolShortName(name);
      // #4658: restore the collapsed-row inline summary. buildToolCard emits a
      // `.tool-card-preview` span but blanks its text for the common case, and
      // the bare _toolShortName above drops the target the worklog view carries
      // in its action-label name. Populate the preview from a quiet,
      // target-based summary so each collapsed row says what it did without
      // expanding. Idempotent across re-decoration (status updates re-run this).
      const previewEl=header.querySelector('.tool-card-preview');
      if(previewEl){
        const summary=_transparentToolSummary(tc);
        if(summary){
          previewEl.textContent=summary;
          previewEl.removeAttribute('hidden');
        }else{
          previewEl.textContent='';
        }
      }
      let statusEl=header.querySelector('.transparent-event-status');
      if(!statusEl){
        statusEl=document.createElement('span');
        statusEl.className='transparent-event-status';
        const toggle=header.querySelector('.tool-card-toggle');
        // Guard against stale toggle (see thinking-preview fix above).
        if(toggle&&toggle.parentNode===header) header.insertBefore(statusEl,toggle);
        else header.appendChild(statusEl);
      }
      if(status==='Completed'){
        statusEl.textContent='';
        statusEl.removeAttribute('data-status');
      }else{
        statusEl.textContent=status;
        statusEl.setAttribute('data-status',status.toLowerCase());
      }
      row.setAttribute('data-event-status',status);
      // Update the 3D progress bar to reflect the new status.
      const progress=card.querySelector('.transparent-event-progress');
      if(progress){
        if(status==='Completed'||status==='Failed'||status==='Interrupted'){
          progress.removeAttribute('data-progress-running');
          progress.setAttribute('data-progress-percent','100%');
          progress.style.setProperty('--transparent-progress-percent','100%');
        }else if(status==='Running'){
          progress.setAttribute('data-progress-running','1');
          progress.setAttribute('data-progress-percent','60%');
          progress.style.setProperty('--transparent-progress-percent','60%');
        }
      }
      let detail=row.querySelector('.tool-card-detail');
      // #5966: on a SETTLED, COLLAPSED tool row, defer the heavy `.tool-card-detail`
      // body (full tool input/output HTML + Prism/KaTeX/Mermaid post-processing)
      // until first expand. A reasoning-heavy history can carry thousands of settled
      // tool rows; eagerly materializing every detail at load is the Transparent-
      // Stream analogue of the #5860 compact-worklog freeze. NOTE buildToolCard
      // PRE-BUILDS `.tool-card-detail` whenever the tool has args/output (hasDetail),
      // so we must strip that prebuilt body too — a `!detail` guard would skip
      // exactly the heavy rows we need to defer (Codex gate F1). The header (name,
      // preview, status, chevron) stays; a deferred row looks identical collapsed.
      // _materializeTransparentToolDetail() rebuilds on expand from the stashed tool
      // call, or from data-anchor-row-id → owner message scene after the
      // _sessionHtmlCache innerHTML round-trip drops the JS stash (#5839 class).
      // Live and already-open rows keep their detail (about to be read).
      const _deferDetail=card&&opts.settled===true&&!card.classList.contains('open')&&_transparentToolRowHasDetail(tc);
      if(_deferDetail){
        if(detail){ detail.remove(); detail=null; }   // drop any buildToolCard prebuilt body
        if(!header.querySelector('.tool-card-toggle')){
          const toggle=document.createElement('span');
          toggle.className='tool-card-toggle';
          toggle.innerHTML=li('chevron-right',12);
          header.appendChild(toggle);
        }
        card.classList.remove('tool-card-no-detail');  // it DOES have detail (deferred)
        row._deferredToolCall=tc;
        row.setAttribute('data-transparent-detail-deferred','1');
      }else if(!detail&&card){
        card.insertAdjacentHTML('beforeend',_transparentToolDetailHtml(tc,status));
        detail=row.querySelector('.tool-card-detail');
        if(!header.querySelector('.tool-card-toggle')){
          const toggle=document.createElement('span');
          toggle.className='tool-card-toggle';
          toggle.innerHTML=li('chevron-right',12);
          header.appendChild(toggle);
        }
      }
      if(detail&&!detail.querySelector('.transparent-detail-modes')){
        const modes=document.createElement('div');
        modes.className='transparent-detail-modes';
        modes.setAttribute('role','tablist');
        modes.innerHTML=`<span class="transparent-detail-mode active" role="tab" tabindex="0" data-mode="full" onclick="_setTransparentDetailMode(this,'full')">Full</span><span class="transparent-detail-mode" role="tab" tabindex="0" data-mode="output" onclick="_setTransparentDetailMode(this,'output')">Output</span>`;
        // Guard: firstChild may be orphaned from a prior DOM rebuild.
        const firstChild=detail.firstChild;
        if(firstChild&&firstChild.parentNode===detail) detail.insertBefore(modes, firstChild);
        else detail.appendChild(modes);
        detail.setAttribute('data-transparent-detail-mode','full');
      }
      if(typeof _syncTransparentEventTimestamp==='function') _syncTransparentEventTimestamp(row, header, {toolCall:tc, ts:opts.ts, live:opts.live===true});
      _wireTransparentHeaderToggle(header);
      _attachCopyButton(header);
    }
    // Attach the 3D progress bar BEFORE the early return so tool rows
    // also get the bottom bar (the function used to return before
    // reaching the bar attachment, which left tool rows bar-less).
    _attachProgressBar(row, opts);
    return row;
  }
  if(type==='thinking'){
    row.classList.add('transparent-thinking-event');
    row.setAttribute('data-event-name','thinking');
    const card=row.querySelector('.thinking-card');
    const header=row.querySelector('.thinking-card-header');
    if(card) card.classList.add('transparent-event-card');
    if(header){
      const btnRow=header.querySelector('.thinking-card-btn-row');
      const copy=header.querySelector('.thinking-copy-btn,.transparent-event-copy');
      const toggle=header.querySelector('.thinking-card-toggle');
      if(copy&&copy.parentNode!==header) header.appendChild(copy);
      if(toggle&&toggle.parentNode!==header) header.appendChild(toggle);
      if(btnRow&&btnRow.parentNode===header&&!btnRow.children.length) btnRow.remove();
      header.style.flexDirection='row';
      const label=header.querySelector('.thinking-card-label');
      if(label) label.textContent='Thinking';
      let preview=header.querySelector('.transparent-event-preview,.transparent-event-thinking-preview');
      const previewText=_transparentEventPreview(opts.preview||opts.text||row.textContent||'');
      if(previewText){
        if(!preview){
          preview=document.createElement('span');
          preview.className='transparent-event-preview transparent-event-thinking-preview';
          if(label&&label.parentNode===header&&label.nextSibling) header.insertBefore(preview,label.nextSibling);
          else if(label&&label.parentNode===header) header.appendChild(preview);
          else header.appendChild(preview);
        }
        preview.classList.add('transparent-event-thinking-preview');
        preview.textContent=previewText;
      }else if(preview){
        preview.remove();
      }
      if(typeof _syncTransparentEventTimestamp==='function') _syncTransparentEventTimestamp(row, header, {ts:opts.ts, live:opts.live===true});
      _wireTransparentHeaderToggle(header);
      _attachCopyButton(header);
    }
    _attachProgressBar(row, opts);
  }
  return row;
}
// ── 3D progress bar (loading path / follow-up indicator) ───────────
// Each transparent event card has a thin 3D bar at its bottom edge.
// While the underlying tool is running (status === 'Running') the bar
// shows a shimmer animation; once the tool completes the bar fills to
// 100% and stops. The bar doubles as a visual step-break between rows
// in the stack, so the eye reads the stream as discrete steps.
function _attachProgressBar(row, opts){
  if(!row) return;
  opts=opts||{};
  const card=row.querySelector('.tool-card,.thinking-card');
  if(!card) return;
  let bar=card.querySelector('.transparent-event-progress');
  if(!bar){
    bar=document.createElement('div');
    bar.className='transparent-event-progress';
    // Append to the card so it sits flush at the bottom edge.
    card.appendChild(bar);
  }
  // Set the running state from opts or current status.
  const status=String(opts.status||(row.getAttribute('data-event-status')||''));
  const isRunning=(status==='Running'||status==='running');
  const isCompleted=(status==='Completed'||status==='completed'||status==='Failed'||status==='failed'||status==='Interrupted'||status==='interrupted');
  if(isRunning) bar.setAttribute('data-progress-running','1');
  else bar.removeAttribute('data-progress-running');
  if(isCompleted){
    bar.setAttribute('data-progress-percent','100%');
    bar.style.setProperty('--transparent-progress-percent','100%');
  }else if(isRunning){
    bar.setAttribute('data-progress-percent','60%');
    bar.style.setProperty('--transparent-progress-percent','60%');
  }else{
    bar.removeAttribute('data-progress-percent');
    bar.style.removeProperty('--transparent-progress-percent');
  }
}
function _setTransparentRowsExpanded(root, expanded){
  const scope=root||document;
  // #5966: "Expand all" must include a capped turn's hidden earlier steps —
  // reveal them first so expansion genuinely opens the whole run. (Collapse-all
  // leaves the cap as-is; it only closes what's mounted.)
  if(expanded){
    scope.querySelectorAll('.transparent-earlier-steps[data-anchor-earlier-steps="1"]').forEach(el=>{
      if(typeof el.click==='function') el.click();
    });
  }
  scope.querySelectorAll('.transparent-event-row .tool-card,.transparent-event-row .thinking-card').forEach(card=>{
    _setTransparentCardOpen(card,!!expanded);
  });
}

export {
  _worklogDetailsExpandedDefault,
  _applyWorklogDetailsExpandedDefault,
  _worklogDetailTextKey,
  _worklogDetailHashKey,
  _worklogDetailBaseKey,
  _worklogDetailDisclosureIsOpen,
  _worklogDetailScrollableBody,
  _setWorklogDetailDisclosureOpen,
  _worklogDetailDisclosureKeyForElement,
  _captureWorklogDetailDisclosureState,
  _restoreWorklogDetailDisclosureState,
  _thinkingCardHtml,
  isSimplifiedToolCalling,
  _thinkingActivityNode,
  chatActivityMode,
  isTransparentStream,
  isFinalAnswerOnlyMode,
  isCompactWorklogMode,
  _toolShortName,
  _transparentEventPreview,
  _transparentToolStatus,
  _transparentToolSummary,
  _copyEventToClipboard,
  _attachCopyButton,
  _transparentEventCountLabel,
  _setTransparentDetailMode,
  _setTransparentCardOpen,
  _transparentToolRowHasDetail,
  _materializeTransparentToolDetail,
  _transparentToolCallFromRowDataset,
  _wireTransparentHeaderToggle,
  _transparentToolDetailHtml,
  _syncTransparentEventControls,
  _rehydrateTransparentStreamDom,
  _decorateTransparentEventRow,
  _attachProgressBar,
  _setTransparentRowsExpanded,
  _worklogDetailDisclosureSelector,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _worklogDetailsExpandedDefault: { enumerable: true, get: () => _worklogDetailsExpandedDefault, set: (value) => { _worklogDetailsExpandedDefault = value; } },
  _applyWorklogDetailsExpandedDefault: { enumerable: true, get: () => _applyWorklogDetailsExpandedDefault, set: (value) => { _applyWorklogDetailsExpandedDefault = value; } },
  _worklogDetailTextKey: { enumerable: true, get: () => _worklogDetailTextKey, set: (value) => { _worklogDetailTextKey = value; } },
  _worklogDetailHashKey: { enumerable: true, get: () => _worklogDetailHashKey, set: (value) => { _worklogDetailHashKey = value; } },
  _worklogDetailBaseKey: { enumerable: true, get: () => _worklogDetailBaseKey, set: (value) => { _worklogDetailBaseKey = value; } },
  _worklogDetailDisclosureIsOpen: { enumerable: true, get: () => _worklogDetailDisclosureIsOpen, set: (value) => { _worklogDetailDisclosureIsOpen = value; } },
  _worklogDetailScrollableBody: { enumerable: true, get: () => _worklogDetailScrollableBody, set: (value) => { _worklogDetailScrollableBody = value; } },
  _setWorklogDetailDisclosureOpen: { enumerable: true, get: () => _setWorklogDetailDisclosureOpen, set: (value) => { _setWorklogDetailDisclosureOpen = value; } },
  _worklogDetailDisclosureKeyForElement: { enumerable: true, get: () => _worklogDetailDisclosureKeyForElement, set: (value) => { _worklogDetailDisclosureKeyForElement = value; } },
  _captureWorklogDetailDisclosureState: { enumerable: true, get: () => _captureWorklogDetailDisclosureState, set: (value) => { _captureWorklogDetailDisclosureState = value; } },
  _restoreWorklogDetailDisclosureState: { enumerable: true, get: () => _restoreWorklogDetailDisclosureState, set: (value) => { _restoreWorklogDetailDisclosureState = value; } },
  _thinkingCardHtml: { enumerable: true, get: () => _thinkingCardHtml, set: (value) => { _thinkingCardHtml = value; } },
  isSimplifiedToolCalling: { enumerable: true, get: () => isSimplifiedToolCalling, set: (value) => { isSimplifiedToolCalling = value; } },
  _thinkingActivityNode: { enumerable: true, get: () => _thinkingActivityNode, set: (value) => { _thinkingActivityNode = value; } },
  chatActivityMode: { enumerable: true, get: () => chatActivityMode, set: (value) => { chatActivityMode = value; } },
  isTransparentStream: { enumerable: true, get: () => isTransparentStream, set: (value) => { isTransparentStream = value; } },
  isFinalAnswerOnlyMode: { enumerable: true, get: () => isFinalAnswerOnlyMode, set: (value) => { isFinalAnswerOnlyMode = value; } },
  isCompactWorklogMode: { enumerable: true, get: () => isCompactWorklogMode, set: (value) => { isCompactWorklogMode = value; } },
  _toolShortName: { enumerable: true, get: () => _toolShortName, set: (value) => { _toolShortName = value; } },
  _transparentEventPreview: { enumerable: true, get: () => _transparentEventPreview, set: (value) => { _transparentEventPreview = value; } },
  _transparentToolStatus: { enumerable: true, get: () => _transparentToolStatus, set: (value) => { _transparentToolStatus = value; } },
  _transparentToolSummary: { enumerable: true, get: () => _transparentToolSummary, set: (value) => { _transparentToolSummary = value; } },
  _copyEventToClipboard: { enumerable: true, get: () => _copyEventToClipboard, set: (value) => { _copyEventToClipboard = value; } },
  _attachCopyButton: { enumerable: true, get: () => _attachCopyButton, set: (value) => { _attachCopyButton = value; } },
  _transparentEventCountLabel: { enumerable: true, get: () => _transparentEventCountLabel, set: (value) => { _transparentEventCountLabel = value; } },
  _setTransparentDetailMode: { enumerable: true, get: () => _setTransparentDetailMode, set: (value) => { _setTransparentDetailMode = value; } },
  _setTransparentCardOpen: { enumerable: true, get: () => _setTransparentCardOpen, set: (value) => { _setTransparentCardOpen = value; } },
  _transparentToolRowHasDetail: { enumerable: true, get: () => _transparentToolRowHasDetail, set: (value) => { _transparentToolRowHasDetail = value; } },
  _materializeTransparentToolDetail: { enumerable: true, get: () => _materializeTransparentToolDetail, set: (value) => { _materializeTransparentToolDetail = value; } },
  _transparentToolCallFromRowDataset: { enumerable: true, get: () => _transparentToolCallFromRowDataset, set: (value) => { _transparentToolCallFromRowDataset = value; } },
  _wireTransparentHeaderToggle: { enumerable: true, get: () => _wireTransparentHeaderToggle, set: (value) => { _wireTransparentHeaderToggle = value; } },
  _transparentToolDetailHtml: { enumerable: true, get: () => _transparentToolDetailHtml, set: (value) => { _transparentToolDetailHtml = value; } },
  _syncTransparentEventControls: { enumerable: true, get: () => _syncTransparentEventControls, set: (value) => { _syncTransparentEventControls = value; } },
  _rehydrateTransparentStreamDom: { enumerable: true, get: () => _rehydrateTransparentStreamDom, set: (value) => { _rehydrateTransparentStreamDom = value; } },
  _decorateTransparentEventRow: { enumerable: true, get: () => _decorateTransparentEventRow, set: (value) => { _decorateTransparentEventRow = value; } },
  _attachProgressBar: { enumerable: true, get: () => _attachProgressBar, set: (value) => { _attachProgressBar = value; } },
  _setTransparentRowsExpanded: { enumerable: true, get: () => _setTransparentRowsExpanded, set: (value) => { _setTransparentRowsExpanded = value; } },
  _worklogDetailDisclosureSelector: { enumerable: true, get: () => _worklogDetailDisclosureSelector },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
