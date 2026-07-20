"""Browser chat-start and synchronous-chat HTTP owners."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations

from api.sessions import foreign_session_access


def _handle_chat_start(handler, body, diag=None):
    try:
        diag.stage("validate_session_id") if diag else None
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        # Reject a stale local Agent runtime before materialising, claiming, or
        # mutating any session state. Gateway-backed turns run in the gateway's
        # process and do not depend on this WebUI process's imported checkout.
        stale_response = _agent_runtime_barrier_response(runner_local_owned=True)
        if stale_response is not None:
            return j(handler, stale_response, status=409)
        diag.stage("get_session") if diag else None
        try:
            s = _get_or_materialize_session(body["session_id"], refresh_cli_messages=True)
        except KeyError:
            # No WebUI sidecar. If this is a foreign-origin session (CLI,
            # TUI, Desktop) with recoverable state.db messages, claim it by
            # materialising a WebUI-owned Session and persisting it as a
            # sidecar. This closes the GET-vs-POST asymmetry where a
            # TUI/Desktop session loads read-only via GET /api/session but
            # 404s on the first POST /api/chat/start, making the typed
            # message disappear into the empty state.
            synth, reason = foreign_session_access.claim(body["session_id"])
            if synth is None:
                # 'was_webui' (deleted WebUI session, client should self-heal
                # via the existing 404 path), 'no_foreign_state' (sid has
                # no recoverable state anywhere), or 'invalid_sid' (path
                # safety violation). All collapse to 404 — the client only
                # knows the right thing to do for "this session is gone".
                return bad(handler, "Session not found", 404)
            if reason == "not_claimable":
                # Foreign store says this session is read-only / owned by
                # a non-WebUI process (messaging, claude_code,
                # external_agent, cron, gateway/unknown, or explicit
                # read_only flag). The session is real and viewable, but
                # the WebUI must not take write ownership of it — that
                # would be an ownership-boundary violation (#4911 review).
                # 403 (not 404) because 404 triggers the frontend's
                # empty-state self-heal handler which strips the URL and
                # clears localStorage; for a legitimately-listed read-only
                # session the user should keep their URL and see a refusal,
                # not have their session vanish.
                return bad(
                    handler,
                    "session is read-only in its foreign store; cannot be claimed writeable in WebUI",
                    403,
                )
            try:
                synth.save()
            except Exception as _save_err:
                # Persisting the sidecar failed: surface a generic 500 to
                # the client (paths sanitised, see _sanitize_error) and log
                # the full exception server-side. Returning the raw str(exc)
                # would leak /root/.hermes/webui/sessions/<sid>.json or any
                # other absolute filesystem path the OSError happened to
                # carry — #4911 review feedback.
                logger.exception(
                    "failed to persist materialised sidecar for foreign session %s",
                    body["session_id"],
                )
                return bad(
                    handler,
                    f"failed to claim session: {_sanitize_error(_save_err)}",
                    500,
                )
            s = synth
            try:
                with LOCK:
                    SESSIONS[s.session_id] = s
                    SESSIONS.move_to_end(s.session_id)
            except Exception:
                # If the in-memory LRU refuses the new session, fall through
                # with the just-persisted sidecar; _start_run will load it
                # from disk if needed.
                pass
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot be continued from WebUI", 403)
        diag.stage("validate_profile") if diag else None
        requested_profile = str(body.get("profile") or "").strip()
        active_profile = _get_active_profile_name()
        if requested_profile:
            try:
                from api.profiles import is_valid_profile_id

                if requested_profile != "default" and not is_valid_profile_id(requested_profile):
                    return bad(handler, "invalid profile", 400)
            except ImportError:
                requested_profile = ""
        session_profile = getattr(s, "profile", None)
        has_persisted_turns = bool(
            getattr(s, "messages", None)
            or getattr(s, "context_messages", None)
            or getattr(s, "pending_user_message", None)
        )
        if not _session_visible_to_active_profile(session_profile, handler):
            if (
                requested_profile
                and _profiles_match(requested_profile, active_profile)
                and not has_persisted_turns
            ):
                # Empty placeholders can still be retagged when the
                # requested profile matches the active request profile.
                s.profile = requested_profile
            else:
                return bad(handler, "Session not found", 404)
        diag.stage("normalize_message") if diag else None
        msg = str(body.get("message", "")).strip()
        if not msg:
            return bad(handler, "message is required")
        diag.stage("normalize_attachments") if diag else None
        attachments = _normalize_chat_attachments(body.get("attachments") or [])[:20]
        recovery = compression_recovery_payload_for_session(s)
        if recovery and not attachments and is_generic_continuation_intent(msg):
            return j(
                handler,
                {
                    "error": "This session exhausted context compression. Start a focused continuation, then describe the next narrow task.",
                    "type": "compression_recovery_required",
                    "recommended_recovery_action": recovery.get("recommended_action"),
                    "compression_recovery": recovery,
                    "session_id": getattr(s, "session_id", body["session_id"]),
                },
                status=409,
            )
        diag.stage("resolve_workspace") if diag else None
        try:
            workspace = _resolve_chat_workspace_with_recovery(s, body.get("workspace"))
        except ValueError as e:
            return bad(handler, str(e))
        requested_model = body.get("model") or s.model
        requested_provider = (
            body.get("model_provider")
            if "model_provider" in body
            else getattr(s, "model_provider", None)
        )
        _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, requested_provider)
        explicit_model_pick = bool(body.get("explicit_model_pick"))
        moa_config = None
        gateway_chat_enabled = webui_gateway_chat_enabled(get_config())
        if body.get("moa_config"):
            if gateway_chat_enabled:
                return bad(handler, "MoA override is unavailable on gateway-backed sessions", 409)
            from api.commands import resolve_moa_config

            try:
                moa_config = resolve_moa_config()
            except RuntimeError as e:
                return bad(handler, str(e), 503)
        diag.stage("resolve_model_provider") if diag else None
        model, model_provider, normalized_model = _resolve_compatible_session_model_state(
            requested_model,
            requested_provider,
            profile_provider=_pp_provider,
            profile_default_model=_pp_default,
            profile_config=_pp_cfg,
            explicit_model_pick=explicit_model_pick,
        )
        # #5979: record a SIGNATURE of the deliberately-picked model+provider so
        # the streaming resolver can preserve a custom-proxy vendor namespace on a
        # cold catalog — but ONLY while the routing context still matches. On a
        # fresh explicit pick, stamp the signature of the resolved model+provider;
        # otherwise leave any prior signature in place (it self-invalidates when
        # the model/provider changes, since the streaming side recomputes and
        # compares). This survives same-model follow-up sends (the onchange marker
        # is one-shot) yet can't outlive a real switch.
        try:
            if explicit_model_pick:
                from api.sessions.store import model_explicit_pick_signature as _mk_sig
                s.model_explicit_pick_signature = _mk_sig(model, model_provider)
        except Exception:
            pass
        catalog_profile_provider = _pp_provider
        if catalog_profile_provider is None and isinstance(_pp_cfg, dict):
            profile_model_config = _pp_cfg.get("model") or {}
            if isinstance(profile_model_config, dict):
                catalog_profile_provider = profile_model_config.get("provider")
        model_provider = _repair_foreign_session_model_provider(
            s,
            requested_model=requested_model,
            requested_provider=requested_provider,
            resolved_model=model,
            resolved_provider=model_provider,
            explicit_model_pick=explicit_model_pick,
            profile_provider=catalog_profile_provider,
        )
        if model_provider == "moa" and moa_config is None:
            if webui_gateway_chat_enabled(get_config()):
                return bad(handler, "MoA override is unavailable on gateway-backed sessions", 409)
            from api.commands import resolve_moa_config

            try:
                moa_config = resolve_moa_config(model)
            except RuntimeError as e:
                return bad(handler, str(e), 503)
        # NOTE: runtime-adapter selection is delegated to _start_run (shared
        # with start_session_turn so both entry points behave identically
        # under runtime_adapter_enabled() / runtime_adapter_runner_enabled()
        # — Q-2979-A2 / Copilot discussion_r3305864087/r3305864173).
        start_run_kwargs = {
            "msg": msg,
            "attachments": attachments,
            "workspace": workspace,
            "model": model,
            "model_provider": model_provider,
            "normalized_model": normalized_model,
            "source": "webui",
            "route": "/api/chat/start",
            "diag": diag,
        }
        if not gateway_chat_enabled and moa_config is not None:
            start_run_kwargs["moa_config"] = moa_config
        recovery_cleared_for_start = None
        def _restore_cleared_recovery():
            if recovery_cleared_for_start is None:
                return None
            s.compression_recovery = recovery_cleared_for_start
            s.recommended_recovery_action = recovery_cleared_for_start.get("recommended_action")
            try:
                s.save()
            except Exception as restore_err:
                logger.exception("failed to restore compression recovery after chat start rejection for %s", getattr(s, "session_id", None))
                return restore_err
            return None

        if recovery:
            recovery_cleared_for_start = copy.deepcopy(recovery)
            clear_compression_recovery(s)
        try:
            response = _start_run(
                s,
                **start_run_kwargs,
            )
        except Exception:
            _restore_cleared_recovery()
            raise
        # Map adapter-selection NotImplementedError (501) onto the legacy
        # bad-request response shape that this route exposed historically
        # before the helper extraction.
        if response.get("_status") == 501 and "error" in response:
            restore_err = _restore_cleared_recovery()
            if restore_err is not None:
                return bad(handler, f"failed to restore compression recovery: {_sanitize_error(restore_err)}", 500)
            return j(handler, {"error": response["error"]}, status=501)
        status = int(response.pop("_status", 200) or 200)
        if status >= 400 and recovery_cleared_for_start is not None:
            restore_err = _restore_cleared_recovery()
            if restore_err is not None:
                return bad(handler, f"failed to restore compression recovery: {_sanitize_error(restore_err)}", 500)
        diag.stage("response_write") if diag else None
        return j(handler, response, status=status)
    finally:
        if diag:
            diag.finish()



def _resolve_chat_workspace_with_recovery(s, requested_workspace) -> str:
    """Recover stale implicit session workspaces without hiding explicit errors."""
    from api.runs.turn_input import resolve_chat_workspace

    return resolve_chat_workspace(
        s,
        requested_workspace,
        resolve_workspace=resolve_trusted_workspace,
        last_workspace=get_last_workspace,
    )


def _normalize_chat_attachments(raw_attachments):
    """Normalize attachment payloads from the browser.

    Older clients send a list of filenames. Newer clients send upload result
    objects containing name/path/mime/size so image attachments can be supplied
    to Hermes as native multimodal inputs for the current turn.
    """
    from api.runs.turn_input import normalize_chat_attachments

    return normalize_chat_attachments(raw_attachments)


def _handle_chat_sync(handler, body):
    """Fallback synchronous chat endpoint (POST /api/chat). Not used by frontend."""
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    if foreign_session_access.is_view_only(str(body.get("session_id") or "")):
        return bad(handler, "Subagent sessions are view-only and cannot be written from WebUI", 400)
    s = get_session(body["session_id"])
    msg = str(body.get("message", "")).strip()
    if not msg:
        return j(handler, {"error": "empty message"}, status=400)
    try:
        workspace = str(resolve_trusted_workspace(body.get("workspace") or s.workspace))
    except ValueError as e:
        return bad(handler, str(e))
    with _get_session_agent_lock(s.session_id):
        s.workspace = workspace
        _sync_requested_provider = (
            body.get("model_provider") if "model_provider" in body else getattr(s, "model_provider", None)
        )
        _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, _sync_requested_provider)
        model, model_provider = _resolve_compatible_session_model_state(
            body.get("model") or s.model,
            _sync_requested_provider,
            profile_provider=_pp_provider,
            profile_default_model=_pp_default,
            profile_config=_pp_cfg,
        )[:2]
        s.model = model
        s.model_provider = model_provider
    from api.runs.synchronous import run_synchronous_chat

    return j(
        handler,
        run_synchronous_chat(
            s,
            msg,
            enabled_toolsets=_resolve_cli_toolsets(),
        ),
    )
