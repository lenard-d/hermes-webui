// ── Shared app dialogs ───────────────────────────────────────────────────────
import { $ } from './state.js';

// showConfirmDialog(opts) and showPromptDialog(opts) replace browser-native dialog calls
// throughout the UI. Both return Promises and support: title, message, confirmLabel,
// cancelLabel, danger (confirm only), placeholder/value/inputType (prompt only).

const APP_DIALOG={resolve:null,kind:null,lastFocus:null};
let _appDialogBound=false;

function _isAppDialogOpen(){
  const overlay=$('appDialogOverlay');
  return !!(overlay&&overlay.style.display!=='none');
}

function _getAppDialogFocusable(){
  return [$('appDialogInput'), $('appDialogCancel'), $('appDialogConfirm'), $('appDialogClose')]
    .filter(el=>el&&el.style.display!=='none'&&!el.disabled);
}

function _finishAppDialog(result, restoreFocus=true){
  const overlay=$('appDialogOverlay');
  const dialog=$('appDialog');
  const input=$('appDialogInput');
  const confirmBtn=$('appDialogConfirm');
  const resolve=APP_DIALOG.resolve;
  const lastFocus=APP_DIALOG.lastFocus;
  APP_DIALOG.resolve=null;
  APP_DIALOG.kind=null;
  APP_DIALOG.lastFocus=null;
  if(overlay){overlay.style.display='none';overlay.setAttribute('aria-hidden','true');}
  if(dialog) dialog.setAttribute('role','dialog');
  if(input){input.value='';input.style.display='none';input.placeholder='';}
  if(confirmBtn){confirmBtn.classList.remove('danger');confirmBtn.textContent=t('dialog_confirm_btn');}
  if(restoreFocus&&lastFocus&&typeof lastFocus.focus==='function'){setTimeout(()=>lastFocus.focus(),0);}
  if(resolve) resolve(result);
}

function _ensureAppDialogBindings(){
  if(_appDialogBound) return;
  _appDialogBound=true;
  const overlay=$('appDialogOverlay');
  const cancelBtn=$('appDialogCancel');
  const confirmBtn=$('appDialogConfirm');
  const closeBtn=$('appDialogClose');
  if(overlay){
    overlay.addEventListener('click',e=>{
      if(e.target===overlay) _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
    });
  }
  if(cancelBtn) cancelBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(closeBtn)  closeBtn.addEventListener('click',()=>_finishAppDialog(APP_DIALOG.kind==='prompt'?null:false));
  if(confirmBtn){
    confirmBtn.addEventListener('click',()=>{
      if(APP_DIALOG.kind==='prompt'){
        const input=$('appDialogInput');
        _finishAppDialog(input?input.value:null);
      }else{
        _finishAppDialog(true);
      }
    });
  }
  document.addEventListener('keydown',e=>{
    if(!_isAppDialogOpen()) return;
    if(e.key==='Escape'){
      e.preventDefault();
      _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
      return;
    }
    if(e.key==='Enter'){
      if(window._isImeEnter&&window._isImeEnter(e)) return;
      const target=e.target;
      const isTextarea=target&&target.tagName==='TEXTAREA';
      if(!isTextarea){
        e.preventDefault();
        if(target===cancelBtn||target===closeBtn){
          _finishAppDialog(APP_DIALOG.kind==='prompt'?null:false);
        }else if(APP_DIALOG.kind==='prompt'){
          const input=$('appDialogInput');
          _finishAppDialog(input?input.value:null);
        }else{
          _finishAppDialog(true);
        }
      }
      return;
    }
    if(e.key==='Tab'){
      const nodes=_getAppDialogFocusable();
      if(!nodes.length) return;
      const idx=nodes.indexOf(document.activeElement);
      let nextIdx=idx;
      if(e.shiftKey){nextIdx=idx<=0?nodes.length-1:idx-1;}
      else{nextIdx=idx===-1||idx===nodes.length-1?0:idx+1;}
      e.preventDefault();
      nodes[nextIdx].focus();
    }
  }, true);
}

function showConfirmDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(false,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='confirm';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_confirm_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){input.style.display='none';input.value='';}
  if(cancelBtn){
    if(opts.hideCancel){cancelBtn.style.display='none';}
    else{cancelBtn.style.display='';cancelBtn.textContent=opts.cancelLabel||t('cancel');}
  }
  if(confirmBtn){
    confirmBtn.textContent=opts.confirmLabel||t('dialog_confirm_btn');
    confirmBtn.classList.toggle('danger',!!opts.danger);
  }
  if(dialog) dialog.setAttribute('role',opts.danger?'alertdialog':'dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>((opts.focusCancel?cancelBtn:confirmBtn)||confirmBtn||cancelBtn).focus(),0);
  });
}

function showPromptDialog(opts={}){
  _ensureAppDialogBindings();
  if(APP_DIALOG.resolve) _finishAppDialog(null,false);
  const overlay=$('appDialogOverlay'),dialog=$('appDialog'),title=$('appDialogTitle'),
    desc=$('appDialogDesc'),input=$('appDialogInput'),cancelBtn=$('appDialogCancel'),confirmBtn=$('appDialogConfirm');
  APP_DIALOG.resolve=null;APP_DIALOG.kind='prompt';APP_DIALOG.lastFocus=document.activeElement;
  if(title) title.textContent=opts.title||t('dialog_prompt_title');
  if(desc) desc.textContent=opts.message||'';
  if(input){
    input.type=opts.inputType||'text';input.style.display='';
    // Pre-fill: prefer `value`, accept `defaultValue` as alias for callers that
    // mirror the standard HTMLInputElement.defaultValue naming. Both empty →
    // blank field (the default rename-from-scratch flow stays unchanged).
    const prefill=(opts.value!=null?opts.value:(opts.defaultValue!=null?opts.defaultValue:''));
    input.value=prefill;input.placeholder=opts.placeholder||'';
    input.autocomplete='off';input.spellcheck=false;
  }
  if(cancelBtn){
    // A prior showConfirmDialog({hideCancel:true}) (e.g. the outside-symlink info
    // dialog, #4581) may have hidden the shared Cancel button; always restore it
    // so a subsequent prompt keeps its Cancel affordance.
    cancelBtn.style.display='';
    cancelBtn.textContent=opts.cancelLabel||t('cancel');
  }
  if(confirmBtn){
    confirmBtn.textContent=opts.confirmLabel||t('create');
    confirmBtn.classList.toggle('danger',!!opts.danger);
  }
  if(dialog) dialog.setAttribute('role',opts.danger?'alertdialog':'dialog');
  if(overlay){overlay.style.display='flex';overlay.setAttribute('aria-hidden','false');}
  return new Promise(resolve=>{
    APP_DIALOG.resolve=resolve;
    setTimeout(()=>{
      if(input&&input.style.display!=='none'){
        input.focus();
        // Selection behavior on focus:
        //   selectStem:true → select everything before the LAST '.' (e.g. for
        //     'report.txt' selects 'report' so a user can retype the basename
        //     without losing the extension; matches macOS Finder rename UX).
        //     Falls back to selecting the full value when there's no '.' or
        //     the dot is at index 0 ('.gitignore' → full select).
        //   selectAll:true → select the entire prefilled value.
        //   default       → caret at end (current behavior).
        const v=input.value||'';
        if(opts.selectStem && v){
          const dot=v.lastIndexOf('.');
          if(dot>0) input.setSelectionRange(0,dot);
          else input.select();
        } else if(opts.selectAll && v){
          input.select();
        }
      } else if(confirmBtn) confirmBtn.focus();
    },0);
  });
}

export {
  APP_DIALOG,
  _appDialogBound,
  _isAppDialogOpen,
  _getAppDialogFocusable,
  _finishAppDialog,
  _ensureAppDialogBindings,
  showConfirmDialog,
  showPromptDialog,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  APP_DIALOG: { enumerable: true, get: () => APP_DIALOG },
  _appDialogBound: { enumerable: true, get: () => _appDialogBound, set: value => { _appDialogBound = value; } },
  _isAppDialogOpen: { enumerable: true, get: () => _isAppDialogOpen, set: value => { _isAppDialogOpen = value; } },
  _getAppDialogFocusable: { enumerable: true, get: () => _getAppDialogFocusable, set: value => { _getAppDialogFocusable = value; } },
  _finishAppDialog: { enumerable: true, get: () => _finishAppDialog, set: value => { _finishAppDialog = value; } },
  _ensureAppDialogBindings: { enumerable: true, get: () => _ensureAppDialogBindings, set: value => { _ensureAppDialogBindings = value; } },
  showConfirmDialog: { enumerable: true, get: () => showConfirmDialog, set: value => { showConfirmDialog = value; } },
  showPromptDialog: { enumerable: true, get: () => showPromptDialog, set: value => { showPromptDialog = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
