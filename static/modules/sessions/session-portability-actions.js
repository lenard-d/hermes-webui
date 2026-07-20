import { SESSION_ICONS as ICONS } from './session-display.js';
import { _buildSessionAction, closeSessionActionMenu } from './session-action-menu.js';
import { renderSessionList } from './session-list-render-port.js';
import { _sessionUrlForSid } from './session-navigation.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { sidebarStateBindings } from './sidebar-store.js';

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
  let copied=true;
  try{ await _copyTextToClipboard(href); }catch(_){ copied=false; }
  if(copied) showToast(existing?t('share_session_link_copied'):t('share_session_created'));
  else showToast(t('share_session_created')+' — '+href,6000);
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
      try{await _createOrRefreshSessionShare(session);}
      catch(err){showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');}
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
      }catch(err){showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');}
    }
  ));
  menu.appendChild(_buildSessionAction(
    t('stop_sharing_session'),
    t('stop_sharing_session_tooltip'),
    ICONS.trash,
    async()=>{
      closeSessionActionMenu();
      try{await _revokeSessionShare(session);}
      catch(err){showToast(t('share_session_revoke_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');}
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

export { _appendSessionCopyLinkAction, _appendSessionDuplicateAction, _appendSessionExportHtmlAction, _appendSessionShareActions, _copySessionLink, _sessionInternalReferenceForSession };
