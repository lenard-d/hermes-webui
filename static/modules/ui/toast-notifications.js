import { _copyText } from './clipboard.js';

const TOAST_DEFAULT_MS=2800;
const TOAST_ERROR_DEFAULT_MS=20000;

function _escapeToastMessage(value){
  return String(value).replace(/[&<>"']/g,character=>({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  })[character]);
}

function clearToastDismissTimer(el){if(!el)return;clearTimeout(el._t);el._t=null;}
function setToastDismissTimer(el,duration){if(!el)return;clearToastDismissTimer(el);el._t=setTimeout(()=>{el.classList.remove('show');},duration);}
function dismissToast(btnOrEl){
  const el=btnOrEl&&btnOrEl.closest?btnOrEl.closest('#toast'):(btnOrEl&&btnOrEl.id==='toast'?btnOrEl:null);
  if(!el)return;
  clearToastDismissTimer(el);
  el.classList.remove('show');
}
function copyToastText(btn){
  const el=btn&&btn.closest?btn.closest('#toast'):null;
  const text=el?(el.dataset.toastMessage||el.textContent||''):'';
  const done=()=>{const old=btn.textContent;btn.textContent='Copied';setTimeout(()=>{btn.textContent=old;},1200);};
  _copyText(text).then(done).catch(()=>{});
}
function showToast(msg,ms,type){
  const el=document.getElementById('toast');if(!el)return;
  const s=String(msg==null?'':msg);let t=type;
  if(!t){const low=s.toLowerCase();if(/fail|error|denied|invalid|unavailable|no active|no workspace match|no model match|no personalities/.test(low))t='error';else if(/warn|queued|takes effect|skipped|fallback/.test(low))t='warning';else if(/saved|created|imported|restored|switched|set to|updated|duplicated|moved to|renamed|deleted|complete|pinned|archived|cleared|stopped/.test(low))t='success';else t='info';}
  const duration=(ms==null)?(t==='error'?TOAST_ERROR_DEFAULT_MS:TOAST_DEFAULT_MS):ms;
  el.className='toast show '+t;
  el.dataset.toastMessage=s;
  if(t==='error') el.innerHTML=`<span class="toast-message">${_escapeToastMessage(s)}</span><button class="toast-copy" type="button" data-toast-copy="1" onclick="copyToastText(this);event.stopPropagation()">Copy</button><button class="toast-dismiss" type="button" aria-label="Dismiss error toast" data-toast-dismiss="1" onclick="dismissToast(this);event.stopPropagation()">Dismiss</button>`;
  else el.textContent=s;
  el.onmouseenter=()=>clearToastDismissTimer(el);
  el.onmouseleave=()=>setToastDismissTimer(el,duration);
  el.onfocusin=()=>clearToastDismissTimer(el);
  el.onfocusout=()=>setToastDismissTimer(el,duration);
  el.onclick=t==='error'?null:()=>dismissToast(el);
  setToastDismissTimer(el,duration);
}

function setStatus(text){
  if(!text)return;
  showToast(text,4000);
}

export { TOAST_DEFAULT_MS, TOAST_ERROR_DEFAULT_MS, clearToastDismissTimer, setToastDismissTimer, dismissToast, copyToastText, showToast, setStatus };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  setStatus: { enumerable: true, get: () => setStatus, set: (value) => { setStatus = value; } },
  clearToastDismissTimer: { enumerable: true, get: () => clearToastDismissTimer, set: (value) => { clearToastDismissTimer = value; } },
  setToastDismissTimer: { enumerable: true, get: () => setToastDismissTimer, set: (value) => { setToastDismissTimer = value; } },
  dismissToast: { enumerable: true, get: () => dismissToast, set: (value) => { dismissToast = value; } },
  copyToastText: { enumerable: true, get: () => copyToastText, set: (value) => { copyToastText = value; } },
  showToast: { enumerable: true, get: () => showToast, set: (value) => { showToast = value; } },
  TOAST_DEFAULT_MS: { enumerable: true, get: () => TOAST_DEFAULT_MS },
  TOAST_ERROR_DEFAULT_MS: { enumerable: true, get: () => TOAST_ERROR_DEFAULT_MS },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
