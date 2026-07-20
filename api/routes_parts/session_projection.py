"""Session import ownership, lineage reconciliation, and bounded API projections."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations

import threading
from collections import OrderedDict


def _lookup_gateway_session_identity(session_id: str) -> dict:
    if not session_id:
        return {}
    metadata = _load_gateway_session_identity_map().get(str(session_id))
    return metadata if isinstance(metadata, dict) else {}


def _lookup_cli_session_metadata(session_id: str, *, all_profiles: bool = False) -> dict:
    if not session_id:
        return {}
    try:
        for row in get_cli_sessions(all_profiles=all_profiles):
            if row.get("session_id") == session_id:
                return row
    except Exception:
        return {}
    return {}


def _session_index_marks_was_webui(sid: str) -> bool:
    """Return True iff ``sid`` is in the WebUI session index as a WebUI- or
    fork-origin row whose sidecar is now gone.

    The WebUI session index (``SESSION_INDEX_FILE``) is the canonical registry
    of sessions the WebUI ever owned. A row there tagged with ``webui`` or
    ``fork`` means the session once had a sidecar that has since been deleted
    (or never materialised on this profile). Returning 404 to the client on
    these ids is what lets the browser self-heal: strip the stale
    ``/session/<id>`` URL and clear localStorage instead of silently
    re-attaching to a now-empty session (#2782).

    Foreign-origin rows (CLI, TUI, Desktop, claude_code, gateway, telegram,
    etc.) — those with explicit non-webui source tags, OR blank sources with
    ``is_cli_session``/``read_only`` markers — are NOT treated as deleted
    WebUI sessions, even when the sidecar is absent.
    """
    if not SESSION_INDEX_FILE.exists():
        return False
    try:
        entries = json.loads(SESSION_INDEX_FILE.read_bytes())
    except Exception:
        return False
    for entry in entries if isinstance(entries, list) else []:
        if entry.get("session_id") != sid:
            continue
        # Classify per source field, not on a collapsed `a or b or c` — a
        # legacy CLI/imported row can carry is_cli_session:true with BLANK
        # source fields, and collapsing-then-defaulting-to-WebUI would wrongly
        # 404 it (it should keep its read-only CLI stub).
        srcs = [
            str(entry.get("source_tag") or "").strip().lower(),
            str(entry.get("raw_source") or "").strip().lower(),
            str(entry.get("session_source") or "").strip().lower(),
        ]
        explicit = [s for s in srcs if s]
        if any(s in ("webui", "fork") for s in explicit):
            # Explicit WebUI-origin (incl. forks, which /api/session/branch
            # stamps session_source="fork") — a deleted sidecar bricks
            # identically. 404.
            return True
        if explicit:
            # Explicit non-WebUI source (cli, telegram, claude_code, ...) —
            # genuine foreign session, keep the existing CLI/read-only stub.
            return False
        # All source fields blank: WebUI-origin UNLESS the row is a legacy
        # CLI/imported session marked only by is_cli_session / read_only.
        is_cli = entry.get("is_cli_session") is True
        is_read_only = bool(entry.get("read_only") or entry.get("is_read_only"))
        return not (is_cli or is_read_only)
    return False


def _session_deleted_tombstone_marks_was_webui(sid: str) -> bool:
    try:
        return sid in _load_webui_deleted_session_tombstone()
    except Exception:
        return False


def _state_db_session_source(sid: str) -> str:
    """Return the lowercased ``sessions.source`` for ``sid`` from state.db.

    Cheap single-row lookup used to distinguish delegated ``subagent`` children
    (which have a recoverable state.db transcript) from genuinely-deleted WebUI
    sessions.  Returns "" on any error / missing row so callers fall back to
    their existing behaviour.
    """
    if not sid or not is_safe_session_id(sid):
        return ""
    try:
        from api.sessions.store import _active_state_db_path
        db_path = _active_state_db_path()
        if not db_path or not Path(db_path).exists():
            return ""
        import sqlite3 as _sqlite
        with closing(_sqlite.connect(str(db_path))) as _conn:
            row = _conn.execute(
                "SELECT source FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    return str(row[0] or "").strip().lower()


def _is_subagent_child_session_id(sid: str) -> bool:
    """Return True when ``sid`` is a delegated subagent child in state.db.

    Delegated ``delegate_task`` children are recorded in Hermes state.db with
    ``source='subagent'`` and a ``parent_session_id``. They frequently have no
    WebUI sidecar (they ran server-side), so opening one from the sidebar must
    recover the transcript from state.db rather than 404 as a deleted WebUI
    session (#5307).
    """
    return _state_db_session_source(sid) == "subagent"


def _session_is_subagent_view_only(sid: str) -> bool:
    """Return True when ``sid`` is a delegated subagent child by ANY signal —
    state.db source OR a persisted WebUI sidecar tagged subagent.

    Delegated children are view-only and owned by the delegate runner. Direct
    transcript/metadata mutation routes (delete / truncate / clear / pin /
    rename / move) must refuse them so a stray WebUI action can't delete or
    fork the child's state.db transcript (#5307). This is the shared
    defense-in-depth guard for routes that bypass
    ``_get_or_materialize_session()``.
    """
    if _is_subagent_child_session_id(sid):
        return True
    try:
        s = get_session(sid)
    except Exception:
        return False
    src = (
        str(getattr(s, "source_tag", "") or getattr(s, "raw_source", "")
            or getattr(s, "session_source", "") or "").strip().lower()
    )
    return src == "subagent"


def _is_claimable_cli_source(cli_meta: dict, state_db_source: str = "") -> tuple[bool, str]:
    """Decide whether a foreign-origin session is safe to claim writeable
    in WebUI. Returns ``(claimable, reason_if_not)``.

    Policy mirrors ``/api/session/import_cli``:
    sessions explicitly marked ``read_only`` in their foreign store are
    surfaced as read-only stubs but never materialised as writable
    WebUI sidecars. We extend that with a denylist of foreign-source
    families whose ownership belongs to a non-WebUI process
    (messaging channels, external agents, Claude Code, scheduled
    cron runs, and platformless gateway fallbacks), so a WebUI POST
    cannot accidentally turn them into writable sidecars and violate
    their ownership boundary (#4911 review + the residual
    gateway/unknown gap flagged in the follow-up + the cron-claim
    policy flagged in the Greptile 4/5 review).

    The check is denylist-based: if a source is in any of the
    refused families below, it is non-claimable. Everything else
    (CLI, TUI, Desktop, plus future local agent sources) is allowed.
    TUI/Desktop sessions whose cli_meta is empty (they don't appear
    in ``get_cli_sessions()`` due to the CLI cap) fall through to
    ``state_db_source``; state.db has a ``source`` column with values
    like ``tui``, ``desktop``, ``cli``, ``cron``, ``claude_code``,
    ``messaging``, ``external_agent``, ``gateway`` (platform-tagged
    gateways land in ``_MESSAGING_RAW_SOURCES`` and are caught by
    the messaging check above; bare ``"gateway"`` / ``"unknown"``
    literals are caught here).
    """
    cm = cli_meta or {}
    if bool(cm.get("read_only")):
        return False, "explicit_readonly"
    session_source = (cm.get("session_source") or "").strip().lower()
    if session_source in {"messaging", "external_agent"}:
        return False, f"session_source={session_source}"
    # Track cli_meta-sourced and state.db-sourced source_tag values
    # separately so the diagnostic reason string never mislabels a
    # cli_meta-sourced denial as a state.db-sourced one (Greptile P2,
    # #4911 follow-up).  The reason is currently discarded by the
    # caller, but it is exported in the return tuple and may surface
    # in a future log / user-visible diagnostic.
    cli_meta_source_tag = (cm.get("source_tag") or cm.get("raw_source") or "").strip().lower()
    if cli_meta_source_tag in {"claude_code", "cron", "external_agent",
                                "gateway", "messaging", "subagent", "unknown"}:
        # gateway/unknown are the platformless gateway fallbacks
        # (gateway/run.py, gateway/slash_commands.py) — they own the
        # conversation in the gateway, not in WebUI.
        # cron sessions are scheduled and owned by the cron runner
        # process; claiming them into a writable WebUI sidecar would
        # let a stray POST break the next scheduled run.
        # messaging / external_agent can be supplied by the foreign
        # store directly in source_tag (in addition to the
        # session_source / messaging-record checks above), and they
        # need the same provenance-correct refusal.
        return False, f"cli_meta_source={cli_meta_source_tag}"
    if _is_messaging_session_record(cm):
        return False, "messaging_record"
    # Empty cli_meta is the common case for TUI/Desktop; fall through
    # to state.db's source column.  Refuse known-foreign state.db sources.
    if not cli_meta_source_tag and state_db_source:
        state_db_source_tag = state_db_source.strip().lower()
        if state_db_source_tag in {"claude_code", "cron", "messaging",
                                    "external_agent", "gateway", "subagent", "unknown"}:
            return False, f"state_db_source={state_db_source_tag}"
    return True, ""


def _claim_or_synthesize_cli_session(sid: str, cli_meta: dict = None):
    """Resolve a session_id that has no WebUI sidecar.

    Returns ``(session_or_None, reason)``. Reasons:

      ``'materialized'``
        A state.db row with messages exists AND the foreign source is
        claimable per :func:`_is_claimable_cli_source` (CLI / TUI /
        Desktop, no explicit read_only, not a messaging / claude_code
        session). ``session`` is a fully populated
        :class:`api.sessions.store.Session` ready for writeable use; the caller
        MUST call ``session.save()`` to persist a WebUI-owned sidecar
        before the first write.  The Session carries the source-tag
        metadata from the CLI/state.db lookup (``is_cli_session=True``,
        ``read_only=False``) so the sidebar still renders the original
        source badge.

      ``'not_claimable'``
        The sid has recoverable state.db messages but the foreign
        source is owned by a non-WebUI process (messaging channel,
        claude_code, external_agent, or explicit read_only). ``session``
        is still returned, but with ``read_only=True`` preserved and
        the foreign source tag intact, so the GET stub continues to
        render the original badge and the read-only banner. The POST
        path must return 403 (not 404) so the user sees a clear
        refusal instead of the empty-state self-heal that the 404
        handler triggers (#4911 review).

      ``'was_webui'``
        The sid is in the WebUI session index as a webui/fork origin row but
        its sidecar is gone.  Callers MUST return 404 so the browser clears
        its stale ``/session/<id>`` URL and localStorage instead of silently
        re-attaching to a now-empty session (#2782).

      ``'no_foreign_state'``
        The sid has no WebUI sidecar AND no recoverable messages in
        state.db.  Callers MUST return 404.

      ``'invalid_sid'``
        ``sid`` failed :func:`is_safe_session_id`.  Callers MUST return 404.

    ``cli_meta`` is an optional pass-through.  Callers that already
    computed ``_lookup_cli_session_metadata(sid)`` (e.g. the GET path
    building a sidebar dict) can pass it in to avoid the redundant
    lookup; callers without it (POST path, tests) pass nothing and the
    helper does the lookup itself.

    Closing the GET-vs-POST asymmetry for foreign-origin sessions: GET
    ``/api/session`` and POST ``/api/chat/start`` both call this helper
    on a missing-sidecar KeyError, so a TUI/Desktop/CLI session can be
    loaded read-only AND continued writeable from the WebUI.
    """
    def build_workspace(sid, cli_meta):
        """Coalesce workspace with sane fallbacks so _start_run doesn't
        trip on a missing field. state.db's cwd is the canonical workspace for
        agent sessions; CLI metadata is the fallback (handles Telegram/etc).
        """
        workspace = (cli_meta or {}).get("workspace") or (cli_meta or {}).get("cwd")
        if not workspace:
            try:
                from api.workspace import get_last_workspace
                workspace = get_last_workspace()
            except Exception:
                workspace = None
        if not workspace:
            try:
                from api.sessions.store import DEFAULT_WORKSPACE
                workspace = DEFAULT_WORKSPACE
            except Exception:
                workspace = "/"
        return workspace

    def build_session(sid, cli_meta, msgs, read_only_flag, is_cli_flag=True):
        return Session(
            session_id=sid,
            title=(cli_meta or {}).get("title") or "CLI Session",
            workspace=build_workspace(sid, cli_meta),
            model=(cli_meta or {}).get("model") or "unknown",
            model_provider=(cli_meta or {}).get("model_provider"),
            messages=msgs,
            created_at=(cli_meta or {}).get("created_at") or 0,
            updated_at=(cli_meta or {}).get("updated_at") or 0,
            profile=(cli_meta or {}).get("profile"),
            # ``is_cli_flag`` is True for genuine CLI/TUI/Desktop sessions so the
            # sidebar renders the source badge and the client's external-session
            # gating applies. It is False for delegated subagent children (#5307):
            # they are recovered read-only and must NOT be CLI-classified, or they
            # would pass the frontend ``_isExternalSession`` poll-skip /
            # active-refresh gates that #3603 keeps narrow.
            is_cli_session=is_cli_flag,
            source_tag=(cli_meta or {}).get("source_tag"),
            raw_source=(cli_meta or {}).get("raw_source"),
            session_source=(cli_meta or {}).get("session_source"),
            source_label=(cli_meta or {}).get("source_label"),
            # ``read_only_flag`` is True for not_claimable sources (foreign
            # store marked them read-only / messaging / claude_code) and
            # False for genuine CLI / TUI / Desktop sessions.  Only the
            # POST claim path with a verified-claimable source can
            # actually write; the GET stub always reflects whatever the
            # helper returns so the read-only banner stays accurate.
            read_only=read_only_flag,
        )

    if not is_safe_session_id(sid):
        return None, "invalid_sid"
    if (
        (
            _session_index_marks_was_webui(sid)
            or (
                _session_deleted_tombstone_marks_was_webui(sid)
                and _state_db_session_source(sid) in ("", "webui", "fork")
            )
        )
        and not _is_subagent_child_session_id(sid)
    ):
        # A delegated subagent child (source='subagent' in state.db) can be
        # registered in the WebUI index as a webui/fork/blank-source row (it
        # shares the parent's lineage) yet have no WebUI sidecar of its own.
        # Those must recover their transcript from state.db below rather than
        # 404 as a genuinely-deleted WebUI session (#5307). Every other
        # index-marked-WebUI id keeps the #2782 self-heal 404 contract.
        #
        # The durable delete tombstone only 404s a row that is WebUI-owned
        # (source webui/fork, or blank = a stale URL with no surviving row).
        # A foreign-source row (messaging/cli/tui/desktop) that happens to
        # carry a tombstone — e.g. a WebUI delete of an imported session whose
        # state.db row the external writer later re-created — must still
        # materialize its transcript, never be self-healed to a 404 (#5504).
        return None, "was_webui"
    if cli_meta is None:
        cli_meta = _lookup_cli_session_metadata(sid) or {}
    msgs = get_cli_session_messages(sid)
    if not msgs:
        return None, "no_foreign_state"
    # TUI/Desktop sessions often have empty cli_meta (they don't appear in
    # get_cli_sessions() because of the cap).  Fall back to the state.db
    # ``source`` column to make the claim-eligibility check robust and to
    # populate the Session's source-tag metadata so the sidebar still
    # renders the correct badge for these sessions.
    state_db_source = ""
    state_db_row = None
    try:
        from api.sessions.store import _active_state_db_path
        db_path = _active_state_db_path()
        if db_path and Path(db_path).exists():
            import sqlite3 as _sqlite
            with closing(_sqlite.connect(str(db_path))) as _conn:
                _conn.row_factory = _sqlite.Row
                _row = _conn.execute(
                    "SELECT source, title, model, cwd, started_at, ended_at "
                    "FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
                if _row is not None:
                    state_db_row = dict(_row)
                    state_db_source = str(_row["source"] or "").strip().lower()
    except Exception:
        state_db_source = ""
    # Populate source metadata from state.db when cli_meta is empty so the
    # synthesized Session carries the right source_tag/source_label.  Only
    # fill fields that are actually missing from cli_meta; the foreign store
    # always wins when both are present.
    #
    # No-mutation contract (Greptile #4911 follow-up): the GET path passes
    # a pre-computed cli_meta dict and expects it to be unchanged after
    # this helper returns.  We use a single copy-on-write rebind at the
    # top of the block and then plain subscript assignment so the
    # caller's dict is never touched in place.
    if state_db_row:
        cli_meta = dict(cli_meta or {})
        if not cli_meta.get("source_tag") and state_db_source:
            cli_meta["source_tag"] = state_db_source
        if not cli_meta.get("raw_source") and state_db_source:
            cli_meta["raw_source"] = state_db_source
        if not cli_meta.get("title") and state_db_row.get("title"):
            cli_meta["title"] = state_db_row["title"]
        if not cli_meta.get("model") and state_db_row.get("model"):
            cli_meta["model"] = state_db_row["model"]
        if not cli_meta.get("workspace") and state_db_row.get("cwd"):
            cli_meta["workspace"] = state_db_row["cwd"]
        # Map state.db timestamps to created_at/updated_at on the
        # synthesized Session.  Without this, the first POST writes
        # epoch (0) timestamps into the permanent sidecar and the
        # sidebar sorts/dates the session as "Jan 1 1970" (Greptile
        # #4911 follow-up, P1).  created_at always comes from
        # started_at; Session.save() never touches created_at (it's
        # not in the metadata touch list), so the mapping is
        # load-bearing on both the GET stub and the POST claim path.
        # updated_at prefers ended_at (last activity) and falls back
        # to started_at — note that on the POST claim path
        # Session.save() defaults to touch_updated_at=True and stamps
        # updated_at to wall-clock now, so this value is only the
        # GET-stub display value; the claimed sidecar's updated_at
        # reflects the moment of claim (the desired "just now" UX).
        if not cli_meta.get("created_at") and state_db_row.get("started_at"):
            cli_meta["created_at"] = state_db_row["started_at"]
        if not cli_meta.get("updated_at"):
            _ended = state_db_row.get("ended_at")
            _started = state_db_row.get("started_at")
            if _ended or _started:
                cli_meta["updated_at"] = _ended or _started
    claimable, _reason = _is_claimable_cli_source(cli_meta, state_db_source)
    if not claimable:
        # The session is real and viewable, but the foreign source forbids
        # the WebUI from taking write ownership.  Build the Session with
        # readonly=True so the GET stub keeps rendering the original
        # read-only badge, and return 'not_claimable' so the POST path
        # 403s instead of bare-404ing.
        #
        # Delegated subagent children (#5307) additionally must NOT be
        # CLI-classified: they are recovered read-only for viewing, but
        # is_cli_session=True would let them pass the frontend
        # _isExternalSession poll-skip / active-refresh gates that #3603
        # keeps narrow. Every other non-claimable foreign source keeps the
        # CLI classification so its source badge renders.
        _sa_child = _is_subagent_child_session_id(sid)
        return (
            build_session(sid, cli_meta, msgs, read_only_flag=True,
                          is_cli_flag=not _sa_child),
            "not_claimable",
        )
    return build_session(sid, cli_meta, msgs, read_only_flag=False), "materialized"


def _request_wants_all_profiles_import(body) -> bool:
    if not isinstance(body, dict):
        return False
    value = body.get("all_profiles")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_import_profile_value(value):
    profile = str(value or "").strip()
    if not profile:
        return None
    try:
        from api.profiles import _PROFILE_ID_RE
        if profile != "default" and not _PROFILE_ID_RE.fullmatch(profile):
            return ""
    except Exception:
        pass
    return profile


def _load_branch_source_or_refuse(handler, sid: str):
    if _session_is_subagent_view_only(sid):
        bad(handler, "Subagent sessions are view-only and cannot be branched from WebUI", 400)
        return None
    try:
        source = get_session(sid)
    except KeyError:
        _foreign_session, _reason = _claim_or_synthesize_cli_session(sid)
        _source_kind = str((getattr(_foreign_session, "source_tag", None) or getattr(_foreign_session, "raw_source", None) or getattr(_foreign_session, "source", None) or "")).strip().lower() if _foreign_session is not None else ""
        if _reason == "not_claimable" and _foreign_session is not None and _source_kind == "cron":
            _foreign_session._branch_source_readonly = True; return _foreign_session
        if _reason == "not_claimable": bad(handler, "Read-only sessions cannot be branched from WebUI", 403); return None
        bad(handler, "Session not found", 404)
        return None
    # A PERSISTED (stored) session can also be read-only (e.g. a cron-owned or
    # messaging-sourced sidecar). Apply the SAME read-only branch gate as the
    # synthesized path: allow forking only a canonical-cron read-only source
    # (server-authoritative source kind, not the id prefix), marking it so the fork
    # never .save()s the read-only source; refuse every other read-only source.
    if bool(getattr(source, "read_only", False)):
        _source_kind = str((getattr(source, "source_tag", None) or getattr(source, "raw_source", None) or getattr(source, "source", None) or "")).strip().lower()
        if _source_kind == "cron":
            source._branch_source_readonly = True
            return source
        bad(handler, "Read-only sessions cannot be branched from WebUI", 403)
        return None
    return source


def _resolve_cli_import_metadata(session_id: str, *, requested_profile=None, allow_all_profiles: bool = False) -> dict:
    cli_meta = _lookup_cli_session_metadata(session_id)
    if cli_meta and (not requested_profile or _profiles_match(cli_meta.get("profile"), requested_profile)):
        return cli_meta
    if not allow_all_profiles:
        return {}
    cli_meta = _lookup_cli_session_metadata(session_id, all_profiles=True)
    if cli_meta and requested_profile and not _profiles_match(cli_meta.get("profile"), requested_profile):
        return {}
    return cli_meta or {}


def _messaging_session_identity(session: dict, raw_source: str) -> str:
    sid = _safe_first(session.get("session_id"))
    if sid and _is_pre_compression_continuation_row(session):
        return f"{raw_source}|session_id:{sid}"

    metadata = _lookup_gateway_session_identity(session.get("session_id"))
    session_key = _safe_first(
        metadata.get("session_key"),
        session.get("session_key"),
        session.get("gateway_session_key"),
    )
    if session_key:
        return f"{raw_source}|session_key:{session_key}"

    chat_id = _safe_first(
        metadata.get("chat_id"),
        session.get("chat_id"),
        session.get("origin_chat_id"),
    )
    thread_id = _safe_first(metadata.get("thread_id"), session.get("thread_id"))
    chat_type = _safe_first(metadata.get("chat_type"), session.get("chat_type"))
    user_id = _safe_first(
        metadata.get("user_id"),
        session.get("user_id"),
        session.get("origin_user_id"),
    )

    identity_parts = []
    if chat_type:
        identity_parts.append(f"chat_type:{chat_type}")
    if chat_id:
        identity_parts.append(f"chat_id:{chat_id}")
    if thread_id:
        identity_parts.append(f"thread_id:{thread_id}")
    if user_id:
        identity_parts.append(f"user_id:{user_id}")

    if identity_parts:
        return f"{raw_source}|" + "|".join(identity_parts)

    return raw_source


def _is_pre_compression_snapshot_id(session_id: str) -> bool:
    sid = _safe_first(session_id)
    if not sid or not all(c in "0123456789abcdefghijklmnopqrstuvwxyz_" for c in sid):
        return False
    try:
        path = SESSION_DIR / f"{sid}.json"
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("pre_compression_snapshot"))
    except Exception:
        return False


def _is_pre_compression_continuation_row(session: dict) -> bool:
    parent_sid = _safe_first(session.get("parent_session_id"))
    return bool(parent_sid and _is_pre_compression_snapshot_id(parent_sid))


def _session_messaging_raw_source(session: dict) -> str:
    raw = _safe_first(
        session.get("raw_source"),
        session.get("source_tag"),
        session.get("source"),
        session.get("platform"),
    )
    if not raw:
        raw = session.get("source_label") or "messaging"
    return _normalize_messaging_source(raw)


def _has_durable_messaging_identity(session: dict) -> bool:
    metadata = _lookup_gateway_session_identity(session.get("session_id"))
    return bool(_safe_first(
        metadata.get("session_key"),
        session.get("session_key"),
        session.get("gateway_session_key"),
        metadata.get("chat_id"),
        session.get("chat_id"),
        session.get("origin_chat_id"),
        metadata.get("thread_id"),
        session.get("thread_id"),
    ))


def _numeric_count(value) -> int:
    try:
        return int(float(_safe_first(value, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _should_hide_stale_messaging_session(
    session: dict,
    active_gateway_session_ids: set[str],
    active_gateway_sources: set[str],
) -> bool:
    """Hide stale Gateway-owned internal rows after an external chat moved on.

    Hermes Gateway keeps the external conversation identity in sessions.json.
    Compression/session-reset can leave old Agent state.db rows behind; those
    rows are implementation segments, not distinct conversations users chose.
    Only apply this aggressive hiding when Gateway is currently advertising an
    active session for the same messaging source. Without that source-of-truth
    file we keep the old fallback behavior.
    """
    raw_source = _session_messaging_raw_source(session)
    if not _is_known_messaging_source(raw_source):
        return False
    if not active_gateway_session_ids or raw_source not in active_gateway_sources:
        return False

    sid = _safe_first(session.get("session_id"))
    if sid and sid in active_gateway_session_ids:
        return False

    if _safe_first(session.get("end_reason")) in _STALE_MESSAGING_END_REASONS:
        return True

    if not _has_durable_messaging_identity(session):
        if _is_pre_compression_continuation_row(session):
            return False
        parent_sid = _safe_first(session.get("parent_session_id"))
        if parent_sid and parent_sid in active_gateway_session_ids:
            return True
        return True

    if session.get("parent_session_id") and not _is_pre_compression_continuation_row(session):
        return True

    message_count = _numeric_count(session.get("message_count"))
    actual_count = _numeric_count(session.get("actual_message_count"))
    if message_count <= 0 and actual_count <= 0:
        return True

    return False


def _is_messaging_session_record(session) -> bool:
    """Return true for sessions backed by external messaging channels."""
    if not session:
        return False
    if (
        (getattr(session, "session_source", None) if not isinstance(session, dict) else session.get("session_source")) == "messaging"
    ):
        return True
    raw = _safe_first(
        getattr(session, "raw_source", None) if not isinstance(session, dict) else session.get("raw_source"),
        getattr(session, "source_tag", None) if not isinstance(session, dict) else session.get("source_tag"),
        getattr(session, "source", None) if not isinstance(session, dict) else session.get("source"),
        session.get("source_label") if isinstance(session, dict) else None,
    )
    return _is_known_messaging_source(raw)


def _messages_include_tool_metadata(messages) -> bool:
    """Return true when returned messages can reconstruct their own tool cards."""
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if isinstance(msg.get("tool_calls"), list) and msg.get("tool_calls"):
            return True
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_use"
            for part in content
        ):
            return True
    return False


def _tool_calls_for_message_window(tool_calls, start_idx: int, message_count: int) -> list:
    """Keep session-level tool calls that point into a returned message window.

    ``assistant_msg_idx`` is stored in the full transcript coordinate space, but
    the frontend renders the returned ``messages`` array from index 0. Rebase the
    index into the returned window so legacy session-level tool cards still
    anchor to their visible assistant turn after paginated loads.
    """
    if not isinstance(tool_calls, list) or message_count <= 0:
        return []
    end_idx = start_idx + message_count
    filtered = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        assistant_idx = tool_call.get("assistant_msg_idx")
        if isinstance(assistant_idx, bool) or not isinstance(assistant_idx, int):
            continue
        if start_idx <= assistant_idx < end_idx:
            rebased = dict(tool_call)
            rebased["assistant_msg_idx"] = assistant_idx - start_idx
            filtered.append(rebased)
    return filtered


def _message_counts_as_renderable_for_window(message) -> bool:
    """Return true when a paginated window should include this transcript row.

    Tool result rows are rendered through their assistant anchor or hidden as raw
    tool output. Empty partial activity rows can be preserved after cancellation
    to keep thinking/tool details inspectable, but they are not reply text. A
    tail page containing only transient metadata makes the frontend open to
    collapsed activity while newer real replies sit behind "load older messages".
    """
    if not isinstance(message, dict):
        return False
    if _is_empty_partial_activity_message(message):
        return False
    role = str(message.get("role") or "").strip().lower()
    return bool(role and role != "tool")


def _tool_call_ids_in_messages(messages) -> set:
    """Collect tool-call IDs declared on renderable rows (assistant tool_calls /
    partial tool_calls / Anthropic tool_use content blocks) so trailing
    tool-result rows can be matched back to a call present in the window."""
    ids = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for key in ("tool_calls", "_partial_tool_calls"):
            for call in msg.get(key) or []:
                if isinstance(call, dict):
                    cid = call.get("id") or call.get("tool_call_id")
                    if cid:
                        ids.add(str(cid))
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    cid = part.get("id")
                    if cid:
                        ids.add(str(cid))
    return ids


def _tool_result_matches_call_ids(message, call_ids) -> bool:
    """Return True if a role:tool row's tool_call_id/tool_use_id is in ``call_ids``."""
    if not call_ids or not isinstance(message, dict):
        return False
    if str(message.get("role") or "").lower() != "tool":
        return False
    tid = message.get("tool_call_id") or message.get("tool_use_id") or ""
    return bool(tid) and str(tid) in call_ids


def _message_window_for_display(messages, msg_limit=None, msg_before=None, expand_renderable=False) -> tuple[list, int]:
    """Return a paginated message window plus its offset in ``messages``.

    ``msg_limit`` is a visible transcript limit, not a raw storage-row cap.
    Tool result rows are hidden or folded into assistant tool cards, so they
    should not consume the user's "load N messages" budget. Return the smallest
    suffix containing the last ``msg_limit`` renderable user/assistant rows, plus
    any intervening tool rows needed for card snippets.

    ``expand_renderable`` is accepted for compatibility with older frontend
    callers. Visible-row expansion is now the default for every limited window.
    """
    _ = expand_renderable
    messages = list(messages or [])
    if msg_before is not None:
        before_idx = max(0, min(int(msg_before), len(messages)))
    else:
        before_idx = len(messages)
    source = messages[:before_idx]
    if not source:
        return [], 0
    if not msg_limit:
        return source, 0
    limit = max(1, int(msg_limit))
    end_idx = len(source)
    last_renderable_idx = None
    for idx in range(end_idx - 1, -1, -1):
        if _message_counts_as_renderable_for_window(source[idx]):
            last_renderable_idx = idx
            break
    if last_renderable_idx is None:
        start_idx = max(0, end_idx - limit)
        return source[start_idx:end_idx], start_idx
    # Keep the last renderable row, plus any immediately-following tool-result
    # rows whose tool_call_id matches a tool-call on a renderable row already in
    # the window. The renderer rebuilds tool cards (CLI-origin / empty
    # S.toolCalls path) from role:"tool" rows indexed by tool_call_id
    # (static/ui.js resultsByTid), so dropping the result row that follows the
    # newest assistant tool-call would leave that card without its snippet.
    # Orphan trailing tool-only rows (no matching call in the window) are still
    # skipped, preserving the visible-row budget. (#4070 ship-review)
    end_idx = last_renderable_idx + 1
    window_tool_call_ids = _tool_call_ids_in_messages(source[: last_renderable_idx + 1])
    while end_idx < len(source) and not _message_counts_as_renderable_for_window(
        source[end_idx]
    ):
        if _tool_result_matches_call_ids(source[end_idx], window_tool_call_ids):
            end_idx += 1
        else:
            break
    start_idx = 0
    renderable_count = 0
    for idx in range(last_renderable_idx, -1, -1):
        if not _message_counts_as_renderable_for_window(source[idx]):
            continue
        renderable_count += 1
        if renderable_count >= limit:
            start_idx = idx
            break
    window = source[start_idx:end_idx]
    return window, start_idx


_LIMITED_TOOL_CONTENT_MAX_CHARS = 4096
# Server-side ceiling on the ?msg_limit= tail-window size. A client could
# otherwise request msg_limit=1000000 and force the server to assemble and
# serialize an unbounded message payload (the frontend's own pagination grows
# by ~30 at a time, with one outline-jump path asking for 9999). The ceiling is
# generous — far above any legitimate visible-row window — so real pagination is
# unaffected; it only caps the pathological/oversized request. When the request
# exceeds the ceiling the response is silently clamped and _messages_truncated
# is set (the existing truncation signal already covers "more rows exist").
_MAX_MSG_LIMIT = 500


def _parse_msg_limit(raw):
    """Parse and clamp the ``?msg_limit=`` query value.

    Returns a positive int clamped to ``[1, _MAX_MSG_LIMIT]``, or ``None`` when
    the value is absent/empty/malformed (the bare no-``msg_limit`` path, which
    intentionally returns the full transcript for callers that need it).
    Extracted from the handler so the clamp expression has direct test coverage.
    """
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, _MAX_MSG_LIMIT))


# If a sidecar JSON file exceeds this threshold, the display-path tail
# optimization fires regardless of message count.  Sessions with few messages
# but large tool outputs (multi-MB JSON) should not force a full-scan merge.
_SIDECAR_BYTE_TAIL_THRESHOLD = 500_000  # 500 KB
# Defensive row backstop for the GET /api/session display path's state.db read.
# This is NOT a semantic window (the display window counts visible rows
# post-reconciliation via _message_window_for_display); it is a safety net so a
# pathological/huge state.db cannot materialize unbounded rows into memory on the
# display path. Legitimate sessions stay far below this; the compressed-session
# case where _state_db_since_timestamp_for_limited_display bails (and would
# otherwise full-scan) is the main beneficiary. Generous on purpose: no real
# conversation approaches it, and the existing since_timestamp optimization
# already handles the common tail-load case. The full-history model-context
# callers (reconciliation, new-turn context) do NOT use this cap.
_STATE_DB_DISPLAY_ROW_BACKSTOP = 50000


def _state_db_backstop_limit_for_display(session, msg_before) -> int | None:
    """Return the row backstop to apply to the display path's state.db read, or
    ``None`` for an uncapped (full-history) read.

    The backstop is a defensive net against a pathological/huge state.db, NOT a
    semantic window. It is applied ONLY on provably-safe reads where no
    ``truncation_boundary`` prefix is required for the merge:
    ``merge_session_messages_append_only`` needs the rows at/around the session's
    ``truncation_boundary`` to reconcile correctly, and a newest-N-only SQL cap
    would drop those boundary rows for a >N-row session and corrupt the merge
    (silently losing the preserved prefix). So this mirrors the same conditions
    ``_state_db_since_timestamp_for_limited_display`` uses to decide a read is
    boundary-free: not ``msg_before`` paging, and no ``truncation_watermark`` /
    ``truncation_boundary``. Extracted for direct test coverage.
    """
    has_boundary_prefix = (
        msg_before is not None
        or getattr(session, "truncation_watermark", None) not in (None, "")
        or getattr(session, "truncation_boundary", None) not in (None, "")
    )
    return None if has_boundary_prefix else _STATE_DB_DISPLAY_ROW_BACKSTOP


_LIMITED_TOOL_CONTENT_NOTICE = (
    "\n\n[Tool output truncated in paginated session response; "
    "load the full transcript to inspect the complete result.]"
)


def _tool_message_for_limited_payload(message):
    """Return a bounded copy of large hidden tool-result rows for paginated loads."""
    if not isinstance(message, dict) or str(message.get("role") or "").lower() != "tool":
        return message
    content = message.get("content")
    if content in (None, ""):
        return message
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content)
    if len(text) <= _LIMITED_TOOL_CONTENT_MAX_CHARS:
        return message
    clipped = dict(message)
    preview = text[:_LIMITED_TOOL_CONTENT_MAX_CHARS] + _LIMITED_TOOL_CONTENT_NOTICE
    if isinstance(content, str):
        clipped["content"] = preview
    elif isinstance(content, list):
        clipped["content"] = [{"type": "text", "text": preview}]
    elif isinstance(content, dict):
        clipped["content"] = {"_truncated": True, "preview": preview}
    else:
        clipped["content"] = preview
    clipped["_content_truncated"] = True
    clipped["_content_original_chars"] = len(text)
    return clipped


def _messages_for_limited_payload(messages) -> list:
    """Bound hidden tool-result payloads before sending a msg_limit response."""
    return [_tool_message_for_limited_payload(msg) for msg in list(messages or [])]


def _limited_webui_messages_for_display(session, state_db_messages) -> list:
    """Return the display sidecar plus only necessary state.db rows for msg_limit.

    Paginated session loads are latency-sensitive and should not stitch every
    lineage segment before slicing the tail. Keep the lightweight
    pre-compression snapshot stitch so continuation sessions can still reveal
    archived history, then merge only newer state.db rows that have not reached
    the sidecar yet.
    """
    sidecar_messages = _webui_sidecar_lineage_messages_for_display(session)
    return _limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar_messages,
        state_db_messages,
    )


def _limited_webui_messages_for_display_with_sidecar(session, sidecar_messages, state_db_messages) -> list:
    if sidecar_messages is None:
        sidecar_messages = _webui_sidecar_lineage_messages_for_display(session)
    else:
        sidecar_messages = list(sidecar_messages or [])
    state_db_messages = list(state_db_messages or [])
    if not state_db_messages:
        return sidecar_messages
    # NOTE: do not short-circuit to the sidecar when state.db has no strictly
    # newer rows. A state.db row whose timestamp is at-or-before the sidecar's
    # newest (recovery / edited-in-place / missing-timestamp cases) is still
    # absent from the sidecar and must be reconciled — dropping it would render
    # a tail that differs from the full merge path (silent wrong-transcript on
    # the paginated load). The append-only merge is O(n) over already-bounded
    # in-memory lists; the real latency win here is skipping the lineage-parent
    # DISK load above, which we still skip. (#4070 ship-review)
    return merge_session_messages_append_only(
        sidecar_messages,
        state_db_messages,
        truncation_watermark=getattr(session, "truncation_watermark", None),
        truncation_boundary=getattr(session, "truncation_boundary", None),
    )


def _sidecar_file_exceeds_threshold(session_id, threshold_bytes) -> bool:
    """Check if the sidecar JSON file for ``session_id`` exceeds ``threshold_bytes``."""
    from api.config import SESSION_DIR
    try:
        p = SESSION_DIR / f"{session_id}.json"
        return os.path.isfile(p) and os.path.getsize(p) > threshold_bytes
    except Exception:
        return False


def _state_db_since_timestamp_for_limited_display(session, msg_limit, msg_before=None):
    """Return (timestamp floor, sidecar messages) for bounded state.db tail reads.

    The display window limit counts visible transcript rows after WebUI sidecar
    and state.db reconciliation, so this deliberately does not SQL ``LIMIT`` raw
    rows.  Instead, for the common initial tail load, keep the full sidecar
    coordinate space and read a conservative recent state.db superset.  Older
    page loads and edit/truncation recovery shapes stay on the full state.db
    path because their correctness depends on older reconciliation rows.
    """
    if msg_limit is None or msg_before is not None:
        return None, None
    if getattr(session, "truncation_watermark", None) not in (None, ""):
        return None, None
    if getattr(session, "truncation_boundary", None) not in (None, ""):
        return None, None

    sidecar_messages = _webui_sidecar_lineage_messages_for_display(session)
    if not sidecar_messages:
        return None, sidecar_messages
    sidecar_timestamps = [_message_timestamp_as_float(msg) for msg in sidecar_messages]
    if any(ts is None for ts in sidecar_timestamps):
        return None, sidecar_messages

    try:
        limit = max(1, int(msg_limit))
    except (TypeError, ValueError):
        return None, sidecar_messages
    raw_budget = max(300, limit * 10)
    if len(sidecar_messages) <= raw_budget:
        _sid = getattr(session, "session_id", "") or ""
        if not _sid or not _sidecar_file_exceeds_threshold(_sid, _SIDECAR_BYTE_TAIL_THRESHOLD):
            return None, sidecar_messages

    floor = min(sidecar_timestamps[-raw_budget:])
    sidecar_before_count = sum(1 for ts in sidecar_timestamps if ts < floor)
    prefix_summary = get_state_db_session_message_prefix_summary(
        getattr(session, "session_id", None),
        floor,
        profile=getattr(session, "profile", None) or None,
    )
    if prefix_summary is None:
        return None, sidecar_messages
    try:
        state_before_count = int(prefix_summary["count"])
        null_timestamp_count = int(prefix_summary["null_timestamp_count"])
    except (KeyError, TypeError, ValueError):
        return None, sidecar_messages
    if null_timestamp_count or state_before_count != sidecar_before_count:
        return None, sidecar_messages
    if sidecar_before_count == 0:
        return floor, sidecar_messages

    sidecar_before_keys = [
        _session_message_visible_key(msg)
        for msg, ts in zip(sidecar_messages, sidecar_timestamps, strict=True)
        if ts < floor
    ]
    state_before_keys = get_state_db_session_message_keys_before_timestamp(
        getattr(session, "session_id", None),
        floor,
        profile=getattr(session, "profile", None) or None,
    )
    if state_before_keys is None or state_before_keys != sidecar_before_keys:
        return None, sidecar_messages
    return floor, sidecar_messages


def _messages_start_with_visible_prefix(messages, prefix) -> bool:
    """Return True when ``messages`` already replays ``prefix`` in display order."""
    messages = list(messages or [])
    prefix = list(prefix or [])
    if not prefix:
        return True
    if len(messages) < len(prefix):
        return False
    try:
        return all(
            _session_message_visible_key(messages[idx]) == _session_message_visible_key(prefix_msg)
            for idx, prefix_msg in enumerate(prefix)
        )
    except Exception:
        return False


def _webui_sidecar_lineage_messages_for_display(session, *, max_hops: int = 20) -> list:
    """Return WebUI sidecar messages stitched across compression snapshots.

    WebUI compression continuations persist the archived transcript in a parent
    sidecar marked ``pre_compression_snapshot`` and keep subsequent turns in the
    child sidecar. Opening the child alone makes older turns look lost. Stitch
    only those snapshot parents for display; ordinary forks also carry
    ``parent_session_id`` but must remain independent conversations.
    """
    segments = []
    current = session
    session_messages = list(getattr(session, "messages", []) or [])
    source = str(getattr(session, "session_source", "") or "").strip().lower()
    root_is_fork = source == "fork"
    seen = {str(getattr(session, "session_id", "") or "")}
    for _ in range(max(0, int(max_hops))):
        parent_id = str(getattr(current, "parent_session_id", "") or "").strip()
        if not parent_id or parent_id in seen or not is_safe_session_id(parent_id):
            break
        parent = Session.load(parent_id)
        if not parent or not getattr(parent, "pre_compression_snapshot", False):
            break
        parent_source = str(getattr(parent, "session_source", "") or "").strip().lower()
        if root_is_fork and parent_source != "fork":
            break
        if not segments and _messages_start_with_visible_prefix(
            session_messages,
            getattr(parent, "messages", []) or [],
        ):
            return session_messages
        segments.append(parent)
        seen.add(parent_id)
        current = parent

    if not segments:
        return list(getattr(session, "messages", []) or [])

    merged = []
    for segment in reversed(segments):
        merged = merge_session_messages_append_only(
            merged,
            getattr(segment, "messages", []) or [],
            truncation_watermark=getattr(segment, "truncation_watermark", None),
            truncation_boundary=getattr(segment, "truncation_boundary", None),
        )
    return merge_session_messages_append_only(
        merged,
        getattr(session, "messages", []) or [],
        truncation_watermark=None,
    )


def _merged_session_messages_for_display(session, cli_messages=None) -> list:
    """Return the message coordinate space exposed by ``GET /api/session``.

    Messaging sessions can have a WebUI sidecar transcript plus messages from
    the Agent/CLI store. WebUI compression continuations can have an archived
    snapshot parent plus a child continuation sidecar. The frontend computes
    fork keep-counts against this merged display list, so branch/fork must slice
    the same list rather than the sidecar-only ``session.messages`` array.
    """
    cli_messages = list(cli_messages or [])
    sidecar_messages = _webui_sidecar_lineage_messages_for_display(session)
    if cli_messages:
        if sidecar_messages and sidecar_messages != cli_messages:
            if len(sidecar_messages) >= len(cli_messages):
                return merge_session_messages_append_only(
                    sidecar_messages,
                    cli_messages,
                    truncation_watermark=getattr(session, "truncation_watermark", None),
                    truncation_boundary=getattr(session, "truncation_boundary", None),
                )
            merged_messages = []
            seen_message_keys = set()
            for msg in sorted(list(cli_messages) + list(sidecar_messages), key=lambda m: (
                float(m.get("timestamp") or 0),
                str(m.get("role") or ""),
                str(m.get("content") or ""),
            )):
                key = _session_message_merge_key(msg)
                if key in seen_message_keys:
                    continue
                seen_message_keys.add(key)
                merged_messages.append(msg)
            return merged_messages
        return sidecar_messages if len(sidecar_messages) > len(cli_messages) else cli_messages
    return sidecar_messages



def _merged_webui_lineage_messages_for_display(session, messages=None) -> list:
    """Include immediate parent-only rows when a WebUI continuation sidecar is partial.

    Compression/continuation sessions should render as one conversation. Most
    child sidecars are cumulative, so this is usually a cheap no-op. If a child
    sidecar accidentally omits rows that still exist in the immediate parent,
    merge those parent-only rows into the display transcript. Explicit forks and
    generic child-session rows remain isolated; they intentionally start from a
    subset of their parent.
    """
    primary_messages = list(messages if messages is not None else (getattr(session, "messages", []) or []))
    parent_id = str(getattr(session, "parent_session_id", "") or "").strip()
    if not parent_id:
        return primary_messages
    if (
        str(getattr(session, "compression_recovery_source_session_id", "") or "").strip()
        and str(getattr(session, "compression_recovery_action", "") or "").strip()
    ):
        return primary_messages
    source = str(getattr(session, "session_source", "") or "").strip().lower()
    relationship = str(getattr(session, "relationship_type", "") or "").strip().lower()
    if source == "fork" or relationship == "child_session":
        return primary_messages
    try:
        parent = get_session(parent_id, metadata_only=False)
    except Exception:
        return primary_messages
    parent_messages = list(getattr(parent, "messages", []) or [])
    if not parent_messages:
        return primary_messages
    if _messages_start_with_visible_prefix(primary_messages, parent_messages):
        return primary_messages
    merged_messages = []
    seen_message_keys = set()
    seen_messages_by_key = {}
    for msg in sorted(list(parent_messages) + list(primary_messages), key=lambda m: (
        float(m.get("timestamp") or 0),
        str(m.get("role") or ""),
        str(m.get("content") or ""),
    )):
        key = _session_message_merge_key(msg)
        if key in seen_message_keys:
            _merge_session_display_metadata(seen_messages_by_key.get(key), msg)
            continue
        seen_message_keys.add(key)
        seen_messages_by_key[key] = msg
        merged_messages.append(msg)
    return merged_messages


def _message_summary(messages) -> dict:
    messages = list(messages or [])
    last_message_at = 0.0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            last_message_at = max(last_message_at, float(msg.get("timestamp") or 0))
        except (TypeError, ValueError):
            pass
    return {"message_count": len(messages), "last_message_at": last_message_at}


def _metadata_only_message_summary(sid: str, profile: str | None = None) -> dict:
    """Return the cheap message summary used by metadata-only session loads.

    Threads ``profile=`` through to ``get_state_db_session_summary`` so
    background-thread reads land on the correct profile's state.db (per the
    cookie-bound profile selector — fixes the same TLS-vs-thread race the
    #2762 fix addressed for write paths).

    This intentionally does not full-read or merge transcripts.  If state.db has
    grown beyond the sidecar count, report that growth so active-session polling
    can refresh.  If state.db only contains restamped replay rows at or below the
    sidecar count, keep the sidecar metadata so polling does not loop forever on
    a false "newer transcript" signal.
    """
    sidecar_session = Session.load_metadata_only(sid)
    sidecar_count = 0
    sidecar_last_message_at = 0.0
    if sidecar_session:
        sidecar_count = _numeric_count(getattr(sidecar_session, "_metadata_message_count", None))
        if sidecar_count <= 0:
            sidecar_count = _numeric_count(sidecar_session.compact().get("message_count"))
        try:
            sidecar_last_message_at = float(getattr(sidecar_session, "updated_at", 0) or 0)
        except (TypeError, ValueError):
            sidecar_last_message_at = 0.0
        if getattr(sidecar_session, "truncation_watermark", None) is not None:
            # Intentional: once the user has truncated this sidecar, metadata
            # polling must keep the sidecar as authoritative.  A full message
            # load can still apply the watermark-aware merge, but the cheap
            # metadata path should not treat later state.db rows as external
            # growth and resurrect turns the user deliberately cut away.
            return {
                "message_count": sidecar_count,
                "last_message_at": sidecar_last_message_at,
            }
    state_summary = get_state_db_session_summary(sid, profile=profile)
    state_count = _numeric_count(state_summary.get("message_count"))
    try:
        state_last_message_at = float(state_summary.get("last_message_at") or 0)
    except (TypeError, ValueError):
        state_last_message_at = 0.0
    if state_count > sidecar_count and state_last_message_at > sidecar_last_message_at:
        return {
            "message_count": state_count,
            "last_message_at": state_last_message_at,
        }
    return {
        "message_count": sidecar_count,
        "last_message_at": sidecar_last_message_at,
    }


def _session_requires_cli_metadata_lookup(session) -> bool:
    """Return True when a sidecar/session row still needs CLI metadata.

    Legacy imported sidecars may predate the ``read_only`` field and therefore
    load with ``read_only=False``. They still persist ``is_cli_session`` and/or
    source metadata from import time, so those markers intentionally keep them
    on the CLI lookup path while ordinary WebUI-native sessions take the fast
    path.

    Supersedes the simpler is-cli-or-messaging gate from PR #1822 — the new
    gate is strictly more inclusive (also covers ``read_only=True`` sidecars,
    ``session_source`` markers, and source_tag/raw_source/platform metadata)
    so all sessions that previously took the slow path still do, plus a few
    more legacy shapes.
    """
    if not session:
        return False

    def _field(name):
        return session.get(name) if isinstance(session, dict) else getattr(session, name, None)

    if _is_messaging_session_record(session):
        return True
    if bool(_field("is_cli_session")) or bool(_field("read_only")):
        return True
    session_source = _normalize_messaging_source(_safe_first(_field("session_source")))
    if session_source in {"messaging", "external_agent", "external-agent"}:
        return True
    return bool(_safe_first(
        _field("source_tag"),
        _field("raw_source"),
        _field("source"),
        _field("source_label"),
        _field("platform"),
    ))


def _is_messaging_session_id(sid: str) -> bool:
    """Detect messaging-backed sessions from WebUI metadata or Agent rows."""
    try:
        session = Session.load(sid)
        if _is_messaging_session_record(session):
            return True
    except Exception:
        pass
    return _is_messaging_session_record(_lookup_cli_session_metadata(sid))


def _session_sort_timestamp(session: dict) -> float:
    return float(
        _safe_first(
            session.get("last_message_at"),
            session.get("updated_at"),
            session.get("created_at"),
            session.get("started_at"),
            0,
        ) or 0
    ) or 0.0


def _is_cli_session_for_settings(session: dict) -> bool:
    """Return True for importable CLI sessions that are safe to classify for settings."""
    if not isinstance(session, dict):
        return False
    if is_cli_session_row(session):
        return True

    # Fallback for legacy local copies that had weak/empty metadata:
    # keep this conservative so messaging sessions do not collapse incorrectly.
    if not session.get("is_cli_session"):
        return False
    source = str(session.get("source") or "").strip().lower()
    if source in MESSAGING_SOURCES:
        return False
    title = str(session.get("title") or "").strip().lower()
    return title in ("", "untitled", "cli", "cli session") or title.endswith(" session") and (
        not source or source == "cli"
    )


def _normalize_sidebar_source_flags(session: dict) -> dict:
    """Return a sidebar row with the frontend CLI flag matching source metadata."""
    if not isinstance(session, dict):
        return session
    normalized = dict(session)
    normalized["is_cli_session"] = is_cli_session_row(normalized)
    return normalized


def _reconcile_session_detail_source_flags(session: dict, state_meta: dict) -> dict:
    """Return a /api/session payload whose source flags match state.db truth.

    WebUI-origin sidecars can carry stale CLI/import flags after older repair or
    import paths touched the JSON file. The sidebar projection already trusts the
    state.db source row for those sessions; the detail endpoint must do the same
    or the frontend opens a WebUI-native transcript as an external session and
    starts the destructive active-refresh reload loop.
    """
    if not isinstance(session, dict):
        return session
    if not _session_source_is_webui(state_meta):
        return dict(session)

    reconciled = dict(session)
    reconciled["is_cli_session"] = False
    reconciled["read_only"] = False
    reconciled["source_tag"] = _safe_first(state_meta.get("source_tag"), "webui")
    reconciled["raw_source"] = _safe_first(state_meta.get("raw_source"), "webui")
    reconciled["session_source"] = _safe_first(state_meta.get("session_source"), "webui")
    reconciled["source_label"] = _safe_first(state_meta.get("source_label"), "WebUI")
    if state_meta.get("source"):
        reconciled["source"] = state_meta["source"]

    for key in ("message_count", "actual_message_count"):
        if state_meta.get(key) is not None:
            reconciled[key] = max(
                _numeric_count(reconciled.get(key)),
                _numeric_count(state_meta.get(key)),
            )
    for key in ("created_at", "updated_at", "last_message_at"):
        if state_meta.get(key) is not None:
            current = reconciled.get(key)
            try:
                reconciled[key] = max(float(current or 0), float(state_meta.get(key) or 0))
            except (TypeError, ValueError):
                reconciled[key] = state_meta[key]
    return reconciled


def _session_source_is_webui(session: dict) -> bool:
    """Return True for state.db/sidebar rows that describe WebUI-origin sessions."""
    if not isinstance(session, dict):
        return False
    for key in ("source_tag", "raw_source", "session_source", "source"):
        if str(session.get(key) or "").strip().lower() == "webui":
            return True
    return False


def _normalized_source_marker(value) -> str:
    marker = str(value or "").strip().lower()
    if marker.endswith(" session"):
        marker = marker[:-len(" session")].strip()
    return marker.replace("-", "_").replace(" ", "_")


def _is_api_server_sidecar_row(session: dict) -> bool:
    """Return True for API-server imported sidecars that need orphan pruning."""
    if not isinstance(session, dict) or _session_source_is_webui(session):
        return False
    markers = {
        _normalized_source_marker(session.get(key))
        for key in ("source", "source_tag", "raw_source", "session_source", "source_label")
    }
    return bool(markers & {"api", "api_server"})


def _session_lineage_ids(session: dict) -> set[str]:
    """Return known ids that identify one logical sidebar lineage."""
    if not isinstance(session, dict):
        return set()
    ids: set[str] = set()
    for key in ("session_id", "_lineage_root_id", "_lineage_tip_id"):
        value = session.get(key)
        if value:
            ids.add(str(value))
    return ids


def _is_duplicate_webui_state_projection(session: dict, represented_webui_ids: set[str]) -> bool:
    """Return True when a state.db row is only a duplicate WebUI-origin projection.

    The "Show non-WebUI sessions" toggle should add external/agent-owned
    conversations, not make WebUI compression continuations appear only when the
    external-session bridge is enabled. WebUI-origin state.db rows are still
    useful metadata sidecars, but if any id in their compression lineage is
    already represented by WebUI session JSON, they should not be injected as an
    additive external row.
    """
    if not _session_source_is_webui(session):
        return False
    return bool(_session_lineage_ids(session) & represented_webui_ids)


def _dedupe_cli_sidebar_sessions_for_api(
    cli: list[dict],
    represented_webui_ids: set[str],
    *,
    show_cron_sessions: bool = False,
    show_webhook_sessions: bool = False,
) -> list[dict]:
    """Return state sidebar rows while preserving project-hidden background rows.

    Agent-side cron and webhook sessions come from state.db rather than the WebUI
    session store. They should stay hidden from the default sidebar, but
    project-assigned messageful rows must remain in the `/api/sessions` payload
    with `default_hidden` so the matching project chip can reveal them (#3134).
    """
    from api.sessions.store import (
        _hide_from_default_sidebar as _hide_background,
        _include_project_hidden_background_sidebar_sessions,
    )

    candidates = [
        s for s in cli
        if s["session_id"] not in represented_webui_ids
        and not _is_duplicate_webui_state_projection(s, represented_webui_ids)
        and is_cli_session_row_visible(s)
    ]
    visible = [
        s for s in candidates
        if not _hide_background(
            s,
            show_cron=show_cron_sessions,
            show_webhook=show_webhook_sessions,
        )
    ]
    return _include_project_hidden_background_sidebar_sessions(candidates, visible)


CLI_VISIBLE_SESSION_CAP = 20


def _cap_recent_cli_sessions(sessions: list[dict], cli_cap: int = CLI_VISIBLE_SESSION_CAP) -> list[dict]:
    """Keep only the most recent CLI-visible sessions after filtering."""
    if cli_cap <= 0:
        return sessions
    kept = []
    cli_seen = 0
    for session in sessions:
        if _is_cli_session_for_settings(session):
            cli_seen += 1
            if cli_seen > cli_cap:
                continue
        kept.append(session)
    return kept


def _merge_cli_sidebar_metadata(ui_session: dict, cli_meta: dict) -> dict:
    """Merge source-of-truth CLI metadata into a sidebar session row.

    Preserve UI-owned state (archived/pinned) while replacing metadata that can
    legitimately drift in WebUI snapshots.
    """
    if not ui_session:
        return ui_session
    if not cli_meta:
        return dict(ui_session)
    merged = dict(ui_session)
    # Only preserve the CLI flag when the imported metadata is actually a CLI
    # row. WebUI sessions are also mirrored into state.db; treating every
    # matching state row as CLI hides long WebUI continuations from the default
    # sidebar source tab.
    merged["is_cli_session"] = is_cli_session_row(cli_meta)
    for key in (
        "source_tag",
        "raw_source",
        "session_source",
        "source_label",
        "user_id",
        "chat_id",
        "chat_type",
        "thread_id",
        "session_key",
        "platform",
        "parent_session_id",
        "end_reason",
        "actual_message_count",
        "_lineage_root_id",
        "_lineage_tip_id",
        "_compression_segment_count",
    ):
        value = _safe_first(cli_meta.get(key))
        if value:
            merged[key] = value

    if cli_meta.get("created_at") is not None:
        merged["created_at"] = cli_meta["created_at"]
    if cli_meta.get("updated_at") is not None:
        merged["updated_at"] = cli_meta["updated_at"]
    if cli_meta.get("last_message_at") is not None:
        merged["last_message_at"] = cli_meta["last_message_at"]
    if cli_meta.get("message_count") is not None:
        merged["message_count"] = max(
            _numeric_count(merged.get("message_count")),
            _numeric_count(cli_meta.get("message_count")),
        )
    elif cli_meta.get("actual_message_count") is not None:
        merged["message_count"] = max(
            _numeric_count(merged.get("message_count")),
            _numeric_count(cli_meta.get("actual_message_count")),
        )

    if cli_meta.get("title"):
        current_title = merged.get("title")
        if not current_title or current_title == "Untitled":
            merged["title"] = cli_meta["title"]

    if cli_meta.get("model"):
        if not merged.get("model") or merged.get("model") == "unknown":
            merged["model"] = cli_meta["model"]
    return merged


def _messaging_source_key(session: dict) -> str | None:
    raw = _session_messaging_raw_source(session)
    if not _is_known_messaging_source(raw):
        return None
    return _messaging_session_identity(session, raw)


def _keep_latest_messaging_session_per_source(
    sessions: list[dict],
    *,
    show_previous_messaging_sessions: bool = False,
) -> list[dict]:
    """Keep only the newest sidebar row per messaging session identity."""
    if show_previous_messaging_sessions:
        return sorted(sessions, key=_session_sort_timestamp, reverse=True)

    gateway_metadata = _load_gateway_session_identity_map()
    active_gateway_session_ids = {str(sid) for sid in gateway_metadata.keys() if sid}
    session_ids = {
        _safe_first(session.get("session_id"))
        for session in sessions
        if isinstance(session, dict)
    }
    visible_active_gateway_session_ids = active_gateway_session_ids & session_ids
    active_gateway_sources = {
        _normalize_messaging_source(_safe_first(meta.get("raw_source"), meta.get("platform")))
        for sid, meta in gateway_metadata.items()
        if sid in visible_active_gateway_session_ids and isinstance(meta, dict)
    }
    active_gateway_sources = {source for source in active_gateway_sources if _is_known_messaging_source(source)}

    kept_sources: set[str] = set()
    best_by_source: dict[str, dict] = {}
    kept: list[dict] = []
    for session in sessions:
        key = _messaging_source_key(session)
        if not key:
            kept.append(session)
            continue
        if _should_hide_stale_messaging_session(session, visible_active_gateway_session_ids, active_gateway_sources):
            continue
        if key in kept_sources:
            kept_sources.add(key)
            current = best_by_source.get(key)
            if current is None or _session_sort_timestamp(session) > _session_sort_timestamp(current):
                best_by_source[key] = session
            continue
        kept_sources.add(key)
        best_by_source[key] = session

    kept.extend(best_by_source.values())
    kept.sort(key=_session_sort_timestamp, reverse=True)
    return kept


def _publish_materialized_session(session, *, persist: bool = True):
    """Persist a new session before making it reachable through the LRU cache.

    Empty branches intentionally remain memory-only until their first turn;
    callers preserve that contract with ``persist=False``. Every materialized
    session that requires durability must complete its first save before cache
    publication so a failed request cannot leave an in-memory ghost.
    """
    if persist:
        session.save(skip_index=True)
    cache_full_session(session.session_id, session)
    if persist:
        try:
            _write_session_index(updates=[session])
        except Exception:
            # The canonical sidecar and in-process cache are already committed.
            # The compact index is a repairable projection and must not turn a
            # durable endpoint success into a reported failure.
            logger.exception(
                "Failed to refresh session index after materializing %s",
                session.session_id,
            )
    return session


# Initial transcript tails are expensive to rebuild for large, tool-heavy
# sessions even after Session.load() is warm: the route still reconciles
# sidecar/state.db rows, derives todo/tool state, redacts the payload, and
# serializes hundreds of KB. Cache only the final, already-redacted response for
# the ordinary idle native-WebUI path. The cache is a presentation optimization,
# never a state source: every key includes the source files' stat signatures and
# any active/pending, messaging, lineage, or recovery shape bypasses it.
_SESSION_DETAIL_TAIL_CACHE_VERSION = 1
_SESSION_DETAIL_TAIL_CACHE_MAX_ENTRIES = 24
_SESSION_DETAIL_TAIL_CACHE_MAX_BYTES = 24 * 1024 * 1024
_SESSION_DETAIL_TAIL_CACHE_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_SESSION_DETAIL_TAIL_CACHE: "OrderedDict[tuple, tuple[dict, int]]" = OrderedDict()
_SESSION_DETAIL_TAIL_CACHE_BYTES = 0
_SESSION_DETAIL_TAIL_CACHE_LOCK = threading.Lock()


def _session_detail_tail_path_stamp(path) -> tuple | None:
    try:
        path = Path(path)
        st = path.stat()
    except (OSError, TypeError, ValueError):
        return None
    return (
        str(path),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        int(st.st_size),
        int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1_000_000_000))),
    )


def _session_detail_tail_source_stamp(sid: str) -> tuple | None:
    if not is_safe_session_id(sid):
        return None
    sidecar_stamp = _session_detail_tail_path_stamp(SESSION_DIR / f"{sid}.json")
    if sidecar_stamp is None:
        return None
    try:
        state_db_path = Path(_active_state_db_path())
    except Exception:
        state_db_path = None
    state_stamps = ()
    if state_db_path is not None:
        state_stamps = tuple(
            _session_detail_tail_path_stamp(path)
            for path in (
                state_db_path,
                Path(f"{state_db_path}-wal"),
                Path(f"{state_db_path}-shm"),
            )
        )
    try:
        profile_config_path = _active_profile_config_path()
    except Exception:
        profile_config_path = None
    return (
        sidecar_stamp,
        state_stamps,
        _session_detail_tail_path_stamp(SETTINGS_FILE),
        _session_detail_tail_path_stamp(profile_config_path),
    )


def _session_detail_tail_cache_eligible(session) -> bool:
    if session is None:
        return False
    if getattr(session, "active_stream_id", None):
        return False
    if getattr(session, "pending_user_message", None) or getattr(session, "pending_started_at", None):
        return False
    if getattr(session, "parent_session_id", None) or getattr(session, "pre_compression_snapshot", False):
        return False
    if getattr(session, "truncation_watermark", None) not in (None, ""):
        return False
    if getattr(session, "truncation_boundary", None) not in (None, ""):
        return False
    if getattr(session, "read_only", False) or getattr(session, "is_cli_session", False):
        return False
    if _is_messaging_session_record(session) or _session_requires_cli_metadata_lookup(session):
        return False
    source = str(getattr(session, "session_source", "") or "").strip().lower()
    return source in ("", "webui")


def _session_detail_tail_cache_key(
    session,
    *,
    msg_limit: int,
    expand_renderable: bool,
) -> tuple | None:
    if not _session_detail_tail_cache_eligible(session):
        return None
    sid = str(getattr(session, "session_id", "") or "")
    source_stamp = _session_detail_tail_source_stamp(sid)
    if source_stamp is None:
        return None
    return (
        _SESSION_DETAIL_TAIL_CACHE_VERSION,
        sid,
        str(getattr(session, "profile", "") or ""),
        max(1, int(msg_limit)),
        bool(expand_renderable),
        source_stamp,
    )


def _session_detail_tail_cache_get(key: tuple | None) -> dict | None:
    if key is None:
        return None
    with _SESSION_DETAIL_TAIL_CACHE_LOCK:
        entry = _SESSION_DETAIL_TAIL_CACHE.get(key)
        if entry is None:
            return None
        _SESSION_DETAIL_TAIL_CACHE.move_to_end(key)
        return entry[0]


def _session_detail_tail_cache_set(key: tuple | None, payload: dict) -> None:
    global _SESSION_DETAIL_TAIL_CACHE_BYTES
    if key is None or not isinstance(payload, dict):
        return
    try:
        entry_bytes = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return
    if entry_bytes > _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRY_BYTES:
        return
    with _SESSION_DETAIL_TAIL_CACHE_LOCK:
        previous = _SESSION_DETAIL_TAIL_CACHE.pop(key, None)
        if previous is not None:
            _SESSION_DETAIL_TAIL_CACHE_BYTES -= previous[1]
        _SESSION_DETAIL_TAIL_CACHE[key] = (payload, entry_bytes)
        _SESSION_DETAIL_TAIL_CACHE_BYTES += entry_bytes
        while (
            len(_SESSION_DETAIL_TAIL_CACHE) > _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRIES
            or _SESSION_DETAIL_TAIL_CACHE_BYTES > _SESSION_DETAIL_TAIL_CACHE_MAX_BYTES
        ):
            _old_key, (_old_payload, old_bytes) = _SESSION_DETAIL_TAIL_CACHE.popitem(last=False)
            _SESSION_DETAIL_TAIL_CACHE_BYTES -= old_bytes


def _clear_session_detail_tail_cache() -> None:
    global _SESSION_DETAIL_TAIL_CACHE_BYTES
    with _SESSION_DETAIL_TAIL_CACHE_LOCK:
        _SESSION_DETAIL_TAIL_CACHE.clear()
        _SESSION_DETAIL_TAIL_CACHE_BYTES = 0


_COMPRESSION_RECOVERY_START_LOCK = threading.Lock()


def _pre_compression_continuation_session_id(session) -> str | None:
    """Return the newest visible descendant for a hidden compression snapshot.

    Mobile browsers can miss the final SSE `done` handoff while backgrounded.
    On reload they may request the archived pre-compression session id from the
    stale URL/localStorage. The old snapshot is intentionally hidden from the
    sidebar, so expose a lightweight recovery hint when a child continuation
    exists either in memory or on disk. Follow bounded snapshot-to-snapshot hops
    so repeated compression still lands on the latest visible continuation.
    """
    if not getattr(session, "pre_compression_snapshot", False):
        return None
    sid = _safe_first(getattr(session, "session_id", None))
    if not sid:
        return None
    # #2980 hardening: the resolved continuation is written to the client's
    # URL/localStorage, so it must stay within the requested snapshot's own
    # profile. Children are matched only by parent_session_id below; a
    # crafted/corrupt foreign-profile sidecar whose parent_session_id collided
    # with this snapshot's id would otherwise leak cross-profile. Pin the
    # snapshot's profile and reject any child that isn't profile-matched.
    snapshot_profile = getattr(session, "profile", None)

    def _child_rows_from_memory(seen_ids: set[str]) -> list:
        rows = []
        try:
            with LOCK:
                memory_sessions = list(SESSIONS.values())
            for child in memory_sessions:
                child_sid = _safe_first(getattr(child, "session_id", None))
                if not child_sid or child_sid in seen_ids:
                    continue
                seen_ids.add(child_sid)
                rows.append(child)
        except Exception:
            pass
        return rows

    def _child_rows_from_index(seen_ids: set[str]) -> list | None:
        if not SESSION_INDEX_FILE.exists():
            return None
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        try:
            persisted_sidecar_ids = {
                path.stem
                for path in SESSION_DIR.glob("*.json")
                if not path.name.startswith("_") and is_safe_session_id(path.stem)
            }
        except Exception:
            return None
        indexed_ids: set[str] = set()
        row_seen_ids = set(seen_ids)
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            child_sid = _safe_first(entry.get("session_id"))
            if not child_sid or not is_safe_session_id(child_sid):
                continue
            indexed_ids.add(child_sid)
            if child_sid in row_seen_ids or not _safe_first(entry.get("parent_session_id")):
                continue
            row_seen_ids.add(child_sid)
            rows.append(entry)
        # Guarantee here is index MEMBERSHIP-completeness, not per-entry content
        # freshness: if any persisted continuation sidecar is absent from the index
        # we bail to the full scan. A sidecar that IS in the index but whose entry is
        # content-stale (mid-write) still yields a valid continuation of the same
        # snapshot; proving freshness would require reading every sidecar, defeating
        # the optimization, so membership-completeness is the intended bar.
        if persisted_sidecar_ids - indexed_ids - seen_ids:
            return None
        return rows

    def _child_rows_from_sidecars(seen_ids: set[str]) -> list:
        rows = []
        try:
            for path in SESSION_DIR.glob("*.json"):
                if path.name.startswith("_"):
                    continue
                child_sid = path.stem
                if not child_sid or child_sid in seen_ids:
                    continue
                child = Session.load_metadata_only(child_sid)
                if child:
                    seen_ids.add(child_sid)
                    rows.append(child)
        except Exception:
            pass
        return rows

    def _row_value(row, key, default=None):
        return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)

    def _row_has_backing_state(row) -> bool:
        child_sid = _safe_first(_row_value(row, "session_id"))
        if not child_sid or not is_safe_session_id(child_sid):
            return False
        if not isinstance(row, dict):
            return True
        return (SESSION_DIR / f"{child_sid}.json").exists()

    def _resolve_from_rows(rows: list) -> str | None:
        children_by_parent: dict[str, list] = {}
        for child in rows:
            parent_sid = _safe_first(_row_value(child, "parent_session_id"))
            child_sid = _safe_first(_row_value(child, "session_id"))
            if not parent_sid or not child_sid or child_sid == sid:
                continue
            # Cross-profile guard: only follow continuations within the snapshot's profile.
            if not _profiles_match(_row_value(child, "profile"), snapshot_profile):
                continue
            children_by_parent.setdefault(parent_sid, []).append(child)

        candidates = []
        frontier = [sid]
        seen = {sid}
        for _ in range(20):
            if not frontier:
                break
            parent_sid = frontier.pop(0)
            for child in children_by_parent.get(parent_sid, []):
                child_sid = _safe_first(_row_value(child, "session_id"))
                if not child_sid or child_sid in seen or not _row_has_backing_state(child):
                    continue
                seen.add(child_sid)
                if _row_value(child, "pre_compression_snapshot", False):
                    frontier.append(child_sid)
                else:
                    candidates.append(child)

        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda child: (
                float(
                    _safe_first(
                        _row_value(child, "updated_at"),
                        _row_value(child, "created_at"),
                        0,
                    ) or 0
                ),
                # Secondary tiebreaker so the index-fast-path and the sidecar-scan
                # path resolve byte-identically on an updated_at/created_at tie
                # (otherwise the chosen sid could differ by iteration order).
                str(_safe_first(_row_value(child, "session_id"), "") or ""),
            ),
        )
        latest_sid = _safe_first(_row_value(latest, "session_id", None)) or None
        # Only hand the client a well-formed session id (it gets written to URL/localStorage).
        if latest_sid and not is_safe_session_id(latest_sid):
            return None
        return latest_sid

    memory_seen_ids: set[str] = set()
    rows = _child_rows_from_memory(memory_seen_ids)
    index_rows = _child_rows_from_index(memory_seen_ids)
    if index_rows is not None:
        return _resolve_from_rows(rows + index_rows)

    rows.extend(_child_rows_from_sidecars(memory_seen_ids))
    return _resolve_from_rows(rows)


def _session_attention_summary(session_id: str) -> dict | None:
    """Return sidebar attention metadata for pending approval/clarify work."""
    approval_count = 0
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_id)
        queue_list = _pending.get(session_id)
        if isinstance(queue_list, list):
            approval_count = len(queue_list)
        elif queue_list:
            approval_count = 1
    if approval_count > 0:
        return {
            "kind": "approval",
            "count": approval_count,
            "severity": "critical",
        }

    clarify_count = int(get_clarify_pending_count(session_id) or 0)
    if clarify_count > 0:
        return {
            "kind": "clarify",
            "count": clarify_count,
            "severity": "question",
        }
    return None


_SIDEBAR_SESSION_RESPONSE_FIELDS = {
    "session_id",
    "title",
    "display_title",
    "_state_db_title",
    "workspace",
    "model",
    "model_provider",
    "message_count",
    "user_message_count",
    "created_at",
    "updated_at",
    "last_message_at",
    "pinned",
    "archived",
    "project_id",
    "profile",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_hit_percent",
    "personality",
    "context_length",
    "config_context_length",
    "window_usage_percent",
    "source_tag",
    "raw_source",
    "session_source",
    "source_label",
    "is_cli_session",
    "is_messaging_session",
    "is_streaming",
    "active_stream_id",
    "has_pending_user_message",
    "pending_started_at",
    "default_hidden",
    "worktree_path",
    "worktree_branch",
    "parent_session_id",
    "parent_title",
    "parent_source",
    "relationship_type",
    "pre_compression_snapshot",
    "_lineage_root_id",
    "_lineage_tip_id",
    "_compression_segment_count",
    "_lineage_collapsed_count",
    "_parent_lineage_root_id",
    "_parent_lineage_tip_id",
    "_cross_surface_child_session",
    "match_type",
    "match_preview",
    # Preserved so the sidebar can suppress rename / action-menu / swipe on
    # read-only (imported CLI + Claude Code) sessions, and render the detailed
    # gateway model label. Dropping these silently regressed both surfaces.
    # Only the latest `gateway_routing` is included (the sidebar label reader
    # prefers it); the unbounded `gateway_routing_history` is intentionally NOT
    # sent in the list payload to avoid per-row bloat.
    "read_only",
    "is_read_only",
    "gateway_routing",
}


def _sidebar_session_response_item(session: dict, *, redact_enabled: bool | None = None) -> dict:
    """Return the bounded /api/sessions row shape used by the sidebar.

    Full session/detail fields such as messages, tool calls, compression
    summaries, context-engine state, gateway routing history, drafts, and
    pending user text are intentionally excluded from the list endpoint. Large
    installs should not ship tens of KB of per-row detail just to render a
    conversation title.
    """
    item = {
        key: value
        for key, value in dict(session).items()
        if key in _SIDEBAR_SESSION_RESPONSE_FIELDS
    }
    if isinstance(item.get("title"), str):
        item["title"] = _redact_text(item["title"], _enabled=redact_enabled)
    _redact_sidebar_title_fields(item, redact_enabled)
    item["attention"] = _session_attention_summary(str(item.get("session_id") or ""))
    return item


def _redact_sidebar_title_fields(item: dict, redact_enabled: bool | None = None) -> None:
    """Redact every user-content-derived title field on a sidebar/search row in place.

    `title` is redacted by the callers directly (they special-case it), but
    `display_title`, `_state_db_title`, and `parent_title` can ALSO carry raw
    user-message-derived text — e.g. #6056 derives a delegated subagent's
    `display_title` from its first user message, and `parent_title` copies a
    parent session's (possibly derived) title. Without this a credential-shaped
    value in a delegated goal would surface in the sidebar / search results even
    with `api_redact_enabled=True`. Shared by `_sidebar_session_response_item`
    (`/api/sessions`) and every `/api/sessions/search` response branch so the two
    endpoints can never drift on which fields get redacted.
    """
    for field in ("display_title", "_state_db_title", "parent_title"):
        value = item.get(field)
        if isinstance(value, str):
            item[field] = _redact_text(value, _enabled=redact_enabled)


__routes_exports__ = (
    "_lookup_gateway_session_identity",
    "_lookup_cli_session_metadata",
    "_session_index_marks_was_webui",
    "_session_deleted_tombstone_marks_was_webui",
    "_state_db_session_source",
    "_is_subagent_child_session_id",
    "_session_is_subagent_view_only",
    "_is_claimable_cli_source",
    "_claim_or_synthesize_cli_session",
    "_request_wants_all_profiles_import",
    "_normalize_import_profile_value",
    "_load_branch_source_or_refuse",
    "_resolve_cli_import_metadata",
    "_messaging_session_identity",
    "_is_pre_compression_snapshot_id",
    "_is_pre_compression_continuation_row",
    "_session_messaging_raw_source",
    "_has_durable_messaging_identity",
    "_numeric_count",
    "_should_hide_stale_messaging_session",
    "_is_messaging_session_record",
    "_messages_include_tool_metadata",
    "_tool_calls_for_message_window",
    "_message_counts_as_renderable_for_window",
    "_tool_call_ids_in_messages",
    "_tool_result_matches_call_ids",
    "_message_window_for_display",
    "_LIMITED_TOOL_CONTENT_MAX_CHARS",
    "_MAX_MSG_LIMIT",
    "_parse_msg_limit",
    "_SIDECAR_BYTE_TAIL_THRESHOLD",
    "_STATE_DB_DISPLAY_ROW_BACKSTOP",
    "_state_db_backstop_limit_for_display",
    "_LIMITED_TOOL_CONTENT_NOTICE",
    "_tool_message_for_limited_payload",
    "_messages_for_limited_payload",
    "_limited_webui_messages_for_display",
    "_limited_webui_messages_for_display_with_sidecar",
    "_sidecar_file_exceeds_threshold",
    "_state_db_since_timestamp_for_limited_display",
    "_messages_start_with_visible_prefix",
    "_webui_sidecar_lineage_messages_for_display",
    "_merged_session_messages_for_display",
    "_merged_webui_lineage_messages_for_display",
    "_message_summary",
    "_metadata_only_message_summary",
    "_session_requires_cli_metadata_lookup",
    "_is_messaging_session_id",
    "_session_sort_timestamp",
    "_is_cli_session_for_settings",
    "_normalize_sidebar_source_flags",
    "_reconcile_session_detail_source_flags",
    "_session_source_is_webui",
    "_normalized_source_marker",
    "_is_api_server_sidecar_row",
    "_session_lineage_ids",
    "_is_duplicate_webui_state_projection",
    "_dedupe_cli_sidebar_sessions_for_api",
    "CLI_VISIBLE_SESSION_CAP",
    "_cap_recent_cli_sessions",
    "_merge_cli_sidebar_metadata",
    "_messaging_source_key",
    "_keep_latest_messaging_session_per_source",
    "_publish_materialized_session",
    "_SESSION_DETAIL_TAIL_CACHE_VERSION",
    "_SESSION_DETAIL_TAIL_CACHE_MAX_ENTRIES",
    "_SESSION_DETAIL_TAIL_CACHE_MAX_BYTES",
    "_SESSION_DETAIL_TAIL_CACHE_MAX_ENTRY_BYTES",
    "_SESSION_DETAIL_TAIL_CACHE",
    "_SESSION_DETAIL_TAIL_CACHE_BYTES",
    "_SESSION_DETAIL_TAIL_CACHE_LOCK",
    "_session_detail_tail_path_stamp",
    "_session_detail_tail_source_stamp",
    "_session_detail_tail_cache_eligible",
    "_session_detail_tail_cache_key",
    "_session_detail_tail_cache_get",
    "_session_detail_tail_cache_set",
    "_clear_session_detail_tail_cache",
    "_COMPRESSION_RECOVERY_START_LOCK",
    "_pre_compression_continuation_session_id",
    "_session_attention_summary",
    "_SIDEBAR_SESSION_RESPONSE_FIELDS",
    "_sidebar_session_response_item",
    "_redact_sidebar_title_fields",
)
