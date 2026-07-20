"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.http.shell import load_saved_prompts


def handle_get(handler, parsed, ctx: RouteContext):
    Path = ctx["Path"]
    _git_bad = ctx["_git_bad"]
    _handle_approval_inject = ctx["_handle_approval_inject"]
    _handle_approval_pending = ctx["_handle_approval_pending"]
    _handle_approval_sse_stream = ctx["_handle_approval_sse_stream"]
    _handle_clarify_inject = ctx["_handle_clarify_inject"]
    _handle_clarify_pending = ctx["_handle_clarify_pending"]
    _handle_clarify_sse_stream = ctx["_handle_clarify_sse_stream"]
    _handle_escape_file_raw = ctx["_handle_escape_file_raw"]
    _handle_escape_file_read = ctx["_handle_escape_file_read"]
    _handle_escape_list_dir = ctx["_handle_escape_list_dir"]
    _handle_file_raw = ctx["_handle_file_raw"]
    _handle_file_read = ctx["_handle_file_read"]
    _handle_folder_download = ctx["_handle_folder_download"]
    _handle_gateway_sse_stream = ctx["_handle_gateway_sse_stream"]
    _handle_git_branches = ctx["_handle_git_branches"]
    _handle_git_diff = ctx["_handle_git_diff"]
    _handle_git_status = ctx["_handle_git_status"]
    _handle_list_dir = ctx["_handle_list_dir"]
    _handle_media = ctx["_handle_media"]
    _handle_session_events_stream = ctx["_handle_session_events_stream"]
    _handle_session_export = ctx["_handle_session_export"]
    _handle_session_sse_stream = ctx["_handle_session_sse_stream"]
    _handle_session_sse_stream_for_session = ctx[
        "_handle_session_sse_stream_for_session"
    ]
    _handle_sessions_search = ctx["_handle_sessions_search"]
    _handle_sse_stream = ctx["_handle_sse_stream"]
    _handle_terminal_output = ctx["_handle_terminal_output"]
    _run_journal_status_payload = ctx["_run_journal_status_payload"]
    _session_events_path_session_id = ctx["_session_events_path_session_id"]
    _stream_id_visible_to_request_profile = ctx["_stream_id_visible_to_request_profile"]
    _terminal_remote_backend_enabled = ctx["_terminal_remote_backend_enabled"]
    bad = ctx["bad"]
    cancel_stream = ctx["cancel_stream"]
    find_run_summary = ctx["find_run_summary"]
    get_last_workspace = ctx["get_last_workspace"]
    get_session = ctx["get_session"]
    j = ctx["j"]
    list_workspace_suggestions = ctx["list_workspace_suggestions"]
    load_settings = ctx["load_settings"]
    load_workspaces = ctx["load_workspaces"]
    parse_qs = ctx["parse_qs"]
    poll_onboarding_oauth_flow = ctx["poll_onboarding_oauth_flow"]
    runtime_stream_alive = ctx["runtime_stream_alive"]

    if parsed.path == "/api/prompts":
        return j(handler, {"prompts": load_saved_prompts()})

    if parsed.path == "/api/session/export":
        return _handle_session_export(handler, parsed)

    if parsed.path == "/api/workspaces":
        return j(
            handler,
            {
                "workspaces": load_workspaces(),
                "last": get_last_workspace(),
                "terminal_remote_backend": _terminal_remote_backend_enabled(),
            },
        )

    if parsed.path == "/api/workspaces/suggest":
        qs = parse_qs(parsed.query)
        prefix = qs.get("prefix", [""])[0]
        return j(
            handler,
            {
                "suggestions": list_workspace_suggestions(prefix),
                "prefix": prefix,
            },
        )

    if parsed.path == "/api/sessions/search":
        return _handle_sessions_search(handler, parsed)

    if parsed.path == "/api/list":
        return _handle_list_dir(handler, parsed)

    if parsed.path == "/api/escape/list":
        return _handle_escape_list_dir(handler, parsed)

    if parsed.path == "/api/git/status":
        return _handle_git_status(handler, parsed)

    if parsed.path == "/api/git/branches":
        return _handle_git_branches(handler, parsed)

    if parsed.path == "/api/git/diff":
        return _handle_git_diff(handler, parsed)

    if parsed.path == "/api/personalities":
        # Read personalities from config.yaml agent.personalities section
        # (matches hermes-agent CLI behavior, not filesystem SOUL.md approach)
        from api.config import reload_config as _reload_cfg

        _reload_cfg()  # pick up config.yaml changes without server restart
        from api.config import get_config as _get_cfg

        _cfg = _get_cfg()
        agent_cfg = _cfg.get("agent", {})
        raw_personalities = agent_cfg.get("personalities", {})
        personalities = []
        if isinstance(raw_personalities, dict):
            for name, value in raw_personalities.items():
                desc = ""
                if isinstance(value, dict):
                    desc = value.get("description", "")
                elif isinstance(value, str):
                    desc = value[:80] + ("..." if len(value) > 80 else "")
                personalities.append({"name": name, "description": desc})
        return j(handler, {"personalities": personalities})

    if parsed.path == "/api/git-info":
        qs = parse_qs(parsed.query)
        sid = qs.get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id required")
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        from api.workspace import GitWorkspaceError, git_status

        try:
            status = git_status(Path(s.workspace))
        except GitWorkspaceError as e:
            return _git_bad(handler, e)
        totals = status.get("totals") or {}
        info = (
            None
            if not status.get("is_git")
            else {
                "branch": status.get("branch"),
                "dirty": totals.get("changed", 0),
                "modified": (totals.get("staged", 0) or 0)
                + (totals.get("unstaged", 0) or 0),
                "untracked": totals.get("untracked", 0),
                "ahead": status.get("ahead", 0),
                "behind": status.get("behind", 0),
                "is_git": True,
            }
        )
        return j(handler, {"git": info})

    if parsed.path == "/api/commands":
        from api.commands import list_commands

        return j(handler, {"commands": list_commands()})

    if parsed.path == "/api/commands/bundles":
        from api.commands import list_command_bundles

        return j(handler, {"bundles": list_command_bundles()})

    if parsed.path == "/api/commands/moa/resolve":
        from api.commands import resolve_moa_config

        try:
            return j(handler, resolve_moa_config())
        except RuntimeError as e:
            return bad(handler, str(e), 503)

    if parsed.path == "/api/updates/check":
        settings = load_settings()
        if not settings.get("check_for_updates", True):
            return j(handler, {"disabled": True})
        include_agent_updates = not bool(settings.get("ignore_agent_updates"))
        qs = parse_qs(parsed.query)
        # ?simulate=1 returns fake behind counts for UI testing (localhost only)
        if (
            qs.get("simulate", ["0"])[0] == "1"
            and handler.client_address[0] == "127.0.0.1"
        ):
            return j(
                handler,
                {
                    "webui": {
                        "name": "webui",
                        "behind": 3,
                        "current_sha": "abc1234",
                        "latest_sha": "def5678",
                        "branch": "master",
                        "repo_url": "https://github.com/nesquena/hermes-webui",
                        "compare_url": "https://github.com/nesquena/hermes-webui/compare/abc1234...def5678",
                    },
                    "agent": {
                        "name": "agent",
                        "behind": 1 if include_agent_updates else 0,
                        "ignored": not include_agent_updates,
                        "current_sha": "aaa0001",
                        "latest_sha": "bbb0002",
                        "branch": "master",
                        "repo_url": "https://github.com/NousResearch/hermes-agent",
                        "compare_url": "https://github.com/NousResearch/hermes-agent/compare/aaa0001...bbb0002",
                    },
                    "checked_at": 0,
                },
            )
        from api.updates import cached_update_status

        return j(handler, cached_update_status(include_agent=include_agent_updates))

    if parsed.path == "/api/chat/stream/status":
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        if not _stream_id_visible_to_request_profile(handler, stream_id):
            return True
        active = runtime_stream_alive(stream_id)
        payload = {"active": active, "stream_id": stream_id, "replay_available": False}
        try:
            journal = find_run_summary(stream_id) if stream_id else None
        except Exception:
            journal = None
        if journal:
            payload["replay_available"] = True
            payload["journal"] = _run_journal_status_payload(journal, active=active)
        return j(handler, payload)

    if parsed.path == "/api/chat/cancel":
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        if not stream_id:
            return bad(handler, "stream_id required")
        if not _stream_id_visible_to_request_profile(handler, stream_id):
            return True
        from api.runs import (
            LegacyJournalRuntimeAdapter,
            runtime_adapter_enabled,
        )

        if runtime_adapter_enabled():
            adapter = LegacyJournalRuntimeAdapter(cancel_delegate=cancel_stream)
            cancelled = adapter.cancel_run(stream_id).accepted
        else:
            cancelled = cancel_stream(stream_id)
        return j(handler, {"ok": True, "cancelled": cancelled, "stream_id": stream_id})

    if parsed.path == "/api/chat/stream":
        return _handle_sse_stream(handler, parsed)

    if parsed.path == "/api/terminal/output":
        return _handle_terminal_output(handler, parsed)

    if parsed.path == "/api/sessions/gateway/stream":
        return _handle_gateway_sse_stream(handler, parsed)

    if parsed.path == "/api/sessions/events":
        return _handle_session_events_stream(handler)

    session_events_session_id = _session_events_path_session_id(parsed.path)
    if session_events_session_id is not None:
        return _handle_session_sse_stream_for_session(
            handler, parsed, session_events_session_id
        )

    if parsed.path == "/api/media":
        return _handle_media(handler, parsed)

    if parsed.path == "/api/file/raw":
        return _handle_file_raw(handler, parsed)

    if parsed.path == "/api/escape/file/raw":
        return _handle_escape_file_raw(handler, parsed)

    if parsed.path == "/api/folder/download":
        return _handle_folder_download(handler, parsed)

    if parsed.path == "/api/file":
        return _handle_file_read(handler, parsed)

    if parsed.path == "/api/escape/file/read":
        return _handle_escape_file_read(handler, parsed)

    if parsed.path == "/api/approval/pending":
        return _handle_approval_pending(handler, parsed)

    if parsed.path == "/api/approval/stream":
        return _handle_approval_sse_stream(handler, parsed)

    if parsed.path == "/api/approval/inject_test":
        # Loopback-only: used by automated tests; blocked from any remote client
        if handler.client_address[0] != "127.0.0.1":
            return j(handler, {"error": "not found"}, status=404)
        return _handle_approval_inject(handler, parsed)

    if parsed.path == "/api/clarify/pending":
        return _handle_clarify_pending(handler, parsed)

    if parsed.path == "/api/clarify/stream":
        return _handle_clarify_sse_stream(handler, parsed)

    if parsed.path == "/api/session/stream":
        return _handle_session_sse_stream(handler, parsed)

    if parsed.path == "/api/clarify/inject_test":
        # Loopback-only: used by automated tests; blocked from any remote client
        if handler.client_address[0] != "127.0.0.1":
            return j(handler, {"error": "not found"}, status=404)
        return _handle_clarify_inject(handler, parsed)

    if parsed.path == "/api/onboarding/oauth/poll":
        qs = parse_qs(parsed.query)
        flow_id = qs.get("flow_id", [""])[0]
        try:
            return j(
                handler,
                poll_onboarding_oauth_flow(flow_id),
                extra_headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return bad(handler, str(e))
        except KeyError as e:
            return bad(handler, str(e), 404)
    return UNHANDLED
