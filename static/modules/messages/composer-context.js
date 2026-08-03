import { autoResize } from './approvals.js';

let _selectedTextReplyBtn=null;
let _selectedTextReplyText='';
export let _pendingSelections=[];  // [{id, name, text}] — named context blocks
let _selectionIdCounter=0;
let _selectedTextReplyRaf=0;
const _persistentStateToastSeen=new Set();

// Read-only compatibility hook for the still-classic composer UI.
if(typeof window!=='undefined'){
  window._hasPendingSelections=()=>_pendingSelections.length>0;
}

function _persistentToastText(value){
  if(value===null||value===undefined)return '';
  if(typeof value==='string')return value;
  try{return JSON.stringify(value);}catch(_){return String(value||'');}
}

function _persistentToastToolName(tool){
  return String(tool&&tool.name||'').trim();
}

function _persistentToastArgs(tool){
  const args=tool&&tool.args;
  return args&&typeof args==='object'?args:{};
}

function _persistentToastPreview(tool){
  return [
    _persistentToastText(tool&&tool.preview),
    _persistentToastText(tool&&tool.snippet),
  ].filter(Boolean).join('\n');
}

function _persistentToastHasWriteIntent(name, text){
  const nameWords=String(name||'').replace(/_/g,' ');
  const haystack=`${nameWords}\n${text}`.toLowerCase();
  if(/\b(read|list|view|search|lookup|get|fetch|load|usage|toggle|delete|remove)\b/.test(nameWords))return false;
  if(/\b(no|not|nothing)\s+(?:was\s+)?(?:saved|updated|created|written|stored|changed)\b/.test(haystack))return false;
  if(/\b(?:unchanged|skipped|dry[- ]run|failed|error)\b/.test(haystack))return false;
  return /\b(save|saved|write|wrote|written|update|updated|create|created|store|stored|persist|persisted|remember|remembered)\b/.test(haystack);
}

function _persistentToastSkillName(tool){
  const args=_persistentToastArgs(tool);
  const raw=args.name||args.skill_name||args.skill||args.title||'';
  const direct=String(raw||'').trim();
  if(direct)return direct;
  const text=_persistentToastPreview(tool);
  const match=text.match(/\bskill(?:\s+updated|\s+created|\s+saved)?\s*[:=]\s*["'`]?([A-Za-z0-9_.-]{2,80})/i);
  return match?match[1]:'';
}

export function _maybeNotifyPersistentStateSaved(tool){
  if(!tool||tool.is_error||typeof showToast!=='function')return;
  const name=_persistentToastToolName(tool);
  if(!name)return;
  const nameKey=name.toLowerCase().replace(/[^a-z0-9]+/g,'_');
  const preview=_persistentToastPreview(tool);
  const argsText=_persistentToastText(_persistentToastArgs(tool));
  const text=`${preview}\n${argsText}`;
  if(!_persistentToastHasWriteIntent(nameKey, text))return;

  const nameWords=nameKey.replace(/_/g,' ');
  const isSkill=/\bskills?\b/.test(nameWords);
  const isMemory=/\b(memory|memories|remember|profile)\b/.test(nameWords);
  if(!isSkill&&!isMemory)return;
  const skillName=isSkill?_persistentToastSkillName(tool):'';
  if(isSkill&&!skillName)return;
  _showPersistentStateToast(isSkill?'skill':'memory', skillName, {
    created: isSkill&&/\b(create|created|new)\b/.test(`${nameKey}\n${preview}`.toLowerCase()),
  });
}

export function _showPersistentStateToast(kind, name, options){
  if(typeof showToast!=='function')return;
  const normalizedKind=String(kind||'').toLowerCase();
  if(normalizedKind!=='skill'&&normalizedKind!=='memory')return;
  const itemName=String(name||'').trim();
  const dedupeKey=[
    S&&S.session&&S.session.session_id||'',
    normalizedKind,
    itemName||'memory',
  ].join(':');
  if(_persistentStateToastSeen.has(dedupeKey))return;
  _persistentStateToastSeen.add(dedupeKey);
  if(_persistentStateToastSeen.size>200){
    const first=_persistentStateToastSeen.values().next().value;
    _persistentStateToastSeen.delete(first);
  }

  if(normalizedKind==='skill'){
    const base=options&&options.created?t('skill_created'):t('skill_updated');
    showToast(itemName?`${base}: ${itemName}`:base,4200,'success');
    return;
  }
  showToast(t('memory_saved'),3600,'success');
}

function _selectedTextReplyT(key, fallback){
  try{
    const val=(typeof t==='function')?t(key):'';
    return val&&val!==key?val:fallback;
  }catch(_err){
    return fallback;
  }
}

function _selectedTextReplyRoot(){
  if(typeof $==='function') return $('messages')||$('msgInner');
  return document.getElementById('messages')||document.getElementById('msgInner');
}

function _selectedTextReplyNodeInChat(node, root){
  if(!node||!root)return false;
  const el=node.nodeType===Node.ELEMENT_NODE?node:node.parentElement;
  return !!(el&&root.contains(el));
}

function _selectedTextReplySelection(){
  if(!window.getSelection)return null;
  const selection=window.getSelection();
  if(!selection||selection.isCollapsed||!selection.rangeCount)return null;
  const root=_selectedTextReplyRoot();
  if(!root)return null;
  const range=selection.getRangeAt(0);
  if(!_selectedTextReplyNodeInChat(range.startContainer, root)||!_selectedTextReplyNodeInChat(range.endContainer, root))return null;
  const text=selection.toString().replace(/\u00a0/g,' ').trim();
  if(!text)return null;
  const rect=range.getBoundingClientRect();
  if(!rect||(!rect.width&&!rect.height))return null;
  return {text, rect};
}

function _formatSelectedTextReplyQuote(text){
  const normalized=String(text||'').replace(/\r\n?/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  if(!normalized)return '';
  return `<!-- hermes-selected-context -->\n${normalized.split('\n').map(line=>`> ${line}`).join('\n')}`;
}

export function insertSavedPromptIntoComposer(text){
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  if(!composer||!text)return;
  const current=String(composer.value||'');
  composer.value=current.trim()?`${current.replace(/\s+$/,'')}\n\n${text}\n\n`:`${text}\n\n`;
  composer.focus();
  try{composer.setSelectionRange(composer.value.length, composer.value.length);}catch(_e){}
  composer.dispatchEvent(new Event('input',{bubbles:true}));
  if(typeof autoResize==='function') autoResize();
}

let _savedPromptsCache=null;

async function _loadSavedPrompts(){
  try{
    const data=await api('/api/prompts');
    _savedPromptsCache=Array.isArray(data&&data.prompts)?data.prompts:[];
  }catch(_e){_savedPromptsCache=[];}
  return _savedPromptsCache;
}

export async function toggleSavedPromptsPopup(){
  const popup=(typeof $==='function'&&$('savedPromptsPopup'))||document.getElementById('savedPromptsPopup');
  const btn=(typeof $==='function'&&$('btnSavedPrompts'))||document.getElementById('btnSavedPrompts');
  if(!popup)return;
  if(popup.style.display!=='none'){
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
    return;
  }
  popup.innerHTML='<div class="saved-prompts-loading">Loading…</div>';
  popup.style.display='flex';
  if(btn)btn.setAttribute('aria-expanded','true');
  const prompts=await _loadSavedPrompts();
  popup.innerHTML='';
  if(!prompts.length){
    const empty=document.createElement('div');
    empty.className='saved-prompts-empty';
    empty.textContent=(typeof t==='function'&&t('saved_prompts_empty'))||'No saved prompts yet.';
    popup.appendChild(empty);
  }else{
    for(const p of prompts){
      const row=document.createElement('div');
      row.className='saved-prompt-row';
      row.setAttribute('role','menuitem');
      const label=document.createElement('span');
      label.className='saved-prompt-label';
      label.textContent=p.label||p.text;
      label.title=p.text;
      row.onclick=()=>{
        insertSavedPromptIntoComposer(p.text);
        popup.style.display='none';
        if(btn)btn.setAttribute('aria-expanded','false');
      };
      const del=document.createElement('button');
      del.className='saved-prompt-delete';
      del.type='button';
      del.title=(typeof t==='function'&&t('saved_prompts_delete'))||'Delete';
      del.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      del.onclick=async(e)=>{
        e.stopPropagation();
        try{await api('/api/prompts',{method:'DELETE',body:JSON.stringify({id:p.id})});}catch(_e){}
        _savedPromptsCache=null;
        await toggleSavedPromptsPopup();
        await toggleSavedPromptsPopup();
      };
      row.appendChild(label);
      row.appendChild(del);
      popup.appendChild(row);
    }
  }
  const addRow=document.createElement('div');
  addRow.className='saved-prompt-add-row';
  const saveBtn=document.createElement('button');
  saveBtn.type='button';
  saveBtn.className='saved-prompt-save-btn';
  saveBtn.textContent=(typeof t==='function'&&t('saved_prompts_save_current'))||'Save current input';
  saveBtn.onclick=async()=>{
    const msgEl=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
    const text=(msgEl&&msgEl.value||'').trim();
    if(!text){
      if(typeof showToast==='function') showToast((typeof t==='function'&&t('saved_prompts_empty_input'))||'Type a prompt first',2000,'error');
      return;
    }
    try{await api('/api/prompts',{method:'POST',body:JSON.stringify({text})});}catch(_e){
      if(typeof showToast==='function') showToast(_e&&_e.message||'Failed to save prompt',2000,'error');
      return;
    }
    _savedPromptsCache=null;
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
    if(typeof showToast==='function') showToast((typeof t==='function'&&t('saved_prompts_saved'))||'Prompt saved',1600);
  };
  addRow.appendChild(saveBtn);
  popup.appendChild(addRow);
}

document.addEventListener('click',(e)=>{
  const popup=(typeof $==='function'&&$('savedPromptsPopup'))||document.getElementById('savedPromptsPopup');
  const btn=(typeof $==='function'&&$('btnSavedPrompts'))||document.getElementById('btnSavedPrompts');
  if(!popup||popup.style.display==='none')return;
  if(!popup.contains(e.target)&&e.target!==btn&&!(btn&&btn.contains(e.target))){
    popup.style.display='none';
    if(btn)btn.setAttribute('aria-expanded','false');
  }
},{capture:false});
function _addNamedContextBlock(text){
  const id='ctx-'+(++_selectionIdCounter);
  const name=(_selectedTextReplyT('context_block_name_default','Context'))+' '+_selectionIdCounter;
  _pendingSelections.push({id, name, text});
  _renderSelectionChips();
  return id;
}

function _removeNamedContextBlock(id){
  _pendingSelections=_pendingSelections.filter(s=>s.id!==id);
  if(!_pendingSelections.length)_selectionIdCounter=0;
  _renderSelectionChips();
}

export function _clearPendingSelections(){
  _selectionIdCounter=0;
  if(!_pendingSelections.length)return false;
  _pendingSelections=[];
  _renderSelectionChips();
  return true;
}
if(typeof window!=='undefined') window._clearPendingSelections=_clearPendingSelections;

function _selectedContextPreview(text){
  const normalized=String(text||'').replace(/\r\n?/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  if(!normalized)return '';
  const max=360;
  return normalized.length>max?normalized.slice(0,max).trimEnd()+'…':normalized;
}

function _renderSelectionChips(){
  const wrap=document.getElementById('composerSelectionChips');
  if(!wrap)return;
  wrap.innerHTML='';
  wrap.hidden=!_pendingSelections.length;
  _pendingSelections.forEach(s=>{
    const card=document.createElement('article');
    card.className='selection-context-card';
    card.dataset.selectionId=s.id;
    card.setAttribute('aria-label', s.name);

    const accent=document.createElement('div');
    accent.className='selection-context-accent';
    accent.setAttribute('aria-hidden','true');

    const body=document.createElement('div');
    body.className='selection-context-body';

    const header=document.createElement('div');
    header.className='selection-context-header';

    const name=document.createElement('button');
    name.type='button';
    name.className='selection-context-name selection-chip-name';
    name.textContent=s.name;
    name.title=_selectedTextReplyT('context_block_rename_hint','Click or press Enter to rename');
    name.setAttribute('aria-label', `${_selectedTextReplyT('context_block_rename_aria','Rename context block')}: ${s.name}`);
    name.addEventListener('click',()=>_editSelectionChipName(s.id,card));
    name.addEventListener('dblclick',()=>_editSelectionChipName(s.id,card));
    name.addEventListener('keydown',e=>{
      if(e.key==='Enter'||e.key===' '||e.key==='F2'){
        e.preventDefault();
        _editSelectionChipName(s.id,card);
      }
    });

    const remove=document.createElement('button');
    remove.type='button';
    remove.className='selection-context-remove selection-chip-remove';
    remove.setAttribute('aria-label', `${_selectedTextReplyT('context_block_remove','Remove context block')}: ${s.name}`);
    remove.innerHTML='&#x2715;';
    remove.addEventListener('click',()=>_removeNamedContextBlock(s.id));

    const quote=document.createElement('blockquote');
    quote.className='selection-context-quote';
    quote.textContent=_selectedContextPreview(s.text);
    quote.title=String(s.text||'');

    header.appendChild(name);
    header.appendChild(remove);
    body.appendChild(header);
    body.appendChild(quote);
    card.appendChild(accent);
    card.appendChild(body);
    wrap.appendChild(card);
  });
  // #4380: pending selection cards are content the primary Send button must
  // recognize (they were moved out of the textarea into _pendingSelections),
  // so refresh the button's enabled/disabled state whenever the set changes —
  // otherwise a selection-only reply can't be sent via click/tap/mobile.
  if(typeof updateSendBtn==='function') updateSendBtn();
}

function _editSelectionChipName(id,chip){
  const s=_pendingSelections.find(x=>x.id===id);
  if(!s)return;
  const nameEl=chip.querySelector('.selection-chip-name');
  if(!nameEl)return;
  if(chip.querySelector('.selection-chip-edit'))return;
  const inp=document.createElement('input');
  inp.type='text';inp.value=s.name;inp.className='selection-chip-edit';
  inp.maxLength=120;
  inp.setAttribute('aria-label', `${_selectedTextReplyT('context_block_rename_aria','Rename context block')}: ${s.name}`);
  inp.title=_selectedTextReplyT('context_block_rename_hint','Click or press Enter to rename');
  nameEl.replaceWith(inp);
  inp.focus();inp.select();
  let done=false;
  const restoreFocus=()=>{
    window.requestAnimationFrame(()=>{
      const safeId=window.CSS&&CSS.escape?CSS.escape(id):String(id).replace(/"/g,'\\"');
      const next=document.querySelector(`[data-selection-id="${safeId}"] .selection-chip-name`);
      if(next&&typeof next.focus==='function')next.focus({preventScroll:true});
    });
  };
  const commit=()=>{ if(done)return; done=true; s.name=(inp.value.trim()||s.name).slice(0,120); _renderSelectionChips(); restoreFocus(); };
  const cancel=()=>{ if(done)return; done=true; _renderSelectionChips(); restoreFocus(); };
  inp.addEventListener('blur',commit);
  inp.addEventListener('keydown',e=>{ if(e.key==='Enter'){e.preventDefault();commit();} if(e.key==='Escape'){cancel();} });
}

export function _composerTextWithPendingSelections(){
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  const current=String(composer&&composer.value||'');
  if(!_pendingSelections.length)return current;
  const blocks=_pendingSelections.map(s=>`**${s.name}:**\n${_formatSelectedTextReplyQuote(s.text)}`).join('\n\n');
  return current.trim()?`${current.replace(/\s+$/,'')}\n\n${blocks}\n\n`:`${blocks}\n\n`;
}

export function _clearComposerAfterQueuedSelectionSend(){
  const sid=arguments.length?arguments[0]:(S.session&&S.session.session_id);
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  const draftText=composer?String(composer.value||''):'';
  const draftFiles=Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];
  if(composer)composer.value='';
  if(sid&&typeof _clearComposerDraft==='function') _clearComposerDraft(sid,draftText,draftFiles);
  _clearPendingSelections();
  if(typeof autoResize==='function') autoResize();
}

export function _flushSelectionBlocksToComposer(){
  if(!_pendingSelections.length)return;
  const composer=(typeof $==='function'&&$('msg'))||document.getElementById('msg');
  if(!composer)return;
  composer.value=_composerTextWithPendingSelections();
  _clearPendingSelections();
  composer.focus();
  try{ composer.setSelectionRange(composer.value.length, composer.value.length); }catch(_e){}
  composer.dispatchEvent(new Event('input',{bubbles:true}));
  if(typeof autoResize==='function') autoResize();
}

function _selectedTextReplyButton(){
  if(_selectedTextReplyBtn)return _selectedTextReplyBtn;
  const btn=document.createElement('button');
  btn.type='button';
  btn.id='selectedTextReplyBtn';
  btn.className='selected-text-reply-btn';
  btn.setAttribute('data-i18n', 'selected_text_reply');
  btn.setAttribute('data-i18n-title', 'selected_text_reply_title');
  btn.setAttribute('data-i18n-aria-label', 'selected_text_reply_title');
  btn.textContent=_selectedTextReplyT('selected_text_reply', 'Reply with selection');
  btn.title=_selectedTextReplyT('selected_text_reply_title', 'Append selected chat text as quoted context');
  btn.setAttribute('aria-label', btn.title);
  btn.addEventListener('mousedown', e=>e.preventDefault());
  btn.addEventListener('click', e=>{
    e.preventDefault();
    if(_selectedTextReplyText){
      _addNamedContextBlock(_selectedTextReplyText);
      _hideSelectedTextReplyButton();
      const selection=window.getSelection&&window.getSelection();
      if(selection&&selection.removeAllRanges)selection.removeAllRanges();
    }
  });
  document.body.appendChild(btn);
  if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
  _selectedTextReplyBtn=btn;
  return btn;
}

function _hideSelectedTextReplyButton(){
  _selectedTextReplyText='';
  if(_selectedTextReplyBtn)_selectedTextReplyBtn.classList.remove('visible');
}

function _positionSelectedTextReplyButton(info){
  const btn=_selectedTextReplyButton();
  _selectedTextReplyText=info.text;
  btn.classList.add('visible');
  const gap=8;
  const btnRect=btn.getBoundingClientRect();
  const width=btnRect.width||150;
  const height=btnRect.height||32;
  const left=Math.min(Math.max(gap, info.rect.left+(info.rect.width/2)-(width/2)), Math.max(gap, window.innerWidth-width-gap));
  const top=Math.max(gap, info.rect.top-height-gap);
  btn.style.left=`${left}px`;
  btn.style.top=`${top}px`;
}

function _updateSelectedTextReplyButton(){
  if(_selectedTextReplyRaf)return;
  _selectedTextReplyRaf=window.requestAnimationFrame(()=>{
    _selectedTextReplyRaf=0;
    const info=_selectedTextReplySelection();
    if(!info){
      _hideSelectedTextReplyButton();
      return;
    }
    _positionSelectedTextReplyButton(info);
  });
}

if(typeof document!=='undefined'){
  document.addEventListener('selectionchange', _updateSelectedTextReplyButton);
  document.addEventListener('mouseup', e=>{
    if(e.target&&e.target.closest&&e.target.closest('.selected-text-reply-btn'))return;
    _updateSelectedTextReplyButton();
  });
  document.addEventListener('keyup', e=>{
    if(e.key&&/Arrow|Shift|Control|Meta|Alt/.test(e.key))_updateSelectedTextReplyButton();
  });
  window.addEventListener('resize', _hideSelectedTextReplyButton);
}
