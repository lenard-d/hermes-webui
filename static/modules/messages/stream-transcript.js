const EPHEMERAL_TURN_FIELDS=[
  '_turnUsage',
  '_turnDuration',
  '_turnTps',
  '_gatewayRouting',
  '_statusCard',
  '_anchor_stream_id',
  '_anchor_activity_scene',
];

function messageIdentityKey(message){
  if(!message||!message.role) return '';
  const timestamp=message._ts||message.timestamp||'';
  let body='';
  if(typeof message.content==='string') body=message.content;
  else if(Array.isArray(message.content)){
    try{
      body=message.content
        .map(part=>(part&&typeof part==='object')?(part.text||part.input_text||'')||'':String(part||''))
        .join('')
        .slice(0,160);
    }catch(_){ body=''; }
  }
  return `${message.role}|${timestamp}|${body.slice(0,160)}`;
}

export function carryForwardEphemeralTurnFields(previousMessages, nextMessages){
  if(!Array.isArray(previousMessages)||!Array.isArray(nextMessages)) return nextMessages;
  if(!previousMessages.length||!nextMessages.length) return nextMessages;
  const previousByIdentity=new Map();
  for(const previous of previousMessages){
    const key=messageIdentityKey(previous);
    if(key) previousByIdentity.set(key,previous);
  }
  for(const next of nextMessages){
    const previous=previousByIdentity.get(messageIdentityKey(next));
    if(!previous) continue;
    for(const field of EPHEMERAL_TURN_FIELDS){
      if(previous[field]!=null&&next[field]==null) next[field]=previous[field];
    }
  }
  return nextMessages;
}

export function createStreamTranscriptProjection(options={}){
  const messageText=typeof options.messageText==='function'
    ? options.messageText
    : message=>String(message&&message.content||'');
  const markerOnlyText=typeof options.markerOnlyText==='function'
    ? options.markerOnlyText
    : ()=>false;

  function isMarkerOnlyAssistantMessage(message){
    return !!(message&&message.role==='assistant'&&markerOnlyText(messageText(message)));
  }

  function isRecoveryControlText(text){
    const normalized=String(text||'').replace(/\s+/g,' ').trim();
    if(!normalized) return false;
    const systemRecovery=/^\[System:/i.test(normalized)
      && (/continue exactly where you left off/i.test(normalized)
        || /do not retry the same tool call/i.test(normalized));
    const backendRecovery=/^the live worker stopped before this run finished\.?$/i.test(normalized);
    return !!(systemRecovery||backendRecovery);
  }

  function isRecoveryControlMessage(message){
    if(!message||message.role==='tool') return false;
    if(message.recovery_control===true) return true;
    return isRecoveryControlText(messageText(message));
  }

  function filterRecoveryControlMessages(messages){
    if(!Array.isArray(messages)) return [];
    return messages.filter(message=>!isRecoveryControlMessage(message));
  }

  function replaceMarkerOnlyAssistantWithStreamError(messages){
    if(!Array.isArray(messages)) return false;
    const message=[...messages].reverse().find(candidate=>candidate&&candidate.role==='assistant');
    if(!isMarkerOnlyAssistantMessage(message)) return false;
    message.content='**Error:** No response received after context compression. Please retry.';
    message.provider_details='The only assistant text returned for this turn was the internal preserved-task-list compression marker, so the WebUI replaced it with an explicit error instead of rendering the marker as a model response.';
    return true;
  }

  function isTerminalStreamErrorMarkerMessage(message){
    return !!(message&&message.role==='assistant'&&typeof message.content==='string'
      && message.content.startsWith('**Connection interrupted:** The browser lost the live SSE connection before the response finished.'));
  }

  function ensureSingleTerminalStreamErrorMarker(messages){
    if(!Array.isArray(messages)) return;
    while(messages.length&&isTerminalStreamErrorMarkerMessage(messages[messages.length-1])) messages.pop();
    messages.push({
      role:'assistant',
      content:'**Connection interrupted:** The browser lost the live SSE connection before the response finished. If the worker completed, reopening this session should restore the settled transcript.',
    });
  }

  return Object.freeze({
    carryForwardEphemeralTurnFields,
    ensureSingleTerminalStreamErrorMarker,
    filterRecoveryControlMessages,
    isRecoveryControlText,
    isTerminalStreamErrorMarkerMessage,
    replaceMarkerOnlyAssistantWithStreamError,
  });
}
