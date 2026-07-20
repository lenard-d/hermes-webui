function _sessionIdFromLocation(){
  if(typeof window==='undefined'||!window.location) return null;
  const marker='/session/';
  const path=window.location.pathname||'';
  const idx=path.indexOf(marker);
  if(idx>=0){
    const raw=path.slice(idx+marker.length).split('/')[0];
    if(raw){try{return decodeURIComponent(raw);}catch(_e){return raw;}}
  }
  try{
    const qs=new URLSearchParams(window.location.search||'');
    return qs.get('session')||qs.get('session_id')||null;
  }catch(_e){return null;}
}
function _composerPrefillIntentFromLocation(){
  const empty={hasParams:false,hasText:false,text:'',autoSend:false};
  if(typeof window==='undefined'||!window.location) return empty;
  try{
    const qs=new URLSearchParams(window.location.search||'');
    const hasQ=qs.has('q');
    const hasPrompt=qs.has('prompt');
    const hasSend=qs.has('send');
    if(!hasQ&&!hasPrompt&&!hasSend) return empty;
    const text=hasQ?(qs.get('q')||''):(hasPrompt?(qs.get('prompt')||''):'');
    return {
      hasParams:true,
      hasText:!!String(text).trim(),
      text,
      autoSend:false
    };
  }catch(_e){return empty;}
}
function _profileQueryIntentFromLocation(){
  const empty={hasParam:false,valid:false,name:''};
  if(typeof window==='undefined'||!window.location) return empty;
  try{
    const qs=new URLSearchParams(window.location.search||'');
    if(!qs.has('profile')) return empty;
    const name=String(qs.get('profile')||'');
    return {
      hasParam:true,
      valid:/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name),
      name
    };
  }catch(_e){return empty;}
}
function _consumeProfileQueryParamFromLocation(){
  if(typeof window==='undefined'||!window.location||!window.history||typeof window.history.replaceState!=='function') return;
  try{
    const current=new URL(window.location.href);
    const before=current.searchParams.toString();
    current.searchParams.delete('profile');
    const after=current.searchParams.toString();
    if(after===before) return;
    const next=current.pathname+(after?`?${after}`:'')+(current.hash||'');
    window.history.replaceState(window.history.state||null,'',next);
  }catch(_e){}
}
function _consumeComposerPrefillParamsFromLocation(){
  if(typeof window==='undefined'||!window.location||!window.history||typeof window.history.replaceState!=='function') return;
  try{
    const current=new URL(window.location.href);
    const before=current.searchParams.toString();
    current.searchParams.delete('q');
    current.searchParams.delete('prompt');
    current.searchParams.delete('send');
    const after=current.searchParams.toString();
    if(after===before) return;
    const next=current.pathname+(after?`?${after}`:'')+(current.hash||'');
    window.history.replaceState(window.history.state||null,'',next);
  }catch(_e){}
}
function _appRootPath(){
  try{
    const base = new URL(document.baseURI||window.location.origin+'/', window.location.origin);
    return base.pathname || '/';
  }catch(_e){return '/';}
}
function _sessionUrlForSid(sid){
  const encoded=encodeURIComponent(sid);
  let base;
  try{base=new URL(`session/${encoded}`, document.baseURI||window.location.origin+'/');}
  catch(_e){base=new URL(`/session/${encoded}`, window.location.origin);}
  try{
    const current=new URL(window.location.href);
    current.searchParams.delete('session');
    current.searchParams.delete('session_id');
    current.searchParams.delete('q');
    current.searchParams.delete('prompt');
    current.searchParams.delete('send');
    base.search=current.searchParams.toString();
    base.hash=current.hash;
  }catch(_e){}
  return base.pathname+base.search+base.hash;
}
function _setActiveSessionUrl(sid){
  if(typeof window==='undefined'||!window.history||!sid) return;
  const next=_sessionUrlForSid(sid);
  if(next && next!==(window.location.pathname+window.location.search+window.location.hash)){
    window.history.pushState({session_id:sid},'',next);
  }
}

function _activeSessionIdForSidebar(){
  if(S.session&&S.session.session_id) return S.session.session_id;
  return _sessionIdFromLocation();
}

export { _activeSessionIdForSidebar, _appRootPath, _composerPrefillIntentFromLocation, _consumeComposerPrefillParamsFromLocation, _consumeProfileQueryParamFromLocation, _profileQueryIntentFromLocation, _sessionIdFromLocation, _sessionUrlForSid, _setActiveSessionUrl };
