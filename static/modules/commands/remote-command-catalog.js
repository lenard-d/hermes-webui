import {COMMANDS} from './command-catalog.js';

let _agentCommandCache=null;
let _agentCommandCachePromise=null;
let _agentCommandCacheReady=false;
let _bundleCommandCache=[];
let _bundleCommandLoadPromise=null;
let _bundleCommandCacheReady=false;
let _skillCommandCache=[];
let _skillCommandLoadPromise=null;
let _skillCommandCacheReady=false;

function _skillCommandSlug(name){
  const raw=String(name||'').trim().toLowerCase();
  if(!raw)return'';
  return raw.replace(/[\s_]+/g,'-').replace(/[^a-z0-9-]/g,'').replace(/-{2,}/g,'-').replace(/^-+|-+$/g,'');
}

function _getReservedSlashCommandSlugs(){
  const reserved=new Set(COMMANDS.map(command=>String(command&&command.name||'').trim().toLowerCase()).filter(Boolean));
  for(const cmd of (_agentCommandCache||[])){
    const names=[cmd.name].concat(Array.isArray(cmd&&cmd.aliases)?cmd.aliases:[]);
    for(const name of names){
      const slug=_skillCommandSlug(name);
      if(slug)reserved.add(slug);
    }
  }
  return reserved;
}

function _buildSkillCommandEntry(skill){
  const skillName=String(skill&&skill.name||'').trim();
  const slug=_skillCommandSlug(skillName);
  if(!slug)return null;
  if(_getReservedSlashCommandSlugs().has(slug)) return null;
  return {
    name:slug,
    desc:String(skill&&skill.description||'').trim()||t('slash_skill_desc'),
    source:'skill',
    skillName,
  };
}

function _buildBundleCommandEntry(bundle){
  const slug=_skillCommandSlug(bundle&&bundle.name);
  if(!slug)return null;
  if(_getReservedSlashCommandSlugs().has(slug)) return null;
  const skillCount=Number(bundle&&bundle.skill_count||0);
  return {
    name:slug,
    desc:String(bundle&&bundle.description||'').trim()||'Skill bundle',
    source:'bundle',
    skillCount:Number.isFinite(skillCount)?skillCount:0,
  };
}

async function loadAgentCommandMetadata(force=false){
  if(_agentCommandCacheReady&&!force)return _agentCommandCache||[];
  if(_agentCommandCachePromise&&!force)return _agentCommandCachePromise;
  _agentCommandCachePromise=(async()=>{
    try{
      const data=await api('/api/commands');
      _agentCommandCache=Array.isArray(data&&data.commands)?data.commands:[];
    }catch(_){
      _agentCommandCache=[];
    }finally{
      _agentCommandCacheReady=true;
      _agentCommandCachePromise=null;
    }
    return _agentCommandCache;
  })();
  return _agentCommandCachePromise;
}

async function getAgentCommandMetadata(name){
  const needle=String(name||'').trim().toLowerCase();
  if(!needle)return null;
  const commands=await loadAgentCommandMetadata();
  return commands.find(cmd=>{
    if(String(cmd&&cmd.name||'').toLowerCase()===needle)return true;
    return Array.isArray(cmd&&cmd.aliases)
      &&cmd.aliases.some(a=>String(a||'').toLowerCase()===needle);
  })||null;
}

function cliOnlyCommandResponse(commandName,meta){
  const name=String((meta&&meta.name)||commandName||'').trim();
  const description=String((meta&&meta.description)||'').trim();
  const detail=description?`\n\n${description}`:'';
  let extra='';
  if(name==='browser'){
    extra='\n\nBrowser tools in WebUI must be configured server-side with the agent/browser environment. Once configured, ask the model to use browser tools directly; `/browser` itself only works in `hermes chat`.';
  }
  return `\`/${name}\` is a Hermes CLI-only command and cannot run inside the WebUI.${detail}${extra}`;
}

async function _runAgentCommandTransport(text,_meta){
  const command=String(text||'').trim();
  if(!command)throw new Error('command is required');
  const data=await api('/api/commands/exec',{
    method:'POST',
    body:JSON.stringify({command}),
  });
  return String(data&&data.output||'(no output)');
}

async function executeAgentCommand(text,meta){
  return _runAgentCommandTransport(text,meta);
}

async function executeAgentPluginCommand(text,meta){
  return _runAgentCommandTransport(text,meta);
}

async function resolveBundleCommand(text,_meta){
  const command=String(text||'').trim();
  if(!command)throw new Error('command is required');
  return api('/api/commands/bundles/resolve',{
    method:'POST',
    body:JSON.stringify({command}),
  });
}

async function loadSkillCommands(force=false){
  if(_skillCommandCacheReady&&!force)return _skillCommandCache;
  if(_skillCommandLoadPromise&&!force)return _skillCommandLoadPromise;
  _skillCommandLoadPromise=(async()=>{
    try{
      const data=await api('/api/skills');
      const deduped=new Map();
      for(const skill of (data&&data.skills)||[]){
        const entry=_buildSkillCommandEntry(skill);
        if(entry&&!deduped.has(entry.name))deduped.set(entry.name,entry);
      }
      _skillCommandCache=Array.from(deduped.values()).sort((a,b)=>a.name.localeCompare(b.name));
    }catch(_){
      _skillCommandCache=[];
    }finally{
      _skillCommandCacheReady=true;
      _skillCommandLoadPromise=null;
    }
    return _skillCommandCache;
  })();
  return _skillCommandLoadPromise;
}

async function loadBundleCommands(force=false){
  if(_bundleCommandCacheReady&&!force)return _bundleCommandCache;
  if(_bundleCommandLoadPromise&&!force)return _bundleCommandLoadPromise;
  _bundleCommandLoadPromise=(async()=>{
    try{
      await loadAgentCommandMetadata();
      const data=await api('/api/commands/bundles');
      const deduped=new Map();
      for(const bundle of (data&&data.bundles)||[]){
        const entry=_buildBundleCommandEntry(bundle);
        if(entry&&!deduped.has(entry.name))deduped.set(entry.name,entry);
      }
      _bundleCommandCache=Array.from(deduped.values()).sort((a,b)=>a.name.localeCompare(b.name));
    }catch(_){
      _bundleCommandCache=[];
    }finally{
      _bundleCommandCacheReady=true;
      _bundleCommandLoadPromise=null;
    }
    return _bundleCommandCache;
  })();
  return _bundleCommandLoadPromise;
}

async function getBundleCommandMetadata(name){
  const needle=String(name||'').trim().toLowerCase();
  if(!needle)return null;
  const bundles=await loadBundleCommands();
  return bundles.find(bundle=>String(bundle&&bundle.name||'').toLowerCase()===needle)||null;
}

function getRemoteCommandMatches(q,seen){
  const matches=[];
  const reserved=_getReservedSlashCommandSlugs();
  if('pet'.startsWith(q)&&!seen.has('pet')){
    const petMeta=Array.isArray(_agentCommandCache)
      ?_agentCommandCache.find(command=>String(command&&command.name||'').toLowerCase()==='pet')
      :null;
    matches.push({
      name:'pet',
      desc:String((petMeta&&petMeta.description)||'Desktop Companion command').trim()||'Desktop Companion command',
      source:'agent',
    });
    seen.add('pet');
  }
  for(const cmd of (_agentCommandCache||[])){
    const name=String(cmd&&cmd.name||'').toLowerCase();
    if(!name.startsWith(q)||seen.has(name))continue;
    if(cmd.cli_only&&name!=='pet')continue;
    matches.push({
      name,
      desc:String(cmd&&cmd.description||'').trim()||'Agent command',
      source:cmd.category==='Plugin'?'plugin':'agent',
    });
    seen.add(name);
  }
  if(_agentCommandCacheReady){
    for(const bundle of _bundleCommandCache){
      if(!bundle.name.startsWith(q)||seen.has(bundle.name)||reserved.has(bundle.name))continue;
      matches.push(bundle);
      seen.add(bundle.name);
    }
  }
  for(const skill of _skillCommandCache){
    if(!skill.name.startsWith(q)||seen.has(skill.name)||reserved.has(skill.name))continue;
    matches.push(skill);
    seen.add(skill.name);
  }
  return matches;
}

function invalidateSkillCommandCache(){
  _skillCommandCache=[];
  _skillCommandCacheReady=false;
  _skillCommandLoadPromise=null;
}

function remoteCommandLoadState(){
  return {
    agentReady:_agentCommandCacheReady,
    agentLoading:!!_agentCommandCachePromise,
    bundleReady:_bundleCommandCacheReady,
    bundleLoading:!!_bundleCommandLoadPromise,
    skillReady:_skillCommandCacheReady,
    skillLoading:!!_skillCommandLoadPromise,
  };
}

export {
  _getReservedSlashCommandSlugs,
  cliOnlyCommandResponse,
  executeAgentCommand,
  executeAgentPluginCommand,
  getAgentCommandMetadata,
  getBundleCommandMetadata,
  getRemoteCommandMatches,
  invalidateSkillCommandCache,
  loadAgentCommandMetadata,
  loadBundleCommands,
  loadSkillCommands,
  remoteCommandLoadState,
  resolveBundleCommand,
};
