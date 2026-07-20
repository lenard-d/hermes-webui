import { SESSION_ICONS as ICONS, _manualTitleRegenerateTimeoutMs } from './session-display.js';
import { _isCliSession, _isMessagingSession } from './session-source.js';
import { _isReadOnlySession } from './sidebar-session-opening.js';
import { _appendExternalHideSessionAction, _archiveSession } from './session-archive-actions.js';
import { _buildSessionAction, _mountSessionActionMenu, closeSessionActionMenu } from './session-action-menu.js';
import { _appendSessionCopyLinkAction, _appendSessionDuplicateAction, _appendSessionExportHtmlAction, _appendSessionShareActions, _copySessionLink } from './session-portability-actions.js';
import { _showProjectPicker } from './session-projects.js';
import { _buildSessionRenameStarter, _startSessionRenameFromRenderedRow } from './session-rename.js';
import { deleteSession, removeWorktree } from './session-removal.js';
import { renderSessionList } from './session-list-render-port.js';
import { _sessionArchiveDescription, _sessionDeleteDescription } from './sidebar-cache.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { sidebarStateBindings } from './sidebar-store.js';

function _openSessionActionMenu(session, anchorEl){
  const isReadOnly = _isReadOnlySession(session);
  if(sidebarStateBindings._sessionActionMenu && sidebarStateBindings._sessionActionSessionId===session.session_id && sidebarStateBindings._sessionActionAnchor===anchorEl){
    closeSessionActionMenu();
    return;
  }
  closeSessionActionMenu();
  const isMessagingSession = _isMessagingSession(session);
  const isCliSession = _isCliSession(session);
  const isExternalSession = isMessagingSession || isCliSession;
  const menu=document.createElement('div');
  menu.className='session-action-menu';
  menu.id='sessionActionMenu-'+(++sidebarStateBindings._sessionActionMenuId);
  menu.setAttribute('role','menu');
  menu.setAttribute('aria-label', 'Conversation actions');
  _appendSessionCopyLinkAction(menu, session);
  if(isReadOnly){
    _appendSessionExportHtmlAction(menu, session);
    _mountSessionActionMenu(menu, session, anchorEl);
    return;
  }
  if(!_isReadOnlySession(session)){
    menu.appendChild(_buildSessionAction(
      t('session_rename'),
      t('session_rename_desc'),
      ICONS.edit,
      ()=>{
        closeSessionActionMenu();
        _startSessionRenameFromRenderedRow(session.session_id);
      }
    ));
  }
  _appendSessionShareActions(menu, session);
  menu.appendChild(_buildSessionAction(
    session.pinned?t('session_unpin'):t('session_pin'),
    session.pinned?t('session_unpin_desc'):t('session_pin_desc'),
    session.pinned?ICONS.pin:ICONS.unpin,
    async()=>{
      closeSessionActionMenu();
      const newPinned=!session.pinned;
      try{
        await api('/api/session/pin',{method:'POST',body:JSON.stringify({session_id:session.session_id,pinned:newPinned})});
        session.pinned=newPinned;
        const cached=(sidebarStateBindings._allSessions||[]).find(s=>s&&s.session_id===session.session_id);
        if(cached) cached.pinned=newPinned;
        if(S.session&&S.session.session_id===session.session_id) S.session.pinned=newPinned;
        renderSessionListFromCache();
        void renderSessionList();
      }catch(err){
        showToast(t('session_pin_failed')+err.message);
        await renderSessionList();
      }
    },
    session.pinned?'is-active':''
  ));
  menu.appendChild(_buildSessionAction(
    t('session_move_project'),
    session.project_id?t('session_move_project_desc_has'):t('session_move_project_desc_none'),
    ICONS.folder,
    async()=>{
      closeSessionActionMenu();
      _showProjectPicker(session, anchorEl);
    }
  ));
  menu.appendChild(_buildSessionAction(
    session.archived?t('session_restore'):t('session_archive'),
    session.archived?t('session_restore_desc'):_sessionArchiveDescription(session),
    session.archived?ICONS.unarchive:ICONS.archive,
    async()=>{
      closeSessionActionMenu();
      await _archiveSession(session,!session.archived);
    }
  ));
  if(isExternalSession) _appendExternalHideSessionAction(menu, session);
  if(!isExternalSession) _appendSessionDuplicateAction(menu, session);
  _appendSessionExportHtmlAction(menu, session);
  if(session.active_stream_id){
    menu.appendChild(_buildSessionAction(
      t('session_stop_response'),
      t('session_stop_response_desc'),
      ICONS.stop,
      async()=>{
        closeSessionActionMenu();
        await cancelSessionStream(session);
        showToast(t('stream_stopped'));
      }
    ));
  }
  menu.appendChild(_buildSessionAction(
    t('session_title_regenerate'),
    t('session_title_regenerate_desc'),
    ICONS.spark,
    async()=>{
      closeSessionActionMenu();
      try{
        if(typeof showToast==='function') showToast(t('session_title_regenerating'), 1600);
        const requestOpts={method:'POST',body:JSON.stringify({session_id:session.session_id})};
        const timeoutMs=await _manualTitleRegenerateTimeoutMs();
        if(timeoutMs) requestOpts.timeoutMs=timeoutMs;
        const response=await api('/api/session/title/regenerate',requestOpts);
        const nextTitle=(response&&response.title)||(response&&response.session&&response.session.title)||'';
        if(nextTitle){
          session.title=nextTitle;
          const cached=(sidebarStateBindings._allSessions||[]).find(item=>item&&item.session_id===session.session_id);
          if(cached) cached.title=nextTitle;
          if(S.session&&S.session.session_id===session.session_id){S.session.title=nextTitle;syncTopbar();}
          renderSessionListFromCache();
        }
        if(typeof showToast==='function') showToast(t('session_title_regenerated', nextTitle||t('untitled')), 2400);
      }catch(err){
        const msg=t('session_title_regenerate_failed')+(err&&err.message?err.message:String(err));
        setStatus(msg);
        if(typeof showToast==='function') showToast(msg,3000,'error');
      }
    }
  ));
  if(!isExternalSession){
    if(session.worktree_path){
      menu.appendChild(_buildSessionAction(
        t('session_worktree_remove'),
        t('session_worktree_remove_desc', session.worktree_path),
        ICONS.trash,
        async()=>{
          closeSessionActionMenu();
          await removeWorktree(session);
        },
        'danger'
      ));
    }
    menu.appendChild(_buildSessionAction(
      t('session_delete'),
      _sessionDeleteDescription(session),
      ICONS.trash,
      async()=>{
        closeSessionActionMenu();
        await deleteSession(session.session_id,()=>Promise.resolve());
      },
      'danger'
    ));
  }
  _mountSessionActionMenu(menu, session, anchorEl);
}

export { _archiveSession, _buildSessionRenameStarter, _copySessionLink, _openSessionActionMenu, closeSessionActionMenu };
