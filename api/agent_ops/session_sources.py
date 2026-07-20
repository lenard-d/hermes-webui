"""Source classification and continuation policy for Agent-owned sessions."""

MESSAGING_SOURCES = {
    'discord',
    'email',
    'wecom',
    'wecom_callback',
    'slack',
    'telegram',
    'weixin',
}

CLI_MIN_UNTITLED_MESSAGE_COUNT = 6
CLI_MIN_UNTITLED_USER_MESSAGE_COUNT = 2

SOURCE_LABELS = {
    'acp': 'ACP',
    'api_server': 'API',
    'cli': 'CLI',
    'cron': 'Cron',
    'discord': 'Discord',
    'email': 'Email',
    'wecom': 'WeCom',
    'wecom_callback': 'WeCom Callback',
    'slack': 'Slack',
    'telegram': 'Telegram',
    'tool': 'Tool',
    'tui': 'TUI',
    'webhook': 'Webhook',
    'webui': 'WebUI',
    'weixin': 'Weixin',
}


def normalize_agent_session_source(raw_source: str | None) -> dict:
    """Return stable source metadata for Hermes Agent session rows.

    ``sessions.source`` is an Agent-level raw value. WebUI needs a smaller,
    durable contract so routes, SSE snapshots, and future sidebar policies do
    not each reimplement raw-source checks.
    """
    raw = str(raw_source or '').strip().lower() or 'unknown'

    if raw == 'webui':
        session_source = 'webui'
    elif raw in {'acp', 'cli', 'tui'}:
        # 'acp' (Agent Client Protocol adapter — Zed, external device bridges)
        # is a local interactive agent client like the CLI/TUI: its sessions
        # live only in state.db, so classifying it 'other' would leave them
        # invisible in both sidebar buckets (webui skips the state.db
        # projection; cli keeps only CLI-classified rows).
        session_source = 'cli'
    elif raw in MESSAGING_SOURCES:
        session_source = 'messaging'
    elif raw == 'cron':
        session_source = 'cron'
    elif raw == 'webhook':
        session_source = 'webhook'
    elif raw == 'tool':
        session_source = 'tool'
    elif raw == 'api_server':
        session_source = 'api'
    else:
        session_source = 'other'

    label = SOURCE_LABELS.get(raw)
    if not label:
        label = raw.replace('_', ' ').title() if raw != 'unknown' else 'Agent'

    return {
        'raw_source': None if raw == 'unknown' else raw,
        'session_source': session_source,
        'source_label': label,
    }


def _with_normalized_source(row: dict) -> dict:
    normalized = normalize_agent_session_source(row.get('source'))
    return {**row, **normalized}


def _optional_col(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return f"s.{name}" if name in columns else f"{fallback} AS {name}"


def _safe_lower(value) -> str:
    return str(value or "").strip().lower()


def _normalize_source_name(value: object) -> str:
    source = _safe_lower(value)
    if not source:
        return ""
    if source.endswith(" session"):
        source = source[:-len(" session")].strip()
    return source


def _looks_like_default_cli_title(row: dict) -> bool:
    """Return True when a CLI row looks like framework-generated metadata."""
    title = _safe_lower(row.get("title"))
    if not title or title == "untitled":
        return True
    if title in {"cli", "cli session"}:
        return True

    source_candidates = {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("session_source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }
    source_candidates.discard("")
    source_candidates.add("cli")
    return any(title == f"{candidate} session" for candidate in source_candidates)


def _as_positive_int(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_score(*values) -> float:
    """First numerically-coercible value as a float, else 0.0.

    Used to score lineage tips by recency. ``last_message_at`` comes from
    ``MAX(timestamp)`` and is normally a numeric epoch, but older/non-standard
    state.db schemas can store an ISO-8601 *text* timestamp. Rather than letting
    a non-numeric value raise ValueError (which previously escaped the DB
    try-block and dropped all lineage metadata), fall through to the next
    candidate (e.g. ``started_at``).
    """
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _count_user_turns(row: dict) -> int:
    user_turns = row.get("actual_user_message_count")
    if user_turns is None:
        user_turns = row.get("user_message_count")
    if user_turns is None:
        messages = row.get("messages") or []
        if isinstance(messages, list):
            return sum(
                1
                for msg in messages
                if _safe_lower(msg.get("role") if isinstance(msg, dict) else msg) == "user"
            )
        return 0
    return _as_positive_int(user_turns)


def _has_cli_lineage(row: dict) -> bool:
    segment_count = _as_positive_int(row.get("_compression_segment_count"))
    return segment_count > 1 or bool(row.get("_lineage_root_id"))


def is_cli_session_row(row: dict) -> bool:
    """Return True for rows that should be treated as CLI-imported sessions."""
    if not isinstance(row, dict):
        return False
    source = _safe_lower(row.get("session_source"))
    source_tag = _safe_lower(row.get("source_tag"))
    raw_source = _safe_lower(row.get("raw_source"))
    source_name = _safe_lower(row.get("source"))
    source_label = _safe_lower(row.get("source_label"))
    if "webui" in {source, source_tag, raw_source, source_name, source_label}:
        return False
    # 'subagent' is a delegated delegate_task child: view-only, owned by the
    # runner, never a writable WebUI/CLI session (#5307). Classify it non-CLI so
    # sidebar rows and every is_cli_session_row() consumer keep it out of the
    # CLI/writable treatment.
    non_cli_sources = MESSAGING_SOURCES | {"cron", "webhook", "tool", "api", "api_server", "subagent"}
    if {source, source_tag, raw_source, source_name, source_label} & non_cli_sources:
        return False
    if source == "messaging":
        return False
    if source == "cli":
        return True
    # External-agent imports (Claude Code, Codex, etc.) are read-only sessions
    # that Hermes discovers on disk and lists alongside CLI/TUI sessions. The
    # client renderer (static/sessions.js: _isCliSession) files them in the CLI
    # bucket via the is_cli_session fallthrough, so the server session-count
    # classifier MUST agree — otherwise the server counts them under
    # webui_session_count while the client renders them under CLI, and the WebUI
    # filter shows a non-zero count with an empty list (#5831). These carry a
    # real title, so they'd otherwise fall through to the conservative
    # default-title gate below and be misclassified as non-CLI.
    if source in {"external_agent", "external-agent"}:
        return True
    if (
        source_tag in {"acp", "cli", "tui"}
        or raw_source in {"acp", "cli", "tui"}
        or source_name in {"acp", "cli", "tui"}
        or source_label in {"acp", "cli", "tui"}
    ):
        return True

    # Legacy imported CLI rows may only be marked as CLI in sidebar metadata.
    # Keep this conservative to avoid treating messaging sessions as CLI.
    return bool(
        row.get("is_cli_session")
        and source not in MESSAGING_SOURCES
        and source_tag not in MESSAGING_SOURCES
        and raw_source not in MESSAGING_SOURCES
        and source_name not in MESSAGING_SOURCES
        and _looks_like_default_cli_title(row)
    )


def is_cli_session_row_visible(row: dict) -> bool:
    """Return whether a CLI-related row should remain visible in the sidebar."""
    if not isinstance(row, dict):
        return False
    if not is_cli_session_row(row):
        return True

    actual_message_count = _as_positive_int(row.get("actual_message_count"))
    message_count = actual_message_count or _as_positive_int(row.get("message_count"))
    if message_count <= 0:
        return False

    if (
        actual_message_count > 0
        and _count_user_turns(row) > 0
        and row.get("ended_at") is None
        and not row.get("end_reason")
    ):
        return True

    interactive_sources = {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }
    if "tui" in interactive_sources:
        return True
    if "acp" in interactive_sources:
        # Like TUI rows, user-driven ACP sessions stay visible even when
        # ended/untitled. Unlike TUI, an ACP connection can record only
        # assistant/tool/system rows (e.g. a replayed or aborted turn), so
        # require at least one user turn before surfacing the row.
        return _count_user_turns(row) > 0

    if _has_cli_lineage(row):
        return True

    if not _looks_like_default_cli_title(row):
        return True

    return _count_user_turns(row) >= CLI_MIN_UNTITLED_USER_MESSAGE_COUNT


def _is_continuation_session(parent: dict | None, child: dict | None) -> bool:
    """Return True when ``child`` is the next segment of the same conversation.

    Compression rotates session ids automatically. A manual CLI close followed
    by ``hermes -c`` also records a new child session; for sidebar projection it
    should continue the same visible conversation rather than becoming a
    separate child-session row. Plain parent/child links that started before the
    parent's ended boundary remain child sessions.

    Do not collapse lineage across raw sources. A WebUI session that continues
    from a Telegram/CLI/etc. parent must remain visible as its own surface-owned
    conversation; otherwise the tip inherits the root's title/source metadata and
    can disappear under messaging/sidebar policies.
    """
    if not parent or not child:
        return False
    if str(child.get('session_source') or '').strip().lower() == 'fork':
        return False
    parent_source = str(parent.get('source') or '').strip().lower()
    child_source = str(child.get('source') or '').strip().lower()
    if parent_source and child_source and parent_source != child_source:
        return False
    if parent.get('end_reason') not in {'compression', 'cli_close'}:
        return False
    ended_at = parent.get('ended_at')
    if ended_at is None:
        # Older state.db rows/tests may not have ended_at populated. Preserve
        # the historical contract that compression/cli_close parent links are
        # continuations when no boundary timestamp is available.
        return True
    try:
        return float(child.get('started_at') or 0) >= float(ended_at)
    except (TypeError, ValueError):
        return False


def _continuation_root_id(rows_by_id: dict[str, dict], session_id: str | None) -> str | None:
    """Return the visible lineage root for ``session_id`` by walking continuations."""
    if not session_id:
        return None
    root_id = str(session_id)
    current_id = root_id
    seen = {current_id}
    for _ in range(len(rows_by_id) + 1):
        current = rows_by_id.get(current_id)
        parent_id = current.get('parent_session_id') if current else None
        parent = rows_by_id.get(parent_id) if parent_id else None
        if not parent or not _is_continuation_session(parent, current):
            return root_id
        if parent_id in seen:
            return root_id
        root_id = str(parent_id)
        current_id = str(parent_id)
        seen.add(current_id)
    return root_id
