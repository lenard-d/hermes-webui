"""Approval and clarification response resolution route domain."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.clarify import resolve_clarify
    from api.helpers import bad, j
    from api.sessions.store import get_session
    from api.route_approvals import (
        _GATEWAY_MIRROR_FLAG,
        _approval_sse_notify_locked,
        _gateway_mirrored_pending_run_id,
        _gateway_queues,
        _lock,
        _pending,
        _permanent_approved,
        approve_permanent,
        approve_session,
        reconcile_gateway_pending_mirror_locked,
        resolve_gateway_approval,
        save_permanent_allowlist,
    )
    from api.sessions.events import publish_session_list_changed


def _resolve_approval_legacy(sid: str, approval_id: str, choice: str) -> bool:
    """Resolve an approval through the existing callback path.

    Slice 3b keeps the RuntimeAdapter as a protocol translator: it delegates to
    this legacy helper rather than owning approval queues or callback state.
    """
    # Pop the targeted entry from the pending queue by approval_id. Old clients
    # that omit approval_id still resolve the oldest entry for compatibility.
    pending = None
    found_target = False
    gateway_keys = []
    with _lock:
        reconcile_gateway_pending_mirror_locked(sid)
        queue = _pending.get(sid)
        if isinstance(queue, list):
            if approval_id:
                # Find and remove the specific entry by approval_id.
                for i, entry in enumerate(queue):
                    if entry.get("approval_id") == approval_id:
                        pending = queue.pop(i)
                        found_target = True
                        break
                else:
                    # A stale explicit id must not accidentally approve the
                    # oldest queued command; duplicate/stale responses are
                    # bounded as not-active by the adapter route.
                    pending = None
            else:
                pending = queue.pop(0) if queue else None
                found_target = pending is not None
            if not queue:
                _pending.pop(sid, None)
        elif queue:
            # Legacy single-dict value.
            if not approval_id or queue.get("approval_id") == approval_id:
                pending = _pending.pop(sid, None)
                found_target = pending is not None
        # When no _pending entry found AND no explicit approval_id was
        # given, peek into _gateway_queues for pattern_keys so legacy
        # no-id clients still work. When approval_id IS given but not
        # found, the caller sent a stale/duplicate id — do NOT fall
        # through to the gateway queue, or a stale click on approval A
        # would resolve the unrelated live approval B.
        if not pending and not approval_id:
            gw_queue = _gateway_queues.get(sid)
            if gw_queue and len(gw_queue) > 0:
                gw_entry = gw_queue[0]
                # _gateway_queues stores _ApprovalEntry objects; their
                # .data dict carries command, pattern_key, pattern_keys.
                gw_data = getattr(gw_entry, 'data', None) or {}
                gateway_keys = gw_data.get("pattern_keys") or [gw_data.get("pattern_key", "")]
                # Peek is not strict — a concurrent resolver may pop a
                # different gateway entry before we reach
                # resolve_gateway_approval below, but approve_session is
                # idempotent over the session key set so the outcome is
                # the same regardless of which entry wins the race.
                found_target = True
        # Notify SSE subscribers of the new head (or empty state) so the UI
        # surfaces any trailing approvals that were queued behind this one
        # without waiting for the next submit_pending. Without this, a parallel
        # tool-call scenario (#527) would leave the second approval invisible
        # in the SSE path until the next event ever fired (the agent thread
        # would be parked indefinitely from the user's perspective).
        if isinstance(_pending.get(sid), list) and _pending[sid]:
            _approval_sse_notify_locked(sid, _pending[sid][0], len(_pending[sid]))
        else:
            _approval_sse_notify_locked(sid, None, 0)

    # Collect keys from both _pending and _gateway_queues
    keys_from_pending = pending.get("pattern_keys") or [pending.get("pattern_key", "")] if pending else []
    all_keys = [k for k in keys_from_pending if k] + [k for k in gateway_keys if k]
    if choice == "session":
        for k in all_keys:
            approve_session(sid, k)
    elif choice == "always":
        for k in all_keys:
            approve_session(sid, k)
            approve_permanent(k)
        save_permanent_allowlist(_permanent_approved)
    # choice == "once": no persistence — approval lasts this single call only.
    # resolve_gateway_approval() below unblocks the parked agent thread for
    # every choice, so "once" still lets the current tool run; we just must not
    # call approve_session() here, or the next matching guarded call would find
    # the pattern already session-approved and skip its approval card (#6017).
    # Unblock the agent thread waiting in the gateway approval queue.
    # This is the primary signal when streaming is active — the agent
    # thread is parked in entry.event.wait() and needs to be woken up.
    gateway_resolved = 0
    if found_target or not approval_id:
        gateway_resolved = resolve_gateway_approval(sid, choice, resolve_all=False) or 0
    # Keep the historical no-id response path truthy for old clients/tests while
    # making stale explicit ids bounded as not-active for Slice 3b.
    resolved = bool(pending) or bool(gateway_resolved) or not bool(approval_id)
    if resolved:
        publish_session_list_changed("attention_resolved")
    return resolved


_GATEWAY_APPROVAL_RELAY_UNAVAILABLE = (
    "Gateway approval could not be relayed because the active run is unavailable. "
    "Reopen the session or retry after it reconnects."
)


def _gateway_pending_approval_without_run_id(sid: str, approval_id: str) -> bool:
    with _lock:
        reconcile_gateway_pending_mirror_locked(sid)
        queue = _pending.get(sid)
        if isinstance(queue, list):
            entries = queue
        elif queue:
            entries = [queue]
        else:
            entries = []
        if approval_id:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("approval_id") == approval_id:
                    return bool(entry.get(_GATEWAY_MIRROR_FLAG))
            return False
        if not entries or not isinstance(entries[0], dict):
            return False
        return bool(entries[0].get(_GATEWAY_MIRROR_FLAG))


def _session_has_pending_approval(sid: str) -> bool:
    """True when the session still has any live pending approval to act on.

    Used to tell a benign STALE-CARD click (the card's approval already
    resolved or its stream ended, so nothing is pending) apart from a stale
    explicit-id click made WHILE a different approval is still live (which must
    stay unresolved so it can't accidentally approve the wrong command — #527).
    Reconciles the gateway mirror first so a purged orphan is not counted.
    """
    with _lock:
        reconcile_gateway_pending_mirror_locked(sid)
        queue = _pending.get(sid)
        if isinstance(queue, list):
            if queue:
                return True
        elif queue:
            return True
        gw_queue = _gateway_queues.get(sid)
        return bool(gw_queue)


def _handle_approval_respond(handler, body):
    sid = body.get("session_id", "")
    if not sid:
        return bad(handler, "session_id is required")
    choice = body.get("choice", "deny")
    if choice not in ("once", "session", "always", "deny"):
        return bad(handler, f"Invalid choice: {choice}")
    approval_id = body.get("approval_id", "")

    # Gateway relay: forward choice to the runs API when session has an active run,
    # or recover the run_id from the mirrored gateway approval entry if the
    # stream pointer has already been cleared.
    try:
        import api.runs as run_domain
        from api.config import get_config as _get_config
        s = get_session(sid)
        _run_id = None
        if s is not None:
            active_sid = getattr(s, "active_stream_id", None)
            if active_sid:
                _run_id = run_domain.gateway_run_for_stream(active_sid)
            if not _run_id and approval_id:
                _run_id = _gateway_mirrored_pending_run_id(sid, approval_id)
        if _run_id:
            if not approval_id:
                return bad(handler, "approval_id is required for gateway approvals")
            from api.runner_client import HttpRunnerClient, RunnerClientError
            _cfg = _get_config()
            _base = run_domain.gateway_base_url(_cfg)
            _key = run_domain.gateway_api_key()
            try:
                HttpRunnerClient(base_url=_base, api_key=_key).respond_approval(_run_id, approval_id, choice)
            except (RunnerClientError, ValueError) as exc:
                return j(handler, {"ok": False, "choice": choice, "relayed": True, "error": str(exc)}, status=502)
            # The outbound relay only resumes the remote run; the local mirror
            # still needs the same cleanup path so the parked entry, mirrored
            # card, and agent signal all settle here too.
            _resolve_approval_legacy(sid, approval_id, choice)
            return j(handler, {"ok": True, "choice": choice, "relayed": True})
        # Only a still-mirrored gateway approval with a missing run should 409;
        # stale or empty gateway clicks fall through to local resolution.
        if run_domain.webui_gateway_chat_enabled(
            _get_config()
        ) and _gateway_pending_approval_without_run_id(
            sid, approval_id
        ):
            return j(
                handler,
                {
                    "ok": False,
                    "choice": choice,
                    "relayed": False,
                    "code": "gateway_run_unavailable",
                    "error": _GATEWAY_APPROVAL_RELAY_UNAVAILABLE,
                },
                status=409,
            )
    except Exception:
        pass  # fall through to local approval path

    from api.runs.adapter import LegacyJournalRuntimeAdapter, runtime_adapter_enabled

    if runtime_adapter_enabled():
        adapter = LegacyJournalRuntimeAdapter(approval_delegate=_resolve_approval_legacy)
        ok = adapter.respond_approval(sid, approval_id, choice).accepted
    else:
        ok = _resolve_approval_legacy(sid, approval_id, choice)
    if not ok and not _session_has_pending_approval(sid):
        # The local resolution path returns False when an explicit approval_id
        # was sent but no matching pending entry exists. There are two distinct
        # causes, and only one is an error:
        #   (a) a STALE CARD — the approval the card was rendered from already
        #       resolved or its stream ended (cancel / fork / provider error /
        #       completion while pending), so the agent's gateway entry was
        #       dropped and reconcile purged the mirror. Nothing is pending for
        #       this session anymore. Before #4771 the frontend was
        #       fire-and-forget and this silently cleared the card; #4771 began
        #       surfacing the bare {ok:false} as "Approval response not
        #       accepted." with a STUCK card (reported by Jamie on .666 / b3nw;
        #       the local-backend variant of #4948).
        #   (b) a STALE EXPLICIT ID while a DIFFERENT approval IS live — that
        #       MUST stay ok:false so a stale click on resolved approval A can
        #       never resolve the unrelated live approval B (#527 guard).
        # Distinguish them: when the session has NO pending approval at all,
        # the click is benign — report it resolved so the UI clears the orphan
        # card instead of dead-ending. When something IS still pending, keep
        # the protective ok:false. `stale_cleared` lets the frontend log/branch
        # without showing an error toast.
        return j(handler, {"ok": True, "choice": choice, "stale_cleared": True})
    return j(handler, {"ok": ok, "choice": choice})


def _resolve_clarify_legacy(sid: str, clarify_id: str, response: str) -> bool:
    """Resolve clarify through the existing callback path without new state."""
    # When a stable clarify_id is provided, match the specific entry so stale
    # or late responses from the frontend are reliably rejected (issue #2639).
    if clarify_id:
        from api.clarify import resolve_clarify_by_id
        return resolve_clarify_by_id(sid, clarify_id, response)
    # Legacy path: resolve the oldest pending entry.  Return the REAL result
    # instead of the old unconditional True so the frontend can detect when
    # there is no pending prompt to resolve.
    resolved = resolve_clarify(sid, response, resolve_all=False)
    return bool(resolved)


def _handle_clarify_respond(handler, body):
    sid = body.get("session_id", "")
    if not sid:
        return bad(handler, "session_id is required")
    response = body.get("response")
    if response is None:
        response = body.get("answer")
    if response is None:
        response = body.get("choice")
    response = str(response or "").strip()
    if not response:
        return bad(handler, "response is required")
    clarify_id = body.get("clarify_id", "")

    from api.runs.adapter import LegacyJournalRuntimeAdapter, runtime_adapter_enabled

    if runtime_adapter_enabled():
        adapter = LegacyJournalRuntimeAdapter(clarify_delegate=_resolve_clarify_legacy)
        ok = adapter.respond_clarify(sid, clarify_id, response).accepted
    else:
        ok = _resolve_clarify_legacy(sid, clarify_id, response)

    if not ok:
        # Both the runtime adapter and legacy paths set ok=False for
        # stale/expired/wrong-session responses.  The 409 status applies
        # uniformly regardless of which path resolved the clarify request.
        return j(handler, {
            "ok": False,
            "error": "Clarification prompt expired or not found. The agent may have already proceeded.",
            "stale": True,
        }, status=409)

    return j(handler, {"ok": True, "response": response})


__routes_exports__ = (
    "_resolve_approval_legacy",
    "_GATEWAY_APPROVAL_RELAY_UNAVAILABLE",
    "_gateway_pending_approval_without_run_id",
    "_session_has_pending_approval",
    "_handle_approval_respond",
    "_resolve_clarify_legacy",
    "_handle_clarify_respond",
)
