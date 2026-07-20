import { _sessionDisplayTitle } from './session-display.js';
import { sessionLoadState } from './session-load-state.js';
import { _isReadOnlySession } from './sidebar-session-opening.js';
import { closeSessionActionMenu } from './session-action-menu.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { sidebarStateBindings } from './sidebar-store.js';

function _findSessionRenameRow(sessionId){
  const sid=String(sessionId||'');
  if(!sid) return null;
  return document.querySelector('.session-item[data-sid="'+sid+'"], .session-child-session[data-sid="'+sid+'"]');
}

function _startSessionRenameFromRenderedRow(sessionId){
  const row=_findSessionRenameRow(sessionId);
  if(row && typeof row._startRename === 'function'){
    row._startRename();
    return;
  }
  if(typeof showToast==='function'){
    showToast(t('session_rename_failed_no_row')||'Could not start rename — row not found.', 3000, 'error');
  }
}

function _buildSessionRenameStarter(session, displayEl, renderDisplay){
  return ()=>{
    if(_isReadOnlySession(session)){ if(typeof showToast==='function') showToast('Read-only imported sessions cannot be renamed.',3000); return; }
    if(sessionLoadState.loadingSessionId&&sessionLoadState.loadingSessionId!==session.session_id) return;

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

export { _buildSessionRenameStarter, _startSessionRenameFromRenderedRow };
