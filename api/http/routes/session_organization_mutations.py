"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    LOCK = ctx["LOCK"]
    SESSIONS = ctx["SESSIONS"]
    SESSION_INDEX_FILE = ctx["SESSION_INDEX_FILE"]
    Session = ctx["Session"]
    SessionBusyError = ctx["SessionBusyError"]
    _active_stream_ids = ctx["_active_stream_ids"]
    _get_or_materialize_session = ctx["_get_or_materialize_session"]
    _handle_session_import = ctx["_handle_session_import"]
    _is_messaging_session_record = ctx["_is_messaging_session_record"]
    _is_subagent_child_session_id = ctx["_is_subagent_child_session_id"]
    _lookup_cli_session_metadata = ctx["_lookup_cli_session_metadata"]
    _profiles_match = ctx["_profiles_match"]
    _session_counts_toward_pin_quota = ctx["_session_counts_toward_pin_quota"]
    _session_field = ctx["_session_field"]
    _session_is_subagent_view_only = ctx["_session_is_subagent_view_only"]
    _session_row_lineage_root_id = ctx["_session_row_lineage_root_id"]
    _visible_pinned_lineage_ids = ctx["_visible_pinned_lineage_ids"]
    _worktree_retained_payload = ctx["_worktree_retained_payload"]
    all_sessions = ctx["all_sessions"]
    apply_cli_source_metadata = ctx["apply_cli_source_metadata"]
    bad = ctx["bad"]
    edit_session = ctx["edit_session"]
    get_active_profile_name = ctx["get_active_profile_name"]
    get_cli_session_messages = ctx["get_cli_session_messages"]
    get_last_workspace = ctx["get_last_workspace"]
    get_session = ctx["get_session"]
    import_cli_session = ctx["import_cli_session"]
    is_cli_session_row = ctx["is_cli_session_row"]
    j = ctx["j"]
    json = ctx["json"]
    load_projects = ctx["load_projects"]
    load_settings = ctx["load_settings"]
    logger = ctx["logger"]
    publish_session_list_changed = ctx["publish_session_list_changed"]
    require = ctx["require"]
    save_projects = ctx["save_projects"]
    time = ctx["time"]
    title_from = ctx["title_from"]
    uuid = ctx["uuid"]

    if parsed.path == "/api/session/pin":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        pin_requested = bool(body.get("pinned", True))
        # TOCTOU guard (Opus stage-389): the count check and the pin write
        # must happen under the same lock, otherwise two parallel pin
        # requests can both pass `len(pinned_ids) >= 3` against the same
        # snapshot and both succeed, leaving the user with 4 pins. The check
        # must be careful not to nest `all_sessions()` (which acquires LOCK
        # internally) inside a `with LOCK:` block — that's a deadlock since
        # LOCK is a non-reentrant `threading.Lock`. We snapshot the
        # persisted index outside the lock, then re-check the in-memory
        # mutation set inside the lock and commit the pin atomically.
        if pin_requested and not getattr(s, "pinned", False):
            # Pre-snapshot from persisted index (acquires LOCK internally,
            # so must run outside our own LOCK acquire below).
            persisted_rows = [
                existing
                for existing in all_sessions()
                if _session_counts_toward_pin_quota(existing)
            ]
            with LOCK:
                # Final authoritative count: merge persisted pinned rows with the
                # in-memory SESSIONS snapshot. Count logical sidebar-visible pin
                # lineages rather than raw session rows so continuation siblings
                # in the same visible lineage do not consume extra pin quota.
                candidate_rows = list(persisted_rows)
                candidate_rows.extend(
                    existing.compact()
                    for existing in SESSIONS.values()
                    if _session_counts_toward_pin_quota(existing)
                )
                target_row = s.compact()
                candidate_rows.append(target_row)
                pinned_lineage_ids = _visible_pinned_lineage_ids(candidate_rows)
                target_lineage = _session_row_lineage_root_id(
                    target_row,
                    {
                        str(_session_field(row, "session_id", "") or ""): row
                        for row in candidate_rows
                        if _session_field(row, "session_id", None)
                    },
                )
                pinned_lineage_ids.discard(target_lineage)
                pinned_sessions_limit = int(
                    load_settings().get("pinned_sessions_limit", 3) or 3
                )
                if len(pinned_lineage_ids) >= pinned_sessions_limit:
                    return bad(
                        handler,
                        f"Up to {pinned_sessions_limit} sessions can be pinned. Unpin one before pinning another.",
                        400,
                    )
                # Mark in-memory pin state under LOCK so concurrent pin
                # requests see the increment immediately, even before
                # save() finishes flushing to disk.
                s.pinned = True
            with edit_session(body["session_id"], session=s) as s:
                s.pinned = True
        else:
            with edit_session(body["session_id"], session=s) as s:
                s.pinned = pin_requested
        publish_session_list_changed(
            "session_pin",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"ok": True, "session": s.compact()})

    # ── Session archive (POST) ──
    if parsed.path == "/api/session/archive":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be archived from WebUI",
                400,
            )
        try:
            s = get_session(sid)
        except KeyError:
            cli_meta = _lookup_cli_session_metadata(sid)
            if not cli_meta:
                return bad(handler, "Session not found", 404)
            if cli_meta.get("read_only"):
                return bad(
                    handler,
                    "Read-only imported sessions cannot be archived from WebUI",
                    400,
                )
            # Delegated subagent children (#5307) are view-only and owned by the
            # delegate runner — never materialize one into a writable WebUI
            # sidecar via the archive fallback (the 3rd of the shared
            # import_cli_session write paths).
            _arch_source_tag = (
                (cli_meta.get("source_tag") or cli_meta.get("raw_source") or "")
                .strip()
                .lower()
            )
            if _arch_source_tag == "subagent" or _is_subagent_child_session_id(sid):
                return bad(
                    handler, "Subagent sessions cannot be archived from WebUI", 400
                )
            if _is_messaging_session_record(cli_meta):
                s = Session(
                    session_id=sid,
                    title=cli_meta.get("title")
                    or title_from(get_cli_session_messages(sid), "CLI Session"),
                    workspace=get_last_workspace(),
                    messages=[],
                    model=cli_meta.get("model") or "unknown",
                    created_at=cli_meta.get("created_at"),
                    updated_at=cli_meta.get("updated_at"),
                )
                apply_cli_source_metadata(
                    s,
                    cli_meta,
                    is_cli_session=is_cli_session_row(cli_meta),
                )
                s.save(touch_updated_at=False)
            else:
                msgs = get_cli_session_messages(sid)
                if not msgs:
                    return bad(handler, "Session not found", 404)
                s = import_cli_session(
                    sid,
                    cli_meta.get("title") or title_from(msgs, "CLI Session"),
                    msgs,
                    cli_meta.get("model") or "unknown",
                    profile=cli_meta.get("profile"),
                    created_at=cli_meta.get("created_at"),
                    updated_at=cli_meta.get("updated_at"),
                    source_metadata=cli_meta,
                )
                apply_cli_source_metadata(
                    s,
                    cli_meta,
                    is_cli_session=is_cli_session_row(cli_meta),
                )
        with edit_session(sid, session=s, touch_updated_at=False) as s:
            s.archived = bool(body.get("archived", True))
        publish_session_list_changed(
            "session_archive",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", sid),
        )
        return j(
            handler,
            {"ok": True, "session": s.compact(), **_worktree_retained_payload(s)},
        )

    # ── Session move to project (POST) ──
    if parsed.path == "/api/session/move":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(
                handler, "Read-only imported sessions cannot be moved from WebUI", 403
            )
        # #1614: refuse moves into a project owned by another profile.
        target_pid = body.get("project_id") or None
        if target_pid:
            # Use the session's own profile for authorization, not the global
            # active profile. A session belongs to a specific profile set at
            # creation; projects from that profile should always be assignable,
            # regardless of which profile is "active" at the process level.
            # Matches the same principle as the profile chip fix — prefer
            # session-scoped state over global active profile. (#3325 follow-up)
            _session_profile = getattr(s, "profile", None) or get_active_profile_name()
            target = next(
                (p for p in load_projects() if p["project_id"] == target_pid),
                None,
            )
            if not target:
                return bad(handler, "Project not found", 404)
            if not _profiles_match(target.get("profile"), _session_profile):
                return bad(handler, "Project not found", 404)
        # #3746: use the repository's bounded edit. The streaming thread holds
        # the same owner lock during checkpoint writes; contention becomes an
        # actionable HTTP 503 instead of outlasting the client's request timeout.
        try:
            with edit_session(
                body["session_id"],
                session=s,
                lock_timeout=5,
            ) as s:
                s.project_id = target_pid
        except SessionBusyError:
            return j(
                handler,
                {"error": "Session is busy (streaming). Please try again in a moment."},
                status=503,
            )
        publish_session_list_changed(
            "session_move",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"ok": True, "session": s.compact()})

    # ── Project CRUD (POST) ──
    if parsed.path == "/api/projects/create":
        try:
            require(body, "name")
        except ValueError as e:
            return bad(handler, str(e))
        import re as _re

        name = body["name"].strip()[:128]
        if not name:
            return bad(handler, "name required")
        color = body.get("color")
        if color and not _re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            return bad(handler, "Invalid color format")
        projects = load_projects()
        # #3331 follow-up (Codex+Opus gate): validate the optional client-supplied
        # `profile` before stamping it, mirroring /api/profile/switch — otherwise a
        # client could create a project tagged with an arbitrary/unknown profile,
        # producing hidden cross-profile rows that can't be managed normally.
        _requested_profile = str(body.get("profile") or "").strip()
        if _requested_profile and _requested_profile != "default":
            from api.profiles import is_valid_profile_id

            if not is_valid_profile_id(_requested_profile):
                return bad(handler, "invalid profile")
        proj = {
            "project_id": uuid.uuid4().hex[:12],
            "name": name,
            "color": color,
            "profile": _requested_profile or get_active_profile_name() or "default",
            "created_at": time.time(),
        }
        projects.append(proj)
        save_projects(projects)
        return j(handler, {"ok": True, "project": proj})

    if parsed.path == "/api/projects/rename":
        try:
            require(body, "project_id", "name")
        except ValueError as e:
            return bad(handler, str(e))
        import re as _re

        projects = load_projects()
        proj = next(
            (p for p in projects if p["project_id"] == body["project_id"]), None
        )
        if not proj:
            return bad(handler, "Project not found", 404)
        # #1614: a project can only be renamed by the profile that owns it.
        active_profile = get_active_profile_name()
        if not _profiles_match(proj.get("profile"), active_profile):
            return bad(handler, "Project not found", 404)
        proj["name"] = body["name"].strip()[:128]
        if "color" in body:
            color = body["color"]
            if color and not _re.match(r"^#[0-9a-fA-F]{3,8}$", color):
                return bad(handler, "Invalid color format")
            proj["color"] = color
        save_projects(projects)
        return j(handler, {"ok": True, "project": proj})

    if parsed.path == "/api/projects/delete":
        try:
            require(body, "project_id")
        except ValueError as e:
            return bad(handler, str(e))
        projects = load_projects()
        proj = next(
            (p for p in projects if p["project_id"] == body["project_id"]), None
        )
        if not proj:
            return bad(handler, "Project not found", 404)
        # #1614: a project can only be deleted by the profile that owns it.
        active_profile = get_active_profile_name()
        if not _profiles_match(proj.get("profile"), active_profile):
            return bad(handler, "Project not found", 404)
        projects = [p for p in projects if p["project_id"] != body["project_id"]]
        save_projects(projects)
        # Unassign all sessions that belonged to this project.
        # #3746: this loop is O(N) full-JSON read+save per session, and each
        # save() reserializes the entire messages array. For a project with many
        # messageful sessions that throughput alone can blow past the client's
        # 30s timeout. For an actively-streaming session we must NOT issue our own
        # s.save() — it would race the streaming thread's atomic writer and it
        # carries the largest in-memory message array. Instead we clear project_id
        # on the live cached Session object (under LOCK); the streaming thread owns
        # that object and persists it on its next checkpoint/final save (the worker
        # always does a final s.save() at turn completion), so the unlink still
        # lands without a competing write. (If the streaming session isn't in the
        # cache for some reason, fall back to a direct save.) Guard each per-session
        # update so one slow/failing session can't abort the whole request.
        if SESSION_INDEX_FILE.exists():
            try:
                index = json.loads(SESSION_INDEX_FILE.read_bytes())
                active_ids = _active_stream_ids()
                deferred_to_stream = []
                for entry in index:
                    if entry.get("project_id") != body["project_id"]:
                        continue
                    sid = entry.get("session_id")
                    try:
                        if entry.get("active_stream_id") in active_ids:
                            # Clear on the live cached object so the streaming
                            # thread's own next save persists project_id=None.
                            cleared_in_cache = False
                            with LOCK:
                                cached = SESSIONS.get(sid)
                                if cached is not None:
                                    cached.project_id = None
                                    cleared_in_cache = True
                            if cleared_in_cache:
                                deferred_to_stream.append(sid)
                                continue
                            # Not cached — fall through to a direct save.
                        s = get_session(sid)
                        s.project_id = None
                        s.save()
                    except Exception:
                        logger.debug("Failed to update session %s", sid)
                if deferred_to_stream:
                    logger.info(
                        "projects/delete: cleared project_id on %d streaming session(s) "
                        "in-cache; streaming thread will persist: %s",
                        len(deferred_to_stream),
                        deferred_to_stream,
                    )
            except Exception:
                logger.debug("Failed to load session index for project unlink")
        return j(handler, {"ok": True})

    # ── Session import from JSON (POST) ──
    if parsed.path == "/api/session/import":
        return _handle_session_import(handler, body)
    return UNHANDLED
