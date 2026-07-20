"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    _ensure_agent_cron_import_path = ctx["_ensure_agent_cron_import_path"]
    _handle_approval_respond = ctx["_handle_approval_respond"]
    _handle_clarify_respond = ctx["_handle_clarify_respond"]
    _handle_create_dir = ctx["_handle_create_dir"]
    _handle_cron_create = ctx["_handle_cron_create"]
    _handle_cron_delete = ctx["_handle_cron_delete"]
    _handle_cron_pause = ctx["_handle_cron_pause"]
    _handle_cron_resume = ctx["_handle_cron_resume"]
    _handle_cron_run = ctx["_handle_cron_run"]
    _handle_cron_update = ctx["_handle_cron_update"]
    _handle_file_create = ctx["_handle_file_create"]
    _handle_file_delete = ctx["_handle_file_delete"]
    _handle_file_move = ctx["_handle_file_move"]
    _handle_file_open_vscode = ctx["_handle_file_open_vscode"]
    _handle_file_path = ctx["_handle_file_path"]
    _handle_file_rename = ctx["_handle_file_rename"]
    _handle_file_reveal = ctx["_handle_file_reveal"]
    _handle_file_save = ctx["_handle_file_save"]
    _handle_gateway_lifecycle = ctx["_handle_gateway_lifecycle"]
    _handle_git_checkout = ctx["_handle_git_checkout"]
    _handle_git_commit = ctx["_handle_git_commit"]
    _handle_git_commit_message = ctx["_handle_git_commit_message"]
    _handle_git_commit_message_selected = ctx["_handle_git_commit_message_selected"]
    _handle_git_commit_selected = ctx["_handle_git_commit_selected"]
    _handle_git_discard = ctx["_handle_git_discard"]
    _handle_git_remote_action = ctx["_handle_git_remote_action"]
    _handle_git_stage = ctx["_handle_git_stage"]
    _handle_git_stash_checkout = ctx["_handle_git_stash_checkout"]
    _handle_git_unstage = ctx["_handle_git_unstage"]
    _handle_memory_write = ctx["_handle_memory_write"]
    _handle_office_file_save = ctx["_handle_office_file_save"]
    _handle_skill_delete = ctx["_handle_skill_delete"]
    _handle_skill_save = ctx["_handle_skill_save"]
    _handle_skill_toggle = ctx["_handle_skill_toggle"]
    _handle_workspace_add = ctx["_handle_workspace_add"]
    _handle_workspace_remove = ctx["_handle_workspace_remove"]
    _handle_workspace_rename = ctx["_handle_workspace_rename"]
    _handle_workspace_reorder = ctx["_handle_workspace_reorder"]
    _sanitize_error = ctx["_sanitize_error"]
    bad = ctx["bad"]
    j = ctx["j"]
    logger = ctx["logger"]

    if parsed.path == "/api/crons/create":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_create(handler, body)

    if parsed.path == "/api/crons/update":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_update(handler, body)

    if parsed.path == "/api/crons/delete":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_delete(handler, body)

    if parsed.path == "/api/crons/run":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_run(handler, body)

    if parsed.path == "/api/crons/pause":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_pause(handler, body)

    if parsed.path == "/api/crons/resume":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_resume(handler, body)

    # ── Git workspace ops (POST) ──
    if parsed.path == "/api/git/stage":
        return _handle_git_stage(handler, body)

    if parsed.path == "/api/git/unstage":
        return _handle_git_unstage(handler, body)

    if parsed.path == "/api/git/discard":
        return _handle_git_discard(handler, body)

    if parsed.path == "/api/git/commit-message":
        return _handle_git_commit_message(handler, body)

    if parsed.path == "/api/git/commit-message-selected":
        return _handle_git_commit_message_selected(handler, body)

    if parsed.path == "/api/git/commit":
        return _handle_git_commit(handler, body)

    if parsed.path == "/api/git/commit-selected":
        return _handle_git_commit_selected(handler, body)

    if parsed.path == "/api/git/fetch":
        return _handle_git_remote_action(handler, body, "fetch")

    if parsed.path == "/api/git/pull":
        return _handle_git_remote_action(handler, body, "pull")

    if parsed.path == "/api/git/push":
        return _handle_git_remote_action(handler, body, "push")

    if parsed.path == "/api/git/checkout":
        return _handle_git_checkout(handler, body)

    if parsed.path == "/api/git/stash-checkout":
        return _handle_git_stash_checkout(handler, body)

    # ── File ops (POST) ──
    if parsed.path == "/api/file/delete":
        return _handle_file_delete(handler, body)

    if parsed.path == "/api/file/save":
        return _handle_file_save(handler, body)

    if parsed.path == "/api/file/office-save":
        return _handle_office_file_save(handler, body)

    if parsed.path == "/api/file/create":
        return _handle_file_create(handler, body)

    if parsed.path == "/api/file/rename":
        return _handle_file_rename(handler, body)

    if parsed.path == "/api/file/move":
        return _handle_file_move(handler, body)

    if parsed.path == "/api/file/create-dir":
        return _handle_create_dir(handler, body)

    if parsed.path == "/api/file/reveal":
        return _handle_file_reveal(handler, body)

    if parsed.path == "/api/file/path":
        return _handle_file_path(handler, body)

    if parsed.path == "/api/file/open-vscode":
        return _handle_file_open_vscode(handler, body)

    # ── Workspace management (POST) ──
    if parsed.path == "/api/workspaces/add":
        return _handle_workspace_add(handler, body)

    if parsed.path == "/api/workspaces/remove":
        return _handle_workspace_remove(handler, body)

    if parsed.path == "/api/workspaces/rename":
        return _handle_workspace_rename(handler, body)

    if parsed.path == "/api/workspaces/reorder":
        return _handle_workspace_reorder(handler, body)

    # ── Approval (POST) ──
    if parsed.path == "/api/approval/respond":
        return _handle_approval_respond(handler, body)

    # ── Clarify (POST) ──
    if parsed.path == "/api/clarify/respond":
        return _handle_clarify_respond(handler, body)

    # ── Commands (POST) ──
    if parsed.path == "/api/commands/bundles/resolve":
        from api.commands import resolve_bundle_command

        command = str(body.get("command", "") or "").strip()
        if not command:
            return bad(handler, "command is required")

        try:
            return j(handler, resolve_bundle_command(command))
        except KeyError:
            return bad(handler, "Bundle command not found", 404)
        except ValueError as e:
            return bad(handler, str(e), 400)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

    if parsed.path == "/api/commands/exec":
        from api.commands import execute_agent_command, execute_plugin_command

        command = str(body.get("command", "") or "").strip()
        if not command:
            return bad(handler, "command is required")

        try:
            return j(handler, {"output": execute_agent_command(command)})
        except KeyError:
            pass
        except ValueError as e:
            return bad(handler, str(e), 400)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

        try:
            return j(handler, {"output": execute_plugin_command(command)})
        except ValueError as e:
            return bad(handler, str(e), 400)
        except KeyError:
            return bad(handler, "Plugin command not found", 404)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

    # ── Skills (POST) ──
    if parsed.path == "/api/skills/save":
        return _handle_skill_save(handler, body)

    if parsed.path == "/api/skills/delete":
        return _handle_skill_delete(handler, body)

    if parsed.path == "/api/skills/toggle":
        return _handle_skill_toggle(handler, body)

    # ── Memory (POST) ──
    if parsed.path == "/api/memory/write":
        return _handle_memory_write(handler, body)

    if parsed.path in {
        "/api/gateway/start",
        "/api/gateway/stop",
        "/api/gateway/restart",
    }:
        return _handle_gateway_lifecycle(handler, parsed.path.rsplit("/", 1)[-1], body)

    # ── Profile API (POST) ──
    if parsed.path == "/api/profile/switch":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        try:
            from api.auth import ensure_trusted_auth_session
            from api.profiles import switch_profile, _validate_profile_name
            from api.helpers import build_profile_cookie

            if name != "default":
                _validate_profile_name(name)
            session_info = ensure_trusted_auth_session(handler)
            if getattr(handler, "_trusted_auth_session_rejected", False):
                return bad(handler, "Authentication required", 401)
            bound_profile = (
                str((session_info or {}).get("bound_profile") or "").strip() or None
            )
            if bound_profile and name != bound_profile:
                return bad(handler, "Profile is bound to the current session", 403)
            # process_wide=False: don't mutate the process-global _active_profile.
            # Per-client profile is managed via cookie + thread-local (#798).
            result = switch_profile(name, process_wide=False)
            # Invalidate the models cache so the very next /api/models request
            # rebuilds from the new profile's config.yaml rather than returning
            # the old profile's cached model list (#1200 — profile-switch model bug).
            from api.config import invalidate_models_cache

            invalidate_models_cache()
            try:
                from api.gateway_watcher import restart_watcher_for_profile

                restart_watcher_for_profile(name)
            except Exception as exc:
                logger.warning(
                    "Failed to restart gateway watcher for profile %s: %s", name, exc
                )
            session_cookie_value = getattr(
                handler, "_trusted_auth_session_cookie_value", None
            )
            if session_cookie_value:
                if bound_profile and name == bound_profile:
                    return j(handler, result)
                extra_header = build_profile_cookie(
                    name, session_cookie_value=session_cookie_value
                )
            else:
                extra_header = build_profile_cookie(name, handler)
            return j(
                handler,
                result,
                extra_headers={
                    "Set-Cookie": extra_header,
                },
            )
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileNotFoundError) as e:
            return bad(handler, _sanitize_error(e), 404)
        except RuntimeError as e:
            return bad(handler, str(e), 409)
    return UNHANDLED
