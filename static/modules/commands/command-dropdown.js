import {
  _activeSlashCommandOffset,
  getSlashAutocompleteMatches,
} from './slash-autocomplete.js';
import {
  loadAgentCommandMetadata,
  loadBundleCommands,
  loadSkillCommands,
  remoteCommandLoadState,
} from './remote-command-catalog.js';

let _cmdSelectedIdx=-1;

function showCmdDropdown(matches){
  const dropdown=$('cmdDropdown');
  if(!dropdown)return;
  dropdown.innerHTML='';
  _cmdSelectedIdx=matches.length?0:-1;
  for(let index=0;index<matches.length;index++){
    const c=matches[index];
    const element=document.createElement('div');
    element.className='cmd-item';
    if(index===_cmdSelectedIdx)element.classList.add('selected');
    element.dataset.idx=index;
    const isSubArg=c.source==='subarg';
    const isPath=c.source==='path';
    const usage=(!isSubArg&&c.arg)
      ?` <span class="cmd-item-arg">${esc(c.arg)}</span>`
      :'';
    const badge=c.source==='skill'
      ?` <span class="cmd-item-badge cmd-item-badge-skill">${esc(t('slash_skill_badge'))}</span>`
      :c.source==='bundle'
      ?' <span class="cmd-item-badge">Bundle</span>'
      :'';
    if(c.source==='skill')element.classList.add('cmd-item-skill');
    if(isPath)element.classList.add('cmd-item-path');
    const nameHtml=isPath
      ?`<div class="cmd-item-name"><span class="cmd-item-path-value">${esc(c.value)}</span></div>`
      :isSubArg
      ?`<div class="cmd-item-name"><span class="cmd-item-parent">/${esc(c.parent)}</span> <span class="cmd-item-subarg">${esc(c.value)}</span></div>`
      :`<div class="cmd-item-name">/${esc(c.name)}${usage}${badge}</div>`;
    element.innerHTML=`${nameHtml}<div class="cmd-item-desc">${esc(c.desc)}</div>`;
    element.onmousedown=event=>{
      event.preventDefault();
      if(isPath){
        const ta=$('msg');
        if(!ta){hideCmdDropdown();return;}
        const start=Number.isFinite(Number(c.tokenStart))?Number(c.tokenStart):ta.selectionStart;
        const end=Number.isFinite(Number(c.tokenEnd))?Number(c.tokenEnd):ta.selectionEnd;
        const path=String(c.value||'');
        const nextPath=path.endsWith('/')?path:`${path}/`;
        const current=String(ta.value||'');
        const safeStart=Math.max(0,Math.min(start,current.length));
        const safeEnd=Math.max(safeStart,Math.min(end,current.length));
        ta.value=current.slice(0,safeStart)+nextPath+current.slice(safeEnd);
        const pos=safeStart+nextPath.length;
        ta.focus();
        ta.setSelectionRange(pos,pos);
        ta.dispatchEvent(new Event('input',{bubbles:true}));
        hideCmdDropdown();
        return;
      }
      const textarea=$('msg');
      const current=String(textarea&&textarea.value||'');
      const slashIndex=_activeSlashCommandOffset(current);
      const prefix=slashIndex>=0?current.slice(0,slashIndex):'';
      const nextValue=prefix+(isSubArg
        ?('/'+c.parent+' '+c.value)
        :('/'+c.name+(c.arg?' ':'')));
      if(textarea)textarea.value=nextValue;
      if(textarea)textarea.focus();
      if(!isSubArg&&c.source!=='skill'&&c.source!=='bundle'&&nextValue.endsWith(' ')){
        getSlashAutocompleteMatches(nextValue).then(matches=>{
          if(($('msg').value||'')!==nextValue)return;
          if(matches.length)showCmdDropdown(matches);
          else hideCmdDropdown();
        });
      }else{
        hideCmdDropdown();
      }
    };
    dropdown.appendChild(element);
  }
  dropdown.classList.add('open');
}

function hideCmdDropdown(){
  const dropdown=$('cmdDropdown');
  if(dropdown)dropdown.classList.remove('open');
  _cmdSelectedIdx=-1;
}

function navigateCmdDropdown(direction){
  const dropdown=$('cmdDropdown');
  if(!dropdown)return;
  const items=dropdown.querySelectorAll('.cmd-item');
  if(!items.length)return;
  items.forEach(element=>element.classList.remove('selected'));
  _cmdSelectedIdx+=direction;
  if(_cmdSelectedIdx<0)_cmdSelectedIdx=items.length-1;
  if(_cmdSelectedIdx>=items.length)_cmdSelectedIdx=0;
  items[_cmdSelectedIdx].classList.add('selected');
  items[_cmdSelectedIdx].scrollIntoView({block:'nearest'});
}

function selectCmdDropdownItem(){
  const dropdown=$('cmdDropdown');
  if(!dropdown)return;
  const items=dropdown.querySelectorAll('.cmd-item');
  if(_cmdSelectedIdx>=0&&_cmdSelectedIdx<items.length){
    items[_cmdSelectedIdx].onmousedown({preventDefault:()=>{}});
  }else if(items.length===1){
    items[0].onmousedown({preventDefault:()=>{}});
  }
  hideCmdDropdown();
}

function refreshSlashCommandDropdown(){
  const textarea=$('msg');
  if(!textarea)return;
  const text=textarea.value||'';
  if(text.indexOf('\n')!==-1||_activeSlashCommandOffset(text)<0){
    hideCmdDropdown();
    return;
  }
  getSlashAutocompleteMatches(text).then(matches=>{
    if(($('msg').value||'')!==text)return;
    if(matches.length)showCmdDropdown(matches);
    else hideCmdDropdown();
  });
}

function ensureSkillCommandsLoadedForAutocomplete(){
  const state=remoteCommandLoadState();
  if(state.skillReady||state.skillLoading)return;
  loadSkillCommands().then(refreshSlashCommandDropdown);
  if(!state.bundleReady&&!state.bundleLoading){
    loadBundleCommands().then(refreshSlashCommandDropdown);
  }
  if(!state.agentReady&&!state.agentLoading){
    loadAgentCommandMetadata().then(refreshSlashCommandDropdown);
  }
}

export {
  ensureSkillCommandsLoadedForAutocomplete,
  hideCmdDropdown,
  navigateCmdDropdown,
  selectCmdDropdownItem,
  showCmdDropdown,
};
