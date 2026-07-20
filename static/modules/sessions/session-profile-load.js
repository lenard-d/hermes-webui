import { _invalidateSessionListRenders } from './sidebar-render-state.js';
import { _setProfileSwitchListEmbargo, renderSessionList } from './session-list-loader.js';
import { sessionListViewBindings as sessionListBindings, showSessionListSkeleton } from './session-list-skeleton.js';
import { startGatewaySSE } from './sidebar-session-events.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

function _sessionProfileMismatchFromError(e){
  if(!e || e.status!==409 || !e.body) return null;
  try{
    const body=JSON.parse(e.body);
    if(body && body.code==='session_profile_mismatch' && body.profile){
      return {profile:String(body.profile), session_id:String(body.session_id||'')};
    }
  }catch(_){ }
  return null;
}

async function _switchProfileForSessionLoad(profile){
  const name=String(profile||'').trim();
  if(!name) throw new Error('missing profile');
  if(name===S.activeProfile) return;
  if(typeof _invalidateSessionListRenders==='function') _invalidateSessionListRenders();
  if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(true);
  if(typeof showSessionListSkeleton==='function') showSessionListSkeleton(name);
  try{
    const data=await api('/api/profile/switch',{method:'POST',body:JSON.stringify({name}),timeoutToast:false});
    S.activeProfile=data.active||name;
    S.activeProfileIsDefault=!!data.is_default;
    if(typeof _resetCronUnreadForProfileSwitch==='function'){
      _resetCronUnreadForProfileSwitch();
    }
    if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
    else localStorage.removeItem('hermes-webui-model');
    if(data.default_model) window._defaultModel=data.default_model;
    if(data.default_model_provider) window._activeProvider=data.default_model_provider;
    if(typeof refreshProfileTransitionReasoningChip==='function'){
      refreshProfileTransitionReasoningChip(data.default_model,data.default_model_provider);
    }
    if(typeof startGatewaySSE==='function') startGatewaySSE();
    if(typeof syncTopbar==='function') syncTopbar();
    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);
    if(typeof renderSessionList==='function') await renderSessionList();
  }catch(switchErr){
    // The switch POST failed, so we're still on the previous profile and its
    // caches are intact. Clear the up-front skeleton and re-render the real
    // list so the sidebar doesn't strand on the skeleton (the #4671 strand bug
    // — _sessionListSkeletonActive hard-gates renderSessionListFromCache + the
    // SSE/poll repaints until an unrelated full render fires). Mirror the
    // canonical switch's catch in panels.js, then rethrow so loadSession's
    // catch(switchErr) still routes into the generic error handler.
    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);
    sessionListBindings._sessionListSkeletonActive=false;
    if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();
    throw switchErr;
  }
}

export { _sessionProfileMismatchFromError, _switchProfileForSessionLoad };
