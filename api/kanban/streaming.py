"""Server-Sent Events transport for Kanban task events."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs

from api.helpers import bad
from api.sse_chunked import end_sse_headers

from .integration import _kb
from .validation import _resolve_board

_KANBAN_SSE_POLL_SECONDS = 0.3
_KANBAN_SSE_HEARTBEAT_SECONDS = 15.0
_KANBAN_SSE_BATCH_LIMIT = 200


def _kanban_sse_fetch_new(board, cursor):
    """Read events with id > cursor from the given board's task_events
    table. Returns ``(new_cursor, events_list)``. Best-effort — returns
    the input cursor and an empty list on any DB error so the SSE loop
    self-heals on transient sqlite contention rather than dropping the
    client."""
    kb = _kb()
    # Guard against a board that's been archived/removed mid-stream:
    # kb.connect(board=<slug>) auto-materialises the directory + DB on
    # first call, which would silently un-archive a board that was just
    # removed. Skip the fetch when the board no longer exists.
    if board is not None:
        try:
            default_slug = getattr(kb, "DEFAULT_BOARD", "default")
        except Exception:
            default_slug = "default"
        if board != default_slug and not kb.board_exists(board):
            return cursor, []
    try:
        conn = kb.connect(board=board)
    except Exception:
        return cursor, []
    try:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(cursor), _KANBAN_SSE_BATCH_LIMIT),
        ).fetchall()
    except Exception:
        return cursor, []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    new_cursor = cursor
    for r in rows:
        payload = None
        try:
            raw = r["payload"]
            if raw:
                payload = json.loads(raw)
        except Exception:
            payload = None
        out.append(
            {
                "id": int(r["id"]),
                "task_id": r["task_id"],
                "run_id": r["run_id"],
                "kind": r["kind"],
                "payload": payload,
                "created_at": int(r["created_at"])
                if r["created_at"] is not None
                else None,
            }
        )
        new_cursor = int(r["id"])
    return new_cursor, out


def _handle_events_sse_stream(handler, parsed):
    """GET /api/kanban/events/stream — long-lived SSE feed of task events.

    Query params:
      since=<int>   Resume from this event id. Defaults to 0 (full backlog
                    on first connect — the client should pass the latest
                    id it knows about so it does not re-receive historical
                    events.) Capped to the most recent _KANBAN_SSE_BATCH_LIMIT.
      board=<slug>  Pin the stream to a specific board. Switching boards
                    requires the client to close and re-open the stream.

    Header (set automatically by EventSource on reconnect):
      Last-Event-ID  Fallback resume cursor when ?since= is absent. The
                     server emits ``id: <event_id>`` on every events frame
                     so the browser can resume cleanly across drops without
                     re-receiving up to _KANBAN_SSE_BATCH_LIMIT events the
                     client already has.

    Mirrors the agent dashboard's WebSocket /events contract event-for-event
    so a client that handles one can handle the other with only the
    transport swapped.
    """
    try:
        board = _resolve_board(parsed)
    except (ValueError, LookupError) as exc:
        return bad(
            handler, str(exc), status=400 if isinstance(exc, ValueError) else 404
        )

    qs = parse_qs(parsed.query or "")
    # Resolution chain: ?since= query param → Last-Event-ID header → 0.
    # The Last-Event-ID header is what EventSource sends automatically on
    # reconnect; honouring it lets the browser resume cleanly without the
    # client needing to track the cursor in JS.
    since_raw = (qs.get("since") or [None])[0]
    if since_raw is None:
        try:
            since_raw = handler.headers.get("Last-Event-ID")
        except Exception:
            since_raw = None
    try:
        cursor = int(since_raw) if since_raw is not None else 0
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0:
        cursor = 0

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)

    # Send an initial frame so the client knows the connection is open
    # and learns the current cursor (in case the server already had a
    # backlog when the client first connected).
    try:
        handler.wfile.write(
            f"event: hello\ndata: {json.dumps({'cursor': cursor, 'board': board})}\n\n".encode(
                "utf-8"
            )
        )
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
        return True

    last_heartbeat = time.monotonic()
    try:
        while True:
            cursor, events = _kanban_sse_fetch_new(board, cursor)
            if events:
                # Emit `id: <last_event_id>` on every events frame so the
                # browser sets Last-Event-ID on auto-reconnect, letting us
                # resume from there without re-streaming the backlog.
                payload = json.dumps({"events": events, "cursor": cursor})
                frame = (f"id: {cursor}\nevent: events\ndata: {payload}\n\n").encode(
                    "utf-8"
                )
                try:
                    handler.wfile.write(frame)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                    return True
                last_heartbeat = time.monotonic()
            else:
                # Heartbeat keeps reverse proxies and the browser from
                # closing an idle stream. SSE comments (lines starting
                # with `:`) are ignored by EventSource.
                if (time.monotonic() - last_heartbeat) >= _KANBAN_SSE_HEARTBEAT_SECONDS:
                    try:
                        handler.wfile.write(b": keepalive\n\n")
                        handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                        return True
                    last_heartbeat = time.monotonic()
            time.sleep(_KANBAN_SSE_POLL_SECONDS)
    except Exception:
        # Any other unexpected exception in the SSE loop should not bubble
        # up to the request handler (which would 500 a long-lived stream).
        return True
