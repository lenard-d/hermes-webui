import { _isReadOnlySession } from './sidebar-session-opening.js';
import { sidebarStateBindings } from './sidebar-store.js';

function _truncatedSessionId(sid){
  sid=String(sid||'').trim();
  if(!sid) return '';
  if(sid.length<=16) return sid;
  return sid.slice(0,12)+'...';
}

function _sessionTitleForForkParent(parentSid){
  if(!parentSid||!Array.isArray(sidebarStateBindings._allSessions)) return '';
  const parent=sidebarStateBindings._allSessions.find(item=>item&&item.session_id===parentSid);
  const title=parent&&String(parent.title||'').trim();
  if(!title||title==='Untitled') return '';
  return title;
}

function _sessionFullTitleTooltip(rawTitle, cleanTitle, session){
  const fallback=String(cleanTitle||'Untitled').trim()||'Untitled';
  const full=String(rawTitle||fallback).trim()||fallback;
  const title=full.startsWith('[SYSTEM:') ? fallback : full;
  if(typeof t==='function'&&_isReadOnlySession(session)) return t('session_readonly_title_hint', title);
  return title;
}

function _sessionForkTooltip(parentLabel){
  const parent=String(parentLabel||'').trim()||'unknown parent';
  const prefix=(typeof t==='function'?t('forked_from'):'Forked from');
  return `${prefix}: ${parent}`;
}

function _sessionLineageBadgeTooltip(label, canExpand){
  const base=String(label||'Prior turns').trim()||'Prior turns';
  if(typeof t==='function'){
    return canExpand
      ? t('session_lineage_toggle_hint', base)
      : t('session_lineage_static_hint', base);
  }
  return base;
}

function _sessionChildBadgeTooltip(label){
  const base=String(label||'Child sessions').trim()||'Child sessions';
  if(typeof t==='function') return t('session_child_toggle_hint', base);
  return base;
}

function _sessionStateTooltip({isStreaming=false,hasUnread=false}={}){
  if(isStreaming) return 'Conversation is running';
  if(hasUnread) return 'Unread completion';
  return '';
}

export {
  _sessionChildBadgeTooltip,
  _sessionForkTooltip,
  _sessionFullTitleTooltip,
  _sessionLineageBadgeTooltip,
  _sessionStateTooltip,
  _sessionTitleForForkParent,
  _truncatedSessionId,
};
