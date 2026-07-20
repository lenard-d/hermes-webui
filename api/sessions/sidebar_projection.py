"""Sidebar filtering, aggregation, and bounded response projection."""

from __future__ import annotations

import json

from api.agent_ops import MESSAGING_SOURCES, is_cli_session_row, is_cli_session_row_visible
from api.clarify import pending_count as clarify_pending_count
from api.helpers import _redact_text
from api.route_approvals import pending_count as approval_pending_count
from .detail_projection import _numeric_count
from .gateway_identity import gateway_session_identity, load_gateway_session_identity_map
from .materialization import _lookup_cli_session_metadata
from .records import SESSION_DIR, Session
from .sidebar import (
    _hide_from_default_sidebar,
    _include_project_hidden_background_sidebar_sessions,
)
from .sources import (
    is_known_messaging_source as _is_known_messaging_source,
    is_messaging_session_record as _is_messaging_session_record,
    normalize_messaging_source as _normalize_messaging_source,
    safe_first as _safe_first,
)

_STALE_MESSAGING_END_REASONS = {"session_reset", "session_switch"}


def _lookup_gateway_session_identity(session_id: str) -> dict:
    return gateway_session_identity(session_id)


def _load_gateway_session_identity_map() -> dict[str, dict]:
    return load_gateway_session_identity_map()

def _messaging_session_identity(
    session: dict,
    raw_source: str,
    *,
    gateway_metadata: dict | None = None,
) -> str:
    sid = _safe_first(session.get("session_id"))
    if sid and _is_pre_compression_continuation_row(session):
        return f"{raw_source}|session_id:{sid}"

    metadata = gateway_metadata
    if metadata is None:
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
    _hide_background = _hide_from_default_sidebar

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


def _messaging_source_key(
    session: dict,
    *,
    gateway_metadata: dict | None = None,
) -> str | None:
    raw = _session_messaging_raw_source(session)
    if not _is_known_messaging_source(raw):
        return None
    return _messaging_session_identity(
        session,
        raw,
        gateway_metadata=gateway_metadata,
    )


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
        session_id = _safe_first(session.get("session_id"))
        key = _messaging_source_key(
            session,
            gateway_metadata=gateway_metadata.get(session_id) if session_id else None,
        )
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


def _session_attention_summary(session_id: str) -> dict | None:
    """Return sidebar attention metadata for pending approval/clarify work."""
    approval_count = int(approval_pending_count(session_id) or 0)
    if approval_count > 0:
        return {
            "kind": "approval",
            "count": approval_count,
            "severity": "critical",
        }

    clarify_count = int(clarify_pending_count(session_id) or 0)
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


cli_visible_session_cap = CLI_VISIBLE_SESSION_CAP


def normalize_source_flags(session: dict) -> dict:
    return _normalize_sidebar_source_flags(session)


def is_cli_session(session: dict) -> bool:
    return _is_cli_session_for_settings(session)


def is_api_server_sidecar(session: dict) -> bool:
    return _is_api_server_sidecar_row(session)


def source_is_webui(session: dict) -> bool:
    return _session_source_is_webui(session)


def lineage_ids(session: dict) -> set[str]:
    return _session_lineage_ids(session)


def merge_external_metadata(session: dict, metadata: dict) -> dict:
    return _merge_cli_sidebar_metadata(session, metadata)


def dedupe_external_rows(
    sessions: list[dict],
    represented_webui_ids: set[str],
    *,
    show_cron_sessions: bool = False,
    show_webhook_sessions: bool = False,
) -> list[dict]:
    return _dedupe_cli_sidebar_sessions_for_api(
        sessions,
        represented_webui_ids,
        show_cron_sessions=show_cron_sessions,
        show_webhook_sessions=show_webhook_sessions,
    )


def keep_latest_messaging(
    sessions: list[dict],
    *,
    show_previous_messaging_sessions: bool = False,
) -> list[dict]:
    return _keep_latest_messaging_session_per_source(
        sessions,
        show_previous_messaging_sessions=show_previous_messaging_sessions,
    )


def cap_recent_cli(sessions: list[dict], *, cli_cap: int) -> list[dict]:
    return _cap_recent_cli_sessions(sessions, cli_cap=cli_cap)


def attention(session_id: str) -> dict | None:
    return _session_attention_summary(session_id)


def response_item(
    session: dict,
    *,
    redact_enabled: bool | None = None,
) -> dict:
    return _sidebar_session_response_item(session, redact_enabled=redact_enabled)


def redact_titles(item: dict, redact_enabled: bool | None = None) -> None:
    _redact_sidebar_title_fields(item, redact_enabled)


def reconcile_detail_source_flags(session: dict, metadata: dict) -> dict:
    return _reconcile_session_detail_source_flags(session, metadata)


def is_messaging_session(session_id: str) -> bool:
    return _is_messaging_session_id(session_id)
