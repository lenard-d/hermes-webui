import { refreshSession } from './session-recovery.js';
import { $, S } from './state.js';

const OFFLINE_RECHECK_MS=2500;
const OFFLINE_HEALTH_TIMEOUT_MS=10000;
const OFFLINE_FETCH_FAILURES_BEFORE_BANNER=2;
let _offlineVisible=false;
let _offlineReason='browser';
let _offlineProbeTimer=null;
let _offlineChecking=false;
let _offlineProbePromise=null;
let _offlineHealthProbePromise=null;
let _offlineFetchProbeFailures=0;
let _offlineRawFetch=null;
let _offlineFetchPatched=false;
function _browserReportsOnline(){return !('onLine' in navigator)||navigator.onLine!==false;}
function _offlineHealthUrl(){const url=new URL('health',document.baseURI||location.href);url.searchParams.set('offline_probe',String(Date.now()));return url.href;}
function _setOfflineChecking(checking){
  _offlineChecking=!!checking;
  const btn=$('offlineCheckNow');
  if(btn){btn.disabled=_offlineChecking;btn.textContent=_offlineChecking?t('offline_checking'):t('offline_check_now');}
}
function _renderOfflineBanner(){
  const banner=$('offlineBanner');
  if(!banner)return;
  const detail=$('offlineDetails');
  if(detail)detail.textContent=t(_offlineReason==='browser'?'offline_browser_detail':'offline_network_detail');
  const title=$('offlineTitle');
  if(title)title.textContent=t('offline_title');
  const auto=$('offlineAutorefresh');
  if(auto)auto.textContent=t('offline_autorefresh');
  _setOfflineChecking(_offlineChecking);
  banner.hidden=false;
  banner.classList.add('visible');
}
function _startOfflineProbeTimer(){
  if(_offlineProbeTimer)return;
  _offlineProbeTimer=setInterval(()=>{checkOfflineRecoveryNow();},OFFLINE_RECHECK_MS);
}
function _stopOfflineProbeTimer(){
  if(_offlineProbeTimer){clearInterval(_offlineProbeTimer);_offlineProbeTimer=null;}
}
function showOfflineBanner(reason){
  _offlineVisible=true;
  _offlineReason=reason||(_browserReportsOnline()?'network':'browser');
  _renderOfflineBanner();
  _startOfflineProbeTimer();
}
function isOfflineBannerVisible(){return _offlineVisible;}
function _hideOfflineBanner(){
  _offlineVisible=false;
  _stopOfflineProbeTimer();
  _setOfflineChecking(false);
  const banner=$('offlineBanner');
  if(banner){banner.classList.remove('visible');banner.hidden=true;}
}
async function _probeOfflineRecovery(){
  if(_offlineHealthProbePromise)return _offlineHealthProbePromise;
  _offlineHealthProbePromise=(async()=>{
    const fetcher=_offlineRawFetch||window.fetch.bind(window);
    // Bound the probe so a black-hole network (connected, server hung, packets
    // dropped) can't delay the banner past a few seconds — the probe now gates
    // the initial banner display on the offline-event/startup paths.
    let ctrl=null,timer=null;
    try{ctrl=(typeof AbortController!=='undefined')?new AbortController():null;}catch(_){ctrl=null;}
    if(ctrl)timer=setTimeout(()=>{try{ctrl.abort();}catch(_){}},OFFLINE_HEALTH_TIMEOUT_MS);
    try{
      const opts={cache:'no-store',credentials:'include'};
      if(ctrl)opts.signal=ctrl.signal;
      const res=await fetcher(_offlineHealthUrl(),opts);
      return !!(res&&res.ok);
    }catch(_){return false;}
    finally{if(timer)clearTimeout(timer);}
  })();
  try{return await _offlineHealthProbePromise;}
  finally{_offlineHealthProbePromise=null;}
}
async function _showOfflineBannerIfProbeFails(reason,opts){
  opts=opts||{};
  const visibleAtStart=_offlineVisible;
  const requireConsecutiveFailures=opts.requireConsecutiveFailures!==false;
  if(visibleAtStart)_setOfflineChecking(true);
  const ok=await _probeOfflineRecovery();
  if(visibleAtStart)_setOfflineChecking(false);
  if(ok){
    _offlineFetchProbeFailures=0;
    if(_offlineVisible){_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();}
    return true;
  }
  if(!visibleAtStart&&requireConsecutiveFailures){
    _offlineFetchProbeFailures+=1;
    if(_offlineFetchProbeFailures<OFFLINE_FETCH_FAILURES_BEFORE_BANNER)return false;
  }
  showOfflineBanner(reason||(_browserReportsOnline()?'network':'browser'));
  return false;
}
async function checkOfflineRecoveryNow(){
  if(_offlineProbePromise)return _offlineProbePromise;
  _offlineProbePromise=(async()=>{
    if(!_offlineVisible)return false;
    _setOfflineChecking(true);
    const ok=await _probeOfflineRecovery();
    _setOfflineChecking(false);
    if(ok){_offlineFetchProbeFailures=0;if(!_offlineVisible)return true;_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();return true;}
    showOfflineBanner(_browserReportsOnline()?'network':'browser');
    return false;
  })();
  try{return await _offlineProbePromise;}
  finally{_offlineProbePromise=null;}
}
// Recover from a transient "Connection lost" without a full page reload.
//
// The offline banner fires whenever a fetch/SSE errors — which Android does
// aggressively every time the PWA is backgrounded, even for a second. The old
// behaviour here was `window.location.reload()`: a hard cold boot that re-runs
// the whole app and re-pulls /api/sessions + /api/session, producing the
// multi-second "reload to see the conversation I was just in" flash on every
// resume. The reload was also intermittent (only when a request actually
// errored that time), matching the reported "sometimes it reloads, sometimes
// it doesn't".
//
// The server keeps the agent running and buffers stream events while no
// subscriber is attached (#2307), so a hard reload is never required to
// recover — we just need to reattach. This does the soft path: hide the
// banner, restart the gateway SSE (bfcache/background kills the connection),
// and re-fetch the active session so any messages that landed while we were
// away appear. A full reload is the fallback only if the soft path throws.
async function _recoverFromOfflineSoftly(){
  try{
    _hideOfflineBanner();
    if(typeof startGatewaySSE==='function') startGatewaySSE();
    if(S.session && typeof refreshSession==='function'){
      await refreshSession();
    }
    // After refreshSession() sets S.activeStreamId, reattach if a stream is live.
    // The server buffers events while no subscriber is attached (#2307/#3863).
    const sid=S.session&&S.session.session_id;
    const streamId=S.session&&S.session.active_stream_id;
    if(sid&&streamId&&typeof attachLiveStream==='function'){
      let status=null;
      try{
        status=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
      }catch(_){/* stream status check failed — leave session refreshed but don't reattach */}
      // Outside the probe's catch so an attachLiveStream throw reaches the
      // outer fallback (hard reload) instead of being silently swallowed.
      if(status&&status.active) attachLiveStream(sid,streamId,S.session.pending_attachments||[],{reconnecting:true});
    }
    return true;
  }catch(_){
    // Soft reattach failed (server mid-restart, session gone, etc.) — fall
    // back to the original hard reload so the user is never stuck offline.
    window.location.reload();
    return false;
  }
}
function _isAbortError(e){return !!(e&&(e.name==='AbortError'||e.code===20));}
function _patchOfflineFetch(){
  if(_offlineFetchPatched||typeof window.fetch!=='function')return;
  _offlineFetchPatched=true;
  _offlineRawFetch=window.fetch.bind(window);
  window.fetch=async function(...args){
    try{return await _offlineRawFetch(...args);}
    catch(e){
      if(!_isAbortError(e)&&(e instanceof TypeError||!_browserReportsOnline())){
        void _showOfflineBannerIfProbeFails(_browserReportsOnline()?'network':'browser');
      }
      throw e;
    }
  };
}
function initOfflineMonitor(){
  _patchOfflineFetch();
  window.addEventListener('offline',()=>{void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});});
  window.addEventListener('online',()=>{if(_offlineVisible)checkOfflineRecoveryNow();});
  if(!_browserReportsOnline())void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initOfflineMonitor,{once:true});
else initOfflineMonitor();
// Redirect to login when the server responds with 401 (auth session expired).
// Handles iOS PWA standalone mode and keeps subpath mounts like /hermes/ from
// escaping to the personal site root /login.
// #5578: on a login-shaped page, reload 'login' WITHOUT a next (avoid self-nesting).

export {
  _browserReportsOnline,
  _offlineHealthUrl,
  _setOfflineChecking,
  _renderOfflineBanner,
  _startOfflineProbeTimer,
  _stopOfflineProbeTimer,
  showOfflineBanner,
  isOfflineBannerVisible,
  _hideOfflineBanner,
  _probeOfflineRecovery,
  _showOfflineBannerIfProbeFails,
  checkOfflineRecoveryNow,
  _recoverFromOfflineSoftly,
  _isAbortError,
  _patchOfflineFetch,
  initOfflineMonitor,
  OFFLINE_RECHECK_MS,
  OFFLINE_HEALTH_TIMEOUT_MS,
  OFFLINE_FETCH_FAILURES_BEFORE_BANNER,
  _offlineVisible,
  _offlineReason,
  _offlineProbeTimer,
  _offlineChecking,
  _offlineProbePromise,
  _offlineHealthProbePromise,
  _offlineFetchProbeFailures,
  _offlineRawFetch,
  _offlineFetchPatched,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _browserReportsOnline: { enumerable: true, get: () => _browserReportsOnline, set: value => { _browserReportsOnline = value; } },
  _offlineHealthUrl: { enumerable: true, get: () => _offlineHealthUrl, set: value => { _offlineHealthUrl = value; } },
  _setOfflineChecking: { enumerable: true, get: () => _setOfflineChecking, set: value => { _setOfflineChecking = value; } },
  _renderOfflineBanner: { enumerable: true, get: () => _renderOfflineBanner, set: value => { _renderOfflineBanner = value; } },
  _startOfflineProbeTimer: { enumerable: true, get: () => _startOfflineProbeTimer, set: value => { _startOfflineProbeTimer = value; } },
  _stopOfflineProbeTimer: { enumerable: true, get: () => _stopOfflineProbeTimer, set: value => { _stopOfflineProbeTimer = value; } },
  showOfflineBanner: { enumerable: true, get: () => showOfflineBanner, set: value => { showOfflineBanner = value; } },
  isOfflineBannerVisible: { enumerable: true, get: () => isOfflineBannerVisible, set: value => { isOfflineBannerVisible = value; } },
  _hideOfflineBanner: { enumerable: true, get: () => _hideOfflineBanner, set: value => { _hideOfflineBanner = value; } },
  _probeOfflineRecovery: { enumerable: true, get: () => _probeOfflineRecovery, set: value => { _probeOfflineRecovery = value; } },
  _showOfflineBannerIfProbeFails: { enumerable: true, get: () => _showOfflineBannerIfProbeFails, set: value => { _showOfflineBannerIfProbeFails = value; } },
  checkOfflineRecoveryNow: { enumerable: true, get: () => checkOfflineRecoveryNow, set: value => { checkOfflineRecoveryNow = value; } },
  _recoverFromOfflineSoftly: { enumerable: true, get: () => _recoverFromOfflineSoftly, set: value => { _recoverFromOfflineSoftly = value; } },
  _isAbortError: { enumerable: true, get: () => _isAbortError, set: value => { _isAbortError = value; } },
  _patchOfflineFetch: { enumerable: true, get: () => _patchOfflineFetch, set: value => { _patchOfflineFetch = value; } },
  initOfflineMonitor: { enumerable: true, get: () => initOfflineMonitor, set: value => { initOfflineMonitor = value; } },
  OFFLINE_RECHECK_MS: { enumerable: true, get: () => OFFLINE_RECHECK_MS },
  OFFLINE_HEALTH_TIMEOUT_MS: { enumerable: true, get: () => OFFLINE_HEALTH_TIMEOUT_MS },
  OFFLINE_FETCH_FAILURES_BEFORE_BANNER: { enumerable: true, get: () => OFFLINE_FETCH_FAILURES_BEFORE_BANNER },
  _offlineVisible: { enumerable: true, get: () => _offlineVisible, set: value => { _offlineVisible = value; } },
  _offlineReason: { enumerable: true, get: () => _offlineReason, set: value => { _offlineReason = value; } },
  _offlineProbeTimer: { enumerable: true, get: () => _offlineProbeTimer, set: value => { _offlineProbeTimer = value; } },
  _offlineChecking: { enumerable: true, get: () => _offlineChecking, set: value => { _offlineChecking = value; } },
  _offlineProbePromise: { enumerable: true, get: () => _offlineProbePromise, set: value => { _offlineProbePromise = value; } },
  _offlineHealthProbePromise: { enumerable: true, get: () => _offlineHealthProbePromise, set: value => { _offlineHealthProbePromise = value; } },
  _offlineFetchProbeFailures: { enumerable: true, get: () => _offlineFetchProbeFailures, set: value => { _offlineFetchProbeFailures = value; } },
  _offlineRawFetch: { enumerable: true, get: () => _offlineRawFetch, set: value => { _offlineRawFetch = value; } },
  _offlineFetchPatched: { enumerable: true, get: () => _offlineFetchPatched, set: value => { _offlineFetchPatched = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
