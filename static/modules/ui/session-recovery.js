import { INFLIGHT_KEY } from './inflight-state.js';
import { clearInflight, dismissReconnect, showReconnectBanner } from './reconnect-banner.js';
import { _isContextCompactionMessage } from './compression-ui.js';
import { msgContent } from './assistant-turn-presentation.js';
import { _renderMessagesWithScrollSnapshot } from './render-support.js';
import { setStatus, showToast } from './composer.js';
import { $, S } from './state.js';
import { syncTopbar } from './topbar-presentation.js';

async function refreshSession(){
  if(window._restartingForUpdate){location.reload();return;}
  dismissReconnect();
  if(!S.session) return;
  try{
    const data=await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session=data.session;
    S.messages=data.session.messages||[];
    _messagesTruncated=!!data.session._messages_truncated;
    _oldestIdx=data.session._messages_offset||0;
    const pendingMsg=getPendingSessionMessage(data.session,S.messages);
    if(pendingMsg) S.messages.push(pendingMsg);
    S.activeStreamId=data.session.active_stream_id||null;
    syncTopbar();
    _renderMessagesWithScrollSnapshot();
    showToast('Conversation refreshed');
  }catch(e){
    setStatus('Refresh failed: '+e.message);
  }
}

function _pendingCurrentTailUserMessage(messages){
  const list=Array.isArray(messages)?messages:[];
  for(let i=list.length-1;i>=0;i--){
    const msg=list[i];
    if(!msg) continue;
    if(String(msg.role||'')==='user'){
      if(_isContextCompactionMessage(msg)) continue;
      return msg;
    }
    if(msg._live||String(msg.role||'')==='tool') continue;
    return null;
  }
  return null;
}

function getPendingSessionMessage(session, messagesOverride=null){
  const text=String(session?.pending_user_message||'').trim();
  if(!text) return null;
  const attachments=Array.isArray(session?.pending_attachments)?session.pending_attachments.filter(Boolean):[];
  const sourceMessages=Array.isArray(messagesOverride)?messagesOverride:session?.messages;
  const messages=Array.isArray(sourceMessages)?sourceMessages:[];
  const currentTailUser=_pendingCurrentTailUserMessage(messages);
  if(currentTailUser){
    const pendingCandidate={role:'user',content:text};
    const sameCurrentTurn=typeof _sameTranscriptMessage==='function'
      ?_sameTranscriptMessage(currentTailUser,pendingCandidate)
      :String(msgContent(currentTailUser)||'').trim()===text;
    if(sameCurrentTurn){
      if(attachments.length&&!currentTailUser.attachments?.length) currentTailUser.attachments=attachments;
      return null;
    }
  }
  return {
    role:'user',
    content:text,
    attachments:attachments.length?attachments:undefined,
    _ts:session?.pending_started_at||Date.now()/1000,
    _pending:true,
    _source:session?.pending_user_source||undefined,
  };
}

async function checkInflightOnBoot(sid){
  const raw=localStorage.getItem(INFLIGHT_KEY);
  if(!raw) return;
  try{
    const {sid:inflightSid,streamId,ts}=JSON.parse(raw);
    if(inflightSid!==sid){clearInflight();return;}
    if (S.activeStreamId && S.activeStreamId === streamId) return;
    if(Date.now()-ts>10*60*1000){clearInflight();return;}
    const status=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId||'')}`);
    if(status.active){
      showReconnectBanner(t('reconnect_active'));
    }else if(Date.now()-ts<90*1000){
      showReconnectBanner(t('reconnect_finished'));
    }else{
      clearInflight();
    }
  }catch(_){
    clearInflight();
  }
}

export {
  _pendingCurrentTailUserMessage,
  checkInflightOnBoot,
  getPendingSessionMessage,
  refreshSession,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  refreshSession:{enumerable:true,get:()=>refreshSession,set:(value)=>{refreshSession=value;}},
  _pendingCurrentTailUserMessage:{enumerable:true,get:()=>_pendingCurrentTailUserMessage,set:(value)=>{_pendingCurrentTailUserMessage=value;}},
  getPendingSessionMessage:{enumerable:true,get:()=>getPendingSessionMessage,set:(value)=>{getPendingSessionMessage=value;}},
  checkInflightOnBoot:{enumerable:true,get:()=>checkInflightOnBoot,set:(value)=>{checkInflightOnBoot=value;}},
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
