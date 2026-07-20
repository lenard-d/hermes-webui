import { _assistantMessageHasVisibleContent, _assistantReasoningPayloadText } from './assistant-turn-presentation.js';
import { _redactToolTargetLabel } from './tool-worklog.js';

function _clipCliToolSnippet(text, maxLen=20000){
  const s=String(text||'');
  if(s.length<=maxLen) return s;
  return `${s.slice(0,maxLen)}\n\n... truncated ${s.length-maxLen} chars ...`;
}

function _cliToolResultText(raw){
  const s=String(raw||'');
  try{
    const rd=JSON.parse(s);
    if(rd && typeof rd==='object'){
      for(const key of ['output','result','error','content','diff','patch']){
        if(Object.prototype.hasOwnProperty.call(rd,key)){
          const v=rd[key];
          if(v==null) return '';
          return typeof v==='string' ? v : JSON.stringify(v,null,2);
        }
      }
    }
  }catch(e){}
  return s;
}

function _cliLooksLikePatchDiff(text){
  const s=String(text||'');
  if(!s) return false;
  if(/\*\*\* Begin Patch/.test(s)) return true;
  if(/^diff --git /m.test(s)) return true;
  if(/^@@\s/m.test(s)) return true;
  if(/(^|\n)---\s+/.test(s) && /(^|\n)\+\+\+\s+/.test(s)) return true;
  return false;
}

function _cliToolResultSnippet(raw){
  const fullText=_cliToolResultText(raw);
  if(_cliLooksLikePatchDiff(fullText)) return _clipCliToolSnippet(fullText);
  return String(fullText||'').slice(0,4000);
}

function _prefixedCliDiffLines(prefix, value){
  return String(value||'').split('\n').map(line=>`${prefix}${line}`).join('\n');
}

function _firstOwnedValue(obj, keys){
  for(const key of keys){
    if(obj && Object.prototype.hasOwnProperty.call(obj,key)) return obj[key];
  }
  return undefined;
}

function _cliPatchSnippetFromArgs(name, args){
  if(!args || typeof args!=='object') return '';
  const toolName=String(name||'').toLowerCase();
  for(const key of ['patch','diff']){
    const v=args[key];
    if(typeof v==='string' && v.trim()) return _clipCliToolSnippet(v);
  }
  for(const key of ['input','content']){
    const v=args[key];
    if(typeof v==='string' && _cliLooksLikePatchDiff(v)) return _clipCliToolSnippet(v);
  }
  const isEditLike=toolName==='apply_patch'
    || toolName==='patch'
    || toolName.includes('edit')
    || toolName==='replace'
    || toolName==='str_replace';
  if(!isEditLike) return '';
  const oldValue=_firstOwnedValue(args,['old_string','old_str','old','before']);
  const newValue=_firstOwnedValue(args,['new_string','new_str','new','after']);
  if(oldValue!==undefined || newValue!==undefined){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const lines=[];
    if(path) lines.push(path);
    if(oldValue!==undefined) lines.push(_prefixedCliDiffLines('-', oldValue));
    if(newValue!==undefined) lines.push(_prefixedCliDiffLines('+', newValue));
    return _clipCliToolSnippet(lines.join('\n'));
  }
  if(Array.isArray(args.edits)){
    const path=String(_firstOwnedValue(args,['file_path','path','filename'])||'');
    const chunks=[];
    if(path) chunks.push(path);
    args.edits.slice(0,5).forEach(edit=>{
      if(!edit || typeof edit!=='object') return;
      const before=_firstOwnedValue(edit,['old_string','old_str','old','before']);
      const after=_firstOwnedValue(edit,['new_string','new_str','new','after']);
      if(before!==undefined) chunks.push(_prefixedCliDiffLines('-', before));
      if(after!==undefined) chunks.push(_prefixedCliDiffLines('+', after));
    });
    if(chunks.length) return _clipCliToolSnippet(chunks.join('\n'));
  }
  return '';
}

function _cliToolCardSnippet(resultSnippet, patchSnippet){
  if(_cliLooksLikePatchDiff(resultSnippet)) return resultSnippet;
  if(!patchSnippet) return resultSnippet || '';
  const result=String(resultSnippet||'').trim();
  if(!result) return patchSnippet;
  const generic=/^(success|ok|done|done\.|exit code: 0)$/i.test(result);
  if(generic) return patchSnippet;
  return `${resultSnippet}\n\n${patchSnippet}`;
}

function _cliToolCardHasDiffSnippet(resultSnippet, patchSnippet){
  return !!patchSnippet || _cliLooksLikePatchDiff(resultSnippet);
}

function _assistantToolAnchorIdxForMessage(messages, rawIdx){
  const list=Array.isArray(messages)?messages:[];
  const current=list[rawIdx];
  if(_assistantMessageHasVisibleContent(current)) return rawIdx;
  if(_assistantReasoningPayloadText(current)) return rawIdx;
  for(let idx=rawIdx-1;idx>=0;idx--){
    if(_assistantMessageHasVisibleContent(list[idx])) return idx;
  }
  return rawIdx;
}
function _toolArgsSnapshot(args, limit){
  if(!args||typeof args!=='object'||Array.isArray(args)) return {};
  const max=Math.max(1,Number(limit)||6);
  const priority=[
    'query','search_query','searchQuery','pattern','q','keyword','keywords','term',
    'url','uri','command','cmd','path','file','file_path','filename','file_glob',
    'glob','offset','limit',
  ];
  // Content / diff-reconstruction keys must not be capped to the short
  // incidental-arg limit, or long commands/paths get cut and recovery-rebuilt
  // diffs (built from old_string/new_string/patch) break (#4928). Mirrors the
  // backend _TOOL_ARG_CONTENT_KEYS / _TOOL_ARG_CONTENT_CAP.
  const contentKeys=new Set(['command','cmd','script','code','patch','diff','old_string','new_string','content','path','file_path']);
  const CONTENT_CAP=4000;
  const keys=[
    ...priority.filter(k=>Object.prototype.hasOwnProperty.call(args,k)),
    ...Object.keys(args).filter(k=>!priority.includes(k)),
  ].slice(0,max);
  const out={};
  keys.forEach(k=>{
    const v=String(args[k]);
    const cap=contentKeys.has(String(k).toLowerCase())?CONTENT_CAP:120;
    let val=v.slice(0,cap)+(v.length>cap?'...':'');
    // Now that content args are retained up to 4000 chars (#4928), a secret on
    // a non-first line / past char 120 would otherwise reach the args block,
    // the Full tab, and clipboard copy unredacted. Redact at the snapshot so
    // every downstream renderer receives already-masked args (#4928 gate).
    if(typeof _redactToolTargetLabel==='function'){ try{ val=_redactToolTargetLabel(val); }catch(e){} }
    out[k]=val;
  });
  return out;
}

export {
  _clipCliToolSnippet,
  _cliToolResultText,
  _cliLooksLikePatchDiff,
  _cliToolResultSnippet,
  _prefixedCliDiffLines,
  _firstOwnedValue,
  _cliPatchSnippetFromArgs,
  _cliToolCardSnippet,
  _cliToolCardHasDiffSnippet,
  _assistantToolAnchorIdxForMessage,
  _toolArgsSnapshot,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _clipCliToolSnippet: { enumerable: true, get: () => _clipCliToolSnippet, set: (value) => { _clipCliToolSnippet = value; } },
  _cliToolResultText: { enumerable: true, get: () => _cliToolResultText, set: (value) => { _cliToolResultText = value; } },
  _cliLooksLikePatchDiff: { enumerable: true, get: () => _cliLooksLikePatchDiff, set: (value) => { _cliLooksLikePatchDiff = value; } },
  _cliToolResultSnippet: { enumerable: true, get: () => _cliToolResultSnippet, set: (value) => { _cliToolResultSnippet = value; } },
  _prefixedCliDiffLines: { enumerable: true, get: () => _prefixedCliDiffLines, set: (value) => { _prefixedCliDiffLines = value; } },
  _firstOwnedValue: { enumerable: true, get: () => _firstOwnedValue, set: (value) => { _firstOwnedValue = value; } },
  _cliPatchSnippetFromArgs: { enumerable: true, get: () => _cliPatchSnippetFromArgs, set: (value) => { _cliPatchSnippetFromArgs = value; } },
  _cliToolCardSnippet: { enumerable: true, get: () => _cliToolCardSnippet, set: (value) => { _cliToolCardSnippet = value; } },
  _cliToolCardHasDiffSnippet: { enumerable: true, get: () => _cliToolCardHasDiffSnippet, set: (value) => { _cliToolCardHasDiffSnippet = value; } },
  _assistantToolAnchorIdxForMessage: { enumerable: true, get: () => _assistantToolAnchorIdxForMessage, set: (value) => { _assistantToolAnchorIdxForMessage = value; } },
  _toolArgsSnapshot: { enumerable: true, get: () => _toolArgsSnapshot, set: (value) => { _toolArgsSnapshot = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
