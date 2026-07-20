var HermesMessages = globalThis.HermesMessages || Object.create(null);
globalThis.HermesMessages = HermesMessages;

// ── Clarify polling ──
let _clarifyPollTimer = null;
let _clarifyHideTimer = null;
let _clarifyVisibleSince = 0;
let _clarifySignature = '';
let _clarifySessionId = null;
let _clarifyId = null;
let _clarifyMissingEndpointWarned = false;
let _clarifyCountdownTimer = null;
let _clarifyExpiresAt = 0;
let _clarifyPendingBySession = new Map();
const CLARIFY_MIN_VISIBLE_MS = 30000;

function _clarifyPromptBelongsToActiveSession(sid) {
  return !!(sid && _promptActiveSessionId() === sid);
}

function _rememberClarifyPending(pending) {
  if (!pending) return null;
  const sid = pending._session_id || _promptActiveSessionId();
  if (!sid) return null;
  const nextPending = {...pending, _session_id: sid};
  _clarifyPendingBySession.set(sid, {pending: nextPending});
  return sid;
}

function _clearClarifyPendingForSession(sid) {
  if (sid) {
    _clarifyPendingBySession.delete(sid);
    if (typeof syncTopbar === 'function') syncTopbar();
  }
}

function _hideClarifyCardIfOwner(sid, force=false, reason="dismissed") {
  if (!sid || _clarifySessionId === sid) hideClarifyCard(force, reason);
}

function _renderPendingClarifyForActiveSession() {
  const sid = _promptActiveSessionId();
  if (!sid) return;
  if (_clarifySessionId && _clarifySessionId !== sid) hideClarifyCard(true, 'session');
  const entry = _clarifyPendingBySession.get(sid);
  if (entry) showClarifyCard(entry.pending);
}

function showClarifyForSession(sid, pending) {
  if (!pending) return;
  pending._session_id = sid;
  showClarifyCard(pending);
}

function _renderPendingPromptsForActiveSession() {
  const sid = _promptActiveSessionId();
  _renderPendingApprovalForActiveSession();
  _renderPendingClarifyForActiveSession();
  if (
    sid &&
    typeof activeSessionHasPendingPromptAttention === 'function' &&
    activeSessionHasPendingPromptAttention()
  ) return;
  if (typeof syncTopbar === 'function') syncTopbar();
}

function _ensureClarifyCardDom() {
  let card = $("clarifyCard");
  if (card) return card;
  const host = $("msgInner") || $("messages");
  if (!host) return null;
  card = document.createElement("div");
  card.className = "clarify-card";
  card.id = "clarifyCard";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-labelledby", "clarifyHeading");
  card.setAttribute("aria-describedby", "clarifyQuestion clarifyHint");
  _setPromptFlyoutHidden(card, true);
  card.innerHTML = `
    <div class="clarify-inner">
      <div class="clarify-header">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 17h.01"/><path d="M9.09 9a3 3 0 1 1 5.82 1c0 2-3 2-3 4"/><circle cx="12" cy="12" r="10"/></svg>
        <span id="clarifyHeading" data-i18n="clarify_heading">Clarification needed</span>
        <span class="clarify-countdown" id="clarifyCountdown"></span>
        <button type="button" class="clarify-collapse" id="clarifyCollapse" aria-expanded="true" aria-label="Collapse clarification" aria-controls="clarifyQuestion clarifyChoices clarifyInput clarifyHint" onclick="toggleClarifyCardCollapsed()" title="Collapse clarification"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg></button>
      </div>
      <div class="clarify-question" id="clarifyQuestion"></div>
      <div class="clarify-choices" id="clarifyChoices"></div>
      <div class="clarify-response">
        <input class="clarify-input" id="clarifyInput" type="text" autocomplete="off" readonly onfocus="this.removeAttribute('readonly')" data-i18n-placeholder="clarify_input_placeholder" placeholder="Type your response…">
        <button class="clarify-submit" id="clarifySubmit" data-i18n="clarify_send">Send</button>
      </div>
      <div class="clarify-hint" id="clarifyHint" data-i18n="clarify_hint">Please choose one option, or type your own response below.</div>
    </div>
  `;
  host.appendChild(card);
  const submit = $("clarifySubmit");
  if (submit) submit.onclick = () => respondClarify();
  const collapse = $("clarifyCollapse");
  if (collapse) collapse.onclick = () => toggleClarifyCardCollapsed();
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  return card;
}

function _syncClarifyCollapseButton(card) {
  const collapse = $("clarifyCollapse");
  if (!collapse || !card) return;
  const collapsed = card.classList.contains("collapsed");
  collapse.setAttribute("aria-expanded", collapsed ? "false" : "true");
  // Icon swap: chevron-down when expanded (click to collapse), chevron-up when collapsed (click to expand)
  const polyline = collapse.querySelector("svg polyline");
  if (polyline) polyline.setAttribute("points", collapsed ? "18 15 12 9 6 15" : "6 9 12 15 18 9");
  const label = collapsed ? "Expand clarification" : "Collapse clarification";
  collapse.setAttribute("aria-label", label);
  collapse.title = label;
}

let _clarifyResizeListenerReady = false;

function _clarifyMessagesNearBottom(messages) {
  if (!messages) return false;
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 150;
}

function _syncClarifyTranscriptSpace(card, opts) {
  opts = opts || {};
  const messages = $("messages");
  if (!messages) return;
  const wasNearBottom = _clarifyMessagesNearBottom(messages);
  if (!card || !card.classList.contains("visible")) {
    messages.classList.remove("clarify-open");
    messages.classList.remove("clarify-collapsed");
    messages.style.removeProperty("--clarify-card-height");
    messages.style.removeProperty("--clarify-dock-height");
    if (wasNearBottom && typeof scrollToBottom === "function" && typeof requestAnimationFrame === "function") {
      requestAnimationFrame(scrollToBottom);
    }
    return;
  }
  const collapsed = card.classList.contains("collapsed");
  messages.classList.add("clarify-open");
  messages.classList.toggle("clarify-collapsed", collapsed);
  const measure = () => {
    if (!card.classList.contains("visible")) return;
    const target = collapsed ? card : (card.querySelector(".clarify-inner") || card);
    const h = target && target.getBoundingClientRect().height;
    if (h > 0) {
      messages.style.setProperty(collapsed ? "--clarify-dock-height" : "--clarify-card-height", Math.ceil(h + 24) + "px");
    }
    if (wasNearBottom && typeof scrollToBottom === "function") scrollToBottom();
  };
  if (opts.immediate) measure();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(measure);
  setTimeout(measure, 420);
}

function _ensureClarifyResizeListener() {
  if (_clarifyResizeListenerReady || typeof window === "undefined") return;
  _clarifyResizeListenerReady = true;
  window.addEventListener("resize", () => {
    const card = $("clarifyCard");
    if (card && card.classList.contains("visible")) {
      _syncClarifyTranscriptSpace(card, {immediate: true});
    }
  }, {passive: true});
}

function toggleClarifyCardCollapsed(forceCollapsed) {
  const card = $("clarifyCard");
  if (!card) return;
  const collapsed = typeof forceCollapsed === "boolean" ? forceCollapsed : !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  _syncClarifyCollapseButton(card);
  _syncClarifyTranscriptSpace(card, {immediate: true});
}

function _clearClarifyHideTimer() {
  if (_clarifyHideTimer) {
    clearTimeout(_clarifyHideTimer);
    _clarifyHideTimer = null;
  }
}

function _clearClarifyCountdownTimer() {
  if (_clarifyCountdownTimer) {
    clearInterval(_clarifyCountdownTimer);
    _clarifyCountdownTimer = null;
  }
  _clarifyExpiresAt = 0;
  const countdown = $("clarifyCountdown");
  if (countdown) {
    countdown.textContent = "";
    countdown.classList.remove("urgent");
  }
}

function _clarifyExpiryMs(pending) {
  const expiresAt = Number(pending && pending.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) return expiresAt * 1000;
  const requestedAt = Number(pending && pending.requested_at);
  const timeoutSeconds = Number(pending && pending.timeout_seconds);
  if (Number.isFinite(requestedAt) && Number.isFinite(timeoutSeconds)) {
    return (requestedAt + timeoutSeconds) * 1000;
  }
  return 0;
}

function _updateClarifyCountdown() {
  const countdown = $("clarifyCountdown");
  if (!countdown || !_clarifyExpiresAt) return;
  const remaining = Math.max(0, Math.ceil((_clarifyExpiresAt - Date.now()) / 1000));
  countdown.textContent = `${remaining}s`;
  countdown.classList.toggle("urgent", remaining <= 10);
}

function _startClarifyCountdown(pending) {
  const expiresAt = _clarifyExpiryMs(pending);
  if (_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt) return;
  _clearClarifyCountdownTimer();
  _clarifyExpiresAt = expiresAt;
  if (!_clarifyExpiresAt) return;
  _updateClarifyCountdown();
  _clarifyCountdownTimer = setInterval(_updateClarifyCountdown, 1000);
}

function _stashClarifyDraft(reason) {
  if (reason !== "expired" && reason !== "terminal") return false;
  const submit = $("clarifySubmit");
  if (submit && submit.classList.contains("loading")) return false;
  const input = $("clarifyInput");
  const draft = String((input && input.value) || "").trim();
  if (!draft) return false;
  const sid = _clarifySessionId || (S.session && S.session.session_id) || "unknown";
  const key = `hermes-clarify-draft-${sid}-${_clarifySignature || "unknown"}`;
  try {
    sessionStorage.setItem(key, JSON.stringify({
      draft,
      reason,
      saved_at: Date.now(),
    }));
  } catch (_) {}
  const composer = $('msg');
  if (composer) {
    const current = String(composer.value || "");
    composer.value = current.trim() ? `${current.replace(/\s+$/, "")}\n\n${draft}` : draft;
    if (typeof autoResize === "function") autoResize();
    if (typeof updateSendBtn === "function") updateSendBtn();
  }
  const notice = reason === "expired"
    ? "Clarification timed out. Your draft was kept in the composer."
    : "Clarification closed. Your draft was kept in the composer.";
  if (typeof setComposerStatus === "function") setComposerStatus(notice);
  else if (typeof setStatus === "function") setStatus(notice);
  if (typeof showToast === "function") showToast(notice, 5000);
  return true;
}

function _resetClarifyCardState() {
  _clearClarifyHideTimer();
  _clearClarifyCountdownTimer();
  _clarifyVisibleSince = 0;
  _clarifySignature = '';
  _clarifyId = null;
}

function hideClarifyCard(force=false, reason="dismissed") {
  const card = $("clarifyCard");
  if (!card) {
    _clarifySessionId = null;
    _resetClarifyCardState();
    if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
    return;
  }
  if (!force && reason !== "expired" && _clarifyVisibleSince) {
    const remaining = CLARIFY_MIN_VISIBLE_MS - (Date.now() - _clarifyVisibleSince);
    if (remaining > 0) {
      const scheduledSignature = _clarifySignature;
      _clearClarifyHideTimer();
      _clarifyHideTimer = setTimeout(() => {
        _clarifyHideTimer = null;
        if (_clarifySignature !== scheduledSignature) return;
        hideClarifyCard(true, reason);
      }, remaining);
      return;
    }
  }
  _stashClarifyDraft(reason);
  _clarifySessionId = null;
  _resetClarifyCardState();
  card.classList.remove("visible");
  _setPromptFlyoutHidden(card, true);
  _syncClarifyTranscriptSpace(null);
  if (typeof unlockComposerForClarify === "function") unlockComposerForClarify();
  $("clarifyQuestion").textContent = "";
  $("clarifyChoices").innerHTML = "";
  $("clarifyInput").value = "";
  $("clarifyInput").disabled = false;
  $("clarifyInput").onkeydown = null;
  const submit = $("clarifySubmit");
  if (submit) { submit.disabled = false; submit.classList.remove("loading"); }
}

function _clarifySetControlsDisabled(disabled, loading=false) {
  const input = $("clarifyInput");
  const submit = $("clarifySubmit");
  if (input) input.disabled = disabled;
  if (submit) {
    submit.disabled = disabled;
    submit.classList.toggle("loading", !!loading);
  }
  const choices = $("clarifyChoices");
  if (choices) {
    choices.querySelectorAll("button").forEach(btn => {
      btn.disabled = disabled;
      if (loading && btn.dataset && btn.dataset.choice === "other") {
        btn.classList.toggle("loading", false);
      }
    });
  }
}

function showClarifyCard(pending) {
  const sid = _rememberClarifyPending(pending);
  if (!_clarifyPromptBelongsToActiveSession(sid)) return;
  const question = pending.question || pending.description || '';
  const choices = Array.isArray(pending.choices_offered)
    ? pending.choices_offered
    : (Array.isArray(pending.choices) ? pending.choices : []);
  const sig = JSON.stringify({
    question,
    choices,
    sid: pending._session_id || (S.session && S.session.session_id) || null,
    clarify_id: pending.clarify_id || null,
  });
  const card = _ensureClarifyCardDom();
  if (!card) return;
  const questionEl = $("clarifyQuestion");
  const choicesEl = $("clarifyChoices");
  const input = $("clarifyInput");
  const sameClarify = card.classList.contains("visible") && _clarifySignature === sig;
  _clarifySessionId = sid;
  _clarifyId = pending.clarify_id || null;
  _clarifySignature = sig;
  if (Number(pending.timeout_seconds) > 0) {
    _startClarifyCountdown(pending);
  } else {
    _clearClarifyCountdownTimer();
  }
  if (!sameClarify) {
    _clarifyVisibleSince = Date.now();
    _clearClarifyHideTimer();
    card.classList.remove("collapsed");
  }
  if (questionEl) questionEl.textContent = question;
  if (choicesEl) {
    choicesEl.innerHTML = '';
    choicesEl.style.display = choices.length ? '' : 'none';
    if (choices.length) {
      choices.forEach((choice, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'clarify-choice';
        btn.dataset.choice = choice;
        btn.onclick = () => respondClarify(choice);
        const badge = document.createElement('span');
        badge.className = 'clarify-choice-badge';
        badge.textContent = String(idx + 1);
        const text = document.createElement('span');
        text.className = 'clarify-choice-text';
        text.textContent = choice;
        btn.appendChild(badge);
        btn.appendChild(text);
        choicesEl.appendChild(btn);
      });
      const other = document.createElement('button');
      other.type = 'button';
      other.className = 'clarify-choice other';
      other.dataset.choice = 'other';
      other.setAttribute('data-i18n', 'clarify_other');
      const otherBadge = document.createElement('span');
      otherBadge.className = 'clarify-choice-badge other';
      otherBadge.textContent = '•';
      const otherText = document.createElement('span');
      otherText.className = 'clarify-choice-text';
      otherText.textContent = t('clarify_other') || 'Other';
      other.appendChild(otherBadge);
      other.appendChild(otherText);
      other.onclick = () => {
        const el = $("clarifyInput");
        if (el) {
          el.focus();
          if (typeof el.select === 'function') el.select();
        }
      };
      choicesEl.appendChild(other);
    }
  }
  if (input) {
    if (!sameClarify) input.value = '';
    input.disabled = false;
    input.removeAttribute('readonly');
    input.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        respondClarify();
      }
    };
  }
  if (typeof lockComposerForClarify === "function") {
    lockComposerForClarify(question ? `Clarification needed: ${question}` : "Clarification needed");
  }
  _clarifySetControlsDisabled(false, false);
  _ensureClarifyResizeListener();
  _setPromptFlyoutHidden(card, false);
  card.classList.add("visible");
  _syncClarifyCollapseButton(card);
  _syncClarifyTranscriptSpace(card, {immediate: true});
  if (typeof applyLocaleToDOM === "function") applyLocaleToDOM();
  // Move focus to clarify input synchronously (not in setTimeout) and
  // only if the user wasn't mid-type in the composer textarea.
  if (input && !sameClarify && document.activeElement !== $('msg')) {
    input.focus({preventScroll: true});
  }
  if (typeof syncTopbar === 'function') syncTopbar();
}

async function respondClarify(response) {
  const sid = _clarifySessionId || (S.session && S.session.session_id);
  if (!sid) return;
  const input = $("clarifyInput");
  let value = typeof response === 'string' ? response : (input ? input.value : '');
  value = String(value || '').trim();
  if (!value) {
    if (input) input.focus();
    return;
  }
  const clarifyId = _clarifyId;
  // Keep a draft copy so we can restore the input on failure (issue #2639).
  const draft = value;
  _clarifySetControlsDisabled(true, true);
  try {
    const result = await api("/api/clarify/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, response: value, clarify_id: clarifyId || "" })
    });
    if (result && result.ok) {
      // Only clear/hide if the visible prompt still matches what was just
      // submitted.  If a parallel SSE event already loaded the next queued
      // prompt, erasing the session cache would leave the agent waiting
      // until timeout (codex review P1, issue #2639).
      if (_clarifyId === clarifyId) {
        _clarifySessionId = null;
        _clarifyId = null;
        _clearClarifyPendingForSession(sid);
        hideClarifyCard(true, 'sent');
        // Echo the user's clarify choice as a visible message in the conversation
        if (S.session && S.session.session_id === sid) {
          S.messages.push({
            role: 'user',
            content: value,
            _clarify_response: true,
            _ts: Date.now() / 1000,
          });
          if (typeof renderMessages === 'function') renderMessages({preserveScroll: true});
        }
      }
    } else {
      // Stale / expired / wrong session — keep the card and draft visible.
      _clarifySetControlsDisabled(false, false);
      if (input) {
        input.value = draft;
        input.focus();
      }
      const errMsg = (result && result.error) || "Clarification response not accepted — the agent may have already proceeded.";
      if (typeof showToast === "function") showToast(errMsg, 5000);
      if (typeof setStatus === "function") setStatus(errMsg);
    }
  } catch(e) {
    // The server returns 409 with ``stale: true`` for both genuinely-expired
    // prompts and wrong-session/next-prompt-loaded races. In both cases the
    // server-side ``_pending`` entry for *this* clarify_id is gone, so
    // retrying it can never succeed — the prior keep-card-and-draft
    // behavior left the user with a permanently 409-ing card and a locked
    // composer, with no affordance to dismiss it (#4504). Treat 409 as
    // terminal here, but only when the visible card still matches what we
    // just submitted: mirroring the success path's ``_clarifyId === clarifyId``
    // guard (codex review P1, #2639) — if a parallel poll already rendered
    // the *next* queued prompt B while A's response was in flight, we must
    // not tear B down on A's late 409. The SSE/poll path will re-render the
    // next prompt's card from scratch via ``showClarifyCard`` either way.
    if (e && e.status === 409) {
      if (_clarifyId === clarifyId) {
        // Same card still showing — dismiss it and rescue the typed draft.
        // Order matters: ``_stashClarifyDraft`` (called from
        // ``hideClarifyCard``) bails when ``#clarifySubmit`` still carries
        // the ``loading`` class set above. Clear loading first, otherwise
        // the typed answer is silently dropped (reviewer P1).
        _clarifySetControlsDisabled(false, false);
        _clarifySessionId = null;
        _clarifyId = null;
        _clearClarifyPendingForSession(sid);
        hideClarifyCard(true, "expired");
        const errMsg = (e.message || "Clarification prompt expired or not found.");
        if (typeof setStatus === "function") setStatus("Clarify: " + errMsg);
        // ``_stashClarifyDraft('expired')`` already surfaces the actionable
        // "Clarification timed out. Your draft was kept in the composer."
        // toast when there is a draft to rescue, so we don't double-toast.
        return;
      }
      // A newer prompt is showing (race between user click and SSE/poll).
      // Don't dismiss it on this late 409 — just re-enable controls and
      // surface the error. The user's draft for the now-stale prompt is
      // dropped intentionally; the next prompt has its own input cycle.
      _clarifySetControlsDisabled(false, false);
      if (typeof setStatus === "function") {
        setStatus("Clarify: previous prompt expired — a newer one is showing.");
      }
      return;
    }
    // Network / other transient errors — keep the card and draft visible so
    // the user can retry once connectivity returns.
    _clarifySetControlsDisabled(false, false);
    if (input) {
      input.value = draft;
      input.focus();
    }
    const errMsg = (e && e.message) || "Failed to deliver clarification response.";
    if (typeof setStatus === "function") setStatus("Clarify: " + errMsg);
    if (typeof showToast === "function") showToast(errMsg, 5000);
  }
}

var _clarifyEventSource = null;
var _clarifyFallbackTimer = null;
var _clarifyHealthTimer = null;
let _clarifyFallbackPollInFlight = false;
let _clarifyPollingSessionId = null;

function startClarifyPolling(sid) {
  stopClarifyPolling();
  _clarifyPollingSessionId = sid || null;
  _clarifyMissingEndpointWarned = false;

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
  // (3-second interval, acceptable tradeoff)
  _startClarifyFallbackPoll(sid);
}

function _startClarifyFallbackPoll(sid) {
  _clarifyPollingSessionId = sid || null;
  // Run one tick immediately so a session already blocked on a pending clarify
  // shows its card instantly (the removed SSE 'initial' event used to do this);
  // then poll on the 3000ms cadence. (#3913 SHOULD-FIX)
  const _tick = async () => {
    if (!S.session || S.session.session_id !== sid) {
      stopClarifyPolling(); _hideClarifyCardIfOwner(sid, true, 'session'); return;
    }
    if (_clarifyFallbackPollInFlight) return;
    _clarifyFallbackPollInFlight = true;
    try {
      const data = await api("/api/clarify/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false});
      if (data.pending) { showClarifyForSession(sid, data.pending); }
      else { _clearClarifyPendingForSession(sid); _hideClarifyCardIfOwner(sid, false, 'expired'); }
    } catch(e) {
      const msg = String((e && e.message) || "");
      // `api()` attaches the raw HTTP status on the thrown Error (err.status).
      // Branch on that structured value instead of scraping the message string
      // so an unrelated stale-session or lifecycle error can never masquerade as
      // a missing clarify endpoint. (#5345)
      const status = (e && typeof e.status === "number") ? e.status : null;
      const currentSid = (S.session && S.session.session_id) || null;
      const logDetails = {
        path: "/api/clarify/pending",
        status: status,
        pollingSessionId: sid,
        currentSessionId: currentSid,
        message: msg,
      };
      // A 404 from the active session domain is a STALE-SESSION signal — e.g.
      // the old profile's session still polling briefly after a profile switch,
      // or a session deleted server-side — NOT a missing clarify endpoint. Stop
      // this stale poll and hide its card silently instead of telling the user
      // to restart the server. Keep this branch before any warn-level logging:
      // routine profile switches must not fill DevTools with expected warnings.
      // (#5343 / #5345)
      const isSessionScoped404 = status === 404
        && (/session/i.test(msg) || (currentSid !== null && currentSid !== sid));
      if (isSessionScoped404) {
        _clearClarifyPendingForSession(sid);
        _hideClarifyCardIfOwner(sid, true, 'session');
        stopClarifyPolling();
        return;
      }
      // Only a GENUINE missing-endpoint 404 (the route-not-found fall-through,
      // body {"error":"not found"}, from a server build that predates
      // /api/clarify/pending) should surface the restart-server warning. The
      // previous code matched arbitrary "404"/"not found" text in ANY caught
      // error message, so an unrelated stale-session 404 or a transient network
      // error produced a false "Clarify endpoint unavailable" toast even though
      // the endpoint is present and returning HTTP 200 on every request. Gate
      // strictly on the structured status + a route-not-found body that is not
      // a session-scoped 404. (#5345)
      const isMissingEndpoint = status === 404
        && /(^|\b)not\s+found(\b|$)/i.test(msg)
        && !isSessionScoped404;
      if (isMissingEndpoint) {
        if (!_clarifyMissingEndpointWarned) {
          _clarifyMissingEndpointWarned = true;
          setComposerStatus("Clarify unavailable on current server build. Restart server.");
          if (typeof showToast === "function") {
            showToast("Clarify endpoint unavailable. Please restart server.", 5000);
          }
          if (typeof console !== "undefined" && console.warn) {
            console.warn("[clarify] pending poll endpoint unavailable", logDetails);
          }
        }
        stopClarifyPolling();
        return;
      }
      // Structured diagnostics: unexpected clarify poll failures should be
      // inspectable without guessing from a toast. Expected stale-session 404s
      // and the handled missing-endpoint route have already returned above.
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[clarify] pending poll failed", logDetails);
      }
    } finally {
      _clarifyFallbackPollInFlight = false;
    }
  };
  _clarifyFallbackTimer = setInterval(_tick, 3000);
  _tick();
}

function stopClarifyPollingForSession(sid) {
  if(sid && _clarifyPollingSessionId && _clarifyPollingSessionId!==sid) return;
  stopClarifyPolling();
}

function stopClarifyPolling() {
  if (_clarifyEventSource) { try { if(_clarifyEventSource.readyState!==2)_clarifyEventSource.close(); } catch(_){} _clarifyEventSource = null; }
  if (_clarifyFallbackTimer) { clearInterval(_clarifyFallbackTimer); _clarifyFallbackTimer = null; }
  if (_clarifyHealthTimer) { clearInterval(_clarifyHealthTimer); _clarifyHealthTimer = null; }
  _clarifyFallbackPollInFlight = false;
  _clarifyPollingSessionId = null;
}

Object.assign(HermesMessages, {
  startClarifyPolling,
  stopClarifyPolling,
  respondClarify,
});
