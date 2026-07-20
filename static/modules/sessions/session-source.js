const _MESSAGING_RAW_SOURCES = new Set(['weixin', 'telegram', 'discord', 'slack', 'email', 'wecom', 'wecom_callback']);
const _MESSAGING_SOURCE_LABELS = Object.freeze({
  weixin: 'WeChat',
  telegram: 'Telegram',
  discord: 'Discord',
  slack: 'Slack',
  email: 'Email',
  wecom: 'WeCom',
  wecom_callback: 'WeCom Callback',
});

export function _isMessagingSession(session) {
  if (!session) return false;
  if (session.session_source === 'messaging') return true;
  const raw = String(session.raw_source || session.source_tag || session.source || '').toLowerCase();
  return _MESSAGING_RAW_SOURCES.has(raw);
}

/** Return true only for sessions whose normalized origin is the WebUI. */
export function _isWebUiSourceSession(session) {
  if (!session) return false;
  const source = String(
    session.session_source || session.raw_source || session.source_tag || session.source || ''
  ).toLowerCase();
  return source === 'webui';
}

export function _isExternalSession(session) {
  if (!session || _isWebUiSourceSession(session)) return false;
  return !!(session.is_cli_session || _isMessagingSession(session));
}

export function _sourceKeyForSession(session) {
  return String(session && (session.raw_source || session.source_tag || session.source || '') || '').toLowerCase();
}

export function _isCliSession(session) {
  if (!session) return false;
  if (session.session_source === 'cli') return true;
  const raw = String(
    session.raw_source || session.source_tag || session.source || session.source_label || ''
  ).toLowerCase();
  if (raw === 'cli' || raw === 'tui' || raw === 'acp') return true;
  if (_isMessagingSession(session)) return false;
  return session.is_cli_session === true;
}

export function _getChannelLabel(session) {
  if (!session) return '';
  if (session.source_label) return session.source_label;
  const raw = _sourceKeyForSession(session);
  return _MESSAGING_SOURCE_LABELS[raw] || raw || '';
}

export const sessionSources=Object.freeze({
  isCli:_isCliSession,
  isExternal:_isExternalSession,
  isMessaging:_isMessagingSession,
  isWebUi:_isWebUiSourceSession,
  key:_sourceKeyForSession,
  channelLabel:_getChannelLabel,
});
