"""Cheap sidecar metadata parsing, fingerprints, and legacy fact caching."""

from __future__ import annotations

import collections
import json
import threading
from pathlib import Path

_SAFE_SID_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
)
LEGACY_SIDECAR_FACTS_LOCK = threading.Lock()
LEGACY_SIDECAR_FACTS: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
LEGACY_SIDECAR_FACTS_MAX = 2000


def is_safe_session_id(sid) -> bool:
    """Return whether *sid* is a non-empty traversal-safe sidecar identifier."""
    return bool(sid) and isinstance(sid, str) and all(c in _SAFE_SID_CHARS for c in sid)

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


def _anchor_scene_index_from_records(records) -> dict:
    """Build the compact anchor-scene fingerprint {scene_key: updated_at} (#5854).

    This is the freshness signal the sidebar-poll comparison needs — scene keys
    plus each scene's ``updated_at`` — WITHOUT the 250-480KB bodies. Persisted in
    the metadata prefix (before ``messages``) so ``load_metadata_only`` and
    ``_persisted_session_meta_prefix`` stay cheap. Mirrors exactly what
    ``_anchor_scene_record_keys`` / ``_anchor_scene_records_updated_at`` read off
    the full records, so the fingerprint comparison is behavior-identical.
    """
    if not isinstance(records, dict):
        return {}
    index = {}
    for key, value in records.items():
        if not key or not isinstance(value, dict):
            continue
        try:
            updated_at = float(value.get('updated_at') or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        index[str(key)] = updated_at
    return index


def _disk_scene_fingerprint(disk_meta_prefix: dict):
    """Resolve the (scene_keys, max_updated_at) freshness signal from a parsed
    metadata prefix dict, preferring the modern ``anchor_scene_index`` and
    falling back to the full ``anchor_activity_scenes`` bodies for legacy files.

    Returns ``None`` when the prefix carries NEITHER field, so callers can tell
    "no scenes" (empty dict/index present) apart from "couldn't determine"
    (legacy file whose scenes serialize after ``messages`` and so aren't in the
    prefix) and fall through to the full metadata load instead of assuming zero.
    """
    if not isinstance(disk_meta_prefix, dict):
        return None
    if 'anchor_scene_index' in disk_meta_prefix:
        raw = disk_meta_prefix.get('anchor_scene_index')
        raw = raw if isinstance(raw, dict) else {}
        keys = {str(k) for k in raw}
        latest = 0.0
        for v in raw.values():
            try:
                fv = float(v or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    if 'anchor_activity_scenes' in disk_meta_prefix:
        records = disk_meta_prefix.get('anchor_activity_scenes')
        records = records if isinstance(records, dict) else {}
        keys = {str(k) for k, val in records.items() if k and isinstance(val, dict)}
        latest = 0.0
        for val in records.values():
            if not isinstance(val, dict):
                continue
            try:
                fv = float(val.get('updated_at') or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    return None


def _sidecar_stat_signature(path):
    """Stat signature for a sidecar path, or None if it can't be stat'd.

    Any edit (atomic-rename or in-place) changes at least one component, so a
    cached entry keyed by this signature is auto-invalidated on the next write.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))),
            int(st.st_size), int(getattr(st, 'st_ctime_ns', int(st.st_ctime * 1_000_000_000))))


def legacy_sidecar_facts_get(session_dir: Path, sid):
    """Return cached authoritative facts for a LEGACY sidecar, or None (#5854).

    Only returns a hit when the file's current stat signature matches the cached
    one, so a stale entry can never be served after an edit.
    """
    if not is_safe_session_id(sid):
        return None
    sig = _sidecar_stat_signature(session_dir / f'{sid}.json')
    if sig is None:
        return None
    with LEGACY_SIDECAR_FACTS_LOCK:
        hit = LEGACY_SIDECAR_FACTS.get(sig)
        if hit is not None:
            LEGACY_SIDECAR_FACTS.move_to_end(sig)
            return dict(hit)
    return None


def legacy_sidecar_facts_put(session_dir: Path, sid, message_count, scene_index, *, expected_sig):
    """Cache authoritative facts for a legacy sidecar keyed by its stat signature.

    #5854 TOCTOU guard: ``expected_sig`` (MANDATORY) is the signature captured
    BEFORE the caller parsed the file. The facts were derived from that snapshot,
    so we only cache when the file's CURRENT signature still equals it —
    otherwise the file was atomically replaced during the parse and these facts
    describe the old content; caching them under the new signature would serve
    stale data. Pass ``None`` explicitly only if the caller genuinely has no
    snapshot (then this is a no-op, refusing to cache unverified facts).
    """
    if not is_safe_session_id(sid):
        return
    if expected_sig is None:
        return
    sig = _sidecar_stat_signature(session_dir / f'{sid}.json')
    if sig is None:
        return
    if sig != expected_sig:
        # File changed under us during the parse — do not cache stale facts.
        return
    entry = {"message_count": message_count,
             "scene_index": dict(scene_index) if isinstance(scene_index, dict) else {}}
    with LEGACY_SIDECAR_FACTS_LOCK:
        LEGACY_SIDECAR_FACTS[sig] = entry
        LEGACY_SIDECAR_FACTS.move_to_end(sig)
        while len(LEGACY_SIDECAR_FACTS) > LEGACY_SIDECAR_FACTS_MAX:
            LEGACY_SIDECAR_FACTS.popitem(last=False)


# Load-time normalization belongs to the serialized record owner.
def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for partial assistant markers recovered on load."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )


def _collapse_adjacent_duplicate_partials(messages) -> tuple[list, bool]:
    """Collapse repeated identical partial markers from the same failed turn."""
    if not isinstance(messages, list):
        return messages, False
    collapsed = []
    changed = False
    previous_partial_sig = None
    for message in messages:
        if isinstance(message, dict) and message.get('_partial'):
            sig = _partial_message_signature(message)
            if previous_partial_sig == sig:
                changed = True
                continue
            previous_partial_sig = sig
        else:
            previous_partial_sig = None
        collapsed.append(message)
    return collapsed, changed
