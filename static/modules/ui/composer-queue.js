import { scrollIfPinned } from './activity-and-scroll.js';
import { S } from './state.js';
import { SESSION_QUEUES, _clearPersistedSessionQueue, _getSessionQueue, _persistSessionQueueStorage, getQueuedSessionCount } from './session-queue-state.js';
import { showToast } from './toast-notifications.js';

const _queueRenderKeys={};  // per-session fingerprint to avoid redundant rebuilds
const _queueCollapsed={};   // per-session: true when user explicitly collapsed the card
let _queueRenderEpoch=0;
function _clearQueueCardDisplay(sid){
  const card=document.getElementById('queueCard');
  const chips=document.getElementById('queueChips');
  if(sid) delete _queueRenderKeys[sid];
  if(card) card.classList.remove('visible');
  if(chips){
    const _chips=chips;
    const _card=card;
    const _sid=String(sid||'');
    const _epoch=_chips.getAttribute('data-queue-render-epoch')||'';
    setTimeout(()=>{
      if((_card&&!_card.classList.contains('visible'))||!_card){
        if((_chips.getAttribute('data-queue-render-sid')||'')===_sid&&(_chips.getAttribute('data-queue-render-epoch')||'')===_epoch){
          _chips.innerHTML='';
        }
      }
    },360);
  }
  const _msgsEl=document.getElementById('messages');
  if(_msgsEl) _msgsEl.classList.remove('queue-open');
  _updateQueuePill(sid,0);
}

function _renderQueueChips(sid){
  const card=document.getElementById('queueCard');
  const inner=document.getElementById('queueChips');
  if(!card||!inner) return;
  const q=_getSessionQueue(sid,false);
  const key=q.map(e=>{const t=e&&(e.text||e.message||e.content||'');return(e&&e._queued_at||0)+':'+t.length+':'+t.slice(0,20);}).join('|');
  if(key===(_queueRenderKeys[sid]||'')&&key!='') return;
  // Skip re-render if user is actively editing inside the queue panel
  if(inner.contains(document.activeElement)&&document.activeElement!==inner) return;
  _queueRenderKeys[sid]=key;
  inner.setAttribute('data-queue-render-sid',sid);
  inner.setAttribute('data-queue-render-epoch',String(++_queueRenderEpoch));
  inner.innerHTML='';
  if(!q.length){
    card.classList.remove('visible');
    const _msgs=document.getElementById('messages');
    if(_msgs) _msgs.classList.remove('queue-open');
    return;
  }
  // Respect user-collapsed state — don't reopen if user explicitly hid the card
  if(_queueCollapsed[sid]){
    // Update chips content without showing card (so data is fresh if user re-expands)
    inner.innerHTML='';
    // fall through to render rows into inner but skip making card visible
  } else {
    card.classList.add('visible');
  }
  // Push messages area up so content isn't hidden behind the flyout
  const _msgs=document.getElementById('messages');
  if(_msgs&&!_queueCollapsed[sid]){
    _msgs.classList.add('queue-open');
    // Measure after 350ms transition completes (not mid-animation — height would be wrong)
    setTimeout(()=>{
      if(!card.classList.contains('visible')) return;
      const h=card.getBoundingClientRect().height;
      if(h>0) _msgs.style.setProperty('--queue-card-height', h+'px');
      if(typeof scrollIfPinned==='function') scrollIfPinned();
    }, 360);
  }

  function _saveAndRefresh(){
    const liveQ=_getSessionQueue(sid,false);
    if(!liveQ.length){delete SESSION_QUEUES[sid];_clearPersistedSessionQueue(sid);}
    else{SESSION_QUEUES[sid]=[...liveQ];_persistSessionQueueStorage(sid,liveQ);}
    delete _queueRenderKeys[sid];
    updateQueueBadge(sid);
  }

  // Header (2+ items)
  if(q.length>1){
    const header=document.createElement('div');
    header.className='queue-card-header';
    const lbl=document.createElement('span');
    lbl.textContent=typeof t==='function'?t('queued_count',q.length):(q.length===1?'1 queued':`${q.length} queued`);
    lbl.title='Sends automatically after the current response completes';
    const actions=document.createElement('span');
    actions.className='queue-card-header-actions';
    const hasFiles=q.some(e=>e&&Array.isArray(e.files)&&e.files.length>0);
    const mergeBtn=document.createElement('button');
    mergeBtn.className='queue-card-btn';
    mergeBtn.title='Combine all into one message'+(hasFiles?' — attachments will be removed':'');
    mergeBtn.innerHTML=li('layers',12)+'Combine';
    mergeBtn.onclick=()=>{
      const _doMerge=(snapshot)=>{
        const combined=snapshot.map(e=>e&&(e.text||e.message||e.content||'')).filter(Boolean).join('\n\n');
        const liveQ=_getSessionQueue(sid,false);
        const first=snapshot.find(e=>e)||{};
        const firstFiles=(snapshot.find(e=>e&&Array.isArray(e.files)&&e.files.length)||{files:[]}).files;
        liveQ.length=0;liveQ.push({text:combined,files:firstFiles,model:first.model||'',model_provider:first.model_provider||null,_queued_at:Date.now()});
        SESSION_QUEUES[sid]=liveQ;
        _persistSessionQueueStorage(sid,liveQ);
        delete _queueRenderKeys[sid];
        updateQueueBadge(sid);
      };
      if(hasFiles){
        if(typeof showToast==='function') showToast('Attachments on queued items will be removed',2600,'warning');
      }
      // Merge from current live queue (no delay — snapshot + defer caused data-loss races)
      _doMerge([..._getSessionQueue(sid,false)]);
    };
    const clearBtn=document.createElement('button');
    clearBtn.className='queue-card-icon-btn';
    clearBtn.title='Clear all queued messages';
    clearBtn.setAttribute('aria-label','Clear all queued messages');
    clearBtn.innerHTML=li('x',13);
    clearBtn.onclick=()=>{q.length=0;_saveAndRefresh();};
    actions.appendChild(mergeBtn);
    actions.appendChild(clearBtn);
    // Hide button — collapses flyout entirely; queue pill re-shows it
    const hideBtn=document.createElement('button');
    hideBtn.className='queue-card-icon-btn';
    hideBtn.title='Hide queue (click the queue pill to show again)';
    hideBtn.setAttribute('aria-label','Hide queue panel');
    hideBtn.innerHTML=li('chevron-down',14);
    hideBtn.onclick=()=>{
      _queueCollapsed[sid]=true;
      card.classList.remove('visible');
      // Read live count at click time (not stale closure q)
      _updateQueuePill(sid,_getSessionQueue(sid,false).length);
    };
    actions.appendChild(hideBtn);
    header.appendChild(lbl);
    header.appendChild(actions);
    inner.appendChild(header);
  }

  let _dragTs=null;  // use _queued_at timestamp — survives re-renders, not an index
  q.forEach((entry,i)=>{
    const _entryTs=entry&&entry._queued_at;
    const entryText=entry&&(entry.text||entry.message||entry.content||'');
    const _files=entry&&Array.isArray(entry.files)?entry.files.filter(Boolean):[];
    const row=document.createElement('div');
    row.className='queue-card-row';
    row.setAttribute('role','listitem');
    row.setAttribute('draggable','true');
    row.ondragstart=(e)=>{if(_entryTs==null) return;_dragTs=_entryTs;row.style.opacity='.4';e.dataTransfer.effectAllowed='move';};
    row.ondragend=()=>{row.style.opacity='';};
    row.ondragover=(e)=>{e.preventDefault();row.style.background='var(--hover-bg)';};
    row.ondragleave=()=>{row.style.background='';};
    row.ondrop=(e)=>{
      e.preventDefault();row.style.background='';
      if(_dragTs!=null&&_dragTs!==_entryTs){
        const fromIdx=q.findIndex(e=>e&&e._queued_at===_dragTs);
        if(fromIdx!==-1&&fromIdx!==i){const moved=q.splice(fromIdx,1)[0];q.splice(i,0,moved);}
        _dragTs=null;_saveAndRefresh();
      }
    };
    // Drag handle
    const drag=document.createElement('span');
    drag.className='queue-card-drag';
    drag.setAttribute('aria-hidden','true');
    drag.innerHTML=typeof li==='function'?li('list-todo',13):'≡';
    // Inline-editable text
    const msgSpan=document.createElement('span');
    msgSpan.className='queue-card-text';
    msgSpan.setAttribute('contenteditable','true');
    msgSpan.setAttribute('role','textbox');
    msgSpan.setAttribute('aria-label','Queued message — edit in place');
    msgSpan.textContent=entryText||(_files.length?'':'—');
    msgSpan.setAttribute('draggable','false');
    msgSpan.onfocus=()=>{msgSpan.style.overflow='auto';msgSpan.style.whiteSpace='pre-wrap';msgSpan.style.textOverflow='clip';};
    msgSpan.onblur=()=>{
      msgSpan.style.overflow='';msgSpan.style.whiteSpace='';msgSpan.style.textOverflow='';
      const newText=msgSpan.textContent.trim();
      if(newText===''&&!_files.length){ msgSpan.textContent=entryText||'—'; return; }
      if(newText!==entryText){
        const liveQ=_getSessionQueue(sid,false);
        const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
        if(idx!==-1){
          liveQ[idx]={...liveQ[idx],text:newText};
          _persistSessionQueueStorage(sid,liveQ);
          delete _queueRenderKeys[sid];
          updateQueueBadge(sid);
        }
      }
    };
    msgSpan.onkeydown=(e)=>{if(e.key==='Enter'){e.preventDefault();msgSpan.blur();}if(e.key==='Escape'){msgSpan.textContent=entryText||'—';msgSpan.blur();}};
    // Compact badges (files, model, profile)
    const badges=document.createElement('span');
    badges.className='queue-card-badges';
    if(_files.length>0){
      const fb=document.createElement('span');
      fb.className='queue-card-file-badge';
      fb.title=_files.map(f=>f&&f.name||'file').join(', ');
      fb.innerHTML=li('paperclip',11)+_files.length;
      badges.appendChild(fb);
    }
    const _model=entry&&entry.model;
    if(_model){
      const mb=document.createElement('span');
      mb.title='Model: '+_model;
      // Use the app's friendly label system if available
      const _modelLabel=(typeof _dynamicModelLabels!=='undefined'&&_dynamicModelLabels[_model])
        ||_model.split('/').pop().replace(/^(gpt-|claude-3\.?5?-|claude-|gemini-)/,'').replace(/-\d{4}-\d{2}-\d{2}$/,'').slice(0,12);
      mb.textContent=_modelLabel;
      badges.appendChild(mb);
    }
    // Profile badge removed — drain cannot server-switch profiles so badge was misleading
    // Delete button
    const delBtn=document.createElement('button');
    delBtn.className='queue-card-icon-btn';
    delBtn.setAttribute('aria-label',typeof t==='function'?t('queued_cancel'):'Remove queued message');
    delBtn.setAttribute('draggable','false');
    delBtn.title='Remove from queue';
    delBtn.innerHTML=li('x',13);
    delBtn.onclick=()=>{
      const liveQ=_getSessionQueue(sid,false);
      const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;
      if(idx!==-1) liveQ.splice(idx,1);
      if(!liveQ.length){delete SESSION_QUEUES[sid];_clearPersistedSessionQueue(sid);}
      else{SESSION_QUEUES[sid]=[...liveQ];_persistSessionQueueStorage(sid,liveQ);}
      delete _queueRenderKeys[sid];
      updateQueueBadge(sid);
    };
    row.appendChild(drag);
    row.appendChild(msgSpan);
    if(badges.childNodes.length) row.appendChild(badges);
    row.appendChild(delBtn);
    inner.appendChild(row);
  });
}

function _updateQueuePill(sid,count){
  const pill=document.getElementById('queuePill');
  if(!pill) return;
  const pillOuter=pill.parentElement;  // .queue-pill-outer — same wrapper as .queue-card
  const card=document.getElementById('queueCard');
  const flyoutVisible=card&&card.classList.contains('visible');
  if(count>0&&!flyoutVisible){
    const label=typeof t==='function'?t('queued_count',count):(count===1?'1 queued':`${count} queued`);
    pill.innerHTML=(typeof li==='function'?li('list-todo',12):'')+
      `<span class="queue-pill-count">${label}</span>`+
      `<span class="queue-pill-chevron">`+(typeof li==='function'?li('chevron-up',12):'▲')+`</span>`;
    pill.title='Show queued messages';
    if(pillOuter) pillOuter.classList.add('show');
    pill.onclick=()=>{
      delete _queueCollapsed[sid];
      const c=document.getElementById('queueCard');
      if(c){
        c.classList.add('visible');
        setTimeout(()=>{
          const firstFocusable=c.querySelector('.queue-card-text, .queue-card-icon-btn');
          if(firstFocusable) firstFocusable.focus();
        }, 360);
      }
      if(pillOuter) pillOuter.classList.remove('show');
      if(typeof scrollIfPinned==='function') scrollIfPinned();
    };
  } else {
    if(pillOuter) pillOuter.classList.remove('show');
    pill.onclick=null;
  }
}

function updateQueueBadge(sessionId){
  const sid=sessionId||(S.session&&S.session.session_id);
  const count=sid?getQueuedSessionCount(sid):0;
  if(count>0&&S.session&&sid===S.session.session_id){
    _renderQueueChips(sid);
    // If card is visible, hide pill. If card is collapsed, update pill count.
    const _cardEl=document.getElementById('queueCard');
    _updateQueuePill(sid,(_cardEl&&_cardEl.classList.contains('visible'))?0:count);
  } else {
    // Always clean up per-session data
    if(sid){delete _queueRenderKeys[sid];delete _queueCollapsed[sid];}
    // Only wipe global DOM if this is the currently active session
    const isActive=S.session&&sid===S.session.session_id;
    if(isActive){
      _clearQueueCardDisplay(sid);
    }
  }
}

export { _clearQueueCardDisplay, _renderQueueChips, _updateQueuePill, updateQueueBadge, _queueRenderKeys, _queueCollapsed, _queueRenderEpoch };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _clearQueueCardDisplay: { enumerable: true, get: () => _clearQueueCardDisplay, set: (value) => { _clearQueueCardDisplay = value; } },
  _renderQueueChips: { enumerable: true, get: () => _renderQueueChips, set: (value) => { _renderQueueChips = value; } },
  _updateQueuePill: { enumerable: true, get: () => _updateQueuePill, set: (value) => { _updateQueuePill = value; } },
  updateQueueBadge: { enumerable: true, get: () => updateQueueBadge, set: (value) => { updateQueueBadge = value; } },
  _queueRenderKeys: { enumerable: true, get: () => _queueRenderKeys },
  _queueCollapsed: { enumerable: true, get: () => _queueCollapsed },
  _queueRenderEpoch: { enumerable: true, get: () => _queueRenderEpoch, set: (value) => { _queueRenderEpoch = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
