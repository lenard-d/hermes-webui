// Shared browser application state. Feature-specific lifecycle state belongs to
// its domain owner; this object remains identity-stable for legacy consumers.
const S={session:null,messages:[],entries:[],busy:false,pendingFiles:[],toolCalls:[],activeStreamId:null,currentDir:'.',activeProfile:'default',activeProfileIsDefault:true,showHiddenWorkspaceFiles:false,todos:[],todoStateMeta:null,_pendingSessionToolsets:null};
const INFLIGHT={}; // keyed by session_id while a request is in flight
const MAX_UPLOAD_BYTES=(window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.maxUploadBytes)||20*1024*1024;
const MAX_UPLOAD_MB=Math.round(MAX_UPLOAD_BYTES/1024/1024);
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function assistantDisplayName(){
  if(S.activeProfile&&S.activeProfile!=='default') return S.activeProfile.charAt(0).toUpperCase()+S.activeProfile.slice(1);
  return window._botName||'Hermes';
}

// Redirect to login when the server responds with 401 (auth session expired).
// Preserve subpath mounts and avoid recursively nesting a login return URL.
function _redirectIfUnauth(res){
  if(!res||res.status!==401) return false;
  const path=(window.location.pathname||'').replace(/\/+$/,'');
  window.location.href=/(?:^|\/)login$/.test(path)
    ? 'login'
    : 'login?next='+encodeURIComponent(window.location.pathname+window.location.search);
  return true;
}

export { S, INFLIGHT, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, $, esc, assistantDisplayName, _redirectIfUnauth };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  S: { enumerable:true, get:()=>S },
  INFLIGHT: { enumerable:true, get:()=>INFLIGHT },
  MAX_UPLOAD_BYTES: { enumerable:true, get:()=>MAX_UPLOAD_BYTES },
  MAX_UPLOAD_MB: { enumerable:true, get:()=>MAX_UPLOAD_MB },
  $: { enumerable:true, get:()=>$ },
  esc: { enumerable:true, get:()=>esc },
  assistantDisplayName: { enumerable:true, get:()=>assistantDisplayName, set:value=>{ assistantDisplayName=value; } },
  _redirectIfUnauth: { enumerable:true, get:()=>_redirectIfUnauth, set:value=>{ _redirectIfUnauth=value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
