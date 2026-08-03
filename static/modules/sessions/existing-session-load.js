import { _restoreComposerDraft, _saveComposerDraftNow } from './composer-drafts.js';
import { sessionLoadState } from './session-load-state.js';
import { _isSessionActivelyViewedForList, _sessionVisitHasUnreadState } from './session-unread.js';
import { _acknowledgeSessionVisit } from './session-visit.js';
import { _checkAndShowHandoffHint, _hideHandoffHint } from './handoff-lifecycle.js';
import { _resolveSessionModelForDisplaySoon } from './session-post-load.js';
import { _isMessagingSession } from './session-source.js';
import { _appRootPath, _setActiveSessionUrl } from './session-navigation.js';
import { _clearDeferredActiveSessionExternalRefresh } from './session-list-refresh.js';
import { _resolveSessionIdFromSidebarLineage } from './session-lineage.js';
import { _clearEmptyComposerModelOverride } from './new-session.js';
import { _rearmActiveSessionStream, _restoreLoadedSession } from './session-load-recovery.js';
import { _sessionProfileMismatchFromError, _switchProfileForSessionLoad } from './session-profile-load.js';
import { _captureSameSessionForceReloadHint, _clearSameSessionForceReloadHint, _prefetchSessionMessages } from './transcript-loading.js';
import { transcriptWindowState } from './transcript-window-state.js';
import { _resetYoloState } from '../messages/approvals.js';

/**
 * Self-heal: clear the stuck session ID from localStorage and URL when a
 * loadSession() call failed during boot (no currentSid). This prevents the
 * browser from retrying the same dead session on every refresh.
 *
 * Called from loadSession() after 401 redirect (undefined data) or any
 * non-404 error (400, 403, 500, network). The 404 path has its own
 * inline self-heal; this helper consolidates the non-404 cases.
 *
 * Only clears when !currentSid — no session is active on screen, so
 * the stored ID is definitely stale. When currentSid is set (already
 * viewing a session), a non-404 failure could be a transient server error
 * and the session may still exist on the server; wiping localStorage in
 * that case is unnecessarily destructive (#4028 follow-up).
 *
 * A click into a *different* dead session (currentSid && currentSid!==sid)
 * must not run it: localStorage and the URL still point at the live session
 * (both are only updated on a successful load), so wiping them would log
 * the user out of a healthy session (#2782).
 */
function _clearStuckSessionOnBoot(sid, currentSid){
  if(!currentSid){
    try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
    try{ history.replaceState(null,'',_appRootPath()); }catch(_){ }
  }
}

async function loadSession(sid){
  const opts = arguments[1] || {};
  // Resolve canonical lineage SID BEFORE both the direct and sidebar preload
  // notifications so extensions always see the canonical session id, not the
  // raw sidebar click id (which may differ after lineage folding).
  if(!opts.skipLineageResolve && typeof _resolveSessionIdFromSidebarLineage==='function'){
    const resolvedSid=_resolveSessionIdFromSidebarLineage(sid);
    if(resolvedSid&&resolvedSid!==sid) sid=resolvedSid;
  }
  // Extension pre-open hook — fires once per sidebar click, not on every call.
  // _openSidebarSession passes _preloadNotified:true so the hook isn't re-fired
  // when loadSession runs the actual navigation inside it.
  if(!opts.skipExtHooks && !opts._preloadNotified && typeof _hermesNotifySessionOpen==='function'){
    var _preResult=_hermesNotifySessionOpen(sid, null, {preload:true, opts:opts});
    if(_preResult&&_preResult.cancel===true){
      return;
    }
  }
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid===sid;
  // Clicking the already-open session in the sidebar is a no-op. Reloading it
  // tears down active pane state and can reset the long-session scroll window
  // to the top even though the user did not navigate anywhere. Explicit
  // refresh paths pass {force:true} when external state.db changes arrive.
  // Do not no-op a same-session click while another load is in flight: the
  // previous transcript may already have been cleared for the pending switch.
  // Static force-reload invariant: if(currentSid===sid && !forceReload) return;
  // #2971: idempotent re-arm before the no-op guard revives a stream a prior
  // failed loadSession killed; no-ops on real switches.
  _rearmActiveSessionStream();
  if(currentSid===sid && !forceReload && (!sessionLoadState.loadingSessionId || sessionLoadState.loadingSessionId===sid)){
    // Re-selecting the already-open session is a no-op for transcript/scroll, but
    // it is still a *visit*: clear a stale sidebar unread dot (e.g. one a
    // background completion left on the open, unfocused pane) before returning.
    if(_sessionVisitHasUnreadState(sid)){
      _acknowledgeSessionVisit(
        sid,
        Number(S.session.message_count || 0),
        Number(S.session.last_message_at || S.session.updated_at || 0)
      );
    }
    return;
  }
  // Mark this session as the in-flight load. Subsequent loadSession() calls
  // will overwrite this; stale awaits use the mismatch to bail out (#1060).
  const _loadGeneration=sessionLoadState.begin(sid);
  const _isCurrentLoad=()=>sessionLoadState.isCurrent(sid,_loadGeneration);
  if(currentSid!==sid&&typeof _uploadPendingFilesSyncProgressForSession==='function')_uploadPendingFilesSyncProgressForSession(sid);
  // Reset scroll state for fresh session navigation — the reader expects to
  // land at the bottom of the new transcript, not wherever a stale unpin flag
  // from a prior session or a stray touch event during loading would place them.
  if (currentSid !== sid && typeof _messageUserUnpinned !== 'undefined') {
    _messageUserUnpinned = false;
    _scrollPinned = true;
  }
  stopApprovalPolling();hideApprovalCard(forceReload);
  if(typeof stopSessionStream==='function') stopSessionStream();
  _resetYoloState();
  if(typeof stopClarifyPolling==='function') stopClarifyPolling();
  if(typeof hideClarifyCard==='function') hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed');
  // Show loading indicator immediately for responsiveness.
  // Cleared by renderMessages() once full session data arrives.
  // Persist the current composer draft before switching away so it can be
  // restored when the user switches back (#1060). Save to server now so the
  // draft survives page refresh and syncs across clients.
  let _draftSavePromise=Promise.resolve();
  if (currentSid && currentSid !== sid) {
    if(typeof window._clearPendingSelections==='function') window._clearPendingSelections();
    if(typeof _clearQueueCardDisplay==='function') _clearQueueCardDisplay(currentSid);
    _draftSavePromise=_saveComposerDraftNow(currentSid, ($('msg') || {}).value || '', S.pendingFiles ? [...S.pendingFiles] : []);
    // Snapshot the live turn before msgInner is replaced. Preserves the activity
    // timer, partial response, and tool cards so switching back does not rebuild
    // the stream UI from scratch.
    if(
      (S.busy||S.activeStreamId||(INFLIGHT&&INFLIGHT[currentSid]))&&
      typeof snapshotLiveTurnHtmlForSession==='function'
    ){
      if(!INFLIGHT[currentSid]){
        INFLIGHT[currentSid]={
          messages:Array.isArray(S.messages)?[...S.messages]:[],
          uploaded:[],
          toolCalls:Array.isArray(S.toolCalls)?[...S.toolCalls]:[],
        };
      }
      snapshotLiveTurnHtmlForSession(currentSid);
    }
  }
  const _keepStaleUntilLoaded = !!opts.keepStaleUntilLoaded && sameSessionForceReload;
  if (currentSid !== sid || forceReload) {
    // #3306: When force-reloading the currently-active session (e.g. external
    // poll triggering a refresh), snapshot the existing messages BEFORE we
    // clear them. _ensureMessagesLoaded() runs the ephemeral-field
    // carry-forward (_turnUsage, _turnDuration, _turnTps, _gatewayRouting,
    // _statusCard, _anchor_stream_id) against S.messages, but by the time the API fetch returns
    // S.messages has already been reset to [] here and the carry-forward is a
    // no-op. The visible symptom is the token-usage badge vanishing ~10s
    // after each assistant turn completes. Stash the snapshot so the
    // carry-forward call can consume it.
    sessionLoadState.pendingCarryForwardSnapshot = (currentSid === sid && forceReload)
      ? (S.messages || []).slice()
      : null;
    // #3239: also capture a reload-width hint BEFORE clearing so the
    // authoritative reload preserves the already-loaded transcript width
    // instead of collapsing a long session back to the default tail window.
    if (sameSessionForceReload) _captureSameSessionForceReloadHint(sid);
    else _clearSameSessionForceReloadHint();
    // #5177: keep-stale-until-loaded path — defer the destructive
    // S.messages/toolCalls clear so the user does NOT see a transcript-wide
    // blank gap during the metadata + messages round-trip. Only the
    // visibility / focus recovery callers in refreshActiveSessionIfExternallyUpdated
    // request this. The new transcript will be SWAPPED into S.messages by the
    // forced _ensureMessagesLoaded(...{force:true}) call below, producing a
    // single render frame with old DOM directly replaced by new DOM rather
    // than the old → empty → new sequence the default branch produces.
    //
    // The session-switch branch (currentSid !== sid) MUST continue to clear
    // synchronously — leaving a prior session's transcript on screen during a
    // navigation is the original bug this clear was written for. We gate
    // strictly on sameSessionForceReload (computed above as part of
    // _keepStaleUntilLoaded) so cross-session switches keep their existing
    // behaviour.
    if (!_keepStaleUntilLoaded) {
      S.messages = [];
      S.toolCalls = [];
      transcriptWindowState.messagesTruncated = false;
      transcriptWindowState.oldestIdx = 0;
    }
    // Close live SSE streams from the session we're leaving. The error
    // handler checks _isSessionActivelyViewed() and won't auto-reconnect
    // for a backgrounded session, preventing leaked connections that would
    // pump token events into an orphaned closure, freezing the main thread.
    if (currentSid && currentSid !== sid && typeof closeOtherLiveStreams === 'function') {
      closeOtherLiveStreams(sid);
    }
    transcriptWindowState.loadingOlder = false;
    const _msgInner = $('msgInner');
    if (_msgInner && currentSid !== sid) _msgInner.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Loading conversation...</div>';
  }
  // Phase 1: Load metadata only (~1KB) for fast session switching. Keep model
  // resolution out of the first-paint path; old provider-shaped model IDs are
  // repaired by the deferred resolver after S.session is assigned.
  // Guard against network/server failures to prevent a permanently stuck loading state.
  let data;
  let _prefetchedMessages;
  try {
    const _metadataRequest=api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    _prefetchedMessages=_prefetchSessionMessages(sid);
    data = await _metadataRequest;
  } catch(e) {
    const profileMismatch=_sessionProfileMismatchFromError(e);
    if(profileMismatch && profileMismatch.profile && !opts.skipProfileResolve){
      if (!_isCurrentLoad()) {
        _rearmActiveSessionStream();
        return;
      }
      try{
        if(typeof showToast==='function') showToast(`Switching to ${profileMismatch.profile} profile for this session…`,2200);
        await _switchProfileForSessionLoad(profileMismatch.profile);
        // Post-await stale-load guard (Codex): the profile switch above does a
        // network POST + session-list re-render, during which the user may have
        // navigated to a different session. If we no longer own the load, bail
        // before clearing _loadingSessionId or retrying so the stale
        // continuation can't hijack the UI back to the old target.
        if (!_isCurrentLoad()) {
          _rearmActiveSessionStream();
          return;
        }
        if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
        return loadSession(sid,{...opts,skipProfileResolve:true,force:true,_preloadNotified:true});
      }catch(switchErr){
        e=switchErr;
      }
    }
    const _msgInner = $('msgInner');
    // Stale-load guard (Codex): a newer loadSession() may have started while this
    // request was awaiting (e.g. the user clicked a healthy session during a
    // boot-time restore). currentSid was snapshotted before the await, so without
    // this guard a failed superseded load could self-heal (wipe localStorage/URL)
    // for the session the user actually navigated to. If we no longer own the
    // load, re-arm the active session's stream and bail before any DOM mutation
    // or self-heal.
    if (!_isCurrentLoad()) {
      _rearmActiveSessionStream();
      return;
    }
    if(_msgInner){
      if(e.status===404){
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Session not available in web UI.</div>';
        // Self-heal (clear saved id + strip /session/<id> URL) only when the
        // 404'd id is the one we are activating: a boot-time restore
        // (!currentSid, #2798) or a mid-session reload of the *current* session
        // whose sidecar was deleted server-side (#2782). A click into a
        // *different* dead session (currentSid && currentSid!==sid) must not run
        // it: localStorage and the URL still point at the live session (both are
        // only updated on a successful load), so wiping them would log the user
        // out of a healthy session. The URL strip is needed in the self-heal
        // case because _sessionIdFromLocation() re-injects the id on reload.
        // Only the rethrow stays gated on !currentSid: boot rethrows to fall
        // through to empty-state; mid-session there is no boot path to reach.
        if(!currentSid || currentSid===sid){
          try{ localStorage.removeItem('hermes-webui-session'); }catch(_){ }
          try{ history.replaceState(null,'',_appRootPath()); }catch(_){ }
          if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
          if(!currentSid){
            throw e;
          }
        }
      } else {
        // Non-404, non-401 failure (400, 403, 500, network): 401 is handled
        // via the if(!data) guard below since api() returns undefined on 401
        // rather than throwing. Clear the stuck session ID only during boot
        // (!currentSid) so the next boot doesn't retry the same dead session.
        // When currentSid is set, a 500/network error may be transient — the
        // session might still exist on the server (#4028 follow-up).
        _clearStuckSessionOnBoot(sid, currentSid);
        _msgInner.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:14px;padding:40px;text-align:center;">Failed to load session. Try refreshing or switching sessions.</div>';
        if(typeof showToast==='function') showToast('Failed to load session',3000,'error');
      }
    }
    _clearSameSessionForceReloadHint(sid);
    // Capture whether this failure self-healed away the current session (a
    // 404 on the *current* session whose sidecar was deleted server-side).
    // In that case there is no live session left to stream for, so we must
    // NOT restart — doing so would spin the SSE reconnect loop against a dead
    // session_id.
    const _selfHealedCurrent = (e.status===404) && (currentSid===sid);
    if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
    // The session stream was stopped unconditionally at the top of this load
    // (mirroring stopApprovalPolling). On the happy path it's restarted ~120
    // lines below, but this failure exit never reaches that point — leaving
    // the session still on screen permanently silenced. bg_task_complete
    // events (the new feature's primary delivery path) would be dropped until
    // the user explicitly navigates to a session again. Restart the stream for
    // the session that remains on screen. Skip when a newer load is already in
    // flight (_loadingSessionId !== null after the reset above): that load owns
    // the stream and starts its own. Skip the self-healed-current case (no live
    // session to stream).
    // #2971: this fetch-error path keeps its bespoke guarded restart (rather
    // than the shared _rearmActiveSessionStream helper used on the other
    // early-returns) because only here can the current session have just
    // self-healed away — re-arming a 404'd/deleted session_id would spin the
    // SSE reconnect loop against a dead session.
    if (currentSid && !_selfHealedCurrent && sessionLoadState.loadingSessionId === null
        && typeof startSessionStream === 'function') {
      startSessionStream(currentSid);
    }
    return;
  }
  // Draft durability and target loading are independent network operations.
  // Keep the durability guarantee, but overlap the old-session POST with both
  // target GETs instead of putting every switch behind an extra round trip.
  await _draftSavePromise;
  if (!_isCurrentLoad()) {
    _rearmActiveSessionStream();
    return;
  }
  // Guard: api() may have redirected (401) and returned undefined; in that case
  // the browser is already navigating away, so abort the rest of this flow.
  // No self-heal: 401 is transient auth expiry — the session still exists
  // server-side. Clearing localStorage would wipe the saved session id and
  // send users to empty state after re-login (#4028 follow-up).
  if (!data) {
    _clearSameSessionForceReloadHint(sid);
    if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;
    // #2971: re-arm the still-displayed session's stream (defensive — harmless
    // if the 401 redirect is already tearing the page down). Idempotent.
    _rearmActiveSessionStream();
    return;
  }
  // Stale response? A newer loadSession() call has already started (#1060).
  if (!_isCurrentLoad()) {
    // #2971: a newer in-flight load owns the final stream arming, but until it
    // assigns S.session and reaches startSessionStream() the currently-shown
    // session must not be left stream-dead by our top-of-function teardown.
    // Re-arm the genuinely-displayed S.session (idempotent — no-ops once the
    // newer load arms its own sid).
    _rearmActiveSessionStream();
    return;
  }
  // #2980: if this (current) load resolved a hidden pre-compression snapshot,
  // follow the backend's continuation hint to the visible continuation so a
  // mobile reload mid-compression doesn't strand the user on a hidden snapshot.
  // Do NOT write URL/localStorage here — let the re-entrant loadSession update
  // them only once the continuation actually loads, so a rejected/deleted/
  // cross-profile continuation can't poison restore state with an unusable id.
  const continuationSid=(data.session&&data.session.continuation_session_id)||'';
  if(continuationSid&&continuationSid!==sid&&!opts.skipContinuationResolve){
    sessionLoadState.loadingSessionId=null;
    return loadSession(continuationSid,{...opts,skipLineageResolve:true,skipContinuationResolve:true,force:true,_preloadNotified:true});
  }
  S.session=data.session;
  if(typeof _clearEmptyComposerModelOverride==='function') _clearEmptyComposerModelOverride();
  // Loading a real existing session abandons any pre-session toolset override
  // staged on the empty composer before any deferred refresh work runs.
  S._pendingSessionToolsets=null;
  if(typeof window!=='undefined'){
    if(!S._bootReady&&typeof window._startBootModelDropdown==='function'){
      Promise.resolve().then(()=>{
        if(!S.session||S.session.session_id!==sid) return undefined;
        return window._startBootModelDropdown();
      }).catch(()=>{});
    }else{
      // Session metadata already carries the active model, and syncTopbar()
      // injects a missing session-scoped option into the select. Mark the
      // provider catalog stale here, but defer its bounded freshness check
      // until the user actually opens the model picker.
      window._modelDropdownReady=null;
    }
  }
  if(typeof _hydrateTodosFromSession==='function') _hydrateTodosFromSession(S.session);
  S.session._modelResolutionDeferred=true;
  S.lastUsage={...(data.session.last_usage||{})};
  // Reset scroll-direction tracker only on real session switches so the new
  // chat's first scroll doesn't compare against the previous chat's scrollTop
  // and false-trigger an unpin (#1731 follow-up — Opus stage-302 SHOULD-FIX).
  // Same-session force refreshes reuse the current transcript viewport; clearing
  // the sticky-unpin state here makes preserveScroll treat a reader mid-answer
  // as pinned and snap them back to the bottom on the next render.
  if (currentSid !== sid) {
    _clearDeferredActiveSessionExternalRefresh();
  }
  if (currentSid !== sid && typeof window !== 'undefined' && typeof window._resetScrollDirectionTracker === 'function') {
    try { window._resetScrollDirectionTracker(); } catch (_) {}
  }
  if(typeof _applyPendingSessionModelForSession==='function') _applyPendingSessionModelForSession(sid);
  _resolveSessionModelForDisplaySoon(sid);
  // Sync workspace display immediately so the chip label reflects the new session's workspace
  // before any async message-loading begins (mirrors how model is handled).
  if(typeof syncTopbar==='function') syncTopbar();
  // Acknowledge the visit as soon as the session metadata is accepted for the
  // in-flight load: clears the viewed count + any stale completion-unread marker
  // `let` (not const): re-read below, after the awaited _ensureMessagesLoaded,
  // so a server_turn_started that attaches a live stream MID-RELOAD is honored
  // by the attach/idle decision instead of being clobbered by the stale snapshot.
  let activeStreamId=S.session.active_stream_id||null;
  // If the server says the session is idle, reset browser-side streaming flags
  // NOW — BEFORE _acknowledgeSessionVisit() below (whose sidebar repaint would
  // otherwise inherit the PREVIOUS session's busy/stream state) and before the
  // async _ensureMessagesLoaded gap. Without this, S.busy can remain true from a
  // still-running stream in the PREVIOUS session while S.session.session_id has
  // already advanced to the new one. _isSessionLocallyStreaming() checks
  // (isActive && S.busy), so the new session would appear locally-streaming
  // (sidebar spinner, Stop button, thinking state on an idle chat) and the visit
  // repaint would manufacture a phantom unread. Also clears stale INFLIGHT
  // entries left behind by a crashed/restarted stream. (#5917 gate: reset must
  // precede the acknowledge repaint.)
  if(!activeStreamId){
    S.activeStreamId=null;
    S.busy=false;
    if(INFLIGHT[sid]){
      delete INFLIGHT[sid];
      if(typeof clearInflightState==='function') clearInflightState(sid);
    }
  }

  // and syncs the polling snapshot so a deferred /api/sessions poll landing
  // during the async message-load gap below cannot re-flag a stale unread dot.
  _acknowledgeSessionVisit(
    S.session.session_id,
    Number(data.session.message_count || 0),
    Number(data.session.last_message_at || data.session.updated_at || 0)
  );
  try{localStorage.setItem('hermes-webui-session',S.session.session_id);}catch(_){}
  _setActiveSessionUrl(S.session.session_id);
  if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);


  if(!await _restoreLoadedSession({
    sid,activeStreamId,_keepStaleUntilLoaded,_loadGeneration,_isCurrentLoad,sameSessionForceReload,_prefetchedMessages,
  })) return;

  // Sync context usage indicator from session data
  const _s=S.session;
  if(_s&&typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    const _pickPositive=(latest,stored,dflt=0)=>Number(latest)>0?latest:(Number(stored)>0?stored:dflt);
    _syncCtxIndicator({
      input_tokens:      _pick(u.input_tokens,      _s.input_tokens),
      output_tokens:     _pick(u.output_tokens,     _s.output_tokens),
      estimated_cost:    _pick(u.estimated_cost,    _s.estimated_cost),
      cache_read_tokens: _pick(u.cache_read_tokens, _s.cache_read_tokens),
      cache_write_tokens:_pick(u.cache_write_tokens,_s.cache_write_tokens),
      cache_hit_percent: _pick(u.cache_hit_percent, _s.cache_hit_percent, null),
      context_length:    _pickPositive(u.context_length, _s.context_length),
      last_prompt_tokens:_pick(u.last_prompt_tokens,_s.last_prompt_tokens),
      post_compression_context_tokens_estimate:_s.post_compression_context_tokens_estimate||null,
      threshold_tokens:  _pick(_s.threshold_tokens,  u.threshold_tokens),
    });
  }
  if(typeof _renderPendingPromptsForActiveSession==='function') _renderPendingPromptsForActiveSession();

  // Restore server-persisted composer draft (synced across clients + survives refresh).
  // Pass sid so _restoreComposerDraft can skip if this session is mid-load (guards
  // against stale writes from slow responses racing to restore the previous draft).
  const _draft = S.session && S.session.composer_draft;
  if (_draft && (typeof _restoreComposerDraft === 'function')) {
    _restoreComposerDraft(_draft, sid, {preserveActiveInput:!!opts.preserveActiveInput || (currentSid===sid&&forceReload)});
  }

  // Clear the in-flight session marker now that this load has completed (#1060).
  if (_isCurrentLoad()) sessionLoadState.loadingSessionId = null;

  // Re-acknowledge the visit after the async message-load gap. A deferred
  // sidebar /api/sessions poll can land while _ensureMessagesLoaded is in
  // flight and re-mark the open session unread; re-syncing here clears that
  // sticky dot once the transcript is settled (#4946).
  //
  // Gate the final ack on _isSessionActivelyViewedForList(sid): a completion
  // that lands while _ensureMessagesLoaded() is in flight AND the tab then goes
  // hidden is correctly marked unread — an UNCONDITIONAL ack here would wrongly
  // clear that hidden-tab-completion marker. Only clear when the session is
  // still actively viewed. (#5917 gate finding)
  if (
    S.session && S.session.session_id === sid &&
    (typeof _isSessionActivelyViewedForList !== 'function' || _isSessionActivelyViewedForList(sid))
  ) {
    _acknowledgeSessionVisit(
      sid,
      Number(S.session.message_count || 0),
      Number(S.session.last_message_at || S.session.updated_at || 0)
    );
  }

  if(typeof renderSessionArtifacts==='function') renderSessionArtifacts();

  // ── Cross-channel handoff hint ──
  // After session fully loaded, check if this is a messaging session with
  // enough conversation rounds to warrant a handoff hint bar.
  if (S.session && _isMessagingSession(S.session)) {
    _checkAndShowHandoffHint(sid);
  } else {
    _hideHandoffHint();
  }
  // Extension post-load hook
  if(!opts.skipExtHooks && typeof _hermesNotifySessionOpen==='function'){
    try{ _hermesNotifySessionOpen(sid, S.session, {loaded:true, opts:opts}); }catch(_){}
  }
}


export { loadSession };
