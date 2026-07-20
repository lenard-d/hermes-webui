import { ICONS, _manualTitleRegenerateTimeoutMs, sessionStateBindings } from './state.js';
import { _isCliSession, _isMessagingSession, _isReadOnlySession } from './message-loading.js';
import { renderSessionList } from './session-list-render-port.js';
import { _sessionDisplayTitle } from './session-display.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _showProjectPicker, deleteSession, removeWorktree } from './management.js';
import { _captureSessionReflowPositions, _sessionPrefersReducedMotion } from './sidebar-motion.js';
import { _sessionUrlForSid } from './session-navigation.js';
import { _getPinnedSessionsLimit, _optimisticallyArchiveSessionInList, _pinnedSessionCount, _pinnedSessionsLimitMessage, _sessionArchiveDescription, _sessionArchiveToast, _sessionDeleteDescription } from './sidebar-cache.js';
import { _sessionSwipeReturnOffsets, sidebarStateBindings } from './sidebar-store.js';

function _focusSessionActionMenuRestoreTarget(target){
  if(!target||!target.isConnected||typeof target.focus!=='function') return false;
  try{target.focus({preventScroll:true});}catch(_){target.focus();}
  return document.activeElement===target;
}

function closeSessionActionMenu({restoreFocus=false}={}){
  const focusTarget=restoreFocus?sidebarStateBindings._sessionActionAnchor:null;
  const fallbackFocusTarget=restoreFocus?sidebarStateBindings._sessionActionPreviousFocus:null;
  if(sidebarStateBindings._sessionActionMenu){
    sidebarStateBindings._sessionActionMenu.remove();
    sidebarStateBindings._sessionActionMenu = null;
  }
  if(sidebarStateBindings._sessionActionAnchor){
    if(sidebarStateBindings._sessionActionAnchor.classList&&sidebarStateBindings._sessionActionAnchor.classList.contains('session-actions-trigger')){
      sidebarStateBindings._sessionActionAnchor.classList.remove('active');
      sidebarStateBindings._sessionActionAnchor.setAttribute('aria-expanded','false');
      sidebarStateBindings._sessionActionAnchor.removeAttribute('aria-controls');
    }
    const row=sidebarStateBindings._sessionActionAnchor.closest('.session-item,.session-child-session');
    if(row) row.classList.remove('menu-open','long-pressing');
    sidebarStateBindings._sessionActionAnchor = null;
  }
  sidebarStateBindings._sessionActionSessionId = null;
  sidebarStateBindings._sessionActionPreviousFocus = null;
  if(!_focusSessionActionMenuRestoreTarget(focusTarget)) _focusSessionActionMenuRestoreTarget(fallbackFocusTarget);
}

function _sessionActionMenuShouldIgnoreScrollTarget(target){
  if(!target || typeof target.closest !== 'function') return false;
  // #5347: active-chat auto-scroll / manual wheel must not dismiss the sidebar menu.
  return Boolean(target.closest('#messages, #msgInner, .messages-inner'));
}

function _sessionActionMenuShouldRepositionOnScroll(target){
  if(!target || typeof target.closest !== 'function') return false;
  return Boolean(target.closest('#sessionList, .session-list'));
}

function _positionSessionActionMenu(anchorEl){
  if(!sidebarStateBindings._sessionActionMenu || !anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(280, Math.max(220, sidebarStateBindings._sessionActionMenu.scrollWidth || 220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  sidebarStateBindings._sessionActionMenu.style.left=left+'px';
  sidebarStateBindings._sessionActionMenu.style.top='8px';
  // Reset any prior clamp so we measure the menu's natural height.
  sidebarStateBindings._sessionActionMenu.style.maxHeight='';
  const menuH=sidebarStateBindings._sessionActionMenu.offsetHeight || 0;
  const margin=8;
  const maxAvail=window.innerHeight-margin*2;
  let top=rect.bottom+6;
  // Prefer flipping above the row when the menu would overflow the bottom and
  // there's room above.
  if(top+menuH>window.innerHeight-margin && rect.top>menuH+12){
    top=rect.top-menuH-6;
  }
  // If the menu is taller than the viewport, or still overflows after the flip
  // attempt (e.g. a top-anchored row with a tall menu and no room above), cap
  // its height to the viewport and let it scroll instead of clipping off-screen.
  if(menuH>maxAvail){
    sidebarStateBindings._sessionActionMenu.style.maxHeight=maxAvail+'px';
    top=margin;
  } else {
    // Clamp vertically so the whole menu stays on-screen at both edges.
    if(top+menuH>window.innerHeight-margin) top=window.innerHeight-margin-menuH;
    if(top<margin) top=margin;
  }
  sidebarStateBindings._sessionActionMenu.style.top=top+'px';
}

function _buildSessionAction(label, meta, icon, onSelect, extraClass=''){
  const opt=document.createElement('button');
  opt.type='button';
  opt.className='ws-opt session-action-opt'+(extraClass?` ${extraClass}`:'');
  opt.setAttribute('role','menuitem');
  // Compact context-menu shape (#3223 redesign, Nathan 2026-06-01): show only
  // icon + label, matching VS Code / browser / ChatGPT conversation menus. The
  // descriptive `meta` is preserved as a hover tooltip (title=) so the
  // information stays discoverable without consuming permanent vertical space —
  // this also keeps the menu short enough to avoid viewport clipping.
  if(meta) opt.title=meta;
  opt.innerHTML=
    `<span class="ws-opt-action">`
      + `<span class="ws-opt-icon">${icon}</span>`
      + `<span class="session-action-copy">`
        + `<span class="ws-opt-name">${esc(label)}</span>`
      + `</span>`
    + `</span>`;
  opt.onclick=async(e)=>{
    e.preventDefault();
    e.stopPropagation();
    await onSelect();
  };
  return opt;
}

function _sessionMarkdownLabel(session){
  const sid=session&&session.session_id?String(session.session_id):'';
  const title=String((session&&(session.title||session.name))||'Conversation').replace(/\s+/g,' ').trim()||'Conversation';
  const shortSid=sid?sid.slice(0,12):'';
  const label=shortSid?`${title} (${shortSid})`:title;
  return label.replace(/([\\\[\]])/g,'\\$1').slice(0,120);
}

function _sessionMarkdownUrlSid(sid){
  return encodeURIComponent(String(sid||'')).replace(/[()]/g, ch => ch==='('?'%28':'%29');
}

function _sessionInternalReferenceForSession(session){
  const sid=session&&session.session_id;
  if(!sid) return '';
  return `[${_sessionMarkdownLabel(session)}](session://${_sessionMarkdownUrlSid(sid)})`;
}

async function _copyTextToClipboard(text){
  if(navigator&&navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){
    await navigator.clipboard.writeText(text);
    return true;
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.setAttribute('readonly','');
  ta.style.position='fixed';
  ta.style.left='-9999px';
  ta.style.top='0';
  document.body.appendChild(ta);
  ta.select();
  try{return document.execCommand('copy');}
  finally{ta.remove();}
}

async function _copySessionLink(session){
  const sid=session&&session.session_id;
  if(!sid) return;
  const ref=(window.location.origin||'')+_sessionUrlForSid(sid);
  try{
    await _copyTextToClipboard(ref);
    showToast(t('session_link_copied'));
  }catch(err){
    showToast(t('session_link_copy_failed')+(err&&err.message?err.message:err));
  }
}

function _mountSessionActionMenu(menu, session, anchorEl){
  sidebarStateBindings._sessionActionPreviousFocus=document.activeElement;
  document.body.appendChild(menu);
  sidebarStateBindings._sessionActionMenu = menu;
  sidebarStateBindings._sessionActionAnchor = anchorEl;
  sidebarStateBindings._sessionActionSessionId = session.session_id;
  if(anchorEl.classList&&anchorEl.classList.contains('session-actions-trigger')){
    anchorEl.classList.add('active');
    anchorEl.setAttribute('aria-expanded','true');
    anchorEl.setAttribute('aria-controls',menu.id);
  }
  const row=anchorEl.closest('.session-item,.session-child-session');
  if(row) row.classList.add('menu-open');
  _positionSessionActionMenu(anchorEl);
  _playSessionActionMenuEntrance(menu);
  const menuItems=()=>Array.from(menu.querySelectorAll('.session-action-opt:not([disabled])'));
  menu.addEventListener('keydown',e=>{
    const items=menuItems();
    if(e.key==='Escape'){
      e.preventDefault();
      e.stopPropagation();
      closeSessionActionMenu({restoreFocus:true});
      return;
    }
    if(!items.length) return;
    const currentIndex=Math.max(0,items.indexOf(document.activeElement));
    let nextIndex=null;
    if(e.key==='ArrowDown') nextIndex=(currentIndex+1)%items.length;
    else if(e.key==='ArrowUp') nextIndex=(currentIndex-1+items.length)%items.length;
    else if(e.key==='Home') nextIndex=0;
    else if(e.key==='End') nextIndex=items.length-1;
    if(nextIndex===null) return;
    e.preventDefault();
    try{items[nextIndex].focus({preventScroll:true});}catch(_){items[nextIndex].focus();}
  });
  const firstAction=menuItems()[0];
  if(firstAction){
    try{firstAction.focus({preventScroll:true});}catch(_){firstAction.focus();}
  }
}

function _findSessionRenameRow(sessionId){
  const sid=String(sessionId||'');
  if(!sid) return null;
  return document.querySelector('.session-item[data-sid="'+sid+'"], .session-child-session[data-sid="'+sid+'"]');
}

function _buildSessionRenameStarter(session, displayEl, renderDisplay){
  return ()=>{
    if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be renamed.',3000); return; }
    if(sessionStateBindings._loadingSessionId&&sessionStateBindings._loadingSessionId!==session.session_id) return;

    closeSessionActionMenu();
    sidebarStateBindings._renamingSid=session.session_id;
    const oldTitle=_sessionDisplayTitle(session)||'Untitled';
    const inp=document.createElement('input');
    inp.className='session-title-input';
    inp.value=oldTitle;
    ['click','mousedown','dblclick','pointerdown'].forEach(ev=>
      inp.addEventListener(ev, e2=>e2.stopPropagation())
    );
    const applyLocalTitle=(target, nextTitle)=>{
      if(!target) return;
      target.title=nextTitle;
      target.display_title=nextTitle;
      target._state_db_title=nextTitle;
    };
    const applyTitle=(nextTitle, updateDom=true)=>{
      applyLocalTitle(session, nextTitle);
      const cached=sidebarStateBindings._allSessions.find(item=>item&&item.session_id===session.session_id);
      applyLocalTitle(cached, nextTitle);
      if(S.session&&S.session.session_id===session.session_id){applyLocalTitle(S.session, nextTitle);syncTopbar();}
      if(updateDom) renderDisplay(_sessionDisplayTitle(session), session);
    };
    let finishDone=false;
    const finish=async(save)=>{
      if(finishDone) return;
      finishDone=true;
      const releaseRename=()=>{
        sidebarStateBindings._renamingSid=null;
        if(inp.isConnected) inp.replaceWith(displayEl);
        setTimeout(()=>{ if(sidebarStateBindings._renamingSid===null) renderSessionListFromCache(); },50);
      };
      if(!save){
        applyTitle(oldTitle,false);
        releaseRename();
        return;
      }
      const newTitle=inp.value.trim()||'Untitled';
      try{
        if(newTitle!==oldTitle){
          await api('/api/session/rename',{method:'POST',body:JSON.stringify({session_id:session.session_id,title:newTitle})});
        }
        applyTitle(newTitle);
      }catch(err){
        applyTitle(oldTitle,false);
        const msg='Rename failed: '+(err&&err.message?err.message:String(err));
        setStatus(msg);
        if(typeof showToast==='function') showToast(msg,3000,'error');
      }finally{
        releaseRename();
      }
    };
    inp.onkeydown=e2=>{
      if(e2.key==='Enter'){
        if(window._isImeEnter&&window._isImeEnter(e2)){return;}
        e2.preventDefault();
        e2.stopPropagation();
        finish(true);
      }
      if(e2.key==='Escape'){e2.preventDefault();e2.stopPropagation();finish(false);}
    };
    inp.onblur=()=>{ if(sidebarStateBindings._renamingSid===session.session_id) finish(true); };
    displayEl.replaceWith(inp);
    setTimeout(()=>{inp.focus();inp.select();},10);
  };
}

function _appendSessionCopyLinkAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_copy_link'),
    t('session_copy_link_desc'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      await _copySessionLink(session);
    }
  ));
}

function _sessionPublicShareUrl(session){
  const token=session&&session.share_token?String(session.share_token).trim():'';
  if(!token) return '';
  return new URL(`/share/${encodeURIComponent(token)}`,location.origin).href;
}

function _syncSessionShareState(session, nextSession){
  if(!session||!nextSession) return;
  session.share_token=nextSession.share_token||null;
  session.share_created_at=nextSession.share_created_at||null;
  const cached=(sidebarStateBindings._allSessions||[]).find(s=>s&&s.session_id===session.session_id);
  if(cached){
    cached.share_token=session.share_token;
    cached.share_created_at=session.share_created_at;
  }
  if(S.session&&S.session.session_id===session.session_id){
    S.session.share_token=session.share_token;
    S.session.share_created_at=session.share_created_at;
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  }
  renderSessionListFromCache();
  void renderSessionList();
}

async function _createOrRefreshSessionShare(session){
  if(!session||!session.session_id) return;
  const existing=_sessionPublicShareUrl(session);
  if(existing){
    const reuse=await showConfirmDialog({
      title:t('share_session'),
      message:t('share_session_existing_confirm'),
      confirmLabel:t('share_session_copy_existing'),
      cancelLabel:t('share_session_refresh_snapshot'),
    });
    if(reuse){
      let copied=true;
      try{ await _copyTextToClipboard(existing); }catch(_){ copied=false; }
      showToast(copied?t('share_session_link_copied'):(t('share_session_status_active')+' — '+existing),copied?2500:6000);
      window.open(existing,'_blank','noopener');
      return;
    }
  }
  const res=await api('/api/share/create',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
  if(res&&res.session) _syncSessionShareState(session,res.session);
  const href=new URL(String(res&&res.share&&res.share.url||''),location.origin).href;
  // The share is now created server-side. A clipboard-copy failure (permissions,
  // focus, non-secure context) must NOT be reported as "Share failed" — the link
  // exists and we still open it. Only surface the copied-vs-not-copied distinction.
  let copied=true;
  try{ await _copyTextToClipboard(href); }catch(_){ copied=false; }
  if(copied){
    showToast(existing?t('share_session_link_copied'):t('share_session_created'));
  }else{
    showToast((existing?t('share_session_created'):t('share_session_created'))+' — '+href,6000);
  }
  window.open(href,'_blank','noopener');
}

async function _revokeSessionShare(session){
  if(!session||!session.session_id||!session.share_token) return;
  const ok=await showConfirmDialog({
    title:t('stop_sharing_session'),
    message:t('stop_sharing_session_confirm'),
    confirmLabel:t('stop_sharing_session'),
    danger:true,
  });
  if(!ok) return;
  const res=await api('/api/share/revoke',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
  if(res&&res.session) _syncSessionShareState(session,res.session);
  showToast(t('share_session_revoked'));
}

function _appendSessionShareActions(menu, session){
  const hasMessages=Number(session&&session.message_count||0)>0;
  if(!hasMessages) return;
  menu.appendChild(_buildSessionAction(
    t('share_session'),
    session&&session.share_token?t('share_session_status_active'):t('share_session_tooltip'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      try{
        await _createOrRefreshSessionShare(session);
      }catch(err){
        showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    },
    session&&session.share_token?'is-active':''
  ));
  if(!(session&&session.share_token)) return;
  menu.appendChild(_buildSessionAction(
    t('share_session_copy_existing'),
    t('share_session_tooltip'),
    ICONS.link,
    async()=>{
      closeSessionActionMenu();
      try{
        const href=_sessionPublicShareUrl(session);
        if(!href) return;
        await _copyTextToClipboard(href);
        showToast(t('share_session_link_copied'));
      }catch(err){
        showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    }
  ));
  menu.appendChild(_buildSessionAction(
    t('stop_sharing_session'),
    t('stop_sharing_session_tooltip'),
    ICONS.trash,
    async()=>{
      closeSessionActionMenu();
      try{
        await _revokeSessionShare(session);
      }catch(err){
        showToast(t('share_session_revoke_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
      }
    },
    'danger'
  ));
}

function _appendSessionDuplicateAction(menu, session){
  menu.appendChild(_buildSessionAction(
    t('session_duplicate'),
    t('session_duplicate_desc'),
    ICONS.dup,
    async()=>{
      closeSessionActionMenu();
      try{
        const res=await api('/api/session/duplicate',{method:'POST',body:JSON.stringify({session_id:session.session_id})});
        if(res.session){
          await loadSession(res.session.session_id);
          await renderSessionList();
          showToast(t('session_duplicated'));
        }
      }catch(err){showToast(t('session_duplicate_failed')+err.message);}
    }
  ));
}

function _appendSessionExportHtmlAction(menu, session){
  // Per-conversation "Export as HTML" — the sidebar ⋮ menu is the app's uniform
  // home for per-conversation actions (matches ChatGPT / Open WebUI). Operates
  // on THIS row's session, not just the active one; the export endpoint accepts
  // any session_id in the active profile and is non-mutating, so it's offered
  // for read-only/imported sessions too. exportSessionHTML(session) is a global
  // defined in boot.js (loaded after sessions.js under defer, so it's bound by
  // the time this click can fire).
  menu.appendChild(_buildSessionAction(
    t('session_export_html'),
    t('session_export_html_desc'),
    ICONS.download,
    ()=>{
      closeSessionActionMenu();
      if(typeof exportSessionHTML==='function') exportSessionHTML(session);
    }
  ));
}

function _playSessionActionMenuEntrance(menu){
  if(!menu) return;
  const reduce=_sessionPrefersReducedMotion();
  if(reduce) return;
  if(typeof menu.animate==='function'){
    try{
      const anim=menu.animate(
        [
          {opacity:0, transform:'translate3d(0,-4px,0) scale(.985)'},
          {opacity:1, transform:'translate3d(0,0,0) scale(1)'}
        ],
        {duration:450, easing:'cubic-bezier(.2,.8,.2,1)'}
      );
      if(anim&&anim.finished) anim.finished.catch(()=>{});
      return;
    }catch(_){}
  }
  menu.classList.add('open-animated');
}

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
  // Rename — first menu item by request (#1764). Double-click rename is
  // timing-sensitive: the first click frequently registers as "open the
  // chat" before the second click arrives, so users open the conversation
  // when they meant to rename it. Putting Rename in the menu eliminates
  // the timing entirely. Only shown for sessions that support rename
  // (read-only imported sessions skip it; same gate as startRename's
  // _isReadOnlySession check).
  if(!_isReadOnlySession(session)){
    menu.appendChild(_buildSessionAction(
      t('session_rename'),
      t('session_rename_desc'),
      ICONS.edit,
      ()=>{
        closeSessionActionMenu();
        // Find the row for this session and call its attached startRename.
        // Falls back to a no-op toast if the row isn't currently rendered
        // (e.g. archived-and-hidden) — extremely rare since the menu only
        // opens from a visible row's three-dot button.
        const row=_findSessionRenameRow(session.session_id);
        if(row && typeof row._startRename === 'function'){
          row._startRename();
        } else if(typeof showToast==='function'){
          showToast(t('session_rename_failed_no_row')||'Could not start rename — row not found.', 3000, 'error');
        }
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
  if(isExternalSession && !session.archived){
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
  if(!isExternalSession){
    _appendSessionDuplicateAction(menu, session);
  }
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
  // Title regeneration stays available for writable imported sessions.
  // Read-only sessions return earlier through the shared action-menu guard.
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
        // Menu Delete has no swipe/removal animation to wait for. Pass an
        // immediate beforeDelete hook so deleteSession() removes the sidebar row
        // optimistically while slow backend cleanup (/api/session/delete,
        // state.db/FTS/journal cleanup) continues.
        await deleteSession(session.session_id,()=>Promise.resolve());
      },
      'danger'
    ));
  }
  _mountSessionActionMenu(menu, session, anchorEl);
}

document.addEventListener('click',e=>{
  if(!sidebarStateBindings._sessionActionMenu) return;
  if(sidebarStateBindings._sessionActionMenu.contains(e.target)) return;
  if(sidebarStateBindings._sessionActionAnchor && sidebarStateBindings._sessionActionAnchor.contains(e.target)) return;
  closeSessionActionMenu();
});
document.addEventListener('scroll',e=>{
  if(!sidebarStateBindings._sessionActionMenu) return;
  if(sidebarStateBindings._sessionActionMenu.contains(e.target)) return;
  if(_sessionActionMenuShouldIgnoreScrollTarget(e.target)) return;
  if(_sessionActionMenuShouldRepositionOnScroll(e.target) && sidebarStateBindings._sessionActionAnchor){
    if(!sidebarStateBindings._sessionActionAnchor.isConnected){
      closeSessionActionMenu();
      return;
    }
    _positionSessionActionMenu(sidebarStateBindings._sessionActionAnchor);
    return;
  }
  closeSessionActionMenu();
}, true);
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && sidebarStateBindings._sessionActionMenu) closeSessionActionMenu({restoreFocus:true});
});
window.addEventListener('resize',()=>{
  if(sidebarStateBindings._sessionActionMenu && sidebarStateBindings._sessionActionAnchor) _positionSessionActionMenu(sidebarStateBindings._sessionActionAnchor);
});

// Generation counter to discard stale API responses (issue #1430).
// Multiple callers (message send, rename, session switch) fire renderSessionList()
// concurrently. Without this guard, a slower older response can overwrite sidebarStateBindings._allSessions
// with stale data, causing sessions to vanish from the sidebar.

export { _archiveSession, _buildSessionRenameStarter, _copySessionLink, _openSessionActionMenu, closeSessionActionMenu };
