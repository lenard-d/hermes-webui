"""Project replayable run-journal events into a live Anchor scene snapshot."""

from __future__ import annotations

import copy

from api.runs import (
    bound_run_journal_snapshot_args,
    find_run_summary,
    read_run_events,
)
from api.streaming import _compact_for_echo_compare, _strip_compact_echo_suffix

def _run_journal_status_payload(summary: dict, *, active: bool = False) -> dict:
    terminal = bool(summary.get("terminal"))
    terminal_state = summary.get("terminal_state")
    if not active and not terminal:
        terminal_state = "lost-worker-bookkeeping"
    return {
        "session_id": summary.get("session_id"),
        "run_id": summary.get("run_id"),
        "last_seq": summary.get("last_seq"),
        "last_event_id": summary.get("last_event_id"),
        "last_event": summary.get("last_event"),
        "terminal": terminal,
        "terminal_state": terminal_state,
    }


_RUN_JOURNAL_TOOL_ID_KEYS = ("tid", "id", "tool_call_id", "tool_use_id", "call_id")


def _run_journal_snapshot_tool_id(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _RUN_JOURNAL_TOOL_ID_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _truncate_journal_snapshot_value(value, *, limit: int = 120):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, dict):
        return {str(k): _truncate_journal_snapshot_value(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_journal_snapshot_value(v, limit=limit) for v in value[:20]]
    return value


def _run_journal_snapshot_recovery_args(payload: dict | None):
    if not isinstance(payload, dict):
        return {}
    args = payload.get("args")
    return bound_run_journal_snapshot_args(args)


def _run_journal_snapshot_arg_detail_score(value) -> int:
    if value in (None, "", [], {}):
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(
            len(str(key)) + _run_journal_snapshot_arg_detail_score(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_run_journal_snapshot_arg_detail_score(item) for item in value)
    return 1


def _run_journal_snapshot_merge_args(existing, incoming):
    if not incoming:
        return existing, False
    if not isinstance(existing, dict) or not existing:
        return incoming, True
    if not isinstance(incoming, dict):
        return existing, False
    merged = copy.deepcopy(existing)
    changed = False
    for key, value in incoming.items():
        current = merged.get(key)
        if key not in merged or (
            _run_journal_snapshot_arg_detail_score(value)
            > _run_journal_snapshot_arg_detail_score(current)
        ):
            merged[key] = value
            changed = True
    return merged, changed


def _run_journal_live_snapshot(
    stream_id: str | None,
    *,
    session_dir=None,
) -> dict | None:
    stream_id = str(stream_id or "").strip()
    if not stream_id:
        return None
    summary = (
        find_run_summary(stream_id)
        if session_dir is None
        else find_run_summary(stream_id, session_dir=session_dir)
    )
    if not summary:
        return None
    session_id = str(summary.get("session_id") or "")
    if not session_id:
        return None
    journal = (
        read_run_events(session_id, stream_id)
        if session_dir is None
        else read_run_events(session_id, stream_id, session_dir=session_dir)
    )
    events = [event for event in (journal.get("events") or []) if isinstance(event, dict)]
    if not events:
        return None

    assistant_text = ""
    reasoning_text = ""
    messages: list[dict] = []
    tool_calls: list[dict] = []
    activity_burst_anchors: list[dict] = []
    current_activity_burst_id = 0
    fresh_segment = True
    last_ts = None
    reasoning_first_tool_count: int | None = None

    def mark_boundary() -> int:
        nonlocal current_activity_burst_id
        text_end = len(assistant_text)
        if text_end <= 0:
            return current_activity_burst_id
        last_end = max(
            [int(anchor.get("textEnd") or 0) for anchor in activity_burst_anchors]
            or [0]
        )
        if text_end > last_end:
            current_activity_burst_id += 1
            activity_burst_anchors.append(
                {"id": current_activity_burst_id, "textEnd": text_end}
            )
        return current_activity_burst_id

    def update_completed_tool(payload: dict) -> None:
        tool_id = _run_journal_snapshot_tool_id(payload)
        name = str(payload.get("name") or "").strip()
        for call in reversed(tool_calls):
            if call.get("done"):
                continue
            call_id = _run_journal_snapshot_tool_id(call)
            if (tool_id and call_id == tool_id) or (not tool_id and name and call.get("name") == name):
                call["done"] = True
                merged_args, args_changed = _run_journal_snapshot_merge_args(
                    call.get("args"),
                    _run_journal_snapshot_recovery_args(payload),
                )
                if args_changed:
                    call["args"] = merged_args
                if payload.get("preview") is not None:
                    call["snippet"] = str(payload.get("preview") or "")
                    call["preview"] = call.get("preview") or call["snippet"]
                if payload.get("duration") is not None:
                    call["duration"] = payload.get("duration")
                if payload.get("is_error") is not None:
                    call["is_error"] = bool(payload.get("is_error"))
                return

        if not name or name == "clarify":
            return
        call = {
            "name": name,
            "preview": str(payload.get("preview") or ""),
            "snippet": str(payload.get("preview") or ""),
            "args": _run_journal_snapshot_recovery_args(payload),
            "done": True,
            "_live": True,
            "_journal_snapshot": True,
            "_journal_stream_id": stream_id,
        }
        tool_id = _run_journal_snapshot_tool_id(payload)
        if tool_id:
            call["tid"] = tool_id
        for key in _RUN_JOURNAL_TOOL_ID_KEYS:
            if payload.get(key):
                call[key] = str(payload.get(key))
        if current_activity_burst_id:
            call["activityBurstId"] = current_activity_burst_id
            call["activitySegmentSeq"] = current_activity_burst_id
        tool_calls.append(call)

    def reasoning_echo_tail_matches(text: str) -> bool:
        candidate = _compact_for_echo_compare(text)
        if not candidate:
            return False
        return _compact_for_echo_compare(reasoning_text).endswith(candidate)

    def strip_reasoning_echo_tail(text: str) -> bool:
        nonlocal reasoning_text, reasoning_first_tool_count
        next_reasoning, did_remove = _strip_compact_echo_suffix(reasoning_text, text)
        if did_remove:
            reasoning_text = next_reasoning
            if not _compact_for_echo_compare(reasoning_text):
                reasoning_first_tool_count = None
        return did_remove

    for event in events:
        event_name = str(event.get("event") or event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        last_ts = event.get("created_at", last_ts)
        if event_name == "token":
            text = str(payload.get("text") or "")
            if text:
                assistant_text += text
                fresh_segment = False
            continue
        if event_name == "reasoning":
            text = str(payload.get("text") or "")
            if text and reasoning_first_tool_count is None:
                reasoning_first_tool_count = len(tool_calls)
            reasoning_text += text
            continue
        if event_name == "interim_assistant":
            visible = str(payload.get("text") or "").strip()
            if visible:
                if payload.get("reasoning_echo") or reasoning_echo_tail_matches(visible):
                    strip_reasoning_echo_tail(visible)
                if payload.get("already_streamed"):
                    if not assistant_text:
                        assistant_text = visible
                else:
                    assistant_text = f"{assistant_text}\n\n{visible}" if assistant_text else visible
                mark_boundary()
                fresh_segment = True
            continue
        if event_name == "tool":
            name = str(payload.get("name") or "").strip()
            if not name or name == "clarify":
                continue
            boundary_id = mark_boundary()
            tool_id = _run_journal_snapshot_tool_id(payload)
            call = {
                "name": name,
                "preview": str(payload.get("preview") or ""),
                "args": _run_journal_snapshot_recovery_args(payload),
                "done": False,
                "_live": True,
                "_journal_snapshot": True,
                "_journal_stream_id": stream_id,
            }
            if tool_id:
                call["tid"] = tool_id
            for key in _RUN_JOURNAL_TOOL_ID_KEYS:
                if payload.get(key):
                    call[key] = str(payload.get(key))
            if boundary_id:
                call["activityBurstId"] = boundary_id
                call["activitySegmentSeq"] = boundary_id
            tool_calls.append(call)
            fresh_segment = True
            continue
        if event_name == "tool_complete":
            update_completed_tool(payload)
            fresh_segment = True

    if assistant_text or reasoning_text:
        message = {
            "role": "assistant",
            "content": assistant_text,
            "_live": True,
            "_journal_snapshot": True,
            "_journal_stream_id": stream_id,
        }
        if reasoning_text:
            message["reasoning"] = reasoning_text
        if last_ts is not None:
            message["_ts"] = last_ts
        messages.append(message)

    def scene_group(segment_seq: int | None = None, burst_id: int | None = None) -> dict:
        group: dict = {}
        if segment_seq:
            group["group_key"] = f"segment:{segment_seq}"
            group["activity_segment_seq"] = segment_seq
        elif burst_id:
            group["group_key"] = f"burst:{burst_id}"
            group["activity_burst_id"] = burst_id
        else:
            group["group_key"] = "activity:0"
        if burst_id:
            group["activity_burst_id"] = burst_id
        return group

    def scene_prose_row(text: str, *, burst_id: int | None, segment_seq: int, status: str) -> dict | None:
        clean = str(text or "").strip()
        if not clean:
            return None
        local_id = f"live-prose:{stream_id}:{segment_seq}"
        return {
            "row_id": local_id,
            "order_index": len(anchor_activity_rows),
            "kind": "process_prose",
            "role": "prose",
            "display_hint": "main_prose",
            "display_hints": {
                "compact_worklog": "main_prose",
                "transparent_stream": "chronological_activity",
            },
            "source_event_type": "token",
            "event_id": None,
            "local_id": local_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "seq": None,
            "status": status,
            "created_at": last_ts,
            "identity": {
                "event_id": None,
                "local_id": local_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "seq": None,
            },
            "group": scene_group(segment_seq, burst_id),
            "text": clean,
            "thinking": None,
            "tool_call_id": "",
            "tool": None,
            "payload": {
                "text": clean,
                "activitySegmentSeq": segment_seq,
                "activityBurstId": burst_id or 0,
            },
        }

    def scene_thinking_row(text: str, *, status: str) -> dict | None:
        clean = str(text or "").strip()
        if not clean:
            return None
        preview = " ".join(clean.split())
        local_id = f"live-thinking:{stream_id}:1"
        return {
            "row_id": local_id,
            "order_index": len(anchor_activity_rows),
            "kind": "reasoning",
            "role": "thinking",
            "display_hint": "collapsed_thinking",
            "display_hints": {
                "compact_worklog": "collapsed_thinking",
                "transparent_stream": "chronological_activity",
            },
            "source_event_type": "reasoning",
            "event_id": None,
            "local_id": local_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "seq": None,
            "status": status,
            "created_at": last_ts,
            "identity": {
                "event_id": None,
                "local_id": local_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "seq": None,
            },
            "group": scene_group(),
            "text": clean,
            "thinking": {
                "text": clean,
                "preview": (preview[:177] + "...") if len(preview) > 180 else preview,
                "dedupe_key": f"thinking:{preview.lower()}" if preview else "",
            },
            "tool_call_id": "",
            "tool": None,
            "payload": {
                "text": clean,
            },
        }

    def scene_tool_row(call: dict, *, fallback_order: int) -> dict | None:
        if not isinstance(call, dict):
            return None
        name = str(call.get("name") or "").strip()
        if not name:
            return None
        tool_id = _run_journal_snapshot_tool_id(call)
        burst_id = int(call.get("activityBurstId") or 0) or None
        segment_seq = int(call.get("activitySegmentSeq") or burst_id or 0) or None
        status = "error" if call.get("is_error") else ("completed" if call.get("done") else "running")
        row_id = f"tool:{tool_id or name}:{fallback_order}"
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        preview = str(call.get("preview") or "")
        snippet = str(call.get("snippet") or "")
        tool = {
            "id": tool_id,
            "tid": tool_id,
            "name": name,
            "args": args,
            "preview": preview,
            "snippet": snippet,
            "done": bool(call.get("done")),
            "is_error": bool(call.get("is_error")),
            "duration": call.get("duration"),
            "started_at": call.get("started_at"),
        }
        payload = {
            "name": name,
            "args": args,
            "preview": preview,
            "snippet": snippet,
            "tid": tool_id,
            "id": tool_id,
            "is_error": bool(call.get("is_error")),
            "duration": call.get("duration"),
            "activitySegmentSeq": segment_seq,
            "activityBurstId": burst_id or 0,
        }
        return {
            "row_id": row_id,
            "order_index": len(anchor_activity_rows),
            "kind": "tool_completed" if call.get("done") else "tool_started",
            "role": "tool",
            "display_hint": "tool_row",
            "display_hints": {
                "compact_worklog": "tool_row",
                "transparent_stream": "chronological_activity",
            },
            "source_event_type": "tool_complete" if call.get("done") else "tool",
            "event_id": None,
            "local_id": tool_id or row_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "seq": None,
            "status": status,
            "created_at": last_ts,
            "identity": {
                "event_id": None,
                "local_id": tool_id or row_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "seq": None,
            },
            "group": scene_group(segment_seq, burst_id),
            "text": snippet or preview,
            "thinking": None,
            "tool_call_id": tool_id,
            "tool": tool,
            "payload": payload,
        }

    anchor_activity_rows: list[dict] = []
    thinking_row_inserted = False
    tool_rows_rendered = 0

    def append_thinking_row(*, force: bool = False) -> None:
        nonlocal thinking_row_inserted
        if thinking_row_inserted:
            return
        if not force and reasoning_first_tool_count and tool_rows_rendered < reasoning_first_tool_count:
            return
        row = scene_thinking_row(reasoning_text, status="running")
        if not row:
            return
        row["order_index"] = len(anchor_activity_rows)
        anchor_activity_rows.append(row)
        thinking_row_inserted = True

    tool_rows_by_burst: dict[int, list[tuple[int, dict]]] = {}
    ungrouped_tool_rows: list[tuple[int, dict]] = []
    for order, call in enumerate(tool_calls):
        burst_id = int(call.get("activityBurstId") or 0) if isinstance(call, dict) else 0
        row = scene_tool_row(call, fallback_order=order)
        if not row:
            continue
        if burst_id:
            tool_rows_by_burst.setdefault(burst_id, []).append((order, row))
        else:
            ungrouped_tool_rows.append((order, row))

    consumed_tools: set[int] = set()
    text_start = 0
    sorted_anchors = sorted(
        [
            anchor
            for anchor in activity_burst_anchors
            if int(anchor.get("textEnd") or 0) > 0
        ],
        key=lambda anchor: int(anchor.get("textEnd") or 0),
    )
    for anchor in sorted_anchors:
        burst_id = int(anchor.get("id") or 0) or None
        text_end = min(len(assistant_text), int(anchor.get("textEnd") or 0))
        segment_seq = burst_id or (len(anchor_activity_rows) + 1)
        prose = scene_prose_row(
            assistant_text[text_start:text_end],
            burst_id=burst_id,
            segment_seq=segment_seq,
            status="completed",
        )
        if prose:
            anchor_activity_rows.append(prose)
            append_thinking_row()
        for order, row in tool_rows_by_burst.get(burst_id or 0, []):
            row["order_index"] = len(anchor_activity_rows)
            anchor_activity_rows.append(row)
            consumed_tools.add(order)
            tool_rows_rendered += 1
            append_thinking_row()
        text_start = max(text_start, text_end)

    if text_start < len(assistant_text):
        segment_seq = max(len(sorted_anchors) + 1, 1)
        tail = scene_prose_row(
            assistant_text[text_start:],
            burst_id=None,
            segment_seq=segment_seq,
            status="running",
        )
        if tail:
            anchor_activity_rows.append(tail)
            append_thinking_row()

    if not assistant_text:
        append_thinking_row()

    for order, row in sorted(ungrouped_tool_rows, key=lambda item: item[0]):
        if order in consumed_tools:
            continue
        row["order_index"] = len(anchor_activity_rows)
        anchor_activity_rows.append(row)
        tool_rows_rendered += 1
        append_thinking_row()

    append_thinking_row(force=True)

    # Keep a live anchor shell during session-switch replay even before the
    # journal has projected visible prose or tool rows from the first events.
    if not anchor_activity_rows and events:
        anchor_activity_rows.append(
            {
                "row_id": f"lifecycle:{stream_id}:running",
                "order_index": 0,
                "kind": "lifecycle_status",
                "role": "lifecycle",
                "display_hint": "quiet_lifecycle_row",
                "display_hints": {
                    "compact_worklog": "quiet_lifecycle_row",
                    "transparent_stream": "chronological_activity",
                },
                "source_event_type": "runtime_journal_snapshot",
                "event_id": None,
                "local_id": f"lifecycle:{stream_id}:running",
                "run_id": stream_id,
                "stream_id": stream_id,
                "seq": None,
                "status": "running",
                "created_at": last_ts,
                "identity": {
                    "event_id": None,
                    "local_id": f"lifecycle:{stream_id}:running",
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "seq": None,
                },
                "group": scene_group(),
                "text": "Working",
                "thinking": None,
                "tool_call_id": "",
                "tool": None,
                "payload": {},
            }
        )

    visible_anchors = [
        anchor
        for anchor in activity_burst_anchors
        if int(anchor.get("textEnd") or 0) < len(assistant_text)
    ]
    segment_count = len(visible_anchors) + (1 if assistant_text else 0)
    current_live_segment_seq = max(segment_count, len(activity_burst_anchors), 0)
    try:
        summary_last_seq = max(0, int(summary.get("last_seq") or 0))
    except (TypeError, ValueError):
        summary_last_seq = 0
    try:
        event_last_seq = max(0, int(events[-1].get("seq") or 0))
    except (TypeError, ValueError):
        event_last_seq = 0
    if event_last_seq >= summary_last_seq:
        last_seq = event_last_seq
        last_event_id = events[-1].get("event_id") or (
            f"{stream_id}:{event_last_seq}" if event_last_seq else summary.get("last_event_id")
        )
    else:
        last_seq = summary_last_seq
        last_event_id = summary.get("last_event_id") or events[-1].get("event_id")

    # Keep returning a live snapshot even when the journal has events but no
    # projected message/tool rows yet. The frontend treats the empty activity
    # scene as "nothing renderable yet" while preserving the live cursor.
    return {
        "session_id": session_id,
        "stream_id": stream_id,
        "last_seq": last_seq,
        "last_event_id": last_event_id,
        "event_count": len(events),
        "fresh_segment": fresh_segment,
        "messages": messages,
        "tool_calls": tool_calls,
        "last_assistant_text": assistant_text,
        "last_reasoning_text": reasoning_text,
        "activity_burst_anchors": activity_burst_anchors,
        "current_activity_burst_id": current_activity_burst_id,
        "current_live_segment_seq": current_live_segment_seq,
        "anchor_activity_scene": {
            "version": "activity_scene_v1",
            "mode": "compact_worklog",
            "identity": {
                "session_id": session_id,
                "stream_id": stream_id,
                "run_id": stream_id,
                "source_message_refs": [],
            },
            "lifecycle": {
                "status": "running",
                "terminal_state": None,
            },
            "final_answer": "",
            "final_message_ref": None,
            "terminal_state": None,
            "activity_rows": anchor_activity_rows,
        },
    }
