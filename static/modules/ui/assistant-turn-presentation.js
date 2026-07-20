import { _stripVisibleAssistantEchoFromThinking } from './activity-and-scroll.js';
import { _isMarkerOnlyAssistantCompressionMessage } from './compression-ui.js';
import { $, S, assistantDisplayName, esc } from './state.js';

function msgContent(m){
  // Extract plain text content from a message for filtering
  let c=m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

function _isRecoveryControlMessageText(text){
  const normalized=String(text||'').replace(/\s+/g,' ').trim();
  if(!normalized) return false;
  const systemRecovery=/^\[System:/i.test(normalized)
    && (/continue exactly where you left off/i.test(normalized)
      || /do not retry the same tool call/i.test(normalized));
  const backendRecovery=/^the live worker stopped before this run finished\.?$/i.test(normalized);
  return !!(systemRecovery || backendRecovery);
}
function _isRecoveryControlMessage(m){
  if(!m||m.role==='tool') return false;
  if(m.recovery_control===true) return true;
  // Backward-compat ONLY: strict fully-anchored text match for pre-marker
  // persisted sessions. NOT provider_details_label — a real "Response
  // interrupted" card carries 'Interruption details' and must stay visible.
  return _isRecoveryControlMessageText(msgContent(m)||String(m.content||''));
}
function _assistantAnchorSceneFinalAnswerText(m){
  const scene=m&&m._anchor_activity_scene&&typeof m._anchor_activity_scene==='object'
    ? m._anchor_activity_scene
    : null;
  const text=scene&&typeof scene.final_answer==='string'?scene.final_answer:'';
  return String(text||'').trim()?text:'';
}
function _assistantMessageHasVisibleContent(m){
  if(!m||m.role!=='assistant') return false;
  if(_isRecoveryControlMessage(m)) return false;
  if(_assistantAnchorSceneFinalAnswerText(m)) return true;
  const content=m.content;
  if(typeof content==='string') return !_isAssistantEmptyPlaceholderContent(m, content)&&!!content.trim();
  if(!Array.isArray(content)) return false;
  return content.some(part=>{
    if(typeof part==='string') return !!part.trim();
    if(!part||typeof part!=='object') return false;
    if(part.type==='text'||part.type==='input_text'||part.type==='output_text'){
      return !!String(part.text||part.content||'').trim();
    }
    return false;
  });
}

function _fmtDateSep(d){
  const todayStart=new Date();todayStart.setHours(0,0,0,0);
  const dStart=new Date(d);dStart.setHours(0,0,0,0);
  const diffDays=Math.round((todayStart-dStart)/86400000);
  if(diffDays===0) return 'Today';
  if(diffDays===1) return 'Yesterday';
  if(diffDays>0 && diffDays<7) return dStart.toLocaleDateString([], {weekday:'long'});
  const opts={month:'short', day:'numeric'};
  if(todayStart.getFullYear()!==dStart.getFullYear()) opts.year='numeric';
  return dStart.toLocaleDateString([], opts);
}
const _ERR_MSG_RE=/^(?:\*\*error\b|error:|connection lost|no response received)/i;
function _messageHasReasoningPayload(m){
  if(!m||m.role!=='assistant') return false;
  if(m.reasoning||m.reasoning_content||m.thinking||m._reasoning) return true;
  if(Array.isArray(m.content)) return m.content.some(p=>p&&(p.type==='thinking'||p.type==='reasoning'));
  if(typeof window!=='undefined'&&typeof window._extractInlineThinkingFromContentForRender==='function'){
    const split=window._extractInlineThinkingFromContentForRender(String(m.content||''),'');
    return !!(split&&split.reasoning);
  }
  return /^\s*(?:<think>[\s\S]*?<\/think>|<\|channel\|?>thought\n?[\s\S]*?<channel\|>|<\|turn\|>thinking\n[\s\S]*?<turn\|>)/.test(String(m.content||''));
}
function _isAssistantEmptyPlaceholderContent(m, content){
  if(!m||m.role!=='assistant') return false;
  if(String(content||'').trim()!=='(empty)') return false;
  return _messageHasReasoningPayload(m);
}
function _formatTurnTps(value){
  const n=Number(value);
  if(!Number.isFinite(n)||n<=0) return '';
  const fixed=n>=100?Math.round(n).toLocaleString():n>=10?n.toFixed(1):n.toFixed(1);
  return `${fixed} t/s`;
}
function isTpsDisplayEnabled(){
  return window._showTps===true;
}
function _assistantRoleHtml(tsTitle='', tpsText=''){
  const _bn=assistantDisplayName();
  const tps=(isTpsDisplayEnabled()&&tpsText)?`<span class="msg-tps-inline" title="Tokens per second">${esc(tpsText)}</span>`:'';
  return `<div class="msg-role assistant" ${tsTitle?`title="${esc(tsTitle)}"`:''}><div class="role-icon assistant">${esc(_bn.charAt(0).toUpperCase())}</div><span class="msg-role-name">${esc(_bn)}</span>${tps}</div>`;
}
function _setAssistantTurnTps(turn, tpsText=''){
  if(!turn) return;
  const role=turn.querySelector('.msg-role.assistant');
  if(!role) return;
  let chip=role.querySelector('.msg-tps-inline');
  const text=String(tpsText||'').trim();
  if(!text){if(chip) chip.remove();return;}
  if(!chip){
    chip=document.createElement('span');
    chip.className='msg-tps-inline';
    chip.title='Tokens per second';
    role.appendChild(chip);
  }
  chip.textContent=text;
}
function _setLiveAssistantTps(value){
  _setAssistantTurnTps($('liveAssistantTurn'), isTpsDisplayEnabled()?_formatTurnTps(value):'');
}
function _createAssistantTurn(tsTitle='', tpsText=''){
  const row=document.createElement('div');
  row.className='msg-row assistant-turn';
  row.dataset.role='assistant';
  if(S.session) row.dataset.sessionId=S.session.session_id;
  row.innerHTML=`${_assistantRoleHtml(tsTitle, tpsText)}<div class="assistant-turn-blocks"></div>`;
  return row;
}
function _setLatestAssistantTurnLandmark(turn, isLatest){
  if(!turn) return;
  const label='Latest Hermes response';
  if(isLatest){
    if(typeof document!=='undefined'){
      document.querySelectorAll('.assistant-turn[data-latest-assistant-response="true"]').forEach(el=>{
        if(el!==turn) _setLatestAssistantTurnLandmark(el, false);
      });
    }
    turn.setAttribute('role','region');
    turn.setAttribute('aria-label',label);
    turn.dataset.latestAssistantResponse='true';
    return;
  }
  if(turn.getAttribute('role')==='region') turn.removeAttribute('role');
  if(turn.getAttribute('aria-label')===label) turn.removeAttribute('aria-label');
  delete turn.dataset.latestAssistantResponse;
}
function _assistantTurnBlocks(turn){
  return turn?turn.querySelector('.assistant-turn-blocks'):null;
}
function _assistantMessageBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs, visibleContent, opts){
  if(!m||m.role!=='assistant') return false;
  if(m._error) return false;
  const isTurnFinalAssistant=!!(opts&&opts.isTurnFinalAssistant);
  const visibleText=String(visibleContent!==undefined?visibleContent:msgContent(m)||'').trim();
  const hasVisibleText=!!visibleText&&!_isAssistantEmptyPlaceholderContent(m, visibleText);
  if(m._live) return true;
  if(hasVisibleText&&m._anchor_activity_scene) return false;
  if(hasVisibleText&&isTurnFinalAssistant) return false;
  if(m._activityBurstId!==undefined||m._liveSegmentSeq!==undefined) return true;
  const hasToolMetadata=!!(
    (toolCallAssistantIdxs&&toolCallAssistantIdxs.has(rawIdx))||
    (Array.isArray(m.tool_calls)&&m.tool_calls.length)||
    (Array.isArray(m.content)&&m.content.some(p=>p&&typeof p==='object'&&p.type==='tool_use'))
  );
  if(hasVisibleText) return false;
  if(hasToolMetadata) return true;
  return false;
}
function _assistantThinkingBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs){
  return !!_assistantReasoningPayloadText(m)||_assistantMessageBelongsInWorklog(m, rawIdx, toolCallAssistantIdxs);
}
function _assistantReasoningPayloadText(m){
  if(!m||m.role!=='assistant') return '';
  const direct=m.reasoning_content||m.reasoning||m.thinking||m._reasoning||'';
  if(String(direct||'').trim()) return String(direct).trim();
  if(Array.isArray(m.content)){
    const parts=m.content
      .filter(p=>p&&typeof p==='object'&&(p.type==='thinking'||p.type==='reasoning'))
      .map(p=>p.text||p.content||'')
      .filter(text=>String(text||'').trim());
    return parts.join('\n').trim();
  }
  const text=String(m.content||'');
  if(typeof window!=='undefined'&&typeof window._extractInlineThinkingFromContentForRender==='function'){
    const split=window._extractInlineThinkingFromContentForRender(text,'');
    if(split&&String(split.reasoning||'').trim()) return String(split.reasoning).trim();
  }
  // Extract a LEADING thinking block even when visible answer text follows it
  // (e.g. "<think>…</think>4"). The matching display-content stripper
  // (_stripLeadingAssistantThinkingMarkup) is non-anchored, so the extractor must
  // be too — a trailing `$` anchor here dropped the reasoning whenever the turn
  // also had a visible answer, hiding the Thinking card entirely (#3401 regression
  // vs master, which used the non-anchored form). (#3709/#3592 family)
  const thinkMatch=text.match(/^\s*<think>([\s\S]*?)<\/think>\s*/);
  if(thinkMatch) return thinkMatch[1].trim();
  const thoughtMatch=text.match(/^\s*<\|channel\|?>thought\n?([\s\S]*?)<channel\|>\s*/);
  if(thoughtMatch) return thoughtMatch[1].trim();
  const turnMatch=text.match(/^\s*<\|turn\|>thinking\n([\s\S]*?)<turn\|>\s*/);
  if(turnMatch) return turnMatch[1].trim();
  return '';
}
function _stripLeadingAssistantThinkingMarkup(content){
  let out=String(content||'');
  const thinkMatch=out.match(/^\s*<think>([\s\S]*?)<\/think>\s*/);
  if(thinkMatch) out=out.replace(/^\s*<think>[\s\S]*?<\/think>\s*/,'').trimStart();
  const thoughtMatch=out.match(/^\s*<\|channel\|?>thought\n?([\s\S]*?)<channel\|>\s*/);
  if(thoughtMatch) out=out.replace(/^\s*<\|channel\|?>thought\n?[\s\S]*?<channel\|>\s*/,'').trimStart();
  const turnMatch=out.match(/^\s*<\|turn\|>thinking\n([\s\S]*?)<turn\|>\s*/);
  if(turnMatch) out=out.replace(/^\s*<\|turn\|>thinking\n[\s\S]*?<turn\|>\s*/,'').trimStart();
  return out;
}
function _assistantVisibleContentForReasoningCompare(m){
  if(!m||m.role!=='assistant') return '';
  const anchorFinal=_assistantAnchorSceneFinalAnswerText(m);
  if(anchorFinal) return anchorFinal;
  let content=m.content||'';
  if(Array.isArray(content)){
    content=content.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('\n');
  }
  if(typeof content==='string'){
    if(typeof window!=='undefined'&&typeof window._extractInlineThinkingFromContentForRender==='function'){
      const split=window._extractInlineThinkingFromContentForRender(content,'');
      content=split&&typeof split.content==='string'?split.content:_stripLeadingAssistantThinkingMarkup(content);
    } else {
      content=_stripLeadingAssistantThinkingMarkup(content);
    }
  }
  if(_isMarkerOnlyAssistantCompressionMessage(m)){
    content='**Error:** No response received after context compression. Please retry.';
  }
  if(_isAssistantEmptyPlaceholderContent(m, content)) return '';
  return String(content||'');
}
function _assistantTurnFinalVisibleContentMap(visWithIdx){
  const out=new Map();
  let runIdxs=[];
  let finalVisible='';
  const flush=()=>{
    for(const idx of runIdxs) out.set(idx, finalVisible);
    runIdxs=[];
    finalVisible='';
  };
  for(const entry of visWithIdx||[]){
    const m=entry&&entry.m;
    if(m&&m.role==='assistant'){
      runIdxs.push(entry.rawIdx);
      const visible=_assistantVisibleContentForReasoningCompare(m);
      if(String(visible||'').trim()) finalVisible=visible;
    }else{
      flush();
    }
  }
  flush();
  return out;
}
function _assistantTurnVisibleContentMap(visWithIdx){
  const out=new Map();
  let runIdxs=[];
  let visibleTexts=[];
  const flush=()=>{
    for(const idx of runIdxs) out.set(idx, visibleTexts.slice());
    runIdxs=[];
    visibleTexts=[];
  };
  for(const entry of visWithIdx||[]){
    const m=entry&&entry.m;
    if(m&&m.role==='assistant'){
      runIdxs.push(entry.rawIdx);
      const visible=_assistantVisibleContentForReasoningCompare(m);
      if(String(visible||'').trim()) visibleTexts.push(visible);
    }else{
      flush();
    }
  }
  flush();
  return out;
}
function _worklogReasoningTextFromMessage(m, rawIdx, toolCallAssistantIdxs, visibleContent, turnFinalVisibleContent, turnVisibleContents){
  const thinkingText=_assistantReasoningPayloadText(m);
  const visibleTexts=Array.isArray(turnVisibleContents)?turnVisibleContents:[];
  return _stripVisibleAssistantEchoFromThinking(thinkingText, visibleContent, turnFinalVisibleContent, ...visibleTexts);
}

export {
  msgContent,
  _isRecoveryControlMessageText,
  _isRecoveryControlMessage,
  _assistantAnchorSceneFinalAnswerText,
  _assistantMessageHasVisibleContent,
  _fmtDateSep,
  _messageHasReasoningPayload,
  _isAssistantEmptyPlaceholderContent,
  _formatTurnTps,
  isTpsDisplayEnabled,
  _assistantRoleHtml,
  _setAssistantTurnTps,
  _setLiveAssistantTps,
  _createAssistantTurn,
  _setLatestAssistantTurnLandmark,
  _assistantTurnBlocks,
  _assistantMessageBelongsInWorklog,
  _assistantThinkingBelongsInWorklog,
  _assistantReasoningPayloadText,
  _stripLeadingAssistantThinkingMarkup,
  _assistantVisibleContentForReasoningCompare,
  _assistantTurnFinalVisibleContentMap,
  _assistantTurnVisibleContentMap,
  _worklogReasoningTextFromMessage,
  _ERR_MSG_RE,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  msgContent: { enumerable: true, get: () => msgContent, set: (value) => { msgContent = value; } },
  _isRecoveryControlMessageText: { enumerable: true, get: () => _isRecoveryControlMessageText, set: (value) => { _isRecoveryControlMessageText = value; } },
  _isRecoveryControlMessage: { enumerable: true, get: () => _isRecoveryControlMessage, set: (value) => { _isRecoveryControlMessage = value; } },
  _assistantAnchorSceneFinalAnswerText: { enumerable: true, get: () => _assistantAnchorSceneFinalAnswerText, set: (value) => { _assistantAnchorSceneFinalAnswerText = value; } },
  _assistantMessageHasVisibleContent: { enumerable: true, get: () => _assistantMessageHasVisibleContent, set: (value) => { _assistantMessageHasVisibleContent = value; } },
  _fmtDateSep: { enumerable: true, get: () => _fmtDateSep, set: (value) => { _fmtDateSep = value; } },
  _messageHasReasoningPayload: { enumerable: true, get: () => _messageHasReasoningPayload, set: (value) => { _messageHasReasoningPayload = value; } },
  _isAssistantEmptyPlaceholderContent: { enumerable: true, get: () => _isAssistantEmptyPlaceholderContent, set: (value) => { _isAssistantEmptyPlaceholderContent = value; } },
  _formatTurnTps: { enumerable: true, get: () => _formatTurnTps, set: (value) => { _formatTurnTps = value; } },
  isTpsDisplayEnabled: { enumerable: true, get: () => isTpsDisplayEnabled, set: (value) => { isTpsDisplayEnabled = value; } },
  _assistantRoleHtml: { enumerable: true, get: () => _assistantRoleHtml, set: (value) => { _assistantRoleHtml = value; } },
  _setAssistantTurnTps: { enumerable: true, get: () => _setAssistantTurnTps, set: (value) => { _setAssistantTurnTps = value; } },
  _setLiveAssistantTps: { enumerable: true, get: () => _setLiveAssistantTps, set: (value) => { _setLiveAssistantTps = value; } },
  _createAssistantTurn: { enumerable: true, get: () => _createAssistantTurn, set: (value) => { _createAssistantTurn = value; } },
  _setLatestAssistantTurnLandmark: { enumerable: true, get: () => _setLatestAssistantTurnLandmark, set: (value) => { _setLatestAssistantTurnLandmark = value; } },
  _assistantTurnBlocks: { enumerable: true, get: () => _assistantTurnBlocks, set: (value) => { _assistantTurnBlocks = value; } },
  _assistantMessageBelongsInWorklog: { enumerable: true, get: () => _assistantMessageBelongsInWorklog, set: (value) => { _assistantMessageBelongsInWorklog = value; } },
  _assistantThinkingBelongsInWorklog: { enumerable: true, get: () => _assistantThinkingBelongsInWorklog, set: (value) => { _assistantThinkingBelongsInWorklog = value; } },
  _assistantReasoningPayloadText: { enumerable: true, get: () => _assistantReasoningPayloadText, set: (value) => { _assistantReasoningPayloadText = value; } },
  _stripLeadingAssistantThinkingMarkup: { enumerable: true, get: () => _stripLeadingAssistantThinkingMarkup, set: (value) => { _stripLeadingAssistantThinkingMarkup = value; } },
  _assistantVisibleContentForReasoningCompare: { enumerable: true, get: () => _assistantVisibleContentForReasoningCompare, set: (value) => { _assistantVisibleContentForReasoningCompare = value; } },
  _assistantTurnFinalVisibleContentMap: { enumerable: true, get: () => _assistantTurnFinalVisibleContentMap, set: (value) => { _assistantTurnFinalVisibleContentMap = value; } },
  _assistantTurnVisibleContentMap: { enumerable: true, get: () => _assistantTurnVisibleContentMap, set: (value) => { _assistantTurnVisibleContentMap = value; } },
  _worklogReasoningTextFromMessage: { enumerable: true, get: () => _worklogReasoningTextFromMessage, set: (value) => { _worklogReasoningTextFromMessage = value; } },
  _ERR_MSG_RE: { enumerable: true, get: () => _ERR_MSG_RE },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
