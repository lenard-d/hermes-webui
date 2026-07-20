import { _deferClearProgrammaticScroll } from './composer-controls.js';
import { rerenderMessages as renderMessages } from './transcript-render-dispatch.js';
import { $, S, esc } from './state.js';
import { MESSAGE_RENDER_WINDOW_DEFAULT, _getVisibleMessagesWithIdx, _messageRenderWindowSize, _messageVirtualScrollTopForVisibleIdx, _messageVisibleIndexForRawIdx, registerMessageVirtualizationLifecycle, compatibilityBindings as virtualStateBindings } from './message-virtualization-state.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';

function _currentMessageRenderWindowSize(){
  return Math.max(
    MESSAGE_RENDER_WINDOW_DEFAULT,
    Number(_messageRenderWindowSize)||MESSAGE_RENDER_WINDOW_DEFAULT
  );
}
function _messageRenderableMessageCount(){
  return _getVisibleMessagesWithIdx().length;
}
function _messageHiddenBeforeCount(){
  return Math.max(0,_messageRenderableMessageCount()-_currentMessageRenderWindowSize());
}
function _isSessionEndlessScrollEnabled(){
  return window._sessionEndlessScrollEnabled===true;
}
function _wireMessageWindowLoadEarlierButton(){
  const indicator=$('loadOlderIndicator');
  if(!indicator) return;
  indicator.onclick=()=>{
    if(typeof _loadOlderMessages==='function') _loadOlderMessages();
  };
}
function _isSessionJumpButtonsEnabled(){
  return window._sessionJumpButtonsEnabled===true;
}
function _applySessionNavigationPrefs(){
  const container=$('messages');
  if(container) container.classList.toggle('session-nav-enabled',_isSessionJumpButtonsEnabled());
  _updateSessionStartJumpButton();
}
function _updateSessionStartJumpButton(){
  const btn=$('jumpToSessionStartBtn');
  const container=$('messages');
  if(!btn||!container) return;
  if(!_isSessionJumpButtonsEnabled()){
    btn.style.display='none';
    return;
  }
  const hasSession=!!(S&&S.session&&S.messages&&S.messages.length);
  const awayFromStart=container.scrollTop>Math.max(240,container.clientHeight*0.35);
  const hasScrollableHistory=container.scrollHeight>container.clientHeight+Math.max(240,container.clientHeight*0.35);
  const canRevealStart=hasScrollableHistory||_messageHiddenBeforeCount()>0||!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  btn.style.display=(hasSession&&canRevealStart&&awayFromStart)?'flex':'none';
}
async function jumpToSessionStart(){
  const container=$('messages');
  if(!container||!S.session) return;
  composerControlsBindings._scrollPinned=false;
  composerControlsBindings._messageUserUnpinned=true;
  composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
  try{
    // During active streaming, skip full message load — API response won't
    // include live messages from the current turn, and replacing S.messages
    // would lose user/assistant/tool messages.
    if(!(S.busy||S.activeStreamId)){
      if(typeof _ensureAllMessagesLoaded==='function') await _ensureAllMessagesLoaded();
    }
    virtualStateBindings._messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    container.scrollTop=0;
    virtualStateBindings._messageVirtualWindowKey='';
    // During streaming, skip renderMessages — it rebuilds the DOM but tool card
    // insertion is blocked by !S.busy, losing Activity until "done" fires.
    if(!(S.busy||S.activeStreamId)){
      renderMessages({ preserveScroll:true });
    }
    requestAnimationFrame(()=>{
      container.scrollTop=0;
      _updateSessionStartJumpButton();
      _deferClearProgrammaticScroll();
    });
  }catch(e){
    console.warn('jumpToSessionStart failed:',e);
    composerControlsBindings._programmaticScroll=false;
  }
}

function _userMessageDomId(rawIdx){
  return `msg-user-${rawIdx}`;
}

function _questionJumpButtonHtml(questionRawIdx, assistantRawIdx){
  if(typeof questionRawIdx!=='number'||questionRawIdx<0) return '';
  const label=t('jump_to_question')||'Response';
  const title=t('jump_to_question_label')||'Jump to the start of this response';
  const aIdx=(typeof assistantRawIdx==='number'&&assistantRawIdx>=0)?assistantRawIdx:-1;
  return `<button class="msg-question-jump-btn session-jump-btn session-jump-btn--inline" type="button" title="${esc(title)}" aria-label="${esc(title)}" onclick="jumpToTurnQuestion(${questionRawIdx},${aIdx})"><span aria-hidden="true">↑</span><span>${esc(label)}</span></button>`;
}

function _highlightQuestionRow(row){
  if(!row) return;
  row.classList.remove('msg-question-highlight');
  void row.offsetWidth;
  row.classList.add('msg-question-highlight');
  window.setTimeout(()=>row.classList.remove('msg-question-highlight'),1800);
}

async function jumpToTurnQuestion(questionRawIdx, assistantRawIdx){
  const container=$('messages');
  if(!container||typeof questionRawIdx!=='number'||questionRawIdx<0) return;
  const scrollToTarget=()=>{
    const hasAssistant=typeof assistantRawIdx==='number'&&assistantRawIdx>=0;
    if(hasAssistant){
      // A single assistant rawIdx can render multiple segment nodes — some hidden
      // (assistant-segment-worklog-source / assistant-segment-anchor are display:none).
      // scrollIntoView() on a hidden node silently no-ops, so only treat a VISIBLE
      // segment (getClientRects().length>0) as a successful target; otherwise fall
      // through to the question-row fallback rather than suppressing it. (#3934)
      const segs=container.querySelectorAll('[data-msg-idx="'+assistantRawIdx+'"]');
      for(const seg of segs){
        if(seg.getClientRects().length>0){
          seg.scrollIntoView({block:'start',behavior:'smooth'});
          return true;
        }
      }
    }
    const row=document.getElementById(_userMessageDomId(questionRawIdx));
    if(!row) return false;
    row.scrollIntoView({block:'center',behavior:'smooth'});
    _highlightQuestionRow(row);
    return true;
  };
  if(scrollToTarget()) return;
  const visWithIdx=_getVisibleMessagesWithIdx();
  const visibleIdx=_messageVisibleIndexForRawIdx(questionRawIdx, visWithIdx);
  if(visibleIdx>=0){
    composerControlsBindings._scrollPinned=false;
    composerControlsBindings._messageUserUnpinned=true;
    composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
    container.scrollTop=_messageVirtualScrollTopForVisibleIdx(visWithIdx, visibleIdx, container);
    virtualStateBindings._messageVirtualWindowKey='';
    renderMessages({ preserveScroll:true });
    requestAnimationFrame(()=>{
      if(!scrollToTarget()&&_messageHiddenBeforeCount()>0){
        virtualStateBindings._messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
        virtualStateBindings._messageVirtualWindowKey='';
        renderMessages({ preserveScroll:true });
        requestAnimationFrame(scrollToTarget);
      }
      _deferClearProgrammaticScroll();
    });
    return;
  }
  if(_messageHiddenBeforeCount()>0){
    virtualStateBindings._messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount());
    virtualStateBindings._messageVirtualWindowKey='';
    renderMessages({ preserveScroll:true });
    requestAnimationFrame(scrollToTarget);
  }
}

queueMicrotask(()=>registerMessageVirtualizationLifecycle({
  currentRenderWindowSize:_currentMessageRenderWindowSize,
}));

export {
  _currentMessageRenderWindowSize,
  _messageRenderableMessageCount,
  _messageHiddenBeforeCount,
  _isSessionEndlessScrollEnabled,
  _wireMessageWindowLoadEarlierButton,
  _isSessionJumpButtonsEnabled,
  _applySessionNavigationPrefs,
  _updateSessionStartJumpButton,
  jumpToSessionStart,
  _userMessageDomId,
  _questionJumpButtonHtml,
  _highlightQuestionRow,
  jumpToTurnQuestion,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _currentMessageRenderWindowSize: { enumerable:true, get:()=>_currentMessageRenderWindowSize, set:value=>{ _currentMessageRenderWindowSize=value; } },
  _messageRenderableMessageCount: { enumerable:true, get:()=>_messageRenderableMessageCount, set:value=>{ _messageRenderableMessageCount=value; } },
  _messageHiddenBeforeCount: { enumerable:true, get:()=>_messageHiddenBeforeCount, set:value=>{ _messageHiddenBeforeCount=value; } },
  _isSessionEndlessScrollEnabled: { enumerable:true, get:()=>_isSessionEndlessScrollEnabled, set:value=>{ _isSessionEndlessScrollEnabled=value; } },
  _wireMessageWindowLoadEarlierButton: { enumerable:true, get:()=>_wireMessageWindowLoadEarlierButton, set:value=>{ _wireMessageWindowLoadEarlierButton=value; } },
  _isSessionJumpButtonsEnabled: { enumerable:true, get:()=>_isSessionJumpButtonsEnabled, set:value=>{ _isSessionJumpButtonsEnabled=value; } },
  _applySessionNavigationPrefs: { enumerable:true, get:()=>_applySessionNavigationPrefs, set:value=>{ _applySessionNavigationPrefs=value; } },
  _updateSessionStartJumpButton: { enumerable:true, get:()=>_updateSessionStartJumpButton, set:value=>{ _updateSessionStartJumpButton=value; } },
  jumpToSessionStart: { enumerable:true, get:()=>jumpToSessionStart, set:value=>{ jumpToSessionStart=value; } },
  _userMessageDomId: { enumerable:true, get:()=>_userMessageDomId, set:value=>{ _userMessageDomId=value; } },
  _questionJumpButtonHtml: { enumerable:true, get:()=>_questionJumpButtonHtml, set:value=>{ _questionJumpButtonHtml=value; } },
  _highlightQuestionRow: { enumerable:true, get:()=>_highlightQuestionRow, set:value=>{ _highlightQuestionRow=value; } },
  jumpToTurnQuestion: { enumerable:true, get:()=>jumpToTurnQuestion, set:value=>{ jumpToTurnQuestion=value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
