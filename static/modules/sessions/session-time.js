import { _isSessionEffectivelyStreaming } from './session-run-state.js';

let _serverTimeDelta = 0;
let _serverTz = '';

function _sessionTimestampMs(session) {
  const raw = Number(session && (session._sidebar_activity_at || session.last_message_at || session.updated_at || session.created_at || 0));
  return Number.isFinite(raw) ? raw * 1000 : 0;
}

function _sessionSortTimestampMs(session) {
  const base = _sessionTimestampMs(session);
  const pending = Number(session && session.pending_started_at);
  const pendingMs = Number.isFinite(pending) ? pending * 1000 : 0;
  return Math.max(base, pendingMs);
}

function _sessionRunningSortRank(session) {
  if(_isSessionEffectivelyStreaming(session)) return 1;
  return session && session.active_stream_id && session.has_pending_user_message ? 1 : 0;
}

function _sessionSidebarSortCompare(a, b) {
  const activeDelta = _sessionRunningSortRank(b) - _sessionRunningSortRank(a);
  if(activeDelta) return activeDelta;
  return _sessionSortTimestampMs(b) - _sessionSortTimestampMs(a);
}

function _serverNowMs() {
  return Date.now() - _serverTimeDelta;
}

function _serverTzOptions() {
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') return undefined;
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m || m[3] !== '00') return undefined;
  const sign = m[1] === '+' ? '-' : '+';
  return { timeZone: `Etc/GMT${sign}${parseInt(m[2])}` };
}

function _formatInServerTz(date, options) {
  if (!_serverTz || _serverTz === '+0000' || _serverTz === '-0000') {
    return date.toLocaleString(undefined, options);
  }
  const m = _serverTz.match(/^([+-])(\d{2})(\d{2})$/);
  if (!m) return date.toLocaleString(undefined, options);
  const sign = m[1] === '+' ? 1 : -1;
  const offsetMin = sign * (parseInt(m[2]) * 60 + parseInt(m[3]));
  const adjusted = new Date(date.getTime() + offsetMin * 60 * 1000);
  return adjusted.toLocaleString(undefined, { ...options, timeZone: 'UTC' });
}

function _localDayOrdinal(timestampMs) {
  const date = new Date(timestampMs);
  return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
}

function _sessionCalendarBoundaries(nowMs) {
  nowMs = nowMs || _serverNowMs();
  const now = new Date(nowMs);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  startOfWeek.setDate(startOfWeek.getDate() - ((startOfWeek.getDay() + 6) % 7));
  const startOfLastWeek = new Date(startOfWeek);
  startOfLastWeek.setDate(startOfLastWeek.getDate() - 7);
  return {
    startOfToday: startOfToday.getTime(),
    startOfYesterday: startOfYesterday.getTime(),
    startOfWeek: startOfWeek.getTime(),
    startOfLastWeek: startOfLastWeek.getTime(),
  };
}

function _formatSessionDate(timestampMs, nowMs) {
  nowMs = nowMs || _serverNowMs();
  const date = new Date(timestampMs);
  const now = new Date(nowMs);
  const options = {month:'short', day:'numeric'};
  if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function _formatRelativeSessionTime(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_unknown');
  nowMs = nowMs || _serverNowMs();
  const diffMs = Math.max(0, nowMs - timestampMs);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  const dayDiff = Math.max(0, _localDayOrdinal(nowMs) - _localDayOrdinal(timestampMs));
  if (timestampMs >= startOfToday) {
    if (diffMs < minute) return t('session_time_minutes_ago', 1);
    if (diffMs < hour) return t('session_time_minutes_ago', Math.floor(diffMs / minute));
    return t('session_time_hours_ago', Math.floor(diffMs / hour));
  }
  if (timestampMs >= startOfYesterday) return t('session_time_days_ago', 1);
  if (timestampMs >= startOfWeek) return t('session_time_days_ago', dayDiff);
  if (timestampMs >= startOfLastWeek) return t('session_time_last_week');
  return _formatSessionDate(timestampMs, nowMs);
}

function _sessionTimeBucketLabel(timestampMs, nowMs) {
  if (!timestampMs) return t('session_time_bucket_older');
  nowMs = nowMs || _serverNowMs();
  const {startOfToday, startOfYesterday, startOfWeek, startOfLastWeek} = _sessionCalendarBoundaries(nowMs);
  if (timestampMs >= startOfToday) return t('session_time_bucket_today');
  if (timestampMs >= startOfYesterday) return t('session_time_bucket_yesterday');
  if (timestampMs >= startOfWeek) return t('session_time_bucket_this_week');
  if (timestampMs >= startOfLastWeek) return t('session_time_bucket_last_week');
  return t('session_time_bucket_older');
}

const sessionTimeBindings={};
Object.defineProperties(sessionTimeBindings,{
  _serverTimeDelta:{enumerable:true,get:()=>_serverTimeDelta,set:value=>{_serverTimeDelta=value;}},
  _serverTz:{enumerable:true,get:()=>_serverTz,set:value=>{_serverTz=value;}},
});
Object.freeze(sessionTimeBindings);

export {
  _formatInServerTz,
  _formatRelativeSessionTime,
  _serverNowMs,
  _serverTzOptions,
  _sessionSidebarSortCompare,
  _sessionSortTimestampMs,
  _sessionTimeBucketLabel,
  _sessionTimestampMs,
  sessionTimeBindings,
};
