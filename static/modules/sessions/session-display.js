function _stripAttachedFilesMarker(text){
  return String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
}

function _sessionDisplayTitle(s){
  const rawTitle=String((s&&(s.display_title||s._state_db_title||s.title))||'Untitled').trim();
  const strip=(typeof _stripAttachedFilesMarker==='function')
    ? _stripAttachedFilesMarker
    : (text)=>String(text||'').replace(/\n\n\[Attached files: [^\]]+\]$/,'').trim();
  const title=strip(rawTitle);
  return title||'Untitled';
}

function _sessionTitleIsDefaultWebUI(rawTitle){
  const title=String(rawTitle||'').replace(/\s+/g,' ').trim();
  return title==='Hermes WebUI'||/^Hermes WebUI #\d+$/.test(title);
}

function _sessionTitleTags(rawTitle){
  if(_sessionTitleIsDefaultWebUI(rawTitle)) return [];
  return String(rawTitle||'').match(/#(?!\d+\b)[\w-]+/g)||[];
}

export { _sessionDisplayTitle, _sessionTitleIsDefaultWebUI, _sessionTitleTags, _stripAttachedFilesMarker };
