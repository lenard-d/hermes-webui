"""Validate, identify, and atomically persist settled Anchor scenes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from ..repository import edit_session
from .transcript import (
    _anchor_scene_message_text,
    _anchor_scene_message_turn_duration,
    _anchor_scene_text_has_long_overlap,
    _anchor_scene_text_key,
)

_ANCHOR_ACTIVITY_SCENE_MAX_BYTES = 256_000
_ANCHOR_ACTIVITY_SCENE_MAX_ROWS = 1_000


def _assistant_anchor_scene_message_ref(message) -> str:
    if not isinstance(message, dict):
        return ""
    payload = _assistant_anchor_scene_message_ref_payload(message)
    return _anchor_scene_message_ref_digest(payload)


def _assistant_anchor_scene_message_ref_payload(message) -> dict:
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part.get("input_text") or ""))
            else:
                parts.append(str(part or ""))
        content_text = "\n".join(parts)
    else:
        content_text = str(content or "")
    payload = {
        "role": role,
        "content": " ".join(content_text.split()),
        "timestamp": message.get("_ts") or message.get("timestamp") or "",
    }
    return payload


def _anchor_scene_message_ref_digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sanitize_anchor_activity_scene(scene):
    if not isinstance(scene, dict):
        raise ValueError("scene must be an object")
    if str(scene.get("version") or "") != "activity_scene_v1":
        raise ValueError("scene.version must be activity_scene_v1")
    rows = scene.get("activity_rows")
    if not isinstance(rows, list):
        raise ValueError("scene.activity_rows must be a list")
    if len(rows) > _ANCHOR_ACTIVITY_SCENE_MAX_ROWS:
        raise ValueError("scene.activity_rows is too large")
    scene_copy = copy.deepcopy(scene)
    encoded = json.dumps(scene_copy, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > _ANCHOR_ACTIVITY_SCENE_MAX_BYTES:
        raise ValueError("scene payload is too large")
    return json.loads(encoded.decode("utf-8"))


def _anchor_scene_candidate_matches_scene(candidate, scene) -> bool:
    if not isinstance(scene, dict):
        return True
    final_key = _anchor_scene_text_key(scene.get("final_answer") or "")
    if not final_key:
        return True
    candidate_key = _anchor_scene_text_key(_anchor_scene_message_text(candidate))
    if not candidate_key:
        return False
    if candidate_key == final_key:
        return True
    if len(final_key) >= 16 and final_key in candidate_key:
        return True
    if len(candidate_key) >= 16 and candidate_key in final_key:
        return True
    return _anchor_scene_text_has_long_overlap(candidate_key, final_key)


def _find_anchor_scene_message(messages, *, message_index=None, message_ref="", scene=None):
    if not isinstance(messages, list):
        return None, None
    normalized_message_ref = _normalize_anchor_scene_message_ref(message_ref)
    candidate = None
    if isinstance(message_index, int) and 0 <= message_index < len(messages):
        maybe_candidate = messages[message_index]
        if isinstance(maybe_candidate, dict) and maybe_candidate.get("role") == "assistant":
            candidate = maybe_candidate
    if normalized_message_ref:
        matches = [
            (idx, message)
            for idx, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "assistant"
            and _assistant_anchor_scene_message_ref(message) == normalized_message_ref
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None, None
        if candidate is None:
            return None, None
    if candidate is not None:
        # Content can be normalized or split during settlement; use the explicit
        # index as the durability fallback only after a unique ref did not pick a
        # different assistant message. The index is the full transcript index.
        if normalized_message_ref and not _anchor_scene_candidate_matches_scene(candidate, scene):
            return None, None
        return message_index, candidate
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return idx, message
    return None, None


def _normalize_anchor_scene_message_ref(message_ref) -> str:
    ref = str(message_ref or "").strip()
    if not ref:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{64}", ref):
        return ref.lower()
    try:
        payload = json.loads(ref)
    except (TypeError, ValueError):
        return ref
    if not isinstance(payload, dict):
        return ref
    canonical = {
        "role": str(payload.get("role") or ""),
        "content": " ".join(str(payload.get("content") or "").split()),
        "timestamp": payload.get("timestamp") or "",
    }
    return _anchor_scene_message_ref_digest(canonical)


def _anchor_scene_records(session) -> dict:
    records = getattr(session, "anchor_activity_scenes", None)
    return records if isinstance(records, dict) else {}

class AnchorSceneMessageNotFound(LookupError):
    """The requested assistant message cannot own the submitted scene."""


def persist_anchor_activity_scene(
    session,
    *,
    scene,
    message_index=None,
    message_ref="",
    stream_id="",
) -> dict:
    """Validate and persist one bounded scene against its assistant message.

    The repository edit is intentionally owned here so message selection,
    record replacement, retention, and the sidecar write form one atomic
    session-domain operation. HTTP authorization remains the adapter's job.
    """
    scene = _sanitize_anchor_activity_scene(scene)
    sid = str(getattr(session, "session_id", "") or "")
    should_save = False
    with edit_session(
        sid,
        session=session,
        touch_updated_at=False,
        skip_index=True,
        save_when=lambda _session: should_save,
    ) as current:
        idx, message = _find_anchor_scene_message(
            getattr(current, "messages", None) or [],
            message_index=message_index,
            message_ref=message_ref,
            scene=scene,
        )
        if message is None or idx is None:
            raise AnchorSceneMessageNotFound("Assistant message not found")
        if scene.get("turn_duration") is None:
            duration = _anchor_scene_message_turn_duration(message)
            if duration is not None:
                scene["turn_duration"] = duration
        ref = _assistant_anchor_scene_message_ref(message)
        records = dict(_anchor_scene_records(current))
        records[ref or f"index:{idx}"] = {
            "version": "anchor_activity_scene_record_v1",
            "message_index": idx,
            "message_ref": ref,
            "stream_id": str(stream_id or ""),
            "scene": scene,
            "updated_at": time.time(),
        }
        if len(records) > 256:
            ordered = sorted(
                records.items(),
                key=lambda item: float((item[1] or {}).get("updated_at") or 0),
            )
            records = dict(ordered[-256:])
        current.anchor_activity_scenes = records
        should_save = True
    return {"message_index": idx, "message_ref": ref}
