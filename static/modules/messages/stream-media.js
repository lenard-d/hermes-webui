// Chunk-aware MEDIA:<ref> projection for live smd parsers.
// The tail store is keyed by parser identity so concurrent live/anchor parsers
// cannot join partial media tokens across DOM owners.
const _MEDIA_TAIL_MAX=4096;
const _SMD_MEDIA_PREFIX='MEDIA:';
export const _SMD_MEDIA_TAIL=typeof WeakMap!=='undefined'?new WeakMap():new Map();
export const __SMD_PARSER_FALLBACK={};

export function _smdMediaPrefixTail(value){
  const text=String(value||'');
  const max=Math.min(_SMD_MEDIA_PREFIX.length,text.length);
  for(let len=max;len>0;len-=1){
    const suffix=text.slice(text.length-len);
    if(_SMD_MEDIA_PREFIX.startsWith(suffix)) return suffix;
  }
  return '';
}

export function _smdAppendPlainText(parent,data,text,baseAddText){
  const value=String(text||'');
  if(parent&&parent.appendChild&&typeof document!=='undefined'&&document.createTextNode){
    parent.appendChild(document.createTextNode(value));
    return;
  }
  if(baseAddText) baseAddText(data,value);
}

export function _smdMediaWriteText(parent,data,baseAddText,writeText,text){
  if(writeText){
    writeText(parent,data,String(text||''));
    return;
  }
  if(baseAddText) baseAddText(data,String(text||''));
}

export function _smdMediaTailSet(tailMap,parser,chunk,parent,baseAddText,data,writeText){
  if(!tailMap||!parser) return;
  if(chunk) tailMap.set(parser,{chunk,parent,baseAddText,data,writeText});
  else tailMap.delete(parser);
}

export function _smdMediaTailEntryChunk(entry){
  return entry&&typeof entry==='object'&&Object.prototype.hasOwnProperty.call(entry,'chunk')?entry.chunk:entry;
}

export function _smdMediaTailSameOwner(entry,parent,baseAddText,writeText){
  return !!entry&&entry.parent===parent&&entry.baseAddText===baseAddText&&entry.writeText===writeText;
}

export function _smdMediaRefHasReliableBoundary(rawRef){
  const raw=String(rawRef||'');
  if(/[?#]$/.test(raw)) return false;
  const ref=raw.split(/[?#]/,1)[0];
  return /\.(?:png|jpe?g|gif|webp|bmp|ico|svg|avif|mp4|webm|mov|m4v|mkv|avi|ogv|mp3|wav|ogg|m4a|aac|wma|opus|flac|oga|pdf|html?|csv|diff|patch|excalidraw)$/i.test(ref);
}

export function _smdMediaTailFlushEntry(entry){
  const chunk=_smdMediaTailEntryChunk(entry);
  if(!chunk) return;
  const match=/^MEDIA:([^\s\)\]]+)$/.exec(String(chunk));
  const emitted=!!(match&&entry&&entry.parent&&_smdAppendMediaNode(entry.parent,match[1]));
  if(!emitted&&entry) _smdMediaWriteText(entry.parent,entry.data,entry.baseAddText,entry.writeText,chunk);
}

export function _smdMediaTailFlush(parser){
  if(!_SMD_MEDIA_TAIL||!parser||!_SMD_MEDIA_TAIL.get) return;
  const entry=_SMD_MEDIA_TAIL.get(parser);
  if(!entry) return;
  _SMD_MEDIA_TAIL.delete(parser);
  _smdMediaTailFlushEntry(entry);
}

export function _smdMediaAwareAddText(baseAddText,parent,data,text,tailMap=_SMD_MEDIA_TAIL,parser,writeText){
  const value=String(text||'');
  const tails=tailMap||_SMD_MEDIA_TAIL;
  const writeCurrent=chunk=>_smdMediaWriteText(parent,data,baseAddText,writeText,chunk);
  if(!value){writeCurrent('');return;}

  let leadEntry=tails&&parser&&tails.get?tails.get(parser):null;
  let lead=_smdMediaTailEntryChunk(leadEntry);
  if(lead&&!_smdMediaTailSameOwner(leadEntry,parent,baseAddText,writeText)){
    if(tails&&parser&&tails.delete) tails.delete(parser);
    _smdMediaTailFlushEntry(leadEntry);
    leadEntry=null;
    lead='';
  }else if(lead&&tails&&parser&&tails.delete){
    tails.delete(parser);
  }
  const combined=lead?lead+value:value;
  if(!/MEDIA:/.test(combined)){
    const prefixTail=_smdMediaPrefixTail(combined);
    if(prefixTail&&tails&&parser&&prefixTail.length<_MEDIA_TAIL_MAX){
      const stable=combined.slice(0,combined.length-prefixTail.length);
      if(stable) writeCurrent(stable);
      _smdMediaTailSet(tails,parser,prefixTail,parent,baseAddText,data,writeText);
      return;
    }
    writeCurrent(combined);
    return;
  }

  const re=/MEDIA:([^\s\)\]]+)/g;
  let last=0;
  let match;
  let unmatchedTail=null;
  while((match=re.exec(combined))){
    const matchEnd=match.index+match[0].length;
    if(match.index>last) writeCurrent(combined.slice(last,match.index));
    if(matchEnd===combined.length&&!_smdMediaRefHasReliableBoundary(match[1])){
      const candidate=combined.slice(match.index);
      if(candidate.length<_MEDIA_TAIL_MAX) unmatchedTail=candidate;
      else writeCurrent(candidate);
      last=combined.length;
      break;
    }
    if(!_smdAppendMediaNode(parent,match[1])) writeCurrent(match[0]);
    last=matchEnd;
  }

  const rest=combined.slice(last);
  if(rest){
    const tailMatch=/MEDIA:[^\s\)\]]*$/.exec(rest);
    const prefixTail=tailMatch?'':_smdMediaPrefixTail(rest);
    const tailValue=tailMatch?tailMatch[0]:prefixTail;
    if(tailValue&&rest.length<_MEDIA_TAIL_MAX){
      const tailStart=tailMatch?tailMatch.index:rest.length-prefixTail.length;
      if(tailStart>0) writeCurrent(rest.slice(0,tailStart));
      unmatchedTail=tailValue;
    }else{
      writeCurrent(rest);
    }
  }
  if(tails&&parser) _smdMediaTailSet(tails,parser,unmatchedTail,parent,baseAddText,data,writeText);
}

export function _smdAppendMediaNode(parent,rawRef){
  if(!parent||!rawRef) return false;
  const mediaHtml=typeof _inlineMediaHtmlForRef==='function'?_inlineMediaHtmlForRef(String(rawRef)):'';
  if(!mediaHtml) return false;
  let host=null;
  try{
    const doc=new DOMParser().parseFromString('<div>'+mediaHtml+'</div>','text/html');
    host=doc.body&&doc.body.firstChild;
  }catch(_){host=null;}
  if(!host||!host.childNodes||!host.childNodes.length) return false;
  const fragment=document.createDocumentFragment();
  while(host.firstChild) fragment.appendChild(host.firstChild);
  parent.appendChild(fragment);
  _smdScheduleMediaPostProcess(parent);
  return true;
}

export function _smdScheduleMediaPostProcess(root){
  if(!root) return;
  if(typeof _postProcessWithAnchorSuppression!=='function'
    &&typeof postProcessRenderedMessages!=='function'
    &&typeof _applyMediaPlaybackPreferences!=='function') return;
  const run=()=>{
    try{
      if(typeof _postProcessWithAnchorSuppression==='function') _postProcessWithAnchorSuppression(root);
      else if(typeof postProcessRenderedMessages==='function') postProcessRenderedMessages(root);
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(root);
    }catch(_){}
  };
  if(typeof requestAnimationFrame==='function') requestAnimationFrame(run);
  else if(typeof setTimeout==='function') setTimeout(run,0);
  else run();
}

export function _smdParserKey(data,element){
  return (data&&data.parser)||(element&&element.__smdParser)||__SMD_PARSER_FALLBACK;
}

export function _smdBindParserIdentity(renderer,parser,element){
  if(renderer&&renderer.data) renderer.data.parser=parser;
  if(element) element.__smdParser=parser;
}

export function _smdClearParserIdentity(element,parser){
  if(!element||(parser&&element.__smdParser!==parser)) return;
  try{delete element.__smdParser;}catch(_){element.__smdParser=null;}
}

export function _smdMediaTailClear(parser){
  if(_SMD_MEDIA_TAIL&&parser) _SMD_MEDIA_TAIL.delete(parser);
}

export function createSmdMediaTextWriter(baseAddText,element,writeText){
  return function addText(data,text){
    const parent=data&&data.nodes&&data.nodes[data.index];
    _smdMediaAwareAddText(
      baseAddText,
      parent,
      data,
      text,
      _SMD_MEDIA_TAIL,
      _smdParserKey(data,element),
      writeText,
    );
  };
}
