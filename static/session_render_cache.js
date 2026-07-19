// Bounded LRU cache for rendered session transcripts.
//
// This module owns its accounting and eviction policy. Callers only provide a
// session id and a render entry; they do not mutate the backing Map directly.

function positiveLimit(value, fallback){
  const parsed=Number(value);
  return Number.isFinite(parsed)&&parsed>0?Math.floor(parsed):fallback;
}

export function createRenderSignature({
  messages=[],
  toolCalls=[],
  session=null,
  messageContent=(message)=>message&&message.content,
  messageHasReasoningPayload=(message)=>!!(
    message&&(message.reasoning||message.thinking||message._reasoning)
  ),
}={}){
  const renderedMessages=Array.isArray(messages)?messages:[];
  const settledToolCalls=Array.isArray(toolCalls)?toolCalls:[];
  let hash=2166136261;

  function add(value){
    const text=String(value==null?'':value);
    for(let index=0;index<text.length;index++){
      hash^=text.charCodeAt(index);
      hash=Math.imul(hash,16777619)>>>0;
    }
    hash^=31;
    hash=Math.imul(hash,16777619)>>>0;
  }

  add(renderedMessages.length);
  for(const message of renderedMessages){
    if(!message||typeof message!=='object'){ add('missing'); continue; }
    add(message.role);add(message.timestamp);add(message._ts);
    add(message._error);add(message._statusCard);add(messageContent(message));
    if(Array.isArray(message.content)){
      add('content-array');
      message.content.forEach((part)=>{
        if(!part||typeof part!=='object'){ add(part); return; }
        add(part.type);add(part.id);add(part.name);add(part.text);add(part.content);
      });
    }
    if(Array.isArray(message.tool_calls)){
      add('message-tool-calls');add(message.tool_calls.length);
      message.tool_calls.forEach((toolCall)=>{
        add(toolCall&&toolCall.id);add(toolCall&&toolCall.name);
        add(toolCall&&toolCall.type);add(JSON.stringify(toolCall&&toolCall.function||{}));
      });
    }
    if(Array.isArray(message._partial_tool_calls)){
      add('partial-tool-calls');add(message._partial_tool_calls.length);
      message._partial_tool_calls.forEach((toolCall)=>{
        add(toolCall&&toolCall.id);add(toolCall&&toolCall.name);add(toolCall&&toolCall.snippet);
      });
    }
    if(messageHasReasoningPayload(message)){
      add(message.reasoning||message.thinking||message._reasoning||'reasoning');
    }
    if(Array.isArray(message.attachments)){
      message.attachments.forEach((attachment)=>{
        add(attachment&&typeof attachment==='object'?JSON.stringify(attachment):attachment);
      });
    }
  }

  add('settled-tool-calls');add(settledToolCalls.length);
  settledToolCalls.forEach((toolCall)=>{
    if(!toolCall||typeof toolCall!=='object'){ add(toolCall); return; }
    add(toolCall.tid);add(toolCall.id);add(toolCall.name);add(toolCall.done);
    add(toolCall.is_diff);add(toolCall.assistant_msg_idx);add(toolCall.snippet);
    add(JSON.stringify(toolCall.args||{}));
  });
  if(session){
    add(session.message_count);add(session.updated_at);
    add(session.compression_anchor_visible_idx);
    add(JSON.stringify(session.compression_anchor_message_key||null));
    add(session.compression_anchor_summary||'');
  }
  return `${renderedMessages.length}:${settledToolCalls.length}:${hash.toString(16)}`;
}

export function createSessionRenderCache(options={}){
  const maxEntries=positiveLimit(options.maxEntries,8);
  const maxEntryBytes=positiveLimit(options.maxEntryBytes,2*1024*1024);
  const maxTotalBytes=positiveLimit(options.maxTotalBytes,8*1024*1024);
  const entries=new Map();
  let retainedBytes=0;

  function entryBytes(entry){
    // JavaScript strings are UTF-16 in memory. Budget retained browser heap,
    // not the compressed or UTF-8 wire representation.
    return String(entry&&entry.html||'').length*2;
  }

  function remove(sessionId){
    const cached=entries.get(sessionId);
    if(!cached) return false;
    retainedBytes=Math.max(0,retainedBytes-Number(cached.bytes||0));
    return entries.delete(sessionId);
  }

  function get(sessionId){
    const cached=entries.get(sessionId);
    if(!cached) return null;
    // Map insertion order is the LRU order; a successful read becomes newest.
    entries.delete(sessionId);
    entries.set(sessionId,cached);
    return cached;
  }

  function set(sessionId,entry){
    if(!sessionId||!entry||typeof entry!=='object') return false;
    const bytes=entryBytes(entry);
    remove(sessionId);
    if(bytes>maxEntryBytes||bytes>maxTotalBytes) return false;

    entries.set(sessionId,{...entry,bytes});
    retainedBytes+=bytes;
    while(entries.size>maxEntries||retainedBytes>maxTotalBytes){
      const oldestSessionId=entries.keys().next().value;
      if(oldestSessionId===undefined) break;
      remove(oldestSessionId);
    }
    return entries.has(sessionId);
  }

  function clear(){
    entries.clear();
    retainedBytes=0;
  }

  return Object.freeze({
    get,
    set,
    delete:remove,
    clear,
    has:(sessionId)=>entries.has(sessionId),
    get size(){ return entries.size; },
    get bytes(){ return retainedBytes; },
  });
}
