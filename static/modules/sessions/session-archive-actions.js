import { _isReadOnlySession } from './sidebar-session-opening.js';
import { _buildSessionAction, closeSessionActionMenu } from './session-action-menu.js';
import { renderSessionList } from './session-list-render-port.js';
import { _captureSessionReflowPositions, _sessionPrefersReducedMotion } from './sidebar-motion.js';
import { _optimisticallyArchiveSessionInList, _sessionArchiveToast } from './sidebar-cache.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _sessionSwipeReturnOffsets, sidebarStateBindings } from './sidebar-store.js';

async function _archiveSession(session, archived=true, beforeListRender=null){
  if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be modified.',3000); return false; }
  const reflowPositions=_captureSessionReflowPositions();
  const renderHold=beforeListRender?Promise.resolve().then(beforeListRender):null;
  try{
    const response=await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived})});
    session.archived=archived;
    const cached=(sidebarStateBindings._allSessions||[]).find(s=>s&&s.session_id===session.session_id);
    if(cached) cached.archived=archived;
    if(S.session&&S.session.session_id===session.session_id) S.session.archived=archived;
    try{ if(archived&&session.session_id&&localStorage.getItem('hermes-webui-session')===session.session_id) localStorage.removeItem('hermes-webui-session'); }catch(_){ }
    showToast(session.archived?_sessionArchiveToast(response,session):t('session_restored'));
    if(renderHold) await renderHold;
    if(sidebarStateBindings._showArchived&&!_sessionPrefersReducedMotion()) _sessionSwipeReturnOffsets.set(session.session_id,'0px');
    sidebarStateBindings._pendingSessionReflowPositions=reflowPositions;
    renderSessionListFromCache();
    void renderSessionList();
    return true;
  }catch(err){if(renderHold) await renderHold.catch(()=>{});sidebarStateBindings._pendingSessionReflowPositions=null;showToast(t('session_archive_failed')+err.message);return false;}
}

function _appendExternalHideSessionAction(menu, session){
  if(session.archived) return;
  menu.appendChild(_buildSessionAction(
    t('session_hide_external'),
    t('session_hide_external_desc'),
    ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      try{
        await api('/api/session/archive',{method:'POST',body:JSON.stringify({session_id:session.session_id,archived:true})});
        _optimisticallyArchiveSessionInList(session.session_id,true);
        session.archived=true;
        if(S.session&&S.session.session_id===session.session_id) S.session.archived=true;
        void renderSessionList();
        showToast(t('session_hidden'));
      }catch(err){showToast(t('session_archive_failed')+err.message);}
    }
  ));
}

export { _appendExternalHideSessionAction, _archiveSession };
