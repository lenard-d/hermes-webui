import { loadSession } from './lifecycle.js';
import { _isCliSession, _sourceKeyForSession } from './message-loading.js';
import { _messageComparableText } from './message-timeline.js';
import { sidebarStateBindings } from './sidebar-state.js';
import { _deferActiveSessionExternalRefresh, refreshActiveSessionIfExternallyUpdated } from './session-list.js';
import { renderSessionListFromCache } from './sidebar-renderer.js';

const ICONS={
  stop:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>',
  pin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><polygon points="8,1.5 9.8,5.8 14.5,6.2 11,9.4 12,14 8,11.5 4,14 5,9.4 1.5,6.2 6.2,5.8"/></svg>',
  unpin:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><polygon points="8,2 9.8,6.2 14.2,6.2 10.7,9.2 12,13.8 8,11 4,13.8 5.3,9.2 1.8,6.2 6.2,6.2"/></svg>',
  folder:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M2 4.5h4l1.5 1.5H14v7H2z"/></svg>',
  archive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><line x1="6" y1="8.5" x2="10" y2="8.5"/></svg>',
  unarchive:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2" width="13" height="3" rx="1"/><path d="M2.5 5v8h11V5"/><polyline points="6.5,7 8,5.5 9.5,7"/></svg>',
  dup:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="4.5" y="4.5" width="8.5" height="8.5" rx="1.5"/><path d="M3 11.5V3h8.5"/></svg>',
  trash:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5v8.5h7v-8.5"/><line x1="7" y1="7" x2="7" y2="11"/><line x1="9" y1="7" x2="9" y2="11"/></svg>',
  more:'<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="none"><circle cx="8" cy="3" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="8" cy="13" r="1.25"/></svg>',
  edit:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 2.5l2 2L5 13H3v-2z"/><path d="M10 4l2 2"/></svg>',
  spark:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8l1.1 3.1 3.1 1.1-3.1 1.1L8 10.2 6.9 7.1 3.8 6l3.1-1.1z"/><path d="M12.5 9.5l.5 1.5 1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5z"/></svg>',
  link:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6.7 9.3a3 3 0 0 1 0-4.2l1.7-1.7a3 3 0 0 1 4.2 4.2l-1 1"/><path d="M9.3 6.7a3 3 0 0 1 0 4.2l-1.7 1.7a3 3 0 0 1-4.2-4.2l1-1"/></svg>',
  download:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14 10.5v2.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-2.5"/><polyline points="4.5 7 8 10.5 11.5 7"/><line x1="8" y1="10.5" x2="8" y2="2"/></svg>',
};

// Tracks which session_id is currently being loaded. Used to discard stale
// responses from in-flight requests when the user switches sessions again
// before the first request completes (#1060).
let _loadingSessionId = null;
// Each loadSession() invocation gets a monotonically increasing generation.
// `_loadingSessionId` only tracks destination session_id, so same-session
// concurrent loads can still race and overwrite each other unless we compare
// the generation token as well.
let _loadSessionGeneration = 0;
// #3306: Snapshot of S.messages captured by loadSession() right before it
// clears them on a force-reload of the active session. Consumed by
// _ensureMessagesLoaded() when calling _carryForwardEphemeralTurnFields so
// ephemeral fields (_turnUsage, _turnDuration, _turnTps, _gatewayRouting,
// _statusCard, _anchor_stream_id) survive the wholesale replace. null when there is nothing
// to carry forward (initial load, switch-to-different-session, etc.).
let _pendingCarryForwardSnapshot = null;

// ── Composer draft persistence ────────────────────────────────────────────────

// Debounced save — prevents hammering the server on every keystroke.
let _draftSaveTimer = null;
const _DRAFT_SAVE_DELAY_MS = 400;
const NEW_CHAT_DRAFT_SESSION_KEY = 'hermes-new-chat-draft-session';
const _composerDraftKnownPayloadSessions = new Set();
const _composerDraftRestoreSuppressedUntilBySid = new Map();
const _COMPOSER_DRAFT_RESTORE_SUPPRESS_MS = 30000;

function _composerDraftFileSignature(file) {
  if (typeof file === 'string') return { value: file };
  if (!file || typeof file !== 'object') return { value: String(file || '') };
  return {
    name: String(file.name || file.filename || ''),
    path: String(file.path || ''),
    size: Number.isFinite(Number(file.size)) ? Number(file.size) : null,
    type: String(file.type || file.mime || ''),
  };
}

// A live browser `File` JSON-serializes to `{}`, so a draft persisted to the
// server loses its name/size/type. Canonicalize files to a plain serializable
// shape BEFORE both persisting and signing, so the suppression signature of the
// just-sent payload matches the signature of the same payload after it has
// round-tripped through the server draft (otherwise a text+attachment send never
// matches its own suppression and the stale tail can repopulate — #5471).
function _composerDraftFilesForPersist(files) {
  if (!Array.isArray(files)) return [];
  return files.filter(Boolean).map((file) => {
    if (typeof file === 'string') return file;
    if (!file || typeof file !== 'object') return String(file || '');
    const canon = {
      name: String(file.name || file.filename || ''),
      path: String(file.path || ''),
      size: Number.isFinite(Number(file.size)) ? Number(file.size) : null,
      type: String(file.type || file.mime || ''),
    };
    if (Number.isFinite(Number(file.lastModified))) canon.lastModified = Number(file.lastModified);
    return canon;
  });
}

function _composerDraftPayloadSignature(text, files) {
  const normalizedText = String(text || '');
  const normalizedFiles = _composerDraftFilesForPersist(files).map(_composerDraftFileSignature);
  return JSON.stringify({ text: normalizedText, files: normalizedFiles });
}

function _composerDraftPayloadSignatureForSid(sid) {
  if (typeof S === 'undefined' || !S.session || S.session.session_id !== sid) return null;
  const draft = S.session.composer_draft || null;
  if (!draft) return null;
  return _composerDraftPayloadSignature(draft.text, draft.files);
}

function _suppressComposerDraftRestoreAfterSubmit(sid, text, files) {
  if (!sid) return;
  const previous = _composerDraftRestoreSuppressedUntilBySid.get(sid);
  // Collect EVERY signature a stale poll could legitimately echo back for this
  // just-sent turn, and suppress a restore matching ANY of them (#5471):
  //  - the submitted-payload signature (final textarea content on send), AND
  //  - the REMEMBERED SERVER DRAFT signature — what was actually persisted last.
  // The two differ in the common Enter-to-send case: `_clearComposerDraft`
  // cancels the pending debounced save, so the server's last draft is often a
  // PREFIX of the submitted text (pause ≥400ms, then send within 400ms of the
  // last keystroke) — an exact submitted-text match would miss that prefix and
  // let it restore. The remembered-server-draft signature must be read BEFORE
  // the `_rememberComposerDraftPayloadState(sid,'',[])` reset below. A genuinely
  // new cross-tab draft matches neither, so it still restores immediately.
  const signatures = [];
  const _addSig = (s) => { if (s && signatures.indexOf(s) === -1) signatures.push(s); };
  _addSig(_composerDraftPayloadSignatureForSid(sid));   // remembered server draft (read first)
  if (arguments.length >= 2) {
    _addSig(_composerDraftPayloadSignature(text, files));  // submitted payload
  } else if (previous && typeof previous === 'object' && Array.isArray(previous.signatures)) {
    previous.signatures.forEach(_addSig);
  }
  _composerDraftRestoreSuppressedUntilBySid.set(
    sid,
    { until: Date.now() + _COMPOSER_DRAFT_RESTORE_SUPPRESS_MS, signatures },
  );
  // Local state must reflect the submitted/cleared composer immediately. The
  // POST that clears the server-side draft is async; same-session refreshes can
  // otherwise race in with the old draft and repopulate the textarea.
  _rememberComposerDraftPayloadState(sid, '', []);
}

function _clearComposerDraftRestoreSuppression(sid) {
  if (!sid) return;
  _composerDraftRestoreSuppressedUntilBySid.delete(sid);
}

function _isComposerDraftRestoreSuppressed(sid, text, files) {
  if (!sid) return false;
  const suppression = _composerDraftRestoreSuppressedUntilBySid.get(sid);
  if (!suppression) return false;
  const until = (suppression && typeof suppression === 'object') ? suppression.until : suppression;
  if (!until) return false;
  if (Date.now() > until) {
    _composerDraftRestoreSuppressedUntilBySid.delete(sid);
    return false;
  }
  const signatures = (suppression && typeof suppression === 'object' && Array.isArray(suppression.signatures))
    ? suppression.signatures
    : null;
  // Legacy/unknown callers still fail closed for the current TTL, but all send
  // paths now pass payload signatures so a different cross-tab draft can restore.
  if (!signatures || !signatures.length) return true;
  if (signatures.indexOf(_composerDraftPayloadSignature(text, files)) !== -1) return true;
  _composerDraftRestoreSuppressedUntilBySid.delete(sid);
  return false;
}

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}

function _sessionEventProfilesMatch(eventProfile, activeProfile){
  if(!(typeof eventProfile === 'string' && eventProfile.trim())) return true;
  return _profileMatchesActiveProfile(eventProfile, activeProfile);
}

function _isRestorableNewChatDraftSession(session, requireDraft=false) {
  if (!session || !session.session_id) return false;
  const messageCount = Number(session.message_count || 0);
  if (messageCount !== 0) return false;
  if (session.active_stream_id || session.pending_user_message || session.worktree_path || session.has_pending_user_message) return false;
  const title = session.title || 'Untitled';
  if (title !== 'Untitled' && title !== 'New Chat') return false;
  const activeProfile = S.activeProfile || 'default';
  const sessionProfile = session.profile || 'default';
  if (!_profileMatchesActiveProfile(sessionProfile, activeProfile)) return false;
  if (!requireDraft) return true;
  const draft = session.composer_draft || {};
  const text = (typeof draft.text === 'string') ? draft.text : '';
  const files = Array.isArray(draft.files) ? draft.files : [];
  return !!(text || files.length);
}

function _rememberNewChatDraftSession(session) {
  if (!_isRestorableNewChatDraftSession(session)) return;
  try { localStorage.setItem(NEW_CHAT_DRAFT_SESSION_KEY, session.session_id); } catch (_) {}
}

function _clearRememberedNewChatDraftSession(sid) {
  if (!sid) return;
  try {
    if (localStorage.getItem(NEW_CHAT_DRAFT_SESSION_KEY) === sid) {
      localStorage.removeItem(NEW_CHAT_DRAFT_SESSION_KEY);
    }
  } catch (_) {}
}

async function _restoreRememberedNewChatDraftSession() {
  let sid = '';
  try { sid = localStorage.getItem(NEW_CHAT_DRAFT_SESSION_KEY) || ''; } catch (_) { sid = ''; }
  if (!sid || (S.session && S.session.session_id === sid)) return false;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    const session = data && data.session;
    if (!_isRestorableNewChatDraftSession(session, true)) {
      _clearRememberedNewChatDraftSession(sid);
      return false;
    }
    await loadSession(sid, {skipLineageResolve:true});
    return !!(S.session && S.session.session_id === sid);
  } catch (_) {
    _clearRememberedNewChatDraftSession(sid);
    return false;
  }
}

function _saveComposerDraft(sid, text, files) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  const normalizedText = String(text || '');
  const normalizedFiles = _composerDraftFilesForPersist(files);
  if (_composerDraftHasPayload(normalizedText, normalizedFiles)) {
    _clearComposerDraftRestoreSuppression(sid);
    _composerDraftKnownPayloadSessions.add(sid);
  }
  _draftSaveTimer = setTimeout(() => {
    api('/api/session/draft', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, text: normalizedText, files: normalizedFiles }),
    }).then(() => {
      _rememberComposerDraftPayloadState(sid, normalizedText, normalizedFiles);
    }).catch(() => {});
  }, _DRAFT_SAVE_DELAY_MS);
}

function _composerDraftHasPayload(text, files) {
  return !!(String(text || '') || (Array.isArray(files) && files.filter(Boolean).length));
}

function _sessionComposerDraftHasPayload(session) {
  const draft = session && session.composer_draft;
  return !!(draft && _composerDraftHasPayload(draft.text, draft.files));
}

function _rememberComposerDraftPayloadState(sid, text, files) {
  if (!sid) return;
  const normalizedText = String(text || '');
  const normalizedFiles = Array.isArray(files) ? files.filter(Boolean) : [];
  if (_composerDraftHasPayload(normalizedText, normalizedFiles)) {
    _composerDraftKnownPayloadSessions.add(sid);
  } else {
    _composerDraftKnownPayloadSessions.delete(sid);
  }
  if (S.session && S.session.session_id === sid) {
    S.session.composer_draft = { text: normalizedText, files: normalizedFiles };
  }
}

// Immediate save used before session switches.
function _saveComposerDraftNow(sid, text, files) {
  if (!sid) return Promise.resolve();
  clearTimeout(_draftSaveTimer);
  const normalizedText = String(text || '');
  const normalizedFiles = _composerDraftFilesForPersist(files);
  if (_composerDraftHasPayload(normalizedText, normalizedFiles)) {
    _clearComposerDraftRestoreSuppression(sid);
  }
  // Most chat switches leave an empty composer. Avoid putting the switch path
  // behind a network POST unless there is new local draft content or an existing
  // server draft that must be cleared.
  if (!_composerDraftHasPayload(normalizedText, normalizedFiles)
      && S.session && S.session.session_id === sid
      && !_sessionComposerDraftHasPayload(S.session)
      && !_composerDraftKnownPayloadSessions.has(sid)) {
    return Promise.resolve();
  }
  return api('/api/session/draft', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, text: normalizedText, files: normalizedFiles }),
  }).then(() => {
    _rememberComposerDraftPayloadState(sid, normalizedText, normalizedFiles);
  }).catch(() => {});
}

// Restore composer draft from server onto #msg textarea.
// Only restores if there's actual text (skip empty/None drafts).
// Guards against double-restore when rapidly switching sessions.
function _restoreComposerDraft(draft, targetSid, opts={}) {
  const ta = $('msg');
  if (!ta) return;
  // targetSid is the session that was requested — if it no longer matches
  // _loadingSessionId, a newer session switch has already begun, so skip.
  if (targetSid && _loadingSessionId !== null && _loadingSessionId !== targetSid) return;
  const text = (draft && typeof draft.text === 'string') ? draft.text : '';
  const files = (draft && Array.isArray(draft.files)) ? draft.files : [];
  const current = ta.value || '';
  const preserveActiveInput = !!(opts && opts.preserveActiveInput);
  const restoreSid = targetSid || (S.session && S.session.session_id);
  const hasServerDraftPayload = _composerDraftHasPayload(text, files);

  if (restoreSid && hasServerDraftPayload && _isComposerDraftRestoreSuppressed(restoreSid, text, files)) return;
  if (restoreSid && !hasServerDraftPayload) _clearComposerDraftRestoreSuppression(restoreSid);

  // Same-session force refreshes are driven by external state changes and may
  // finish seconds after the user continued typing. In that case the local
  // composer is the authoritative in-progress draft; never replace non-empty
  // local input with an older server draft. Cross-session switches still restore
  // normally so the previous session's composer contents do not leak forward.
  if (preserveActiveInput && current && current !== text) return;

  // If there's no text and no files, clear the textarea (a previous session's
  // draft may still be sitting there from a cross-session switch).
  if (!text && !files.length) {
    if (current) {
      ta.value = '';
      if (typeof autoResize === 'function') autoResize();
      if (typeof updateSendBtn === 'function') updateSendBtn();
    }
    return;
  }
  // Only update if different to avoid cursor jumps on unrelated session switches.
  if (current !== text) {
    ta.value = text;
    if (typeof autoResize === 'function') autoResize();
    if (typeof updateSendBtn === 'function') updateSendBtn();
  }
  // Files restoration is skipped for now (requires S.pendingFiles plumbing).
}

// Clear the saved draft for a session (called when message is sent).
function _clearComposerDraft(sid, text, files) {
  if (!sid) return;
  clearTimeout(_draftSaveTimer);
  _clearRememberedNewChatDraftSession(sid);
  if (arguments.length >= 2) _suppressComposerDraftRestoreAfterSubmit(sid, text, files);
  else _suppressComposerDraftRestoreAfterSubmit(sid);
  return api('/api/session/draft', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, text: '' }),
  }).then(() => {
    _rememberComposerDraftPayloadState(sid, '', []);
  }).catch(() => {});
}

const SESSION_VIEWED_COUNTS_KEY = 'hermes-session-viewed-counts';
const SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread';
const SESSION_OBSERVED_STREAMING_KEY = 'hermes-session-observed-streaming';
// Per-profile session-count cache (issue #4717 / #4662 Phase 1.5). Records how
// many sessions each profile rendered last time, keyed by profile name, so a
// profile switch can pick an honest loading skeleton BEFORE the new /api/sessions
// fetch resolves: a profile we last saw with zero sessions shows an empty-state
// placeholder instead of a content skeleton that implies data which never arrives.
// A profile we've never recorded falls back to the normal content skeleton (safe
// default — never hide a skeleton for a profile that may well have conversations).
const SESSION_PROFILE_COUNTS_KEY = 'hermes-session-profile-counts';
let _sessionProfileCounts = null;
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
let _sessionObservedStreaming = null;
const _sessionStreamingById = new Map();
const _sessionListSnapshotById = new Map();
const _sessionListSourceById = new Map();
let _sessionListPointerActive = false;
let _sessionListLastScrollAt = 0;
let _pendingSessionListPayload = null;
let _pendingSessionListApplyTimer = 0;
let _sessionListLoadError = null;
let _sessionListHasLoadedOnce = false;
const _SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
const SESSION_LIST_INTERACTION_IDLE_MS = 700;
const SESSION_SWIPE_DURATION_MS = 500;
const SESSION_SWIPE_REFLOW_LEAD_MS = 220;
const SESSION_REFLOW_TIMEOUT_MS = 420;
const SESSION_LIST_FLIP_TIMEOUT_MS = 460;
const SESSION_LONG_PRESS_DELAY_MS = 400;
const SESSION_ARCHIVE_SWIPE_THRESHOLD_PX = 128;
const SESSION_DELETE_SWIPE_THRESHOLD_PX = 128;
const SESSION_SWIPE_CANCEL_RATIO = 0.75;

function _manualTitleAuxConfigFromPayload(auxData){
  if(!auxData||typeof auxData!=='object'||Array.isArray(auxData)) return null;
  if(auxData.title_generation&&typeof auxData.title_generation==='object') return auxData;
  const tasks=Array.isArray(auxData.tasks)?auxData.tasks:null;
  if(!tasks) return null;
  const taskMap={};
  for(const task of tasks){
    if(task&&typeof task==='object'&&typeof task.task==='string'&&task.task){
      taskMap[task.task]=task;
    }
  }
  if(!Object.keys(taskMap).length) return null;
  return taskMap;
}

async function _loadManualTitleAuxConfig(){
  try{
    const auxData=await api('/api/model/auxiliary',{retries:0,timeoutToast:false});
    return _manualTitleAuxConfigFromPayload(auxData);
  }catch(_){
    return null;
  }
}

async function _manualTitleRegenerateTimeoutMs(){
  let cfg=null;
  try{
    const auxConfig=await _loadManualTitleAuxConfig();
    cfg=auxConfig&&auxConfig.title_generation;
  }catch(_){
    return null;
  }
  const timeoutSeconds=Number(cfg&&cfg.timeout);
  if(!Number.isFinite(timeoutSeconds)||timeoutSeconds<=0) return null;
  return Math.max(30000,Math.round((timeoutSeconds+5)*1000));
}

function _formatSessionModelWithGateway(s){
  if(!s||!s.model)return'';
  const routing=(typeof _latestGatewayRoutingForSession==='function')?_latestGatewayRoutingForSession(s):(s.gateway_routing||null);
  if(typeof _formatGatewayModelLabel==='function'){
    return _formatGatewayModelLabel(s.model,s.model,routing)||getModelLabel(s.model);
  }
  return s.model;
}

function _getSessionViewedCounts() {
  if (_sessionViewedCounts !== null) return _sessionViewedCounts;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_VIEWED_COUNTS_KEY) || '{}');
    _sessionViewedCounts = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionViewedCounts = {};
  }
  return _sessionViewedCounts;
}

// ── Per-profile session-count cache (#4717) ──────────────────────────────────
function _getSessionProfileCounts() {
  if (_sessionProfileCounts !== null) return _sessionProfileCounts;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_PROFILE_COUNTS_KEY) || '{}');
    _sessionProfileCounts = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionProfileCounts = {};
  }
  return _sessionProfileCounts;
}

// Record how many sessions a profile currently shows, so the NEXT switch into
// it can pick an honest skeleton. Called after a real list render resolves.
function _recordSessionProfileCount(profile, count) {
  const name = (profile || '').trim();
  if (!name) return;
  const n = Number(count);
  if (!Number.isFinite(n) || n < 0) return;
  const counts = _getSessionProfileCounts();
  if (counts[name] === n) return;  // no-op write avoidance
  counts[name] = n;
  try {
    localStorage.setItem(SESSION_PROFILE_COUNTS_KEY, JSON.stringify(counts));
  } catch (_){
    // Ignore localStorage write failures (private mode / quota).
  }
}

// Return the last-known session count for a profile, or null if we've never
// recorded one (caller must treat null as "unknown" → keep the content skeleton).
function _knownSessionProfileCount(profile) {
  const name = (profile || '').trim();
  if (!name) return null;
  const counts = _getSessionProfileCounts();
  const v = counts[name];
  return (typeof v === 'number' && Number.isFinite(v)) ? v : null;
}

function _saveSessionViewedCounts() {
  try {
    localStorage.setItem(SESSION_VIEWED_COUNTS_KEY, JSON.stringify(_getSessionViewedCounts()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _setSessionViewedCount(sid, messageCount = 0) {
  if (!sid) return;
  const counts = _getSessionViewedCounts();
  const next = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  counts[sid] = next;
  _saveSessionViewedCounts();
  // If the viewed count is now current, any prior completion-unread marker is
  // stale — clear it so _hasUnreadForSession doesn't short-circuit (#3020).
  _clearSessionCompletionUnread(sid);
}

function _getSessionCompletionUnread() {
  if (_sessionCompletionUnread !== null) return _sessionCompletionUnread;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_COMPLETION_UNREAD_KEY) || '{}');
    _sessionCompletionUnread = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionCompletionUnread = {};
  }
  return _sessionCompletionUnread;
}

function _saveSessionCompletionUnread() {
  try {
    localStorage.setItem(SESSION_COMPLETION_UNREAD_KEY, JSON.stringify(_getSessionCompletionUnread()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _markSessionCompletionUnread(sid, messageCount = 0, meta = null) {
  if (!sid) return;
  const unread = _getSessionCompletionUnread();
  const count = Number.isFinite(messageCount) ? Number(messageCount) : 0;
  const entry = {message_count: count, completed_at: Date.now()};
  // Cron markers carry source+profile so profile switches can clear only that
  // cross-profile leak without wiping ordinary chat completion unread (#5960).
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    if (meta.source) entry.source = String(meta.source);
    if (typeof meta.profile === 'string' && meta.profile.trim()) {
      entry.profile = meta.profile.trim();
    }
  }
  unread[sid] = entry;
  _saveSessionCompletionUnread();
}

function _markSessionCompletionUnreadIfBackground(sid, messageCount = null, meta = null) {
  if (!sid) return false;
  let count = Number.isFinite(messageCount) ? Number(messageCount) : NaN;
  if (!Number.isFinite(count)) {
    const snapshot = _sessionListSnapshotById.get(sid)
      || (sidebarStateBindings._allSessions || []).find(s => s && s.session_id === sid)
      || null;
    count = Number(snapshot && snapshot.message_count) || 0;
  }
  if (_isSessionActivelyViewedForList(sid)) {
    _setSessionViewedCount(sid, count);
    if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
    return false;
  }
  _markSessionCompletionUnread(sid, count, meta);
  if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
  return true;
}

function _clearSessionCompletionUnread(sid) {
  if (!sid) return;
  const unread = _getSessionCompletionUnread();
  if (!Object.prototype.hasOwnProperty.call(unread, sid)) return;
  delete unread[sid];
  _saveSessionCompletionUnread();
}

// True when a session row is a cron-origin session for unread-dot scoping.
function _isCronSessionForUnread(session) {
  if (!session) return false;
  const key = (typeof _sourceKeyForSession === 'function')
    ? _sourceKeyForSession(session)
    : String(
      session.raw_source
      || session.source_tag
      || session.source
      || session.session_source
      || ''
    ).toLowerCase();
  if (key === 'cron') return true;
  return String(session.session_source || '').toLowerCase() === 'cron';
}

// Build {source, profile} for a cron session row; null for ordinary chat.
function _cronCompletionUnreadMetaForSession(session) {
  if (!_isCronSessionForUnread(session)) return null;
  const fromRow = (session && typeof session.profile === 'string' && session.profile.trim())
    ? session.profile.trim()
    : '';
  const active = (typeof S !== 'undefined' && S && typeof S.activeProfile === 'string' && S.activeProfile.trim())
    ? S.activeProfile.trim()
    : 'default';
  return {source: 'cron', profile: fromRow || active};
}

// Resolve whether a persisted marker is cron and which profile owns it.
// Untagged/legacy markers are migrated from the sidebar session row when known.
function _resolveCronCompletionMarkerOrigin(sid, marker) {
  let isCron = !!(marker && marker.source === 'cron');
  let profile = (marker && typeof marker.profile === 'string' && marker.profile.trim())
    ? marker.profile.trim()
    : '';
  let session = null;
  if (Array.isArray(sidebarStateBindings._allSessions)) {
    session = sidebarStateBindings._allSessions.find((s) => s && s.session_id === sid) || null;
  }
  if (!session && typeof _sessionListSnapshotById !== 'undefined'
    && _sessionListSnapshotById && typeof _sessionListSnapshotById.get === 'function') {
    // Snapshot alone lacks source/profile; keep null.
    session = null;
  }
  if (session) {
    if (!isCron && _isCronSessionForUnread(session)) isCron = true;
    if (!profile) {
      const sp = (typeof session.profile === 'string' && session.profile.trim())
        ? session.profile.trim()
        : '';
      if (sp) profile = sp;
    }
  }
  // Persist migration so later switches don't re-resolve from a cleared list.
  if (marker && isCron) {
    if (marker.source !== 'cron') marker.source = 'cron';
    if (profile && marker.profile !== profile) marker.profile = profile;
  }
  return {isCron, profile: profile || ''};
}

// A profile name provably resolving to the root profile: the literal
// 'default' alias, or a roster entry flagged is_default (renamed root).
// Unknown names fail closed — exact-name matching still applies to them.
function _cronProfileNameIsRootAlias(name) {
  if (name === 'default') return true;
  if (typeof _profilesCache !== 'undefined' && _profilesCache
    && Array.isArray(_profilesCache.profiles)) {
    const entry = _profilesCache.profiles.find((p) => p && p.name === name);
    if (entry && entry.is_default) return true;
  }
  return false;
}

// default/renamed-root equivalence for cron-marker ownership (mirrors server
// _profiles_match enough for the active surface: literal 'default' ↔ root).
function _cronMarkerProfileMatchesActive(origin, activeProfile) {
  const originName = (typeof origin === 'string' && origin.trim()) ? origin.trim() : '';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim())
    ? activeProfile.trim()
    : 'default';
  if (!originName) return false;
  if (originName === activeName) return true;
  if (typeof _profileMatchesActiveProfile === 'function'
    && _profileMatchesActiveProfile(originName, activeName)) {
    return true;
  }
  // Reverse alias: marker tagged with the renamed-root name while the active
  // root surface reports a different alias. Match only when the origin name
  // ITSELF provably resolves to the root — never "active is default → match
  // all", which under-cleared other profiles' markers on switch to 'default'.
  if (typeof S !== 'undefined' && S && S.activeProfileIsDefault
    && typeof _cronProfileNameIsRootAlias === 'function'
    && _cronProfileNameIsRootAlias(originName)) {
    return true;
  }
  return false;
}

// Drop persisted cron unread dots that belong to inactive profiles. Ordinary
// (non-cron) completion markers stay put — sticky all-profile sidebars still
// need those. Called from the shared profile-switch reset in panels.js.
function _clearCronSessionCompletionUnreadForInactiveProfiles(activeProfile) {
  const active = (typeof activeProfile === 'string' && activeProfile.trim())
    ? activeProfile.trim()
    : 'default';
  const unread = _getSessionCompletionUnread();
  let changed = false;
  for (const sid of Object.keys(unread)) {
    const marker = unread[sid];
    if (!marker || typeof marker !== 'object' || Array.isArray(marker)) continue;
    const resolved = _resolveCronCompletionMarkerOrigin(sid, marker);
    if (!resolved.isCron) continue;
    // Only clear when we know the owning profile AND it is not the active one
    // (incl. default/renamed-root equivalence). Untagged + unresolvable stays.
    if (!resolved.profile) continue;
    if (_cronMarkerProfileMatchesActive(resolved.profile, active)) continue;
    delete unread[sid];
    changed = true;
  }
  if (!changed) return false;
  _saveSessionCompletionUnread();
  if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
  return true;
}

function _clearSessionViewedCount(sid) {
  if (!sid) return;
  const counts = _getSessionViewedCounts();
  if (!Object.prototype.hasOwnProperty.call(counts, sid)) return;
  delete counts[sid];
  _saveSessionViewedCounts();
}

function _hasSessionCompletionUnread(sid) {
  if (!sid) return false;
  return Object.prototype.hasOwnProperty.call(_getSessionCompletionUnread(), sid);
}

function _getSessionObservedStreaming() {
  if (_sessionObservedStreaming !== null) return _sessionObservedStreaming;
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_OBSERVED_STREAMING_KEY) || '{}');
    _sessionObservedStreaming = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_){
    _sessionObservedStreaming = {};
  }
  return _sessionObservedStreaming;
}

function _saveSessionObservedStreaming() {
  try {
    localStorage.setItem(SESSION_OBSERVED_STREAMING_KEY, JSON.stringify(_getSessionObservedStreaming()));
  } catch (_){
    // Ignore localStorage write failures.
  }
}

function _rememberObservedStreamingSession(s) {
  if (!s || !s.session_id) return;
  const observed = _getSessionObservedStreaming();
  observed[s.session_id] = {
    message_count: Number(s.message_count || 0),
    last_message_at: Number(s.last_message_at || 0),
    observed_at: Date.now(),
  };
  _saveSessionObservedStreaming();
}

function _forgetObservedStreamingSession(sid) {
  if (!sid) return;
  const observed = _getSessionObservedStreaming();
  if (!Object.prototype.hasOwnProperty.call(observed, sid)) return;
  delete observed[sid];
  _saveSessionObservedStreaming();
}

function _hasUnreadForSession(s) {
  if (!s || !s.session_id) return false;
  if (_hasSessionCompletionUnread(s.session_id)) return true;
  const counts = _getSessionViewedCounts();
  if (!Object.prototype.hasOwnProperty.call(counts, s.session_id)) {
    _setSessionViewedCount(s.session_id, Number(s.message_count || 0));
    return false;
  }
  if (!Number.isFinite(s.message_count)) return false;
  return s.message_count > Number(counts[s.session_id] || 0);
}

// Keep the sidebar polling snapshot current for a just-visited session so a
// deferred /api/sessions list refresh landing across the async message-load gap
// cannot treat the unchanged, already-open session as a fresh background
// completion and re-flag a stale unread dot (#4946).
function _syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt) {
  if (!sid) return;
  const count = Number(messageCount || 0);
  const last = Number(lastMessageAt || 0);
  _sessionListSnapshotById.set(sid, {message_count: count, last_message_at: last});
  // #5917 gate finding: derive the visited session's streaming state from its
  // OWN (target-owned) metadata, NOT the global S.busy / S.activeStreamId
  // flags. When switching from a BUSY session A to an IDLE session B, those
  // globals can still belong to A at this point in the load, so reading them
  // here would wrongly record idle B as streaming — a later hidden-tab poll
  // would then see a streaming->stopped transition and manufacture a phantom
  // unread completion for B. Only the session object's own is_streaming /
  // active_stream_id / pending-message fields describe THIS session.
  const target = (S.session && S.session.session_id === sid) ? S.session : null;
  const isStreaming = Boolean(
    target && (
      target.is_streaming ||
      target.active_stream_id ||
      target.pending_user_message ||
      target.has_pending_user_message
    )
  );
  _sessionStreamingById.set(sid, isStreaming);
  if (!isStreaming) _forgetObservedStreamingSession(sid);
}

// Acknowledge that the user actually visited/opened `sid`: clear its viewed
// count (which also clears any stale completion-unread marker, #3020), sync the
// polling snapshot so a deferred list poll cannot re-flag it, then repaint from
// cache. Repainting via renderSessionListFromCache() recomputes each row's
// aggregated unread state (own + children) authoritatively, so a lineage
// PARENT keeps its own / other children's unread dot instead of being stripped
// by ad-hoc DOM surgery (Greptile concern (b) on #4946).
function _acknowledgeSessionVisit(sid, messageCount = 0, lastMessageAt = 0) {
  if (!sid) return;
  _setSessionViewedCount(sid, messageCount);
  _syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt);
  if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
}

// Does the session currently carry any unread state that a visit should clear?
// Used by the same-session no-op guard so re-selecting the already-open session
// still clears a stale dot before short-circuiting.
function _sessionVisitHasUnreadState(sid) {
  if (!sid) return false;
  if (_hasSessionCompletionUnread(sid)) return true;
  if (!S.session || S.session.session_id !== sid) return false;
  return _hasUnreadForSession(S.session);
}

function _isSessionActivelyViewedForList(sid) {
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  if (typeof _loadingSessionId !== 'undefined' && _loadingSessionId && _loadingSessionId !== sid) return false;
  if (typeof document !== 'undefined' && document.visibilityState && document.visibilityState !== 'visible') return false;
  if (typeof document !== 'undefined' && typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
  return true;
}

function _isSessionLocallyStreaming(s) {
  if (!s || !s.session_id) return false;
  const isActive = S.session && s.session_id === S.session.session_id;
  // For the active session, rely on S.busy to indicate an ongoing stream.
  // INFLIGHT entries for non-active sessions are artifacts of interrupted
  // streams (page refresh, network disconnect, gateway restart) where
  // `delete INFLIGHT[sid]` was never reached — they should NOT cause the
  // sidebar spinner to appear on completed sessions. (#2066)
  return isActive && Boolean(S.busy);
}

function _isSessionEffectivelyStreaming(s) {
  return Boolean(s && (
    s.is_streaming ||
    _hasPendingUserMessageSignal(s) ||
    _isSessionLocallyStreaming(s)
  ));
}

function _hasPendingUserMessageSignal(s) {
  return Boolean(s && (s.pending_user_message || s.has_pending_user_message));
}

function _isServerIdleSessionRow(s) {
  return Boolean(s && s.session_id && !s.is_streaming && !s.active_stream_id && !s.pending_user_message && !s.has_pending_user_message && !s.pending_started_at);
}

function _reconcileActiveSessionIdleStateFromList(serverRows) {
  if (!S || !S.session || !S.session.session_id) return false;
  if (!Array.isArray(serverRows)) return false;
  const sid=S.session.session_id;
  // #4354: clear a stuck indicator when the server reports idle — server
  // is_streaming/active_stream_id is authoritative. BUT skip the ONE session
  // that is actively mid-send (#2689 start-race): during the /api/chat/start
  // round-trip the server row is still idle while the client owns the optimistic
  // turn, so reconciling it here would blank the just-sent bubble + queue a
  // spurious force-reload. A long-hung session has _sendInProgress===false, so
  // it still gets unstuck — only the in-flight start window is protected.
  if (typeof _sendInProgress !== 'undefined' && _sendInProgress && sid === _sendInProgressSid) return false;
  const serverRow=serverRows.find(s=>s&&s.session_id===sid);
  if (!serverRow) return false;
  if (!_isServerIdleSessionRow(serverRow)) return false;
  let changed=false;
  if (S.busy) { S.busy=false; changed=true; }
  if (S.activeStreamId) { S.activeStreamId=null; changed=true; }
  if (INFLIGHT&&INFLIGHT[sid]) {
    delete INFLIGHT[sid];
    if (typeof clearInflightState==='function') clearInflightState(sid);
    changed=true;
  }
  if (S.session) {
    S.session.active_stream_id=null;
    S.session.pending_user_message=null;
  }
  _sessionStreamingById.set(sid, false);
  _forgetObservedStreamingSession(sid);
  if (typeof hideApprovalCard==='function') hideApprovalCard(true);
  if (typeof hideLiveRunStatus==='function') hideLiveRunStatus(sid);
  if (typeof clearLiveToolCards==='function') clearLiveToolCards();
  if (changed&&typeof updateSendBtn==='function') updateSendBtn();
  if (changed&&typeof _scheduleActiveSessionIdleReload==='function') _scheduleActiveSessionIdleReload(sid);
  return changed;
}

function _scheduleActiveSessionIdleReload(sid) {
  if(!sid) return;
  setTimeout(async () => {
    if(!S||!S.session||S.session.session_id !== sid) return;
    // #5409: skip idle reload while any loadSession() is in flight — avoids
    // a race where the idle reload overwrites _loadingSessionId and silently
    // cancels an in-progress session switch (most visible on iOS PWA with
    // large sessions where Phase 1 metadata fetch is slow).
    if(typeof _loadingSessionId !== 'undefined' && _loadingSessionId) return;
    if(S.busy || S.activeStreamId) return;
    if(typeof _isMessageReaderUnpinned==='function'&&_isMessageReaderUnpinned()){
      _deferActiveSessionExternalRefresh('idle-reconcile');
      return;
    }
    try{
      // Avoid an unconditional same-session force reload the moment streaming
      // settles. On mobile PWA this produces a visible end-of-turn flash and can
      // briefly restore the pane with stale layout geometry. Reconcile against
      // server metadata for the just-finished active turn first
      // (ignoreStreamJustFinished bypasses only the post-stream cooldown; the
      // reconcile still reloads ONLY when the message count actually changed).
      // The 'idle-reconcile' reason is non-'poll', so it coexists with the
      // #3916/#4195 poll-only external gate without bypassing it. Preserve the
      // original forced reload as a fallback when the probe request itself fails.
      const outcome = await refreshActiveSessionIfExternallyUpdated('idle-reconcile', {
        ignoreStreamJustFinished: true,
      });
      if(outcome === 'failed'){
        await loadSession(sid, {force:true, externalRefreshReason:'idle-reconcile'});
      }
    }catch(_){}
  },0);
}

function _purgeStaleInflightEntries() {
  // Clean up INFLIGHT entries for sessions the server confirms are NOT
  // streaming. This prevents the in-memory cache from growing unbounded
  // when streams end abnormally. (#2066)  Additionally, any INFLIGHT entry
  // whose session id is no longer present in the current _allSessions list
  // (deleted / archived / filtered out) is also removed so that ghost entries
  // from deleted sessions do not accumulate. (#2092)
  if (typeof INFLIGHT !== 'object' || !INFLIGHT) return;
  const sessionsById = new Map();
  if (Array.isArray(sidebarStateBindings._allSessions)) {
    for (const s of sidebarStateBindings._allSessions) {
      if (s && s.session_id) sessionsById.set(s.session_id, s);
    }
  }
  const sourceById = typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.get === 'function'
    ? _sessionListSourceById
    : null;
  const currentSidebarSource = typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string'
    ? sidebarStateBindings._allSessionsScope.sidebarSource
    : null;
  for (const sid of Object.keys(INFLIGHT)) {
    // #4354: purge stale INFLIGHT even for a hung/idle session, BUT skip the one
    // session actively mid-send (#2689 start-race) — during /api/chat/start the
    // server row is briefly idle while the client owns the optimistic INFLIGHT
    // entry; purging it here would drop the in-flight turn's local state.
    if (typeof _sendInProgress !== 'undefined' && _sendInProgress && sid === _sendInProgressSid) {
      continue;
    }
    if (!sessionsById.has(sid)) {
      const knownSource = sourceById ? sourceById.get(sid) : null;
      if (currentSidebarSource && (!knownSource || knownSource !== currentSidebarSource)) {
        continue;
      }
      // Session is absent from _allSessions — it was deleted / archived /
      // filtered and can never stream again, so drop the entry.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
      continue;
    }
    const s = sessionsById.get(sid);
    if (!s.is_streaming) {
      // Session exists but is not streaming — purge it.
      delete INFLIGHT[sid];
      if (typeof clearInflightState === 'function') clearInflightState(sid);
    }
    // Sessions that exist and are still streaming are preserved.
  }
}

function _rememberSessionListSource(s, sid = null, allowScopeFallback = true) {
  const resolvedSid = sid || (s && s.session_id);
  if (!resolvedSid) return;
  let source = null;
  if (s && typeof _isCliSession === 'function') {
    source = _isCliSession(s) ? 'cli' : 'webui';
  }
  if (!source && Array.isArray(sidebarStateBindings._allSessions)) {
    const cached = sidebarStateBindings._allSessions.find(item => item && item.session_id === resolvedSid);
    if (cached && typeof _isCliSession === 'function') {
      source = _isCliSession(cached) ? 'cli' : 'webui';
    }
  }
  if (!source
    && allowScopeFallback
    && typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string') {
    source = sidebarStateBindings._allSessionsScope.sidebarSource;
  }
  if (source
    && typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.set === 'function') {
    _sessionListSourceById.set(resolvedSid, source);
  }
}

function _rememberRenderedStreamingState(s, isStreaming) {
  if (!s || !s.session_id || !isStreaming) return;
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s);
  _sessionStreamingById.set(s.session_id, true);
  _rememberObservedStreamingSession(s);
}

function _inflightHasVisibleLiveState(inflight) {
  if (!inflight || typeof inflight !== 'object') return false;
  if (String(inflight.lastAssistantText || '').trim()) return true;
  if (String(inflight.lastReasoningText || '').trim()) return true;
  if (String(inflight.liveTurnHtml || '').trim()) return true;
  if (Array.isArray(inflight.toolCalls) && inflight.toolCalls.length) return true;
  if (Array.isArray(inflight.activityBurstAnchors) && inflight.activityBurstAnchors.length) return true;
  if (Array.isArray(inflight.messages)) {
    return inflight.messages.some((msg) => {
      if (!msg) return false;
      if (msg.role === 'user') return Boolean(_messageComparableText(msg));
      if (msg.role !== 'assistant') return false;
      const content = msg.content;
      if (typeof content === 'string') return content.trim();
      if (Array.isArray(content)) return content.length > 0;
      return Boolean(content);
    });
  }
  return false;
}

function _serverLiveSnapshotToolId(tc){
  return String(tc&&(tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'')||'').trim();
}

function _serverLiveSnapshotInflight(snapshot, uploaded){
  if(!snapshot||typeof snapshot!=='object') return null;
  const rawMessages=Array.isArray(snapshot.messages)?snapshot.messages:[];
  const messages=rawMessages
    .filter(m=>m&&m.role)
    .map(m=>({...m,_live:m._live!==false,_journal_snapshot:true}));
  const rawToolCalls=Array.isArray(snapshot.tool_calls)?snapshot.tool_calls:[];
  const toolCalls=rawToolCalls
    .filter(tc=>tc&&tc.name)
    .map(tc=>{
      const next={...tc,_live:true,_journal_snapshot:true};
      const tid=_serverLiveSnapshotToolId(next);
      if(tid&&!next.tid) next.tid=tid;
      return next;
    });
  let lastAssistantText=String(snapshot.last_assistant_text||snapshot.lastAssistantText||'');
  let lastReasoningText=String(snapshot.last_reasoning_text||snapshot.lastReasoningText||'');
  const lastLiveAssistant=[...messages].reverse().find(m=>m&&m.role==='assistant'&&m._live);
  if(lastLiveAssistant){
    if(!lastAssistantText&&typeof lastLiveAssistant.content==='string') lastAssistantText=lastLiveAssistant.content;
    if(!lastReasoningText&&typeof lastLiveAssistant.reasoning==='string') lastReasoningText=lastLiveAssistant.reasoning;
  }
  if((lastAssistantText||lastReasoningText)&&!lastLiveAssistant){
    messages.push({
      role:'assistant',
      content:lastAssistantText,
      reasoning:lastReasoningText||undefined,
      _live:true,
      _journal_snapshot:true,
    });
  }
  const replayAfterSeq=Number(snapshot.last_seq||0);
  const activityBurstAnchors=Array.isArray(snapshot.activity_burst_anchors)
    ? snapshot.activity_burst_anchors
    : (Array.isArray(snapshot.activityBurstAnchors)?snapshot.activityBurstAnchors:[]);
  const anchorActivityScene=(snapshot.anchor_activity_scene&&snapshot.anchor_activity_scene.version==='activity_scene_v1')
    ? snapshot.anchor_activity_scene
    : ((snapshot.anchorActivityScene&&snapshot.anchorActivityScene.version==='activity_scene_v1')?snapshot.anchorActivityScene:null);
  const hasAnchorActivityScene=!!(anchorActivityScene&&Array.isArray(anchorActivityScene.activity_rows)&&anchorActivityScene.activity_rows.length);
  if(!messages.length&&!toolCalls.length&&!lastAssistantText&&!lastReasoningText&&!hasAnchorActivityScene) return null;
  return {
    streamId:String(snapshot.stream_id||snapshot.streamId||''),
    messages,
    uploaded:Array.isArray(uploaded)?[...uploaded]:[],
    toolCalls,
    todos:null,
    todoStateMeta:null,
    reattach:true,
    journalSnapshot:true,
    lastAssistantText,
    lastReasoningText,
    lastRunJournalSeq:Number.isFinite(replayAfterSeq)?Math.max(0,replayAfterSeq):0,
    lastRunJournalEventId:String(snapshot.last_event_id||snapshot.lastEventId||''),
    anchorActivityScene,
    currentActivityBurstId:Number(snapshot.current_activity_burst_id||snapshot.currentActivityBurstId||0)||0,
    currentLiveSegmentSeq:Number(snapshot.current_live_segment_seq||snapshot.currentLiveSegmentSeq||0)||0,
    activityBurstAnchors,
  };
}

function _selectLiveRecoveryInflight(localInflight, serverLiveSnapshot, activeStreamId){
  if(!serverLiveSnapshot) return localInflight||null;
  if(!localInflight||!_inflightHasVisibleLiveState(localInflight)) return serverLiveSnapshot;

  // The run journal owns the Worklog projection. A same-stream browser tail
  // wins only when it advanced after the metadata snapshot was read.
  const requestedActiveId=String(activeStreamId||'').trim();
  const localId=String(localInflight.streamId||'').trim();
  const serverId=String(serverLiveSnapshot.streamId||'').trim();
  const activeId=requestedActiveId||serverId;
  const selectDurableSnapshot=()=>{
    if(activeId&&localId===activeId&&Array.isArray(localInflight.todos)&&localInflight.todoStateMeta){
      return {...serverLiveSnapshot,todos:localInflight.todos,todoStateMeta:localInflight.todoStateMeta};
    }
    return serverLiveSnapshot;
  };
  if(requestedActiveId&&serverId&&serverId!==requestedActiveId){
    return localId===requestedActiveId?localInflight:null;
  }
  if(activeId&&localId!==activeId) return selectDurableSnapshot();

  const localSeq=Math.max(0,Number(localInflight.lastRunJournalSeq)||0);
  const serverSeq=Math.max(0,Number(serverLiveSnapshot.lastRunJournalSeq)||0);
  return serverSeq>=localSeq?selectDurableSnapshot():localInflight;
}

function _anchorActivitySceneStreamId(scene){
  if(!scene||typeof scene!=='object') return '';
  const identity=scene.identity&&typeof scene.identity==='object'?scene.identity:null;
  return String(scene.stream_id||scene.streamId||(identity&&(identity.stream_id||identity.streamId))||'').trim();
}

function _anchorActivitySceneMatchesStream(scene, activeStreamId){
  const activeId=String(activeStreamId||'').trim();
  if(!activeId) return true;
  const sceneId=_anchorActivitySceneStreamId(scene);
  return !sceneId||sceneId===activeId;
}

function _runtimeJournalAnchorActivitySceneForSession(sid, activeStreamId){
  const inflight=INFLIGHT&&sid?INFLIGHT[sid]:null;
  if(inflight&&inflight.anchorActivityScene&&inflight.anchorActivityScene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(inflight.anchorActivityScene, activeStreamId)){
    return inflight.anchorActivityScene;
  }
  const snapshot=S.session&&S.session.runtime_journal_snapshot;
  const scene=snapshot&&(snapshot.anchor_activity_scene||snapshot.anchorActivityScene);
  return scene&&scene.version==='activity_scene_v1'&&_anchorActivitySceneMatchesStream(scene, activeStreamId)?scene:null;
}

function _renderRuntimeJournalAnchorActivityScene(activeStreamId, sid){
  if(!activeStreamId||typeof window==='undefined'||typeof window._renderLiveAnchorActivitySceneSnapshotForStream!=='function') return false;
  const scene=_runtimeJournalAnchorActivitySceneForSession(sid, activeStreamId);
  if(!scene) return false;
  return !!window._renderLiveAnchorActivitySceneSnapshotForStream(activeStreamId, scene, sid);
}

function _rememberRenderedSessionSnapshot(s) {
  if (!s || !s.session_id) return;
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s);
  const previous = _sessionListSnapshotById.get(s.session_id);
  if (previous) return;
  _sessionListSnapshotById.set(s.session_id, {
    message_count: Number(s.message_count || 0),
    last_message_at: Number(s.last_message_at || 0),
  });
}

function _markSessionCompletedInList(session, previousSid = null) {
  if (!session || !Array.isArray(sidebarStateBindings._allSessions)) return;
  const finalSid = session.session_id || previousSid;
  if (!finalSid) return;
  const finalIdx = sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === finalSid);
  const previousIdx = previousSid ? sidebarStateBindings._allSessions.findIndex(s => s && s.session_id === previousSid) : -1;
  const idx = finalIdx >= 0 ? finalIdx : previousIdx;
  if (idx < 0) return;
  const {messages: _messages, tool_calls: _toolCalls, ...sessionMeta} = session;
  const messageCount = Number(
    session.message_count != null
      ? session.message_count
      : (Array.isArray(session.messages) ? session.messages.length : (sidebarStateBindings._allSessions[idx].message_count || 0))
  );
  const lastMessageAt = Number(session.last_message_at || session.updated_at || sidebarStateBindings._allSessions[idx].last_message_at || 0);
  sidebarStateBindings._allSessions[idx] = {
    ...sidebarStateBindings._allSessions[idx],
    ...sessionMeta,
    session_id: finalSid,
    message_count: messageCount,
    last_message_at: lastMessageAt,
    active_stream_id: null,
    pending_user_message: null,
    pending_started_at: null,
    is_streaming: false,
  };
  if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(sidebarStateBindings._allSessions[idx], finalSid);
  _sessionStreamingById.set(finalSid, false);
  _forgetObservedStreamingSession(finalSid);
  if (previousSid && previousSid !== finalSid) {
    for (let i = sidebarStateBindings._allSessions.length - 1; i >= 0; i--) {
      if (i !== idx && sidebarStateBindings._allSessions[i] && sidebarStateBindings._allSessions[i].session_id === previousSid) {
        sidebarStateBindings._allSessions.splice(i, 1);
      }
    }
    _sessionStreamingById.delete(previousSid);
    _forgetObservedStreamingSession(previousSid);
    _sessionListSnapshotById.delete(previousSid);
    _sessionListSourceById.delete(previousSid);
  }
  _sessionListSnapshotById.set(finalSid, {
    message_count: messageCount,
    last_message_at: lastMessageAt,
  });
  renderSessionListFromCache();
}

function _markPollingCompletionUnreadTransitions(sessions) {
  if (!Array.isArray(sessions)) return;
  const seen = new Set();
  const sourceById = typeof _sessionListSourceById !== 'undefined'
    && _sessionListSourceById
    && typeof _sessionListSourceById.get === 'function'
    && typeof _sessionListSourceById.keys === 'function'
    && typeof _sessionListSourceById.delete === 'function'
    ? _sessionListSourceById
    : new Map();
  const currentSidebarSource = typeof sidebarStateBindings._allSessionsScope !== 'undefined'
    && sidebarStateBindings._allSessionsScope
    && typeof sidebarStateBindings._allSessionsScope.sidebarSource === 'string'
    ? sidebarStateBindings._allSessionsScope.sidebarSource
    : null;
  for (const s of sessions) {
    if (!s || !s.session_id) continue;
    const sid = s.session_id;
    seen.add(sid);
    if (typeof _rememberSessionListSource === 'function') _rememberSessionListSource(s, sid);
    const wasStreaming = _sessionStreamingById.get(sid);
    const isStreaming = _isSessionEffectivelyStreaming(s);
    const previousSnapshot = _sessionListSnapshotById.get(sid);
    const observedStreaming = _getSessionObservedStreaming()[sid];
    const messageCount = Number(s.message_count || 0);
    const lastMessageAt = Number(s.last_message_at || 0);
    const hasServerRunSignal=Boolean(s.is_streaming||_hasPendingUserMessageSignal(s));
    const canMarkCompletedStream=Boolean(hasServerRunSignal||previousSnapshot||observedStreaming);
    const completedObservedStream = canMarkCompletedStream&&wasStreaming === true && !isStreaming;
    const completedWithNewMessages = Boolean(
      (previousSnapshot || observedStreaming)
      && !isStreaming
      && (
        messageCount > Number((previousSnapshot || observedStreaming).message_count || 0)
        || lastMessageAt > Number((previousSnapshot || observedStreaming).last_message_at || 0)
      )
    );
    const completedPersistedObservedStream = Boolean(observedStreaming && !isStreaming);
    if (completedObservedStream || completedPersistedObservedStream || completedWithNewMessages) {
      if (!_isSessionActivelyViewedForList(sid)) {
        // Tag cron session-list markers with source+profile so profile-switch
        // reset can clear only inactive-profile cron dots (#5960 / #5975 re-gate).
        const meta = (typeof _cronCompletionUnreadMetaForSession === 'function')
          ? _cronCompletionUnreadMetaForSession(s)
          : null;
        // Defense: never re-create a cron unread for a non-active profile while
        // the sidebar is single-profile (stale pre-switch payloads).
        const allProfilesOn = (typeof sidebarStateBindings._showAllProfiles !== 'undefined' && !!sidebarStateBindings._showAllProfiles);
        if (
          meta
          && meta.source === 'cron'
          && meta.profile
          && !allProfilesOn
          && typeof _cronMarkerProfileMatchesActive === 'function'
          && !_cronMarkerProfileMatchesActive(meta.profile, (typeof S !== 'undefined' && S && S.activeProfile) || 'default')
        ) {
          // Skip mark for inactive-profile cron row.
        } else {
          _markSessionCompletionUnread(sid, s.message_count, meta);
        }
      } else {
        // Sync viewed count so we don't flag stale unread on tab switch (#3020)
        _setSessionViewedCount(sid, messageCount);
      }
    }
    _sessionStreamingById.set(sid, isStreaming);
    if (isStreaming) {
      _rememberObservedStreamingSession(s);
    } else {
      _forgetObservedStreamingSession(sid);
    }
    _sessionListSnapshotById.set(sid, {
      message_count: messageCount,
      last_message_at: lastMessageAt,
    });
  }
  const staleRuntimeStateSids = new Set([
    ...Array.from(_sessionStreamingById.keys()),
    ...Array.from(_sessionListSnapshotById.keys()),
    ...Array.from(sourceById.keys()),
  ]);
  for (const sid of staleRuntimeStateSids) {
    if (seen.has(sid)) continue;
    const knownSource = sourceById.get(sid);
    if (currentSidebarSource && (!knownSource || knownSource !== currentSidebarSource)) continue;
    _sessionStreamingById.delete(sid);
    _sessionListSnapshotById.delete(sid);
    sourceById.delete(sid);
  }
}

export const sessionState=Object.freeze({saveDraft:_saveComposerDraft,saveDraftNow:_saveComposerDraftNow,restoreDraft:_restoreComposerDraft,clearDraft:_clearComposerDraft,acknowledgeVisit:_acknowledgeSessionVisit,hasUnread:_hasUnreadForSession,selectLiveRecovery:_selectLiveRecoveryInflight,markCompleted:_markSessionCompletedInList});

export { ICONS, SESSION_ARCHIVE_SWIPE_THRESHOLD_PX, SESSION_DELETE_SWIPE_THRESHOLD_PX, SESSION_LIST_FLIP_TIMEOUT_MS, SESSION_LIST_INTERACTION_IDLE_MS, SESSION_LONG_PRESS_DELAY_MS, SESSION_REFLOW_TIMEOUT_MS, SESSION_SWIPE_CANCEL_RATIO, SESSION_SWIPE_DURATION_MS, SESSION_SWIPE_REFLOW_LEAD_MS, _SESSION_LIST_BOOT_TIMEOUT_MS, _acknowledgeSessionVisit, _clearComposerDraft, _clearCronSessionCompletionUnreadForInactiveProfiles, _clearSessionCompletionUnread, _clearSessionViewedCount, _forgetObservedStreamingSession, _formatSessionModelWithGateway, _hasUnreadForSession, _inflightHasVisibleLiveState, _isServerIdleSessionRow, _isSessionActivelyViewedForList, _isSessionEffectivelyStreaming, _isSessionLocallyStreaming, _knownSessionProfileCount, _manualTitleRegenerateTimeoutMs, _markPollingCompletionUnreadTransitions, _markSessionCompletedInList, _markSessionCompletionUnread, _markSessionCompletionUnreadIfBackground, _profileMatchesActiveProfile, _purgeStaleInflightEntries, _reconcileActiveSessionIdleStateFromList, _recordSessionProfileCount, _rememberNewChatDraftSession, _rememberRenderedSessionSnapshot, _rememberRenderedStreamingState, _rememberSessionListSource, _renderRuntimeJournalAnchorActivityScene, _restoreComposerDraft, _restoreRememberedNewChatDraftSession, _saveComposerDraft, _saveComposerDraftNow, _selectLiveRecoveryInflight, _serverLiveSnapshotInflight, _sessionEventProfilesMatch, _sessionStreamingById, _sessionVisitHasUnreadState, _setSessionViewedCount };

export const sessionStateBindings=Object.freeze({
  get _loadSessionGeneration(){ return _loadSessionGeneration; },
  set _loadSessionGeneration(value){ _loadSessionGeneration=value; },
  get _loadingSessionId(){ return _loadingSessionId; },
  set _loadingSessionId(value){ _loadingSessionId=value; },
  get _pendingCarryForwardSnapshot(){ return _pendingCarryForwardSnapshot; },
  set _pendingCarryForwardSnapshot(value){ _pendingCarryForwardSnapshot=value; },
  get _pendingSessionListApplyTimer(){ return _pendingSessionListApplyTimer; },
  set _pendingSessionListApplyTimer(value){ _pendingSessionListApplyTimer=value; },
  get _pendingSessionListPayload(){ return _pendingSessionListPayload; },
  set _pendingSessionListPayload(value){ _pendingSessionListPayload=value; },
  get _sessionListHasLoadedOnce(){ return _sessionListHasLoadedOnce; },
  set _sessionListHasLoadedOnce(value){ _sessionListHasLoadedOnce=value; },
  get _sessionListLastScrollAt(){ return _sessionListLastScrollAt; },
  set _sessionListLastScrollAt(value){ _sessionListLastScrollAt=value; },
  get _sessionListLoadError(){ return _sessionListLoadError; },
  set _sessionListLoadError(value){ _sessionListLoadError=value; },
  get _sessionListPointerActive(){ return _sessionListPointerActive; },
  set _sessionListPointerActive(value){ _sessionListPointerActive=value; },
});
