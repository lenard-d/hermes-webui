import { showApprovalForSession } from './approvals.js';
import { showClarifyForSession } from './clarify.js';
import {
  _attentionSoundKey,
  playAttentionSound,
  sendBrowserNotification,
} from './notifications.js';

// Content events grow one live assistant turn. Terminal settlement and
// transport recovery intentionally live elsewhere so late or stale terminal
// frames cannot be confused with ordinary transcript growth.
export function createStreamContentEventOwner(options={}){
  const activeSid=String(options.sessionId||'');
  const streamId=String(options.streamId||'');
  const S=options.state&&typeof options.state==='object'?options.state:{};
  const INFLIGHT=options.inflightStore&&typeof options.inflightStore==='object'
    ? options.inflightStore
    : {};
  const turn=options.turn&&typeof options.turn==='object'?options.turn:{};
  const renderer=options.renderer&&typeof options.renderer==='object'?options.renderer:{};
  const anchor=options.anchor&&typeof options.anchor==='object'?options.anchor:{};
  const completeCompression=typeof options.completeAutomaticCompression==='function'
    ? options.completeAutomaticCompression
    : ()=>{};
  const persistInflight=typeof options.persistInflight==='function'
    ? options.persistInflight
    : ()=>{};

  function attach(source){
    source.addEventListener('token',event=>{
      if(turn.isTerminal()) return;
      const data=JSON.parse(event.data);
      turn.appendAssistantText(data.text);
      turn.syncInflight();
      if(!S.session||S.session.session_id!==activeSid) return;
      completeCompression(activeSid);
      if(turn.isFreshSegment()) appendThinking('',renderer.liveThinkingPlacement());
      if(turn.assistantRow()){
        renderer.ensureAssistantRow();
        renderer.scheduleRender();
      }else{
        const parsed=renderer.parseStreamState();
        if(String((parsed&&parsed.displayText)||'').trim()) renderer.ensureAssistantRow();
        renderer.scheduleRender(parsed);
      }
    });

    source.addEventListener('interim_assistant',event=>{
      if(turn.isTerminal()) return;
      const data=JSON.parse(event.data);
      const visible=String(data&&data.text?data.text:'').trim();
      const alreadyStreamed=!!(data&&data.already_streamed);
      if(!visible) return;
      if(data&&data.reasoning_echo) anchor.stripReasoningEcho(visible);
      turn.setLiveReasoningText('');
      if(alreadyStreamed){
        if(!S.session||S.session.session_id!==activeSid){
          turn.recordActivityBoundary();
          renderer.resetAssistantSegment();
          return;
        }
        completeCompression(activeSid);
        const parsed=renderer.parseStreamState();
        if(String((parsed&&parsed.displayText)||'').trim()||turn.assistantRow()){
          renderer.ensureAssistantRow(true);
          renderer.flushPendingSegment({force:true});
          if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
          if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
          turn.recordActivityBoundary();
        }
        renderer.resetAssistantSegment();
        return;
      }
      turn.appendInterimAssistantText(visible);
      turn.pushInterimSnippet(visible);
      turn.syncInflight();
      if(!S.session||S.session.session_id!==activeSid){
        turn.recordActivityBoundary();
        renderer.resetAssistantSegment();
        return;
      }
      completeCompression(activeSid);
      renderer.ensureAssistantRow(true);
      const assistantRow=turn.assistantRow();
      if(assistantRow) assistantRow.setAttribute('data-interim','1');
      renderer.flushPendingSegment({force:true,skipAnchorProcessProse:true});
      if(typeof finalizeThinkingCard==='function') finalizeThinkingCard();
      if(typeof closeCurrentLiveActivityGroup==='function') closeCurrentLiveActivityGroup();
      anchor.apply('interim_assistant',data,event);
      const collapseThreshold=3;
      if(turn.interimSnippetCount()>collapseThreshold&&assistantRow){
        const blocks=assistantRow.parentElement;
        if(blocks){
          const anchorSceneOwnsLive=!!(blocks.closest&&blocks.closest('[data-anchor-scene-live-owner="1"]'));
          if(anchorSceneOwnsLive){
            blocks.querySelectorAll('.interim-collapse-toggle').forEach(element=>element.remove());
          }else{
            const allInterim=Array.from(blocks.querySelectorAll('[data-interim="1"]'));
            const toHide=allInterim.slice(0,allInterim.length-collapseThreshold);
            let toggle=blocks.querySelector('.interim-collapse-toggle');
            if(!toggle){
              toggle=document.createElement('span');
              toggle.className='interim-collapse-toggle';
              toggle.dataset.threshold=String(collapseThreshold);
              if(toHide.length) toHide[0].before(toggle);
            }
            if(!toggle.dataset.expanded) toHide.forEach(element=>element.classList.add('interim-collapsed'));
            const hiddenCount=blocks.querySelectorAll('[data-interim="1"].interim-collapsed').length;
            if(hiddenCount) toggle.textContent='Show '+hiddenCount+' earlier update'+(hiddenCount===1?'':'s');
          }
        }
      }
      turn.recordActivityBoundary();
      renderer.resetAssistantSegment();
      renderer.scheduleRender();
    });

    source.addEventListener('reasoning',event=>{
      if(turn.isTerminal()||!turn.ownsActiveStreamOrBackground()) return;
      const data=JSON.parse(event.data);
      const text=data.text||'';
      turn.appendReasoning(text);
      if(text&&S.session&&S.session.session_id===activeSid) completeCompression(activeSid);
      turn.syncInflight();
      if(text&&S.session&&S.session.session_id===activeSid&&S.activeStreamId===streamId){
        const liveThinkingText=turn.liveThinkingText();
        const fallback={};
        if(!anchor.upsertReasoning(liveThinkingText,fallback)){
          renderer.updateLiveThinking(liveThinkingText,{
            ...fallback,
            anchorRenderFallback:true,
            sessionId:activeSid,
            streamId,
          });
        }
      }
    });

    source.addEventListener('todo_state',event=>{
      let data;
      try{ data=JSON.parse(event.data||'{}'); }catch(_){ return; }
      if(!data||typeof data!=='object') return;
      if(data.session_id&&data.session_id!==activeSid) return;
      if(!S.session||S.session.session_id!==activeSid) return;
      if(!Array.isArray(data.todos)) return;
      const incomingTimestamp=Number(data.ts)||0;
      const currentTimestamp=(S.todoStateMeta&&Number(S.todoStateMeta.ts))||0;
      if(incomingTimestamp&&currentTimestamp&&incomingTimestamp<currentTimestamp) return;
      S.todos=data.todos;
      S.todoStateMeta={
        ts:incomingTimestamp||(Date.now()/1000),
        source:String(data.source||'tool'),
        version:Number(data.version)||1,
      };
      const inflight=INFLIGHT[activeSid];
      if(inflight){
        inflight.todos=S.todos;
        inflight.todoStateMeta=S.todoStateMeta;
      }
      persistInflight();
      if(typeof scheduleTodosRefresh==='function') scheduleTodosRefresh();
    });

    source.addEventListener('approval',event=>{
      const data=JSON.parse(event.data);
      anchor.apply('approval',data,event);
      showApprovalForSession(activeSid,data,1);
      playAttentionSound(_attentionSoundKey(activeSid,'approval',1));
      sendBrowserNotification('Approval required',data.description||'Tool approval needed',{sid:activeSid});
    });

    source.addEventListener('clarify',event=>{
      const data=JSON.parse(event.data);
      anchor.apply('clarify',data,event);
      showClarifyForSession(activeSid,data);
      playAttentionSound(_attentionSoundKey(activeSid,'clarify',1));
      sendBrowserNotification('Clarification needed',data.question||'Tool clarification needed',{sid:activeSid});
    });
  }

  return Object.freeze({attach});
}
