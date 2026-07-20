"""Recovered context and sidecar metadata parsing.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _content_has_reasoning_only_parts(content) -> bool:
    if not isinstance(content, list) or not content:
        return False
    saw_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            if str(part or '').strip():
                return False
            continue
        part_type = str(part.get('type') or '').lower()
        if part_type in {'thinking', 'reasoning'}:
            text = part.get('thinking') or part.get('reasoning') or part.get('text') or ''
            if str(text).strip():
                saw_reasoning = True
            continue
        if part_type == 'text' and str(part.get('text') or part.get('content') or '').strip():
            return False
        if part_type not in {'text', 'thinking', 'reasoning'}:
            return False
    return saw_reasoning


def _active_stream_ids():
    # Runtime ownership combines live transports with detached workers so stale
    # repair cannot misclassify a provider-blocked or cancelling run as dead.
    return set(_cfg.runtime_active_run_ids())


def _recovered_model_context_projection(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    projected = dict(message)
    projected.pop('reasoning', None)
    if projected.get('_error'):
        return None
    if _content_has_reasoning_only_parts(projected.get('content')):
        if projected.get('tool_calls'):
            projected['content'] = ''
        else:
            return None
    projected_text = _normalize_journal_recovery_text(projected.get('content'))
    if not projected_text and not projected.get('tool_call_id') and not projected.get('tool_calls'):
        return None
    return projected


def _append_recovered_context_projection(
    session,
    context_messages: list,
    recovered: dict,
) -> None:
    recovered_text = _normalize_journal_recovery_text(recovered.get('content'))
    if recovered_text:
        if recovered.get('role') == 'user':
            if _message_matches_pending_checkpoint(
                context_messages[-1] if context_messages else None,
                recovered.get('content'),
                recovered.get('timestamp'),
                recovered.get('_source'),
                recovered.get('attachments'),
            ):
                return
        else:
            for existing in reversed(context_messages[-8:]):
                if not isinstance(existing, dict) or existing.get('role') != recovered.get('role'):
                    continue
                if _normalize_journal_recovery_text(existing.get('content')) == recovered_text:
                    return
    context_messages.append(dict(recovered))


def _seed_recovered_context_from_messages(session, context_messages: list) -> None:
    for message in getattr(session, 'messages', None) or []:
        projected = _recovered_model_context_projection(message)
        if projected is None:
            continue
        context_messages.append(projected)


def _append_recovered_turn_to_context(session, recovered: dict) -> None:
    context_messages = getattr(session, 'context_messages', None)
    if not isinstance(context_messages, list):
        context_messages = []
        session.context_messages = context_messages
    if not context_messages:
        _seed_recovered_context_from_messages(session, context_messages)
    projected = _recovered_model_context_projection(recovered)
    if projected is None:
        return
    _append_recovered_context_projection(session, context_messages, projected)


def _append_recovered_pending_turn(session, *, timestamp: int | None = None) -> dict | None:
    pending_text = str(session.pending_user_message or '')
    if not pending_text:
        return None
    recovered_ts = int(time.time())
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        recovered_ts = int(timestamp)
    recovered: dict = {
        'role': 'user',
        'content': session.pending_user_message,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    pending_source = getattr(session, 'pending_user_source', None)
    if pending_source and pending_source != 'webui':
        recovered['_source'] = pending_source
    if session.pending_attachments:
        recovered['attachments'] = list(session.pending_attachments)
    session.messages.append(recovered)
    _append_recovered_turn_to_context(session, recovered)
    # The new user turn is now committed to messages (#3831): advance the
    # truncation watermark to the new message's timestamp so that
    # merge_session_messages_append_only() still filters out replaced
    # pre-edit rows from state.db whose timestamps fall below the boundary.
    # The merge's sidecar_advanced_past_watermark guard allows state.db rows
    # newer than the watermark, so post-edit turns are not dropped.
    # Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = recovered_ts
    return recovered


def _is_streaming_session(active_stream_id, active_stream_ids):
    return bool(active_stream_id and active_stream_id in active_stream_ids)

def _session_sort_timestamp(session):
    if isinstance(session, dict):
        return session.get('last_message_at') or session.get('updated_at') or 0
    return _last_message_timestamp(getattr(session, 'messages', None)) or getattr(session, 'updated_at', 0) or 0


def _message_timestamp(message):
    if not isinstance(message, dict):
        return None
    raw = message.get('_ts') or message.get('timestamp')
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_empty_partial_activity_message(message):
    """Return True for cancelled/recovered activity rows with no reply text."""
    if not isinstance(message, dict):
        return False
    if message.get('role') != 'assistant' or not message.get('_partial'):
        return False
    content = message.get('content', '')
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text' and str(part.get('text') or part.get('content') or '').strip():
                    return False
                continue
            if str(part or '').strip():
                return False
        return True
    return not str(content or '').strip()


def _last_message_timestamp(messages, *, tail_window: int = 8):
    """perf(session-load-latency) Priority 1: bounded tail-scan.

    Old behavior: reversed-iterate ALL messages until a non-tool, non-empty
    message's timestamp is found. For a 2,730-message session on eMMC, that's
    ~500ms of Python attribute lookups, repeated on every /api/session
    response.

    New behavior: the messages array is chronologically ordered, so the
    last non-tool message is at the very end. We scan only the last
    ``tail_window`` messages — covers the realistic case where 1-3 tool
    rows sit after the last assistant/user message. Falls back to a full
    scan only when no timestamp is found in the window, which preserves
    exact correctness for messages with very large trailing tool clusters
    (rare in practice; we'd need >8 consecutive tool rows to hit it).
    """
    if not isinstance(messages, list):
        return None
    n = len(messages)
    start = max(0, n - max(1, int(tail_window)))
    # Walk from the end backwards. reversed() over a slice still creates
    # a full reverse iterator, but only the slice's elements are touched.
    for message in reversed(messages[start:]):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    # Window miss — fall back to the original full-reversed scan. The
    # caller pays this cost only when the heuristic didn't find a hit,
    # which means the session is unusual (long tool tail or all-empty
    # messages).
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    return None


def _message_role(message):
    if not isinstance(message, dict):
        return ''
    return str(message.get('role', '')).strip().lower()


def _find_top_level_json_key(text, key):
    """Return the byte offset of a top-level JSON object key, if present."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            start = i
            i += 1
            escaped = False
            chars = []
            while i < n:
                c = text[i]
                if escaped:
                    chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    break
                else:
                    chars.append(c)
                i += 1
            if i >= n:
                return None
            if depth == 1 and ''.join(chars) == key:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] == ':':
                    return start
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return None


def _read_file_head(path: Path, max_prefix_bytes: int = 4096) -> str:
    """Read at most ``max_prefix_bytes`` bytes from ``path`` and decode UTF-8."""
    if not isinstance(path, Path):
        path = Path(path)
    if max_prefix_bytes <= 0:
        return ''
    with path.open('rb') as fp:
        return fp.read(max_prefix_bytes).decode('utf-8', errors='ignore')


def _read_metadata_json_prefix(path, max_prefix_bytes=65536):
    """Read only the metadata portion before the large arrays.

    #5854: stop at the top-level ``messages`` key OR the top-level
    ``anchor_activity_scenes`` key, whichever appears first. On the modern
    layout scenes serialize AFTER ``messages`` so this stops at ``messages`` as
    before (the prefix is now small — scene bodies are no longer in it). On the
    LEGACY layout scenes serialize BEFORE ``messages`` and can be 250-480KB, so
    stopping at ``anchor_activity_scenes`` keeps the read cheap and — critically
    — still captures ``message_count`` (which is written before both). Without
    the scenes-stop a legacy large-scene sidecar overflows ``max_prefix_bytes``
    and forces a full multi-MB parse on every poll (the #4633 churn).
    """
    buf = ''
    with open(path, 'r', encoding='utf-8') as f:
        while len(buf.encode('utf-8')) < max_prefix_bytes:
            chunk = f.read(4096)
            if not chunk:
                return None
            buf += chunk
            stop_pos = _find_top_level_json_key(buf, 'messages')
            scenes_pos = _find_top_level_json_key(buf, 'anchor_activity_scenes')
            if scenes_pos is not None and (stop_pos is None or scenes_pos < stop_pos):
                stop_pos = scenes_pos
            if stop_pos is None:
                continue
            prefix = buf[:stop_pos].rstrip()
            if prefix.endswith(','):
                prefix = prefix[:-1].rstrip()
            return f'{prefix}\n}}'
    return None


def _load_session_from_path(path: Path) -> "Session | None":
    """Load a session from an explicit JSON path without consulting SESSION_DIR."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
    return Session(**data)


def _lookup_index_message_count(session_id):
    """Return the indexed message count without loading the full session file."""
    return _index_message_count_map().get(str(session_id))


def _index_message_count_map(entries=None) -> dict[str, int]:
    """Return indexed message counts keyed by session id.

    ``load_metadata_only()`` is called in loops for stale lineage/sidebar rows.
    Reading and parsing ``_index.json`` once per row turns /api/sessions into an
    accidental O(n²) poll for old sidecars that predate persisted
    ``message_count``. Accepting already-loaded index rows lets callers reuse
    the index they just parsed.
    """
    if entries is None:
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return {}
    if not isinstance(entries, list):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('session_id') or '')
        if not sid:
            continue
        count = entry.get('message_count')
        if not isinstance(count, int):
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
        if count >= 0:
            counts[sid] = count
    return counts


def _parse_nonnegative_int(value):
    if isinstance(value, int) and value >= 0:
        return value
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def model_explicit_pick_signature(model, model_provider) -> str:
    """Stable signature of a (model, provider) selection for #5979 explicit-pick
    provenance. The persisted ``Session.model_explicit_pick_signature`` is set to
    this when the user deliberately picks a model; the streaming resolver only
    treats a selection as deliberate when the CURRENT routing context produces
    the same signature. Any model/provider change (chat-start, session-update,
    normalization, provider repair) yields a different signature and thus
    invalidates the stale pick — so a #433 first-party leftover is never wrongly
    preserved. Uses \\x1f (unit separator) so it can't collide with model ids.
    """
    _m = str(model or "").strip()
    _p = str(model_provider or "").strip().lower()
    return f"{_m}\x1f{_p}"

__all__ = ['_content_has_reasoning_only_parts', '_active_stream_ids', '_recovered_model_context_projection', '_append_recovered_context_projection', '_seed_recovered_context_from_messages', '_append_recovered_turn_to_context', '_append_recovered_pending_turn', '_is_streaming_session', '_session_sort_timestamp', '_message_timestamp', '_is_empty_partial_activity_message', '_last_message_timestamp', '_message_role', '_find_top_level_json_key', '_read_file_head', '_read_metadata_json_prefix', '_load_session_from_path', '_lookup_index_message_count', '_index_message_count_map', '_parse_nonnegative_int', 'model_explicit_pick_signature']
