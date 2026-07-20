import { _sourceKeyForSession } from './session-source.js';
import { sessionLoadState } from './session-load-state.js';
import { _profileMatchesActiveProfile } from './session-profile-scope.js';
import { sessionRunRegistry } from './session-run-registry.js';
import { sidebarStateBindings } from './sidebar-store.js';

const _sessionListSnapshotById=sessionRunRegistry.snapshotById;
const _sessionStreamingById=sessionRunRegistry.streamingById;

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
    return false;
  }
  _markSessionCompletionUnread(sid, count, meta);
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
  if (sessionLoadState.loadingSessionId && sessionLoadState.loadingSessionId !== sid) return false;
  if (typeof document !== 'undefined' && document.visibilityState && document.visibilityState !== 'visible') return false;
  if (typeof document !== 'undefined' && typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
  return true;
}


export const sessionUnread=Object.freeze({
  hasUnread:_hasUnreadForSession,
  markCompletion:_markSessionCompletionUnread,
  markBackgroundCompletion:_markSessionCompletionUnreadIfBackground,
  clearCompletion:_clearSessionCompletionUnread,
});

export {
  _clearCronSessionCompletionUnreadForInactiveProfiles,
  _clearSessionCompletionUnread,
  _clearSessionViewedCount,
  _cronCompletionUnreadMetaForSession,
  _cronMarkerProfileMatchesActive,
  _forgetObservedStreamingSession,
  _getSessionObservedStreaming,
  _hasSessionCompletionUnread,
  _hasUnreadForSession,
  _isSessionActivelyViewedForList,
  _knownSessionProfileCount,
  _markSessionCompletionUnread,
  _markSessionCompletionUnreadIfBackground,
  _recordSessionProfileCount,
  _rememberObservedStreamingSession,
  _sessionVisitHasUnreadState,
  _setSessionViewedCount,
};
