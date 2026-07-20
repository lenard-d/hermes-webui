"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_get(handler, parsed, ctx: RouteContext):
    RequestDiagnostics = ctx["RequestDiagnostics"]
    _active_skill_search_dirs = ctx["_active_skill_search_dirs"]
    _active_skills_dir = ctx["_active_skills_dir"]
    _all_profiles_enabled = ctx["_all_profiles_enabled"]
    _cron_jobs_cross_profile = ctx["_cron_jobs_cross_profile"]
    _ensure_agent_cron_import_path = ctx["_ensure_agent_cron_import_path"]
    _find_skill_in_dirs = ctx["_find_skill_in_dirs"]
    _gateway_status_payload = ctx["_gateway_status_payload"]
    _get_active_profile_name = ctx["_get_active_profile_name"]
    _handle_cron_delivery_options = ctx["_handle_cron_delivery_options"]
    _handle_cron_history = ctx["_handle_cron_history"]
    _handle_cron_output = ctx["_handle_cron_output"]
    _handle_cron_recent = ctx["_handle_cron_recent"]
    _handle_cron_run_detail = ctx["_handle_cron_run_detail"]
    _handle_cron_status = ctx["_handle_cron_status"]
    _handle_mcp_servers_list = ctx["_handle_mcp_servers_list"]
    _handle_mcp_tools_list = ctx["_handle_mcp_tools_list"]
    _handle_memory_read = ctx["_handle_memory_read"]
    _handle_notes_item = ctx["_handle_notes_item"]
    _handle_notes_search = ctx["_handle_notes_search"]
    _handle_notes_sources_list = ctx["_handle_notes_sources_list"]
    _is_isolated_profile_mode = ctx["_is_isolated_profile_mode"]
    _skill_view_from_active_dir = ctx["_skill_view_from_active_dir"]
    _skills_list_from_dir = ctx["_skills_list_from_dir"]
    bad = ctx["bad"]
    get_profile_default_workspace = ctx["get_profile_default_workspace"]
    j = ctx["j"]
    logger = ctx["logger"]
    parse_qs = ctx["parse_qs"]

    if parsed.path == "/api/crons":
        # #4768: in split-container / minimal Docker deployments the WebUI image may
        # not ship the agent's `cron` package on its import path. Degrade gracefully
        # (empty list + cron_unavailable flag) instead of 500ing the whole Task tab.
        # Only treat a genuinely-absent cron package as "unavailable"; a
        # ModuleNotFoundError whose missing module is an internal dependency of an
        # existing cron/jobs.py is a real bug and must still surface.
        _ensure_agent_cron_import_path()
        active_profile = _get_active_profile_name() or "default"
        try:
            active_jobs, other_jobs = _cron_jobs_cross_profile(active_profile)
        except ModuleNotFoundError as exc:
            if exc.name in ("cron", "cron.jobs"):
                return j(handler, {"jobs": [], "cron_unavailable": True})
            raise
        all_profiles = _all_profiles_enabled(parsed)
        jobs = active_jobs + other_jobs if all_profiles else active_jobs
        hidden_other_count = 0 if all_profiles else len(other_jobs)
        return j(
            handler,
            {
                "jobs": jobs,
                "all_profiles": all_profiles,
                "active_profile": active_profile,
                "other_profile_count": hidden_other_count,
            },
        )

    if parsed.path == "/api/crons/output":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_output(handler, parsed)

    if parsed.path == "/api/crons/history":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_history(handler, parsed)

    if parsed.path == "/api/crons/run":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_run_detail(handler, parsed)

    if parsed.path == "/api/crons/recent":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_recent(handler, parsed)

    if parsed.path == "/api/crons/status":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            return _handle_cron_status(handler, parsed)

    if parsed.path == "/api/crons/delivery-options":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_delivery_options(handler)

    # ── Skills API (GET) ──
    if parsed.path == "/api/skills":
        qs = parse_qs(parsed.query)
        category = qs.get("category", [None])[0]
        data = _skills_list_from_dir(_active_skills_dir(), category=category)
        return j(handler, {"skills": data.get("skills", [])})

    if parsed.path == "/api/skills/usage":
        from api.skill_usage import read_skill_usage

        raw = read_skill_usage(_active_skills_dir())
        # Pass through agent's format as-is; defensive coercion for fields
        usage = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(v, dict):
                    usage[k] = {"use_count": 0, "view_count": 0, "patch_count": 0}
                    continue
                usage[k] = {
                    "use_count": (
                        int(v["use_count"]) if v.get("use_count") is not None else 0
                    ),
                    "view_count": (
                        int(v["view_count"]) if v.get("view_count") is not None else 0
                    ),
                    "patch_count": (
                        int(v["patch_count"]) if v.get("patch_count") is not None else 0
                    ),
                }
                # Preserve agent's metadata (timestamps, state, etc.)
                for meta_key in v:
                    if meta_key not in usage[k]:
                        usage[k][meta_key] = v[meta_key]
        skills_data = _skills_list_from_dir(_active_skills_dir()).get("skills", [])
        skill_names = sorted({s["name"] for s in skills_data})
        total = sum(
            e.get("use_count", 0) + e.get("view_count", 0) + e.get("patch_count", 0)
            for e in usage.values()
        )
        unique = sum(
            1
            for e in usage.values()
            if e.get("use_count", 0) > 0
            or e.get("view_count", 0) > 0
            or e.get("patch_count", 0) > 0
        )
        return j(
            handler,
            {
                "usage": usage,
                "skill_names": skill_names,
                "total_invocations": total,
                "unique_skills_used": unique,
            },
        )

    if parsed.path == "/api/skills/content":
        qs = parse_qs(parsed.query)
        name = qs.get("name", [""])[0]
        if not name:
            return j(handler, {"error": "name required"}, status=400)
        file_path = qs.get("file", [""])[0]
        if file_path:
            # Serve a linked file from the skill directory
            import re as _re

            if _re.search(r"[*?\[\]]", name):
                return bad(handler, "Invalid skill name", 400)
            skills_dir = _active_skills_dir()
            skill_dir, _skill_md = _find_skill_in_dirs(
                name, _active_skill_search_dirs(skills_dir)
            )
            if not skill_dir:
                return bad(handler, "Skill not found", 404)
            target = (skill_dir / file_path).resolve()
            try:
                target.relative_to(skill_dir.resolve())
            except ValueError:
                return bad(handler, "Invalid file path", 400)
            if not target.exists() or not target.is_file():
                return bad(handler, "File not found", 404)
            return j(
                handler,
                {"content": target.read_text(encoding="utf-8"), "path": file_path},
            )
        data = _skill_view_from_active_dir(name)
        if not isinstance(data.get("linked_files"), dict):
            data["linked_files"] = {}
        return j(handler, data)

    # ── Memory API (GET) ──
    if parsed.path == "/api/memory":
        return _handle_memory_read(handler, parsed)

    # ── Profile API (GET) ──
    if parsed.path == "/api/profiles":
        from api import profiles as profiles_api

        diag = RequestDiagnostics.maybe_start(
            "GET",
            parsed.path,
            logger=logger,
            print_fn=getattr(handler, "_safe_webui_print", None),
        )
        try:
            diag.stage("list_profiles_api") if diag else None
            profiles_payload = profiles_api.list_profiles_api()
            diag.stage("active_profile_lookup") if diag else None
            active = profiles_api.get_active_profile_name()
            diag.stage("isolated_mode_check") if diag else None
            return j(
                handler,
                {
                    "profiles": profiles_payload,
                    "active": active,
                    "single_profile_mode": _is_isolated_profile_mode(),
                },
            )
        finally:
            if diag:
                diag.finish()

    if parsed.path == "/api/profile/active":
        from api import profiles as profiles_api

        active_profile_name = profiles_api.get_active_profile_name()
        # Resolve the ACTIVE PROFILE's configured workspace so a cold boot with a
        # profile cookie shows the right composer workspace chip on a blank
        # new-chat page (#5169). Use get_profile_default_workspace() (NOT
        # get_last_workspace) so a named profile without its own last_workspace.txt
        # resolves to its config.yaml workspace/terminal.cwd rather than leaking the
        # GLOBAL last-workspace file (the #5169 regression Codex flagged). It is
        # profile-scoped via the per-request hermes_profile cookie set in server.py.
        # Fail open: a resolution error must never 500 this boot-critical endpoint.
        try:
            _profile_default_workspace = get_profile_default_workspace()
        except Exception:
            logger.debug(
                "Failed to resolve profile default workspace for /api/profile/active",
                exc_info=True,
            )
            _profile_default_workspace = None
        return j(
            handler,
            {
                "name": active_profile_name,
                "path": str(profiles_api.get_active_hermes_home()),
                "is_default": profiles_api._is_root_profile(active_profile_name),
                "default_workspace": _profile_default_workspace,
            },
        )

    # ── Gateway Status (GET) ──
    if parsed.path == "/api/gateway/status":
        return j(handler, _gateway_status_payload())

    # ── MCP Servers (GET) ──
    if parsed.path == "/api/mcp/servers":
        return _handle_mcp_servers_list(handler)

    # ── MCP Tools (GET) ──
    if parsed.path == "/api/mcp/tools":
        return _handle_mcp_tools_list(handler)

    if parsed.path == "/api/notes/sources":
        return _handle_notes_sources_list(handler)
    if parsed.path == "/api/notes/search":
        return _handle_notes_search(handler, parsed)
    if parsed.path == "/api/notes/item":
        return _handle_notes_item(handler, parsed)

    # ── Checkpoints / Rollback (GET) ──
    if parsed.path == "/api/rollback/list":
        qs = parse_qs(parsed.query)
        workspace = qs.get("workspace", [""])[0]
        if not workspace:
            return bad(handler, "workspace query parameter is required")
        try:
            from api.rollback import list_checkpoints

            return j(handler, list_checkpoints(workspace))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/list failed")
            return bad(handler, str(e), status=500)

    if parsed.path == "/api/rollback/diff":
        qs = parse_qs(parsed.query)
        workspace = qs.get("workspace", [""])[0]
        checkpoint = qs.get("checkpoint", [""])[0]
        if not workspace or not checkpoint:
            return bad(
                handler, "workspace and checkpoint query parameters are required"
            )
        try:
            from api.rollback import get_checkpoint_diff

            return j(handler, get_checkpoint_diff(workspace, checkpoint))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/diff failed")
            return bad(handler, str(e), status=500)
    return UNHANDLED
