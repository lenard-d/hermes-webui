"""Build and reconcile renderer-neutral rows for persisted Anchor scenes."""

from __future__ import annotations

import copy
import json

from .transcript import (
    _anchor_scene_clean_text,
    _anchor_scene_content_text,
    _anchor_scene_content_tool,
    _anchor_scene_content_visible_text,
    _anchor_scene_message_has_content_tool_use,
    _anchor_scene_text_key,
    _anchor_scene_tool_args,
    _anchor_scene_tool_id,
    _anchor_scene_tool_name,
)

def _anchor_scene_row_base(role, kind, source_event_type, order_index, message_index, stream_id=""):
    return {
        "row_id": f"hydrated:{stream_id or 'stream'}:{role}:{message_index}:{order_index}",
        "order_index": order_index,
        "kind": kind,
        "role": role,
        "display_hint": {
            "prose": "main_prose",
            "thinking": "collapsed_thinking",
            "tool": "tool_row",
            "terminal": "terminal_status_row",
        }.get(role, "activity_row"),
        "display_hints": {
            "compact_worklog": {
                "prose": "main_prose",
                "thinking": "collapsed_thinking",
                "tool": "tool_row",
                "terminal": "terminal_status_row",
            }.get(role, "activity_row"),
            "transparent_stream": "chronological_activity",
        },
        "source_event_type": source_event_type,
        "event_id": None,
        "local_id": None,
        "run_id": None,
        "stream_id": stream_id or None,
        "seq": order_index,
        "status": "completed",
        "created_at": None,
        "identity": {"event_id": None, "local_id": None, "run_id": None, "stream_id": stream_id or None, "seq": order_index},
        "group": {
            "group_key": f"assistant:{message_index}" if isinstance(message_index, int) else f"activity:{order_index}",
            "activity_burst_id": None,
            "activity_segment_seq": None,
            "assistant_msg_idx": message_index if isinstance(message_index, int) else None,
        },
        "text": "",
        "thinking": None,
        "tool_call_id": None,
        "tool": None,
        "payload": {"assistant_msg_idx": message_index if isinstance(message_index, int) else None},
    }


def _anchor_scene_prose_row(text, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("prose", "process_prose", "settled_message", order_index, message_index, stream_id)
    row["text"] = str(text or "")
    row["payload"]["text"] = row["text"]
    return row


def _anchor_scene_thinking_row(text, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("thinking", "reasoning", "reasoning", order_index, message_index, stream_id)
    row["text"] = str(text or "")
    preview = _anchor_scene_clean_text(text)
    row["thinking"] = {
        "text": row["text"],
        "preview": (preview[:177] + "...") if len(preview) > 180 else preview,
        "dedupe_key": f"thinking:{preview.lower()}" if preview else "",
    }
    row["payload"]["text"] = row["text"]
    return row


def _anchor_scene_tool_row(tool, order_index, message_index, stream_id=""):
    row = _anchor_scene_row_base("tool", "tool_completed", "tool_complete", order_index, message_index, stream_id)
    tid = _anchor_scene_tool_id(tool)
    name = _anchor_scene_tool_name(tool)
    args = _anchor_scene_tool_args(tool)
    preview = str((tool or {}).get("preview") or (tool or {}).get("summary") or "")
    snippet = str((tool or {}).get("snippet") or (tool or {}).get("result") or (tool or {}).get("output") or "")
    row["row_id"] = f"hydrated:{stream_id or 'stream'}:tool:{tid}" if tid else row["row_id"]
    row["tool_call_id"] = tid or None
    row["tool"] = {
        "id": tid or None,
        "name": name,
        "args": args,
        "preview": preview,
        "snippet": snippet,
        "result": copy.deepcopy((tool or {}).get("result")) if isinstance(tool, dict) else None,
        "output": copy.deepcopy((tool or {}).get("output")) if isinstance(tool, dict) else None,
        "done": True,
        "is_error": bool((tool or {}).get("is_error") or (tool or {}).get("error")),
        "duration": (tool or {}).get("duration") if isinstance(tool, dict) else None,
        "started_at": (tool or {}).get("started_at") if isinstance(tool, dict) else None,
        "signature": f"{name}|{tid}|{json.dumps(args, sort_keys=True, default=str)}",
    }
    row["payload"].update({"tid": tid, "id": tid, "name": name, "args": args, "preview": preview, "snippet": snippet})
    return row


def _anchor_scene_tool_row_id(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(
        row.get("tool_call_id")
        or tool.get("id")
        or tool.get("tid")
        or tool.get("tool_call_id")
        or tool.get("tool_use_id")
        or tool.get("call_id")
        or payload.get("tid")
        or payload.get("id")
        or ""
    ).strip()


def _anchor_scene_tool_row_name(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(tool.get("name") or payload.get("name") or "tool").strip().lower()


def _anchor_scene_tool_rows_have_compatible_names(existing, incoming) -> bool:
    existing_name = _anchor_scene_tool_row_name(existing)
    incoming_name = _anchor_scene_tool_row_name(incoming)
    return (
        not existing_name
        or not incoming_name
        or existing_name == "tool"
        or incoming_name == "tool"
        or existing_name == incoming_name
    )


def _anchor_scene_tool_row_args(row):
    if not isinstance(row, dict):
        return None
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    args = tool.get("args") if isinstance(tool.get("args"), dict) else payload.get("args")
    return args if isinstance(args, dict) and args else None


def _anchor_scene_object_contains_subset(base, subset) -> bool:
    if not isinstance(base, dict) or not isinstance(subset, dict):
        return False
    for key, value in subset.items():
        if key not in base:
            return False
        if json.dumps(base[key], sort_keys=True, separators=(",", ":")) != json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ):
            return False
    return True


def _anchor_scene_tool_rows_have_compatible_invocation(existing, incoming) -> bool:
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return False
    existing_tool = existing.get("tool") if isinstance(existing.get("tool"), dict) else {}
    incoming_tool = incoming.get("tool") if isinstance(incoming.get("tool"), dict) else {}
    existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
    incoming_payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}
    existing_command = str(existing_tool.get("command") or existing_payload.get("command") or "").strip()
    incoming_command = str(incoming_tool.get("command") or incoming_payload.get("command") or "").strip()
    if existing_command and incoming_command:
        return existing_command == incoming_command
    existing_args = _anchor_scene_tool_row_args(existing)
    incoming_args = _anchor_scene_tool_row_args(incoming)
    if not existing_args or not incoming_args:
        return False
    return _anchor_scene_object_contains_subset(
        existing_args,
        incoming_args,
    ) or _anchor_scene_object_contains_subset(incoming_args, existing_args)


def _anchor_scene_tool_row_has_invocation_evidence(row) -> bool:
    if not isinstance(row, dict):
        return False
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    command = str(tool.get("command") or payload.get("command") or "").strip()
    args = _anchor_scene_tool_row_args(row)
    return bool(command or args)


def _anchor_scene_tool_rows_can_name_match(existing, incoming) -> bool:
    if not _anchor_scene_tool_rows_have_compatible_names(existing, incoming):
        return False
    if _anchor_scene_tool_row_has_invocation_evidence(existing) and _anchor_scene_tool_row_has_invocation_evidence(incoming):
        return _anchor_scene_tool_rows_have_compatible_invocation(existing, incoming)
    return True


def _anchor_scene_tool_rows_have_different_explicit_ids(existing, incoming) -> bool:
    existing_id = _anchor_scene_tool_row_id(existing)
    incoming_id = _anchor_scene_tool_row_id(incoming)
    return bool(existing_id and incoming_id and existing_id != incoming_id)


def _anchor_scene_tool_row_started_at(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    value = tool.get("started_at")
    if value is None or value == "":
        value = payload.get("started_at")
    return str(value) if value is not None and value != "" else ""


def _anchor_scene_tool_rows_have_same_started_at(existing, incoming) -> bool:
    existing_started_at = _anchor_scene_tool_row_started_at(existing)
    incoming_started_at = _anchor_scene_tool_row_started_at(incoming)
    return bool(existing_started_at and incoming_started_at and existing_started_at == incoming_started_at)


def _anchor_scene_tool_row_body_text(row) -> str:
    if not isinstance(row, dict):
        return ""
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for value in (
        tool.get("snippet"),
        payload.get("snippet"),
        tool.get("output"),
        payload.get("output"),
        tool.get("result"),
        payload.get("result"),
        tool.get("preview"),
        payload.get("preview"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _anchor_scene_tool_rows_have_compatible_body(existing, incoming) -> bool:
    existing_body = _anchor_scene_tool_row_body_text(existing)
    incoming_body = _anchor_scene_tool_row_body_text(incoming)
    return bool(
        existing_body
        and incoming_body
        and (
            existing_body == incoming_body
            or existing_body.startswith(incoming_body)
            or incoming_body.startswith(existing_body)
        )
    )


def _anchor_scene_matching_content_tool_row_index(
    rows,
    content_tool_indexes,
    incoming_row,
    ordinal,
    used_indexes,
    incoming_total=0,
    id_flexible_indexes=None,
):
    if not isinstance(rows, list) or not isinstance(content_tool_indexes, list) or not isinstance(incoming_row, dict):
        return None
    incoming_id = _anchor_scene_tool_row_id(incoming_row)
    for index in content_tool_indexes:
        if index in used_indexes or index < 0 or index >= len(rows):
            continue
        existing_id = _anchor_scene_tool_row_id(rows[index])
        if existing_id and incoming_id and existing_id == incoming_id:
            return index
    if len(content_tool_indexes) == 1 and incoming_total == 1:
        index = content_tool_indexes[0]
        if (
            index not in used_indexes
            and 0 <= index < len(rows)
            and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row)
        ):
            return index
    available_indexes = [
        index for index in content_tool_indexes if index not in used_indexes and 0 <= index < len(rows)
    ]
    if len(available_indexes) == 1:
        index = available_indexes[0]
        if incoming_total == 1 and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row):
            return index
        if _anchor_scene_tool_rows_have_compatible_names(
            rows[index],
            incoming_row,
        ) and _anchor_scene_tool_rows_have_compatible_invocation(rows[index], incoming_row):
            return index
    reusable_indexes = [
        index for index in content_tool_indexes if index in used_indexes and 0 <= index < len(rows)
    ]
    if len(reusable_indexes) == 1 and incoming_total == 1:
        index = reusable_indexes[0]
        existing_id = _anchor_scene_tool_row_id(rows[index])
        id_flexible = isinstance(id_flexible_indexes, set) and index in id_flexible_indexes
        if _anchor_scene_tool_rows_have_compatible_names(
            rows[index],
            incoming_row,
        ) and (
            (existing_id and incoming_id and existing_id == incoming_id)
            or (
                id_flexible
                and _anchor_scene_tool_rows_have_same_started_at(rows[index], incoming_row)
                and _anchor_scene_tool_rows_have_compatible_body(rows[index], incoming_row)
            )
        ) and _anchor_scene_tool_rows_have_compatible_invocation(rows[index], incoming_row):
            return index
    for index in content_tool_indexes:
        if index in used_indexes or index < 0 or index >= len(rows):
            continue
        existing_id = _anchor_scene_tool_row_id(rows[index])
        if not existing_id and not incoming_id and _anchor_scene_tool_rows_can_name_match(rows[index], incoming_row):
            return index
    return None


def _anchor_scene_content_rows(message, order_index, message_index, stream_id="", *, is_final_message=False):
    if not _anchor_scene_message_has_content_tool_use(message):
        return None
    rows = []
    content = message.get("content") if isinstance(message, dict) else []
    last_tool_index = -1
    for idx, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == "tool_use":
            last_tool_index = idx
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            if is_final_message and idx > last_tool_index:
                continue
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_prose_row(text, order_index + len(rows), message_index, stream_id))
            continue
        part_type = part.get("type")
        if part_type in ("text", "input_text", "output_text"):
            if is_final_message and idx > last_tool_index and _anchor_scene_content_visible_text(part):
                continue
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_prose_row(text, order_index + len(rows), message_index, stream_id))
            continue
        if part_type in ("thinking", "reasoning"):
            text = _anchor_scene_content_text(part)
            if _anchor_scene_clean_text(text):
                rows.append(_anchor_scene_thinking_row(text, order_index + len(rows), message_index, stream_id))
            continue
        if part_type == "tool_use":
            rows.append(
                _anchor_scene_tool_row(
                    _anchor_scene_content_tool(part),
                    order_index + len(rows),
                    message_index,
                    stream_id,
                )
            )
    return rows


def _anchor_scene_row_key(row) -> str:
    if not isinstance(row, dict):
        return ""
    if row.get("role") == "tool":
        tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
        return "tool:" + str(
            row.get("tool_call_id")
            or tool.get("id")
            or tool.get("tid")
            or tool.get("tool_call_id")
            or tool.get("tool_use_id")
            or tool.get("call_id")
            or row.get("row_id")
            or ""
        )
    if row.get("role") in ("prose", "thinking"):
        return f"{row.get('role')}:{_anchor_scene_text_key(row.get('text'))}"
    if row.get("role") == "lifecycle":
        source_type = str(row.get("source_event_type") or row.get("source") or "")
        if source_type in ("compressing", "compressed"):
            return "lifecycle:compression"
    return f"{row.get('role') or row.get('kind')}:{row.get('source_event_type') or ''}:{row.get('status') or ''}:{row.get('row_id') or ''}"


def _anchor_scene_row_has_live_identity(row) -> bool:
    if not isinstance(row, dict):
        return False
    values = [row.get("row_id"), row.get("local_id"), row.get("event_id")]
    identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
    values.extend([identity.get("local_id"), identity.get("event_id")])
    return any(str(value or "").startswith("live-") for value in values)


def _anchor_scene_settle_live_running_row(row, *, has_settled_thinking: bool):
    if not isinstance(row, dict):
        return row
    role = row.get("role")
    if role not in ("thinking", "prose", "tool"):
        return row
    if str(row.get("status") or "").lower() != "running":
        return row
    if not _anchor_scene_row_has_live_identity(row):
        return row
    if role == "thinking" and has_settled_thinking:
        return None
    next_row = copy.deepcopy(row)
    next_row["status"] = "completed"
    return next_row
