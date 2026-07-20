import { $ } from './state.js';

const WHATS_NEW_SUMMARY_STORAGE_KEY='hermes-whats-new-generated-summaries';
const WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES=256*1024;

function _isSafeUpdateCompareUrl(url){
  if(!url||!/^https?:\/\//i.test(url)) return false;
  try{
    const parsed=new URL(url);
    return parsed.protocol==='https:'||parsed.protocol==='http:';
  }catch(_){
    return false;
  }
}

function _updateCompareUrl(info){
  if(!info) return null;
  const compareUrl=info.compare_url||null;
  if(compareUrl) return _isSafeUpdateCompareUrl(compareUrl)?compareUrl:null;
  const repo_url=info.repo_url;
  const currentSha=info.current_sha;
  const latestSha=info.latest_sha;
  if(!(repo_url&&currentSha&&latestSha)) return null;
  const fallbackUrl=repo_url+'/compare/'+currentSha+'...'+latestSha;
  return _isSafeUpdateCompareUrl(fallbackUrl)?fallbackUrl:null;
}

function _updateWhatsNewTargets(data){
  const targets=[
    {key:'webui',label:'WebUI',info:data&&data.webui},
    {key:'agent',label:'Agent',info:data&&data.agent},
  ];
  return targets.map((target)=>({
    key:target.key,
    label:target.label,
    info:target.info,
    url:_updateCompareUrl(target.info),
  })).filter((target)=>target.info&&target.info.behind>0&&target.url);
}

function _appendUpdateDiffLinks(container,targets,prefix){
  if(!container) return;
  if(prefix) container.appendChild(document.createTextNode(prefix));
  targets.forEach((target,idx)=>{
    if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent=target.label;
    container.appendChild(link);
  });
}

function _syncUpdateSummaryExpandButton(expanded){
  const btn=$('btnUpdateSummaryExpand');
  if(!btn) return;
  btn.setAttribute('aria-expanded',expanded?'true':'false');
  btn.textContent=expanded?'Collapse summary':'Expand summary';
}

function _hideUpdateSummaryPanel(){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  const toolbar=$('updateSummaryToolbar');
  if(panel){
    panel.style.display='none';
    panel.classList.remove('update-summary-expanded');
  }
  if(toolbar) toolbar.style.display='none';
  _syncUpdateSummaryExpandButton(false);
  if(text) text.textContent='';
  if(links){links.replaceChildren();links.style.display='none';}
}

function toggleUpdateSummaryExpanded(){
  const panel=$('updateSummaryPanel');
  if(!panel||panel.style.display==='none') return;
  const expanded=!panel.classList.contains('update-summary-expanded');
  panel.classList.toggle('update-summary-expanded',expanded);
  _syncUpdateSummaryExpandButton(expanded);
}

function _summaryStorageByteLength(value){
  const text=typeof value==='string'?value:JSON.stringify(value);
  if(text==null) return 0;
  if(typeof TextEncoder==='function') return new TextEncoder().encode(text).length;
  let bytes=0;
  for(const ch of text){
    const code=ch.codePointAt(0);
    bytes+=code<=0x7f?1:(code<=0x7ff?2:(code<=0xffff?3:4));
  }
  return bytes;
}

function _summaryCacheEntriesSortedByRecency(entries){
  return entries.slice().sort((left,right)=>{
    const leftKey=left[0];
    const rightKey=right[0];
    const leftSummary=left[1];
    const rightSummary=right[1];
    const leftUpdatedAt=leftSummary&&leftSummary.updatedAt;
    const rightUpdatedAt=rightSummary&&rightSummary.updatedAt;
    if(typeof leftUpdatedAt==='number'&&typeof rightUpdatedAt==='number'&&leftUpdatedAt!==rightUpdatedAt){
      return rightUpdatedAt-leftUpdatedAt;
    }
    if(typeof leftUpdatedAt==='number') return -1;
    if(typeof rightUpdatedAt==='number') return 1;
    if(leftKey==='webui'&&rightKey!=='webui') return -1;
    if(rightKey==='webui'&&leftKey!=='webui') return 1;
    if(leftKey==='agent'&&rightKey!=='agent') return -1;
    if(rightKey==='agent'&&leftKey!=='agent') return 1;
    return leftKey<rightKey?-1:(leftKey>rightKey?1:0);
  });
}

function _loadStoredUpdateSummaries(){
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  try{
    const raw=sessionStorage.getItem(WHATS_NEW_SUMMARY_STORAGE_KEY);
    if(!raw) return window._whatsNewGeneratedSummaries;
    const stored=JSON.parse(raw);
    if(stored&&typeof stored==='object') window._whatsNewGeneratedSummaries=stored;
  }catch(_e){
    try{sessionStorage.removeItem(WHATS_NEW_SUMMARY_STORAGE_KEY);}catch(_ignore){}
  }
  return window._whatsNewGeneratedSummaries;
}

function _persistGeneratedSummaries(){
  const current=window._whatsNewGeneratedSummaries||{};
  const next={};
  try{
    _summaryCacheEntriesSortedByRecency(Object.entries(current)).forEach((entry)=>{
      const candidate={...next,...Object.fromEntries([entry])};
      if(_summaryStorageByteLength(JSON.stringify(candidate))<=WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES){
        Object.assign(next,Object.fromEntries([entry]));
      }
    });
    window._whatsNewGeneratedSummaries=next;
    sessionStorage.setItem(WHATS_NEW_SUMMARY_STORAGE_KEY,JSON.stringify(next));
  }catch(_e){}
}

function _pruneGeneratedSummaries(data){
  const cache=_loadStoredUpdateSummaries();
  const valid=new Set(_updateWhatsNewTargets(data||{}).map((target)=>target.key));
  let changed=false;
  Object.keys(cache).forEach((key)=>{
    if(!valid.has(key)){delete cache[key];changed=true;}
  });
  if(changed) _persistGeneratedSummaries();
}

function _updateSummarySignature(info){
  if(!info) return '';
  return [info.current_sha||'',info.latest_sha||'',info.behind||0,info.compare_url||''].join('|');
}

function _updateSummaryButtonLabel(target,data){
  const labels=target.key==='webui'
    ? {generate:'Generate WebUI update summary',view:'View generated WebUI update summary',regenerate:'Re-generate WebUI update summary'}
    : {generate:'Generate Agent update summary',view:'View generated Agent update summary',regenerate:'Re-generate Agent update summary'};
  const cache=_loadStoredUpdateSummaries()[target.key];
  const signature=_updateSummarySignature(data&&data[target.key]);
  if(cache&&cache.signature===signature&&cache.payload) return labels.view;
  if(cache&&cache.signature!==signature) return labels.regenerate;
  return labels.generate;
}

function _rememberGeneratedSummary(target,payload,data){
  if(!target) return;
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  window._whatsNewGeneratedSummaries[target]={
    signature:_updateSummarySignature(data&&data[target]),
    payload,
    updatedAt:Date.now(),
  };
  _persistGeneratedSummaries();
}

function _renderUpdateSummaryPanel(payload,data,targetKey){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  const toolbar=$('updateSummaryToolbar');
  if(!panel||!text) return;
  panel.style.display='block';
  panel.classList.remove('update-summary-expanded');
  _syncUpdateSummaryExpandButton(false);
  if(toolbar) toolbar.style.display='flex';
  const sections=Array.isArray(payload&&payload.summary_sections)?payload.summary_sections:null;
  text.replaceChildren();
  if(sections&&sections.length){
    const wrap=document.createElement('div');
    wrap.id='updateSummarySections';
    wrap.style.display='grid';
    wrap.style.gap='8px';
    sections.forEach((section)=>{
      const block=document.createElement('section');
      const title=document.createElement('div');
      title.style.fontWeight='650';
      title.style.marginBottom='3px';
      title.textContent=section.title||'Summary';
      block.appendChild(title);
      const ul=document.createElement('ul');
      ul.style.margin='0';
      ul.style.paddingLeft='18px';
      (Array.isArray(section.items)?section.items:[]).forEach((item)=>{
        const li=document.createElement('li');
        li.textContent=String(item||'').trim();
        if(li.textContent) ul.appendChild(li);
      });
      if(!ul.children.length){
        const li=document.createElement('li');
        li.textContent='No summary details available.';
        ul.appendChild(li);
      }
      block.appendChild(ul);
      wrap.appendChild(block);
    });
    text.appendChild(wrap);
  }else{
    text.textContent=(payload&&payload.summary)||payload||'No summary available.';
  }
  const targets=_updateWhatsNewTargets(data||window._updateData||{}).filter((target)=>!targetKey||target.key===targetKey);
  if(links){
    links.replaceChildren();
    if(targets.length){
      links.style.display='block';
      _appendUpdateDiffLinks(links,targets,'Regular diff comparison: ');
    }else{
      links.style.display='none';
    }
  }
}

async function showWhatsNewSummary(target){
  const data=window._updateData||{};
  const scopedUpdates=target?{[target]:data[target]}:data;
  const cache=target?_loadStoredUpdateSummaries()[target]:null;
  const signature=target?_updateSummarySignature(data[target]):'';
  if(cache&&cache.signature===signature&&cache.payload){
    _renderUpdateSummaryPanel(cache.payload,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
    return;
  }
  _renderUpdateSummaryPanel({summary:'Writing a simple summary…'},data,target);
  try{
    const res=await api('/api/updates/summary',{method:'POST',body:JSON.stringify({updates:scopedUpdates,target:target||null}),timeoutMs:60000});
    _rememberGeneratedSummary(target,res,data);
    _renderUpdateSummaryPanel(res,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
  }catch(e){
    console.warn('[updates] summary failed',e);
    _renderUpdateSummaryPanel({
      summary_sections:[
        {title:"What you'll notice",items:['Could not generate the summary right now.']},
        {title:'Worth knowing',items:['Try again later, or use the comparison links below for the raw update details.']},
      ],
    },data,target);
  }
}

function _renderUpdateWhatsNewLinks(data){
  const options=arguments.length>1&&arguments[1]?arguments[1]:{};
  const container=$('updateWhatsNewLinks');
  if(!container) return;
  container.replaceChildren();
  const targets=_updateWhatsNewTargets(data);
  if(!targets.length){
    container.style.display='none';
    _hideUpdateSummaryPanel();
    return;
  }
  container.style.display='block';
  _pruneGeneratedSummaries(data);
  const useSummary=(options.mode||'')==='summary'||window._whatsNewSummaryEnabled===true;
  if(useSummary){
    targets.forEach((target,idx)=>{
      if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='linklike';
      btn.style.color='var(--accent)';
      btn.style.textDecoration='underline';
      btn.style.background='none';
      btn.style.border='0';
      btn.style.padding='0';
      btn.style.cursor='pointer';
      btn.textContent=_updateSummaryButtonLabel(target,data);
      btn.onclick=()=>showWhatsNewSummary(target.key);
      container.appendChild(btn);
    });
    return;
  }
  _hideUpdateSummaryPanel();
  if(targets.length===1){
    const target=targets[0];
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent="What's new in "+target.label+'?';
    container.appendChild(link);
    return;
  }
  _appendUpdateDiffLinks(container,targets,"What's new: ");
}

export {
  WHATS_NEW_SUMMARY_STORAGE_KEY,
  WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES,
  _appendUpdateDiffLinks,
  _hideUpdateSummaryPanel,
  _isSafeUpdateCompareUrl,
  _loadStoredUpdateSummaries,
  _persistGeneratedSummaries,
  _pruneGeneratedSummaries,
  _rememberGeneratedSummary,
  _renderUpdateSummaryPanel,
  _renderUpdateWhatsNewLinks,
  _summaryCacheEntriesSortedByRecency,
  _summaryStorageByteLength,
  _syncUpdateSummaryExpandButton,
  _updateCompareUrl,
  _updateSummaryButtonLabel,
  _updateSummarySignature,
  _updateWhatsNewTargets,
  showWhatsNewSummary,
  toggleUpdateSummaryExpanded,
};

const compatibilityBindings={};
for(const [name,getter,setter] of [
  ['_isSafeUpdateCompareUrl',()=>_isSafeUpdateCompareUrl,(value)=>{_isSafeUpdateCompareUrl=value;}],
  ['_updateCompareUrl',()=>_updateCompareUrl,(value)=>{_updateCompareUrl=value;}],
  ['_updateWhatsNewTargets',()=>_updateWhatsNewTargets,(value)=>{_updateWhatsNewTargets=value;}],
  ['_appendUpdateDiffLinks',()=>_appendUpdateDiffLinks,(value)=>{_appendUpdateDiffLinks=value;}],
  ['_hideUpdateSummaryPanel',()=>_hideUpdateSummaryPanel,(value)=>{_hideUpdateSummaryPanel=value;}],
  ['_syncUpdateSummaryExpandButton',()=>_syncUpdateSummaryExpandButton,(value)=>{_syncUpdateSummaryExpandButton=value;}],
  ['toggleUpdateSummaryExpanded',()=>toggleUpdateSummaryExpanded,(value)=>{toggleUpdateSummaryExpanded=value;}],
  ['_summaryStorageByteLength',()=>_summaryStorageByteLength,(value)=>{_summaryStorageByteLength=value;}],
  ['_summaryCacheEntriesSortedByRecency',()=>_summaryCacheEntriesSortedByRecency,(value)=>{_summaryCacheEntriesSortedByRecency=value;}],
  ['_loadStoredUpdateSummaries',()=>_loadStoredUpdateSummaries,(value)=>{_loadStoredUpdateSummaries=value;}],
  ['_persistGeneratedSummaries',()=>_persistGeneratedSummaries,(value)=>{_persistGeneratedSummaries=value;}],
  ['_pruneGeneratedSummaries',()=>_pruneGeneratedSummaries,(value)=>{_pruneGeneratedSummaries=value;}],
  ['_updateSummarySignature',()=>_updateSummarySignature,(value)=>{_updateSummarySignature=value;}],
  ['_updateSummaryButtonLabel',()=>_updateSummaryButtonLabel,(value)=>{_updateSummaryButtonLabel=value;}],
  ['_rememberGeneratedSummary',()=>_rememberGeneratedSummary,(value)=>{_rememberGeneratedSummary=value;}],
  ['_renderUpdateSummaryPanel',()=>_renderUpdateSummaryPanel,(value)=>{_renderUpdateSummaryPanel=value;}],
  ['showWhatsNewSummary',()=>showWhatsNewSummary,(value)=>{showWhatsNewSummary=value;}],
  ['_renderUpdateWhatsNewLinks',()=>_renderUpdateWhatsNewLinks,(value)=>{_renderUpdateWhatsNewLinks=value;}],
]){
  Object.defineProperty(compatibilityBindings,name,{enumerable:true,get:getter,set:setter});
}
Object.defineProperties(compatibilityBindings,{
  WHATS_NEW_SUMMARY_STORAGE_KEY:{enumerable:true,get:()=>WHATS_NEW_SUMMARY_STORAGE_KEY},
  WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES:{enumerable:true,get:()=>WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES},
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
