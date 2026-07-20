"""Hydrate persisted Anchor scenes from settled transcript and tool evidence."""

from __future__ import annotations

import copy

from .persistence import (
    _anchor_scene_candidate_matches_scene,
    _assistant_anchor_scene_message_ref,
)
from .rows import (
    _anchor_scene_content_rows,
    _anchor_scene_matching_content_tool_row_index,
    _anchor_scene_prose_row,
    _anchor_scene_row_key,
    _anchor_scene_settle_live_running_row,
    _anchor_scene_thinking_row,
    _anchor_scene_tool_row,
    _anchor_scene_tool_rows_have_different_explicit_ids,
)
from .transcript import (
    _anchor_scene_clean_text,
    _anchor_scene_final_answer_text,
    _anchor_scene_is_bounded_tool_body_preview,
    _anchor_scene_message_reasoning_text,
    _anchor_scene_message_text,
    _anchor_scene_message_turn_duration,
    _anchor_scene_row_is_stale_token_answer,
    _anchor_scene_row_looks_like_final_answer,
    _anchor_scene_text_key,
)

def _complete_hydrated_anchor_scene(messages, scene, message_index, *, message_offset=0, tool_calls=None, stream_id=""):
    if not isinstance(messages, list) or not isinstance(scene, dict) or not isinstance(message_index, int):
        return scene
    local_final_idx = message_index - int(message_offset or 0)
    if local_final_idx < 0 or local_final_idx >= len(messages):
        return scene
    final_message = messages[local_final_idx]
    if not isinstance(final_message, dict) or final_message.get("role") != "assistant":
        return scene
    turn_start = -1
    for idx in range(local_final_idx - 1, -1, -1):
        message = messages[idx]
        if isinstance(message, dict) and message.get("role") == "user":
            turn_start = idx
            break
    message_final_answer = _anchor_scene_final_answer_text(final_message)
    scene_final_answer = scene.get("final_answer") if isinstance(scene.get("final_answer"), str) else ""
    final_answer = message_final_answer if _anchor_scene_clean_text(message_final_answer) else scene_final_answer
    final_key = _anchor_scene_text_key(final_answer)
    rows = []
    seen = {}

    def merge_duplicate_tool_row(existing, incoming, *, prefer_incoming_body=False):
        if not isinstance(existing, dict) or not isinstance(incoming, dict):
            return existing
        merged = copy.deepcopy(existing)
        merged_tool = merged.get("tool") if isinstance(merged.get("tool"), dict) else {}
        incoming_tool = incoming.get("tool") if isinstance(incoming.get("tool"), dict) else {}
        merged_payload = merged.get("payload") if isinstance(merged.get("payload"), dict) else {}
        incoming_payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}

        def empty(value):
            return value is None or value == "" or value == {}

        def merge_missing_args(existing_args, incoming_args):
            if not isinstance(incoming_args, dict) or not incoming_args:
                return existing_args, False
            base = copy.deepcopy(existing_args) if isinstance(existing_args, dict) else {}
            changed = not isinstance(existing_args, dict)
            for key, value in incoming_args.items():
                if key not in base:
                    base[key] = copy.deepcopy(value)
                    changed = True
            return base, changed

        for key in ("snippet", "result", "output"):
            incoming_value = incoming_tool.get(key)
            if not empty(incoming_value) and (
                empty(merged_tool.get(key))
                or (
                    prefer_incoming_body
                    and _anchor_scene_is_bounded_tool_body_preview(merged_tool.get(key), incoming_value)
                )
            ):
                merged_tool[key] = copy.deepcopy(incoming_value)
            incoming_value = incoming_payload.get(key)
            if not empty(incoming_value) and (
                empty(merged_payload.get(key))
                or (
                    prefer_incoming_body
                    and _anchor_scene_is_bounded_tool_body_preview(merged_payload.get(key), incoming_value)
                )
            ):
                merged_payload[key] = copy.deepcopy(incoming_value)
        for key in ("preview", "command", "duration", "started_at"):
            incoming_value = incoming_tool.get(key)
            if not empty(incoming_value) and empty(merged_tool.get(key)):
                merged_tool[key] = copy.deepcopy(incoming_value)
            incoming_value = incoming_payload.get(key)
            if not empty(incoming_value) and empty(merged_payload.get(key)):
                merged_payload[key] = copy.deepcopy(incoming_value)
        merged_args, args_changed = merge_missing_args(merged_tool.get("args"), incoming_tool.get("args"))
        if args_changed:
            merged_tool["args"] = merged_args
        merged_payload_args, payload_args_changed = merge_missing_args(
            merged_payload.get("args"),
            incoming_payload.get("args"),
        )
        if payload_args_changed:
            merged_payload["args"] = merged_payload_args
        merged["tool"] = merged_tool
        merged["payload"] = merged_payload
        return merged

    def push(row, *, prefer_incoming_tool_body=False):
        if not isinstance(row, dict):
            return
        row = _anchor_scene_settle_live_running_row(
            row,
            has_settled_thinking=any(existing.get("role") == "thinking" for existing in rows),
        )
        if row is None or not isinstance(row, dict):
            return
        text_key = _anchor_scene_text_key(row.get("text"))
        if row.get("role") in ("prose", "thinking") and _anchor_scene_row_looks_like_final_answer(text_key, final_key):
            return
        if _anchor_scene_row_is_stale_token_answer(row, text_key, final_key):
            return
        key = _anchor_scene_row_key(row)
        if key and key in seen:
            if key.startswith("tool:"):
                index = seen[key]
                rows[index] = merge_duplicate_tool_row(
                    rows[index],
                    row,
                    prefer_incoming_body=prefer_incoming_tool_body,
                )
                return
            if key == "lifecycle:compression":
                index = seen[key]
                next_row = copy.deepcopy(row)
                next_row["order_index"] = index
                next_row["seq"] = index
                rows[index] = next_row
            return
        if key:
            seen[key] = len(rows)
        next_row = copy.deepcopy(row)
        next_row["order_index"] = len(rows)
        next_row["seq"] = len(rows)
        rows.append(next_row)

    order = 0
    content_tool_indexes_by_idx = {}
    used_content_tool_indexes_by_idx = {}
    id_flexible_content_tool_indexes_by_idx = {}
    for local_idx in range(turn_start + 1, local_final_idx + 1):
        message = messages[local_idx]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        absolute_idx = int(message_offset or 0) + local_idx
        text = _anchor_scene_message_text(message)
        content_rows = _anchor_scene_content_rows(
            message,
            order,
            absolute_idx,
            stream_id,
            is_final_message=local_idx == local_final_idx,
        )
        content_tool_indexes = []
        used_content_tool_indexes = set()
        id_flexible_content_tool_indexes = set()
        if content_rows:
            for row in content_rows:
                previous_len = len(rows)
                push(row)
                if row.get("role") == "tool" and len(rows) > previous_len:
                    content_tool_indexes.append(len(rows) - 1)
                order += 1
            if content_tool_indexes:
                content_tool_indexes_by_idx[absolute_idx] = content_tool_indexes
                used_content_tool_indexes_by_idx[absolute_idx] = used_content_tool_indexes
                id_flexible_content_tool_indexes_by_idx[absolute_idx] = id_flexible_content_tool_indexes
        elif _anchor_scene_clean_text(text):
            push(_anchor_scene_prose_row(text, order, absolute_idx, stream_id))
            order += 1
        reasoning = _anchor_scene_message_reasoning_text(message)
        if _anchor_scene_clean_text(reasoning) and _anchor_scene_text_key(reasoning) != _anchor_scene_text_key(text):
            push(_anchor_scene_thinking_row(reasoning, order, absolute_idx, stream_id))
            order += 1
        for key in ("tool_calls", "_partial_tool_calls"):
            calls = message.get(key)
            if isinstance(calls, list):
                for tool_ordinal, call in enumerate(calls):
                    row = _anchor_scene_tool_row(call, order, absolute_idx, stream_id)
                    content_match_index = _anchor_scene_matching_content_tool_row_index(
                        rows,
                        content_tool_indexes,
                        row,
                        tool_ordinal,
                        used_content_tool_indexes,
                        len(calls),
                        id_flexible_content_tool_indexes,
                    )
                    if content_match_index is not None:
                        if _anchor_scene_tool_rows_have_different_explicit_ids(
                            rows[content_match_index],
                            row,
                        ):
                            id_flexible_content_tool_indexes.add(content_match_index)
                        rows[content_match_index] = merge_duplicate_tool_row(rows[content_match_index], row)
                        incoming_key = _anchor_scene_row_key(row)
                        if incoming_key:
                            seen[incoming_key] = content_match_index
                        used_content_tool_indexes.add(content_match_index)
                        order += 1
                        continue
                    push(row)
                    order += 1
    external_tool_counts = {}
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        try:
            absolute_idx = int(call.get("assistant_msg_idx"))
        except (TypeError, ValueError):
            continue
        external_tool_counts[absolute_idx] = external_tool_counts.get(absolute_idx, 0) + 1
    external_tool_ordinals = {}
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        try:
            absolute_idx = int(call.get("assistant_msg_idx"))
        except (TypeError, ValueError):
            continue
        local_idx = absolute_idx - int(message_offset or 0)
        if not (turn_start < local_idx <= local_final_idx):
            continue
        row = _anchor_scene_tool_row(call, order, absolute_idx, stream_id)
        tool_ordinal = external_tool_ordinals.get(absolute_idx, 0)
        external_tool_ordinals[absolute_idx] = tool_ordinal + 1
        content_match_index = _anchor_scene_matching_content_tool_row_index(
            rows,
            content_tool_indexes_by_idx.get(absolute_idx, []),
            row,
            tool_ordinal,
            used_content_tool_indexes_by_idx.setdefault(absolute_idx, set()),
            external_tool_counts.get(absolute_idx, 0),
            id_flexible_content_tool_indexes_by_idx.setdefault(absolute_idx, set()),
        )
        if content_match_index is not None:
            if _anchor_scene_tool_rows_have_different_explicit_ids(rows[content_match_index], row):
                id_flexible_content_tool_indexes_by_idx.setdefault(absolute_idx, set()).add(content_match_index)
            rows[content_match_index] = merge_duplicate_tool_row(
                rows[content_match_index],
                row,
                prefer_incoming_body=True,
            )
            incoming_key = _anchor_scene_row_key(row)
            if incoming_key:
                seen[incoming_key] = content_match_index
            used_content_tool_indexes_by_idx[absolute_idx].add(content_match_index)
            order += 1
            continue
        push(row, prefer_incoming_tool_body=True)
        order += 1
    for row in scene.get("activity_rows") or []:
        if isinstance(row, dict) and row.get("role") != "terminal":
            push(row)
    for row in scene.get("activity_rows") or []:
        if isinstance(row, dict) and row.get("role") == "terminal":
            push(row)
    repaired = copy.deepcopy(scene)
    repaired["version"] = "activity_scene_v1"
    repaired["mode"] = repaired.get("mode") or "compact_worklog"
    repaired["final_answer"] = final_answer if _anchor_scene_clean_text(final_answer) else repaired.get("final_answer", "")
    repaired["final_message_ref"] = _assistant_anchor_scene_message_ref(final_message)
    if repaired.get("turn_duration") is None:
        duration = _anchor_scene_message_turn_duration(final_message)
        if duration is not None:
            repaired["turn_duration"] = duration
    repaired["activity_rows"] = rows
    identity = repaired.get("identity") if isinstance(repaired.get("identity"), dict) else {}
    identity = dict(identity)
    identity["source_message_refs"] = [
        _assistant_anchor_scene_message_ref(message)
        for message in messages[turn_start + 1 : local_final_idx + 1]
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    repaired["identity"] = identity
    return repaired


def _hydrate_anchor_activity_scenes(messages, records, *, message_offset=0, tool_calls=None):
    if not isinstance(messages, list) or not isinstance(records, dict) or not records:
        return messages
    by_ref = {}
    by_index = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        scene = record.get("scene")
        if not isinstance(scene, dict):
            continue
        ref = str(record.get("message_ref") or key or "")
        if ref:
            by_ref[ref] = record
        try:
            idx = int(record.get("message_index"))
        except (TypeError, ValueError):
            idx = None
        if idx is not None:
            by_index[idx] = record
    out = list(messages)
    # Read-side ref-ambiguity guard (parity with the write-side
    # _find_anchor_scene_message, which returns None when a ref matches >1
    # message). If two assistant messages ever share a ref (byte-identical
    # whitespace-normalized content + identical _ts), attaching the same scene
    # to both would render duplicate worklog groups. Count ref occurrences and
    # fall through to the index-based match (which is positional, unambiguous)
    # for any ref that resolves to more than one assistant message.
    _ref_counts: dict[str, int] = {}
    for _m in messages:
        if isinstance(_m, dict) and _m.get("role") == "assistant":
            _r = _assistant_anchor_scene_message_ref(_m)
            if _r:
                _ref_counts[_r] = _ref_counts.get(_r, 0) + 1
    for local_idx, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        absolute_idx = int(message_offset or 0) + local_idx
        _msg_ref = _assistant_anchor_scene_message_ref(message)
        record = by_ref.get(_msg_ref) if _ref_counts.get(_msg_ref, 0) <= 1 else None
        if not record:
            candidate = by_index.get(absolute_idx)
            if candidate and _anchor_scene_candidate_matches_scene(message, candidate.get("scene") or {}):
                record = candidate
        if not record:
            continue
        scene = record.get("scene")
        if not isinstance(scene, dict):
            continue
        next_message = dict(message)
        stream_id = record.get("stream_id")
        next_message["_anchor_activity_scene"] = _complete_hydrated_anchor_scene(
            messages,
            scene,
            absolute_idx,
            message_offset=message_offset,
            tool_calls=tool_calls,
            stream_id=str(stream_id or ""),
        )
        if stream_id:
            next_message["_anchor_stream_id"] = str(stream_id)
        out[local_idx] = next_message
    return out
