import { sessionLoadState } from './session-load-state.js';
import { _profileMatchesActiveProfile } from './session-profile-scope.js';
import { _getChannelLabel, _isCliSession, _isExternalSession, _isMessagingSession, _sourceKeyForSession } from './session-source.js';
import { _clearSessionCompletionUnread, _clearSessionViewedCount, _forgetObservedStreamingSession, _isSessionActivelyViewedForList, _setSessionViewedCount } from './session-unread.js';
import { loadSession } from './lifecycle.js';
import { messageTimelineBindings } from './message-timeline.js';
import { NO_PROJECT_FILTER, SESSION_ARCHIVED_MAX_LOADED_LIMIT, SESSION_ARCHIVED_PAGE_SIZE, _selectedSessions, sidebarStateBindings } from './sidebar-store.js';
import { renderSessionList } from './session-list-render-port.js';
import { renderSessionListFromCache } from './sidebar-render-port.js';

const _HANDOFF_THRESHOLD = 10;  // conversation rounds
const _HANDOFF_STORAGE_PREFIX = 'handoff:';
const _HANDOFF_SUFFIX_DISMISSED_AT = 'dismissed_at';
const _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT = 'summary_handled_at';
function _externalImportPayload(session) {
  const payload = {session_id: session.session_id};
  if (sidebarStateBindings._showAllProfiles && session && typeof session.profile === 'string' && session.profile) {
    payload.all_profiles = true;
    payload.profile = session.profile;
  }
  return payload;
}

function _sidebarSessionProfileName(session){
  const raw=session&&typeof session.profile==='string'?session.profile.trim():'';
  return raw||'';
}

async function _ensureSidebarSessionProfile(session){
  const targetProfile=_sidebarSessionProfileName(session);
  if(!sidebarStateBindings._showAllProfiles||!targetProfile) return false;
  const activeProfile=S.activeProfile||'default';
  if(_profileMatchesActiveProfile(targetProfile,activeProfile)) return false;
  if(typeof switchToProfile!=='function') return false;
  sidebarStateBindings._profileSwitchOpeningExistingSession=true;
  try{
    await switchToProfile(targetProfile);
  }finally{
    sidebarStateBindings._profileSwitchOpeningExistingSession=false;
  }
  return _profileMatchesActiveProfile(targetProfile,S.activeProfile||'default');
}

async function _openSidebarSession(session, loadOpts={}){
  if(!session||!session.session_id) return;
  // Extension pre-open hook — before any side-effects (external import, profile switching).
  // Handler returns {cancel:true} to prevent the open.
  if(!loadOpts.skipExtHooks && typeof _hermesNotifySessionOpen==='function'){
    var _preResult=_hermesNotifySessionOpen(session.session_id, null, {preload:true, opts:loadOpts});
    if(_preResult&&_preResult.cancel===true) return;
  }
  // #5409: close mobile sidebar AFTER veto guard passes — only close if open proceeds.
  if(typeof closeMobileSidebar==='function')closeMobileSidebar();
  if(_isExternalSession(session)){
    try{await api('/api/session/import_cli',{method:'POST',body:JSON.stringify(_externalImportPayload(session))});}
    catch(_e){ /* import failed -- fall through to read-only view */ }
  }
  await _ensureSidebarSessionProfile(session);
  // Tell loadSession to skip its pre-hook — we already ran it above.
  await loadSession(session.session_id, Object.assign({}, loadOpts, {_preloadNotified:true}));
  renderSessionListFromCache();
}

function _isReadOnlySession(session) {
  return !!(session && (session.read_only || session.is_read_only));
}

function _isBranchableReadOnlySession(session) {
  if (!_isReadOnlySession(session)) return false;
  const sources = [
    session && session.source_tag,
    session && session.raw_source,
    session && session.source,
  ].map(v => String(v || '').trim().toLowerCase());
  return sources.includes('cron');
}

function _sessionSourceLabel(filter, count) {
  const n = Number(count) || 0;
  return filter === 'cli' ? `CLI sessions (${n})` : `WebUI sessions (${n})`;
}

function _clearSessionSourceTabCounts() {
  sidebarStateBindings._serverWebuiSessionCount = null;
  sidebarStateBindings._serverCliSessionCount = null;
}

function _requestedSessionSidebarSource() {
  return window._showCliSessions ? sidebarStateBindings._sessionSourceFilter : 'webui';
}

function _sessionListExcludeHiddenEnabled() {
  return sidebarStateBindings._activeProject===null || sidebarStateBindings._activeProject===NO_PROJECT_FILTER;
}

function _sessionArchivePagingFilterActive() {
  let searchActive=false;
  try{
    const searchEl=typeof $==='function' ? $('sessionSearch') : null;
    searchActive=Boolean(searchEl&&String(searchEl.value||'').trim());
  }catch(_e){ searchActive=false; }
  return Boolean(searchActive||sidebarStateBindings._activeProject);
}

function _sessionListQueryString() {
  const qs = new URLSearchParams();
  qs.set('sidebar_source', _requestedSessionSidebarSource());
  if(_sessionListExcludeHiddenEnabled()) qs.set('exclude_hidden','1');
  if(sidebarStateBindings._showAllProfiles) qs.set('all_profiles','1');
  if(sidebarStateBindings._showArchived){
    qs.set('include_archived','1');
    if(!_sessionArchivePagingFilterActive()){
      const archiveLimit=Math.min(
        SESSION_ARCHIVED_MAX_LOADED_LIMIT,
        Math.max(SESSION_ARCHIVED_PAGE_SIZE, Number(sidebarStateBindings._archivedRowsLoadedLimit)||SESSION_ARCHIVED_PAGE_SIZE)
      );
      qs.set('archived_limit', String(archiveLimit));
    }
  }
  return `?${qs.toString()}`;
}

function _sessionSourceTabCount(filter, renderedWebuiSessionCount, renderedCliSessionCount) {
  const serverCount = filter === 'cli' ? sidebarStateBindings._serverCliSessionCount : sidebarStateBindings._serverWebuiSessionCount;
  if (Number.isFinite(serverCount)) return serverCount;
  return filter === 'cli' ? renderedCliSessionCount : renderedWebuiSessionCount;
}

function _setActiveProjectFilter(projectId) {
  const next = projectId === NO_PROJECT_FILTER ? NO_PROJECT_FILTER : (projectId || null);
  if (sidebarStateBindings._activeProject === next) return;
  sidebarStateBindings._activeProject = next;
  renderSessionListFromCache();
  void renderSessionList({deferWhileInteracting:false});
}

function _setSessionSourceFilter(filter) {
  const next = filter === 'cli' ? 'cli' : 'webui';
  if (sidebarStateBindings._sessionSourceFilter === next) return;
  sidebarStateBindings._sessionSourceFilter = next;
  sidebarStateBindings._activeProject = null;
  _selectedSessions.clear();
  sidebarStateBindings._sessionSelectMode = false;
  try { localStorage.setItem('hermes-session-source-filter', next); } catch (_e) {}
  renderSessionListFromCache();
  void renderSessionList({deferWhileInteracting:false});
}

function _restoreSessionSourceFilter() {
  try {
    const raw = localStorage.getItem('hermes-session-source-filter');
    if (raw === 'cli' || raw === 'webui') sidebarStateBindings._sessionSourceFilter = raw;
  } catch (_e) {}
}

function _normalizeMessageForCliImportComparison(message) {
  if (!message || typeof message !== 'object') return message;
  const clone = { ...message };
  delete clone.timestamp;
  delete clone._ts;
  return clone;
}

function _isCliImportRefreshPrefixMatch(localMessages, freshMessages) {
  if (!Array.isArray(localMessages) || !Array.isArray(freshMessages)) return false;
  if (localMessages.length > freshMessages.length) return false;
  for (let i = 0; i < localMessages.length; i += 1) {
    if (JSON.stringify(_normalizeMessageForCliImportComparison(localMessages[i])) !== JSON.stringify(_normalizeMessageForCliImportComparison(freshMessages[i]))) {
      return false;
    }
  }
  return true;
}

function _handoffStorageKey(sid) {
  return `${_HANDOFF_STORAGE_PREFIX}${sid}:`;
}

function _getHandoffStorageValue(sid, suffix) {
  try {
    const raw = localStorage.getItem(_handoffStorageKey(sid) + suffix);
    return raw ? parseFloat(raw) : null;
  } catch { return null; }
}

function _setHandoffStorageValue(sid, suffix, ts) {
  const key = _handoffStorageKey(sid) + suffix;
  try {
    if (!Number.isFinite(ts)) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, String(ts));
  } catch {}
}

function _clearHandoffStorageForSession(sid) {
  if (!sid) return;
  try {
    _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT, null);
    _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT, null);
  } catch {}
  // Session deletion should also prune per-session tracking maps. Otherwise
  // heavy users accumulate one localStorage entry per deleted session forever,
  // which increases quota pressure and can make future UI persistence fail.
  try { _clearSessionViewedCount(sid); } catch {}
  try { _clearSessionCompletionUnread(sid); } catch {}
  try { _forgetObservedStreamingSession(sid); } catch {}
}

function _getHandoffDismissedAt(sid) {
  return _getHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT);
}

function _setHandoffDismissedAt(sid, ts) {
  _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_DISMISSED_AT, ts);
}

function _getHandoffSummaryHandledAt(sid) {
  return _getHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT);
}

function _setHandoffSummaryHandledAt(sid, ts) {
  _setHandoffStorageValue(sid, _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT, ts);
}

function _getHandoffSince(sid) {
  const dismissedAt = _getHandoffDismissedAt(sid);
  const summaryHandledAt = _getHandoffSummaryHandledAt(sid);
  if (Number.isFinite(dismissedAt) && Number.isFinite(summaryHandledAt)) return Math.max(dismissedAt, summaryHandledAt);
  if (Number.isFinite(dismissedAt)) return dismissedAt;
  if (Number.isFinite(summaryHandledAt)) return summaryHandledAt;
  return null;
}

function _handoffMessagesEl() {
  return document.getElementById('messages');
}

function _handoffIsMessagesNearBottom(el) {
  if (!el) return false;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 150;
}

function _syncHandoffDockSpace(open) {
  const messages = _handoffMessagesEl();
  if (!messages) return;
  const wasNearBottom = _handoffIsMessagesNearBottom(messages);
  if (!open) {
    messages.classList.remove('handoff-dock-visible');
    messages.style.removeProperty('--handoff-dock-height');
    if (wasNearBottom && typeof scrollToBottom === 'function') requestAnimationFrame(scrollToBottom);
    return;
  }
  messages.classList.add('handoff-dock-visible');
  const measure = () => {
    const container = $('handoffHintContainer');
    const h = container && container.getBoundingClientRect().height;
    if (h > 0) messages.style.setProperty('--handoff-dock-height', Math.ceil(h + 24) + 'px');
    if (wasNearBottom && typeof scrollToBottom === 'function') scrollToBottom();
  };
  requestAnimationFrame(measure);
  setTimeout(measure, 360);
}

async function _checkAndShowHandoffHint(sid) {
  try {
    const since = _getHandoffSince(sid);
    const body = { session_id: sid };
    if (since != null) body.since = since;

    const result = await api('/api/session/conversation-rounds', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    // Stale? Session switched while we were fetching.
    if (!S.session || S.session.session_id !== sid) return;

    if (result && result.ok && result.should_show) {
      _showHandoffHint(sid, result.rounds);
    } else {
      const container = $('handoffHintContainer');
      const isSameVisibleSession = !!(
        container &&
        container.classList.contains('is-visible') &&
        container.dataset.sessionId === String(sid)
      );
      if (!isSameVisibleSession) _hideHandoffHint();
    }
  } catch (e) {
    console.warn('Handoff hint check failed:', e);
    _hideHandoffHint();
  }
}

function _showHandoffHint(sid, rounds) {
  const container = $('handoffHintContainer');
  if (!container) return;

  // Clear any existing content.
  container.innerHTML = '';
  container.style.display = '';
  container.classList.add('is-visible');
  container.dataset.sessionId = String(sid);

  const channel = _getChannelLabel(S.session);
  const hintText = channel
    ? `${channel} handoff`
    : `Conversation handoff`;
  const hintMeta = `${rounds} new conversation rounds`;

  const bar = document.createElement('div');
  bar.className = 'handoff-hint-bar';
  bar.id = 'handoffHintBar';
  bar.innerHTML = `
    <div class="handoff-hint-text">
      <span class="handoff-hint-dot" aria-hidden="true"></span>
      <span class="handoff-hint-label">${esc(hintText)}</span>
      <span class="handoff-hint-meta">${esc(hintMeta)}</span>
    </div>
    <div class="handoff-hint-actions">
      <button class="handoff-hint-action" type="button">View summary</button>
      <button class="handoff-hint-dismiss" type="button" onclick="event.stopPropagation(); _dismissHandoffHint('${esc(sid)}')" title="Dismiss">
        Close
      </button>
    </div>
  `;

  // Click on the bar (not the explicit close button) triggers summary generation.
  bar.addEventListener('click', (e) => {
    if (e.target.closest('.handoff-hint-dismiss')) return;
    _generateHandoffSummary(sid, rounds);
  });

  container.appendChild(bar);
  _syncHandoffDockSpace(true);
}

function _hideHandoffHint() {
  const container = $('handoffHintContainer');
  if (container) {
    container.innerHTML = '';
    container.style.display = 'none';
    container.classList.remove('is-visible');
    delete container.dataset.sessionId;
  }
  _syncHandoffDockSpace(false);
}

function _dismissHandoffHint(sid) {
  _setHandoffDismissedAt(sid, Date.now() / 1000);
  _hideHandoffHint();
}

function _buildHandoffSummaryToolMessage(summary, channel, rounds, fallback) {
  const generatedAt = Date.now() / 1000;
  return {
    role: 'tool',
    tool_call_id: '',
    name: 'handoff_summary',
    timestamp: generatedAt,
    _ts: generatedAt,
    content: JSON.stringify({
      _handoff_summary_card: true,
      session_id: sidValue(),
      summary: String(summary || '').trim(),
      channel: (typeof channel === 'string' && channel.trim()) ? channel.trim() : null,
      rounds: Number.isFinite(rounds) ? rounds : null,
      fallback: !!fallback,
      generated_at: generatedAt,
    }),
  };
}

function sidValue() {
  return S && S.session && S.session.session_id ? S.session.session_id : null;
}

function _extractHandoffSummaryPayload(content){
  if(!content) return null;
  if(typeof content!=='string') return null;
  try {
    const parsed=JSON.parse(content);
    return parsed&&typeof parsed==='object'&&parsed._handoff_summary_card===true?parsed:null;
  } catch (e) {
    return null;
  }
}

async function _generateHandoffSummary(sid, rounds) {
  // Treat handoff like a slash-command result: the composer dock entry
  // disappears and the transient summary card renders in the transcript.
  _hideHandoffHint();
  const channel = _getChannelLabel(S.session);
  if (typeof setHandoffUi === 'function') {
    setHandoffUi({
      sessionId: sid,
      phase: 'running',
      channel,
      rounds,
    });
  }

  try {
    const since = _getHandoffSince(sid);
    const body = { session_id: sid };
    if (since != null) body.since = since;

    const result = await api('/api/session/handoff-summary', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const isSuccess = result && result.ok && result.summary;
    if (isSuccess) {
      _setHandoffSummaryHandledAt(sid, Date.now() / 1000);
      _setHandoffDismissedAt(sid, null);
      const marker=_buildHandoffSummaryToolMessage(result.summary, channel, result.rounds || rounds, !!result.fallback);
      if (S.session && S.session.session_id === sid) {
        S.messages = [...S.messages, marker];
        if (typeof renderMessages === 'function') renderMessages();
      }
      if (typeof setHandoffUi === 'function') {
        setHandoffUi(null);
      }
    } else if (S.session && S.session.session_id === sid && typeof setHandoffUi === 'function') {
      // Keep transient card while the user can retry the action.
      setHandoffUi({
        sessionId: sid,
        phase: 'error',
        channel,
        rounds,
        errorText: 'Could not generate summary. Please try again.',
      });
    } else {
      // Stale session response path: only record success baseline.
    }
  } catch (e) {
    console.warn('Handoff summary failed:', e);
    if (S.session && S.session.session_id === sid && typeof setHandoffUi === 'function') {
      setHandoffUi({
        sessionId: sid,
        phase: 'error',
        channel,
        rounds,
        errorText: 'Summary generation failed: ' + e.message,
      });
    }
  }

  // If generation succeeds, set a baseline so only new activity after that time
  // can re-trigger handoff prompts. Failures keep the hint active so users can
  // retry.
}

function _afterSessionFirstPaint(fn, delayMs=0){
  return new Promise((resolve)=>{
    const invoke=()=>{
      try{ resolve(typeof fn==='function' ? fn() : undefined); }
      catch(_){ resolve(undefined); }
    };
    const run=()=>{
      if(typeof requestIdleCallback==='function'){
        requestIdleCallback(invoke,{timeout:1500});
      }else{
        setTimeout(invoke, delayMs);
      }
    };
    if(typeof requestAnimationFrame==='function'){
      requestAnimationFrame(()=>requestAnimationFrame(run));
    }else{
      setTimeout(run, delayMs);
    }
  });
}

function _deferSessionSideEffect(sid, fn, delayMs=0){
  if(!sid||typeof fn!=='function') return Promise.resolve();
  return _afterSessionFirstPaint(()=>{
    if(!S.session||S.session.session_id!==sid) return undefined;
    return fn();
  },delayMs);
}

function _deferWorkspaceRefreshForSession(sid, opts={}){
  _deferSessionSideEffect(sid,()=>{
    const load=loadDir('.', opts);
    if(load&&typeof load.catch==='function') load.catch(()=>{});
  },150);
}

function _resolveSessionModelForDisplaySoon(sid){
  if(!sid) return;
  _deferSessionSideEffect(sid,async()=>{
    try{
      const data=await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=1`);
      const model=data&&data.session&&data.session.model;
      const provider=data&&data.session&&data.session.model_provider;
      if(!model||!S.session||S.session.session_id!==sid) return;
      S.session.model=model;
      S.session.model_provider=provider||null;
      const resolvedContextLength=data.session.context_length||S.session.context_length||0;
      S.session.context_length=resolvedContextLength;
      S.session.threshold_tokens=data.session.threshold_tokens||0;
      S.session.last_prompt_tokens=data.session.last_prompt_tokens||0;
      S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;
      S.session._modelResolutionDeferred=false;
      syncTopbar();
      if(typeof _syncCtxIndicator==='function'){
        const u=S.lastUsage||{};
        const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
        _syncCtxIndicator({
          input_tokens:_pick(u.input_tokens,S.session.input_tokens),
          output_tokens:_pick(u.output_tokens,S.session.output_tokens),
          estimated_cost:_pick(u.estimated_cost,S.session.estimated_cost),
          cache_read_tokens:_pick(u.cache_read_tokens,S.session.cache_read_tokens),
          cache_write_tokens:_pick(u.cache_write_tokens,S.session.cache_write_tokens),
          cache_hit_percent:_pick(u.cache_hit_percent,S.session.cache_hit_percent,null),
          context_length:resolvedContextLength||u.context_length||0,
          last_prompt_tokens:_pick(u.last_prompt_tokens,S.session.last_prompt_tokens),
          post_compression_context_tokens_estimate:S.session.post_compression_context_tokens_estimate,
          threshold_tokens:data.session.threshold_tokens||0,
        });
      }
    }catch(_){
      // Keep session switching non-blocking; the next load can try again.
    }
  },0);
}

// Tracks whether the current session has older messages that were not
// loaded during the initial paginated fetch (msg_limit window).
// When true, scrolling to the top triggers _loadOlderMessages().
let _messagesTruncated = false;

// Load session messages if not already present.
// Called after loadSession fetches metadata (messages=0).
// Idempotent: if messages are already in S.messages, resolves immediately.
// Handles streaming sessions specially: restores from INFLIGHT cache or API.
// msg_limit (default 30): fetch a tail window with roughly N visible
// user/assistant rows for fast switching. Tool rows inside the window are
// server-bounded and do not consume the visible-message budget.
// Older messages are loaded on-demand via _loadOlderMessages().
const _INITIAL_MSG_LIMIT = 30;
// ============================================================================
// COUPLED CONSTANT — keep in sync with api/routes.py:_MAX_MSG_LIMIT.
// ============================================================================
// This is a hand-mirrored copy of the backend's GET /api/session ?msg_limit=
// ceiling. _loadOlderMessages grows its msg_limit tail window by
// +_INITIAL_MSG_LIMIT each load; once growth would exceed this ceiling the
// server clamps and the tail stops growing, so we switch to msg_before paging
// (a fixed-size backward page keyed off _oldestIdx) instead. This const is the
// static FALLBACK default only — the live ceiling is read from the /api/session
// `_msg_limit_max` metadata into _msgLimitMax below (#6177), so the two can no
// longer drift. Keep this fallback value roughly in sync with the backend
// _MAX_MSG_LIMIT for the mixed-version case where the server omits the field.
const _MSG_LIMIT_MAX = 500;
// Live server-advertised msg_limit ceiling. Declared at module scope with the
// static fallback so the reload-width paths (_ensureMessagesLoaded /
// _loadOlderMessages) always read a defined value even before the first
// /api/session response lands; refreshed from `_msg_limit_max` on each load.
let _msgLimitMax = _MSG_LIMIT_MAX;
let _sameSessionForceReloadHint = null;

function _currentLoadedRenderableMessageCount(){
  if(typeof _messageRenderableMessageCount==='function'){
    try{return Math.max(0,Number(_messageRenderableMessageCount())||0);}
    catch(_){}
  }
  let count=0;
  for(const m of (S.messages||[])){
    if(m&&m.role&&m.role!=='tool') count++;
  }
  return count;
}

function _captureSameSessionForceReloadHint(sid){
  const loadedRenderableCount=_currentLoadedRenderableMessageCount();
  const loadedMessageCount=Array.isArray(S.messages)?S.messages.length:0;
  const knownMessageCount=Number(S.session&&S.session.session_id===sid&&S.session.message_count)||loadedMessageCount;
  if(!sid || (loadedRenderableCount<=0 && loadedMessageCount<=0)){
    _sameSessionForceReloadHint=null;
    return;
  }
  _sameSessionForceReloadHint={
    session_id:sid,
    loaded_renderable_count:loadedRenderableCount,
    loaded_message_count:loadedMessageCount,
    message_count:knownMessageCount,
    truncated:!!_messagesTruncated,
  };
}

function _clearSameSessionForceReloadHint(sid){
  if(!_sameSessionForceReloadHint) return;
  if(!sid || _sameSessionForceReloadHint.session_id===sid) _sameSessionForceReloadHint=null;
}

function _messageReloadLimitForSession(sid){
  const hint=_sameSessionForceReloadHint;
  if(hint&&hint.session_id===sid){
    const loadedRenderableCount=Math.max(0,Number(hint.loaded_renderable_count)||0);
    const loadedMessageCount=Math.max(0,Number(hint.loaded_message_count)||0);
    if(loadedRenderableCount>0 || loadedMessageCount>0){
      if(!hint.truncated) return null;
      const previousMessageCount=Math.max(0,Number(hint.message_count)||0);
      const currentMessageCount=Math.max(0,Number(S.session&&S.session.session_id===sid&&S.session.message_count)||0);
      const appendedMessageCount=Math.max(0,currentMessageCount-previousMessageCount);
      return Math.max(_INITIAL_MSG_LIMIT,loadedRenderableCount,loadedMessageCount+appendedMessageCount);
    }
  }
  return _INITIAL_MSG_LIMIT;
}

function _syncToolCallsForLoadedMessages(messages, sessionToolCalls){
  const msgs=Array.isArray(messages)?messages:[];
  // During active streaming, skip — clearing S.toolCalls would lose Activity
  // and the renderMessages fallback is blocked by S.busy=true.
  if(S.busy||S.activeStreamId) return;
  // Persist the loaded compact tool summary onto S.session so the renderMessages
  // derived rebuild can use it as a durable per-tid snippet fallback on cold
  // load (#4927). loadSession keeps the messages=0 session object (tool_calls
  // []), and the messages=1 summary arrives only as this argument — without
  // copying it across, the fallback source is empty exactly on the cold-load
  // path it's meant to repair.
  if(S.session&&Array.isArray(sessionToolCalls)) S.session.tool_calls=sessionToolCalls.map(tc=>({...tc}));
  const hasMessageToolMetadata=msgs.some(m=>{
    if(!m) return false;
    const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;
    // `_partial_tool_calls` are emitted by interrupted/partial turns and must also
    // anchor rendering to the owning assistant message, so we can reconstruct
    // settled tool cards from the message history when available.
    const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;
    const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');
    return hasTc||hasPartialTc||hasTu;
  });
  if(!hasMessageToolMetadata&&Array.isArray(sessionToolCalls)&&sessionToolCalls.length){
    S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));
  }else{
    S.toolCalls=[];
  }
}

async function _ensureMessagesLoaded(sid, opts) {
  // `opts` is an explicit named parameter (vs loadSession's arguments[1]
  // pattern) because _ensureMessagesLoaded is a module-private helper: it is
  // only called from inside loadSession, so the public signature does not need
  // to be preserved. Strict-mode engines optimize named params more reliably
  // than arguments-indexing, and a named opts is self-documenting for static
  // analysis. Callers pass {force:true} when they need to BYPASS the
  // "messages already populated" early-return — currently only the #5177
  // keep-stale-until-loaded path, which intentionally leaves the old messages
  // in place (to avoid a visible disappear/reappear gap) and relies on
  // _ensureMessagesLoaded to fetch and SWAP the new transcript into
  // S.messages in a single frame.
  opts = opts || {};
  const _loadGeneration = Number.isFinite(opts.loadGeneration) ? Number(opts.loadGeneration) : null;
  const _ownsLoad = () => sessionLoadState.loadingSessionId === sid && (_loadGeneration === null || sessionLoadState.generation === _loadGeneration);
  if (!_ownsLoad()) return;
  // Already have messages? (e.g. from INFLIGHT restore path, already set)
  if (!opts.force && S.messages && S.messages.length > 0 && S.messages[0] && S.messages[0].role) {
    _clearSameSessionForceReloadHint(sid);
    return;
  }
  // Fetch session messages with a tail window for fast initial load.
  const reloadLimit = _messageReloadLimitForSession(sid); // defaults to _INITIAL_MSG_LIMIT
  // A reload window above the server's msg_limit ceiling would be clamped by
  // the backend (returning only the last _MSG_LIMIT_MAX rows), which can
  // silently SHRINK an already-loaded transcript that had more than the ceiling
  // of rows visible (rows 400–999 replaced by 500–999). When the requested
  // window exceeds the ceiling, fall back to the bare full-transcript request
  // (no msg_limit / no expand_renderable) so a same-session refresh never drops
  // already-loaded older rows (Codex gate #6154, silent row-loss).
  const boundedReloadLimit = (reloadLimit && reloadLimit <= _msgLimitMax) ? reloadLimit : null;
  const reloadLimitParam = boundedReloadLimit ? `&msg_limit=${boundedReloadLimit}` : '';
  // Older frontends used expand_renderable=1 to request visible-row expansion.
  // The server now counts msg_limit by visible transcript rows by default; keep
  // the flag for compatibility with mixed-version deployments.
  const expandParam = boundedReloadLimit ? '&expand_renderable=1' : '';
  let data;
  try {
    data = await api(
      `/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0${reloadLimitParam}${expandParam}`,
      {timeoutMs:120000}
    );
  } finally {
    if (_ownsLoad()) _clearSameSessionForceReloadHint(sid);
  }
  if (!_ownsLoad()) return;
  // Guard: api() may have redirected (401) and returned undefined.
  if (!data || !data.session) return;
  _messagesTruncated = !!data.session._messages_truncated;
  messageTimelineBindings._oldestIdx = data.session._messages_offset || 0;
  _msgLimitMax = data.session._msg_limit_max || _MSG_LIMIT_MAX;
  // #3162: `msgs` is reassigned below by the #3018 ephemeral-field carry-forward,
  // so it must be `let`, not `const`. The `const` form threw a TypeError inside
  // _ensureMessagesLoaded() that surfaced as a "Failed to load conversation messages"
  // toast on every mobile message (SSE/visibility events trigger this reload path
  // more aggressively on mobile).
  let msgs = (data.session.messages || []).filter(m => m && m.role);
  // Skip _syncToolCalls when INFLIGHT exists — the INFLIGHT restore path
  // (loadSession line ~871) will overwrite S.toolCalls from INFLIGHT[sid].toolCalls.
  // Clearing here and then overwriting is wasteful, and if S.busy becomes true
  // before the next render, the fallback can't re-derive from messages.
  if(!(typeof INFLIGHT !== 'undefined' && INFLIGHT && INFLIGHT[sid])){
    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);
  }
  clearLiveToolCards();
  // #3018: preserve client-side ephemeral turn fields (_turnUsage, _turnDuration,
  // _turnTps, _gatewayRouting, _statusCard, _anchor_stream_id) across the loadSession replace.
  if(typeof window._carryForwardEphemeralTurnFields==='function'){
    // #3306: Prefer the pre-clear snapshot stashed by loadSession() on a
    // force-reload of the active session; S.messages was reset to [] there
    // and would otherwise yield an empty carry-forward.
    const _prev = (Array.isArray(sessionLoadState.pendingCarryForwardSnapshot) && sessionLoadState.pendingCarryForwardSnapshot.length)
      ? sessionLoadState.pendingCarryForwardSnapshot
      : (S.messages || []);
    msgs=window._carryForwardEphemeralTurnFields(_prev, msgs);
    sessionLoadState.pendingCarryForwardSnapshot = null;
  }
  if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();
  S.messages = msgs;
  // Expand render window to cover all loaded messages so the next
  // renderMessages() doesn't hide most of them behind a tiny window.
  if(typeof _messageRenderableMessageCount==='function'&&typeof _currentMessageRenderWindowSize==='function'){
    _messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(), _messageRenderableMessageCount());
  }
  if(S.session&&S.session.session_id===sid){
    S.session.message_count=Number(data.session.message_count || msgs.length);
    S.lastUsage={...(data.session.last_usage||S.lastUsage||{})};
    // Phase 2: the messages=1 response carries the canonical cold-load
    // `todo_state` snapshot, derived server-side from the FULL untruncated
    // message list (api/routes.py + api/todo_state.py). The earlier
    // messages=0 fetch in loadSession() does not include this field —
    // attach_todo_state is gated on `load_messages`. Without applying it
    // here, long sessions whose latest todo write falls outside the
    // _INITIAL_MSG_LIMIT tail would lose the panel on refresh: the
    // legacy reverse-scan in _legacyTodosFromMessages() can only see the
    // tail S.messages, while the authoritative snapshot was already
    // computed by the server and is sitting in this very response.
    // _hydrateTodosFromSession is idempotent and picks newer of
    // cold-load vs INFLIGHT by timestamp, so calling it again here is
    // safe even when an INFLIGHT snapshot was already restored.
    if(data.session.todo_state !== undefined){
      S.session.todo_state = data.session.todo_state;
    }else{
      delete S.session.todo_state;
    }
    if(typeof _hydrateTodosFromSession === 'function'){
      _hydrateTodosFromSession(S.session);
    }
    if(typeof scheduleTodosRefresh === 'function'){
      scheduleTodosRefresh();
    }
    // Only sync the viewed count (which also clears any completion-unread
    // marker via _setSessionViewedCount -> _clearSessionCompletionUnread)
    // when the session is STILL actively viewed. A hidden-tab completion that
    // lands during the awaited message fetch above must NOT be silently marked
    // read here — mirror the same _isSessionActivelyViewedForList(sid) guard
    // used on the post-load re-ack in loadSession(). (#5917 gate finding)
    if(typeof _isSessionActivelyViewedForList !== 'function' || _isSessionActivelyViewedForList(sid)){
      _setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));
    }
    if(typeof syncTopbar==='function') syncTopbar();
  }
}

export const sessionMessages=Object.freeze({open:_openSidebarSession,isReadOnly:_isReadOnlySession,isCli:_isCliSession,ensureLoaded:_ensureMessagesLoaded});

export { _INITIAL_MSG_LIMIT, _captureSameSessionForceReloadHint, _checkAndShowHandoffHint, _clearHandoffStorageForSession, _clearSameSessionForceReloadHint, _clearSessionSourceTabCounts, _deferWorkspaceRefreshForSession, _dismissHandoffHint, _ensureMessagesLoaded, _externalImportPayload, _getChannelLabel, _hideHandoffHint, _isBranchableReadOnlySession, _isCliImportRefreshPrefixMatch, _isCliSession, _isExternalSession, _isMessagingSession, _isReadOnlySession, _msgLimitMax, _openSidebarSession, _requestedSessionSidebarSource, _resolveSessionModelForDisplaySoon, _restoreSessionSourceFilter, _sessionArchivePagingFilterActive, _sessionListExcludeHiddenEnabled, _sessionListQueryString, _sessionSourceLabel, _sessionSourceTabCount, _setActiveProjectFilter, _setSessionSourceFilter, _sourceKeyForSession, _syncToolCallsForLoadedMessages };

export const messageLoadingBindings=Object.freeze({
  get _messagesTruncated(){ return _messagesTruncated; },
  set _messagesTruncated(value){ _messagesTruncated=value; },
});
