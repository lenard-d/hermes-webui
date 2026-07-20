"""Session detail projection across sidecars, lineage, and Agent state."""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from api.config import SETTINGS_FILE, get_config_path
from api.profiles import _profiles_match, get_active_hermes_home
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
    LOCK,
    SESSIONS,
    SESSION_DIR,
    SESSION_INDEX_FILE,
    Session,
    _is_empty_partial_activity_message,
    is_safe_session_id,
)
from .sources import (
    is_messaging_session_record as _is_messaging_session_record,
    requires_external_metadata_lookup as _session_requires_cli_metadata_lookup,
    safe_first as _safe_first,
)
from .state_db import _active_state_db_path


def _active_profile_config_path() -> Path:
    try:
        return Path(get_active_hermes_home()) / "config.yaml"
    except Exception:
        return get_config_path()

def _numeric_count(value) -> int:
    try:
        return int(float(_safe_first(value, 0) or 0))
    except (TypeError, ValueError):
        return 0


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


class SessionDetailProjection:
    """Deep interface for reconciled and bounded session detail payloads."""

    max_message_limit = _MAX_MSG_LIMIT

    @staticmethod
    def numeric_count(value) -> int:
        return _numeric_count(value)

    @staticmethod
    def parse_message_limit(raw):
        return _parse_msg_limit(raw)

    @staticmethod
    def state_db_backstop(session, msg_before) -> int | None:
        return _state_db_backstop_limit_for_display(session, msg_before)

    @staticmethod
    def limited_state_db_floor(session, msg_limit, *, msg_before=None):
        return _state_db_since_timestamp_for_limited_display(
            session,
            msg_limit,
            msg_before=msg_before,
        )

    @staticmethod
    def merge_session_messages(session, external_messages=None) -> list:
        return _merged_session_messages_for_display(session, external_messages)

    @staticmethod
    def merge_limited_messages(session, sidecar_messages, state_db_messages) -> list:
        return _limited_webui_messages_for_display_with_sidecar(
            session,
            sidecar_messages,
            state_db_messages,
        )

    @staticmethod
    def limited_messages(session, state_db_messages) -> list:
        return _limited_webui_messages_for_display(session, state_db_messages)

    @staticmethod
    def merge_lineage_messages(session, messages=None) -> list:
        return _merged_webui_lineage_messages_for_display(session, messages)

    @staticmethod
    def sidecar_lineage_messages(session) -> list:
        return _webui_sidecar_lineage_messages_for_display(session)

    @staticmethod
    def message_summary(messages) -> dict:
        return _message_summary(messages)

    @staticmethod
    def metadata_summary(session_id: str, profile: str | None = None) -> dict:
        return _metadata_only_message_summary(session_id, profile=profile)

    @staticmethod
    def message_window(
        messages,
        msg_limit=None,
        msg_before=None,
        expand_renderable=False,
    ):
        return _message_window_for_display(
            messages,
            msg_limit,
            msg_before,
            expand_renderable,
        )

    @staticmethod
    def bounded_messages(messages) -> list:
        return _messages_for_limited_payload(messages)

    @staticmethod
    def window_tool_calls(tool_calls, start_idx: int, message_count: int) -> list:
        return _tool_calls_for_message_window(tool_calls, start_idx, message_count)

    @staticmethod
    def cached_tail(session, *, msg_limit: int, expand_renderable: bool):
        if not _session_detail_tail_cache_eligible(session):
            return None, None
        key = _session_detail_tail_cache_key(
            session,
            msg_limit=msg_limit,
            expand_renderable=expand_renderable,
        )
        return key, _session_detail_tail_cache_get(key)

    @staticmethod
    def tail_cache_eligible(session) -> bool:
        return _session_detail_tail_cache_eligible(session)

    @staticmethod
    def tail_cache_key(session, *, msg_limit: int, expand_renderable: bool):
        return _session_detail_tail_cache_key(
            session,
            msg_limit=msg_limit,
            expand_renderable=expand_renderable,
        )

    @staticmethod
    def store_cached_tail(key, payload) -> None:
        _session_detail_tail_cache_set(key, payload)

    @staticmethod
    def continuation_session_id(session) -> str | None:
        return _pre_compression_continuation_session_id(session)


session_detail_projection = SessionDetailProjection()
