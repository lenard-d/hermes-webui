import { loadSession } from './lifecycle.js';
import { _profileMatchesActiveProfile, sessionStateStoreBindings } from './session-state-store.js';

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
  // sessionStateStoreBindings._loadingSessionId, a newer switch already began.
  if (targetSid && sessionStateStoreBindings._loadingSessionId !== null && sessionStateStoreBindings._loadingSessionId !== targetSid) return;
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


export const composerDrafts=Object.freeze({
  save:_saveComposerDraft,
  saveNow:_saveComposerDraftNow,
  restore:_restoreComposerDraft,
  clear:_clearComposerDraft,
  rememberNewChat:_rememberNewChatDraftSession,
  restoreRememberedNewChat:_restoreRememberedNewChatDraftSession,
});

export { _clearComposerDraft, _rememberNewChatDraftSession, _restoreComposerDraft, _restoreRememberedNewChatDraftSession, _saveComposerDraft, _saveComposerDraftNow };
