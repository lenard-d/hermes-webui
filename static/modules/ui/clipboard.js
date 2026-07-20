function _copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(text).catch(()=>{
      // Fallback if clipboard API fails (e.g. permissions)
      return _fallbackCopy(text);
    });
  }
  return _fallbackCopy(text);
}
function _fallbackCopy(text){
  return new Promise((resolve,reject)=>{
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.cssText='position:fixed;left:0;top:0;width:2em;height:2em;padding:0;border:none;outline:none;box-shadow:none;background:transparent;z-index:-1';
    document.body.appendChild(ta);
    ta.focus();ta.select();
    try{document.execCommand('copy');resolve();}
    catch(e){reject(e);}
    finally{document.body.removeChild(ta);}
  });
}

export { _copyText, _fallbackCopy };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _copyText: { enumerable: true, get: () => _copyText, set: value => { _copyText = value; } },
  _fallbackCopy: { enumerable: true, get: () => _fallbackCopy, set: value => { _fallbackCopy = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
