import { _clarifyPendingBySession } from './clarify.js';

export function transcript(){
  const lines=[`# Hermes session ${S.session?.session_id||''}`,``,
    `Workspace: ${S.session?.workspace||''}`,`Model: ${S.session?.model||''}`,``];
  for(const m of S.messages){
    if(!m||m.role==='tool')continue;
    let c=m.content||'';
    if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('\n');
    const ct=String(c).trim();
    if(!ct&&!m.attachments?.length)continue;
    const attach=m.attachments?.length?`\n\n_Files: ${m.attachments.join(', ')}_`:'';
    lines.push(`## ${m.role}`,'',ct+attach,'');
  }
  return lines.join('\n');
}

let _composerAutoResizeRaf=0;
export function autoResize(){
  if(_composerAutoResizeRaf && typeof cancelAnimationFrame==='function'){
    cancelAnimationFrame(_composerAutoResizeRaf);
    _composerAutoResizeRaf=0;
  }
  const el=$('msg');
  const _prevComposerH=el.offsetHeight;
  // #5514: autoResize() momentarily sets the textarea to height:'auto' (collapses
  // a multi-row composer toward its 1-row min) before reading scrollHeight and
  // restoring the measured height. That transient collapse GROWS the flex:1
  // #messages viewport, and reading scrollHeight forces a synchronous reflow — so
  // the browser CLAMPS a bottom-anchored scrollTop DOWN by the collapse delta and
  // does NOT restore it when the height snaps back. The reader is left stranded
  // Δpx above the bottom on EVERY keystroke while the composer is multi-row (Δ ∝
  // composer height), and the clamp's async scroll event also sticky-unpins the
  // reader (_messageUserUnpinned=true), dead-ending the grow-path re-pin below and
  // stream auto-follow until they manually scroll back. #5516's net-growth gate
  // never caught this because a steady-state keystroke has no NET height change.
  // Root-cause fix: snapshot the transcript's scrollTop BEFORE the height
  // round-trip and restore it AFTER, undoing the transient clamp within the same
  // synchronous task so the poisoning scroll event never fires. This protects
  // pinned readers AND near-bottom readers who scrolled up to re-read (their exact
  // position is preserved), takes no _programmaticScroll latch, and is inert to
  // iOS dynamic-toolbar reflows. A genuine NET grow/shrink still lands the reader
  // Δnet off-bottom; the grow-gated re-pin below (and the #composerWrap
  // ResizeObserver) then snap a still-pinned reader to the true bottom.
  const _msgs=$('messages');
  const _prevScrollTop=_msgs?_msgs.scrollTop:0;
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,200)+'px';
  if(_msgs&&_msgs.scrollTop!==_prevScrollTop) _msgs.scrollTop=_prevScrollTop;
  updateSendBtn();
  // Genuine NET growth (a new row that keeps the composer taller than before)
  // still shrinks the settled viewport, so a pinned reader must be re-pinned to
  // the true bottom. Guarded to fire only on real growth and only when genuinely
  // pinned (the helper no-ops for a scrolled-away reader). The #composerWrap
  // ResizeObserver is the safety net for growth paths that don't route here.
  if(el.offsetHeight>_prevComposerH && typeof _repinMessagesAfterComposerResize==='function') _repinMessagesAfterComposerResize();
}
export function scheduleComposerAutoResize(){
  if(typeof requestAnimationFrame!=='function'){autoResize();return;}
  if(_composerAutoResizeRaf) return;
  _composerAutoResizeRaf=requestAnimationFrame(()=>{
    _composerAutoResizeRaf=0;
    autoResize();
  });
}


// ── YOLO mode state ──
// Session-scoped; stored server-side in memory (tools/approval.py).
// Lifecycle:
//   • Page reload: state PERSISTS — _fetchYoloState() re-syncs from backend.
//   • Cross-tab: state is SHARED — enabling YOLO in Tab A affects Tab B for
//     the same session (both poll the same server-side flag).
//   • Server restart: state is LOST — in-memory only, not persisted to disk.
//   • Session switch: state resets — loadSession() clears _yoloEnabled and
//     fetches the new session's state.
let _yoloEnabled = false;

export async function _fetchYoloState(sid) {
  try {
    const data = await api('/api/session/yolo?session_id=' + encodeURIComponent(sid));
    _yoloEnabled = !!data.yolo_enabled;
    _updateYoloPill();
  } catch (_) { /* ignore */ }
}

export function _updateYoloPill() {
  const pill = $('yoloPill');
  if (!pill) return;
  pill.style.display = _yoloEnabled ? '' : 'none';
  if (_yoloEnabled) {
    pill.title = t('yolo_pill_title_active');
    pill.setAttribute('data-i18n-title', 'yolo_pill_title_active');
  }
  if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
}

export async function toggleYoloFromApproval() {
  const sid = S.session && S.session.session_id;
  if (!sid) return;
  try {
    await api('/api/session/yolo', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, enabled: true }),
    });
    _yoloEnabled = true;
    _updateYoloPill();
    hideApprovalCard(true);
    showToast(t('yolo_enabled'));
  } catch (e) { showToast('YOLO: ' + e.message); }
}

// ── Approval polling ──
let _approvalPollTimer = null;
let _approvalFallbackPollInFlight = false;
let _approvalHideTimer = null;
let _approvalVisibleSince = 0;
let _approvalSignature = '';
const APPROVAL_MIN_VISIBLE_MS = 30000;

// showApprovalCard moved above respondApproval

export function _setPromptFlyoutHidden(card, hidden) {
  if (!card) return;
  if (hidden) {
    card.setAttribute("aria-hidden", "true");
    card.setAttribute("inert", "");
    const markHidden = () => {
      if (!card.classList || !card.classList.contains("visible")) card.hidden = true;
    };
    if (typeof setTimeout === "function") setTimeout(markHidden, 450);
    else markHidden();
    return;
  }
  card.hidden = false;
  card.setAttribute("aria-hidden", "false");
  card.removeAttribute("inert");
  // Force the unhidden, pre-visible state to be observed so the existing
  // transform/opacity transition can still animate when `.visible` is added.
  void card.offsetHeight;
}

function _clearApprovalHideTimer() {
  if (_approvalHideTimer) {
    clearTimeout(_approvalHideTimer);
    _approvalHideTimer = null;
  }
}

function _resetApprovalCardState() {
  _clearApprovalHideTimer();
  _approvalVisibleSince = 0;
  _approvalSignature = '';
}

export function hideApprovalCard(force=false) {
  const card = $("approvalCard");
  if (!card) return;
  if (!force && _approvalVisibleSince) {
    const remaining = APPROVAL_MIN_VISIBLE_MS - (Date.now() - _approvalVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _approvalSignature;
      _clearApprovalHideTimer();
      _approvalHideTimer = setTimeout(() => {
        _approvalHideTimer = null;
        if (_approvalSignature !== scheduledSignature) return;
        hideApprovalCard(true);
      }, remaining);
      return;
    }
  }
  _approvalSessionId = null;
  _resetApprovalCardState();
  card.classList.remove("visible");
  card.classList.remove("collapsed");
  _setPromptFlyoutHidden(card, true);
  _syncApprovalTranscriptSpace(null);
  $("approvalCmd").textContent = "";
  $("approvalDesc").textContent = "";
}

// Track session_id of the active approval so respond goes to the right session
export let _approvalSessionId = null;
let _approvalCurrentId = null;  // approval_id of the card currently shown
let _approvalPendingBySession = new Map();
let _approvalResponding = null;

const _DISMISSED_APPROVALS_KEY = 'hermes_dismissed_approvals';

// Dismissed approvals are namespaced by session so that two sessions carrying
// the SAME approval_id (e.g. a gateway/run source that reuses externally
// supplied IDs across sessions) can't have a dismissal in one session hide the
// other's still-pending approval. Stored value is "<sid>\u0000<approval_id>".
function _approvalDismissKey(sid, approvalId) {
  if (!approvalId) return '';
  return String(sid || '') + '\u0000' + String(approvalId);
}

function _getDismissedApprovals() {
  try { return JSON.parse(localStorage.getItem(_DISMISSED_APPROVALS_KEY) || '[]'); }
  catch (_) { return []; }
}

function _isApprovalDismissed(sid, approvalId) {
  const key = _approvalDismissKey(sid, approvalId);
  if (!key) return false;
  return _getDismissedApprovals().includes(key);
}

function _markApprovalDismissed(sid, approvalId) {
  const key = _approvalDismissKey(sid, approvalId);
  if (!key) return;
  const set = _getDismissedApprovals().filter(k => k !== key);
  set.push(key);
  try { localStorage.setItem(_DISMISSED_APPROVALS_KEY, JSON.stringify(set.slice(-100))); }
  catch (_) {}
}

function _unmarkApprovalDismissed(sid, approvalId) {
  const key = _approvalDismissKey(sid, approvalId);
  if (!key) return;
  const set = _getDismissedApprovals().filter(k => k !== key);
  try { localStorage.setItem(_DISMISSED_APPROVALS_KEY, JSON.stringify(set)); }
  catch (_) {}
}

export function _promptActiveSessionId() {
  return (S.session && S.session.session_id) || null;
}

function _approvalPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

export function activeSessionHasPendingPromptAttention() {
  const sid = _promptActiveSessionId();
  return !!(sid && (
    _approvalPendingBySession.has(sid) ||
    _clarifyPendingBySession.has(sid)
  ));
}

function _rememberApprovalPending(pending, pendingCount) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _approvalPendingBySession.set(sid, {pending: nextPending, pendingCount: pendingCount || 1});
  return sid;
}

export function _clearApprovalPendingForSession(sid) {
  if (sid) {
    _approvalPendingBySession.delete(sid);
    if (typeof syncTopbar === 'function') syncTopbar();
  }
}

function _hideApprovalCardIfOwner(sid, force=false) {
  if (!sid || _approvalSessionId === sid) hideApprovalCard(force);
}

function _approvalPollingSessionMissingOrMismatched(sid) {
  return !sid || !S.session || S.session.session_id !== sid;
}

export function _renderPendingApprovalForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_approvalSessionId && _approvalSessionId !== sid) hideApprovalCard(true);
  const entry = _approvalPendingBySession.get(sid);
  if (entry) showApprovalCard(entry.pending, entry.pendingCount);
}

function _approvalResponseMatches(sid, approvalId) {
  return !!(
    _approvalResponding &&
    _approvalResponding.sid === sid &&
    (_approvalResponding.approvalId || null) === (approvalId || null)
  );
}

function _setApprovalControlsDisabled(choice, disabled) {
  ["approvalBtnOnce","approvalBtnSession","approvalBtnAlways","approvalBtnDeny"].forEach(id => {
    const b = $(id);
    if (!b) return;
    b.disabled = !!disabled;
    if (disabled && choice && b.id === "approvalBtn" + choice.charAt(0).toUpperCase() + choice.slice(1)) {
      b.classList.add("loading");
    } else {
      b.classList.remove("loading");
    }
  });
}

export function showApprovalForSession(sid, pending, pendingCount) {
  if (!pending) return;
  pending._session_id = sid;
  showApprovalCard(pending, pendingCount);
}

function showApprovalCard(pending, pendingCount) {
  const sid = _rememberApprovalPending(pending, pendingCount);
  if (!_approvalPromptBelongsToActiveSession(sid)) return;
  if (pending && pending.approval_id && _isApprovalDismissed(sid, pending.approval_id)) return;
  const keys = pending.pattern_keys || (pending.pattern_key ? [pending.pattern_key] : []);
  const desc = (pending.description || "") + (keys.length ? " [" + keys.join(", ") + "]" : "");
  const cmd = pending.command || "";
  const sig = JSON.stringify({desc, cmd, sid: pending._session_id || (S.session && S.session.session_id) || null, approval_id: pending.approval_id || null});
  const card = $("approvalCard");
  const sameApproval = card.classList.contains("visible") && _approvalSignature === sig;
  $("approvalDesc").textContent = desc;
  $("approvalCmd").textContent = cmd;
  _approvalSessionId = sid;
  _approvalCurrentId = pending.approval_id || null;
  _approvalSignature = sig;
  // Show "1 of N" counter when multiple approvals are queued
  const counter = $("approvalCounter");
  if (counter) {
    if (pendingCount && pendingCount > 1) {
      counter.textContent = "1 of " + pendingCount + " pending";
      counter.style.display = "";
    } else {
      counter.style.display = "none";
    }
  }
  if (!sameApproval) {
    _approvalVisibleSince = Date.now();
    _clearApprovalHideTimer();
    // A distinct approval must always render expanded — never inherit a prior
    // approval's collapsed state, which would hide its command + action buttons. (#3515)
    card.classList.remove("collapsed");
  }
  const responding = _approvalResponseMatches(sid, _approvalCurrentId);
  _setApprovalControlsDisabled(
    responding ? _approvalResponding.choice : null,
    responding,
  );
  _setPromptFlyoutHidden(card, false);
  card.classList.add("visible");
  _syncApprovalCollapseButton(card);
  _syncApprovalTranscriptSpace(card, {immediate: true});
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  const onceBtn = $("approvalBtnOnce");
  if (onceBtn && document.activeElement !== $('msg')) {
    setTimeout(() => onceBtn.focus({preventScroll: true}), 50);
  }
  if (typeof syncTopbar === 'function') syncTopbar();
}

export function dismissApprovalCard() {
  const sid = _approvalSessionId;
  if (_approvalCurrentId) _markApprovalDismissed(sid, _approvalCurrentId);
  hideApprovalCard(true);
  if (sid) _clearApprovalPendingForSession(sid);
}

function _syncApprovalCollapseButton(card) {
  const collapse = $("approvalCollapse");
  if (!collapse || !card) return;
  const collapsed = card.classList.contains("collapsed");
  collapse.setAttribute("aria-expanded", collapsed ? "false" : "true");
  // Icon swap: chevron-down when expanded (click to collapse), chevron-up when collapsed (click to expand)
  const polyline = collapse.querySelector("svg polyline");
  if (polyline) polyline.setAttribute("points", collapsed ? "18 15 12 9 6 15" : "6 9 12 15 18 9");
  const label = collapsed ? "Expand approval" : "Collapse approval";
  collapse.setAttribute("aria-label", label);
  collapse.title = label;
}

function _approvalMessagesNearBottom(messages) {
  if (!messages) return false;
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 150;
}

function _syncApprovalTranscriptSpace(card, opts) {
  opts = opts || {};
  const messages = $("messages");
  if (!messages) return;
  const wasNearBottom = _approvalMessagesNearBottom(messages);
  if (!card || !card.classList.contains("visible")) {
    messages.classList.remove("approval-open");
    messages.classList.remove("approval-collapsed");
    messages.style.removeProperty("--approval-card-height");
    messages.style.removeProperty("--approval-dock-height");
    if (wasNearBottom && typeof scrollToBottom === "function" && typeof requestAnimationFrame === "function") {
      requestAnimationFrame(scrollToBottom);
    }
    return;
  }
  const collapsed = card.classList.contains("collapsed");
  messages.classList.add("approval-open");
  messages.classList.toggle("approval-collapsed", collapsed);
  const measure = () => {
    if (!card.classList.contains("visible")) return;
    const target = collapsed ? card : (card.querySelector(".approval-inner") || card);
    const h = target && target.getBoundingClientRect().height;
    if (h > 0) {
      messages.style.setProperty(collapsed ? "--approval-dock-height" : "--approval-card-height", Math.ceil(h + 24) + "px");
    }
    if (wasNearBottom && typeof scrollToBottom === "function") scrollToBottom();
  };
  if (opts.immediate) measure();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(measure);
  setTimeout(measure, 420);
}

function _restoreFailedApprovalResponse(sid, errMsg) {
  _approvalResponding = null;
  _setApprovalControlsDisabled(null, false);
  if (_approvalPromptBelongsToActiveSession(sid)) _renderPendingApprovalForActiveSession();
  if (typeof showToast === "function") showToast(errMsg, 5000);
  if (typeof setStatus === "function") setStatus(errMsg);
}

export function toggleApprovalCardCollapsed(forceCollapsed) {
  const card = $("approvalCard");
  if (!card) return;
  const collapsed = typeof forceCollapsed === "boolean" ? forceCollapsed : !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  _syncApprovalCollapseButton(card);
  _syncApprovalTranscriptSpace(card, {immediate: true});
}

export async function respondApproval(choice) {
  const sid = _approvalSessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const approvalId = _approvalCurrentId;
  if (_approvalResponseMatches(sid, approvalId)) return;
  _unmarkApprovalDismissed(sid, approvalId);
  _approvalResponding = {sid, approvalId: approvalId || null, choice};
  _setApprovalControlsDisabled(choice, true);
  try {
    const result = await api("/api/approval/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, choice, approval_id: approvalId })
    });
    if (result && result.ok) {
      _approvalResponding = null;
      const pendingEntry = _approvalPendingBySession.get(sid);
      const samePending = !!(pendingEntry && pendingEntry.pending && (pendingEntry.pending.approval_id || null) === (approvalId || null));
      // `stale_cleared` means the server found nothing pending for this session
      // (the approval already resolved or its stream ended while the card was
      // up). The orphan card must be cleared unconditionally so it can never
      // get stuck — even if the displayed id has since drifted. (#4948 local
      // variant: previously surfaced as a stuck "Approval response not
      // accepted." toast.)
      if (result.stale_cleared || (_approvalSessionId === sid && _approvalCurrentId === approvalId)) {
        _approvalSessionId = null;
        _approvalCurrentId = null;
        hideApprovalCard(true);
      }
      if (samePending || result.stale_cleared) _clearApprovalPendingForSession(sid);
      // Hardening for the narrow stale-clear race: a brand-new approval could
      // have been parked server-side after the server's empty-check but before
      // we processed this stale response. The unconditional clear above would
      // hide that fresh card. Re-query the authoritative server pending state
      // (same endpoint the fallback poll uses) so any approval that arrived in
      // the window re-surfaces immediately instead of waiting for the next
      // SSE/poll tick. Best-effort; poll/SSE remain the backstop. (Opus review
      // nit on the #4948 fix.)
      if (result.stale_cleared) {
        api("/api/approval/pending?session_id=" + encodeURIComponent(sid), {timeoutToast: false})
          .then(data => {
            if (data && data.pending && _approvalPromptBelongsToActiveSession(sid)) {
              showApprovalForSession(sid, data.pending, data.pending_count || 1);
            }
          })
          .catch(() => {});
      }
      return;
    }
    const errMsg = (result && result.error) || "Approval response not accepted.";
    _restoreFailedApprovalResponse(sid, errMsg);
  } catch(e) {
    const errMsg = (e && e.message) || (t("approval_responding") + " failed");
    _restoreFailedApprovalResponse(sid, errMsg);
  }
}

export function startApprovalPolling(sid) {
  stopApprovalPolling();
  _approvalPollingSessionId = sid || null;

  // Use HTTP polling instead of SSE to avoid browser connection pool exhaustion.
  // Browsers limit to 6 concurrent HTTP connections per origin over HTTP/1.1.
  // With 6 persistent SSE streams (sessions/events, gateway/stream,
  // session/stream, approval/stream, clarify/stream, chat/stream), the pool
  // fills and all fetch() requests queue indefinitely. The server responds
  // normally (curl works), but the browser has no available sockets.
  //
  // This was introduced in v0.51.340 when /api/session/stream was added as
  // the 6th persistent SSE connection. Until we multiplex streams or serve
  // SSE from a separate origin, use HTTP polling to free 2 connection slots.
  // (1.5-second interval, acceptable tradeoff)
  _startApprovalFallbackPoll(sid);
}

let _approvalEventSource = null;
let _approvalSSEHealthTimer = null;
let _approvalPollingSessionId = null;

function _startApprovalFallbackPoll(sid) {
  // Run one tick immediately so a session already blocked on a pending approval
  // shows its card instantly (the removed SSE 'initial' event used to do this);
  // then poll on the 1500ms cadence. (#3913 SHOULD-FIX)
  const _tick = async () => {
    if (_approvalPollingSessionMissingOrMismatched(sid)) {
      stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true); return;
    }
    if (_approvalFallbackPollInFlight) return;
    _approvalFallbackPollInFlight = true;
    try {
      const data = await api("/api/approval/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false});
      if (data.pending) { showApprovalForSession(sid, data.pending, data.pending_count||1); }
      else if (!_approvalPollingSessionMissingOrMismatched(sid)) {
        const _resolvedEntry = _approvalPendingBySession.get(sid);
        _clearApprovalPendingForSession(sid);
        const _resolvedId = _resolvedEntry && _resolvedEntry.pending && _resolvedEntry.pending.approval_id;
        if (_resolvedId) _unmarkApprovalDismissed(sid, _resolvedId);
        _hideApprovalCardIfOwner(sid);
        if (!S.busy) {
          stopApprovalPollingForSession(sid);
        }
      }
    } catch(e) { /* ignore poll errors */ }
    finally { _approvalFallbackPollInFlight = false; }
  };
  _approvalPollTimer = setInterval(_tick, 1500);  // matches the v0.50.247 polling cadence so degraded-mode users see the same responsiveness
  _tick();
}

export function stopApprovalPollingForSession(sid) {
  if(sid && _approvalPollingSessionId && _approvalPollingSessionId!==sid) return;
  stopApprovalPolling();
}

export function stopApprovalPolling() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
  if (_approvalEventSource) { try { if(_approvalEventSource.readyState!==2)_approvalEventSource.close(); } catch(_){} _approvalEventSource = null; }
  if (_approvalSSEHealthTimer) { clearInterval(_approvalSSEHealthTimer); _approvalSSEHealthTimer = null; }
  _approvalFallbackPollInFlight = false;
  _approvalPollingSessionId = null;
}
