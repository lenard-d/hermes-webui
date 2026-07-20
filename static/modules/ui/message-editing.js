import { msgContent } from './assistant-turn-presentation.js';
import { setStatus } from './toast-notifications.js';
import { _deliberateSessionModelPick, _reArmRecoveryPick } from './model-state.js';
import { renderMessages } from './renderer.js';
import { $, S } from './state.js';

function editMessage(btn) {
  if(S.busy) return;
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const msgIdx = parseInt(row.dataset.msgIdx, 10);
  const originalText = row.dataset.rawText || '';
  const body = row.querySelector('.msg-body');
  if(!body || row.dataset.editing) return;
  row.dataset.editing = '1';

  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = originalText;
  body.replaceWith(ta);
  requestAnimationFrame(() => { autoResizeTextarea(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  ta.addEventListener('input', () => autoResizeTextarea(ta));

  const bar = document.createElement('div');
  bar.className = 'msg-edit-bar';
  bar.innerHTML = `<button class="msg-edit-send">Send edit</button><button class="msg-edit-cancel">Cancel</button>`;
  ta.after(bar);

  bar.querySelector('.msg-edit-send').onclick = async () => {
    const newText = ta.value.trim();
    if(!newText) return;
    await submitEdit(msgIdx, newText);
  };
  bar.querySelector('.msg-edit-cancel').onclick = () => cancelEdit(row, originalText, body);

  ta.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey) { if(window._isImeEnter&&window._isImeEnter(e)) return; e.preventDefault(); bar.querySelector('.msg-edit-send').click(); }
    if(e.key==='Escape') { e.preventDefault(); cancelEdit(row, originalText, body); }
  });
}

function cancelEdit(row, originalText, originalBody) {
  void originalText;
  delete row.dataset.editing;
  const ta = row.querySelector('.msg-edit-area');
  const bar = row.querySelector('.msg-edit-bar');
  if(ta) ta.replaceWith(originalBody);
  if(bar) bar.remove();
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
}

async function submitEdit(msgIdx, newText) {
  if(!S.session || S.busy) return;
  const initialSid = S.session.session_id;
  const absoluteKeepCount = _oldestIdx + msgIdx;
  const _recoveryPick=_deliberateSessionModelPick(initialSid);
  if(typeof _ensureAllMessagesLoaded==='function'){
    await _ensureAllMessagesLoaded();
  }
  if(!S.session || S.session.session_id !== initialSid) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: initialSid,
      keep_count: absoluteKeepCount
    })});
    if(!S.session || S.session.session_id !== initialSid) return;
    S.messages = S.messages.slice(0, absoluteKeepCount);
    renderMessages();
    $('msg').value = newText;
    _reArmRecoveryPick(initialSid, _recoveryPick);
    await send();
  } catch(e) { setStatus(t('edit_failed') + e.message); }
}

async function regenerateResponse(btn) {
  if(!S.session || S.busy) return;
  const row = btn.closest('[data-msg-idx]');
  if(!row) return;
  const assistantIdx = parseInt(row.dataset.msgIdx, 10);
  const absoluteKeepCount = _oldestIdx + assistantIdx;
  const initialSid = S.session.session_id;
  let lastUserText = '';
  for(let i = assistantIdx - 1; i >= 0; i--) {
    const m = S.messages[i];
    if(m && m.role === 'user') { lastUserText = msgContent(m); break; }
  }
  if(!lastUserText) return;
  if(typeof _ensureAllMessagesLoaded==='function'){
    await _ensureAllMessagesLoaded();
  }
  if(!S.session || S.session.session_id !== initialSid) return;
  try {
    await api('/api/session/truncate', {method:'POST', body:JSON.stringify({
      session_id: initialSid,
      keep_count: absoluteKeepCount
    })});
    S.messages = S.messages.slice(0, absoluteKeepCount);
    renderMessages();
    $('msg').value = lastUserText;
    await send();
  } catch(e) { setStatus(t('regen_failed') + e.message); }
}

export {
  editMessage,
  cancelEdit,
  autoResizeTextarea,
  submitEdit,
  regenerateResponse,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  editMessage: { enumerable: true, get: () => editMessage, set: (value) => { editMessage = value; } },
  cancelEdit: { enumerable: true, get: () => cancelEdit, set: (value) => { cancelEdit = value; } },
  autoResizeTextarea: { enumerable: true, get: () => autoResizeTextarea, set: (value) => { autoResizeTextarea = value; } },
  submitEdit: { enumerable: true, get: () => submitEdit, set: (value) => { submitEdit = value; } },
  regenerateResponse: { enumerable: true, get: () => regenerateResponse, set: (value) => { regenerateResponse = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
