import { loadSession } from './session-lifecycle-port.js';
import { renderSessionList } from './session-list-render-port.js';
import { _sessionIdFromLocation } from './session-navigation.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { exitSessionSelectMode } from './sidebar-selection.js';
import { SHOW_ALL_PROFILES_STORAGE_KEY, sidebarStateBindings } from './sidebar-store.js';

async function _handleActiveSessionStorageEvent(e){
  if(!e || e.key !== 'hermes-webui-session') return;
  // Each tab owns its active conversation via its URL. Cross-tab storage
  // changes update sidebar presentation without navigating this tab.
  renderSessionListFromCache();
}

async function _handleShowAllProfilesStorageEvent(e){
  if(!e || e.key !== SHOW_ALL_PROFILES_STORAGE_KEY) return;
  const next=e.newValue==='1'||e.newValue==='true';
  if(sidebarStateBindings._showAllProfiles===next) return;
  sidebarStateBindings._showAllProfiles=next;
  await renderSessionList({deferWhileInteracting:false});
}

function navigateSession(dir){
  const rows=[...document.querySelectorAll('.session-item[data-sid]')];
  const sids=rows.map(r=>r.dataset.sid);
  const cur=S.session&&S.session.session_id;
  const i=sids.indexOf(cur);
  if(i<0||!sids.length)return;
  const next=sids[Math.min(Math.max(i+dir,0),sids.length-1)];
  if(next&&next!==cur) loadSession(next);
}

if(typeof window!=='undefined'){
  window.addEventListener('storage', (e) => {
    void _handleActiveSessionStorageEvent(e);
    void _handleShowAllProfilesStorageEvent(e);
  });
  window.addEventListener('popstate', () => {
    const sid=_sessionIdFromLocation();
    if(!sid || (S.session && S.session.session_id===sid)) return;
    if(S.busy){
      if(typeof showToast==='function') showToast('Finish the current turn before switching sessions.',3000);
      return;
    }
    void loadSession(sid);
  });
}

document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'&&sidebarStateBindings._sessionSelectMode) exitSessionSelectMode();
});

document.addEventListener('keydown',(e)=>{
  if(e.key!=='j'&&e.key!=='k') return;
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  if(typeof _isInteractiveSwipeTarget==='function'&&_isInteractiveSwipeTarget(e.target)) return;
  e.preventDefault();
  navigateSession(e.key==='j'?1:-1);
});

export { navigateSession };
