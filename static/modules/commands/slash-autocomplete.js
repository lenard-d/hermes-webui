import {COMMANDS} from './command-catalog.js';
import {
  getRemoteCommandMatches,
  invalidateSkillCommandCache,
} from './remote-command-catalog.js';

const SLASH_SUBARG_SOURCES={
  model:{desc:t('cmd_model'),subArgs:'models'},
  personality:{desc:t('cmd_personality'),subArgs:'personalities'},
};

let _slashModelCache=null;
let _slashModelCachePromise=null;
let _slashPersonalityCache=null;
let _slashPersonalityCachePromise=null;
let _slashSkillCache=null;
let _slashSkillCachePromise=null;

function _invalidateSlashModelCache(){
  _slashModelCache=null;
  _slashModelCachePromise=null;
}

function _normalizeSlashSubArg(value){
  return String(value||'').trim();
}

function _getSlashModelSubArgsFromDom(){
  const select=$('modelSelect');
  if(!select)return[];
  const values=[];
  for(const option of Array.from(select.options||[])){
    const value=_normalizeSlashSubArg(option.value||option.textContent||'');
    if(value)values.push(value);
  }
  return Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
}

async function _loadSlashModelSubArgs(force=false){
  const domValues=_getSlashModelSubArgsFromDom();
  if(domValues.length&&!force){
    _slashModelCache=domValues;
    return domValues;
  }
  if(_slashModelCache&&!force)return _slashModelCache;
  if(_slashModelCachePromise&&!force)return _slashModelCachePromise;
  _slashModelCachePromise=(async()=>{
    try{
      const data=await api('/api/models');
      const values=[];
      for(const group of (data&&data.groups)||[]){
        for(const model of (group&&group.models)||[]){
          const id=_normalizeSlashSubArg(model&&model.id);
          if(id)values.push(id);
        }
        for(const model of (group&&group.extra_models)||[]){
          const id=_normalizeSlashSubArg(model&&model.id);
          if(id)values.push(id);
        }
      }
      _slashModelCache=Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
      return _slashModelCache;
    }catch(_){
      _slashModelCache=domValues;
      return domValues;
    }finally{
      _slashModelCachePromise=null;
    }
  })();
  return _slashModelCachePromise;
}

async function _loadSlashPersonalitySubArgs(force=false){
  if(_slashPersonalityCache&&!force)return _slashPersonalityCache;
  if(_slashPersonalityCachePromise&&!force)return _slashPersonalityCachePromise;
  _slashPersonalityCachePromise=(async()=>{
    try{
      const data=await api('/api/personalities');
      const values=['none'];
      for(const personality of (data&&data.personalities)||[]){
        const name=_normalizeSlashSubArg(personality&&personality.name);
        if(name)values.push(name);
      }
      _slashPersonalityCache=Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
      return _slashPersonalityCache;
    }catch(_){
      _slashPersonalityCache=['none'];
      return _slashPersonalityCache;
    }finally{
      _slashPersonalityCachePromise=null;
    }
  })();
  return _slashPersonalityCachePromise;
}

async function _loadSlashSkillSubArgs(force=false){
  if(_slashSkillCache&&!force)return _slashSkillCache;
  if(_slashSkillCachePromise&&!force)return _slashSkillCachePromise;
  _slashSkillCachePromise=(async()=>{
    try{
      const data=await api('/api/skills');
      const values=[];
      for(const skill of (data&&data.skills)||[]){
        const name=_normalizeSlashSubArg(skill&&skill.name);
        if(name)values.push(name);
      }
      _slashSkillCache=Array.from(new Set(values)).sort((a,b)=>a.localeCompare(b));
      return _slashSkillCache;
    }catch(_){
      _slashSkillCache=null;
      return[];
    }finally{
      _slashSkillCachePromise=null;
    }
  })();
  return _slashSkillCachePromise;
}

function invalidateSlashSkillCaches(){
  _slashSkillCache=null;
  _slashSkillCachePromise=null;
  invalidateSkillCommandCache();
}

function _getSlashSubArgOptions(spec){
  if(Array.isArray(spec))return Promise.resolve(spec.slice());
  if(spec==='models') return _loadSlashModelSubArgs();
  if(spec==='personalities') return _loadSlashPersonalitySubArgs();
  if(spec==='skills') return _loadSlashSkillSubArgs();
  return Promise.resolve([]);
}

function getMatchingCommands(prefix){
  const query=prefix.toLowerCase();
  const matches=COMMANDS
    .filter(command=>command.name.startsWith(query))
    .map(command=>({...command,source:'builtin'}));
  const seen=new Set(matches.map(command=>command.name));
  for(const [name,spec] of Object.entries(SLASH_SUBARG_SOURCES)){
    if(!name.startsWith(query)||seen.has(name))continue;
    matches.push({name,desc:spec.desc,arg:'name',source:'subarg-command'});
    seen.add(name);
  }
  matches.push(...getRemoteCommandMatches(query,seen));
  return matches;
}

function _activeSlashCommandOffset(text){
  if(!text||text.indexOf('\n')!==-1)return-1;
  for(let i=0;i<text.length;i++){
    if(text[i]!=='/')continue;
    if(i===0)return i;
    if(/\s/.test(text[i-1])){
      if(i+1<text.length&&text[i+1]==='~')continue;
      return i;
    }
  }
  return-1;
}

function _parseSlashAutocomplete(text){
  const slashIndex=_activeSlashCommandOffset(text);
  if(slashIndex<0)return null;
  const raw=text.slice(slashIndex+1);
  const hasSpace=/\s/.test(raw);
  const parts=raw.split(/\s+/);
  const commandName=(parts[0]||'').toLowerCase();
  const command=COMMANDS.find(candidate=>candidate.name===commandName);
  const subArgSource=(command&&command.subArgs)?command:SLASH_SUBARG_SOURCES[commandName];
  if(!hasSpace||!subArgSource)return {kind:'commands',query:raw};
  const argText=raw.slice(commandName.length).replace(/^\s+/,'');
  return {kind:'subargs',command:{name:commandName,desc:subArgSource.desc,subArgs:subArgSource.subArgs},query:argText.toLowerCase(),rawQuery:argText};
}

async function getSlashAutocompleteMatches(text){
  const parsed=_parseSlashAutocomplete(text);
  if(!parsed)return[];
  if(parsed.kind==='commands')return getMatchingCommands(parsed.query);
  const options=await _getSlashSubArgOptions(parsed.command.subArgs);
  return options
    .filter(option=>String(option).toLowerCase().startsWith(parsed.query))
    .map(option=>({
      name:parsed.command.name,
      value:String(option),
      desc:parsed.command.desc,
      source:'subarg',
      parent:parsed.command.name,
    }));
}

function _findComposerPathToken(text,cursor){
  const value=String(text||'');
  const rawCursor=Number(cursor);
  const position=Number.isFinite(rawCursor)?Math.max(0,Math.min(rawCursor,value.length)):value.length;
  let start=position;
  while(start>0&&!/\s/.test(value.charAt(start-1)))start-=1;
  let end=position;
  while(end<value.length&&!/\s/.test(value.charAt(end)))end+=1;
  const prefix=value.slice(start,position);
  if(!prefix.startsWith('~/'))return null;
  return {start,end,prefix};
}

async function getComposerPathAutocompleteMatches(text,cursor){
  const token=_findComposerPathToken(text,cursor);
  if(!token||typeof api!=='function')return[];
  const query=new URLSearchParams({prefix:token.prefix}).toString();
  const data=await api(`/api/workspaces/suggest?${query}`);
  const needle=token.prefix.toLowerCase();
  return ((data&&data.suggestions)||[])
    .map(path=>String(path||''))
    .filter(path=>path&&path.toLowerCase().startsWith(needle))
    .map(path=>({
      name:path,
      value:path,
      desc:'Workspace path',
      source:'path',
      tokenStart:token.start,
      tokenEnd:token.end,
    }));
}

export {
  _activeSlashCommandOffset,
  _findComposerPathToken,
  _invalidateSlashModelCache,
  getComposerPathAutocompleteMatches,
  getMatchingCommands,
  getSlashAutocompleteMatches,
  invalidateSlashSkillCaches,
};
