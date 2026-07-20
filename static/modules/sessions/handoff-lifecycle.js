import { _getChannelLabel } from './session-source.js';
import { _clearSessionCompletionUnread, _clearSessionViewedCount, _forgetObservedStreamingSession } from './session-unread.js';

const _HANDOFF_THRESHOLD = 10;
const _HANDOFF_STORAGE_PREFIX = 'handoff:';
const _HANDOFF_SUFFIX_DISMISSED_AT = 'dismissed_at';
const _HANDOFF_SUFFIX_SUMMARY_HANDLED_AT = 'summary_handled_at';

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

export const handoffLifecycle=Object.freeze({check:_checkAndShowHandoffHint,dismiss:_dismissHandoffHint,hide:_hideHandoffHint,clearSession:_clearHandoffStorageForSession});

export { _checkAndShowHandoffHint, _clearHandoffStorageForSession, _dismissHandoffHint, _hideHandoffHint };
