import { _copyText } from './clipboard.js';
import { showToast } from './composer.js';

function copyStatusSessionId(btn){
  const text=btn&&btn.getAttribute('data-copy-status-session');
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=(typeof li==='function')?li('check',13):t('copied');
    btn.classList.add('copied');
    setTimeout(()=>{btn.innerHTML=orig;btn.classList.remove('copied');},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function copyMsg(btn){
  const row=btn.closest('[data-raw-text]');
  const text=row?row.dataset.rawText:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;btn.innerHTML=li('check',13);btn.style.color='var(--blue)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}
function _copyThinkingText(btn){
  const card=btn&&btn.closest?btn.closest('.thinking-card'):null;
  if(!card)return;
  const pre=card.querySelector('.thinking-card-body pre');
  const text=pre?pre.textContent:'';
  if(!text)return;
  _copyText(text).then(()=>{
    const orig=btn.innerHTML;
    btn.innerHTML=li('check',12);
    btn.style.color='var(--accent)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1500);
  }).catch(()=>showToast(t('copy_failed')));
}

export { copyStatusSessionId, copyMsg, _copyThinkingText };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  copyStatusSessionId: { enumerable: true, get: () => copyStatusSessionId, set: value => { copyStatusSessionId = value; } },
  copyMsg: { enumerable: true, get: () => copyMsg, set: value => { copyMsg = value; } },
  _copyThinkingText: { enumerable: true, get: () => _copyThinkingText, set: value => { _copyThinkingText = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
