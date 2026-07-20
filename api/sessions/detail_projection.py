"""Session detail projection across sidecars, lineage, and Agent state."""

from __future__ import annotations

import os

from .cache import get_session
from .state_db import (
    get_state_db_session_message_keys_before_timestamp,
    get_state_db_session_message_prefix_summary,
    get_state_db_session_summary,
)
from .message_identity import (
    _merge_session_display_metadata,
    _message_timestamp_as_float,
    _session_message_merge_key,
    _session_message_visible_key,
)
from .reconciliation import merge_session_messages_append_only
from .records import (
    SESSION_DIR,
    Session,
    is_safe_session_id,
)
from .sources import safe_first as _safe_first

def _numeric_count(value) -> int:
    try:
        return int(float(_safe_first(value, 0) or 0))
    except (TypeError, ValueError):
        return 0


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


state_db_display_row_backstop = _STATE_DB_DISPLAY_ROW_BACKSTOP


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


def numeric_count(value) -> int:
    return _numeric_count(value)


def state_db_backstop(session, msg_before) -> int | None:
    return _state_db_backstop_limit_for_display(session, msg_before)


def limited_state_db_floor(session, msg_limit, *, msg_before=None):
    return _state_db_since_timestamp_for_limited_display(
        session,
        msg_limit,
        msg_before=msg_before,
    )


def merge_session_messages(session, external_messages=None) -> list:
    return _merged_session_messages_for_display(session, external_messages)


def merge_limited_messages(session, sidecar_messages, state_db_messages) -> list:
    return _limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar_messages,
        state_db_messages,
    )


def limited_messages(session, state_db_messages) -> list:
    return _limited_webui_messages_for_display(session, state_db_messages)


def merge_lineage_messages(session, messages=None) -> list:
    return _merged_webui_lineage_messages_for_display(session, messages)


def sidecar_lineage_messages(session) -> list:
    return _webui_sidecar_lineage_messages_for_display(session)


def message_summary(messages) -> dict:
    return _message_summary(messages)


def metadata_summary(session_id: str, profile: str | None = None) -> dict:
    return _metadata_only_message_summary(session_id, profile=profile)
