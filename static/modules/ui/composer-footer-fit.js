// ── Fit-based composer footer collapse ──────────────────────────────────────
// Stage classes on .composer-footer:
//   (none) full labels · .cf-icons icon chips · .cf-icons.cf-burger hamburger.
let _composerFitScheduled=false;
let _composerFitResizeObserver=null;
let _composerFitMutationObserver=null;
let _composerFitObservedFooter=null;
let _composerFitResizeListenerBound=false;

function _fitComposerFooter(){
  const footer=document.querySelector('.composer-footer');
  if(!footer) return;
  const left=footer.querySelector('.composer-left');
  if(!left) return;
  if(!left.clientWidth) return;
  const overflows=function(){return left.scrollWidth>left.clientWidth+1;};
  footer.classList.remove('cf-icons','cf-burger');
  if(!overflows()) return;
  footer.classList.add('cf-icons');
  if(!overflows()) return;
  footer.classList.add('cf-burger');
}
window._fitComposerFooter=_fitComposerFooter;

function _scheduleComposerFit(){
  if(_composerFitScheduled) return;
  _composerFitScheduled=true;
  requestAnimationFrame(function(){
    _composerFitScheduled=false;
    try{_fitComposerFooter();}catch(_){ }
  });
}
window._scheduleComposerFit=_scheduleComposerFit;

function _initComposerFooterFit(){
  const footer=document.querySelector('.composer-footer');
  const left=footer&&footer.querySelector('.composer-left');
  if(!footer||!left) return;
  _scheduleComposerFit();
  if(_composerFitObservedFooter===footer) return;
  if(_composerFitResizeObserver){try{_composerFitResizeObserver.disconnect();}catch(_){ }}
  if(_composerFitMutationObserver){try{_composerFitMutationObserver.disconnect();}catch(_){ }}
  _composerFitResizeObserver=null;
  _composerFitMutationObserver=null;
  _composerFitObservedFooter=footer;
  if(window.ResizeObserver){
    try{
      _composerFitResizeObserver=new ResizeObserver(_scheduleComposerFit);
      _composerFitResizeObserver.observe(footer);
      // Also observe the left control group directly: the footer's outer width
      // may not change when right-side controls (status/context chips) appear or
      // resize, but that shrinks .composer-left's available room and must
      // retrigger a refit. (Codex gate #4657.)
      if(left && left!==footer){try{_composerFitResizeObserver.observe(left);}catch(_){ }}
    }catch(_){ }
  }
  if(window.MutationObserver){
    try{
      _composerFitMutationObserver=new MutationObserver(_scheduleComposerFit);
      _composerFitMutationObserver.observe(left,{
        childList:true,subtree:true,characterData:true,
        attributes:true,attributeFilter:['class','style','hidden']
      });
    }catch(_){ }
  }
  if(!_composerFitResizeListenerBound){
    window.addEventListener('resize',_scheduleComposerFit);
    _composerFitResizeListenerBound=true;
  }
}
window._initComposerFooterFit=_initComposerFooterFit;

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',_initComposerFooterFit);
}else{
  _initComposerFooterFit();
}

export {
  _fitComposerFooter,
  _scheduleComposerFit,
  _initComposerFooterFit,
  _composerFitScheduled,
  _composerFitResizeObserver,
  _composerFitMutationObserver,
  _composerFitObservedFooter,
  _composerFitResizeListenerBound,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _fitComposerFooter: { enumerable: true, get: () => _fitComposerFooter, set: (value) => { _fitComposerFooter = value; } },
  _scheduleComposerFit: { enumerable: true, get: () => _scheduleComposerFit, set: (value) => { _scheduleComposerFit = value; } },
  _initComposerFooterFit: { enumerable: true, get: () => _initComposerFooterFit, set: (value) => { _initComposerFooterFit = value; } },
  _composerFitScheduled: { enumerable: true, get: () => _composerFitScheduled, set: (value) => { _composerFitScheduled = value; } },
  _composerFitResizeObserver: { enumerable: true, get: () => _composerFitResizeObserver, set: (value) => { _composerFitResizeObserver = value; } },
  _composerFitMutationObserver: { enumerable: true, get: () => _composerFitMutationObserver, set: (value) => { _composerFitMutationObserver = value; } },
  _composerFitObservedFooter: { enumerable: true, get: () => _composerFitObservedFooter, set: (value) => { _composerFitObservedFooter = value; } },
  _composerFitResizeListenerBound: { enumerable: true, get: () => _composerFitResizeListenerBound, set: (value) => { _composerFitResizeListenerBound = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
