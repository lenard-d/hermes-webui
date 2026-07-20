import { $, } from './state.js';
import { INFLIGHT_KEY, INFLIGHT_STATE_KEY, _isStorageQuotaError } from './inflight-state.js';

function markInflight(sid, streamId) {
  const payload=JSON.stringify({sid, streamId, ts: Date.now()});
  try{
    localStorage.setItem(INFLIGHT_KEY, payload);
  }catch(err){
    if(!_isStorageQuotaError(err)) return;
    try{
      localStorage.removeItem(INFLIGHT_STATE_KEY);
      localStorage.setItem(INFLIGHT_KEY, payload);
    }catch(_){}
  }
}
function clearInflight() {
  localStorage.removeItem(INFLIGHT_KEY);
}
function showReconnectBanner(msg) {
  $('reconnectMsg').textContent = msg || 'A response may have been in progress when you last left.';
  $('reconnectBanner').classList.add('visible');
}
function dismissReconnect() {
  $('reconnectBanner').classList.remove('visible');
  clearInflight();
}

export { markInflight, clearInflight, showReconnectBanner, dismissReconnect };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  markInflight: { enumerable: true, get: () => markInflight, set: value => { markInflight = value; } },
  clearInflight: { enumerable: true, get: () => clearInflight, set: value => { clearInflight = value; } },
  showReconnectBanner: { enumerable: true, get: () => showReconnectBanner, set: value => { showReconnectBanner = value; } },
  dismissReconnect: { enumerable: true, get: () => dismissReconnect, set: value => { dismissReconnect = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
