import { sessionRunRegistry } from './session-run-registry.js';
import { _isSessionEffectivelyStreaming, _rememberSessionListSource } from './session-run-state.js';
import { _forgetObservedStreamingSession } from './session-unread.js';
import { _isCliSession } from './session-source.js';
import { NO_PROJECT_FILTER, sidebarStateBindings } from './sidebar-store.js';
import { _activeSessionIdForSidebar } from './session-navigation.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';
import { _attachChildSessionsToSidebarRows } from './session-child-attachment.js';
import { _collapseSessionLineageForSidebar, _isChildSession } from './session-lineage.js';

const _sessionStreamingById=sessionRunRegistry.streamingById;

function upsertActiveSessionForLocalTurn({title='', messageCount=0, timestampMs=Date.now()}={}){
  if(!S.session||!S.session.session_id) return;
  const sid=S.session.session_id;
  const nowSec=Math.floor((Number(timestampMs)||Date.now())/1000);
  const localCount=Array.isArray(S.messages)?S.messages.length:0;
  const count=Math.max(Number(S.session.message_count||0),Number(messageCount||0),localCount,1);
  S.session.message_count=count;
  S.session.last_message_at=nowSec;
  S.session.updated_at=nowSec;
  if((S.session.title==='Untitled'||!S.session.title)&&title){
    S.session.title=title;
  }
  const existingIdx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===sid);
  const row={
    ...S.session,
    session_id:sid,
    title:S.session.title||title||'New chat',
    message_count:count,
    last_message_at:nowSec,
    updated_at:nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:true,
  };
  if(existingIdx>=0) sidebarStateBindings._allSessions[existingIdx]={...sidebarStateBindings._allSessions[existingIdx],...row};
  else sidebarStateBindings._allSessions.unshift(row);
  renderSessionListFromCache();
}

function _sessionRowsWithActiveEphemeralSession(rows){
  rows=Array.isArray(rows)?rows:[];
  if(!S.session||!S.session.session_id) return rows;
  const sid=S.session.session_id;
  if(rows.some(s=>s&&s.session_id===sid)) return rows;
  const nowSec=Math.floor(Date.now()/1000);
  const activeRow={
    ...S.session,
    session_id:sid,
    title:S.session.title||'New Chat',
    display_title:S.session.display_title||S.session.title||'New Chat',
    message_count:0,
    last_message_at:S.session.last_message_at||S.session.updated_at||nowSec,
    updated_at:S.session.updated_at||S.session.last_message_at||nowSec,
    profile:S.session.profile||S.activeProfile||'default',
    is_streaming:false,
  };
  return [activeRow,...rows];
}

function _ensureActiveSessionRowPresent(rows, sourceRows){
  rows=Array.isArray(rows)?rows:[];
  const activeSid=_activeSessionIdForSidebar();
  if(!activeSid||rows.some(s=>s&&s.session_id===activeSid)) return rows;
  const activeRow=(Array.isArray(sourceRows)?sourceRows:[]).find(s=>s&&s.session_id===activeSid);
  // Only re-inject the active FRESHLY-CREATED 0-message ephemeral chat. An active
  // conversation that already has messages and was filtered out by the search
  // query must stay filtered — re-adding it here would pollute unrelated search
  // results with the current chat (#3408 review, Codex).
  if(activeRow && Number(activeRow.message_count||0)<=0){
    return [activeRow,...rows];
  }
  return rows;
}

function clearOptimisticSessionStreaming(sid){
  sid=sid||(S.session&&S.session.session_id)||'';
  if(!sid) return;
  if(typeof _rememberSessionListSource==='function') _rememberSessionListSource(null, sid, false);
  if(S.session&&S.session.session_id===sid){
    S.session.active_stream_id=null;
    S.activeStreamId=null;
  }
  if(Array.isArray(sidebarStateBindings._allSessions)){
    const idx=sidebarStateBindings._allSessions.findIndex(s=>s&&s.session_id===sid);
    if(idx>=0){
      sidebarStateBindings._allSessions[idx]={
        ...sidebarStateBindings._allSessions[idx],
        active_stream_id:null,
        pending_user_message:null,
        pending_started_at:null,
        is_streaming:false,
      };
    }
  }
  if(typeof _sessionStreamingById!=='undefined'&&_sessionStreamingById&&typeof _sessionStreamingById.set==='function'){
    _sessionStreamingById.set(sid,false);
  }
  if(typeof _forgetObservedStreamingSession==='function') _forgetObservedStreamingSession(sid);
  renderSessionListFromCache();
}


// Top-level so BOTH the sidebar visibility predicate (_sidebarRowHasVisibleMessages,
// reached via renderSessionListFromCache -> _partitionSidebarSessionRows) and the
// per-row renderer (_renderOneSession, nested in renderSessionListFromCache) can call
// it. It was previously declared INSIDE renderSessionListFromCache and relied on
// function hoisting — but hoisting is scoped to the enclosing function, so the
// top-level _sidebarRowHasVisibleMessages threw "ReferenceError: _sessionAttentionState
// is not defined" on every cache render, crashing the sidebar (#3696, regressed in
// #3672 when _sidebarRowHasVisibleMessages was extracted to top level). Pure function
// (only its arg `s` plus the i18n global `t`), so hoisting it is safe.
function _sessionAttentionState(s){
  const attention=s&&s.attention&&typeof s.attention==='object'?s.attention:null;
  if(!attention||!attention.kind||!Number.isFinite(Number(attention.count))||Number(attention.count)<=0)return null;
  const kind=String(attention.kind)==='approval'?'approval':(String(attention.kind)==='clarify'?'clarify':'attention');
  const count=Math.max(1,Number(attention.count)||1);
  const labelKey=kind==='approval'?'session_attention_approval':(kind==='clarify'?'session_attention_clarify':'session_attention_generic');
  const titleKey=kind==='approval'?'session_attention_approval_title':(kind==='clarify'?'session_attention_clarify_title':'session_attention_generic_title');
  const fallback=kind==='approval'?(count===1?'Approval':`${count} approvals`):(kind==='clarify'?(count===1?'Question':`${count} questions`):(count===1?'Attention':`${count} items`));
  const titleFallback=kind==='approval'?'Waiting for permission decision':(kind==='clarify'?'Waiting for your answer':'Waiting for user action');
  const label=(typeof t==='function')?t(labelKey,count):fallback;
  const title=(typeof t==='function')?t(titleKey,count):titleFallback;
  return {kind,count,severity:String(attention.severity||''),label,title};
}

function _sidebarRowHasVisibleMessages(s, activeSidForSidebar){
  return (s.message_count||0)>0 ||
    _sessionAttentionState(s) ||
    _isSessionEffectivelyStreaming(s) ||
    !!s.active_stream_id ||
    !!s.pending_user_message ||
    !!s.has_pending_user_message ||
    (activeSidForSidebar&&s.session_id===activeSidForSidebar) ||
    // #5306: a linked delegate child of the currently-active/streaming parent
    // must stay rendered for the duration of the parent's turn. A subagent child
    // that transiently reports message_count===0 between /api/sessions polls would
    // otherwise be dropped HERE (before _attachChildSessionsToSidebarRows ever sees
    // it), so it never reaches sessionsRaw, vanishes from the sidebar, then
    // reappears on the next refresh once its list metadata catches up — the flicker.
    // Scoped to children of the ACTIVE parent, mirroring the active-session
    // exception above, so unrelated truly-empty sessions are still hidden.
    (activeSidForSidebar&&s.parent_session_id===activeSidForSidebar&&_isChildSession(s)) ||
    (S.session&&s.session_id===S.session.session_id&&(S.session.message_count||0)>0);
}

function _partitionSidebarSessionRows(allMatched, activeSidForSidebar){
  let cliSessionCount=0;
  const webuiProfileFiltered=[];
  const cliProfileFiltered=[];
  const webuiReferenceRaw=[];
  const cliReferenceRaw=[];
  const webuiSessionsRaw=[];
  const cliSessionsRaw=[];
  let webuiArchivedCount=0;
  let cliArchivedCount=0;
  for(const s of allMatched){
    if(!_sidebarRowHasVisibleMessages(s, activeSidForSidebar)) continue;
    const isCli=_isCliSession(s);
    if(isCli) cliSessionCount++;
    if(s.default_hidden&&!(sidebarStateBindings._activeProject&&sidebarStateBindings._activeProject!==NO_PROJECT_FILTER&&s.project_id===sidebarStateBindings._activeProject)) continue;
    const profileFiltered=isCli ? cliProfileFiltered : webuiProfileFiltered;
    const referenceRaw=isCli ? cliReferenceRaw : webuiReferenceRaw;
    const sessionsRaw=isCli ? cliSessionsRaw : webuiSessionsRaw;
    profileFiltered.push(s);
    if(sidebarStateBindings._activeProject===NO_PROJECT_FILTER){
      if(s.project_id) continue;
    } else if(sidebarStateBindings._activeProject){
      if(s.project_id!==sidebarStateBindings._activeProject) continue;
    }
    referenceRaw.push(s);
    if(s.archived){
      if(isCli) cliArchivedCount++;
      else webuiArchivedCount++;
    }
    if(!sidebarStateBindings._showArchived&&s.archived) continue;
    sessionsRaw.push(s);
  }
  if(sidebarStateBindings._sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0){
    sidebarStateBindings._sessionSourceFilter='webui';
  }
  const showCliOnly=sidebarStateBindings._sessionSourceFilter==='cli';
  const serverArchivedCount=showCliOnly?sidebarStateBindings._archivedCliCount:sidebarStateBindings._archivedWebuiCount;
  return {
    cliSessionCount,
    profileFiltered: showCliOnly ? cliProfileFiltered : webuiProfileFiltered,
    sessionsRaw: showCliOnly ? cliSessionsRaw : webuiSessionsRaw,
    archivedCount: Math.max(showCliOnly ? cliArchivedCount : webuiArchivedCount, Number(serverArchivedCount||0)),
    webuiReferenceRaw,
    cliReferenceRaw,
    webuiSessionsRaw,
    cliSessionsRaw,
  };
}

// Hidden archived-ancestor reference rows (sidebar_reference_sessions) arrive
// from /api/sessions WITHOUT the client-side project/source scoping that
// _partitionSidebarSessionRows applies to the visible rows. Appending them to
// EVERY render unconditionally let an archived parent from a DIFFERENT project
// (or the other source bucket) enter a project/source-filtered render's
// suppression context — silently hiding a visible child/fork whose archived
// ancestor lives outside the current view. Scope the references to the same
// project + source bucket as the render they feed before using them.
function _scopedSidebarReferenceRows(isCli){
  if(typeof sidebarStateBindings._sidebarReferenceSessions==='undefined'||!Array.isArray(sidebarStateBindings._sidebarReferenceSessions)||!sidebarStateBindings._sidebarReferenceSessions.length) return [];
  return sidebarStateBindings._sidebarReferenceSessions.filter(s=>{
    if(!s) return false;
    // Source scope: only references in the same webui/cli bucket as this render.
    if(_isCliSession(s)!==!!isCli) return false;
    // Project scope: mirror _partitionSidebarSessionRows exactly.
    if(sidebarStateBindings._activeProject===NO_PROJECT_FILTER){ if(s.project_id) return false; }
    else if(sidebarStateBindings._activeProject){ if(s.project_id!==sidebarStateBindings._activeProject) return false; }
    return true;
  });
}

function _renderSidebarRowsFromRawSessions(sessionsRaw, referenceSessionsRaw){
  const referenceRows=Array.isArray(referenceSessionsRaw)?referenceSessionsRaw:sessionsRaw;
  return _attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw, referenceRows);
}

export const sidebarRowBehavior=Object.freeze({
  upsertActive:upsertActiveSessionForLocalTurn,
  clearOptimistic:clearOptimisticSessionStreaming,
  withActiveEphemeral:_sessionRowsWithActiveEphemeralSession,
  ensureActive:_ensureActiveSessionRowPresent,
  partition:_partitionSidebarSessionRows,
  projectRows:_renderSidebarRowsFromRawSessions,
});

export {
  _ensureActiveSessionRowPresent,
  _partitionSidebarSessionRows,
  _renderSidebarRowsFromRawSessions,
  _scopedSidebarReferenceRows,
  _sessionAttentionState,
  _sessionRowsWithActiveEphemeralSession,
  clearOptimisticSessionStreaming,
  upsertActiveSessionForLocalTurn,
};
