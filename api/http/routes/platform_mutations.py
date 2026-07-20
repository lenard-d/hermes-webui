"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.http.shell import load_saved_prompts, save_saved_prompts


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    SESSION_DIR = ctx["SESSION_DIR"]
    _active_state_db_path = ctx["_active_state_db_path"]
    _build_share_metadata_sidecar = ctx["_build_share_metadata_sidecar"]
    _handle_escape_authorize = ctx["_handle_escape_authorize"]
    _kanban_unknown_endpoint = ctx["_kanban_unknown_endpoint"]
    _publish_session_list_changed = ctx["_publish_session_list_changed"]
    _resolve_share_session_pair = ctx["_resolve_share_session_pair"]
    bad = ctx["bad"]
    copy = ctx["copy"]
    create_or_refresh_share = ctx["create_or_refresh_share"]
    edit_session = ctx["edit_session"]
    j = ctx["j"]
    load_settings = ctx["load_settings"]
    logger = ctx["logger"]
    revoke_share = ctx["revoke_share"]
    time = ctx["time"]
    uuid = ctx["uuid"]

    if parsed.path == "/api/escape/authorize":
        return _handle_escape_authorize(handler, parsed, body)

    if parsed.path == "/api/updates/check":
        settings = load_settings()
        if not settings.get("check_for_updates", True):
            force = bool(body.get("force", False)) if isinstance(body, dict) else False
            if force:
                # Manual force-check bypasses auto-check toggle (#6082)
                pass
            else:
                return j(handler, {"disabled": True})
        include_agent_updates = not bool(settings.get("ignore_agent_updates"))
        force = bool(body.get("force", False))
        # Allow the client to pass the channel explicitly in the POST body. This
        # avoids a race on channel switch: the Settings dropdown re-checks
        # immediately, but its autosave PUT (debounced) may not have landed
        # server-side yet, so reading the saved setting here could answer for the
        # OLD channel. An explicit body channel (validated against the enum) wins;
        # otherwise fall back to the saved setting. (Fable UX gate.)
        channel = body.get("channel") if isinstance(body, dict) else None
        if channel not in ("stable", "experimental"):
            channel = settings.get("update_channel")
        from api.updates import check_for_updates

        return j(
            handler,
            check_for_updates(
                force=force, include_agent=include_agent_updates, channel=channel
            ),
        )

    if parsed.path == "/api/extensions/toggle":
        from api.extensions import ExtensionToggleError, set_extension_user_enabled

        try:
            return j(
                handler,
                set_extension_user_enabled(body.get("id"), body.get("enabled")),
            )
        except ExtensionToggleError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension toggle failed")
            return bad(handler, "Failed to update extension state", status=500)

    if parsed.path == "/api/extensions/sidecar-proxy-consent":
        from api.extensions import (
            ExtensionSidecarProxyError,
            set_extension_sidecar_proxy_consent,
        )

        try:
            return j(
                handler,
                set_extension_sidecar_proxy_consent(
                    body.get("id"),
                    body.get("approved"),
                ),
            )
        except ExtensionSidecarProxyError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension sidecar proxy consent update failed")
            return bad(handler, "Failed to update extension state", status=500)

    if parsed.path == "/api/extensions/install":
        from api.extensions import ExtensionInstallError, install_extension

        try:
            return j(
                handler,
                install_extension(
                    body.get("id"), body.get("download_url"), body.get("sha256")
                ),
            )
        except ExtensionInstallError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension install failed")
            return bad(handler, "Failed to install extension", status=500)

    if parsed.path == "/api/extensions/uninstall":
        from api.extensions import ExtensionInstallError, uninstall_extension

        try:
            return j(
                handler,
                uninstall_extension(body.get("id")),
            )
        except ExtensionInstallError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension uninstall failed")
            return bad(handler, "Failed to uninstall extension", status=500)

    if parsed.path == "/api/session/recovery/repair-safe":
        from api.sessions import repair_safe_session_recovery

        result = repair_safe_session_recovery(
            SESSION_DIR, state_db_path=_active_state_db_path()
        )
        return j(handler, result, status=200 if result.get("clean") else 409)

    if parsed.path.startswith("/api/kanban/"):
        from api.kanban import handle_kanban_post

        result = handle_kanban_post(handler, parsed, body)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "POST")
        return True
    if parsed.path == "/api/dashboard/config":
        from api import dashboard_probe

        try:
            j(handler, dashboard_probe.save_dashboard_config(body))
        except ValueError as exc:
            bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("dashboard config save failed")
            bad(handler, str(exc), status=500)
        return True

    if parsed.path == "/api/prompts":
        text = str(body.get("text") or "").strip()
        label = str(body.get("label") or "").strip()
        if not text:
            return bad(handler, "text is required")
        if len(text) > 8000:
            return bad(handler, "text too long (max 8000 chars)")
        prompts = load_saved_prompts()
        if len(prompts) >= 200:
            return bad(handler, "saved prompts limit reached (max 200)")
        new_prompt = {
            "id": uuid.uuid4().hex[:12],
            "label": label or text[:60],
            "text": text,
            "created_at": time.time(),
        }
        prompts.append(new_prompt)
        save_saved_prompts(prompts)
        return j(handler, {"ok": True, "prompt": new_prompt})

    if parsed.path == "/api/share/create":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return bad(handler, "session_id is required", 400)
        try:
            snapshot_session, stored_session, cli_meta = _resolve_share_session_pair(
                sid, handler
            )
        except KeyError:
            return bad(handler, "Session not found", 404)
        # A pre-resolved WebUI session is never a missing-record seed: delete
        # may have won after resolution but before this owner is acquired. Only
        # a genuinely external session may materialize its first local sidecar.
        share_metadata_seed = None
        if stored_session is None:
            share_metadata_seed = _build_share_metadata_sidecar(
                sid,
                snapshot_session,
                cli_meta=cli_meta,
            )
        share_meta = None
        had_existing_share = False
        try:
            with edit_session(
                sid,
                session=share_metadata_seed,
                touch_updated_at=False,
            ) as persisted_session:
                # Keep transcript preparation outside the owner, but resolve
                # the token and commit the public file under the same owner as
                # its session metadata. Create and revoke can no longer pass
                # each other between share-file I/O and sidecar persistence.
                share_snapshot = copy.copy(snapshot_session)
                share_snapshot.share_token = persisted_session.share_token
                had_existing_share = bool(
                    str(persisted_session.share_token or "").strip()
                )
                share_meta = create_or_refresh_share(share_snapshot)
                persisted_session.share_token = share_meta["share_token"]
                persisted_session.share_created_at = share_meta["share_created_at"]
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as exc:
            return bad(handler, str(exc), 400)
        except Exception:
            logger.exception("share metadata persistence failed for session %s", sid)
            if share_meta is not None and not had_existing_share:
                # The public snapshot was committed first so its sanitized I/O
                # completed under the owner before its matching sidecar save.
                # If that save fails, fail closed instead of leaving an
                # untracked public conversation.
                compensation_session = copy.copy(snapshot_session)
                compensation_session.share_token = share_meta["share_token"]
                try:
                    revoke_share(compensation_session)
                except Exception:
                    logger.exception(
                        "failed to compensate unpersisted share for session %s",
                        sid,
                    )
            return bad(
                handler,
                "Failed to persist session metadata for shared conversation",
                500,
            )
        _publish_session_list_changed(
            "session_share_create",
            profile=getattr(persisted_session, "profile", None),
            session_id=sid,
        )
        response_session = copy.copy(persisted_session)
        response_session.messages = list(
            getattr(snapshot_session, "messages", None) or []
        )
        return j(
            handler,
            {
                "ok": True,
                "share": {
                    "token": share_meta["share_token"],
                    "url": f"/share/{share_meta['share_token']}",
                    "title": share_meta["share_title"],
                    "message_count": share_meta["share_message_count"],
                    "created_at": share_meta["share_created_at"],
                    "updated_at": share_meta["share_updated_at"],
                },
                "session": response_session.compact()
                | {"messages": response_session.messages},
            },
        )

    if parsed.path == "/api/share/revoke":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return bad(handler, "session_id is required", 400)
        try:
            snapshot_session, stored_session, cli_meta = _resolve_share_session_pair(
                sid, handler
            )
        except KeyError:
            return bad(handler, "Session not found", 404)
        share_metadata_seed = None
        if stored_session is None:
            token = str(getattr(snapshot_session, "share_token", "") or "").strip()
            if not token:
                return bad(handler, "Session not found", 404)
            share_metadata_seed = _build_share_metadata_sidecar(
                sid,
                snapshot_session,
                cli_meta=cli_meta,
            )
            share_metadata_seed.share_token = token
            share_metadata_seed.share_created_at = getattr(
                snapshot_session,
                "share_created_at",
                None,
            )
        share_revocation_completed = False
        try:
            with edit_session(
                sid,
                session=share_metadata_seed,
                touch_updated_at=False,
            ) as target_session:
                revoke_share(target_session)
                share_revocation_completed = True
                target_session.share_token = None
                target_session.share_created_at = None
        except KeyError:
            return bad(handler, "Session not found", 404)
        except Exception:
            if not share_revocation_completed:
                logger.exception("share revocation failed for session %s", sid)
                return bad(handler, "Failed to revoke shared conversation", 500)
            # The public file was revoked atomically before __exit__ attempted
            # the sidecar save. Report that partial state truthfully; a retry
            # can clear the remaining local token without republishing it.
            logger.exception(
                "revoked share metadata cleanup failed for session %s",
                sid,
            )
            return bad(
                handler,
                "Share was revoked, but session metadata could not be updated",
                500,
            )
        _publish_session_list_changed(
            "session_share_revoke",
            profile=getattr(target_session, "profile", None),
            session_id=sid,
        )
        response_session = copy.copy(target_session)
        response_session.messages = list(
            getattr(snapshot_session, "messages", None) or []
        )
        return j(
            handler,
            {
                "ok": True,
                "session": response_session.compact()
                | {"messages": response_session.messages},
            },
        )
    return UNHANDLED
