// URL projection and fail-closed scheme policy for live streaming markdown.
// Settled markdown owns its separate full HTML sanitization pipeline; this
// module protects attributes at the moment smd creates live DOM nodes.
export const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|\/|#|\?|\.|api|session\/)/i;
const _SMD_SAFE_IMG_URL_RE=/^(?:https?:|mailto:|tel:|\/|#|\?|\.)/i;

export function _smdImgSrcAllowed(value){
  const source=String(value||'');
  if(/^data:/i.test(source)){
    return typeof _isSafeDataImageUri==='function'&&_isSafeDataImageUri(source);
  }
  return _SMD_SAFE_IMG_URL_RE.test(source);
}

export function _smdLinkHref(raw){
  const href=String(raw||'');
  if(/^session:\/\//i.test(href)){
    const sid=href.replace(/^session:\/\//i,'').split(/[?#]/)[0];
    try{
      const decoded=decodeURIComponent(sid);
      if(typeof _sessionUrlForSid==='function') return _sessionUrlForSid(decoded);
      return 'session/'+encodeURIComponent(decoded);
    }catch(_){
      return 'session/'+encodeURIComponent(sid);
    }
  }
  if(/^workspace:\/\//i.test(href)){
    try{
      const rel=decodeURIComponent(href.replace(/^workspace:\/\//i,'')).replace(/^~\//,'').replace(/^\.\//,'');
      return '#workspace='+encodeURIComponent(rel);
    }catch(_){
      return '#';
    }
  }
  if(!/^file:\/\//i.test(href)) return href;
  try{
    const path=decodeURIComponent(href.replace(/^file:\/\//i,''));
    return 'api/media?path='+encodeURIComponent(path)+'&inline=1';
  }catch(_){
    return 'api/media?path='+encodeURIComponent(href.replace(/^file:\/\//i,''))+'&inline=1';
  }
}

export function _smdFileHref(raw){
  return _smdLinkHref(raw);
}

export function _sanitizeSmdLinks(root){
  if(!root||!root.querySelectorAll) return;
  const anchors=root.querySelectorAll('a[href]');
  for(const node of anchors){
    const value=node.getAttribute('href')||'';
    if(/^(file|workspace|session):\/\//i.test(value)){
      node.setAttribute('href',_smdLinkHref(value));
      if(node.classList&&/^session:\/\//i.test(value)) node.classList.add('session-link');
      continue;
    }
    if(!_SMD_SAFE_URL_RE.test(value)){
      node.removeAttribute('href');
      node.setAttribute('data-blocked-scheme','1');
    }
  }
  const images=root.querySelectorAll('img[src]');
  for(const node of images){
    const value=node.getAttribute('src')||'';
    if(!_smdImgSrcAllowed(value)){
      node.removeAttribute('src');
      node.setAttribute('data-blocked-scheme','1');
    }
  }
}

export function installSafeSmdAttributes(renderer){
  if(!renderer) return renderer;
  const baseSetAttr=renderer.set_attr;
  renderer.set_attr=(data,attr,value)=>{
    const isHref=window.smd&&attr===window.smd.HREF;
    const isSrc=window.smd&&attr===window.smd.SRC;
    const allowed=isSrc?_smdImgSrcAllowed(value):_SMD_SAFE_URL_RE.test(String(value||''));
    if(isHref&&/^(file|workspace|session):\/\//i.test(String(value||''))){
      baseSetAttr(data,attr,_smdLinkHref(value));
      if(/^session:\/\//i.test(String(value||''))){
        const node=data&&data.nodes&&data.nodes[data.index];
        if(node&&node.classList) node.classList.add('session-link');
      }
      return;
    }
    if((isHref||isSrc)&&!allowed){
      const node=data&&data.nodes&&data.nodes[data.index];
      if(node&&node.setAttribute) node.setAttribute('data-blocked-scheme','1');
      return;
    }
    baseSetAttr(data,attr,value);
  };
  return renderer;
}
