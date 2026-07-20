"""SSE replay, runner reattach, gateway probing, and session event transport."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations


def _sse_with_id(handler, event, data, event_id=None):
    if event_id:
        handler.wfile.write(f"id: {event_id}\n".encode("utf-8"))
    _sse(handler, event, data)


def _session_events_path_session_id(path: str | None) -> str | None:
    path = str(path or "")
    parts = path.strip("/").split("/")
    if len(parts) != 4:
        return None
    if parts[0] != "api" or parts[1] != "sessions" or parts[3] != "events":
        return None
    sid = str(parts[2] or "").strip()
    return sid or None


def _session_events_resume_event_id(handler, parsed) -> str | None:
    headers = getattr(handler, "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("Last-Event-ID")
        except Exception:
            raw = None
    raw = str(raw or "").strip()
    if raw:
        return raw
    qs = parse_qs(getattr(parsed, "query", "") or "")
    raw = str(qs.get("after_event_id", [None])[0] or "").strip()
    return raw or None


def _session_snapshot_payload(session, *, active_stream_id: str | None = None) -> dict:
    try:
        payload = session.compact(
            include_runtime=bool(active_stream_id),
            active_stream_ids={active_stream_id} if active_stream_id else None,
        )
    except Exception:
        payload = {"session_id": str(getattr(session, "session_id", "") or "")}
    return {"session": payload}


def _parse_run_journal_event_id(raw: str | None) -> tuple[str | None, int | None]:
    return _shared_parse_run_journal_event_id(raw)


def _parse_run_journal_after_seq(qs: dict, stream_id: str | None = None) -> int | None:
    event_run_id, event_seq = _parse_run_journal_event_id(qs.get("after_event_id", [None])[0])
    if event_run_id:
        if stream_id and event_run_id != stream_id:
            return None
        return event_seq
    raw = qs.get("after_seq", [None])[0]
    if raw in (None, ""):
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _replay_run_journal(
    handler,
    stream_id: str,
    after_seq: int | None,
    *,
    max_seq: int | None = None,
    include_stale: bool = True,
) -> bool:
    summary = find_run_summary(stream_id)
    if not summary:
        return False
    journal = read_run_events(
        str(summary.get("session_id") or ""),
        stream_id,
        after_seq=after_seq,
        max_seq=max_seq,
    )
    for entry in journal.get("events") or []:
        _sse_with_id(
            handler,
            entry.get("event") or entry.get("type") or "message",
            entry.get("payload"),
            entry.get("event_id"),
        )
    if include_stale and not summary.get("terminal"):
        stale = stale_interrupted_event(
            str(summary.get("session_id") or ""),
            stream_id,
            after_seq=after_seq,
        )
        if stale:
            _sse_with_id(handler, stale["event"], stale["payload"], stale["event_id"])
    return True


def _run_journal_same_run_seq(event_id: str | None, stream_id: str) -> int | None:
    event_run_id, event_seq = _parse_run_journal_event_id(event_id)
    if event_run_id != stream_id:
        return None
    return event_seq


def _run_journal_covers_offline_gap(
    stream_id: str, after_seq: int | None, cutoff_seq: int | None
) -> bool:
    """Return True when the run journal PROVABLY backfills a dropped-frame gap.

    When StreamChannel evicted frames from its offline buffer
    (``offline_dropped_events > 0``), draining the retained tail to a
    reconnecting client is only safe if the journal replay actually covers
    everything from the client's cursor (*after_seq*, ``None`` = start of run)
    through the snapshot cutoff — journal seqs are assigned contiguously from 1,
    so coverage means every seq in ``(after_seq, cutoff_seq]`` is present. A
    missing journal, a journal that stops short of the cutoff, or a window with
    malformed/dropped lines all mean the evicted frames are unrecoverable here
    and the caller must signal recovery instead of streaming tail-only.

    ``cutoff_seq is None`` (the channel never saw a same-run journaled event id)
    counts as not covered: nothing can be proven against an unknown cutoff.
    """
    if cutoff_seq is None:
        return False
    floor = max(0, int(after_seq)) if after_seq is not None else 0
    if floor >= cutoff_seq:
        # Client cursor is already at/past everything the buffer ever held.
        return True
    try:
        summary = find_run_summary(stream_id)
        if not summary:
            return False
        journal = read_run_events(
            str(summary.get("session_id") or ""),
            stream_id,
            after_seq=floor,
            max_seq=cutoff_seq,
        )
    except Exception:
        logger.debug(
            "Run journal coverage check failed for stream %s", stream_id, exc_info=True
        )
        return False
    cutoff = int(cutoff_seq)
    seqs = set()
    for entry in journal.get("events") or []:
        try:
            seq = int(entry.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if floor < seq <= cutoff:
            seqs.add(seq)
    # Seqs are unique and bounded to the window, so full coverage means one
    # distinct seq per slot — no need to materialize the whole range.
    return len(seqs) == cutoff - floor


def _sse_replay_run_journal_gap_checked(
    handler, qs: dict, stream_id: str, stream_snapshot: dict
) -> tuple[bool, int | None]:
    """Journal-replay for a reconnecting client, enforcing offline-gap coverage.

    Returns ``(gap_recovered, replay_cutoff_seq)``. When the channel evicted
    frames from its offline buffer (``offline_dropped_events > 0`` in the
    subscribe snapshot) and the run journal cannot PROVE it backfills the gap
    (see ``_run_journal_covers_offline_gap``), a recovery_control apperror has
    been emitted and the caller must return instead of draining the retained
    tail (``gap_recovered=True``).
    """
    if not (
        qs.get("replay", [""])[0]
        or qs.get("after_seq", [None])[0] not in (None, "")
        or qs.get("after_event_id", [None])[0]
    ):
        return False, None
    try:
        offline_dropped = int(stream_snapshot.get("offline_dropped_events") or 0)
    except (TypeError, ValueError):
        offline_dropped = 0
    snapshot_cutoff_seq = _run_journal_same_run_seq(
        str(stream_snapshot.get("last_event_id") or ""),
        stream_id,
    )
    after_seq = _parse_run_journal_after_seq(qs, stream_id)
    # The subscribe snapshot already queued the retained offline tail, which
    # covers [first buffered frame → snapshot cutoff] by itself. The journal
    # only has to bridge (client cursor → first buffered frame) — and the
    # replay/dedup cutoff must stop there too, or the drain loop's
    # `seq <= replay_cutoff_seq` filter would eat queued frames the journal
    # never emitted. Without a parseable first-frame id (empty buffer, foreign
    # run, unjournaled head frame) fall back to the full (cursor → cutoff]
    # window as before.
    replay_max_seq = snapshot_cutoff_seq
    first_buffered_seq = _run_journal_same_run_seq(
        str(stream_snapshot.get("offline_first_event_id") or ""),
        stream_id,
    )
    if first_buffered_seq is not None:
        replay_max_seq = first_buffered_seq - 1
        if snapshot_cutoff_seq is not None:
            replay_max_seq = min(replay_max_seq, snapshot_cutoff_seq)
    covered = offline_dropped <= 0 or _run_journal_covers_offline_gap(
        stream_id, after_seq, replay_max_seq
    )
    replay_cutoff_seq = None
    replay_failed = False
    if covered:
        try:
            if _replay_run_journal(
                handler,
                stream_id,
                after_seq,
                max_seq=replay_max_seq,
                include_stale=False,
            ):
                replay_cutoff_seq = replay_max_seq
        except _CLIENT_DISCONNECT_ERRORS:
            raise
        except Exception:
            replay_failed = True
            logger.debug("Failed to replay active run journal for stream %s", stream_id, exc_info=True)
    if offline_dropped > 0 and (not covered or replay_failed):
        _sse_offline_gap_recovery(handler, stream_id, offline_dropped)
        return True, None
    # Two distinct dedup bounds feed the drain loop's `seq <=` filter: frames
    # the journal replay just emitted (replay_cutoff_seq, capped at the buffer
    # head so queued frames the journal never sent survive) AND frames the
    # client already holds per its own cursor. A cursor at/inside the retained
    # tail (after_seq >= first buffered frame) would otherwise get the queued
    # copy of frames it already rendered — a double-render, since this filter
    # is the only dedup for replayed streams.
    if after_seq is not None:
        cursor_bound = after_seq
        if snapshot_cutoff_seq is not None:
            # A legitimate cursor can never exceed the channel's last known
            # frame; clamping keeps a bogus/corrupt cursor from filtering the
            # queued terminal frame and pinning the loop on heartbeats.
            cursor_bound = min(cursor_bound, snapshot_cutoff_seq)
        replay_cutoff_seq = (
            cursor_bound
            if replay_cutoff_seq is None
            else max(replay_cutoff_seq, cursor_bound)
        )
    return False, replay_cutoff_seq


def _sse_offline_gap_recovery(handler, stream_id: str, offline_dropped: int) -> None:
    """Signal an unrecoverable replay gap instead of streaming tail-only.

    Frames were evicted from the channel's capped offline buffer and the run
    journal cannot prove it backfills (client cursor → snapshot cutoff]:
    draining the retained tail would render a silent transcript hole that ends
    in a normal ``stream_end``. Emit the established ``recovery_control``
    apperror (same client contract as ``run_journal.stale_interrupted_event``)
    so the tab restores the transcript from persisted session state instead.
    """
    # The client only acts on the recovery signal when the payload names its
    # session (eventMatchesCurrent), so fall back to the journal summary when
    # the pre-worker owner registration is already gone.
    try:
        session_id = runtime_run_session_id(stream_id) or ""
        if not session_id:
            session_id = str((find_run_summary(stream_id) or {}).get("session_id") or "")
    except Exception:
        session_id = ""
    _sse(
        handler,
        "apperror",
        {
            "type": "interrupted",
            "recovery_control": True,
            "message": (
                "The live stream's replay buffer overflowed while no tab was "
                "attached and the run journal cannot backfill the dropped frames."
            ),
            "hint": "The transcript was restored to the last saved state.",
            "session_id": session_id,
            "stream_id": stream_id,
            "offline_dropped_events": offline_dropped,
        },
    )


def _runner_stream_cursor_from_query(qs: dict) -> str | None:
    cursor = str(qs.get("cursor", [""])[0] or "").strip()
    if cursor:
        return cursor
    after_seq = _parse_run_journal_after_seq(qs)
    return str(after_seq) if after_seq is not None else None


def _runner_event_name(entry: dict) -> str:
    return str(entry.get("event") or entry.get("type") or "message")


def _runner_event_payload(entry: dict):
    if "payload" in entry:
        return entry.get("payload")
    if "data" in entry:
        return entry.get("data")
    return entry


def _runner_event_id(run_id: str, entry: dict) -> str | None:
    event_id = entry.get("event_id") or entry.get("id")
    if event_id:
        return str(event_id)
    seq = entry.get("seq")
    if seq not in (None, ""):
        return f"{run_id}:{seq}"
    return None


def _stream_runner_run_events(handler, run_id: str, cursor: str | None = None) -> bool:
    """Stream events from a configured runner without WebUI-owned runtime maps."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return False
    try:
        from api.runtime_adapter import build_runtime_adapter, runtime_adapter_runner_enabled

        if not runtime_adapter_runner_enabled():
            return False
        adapter = build_runtime_adapter(runner_client_factory=_runtime_runner_client_factory)
    except NotImplementedError:
        return False
    if adapter is None:
        return False

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)
    cursor_value = cursor
    try:
        while True:
            try:
                event_stream = adapter.observe_run(run_id, cursor=cursor_value)
            except Exception as exc:
                _sse(handler, "error", {"error": _sanitize_error(exc)})
                break
            emitted = False
            terminal = False
            for entry in list(getattr(event_stream, "events", []) or []):
                if not isinstance(entry, dict):
                    continue
                event = _runner_event_name(entry)
                _sse_with_id(handler, event, _runner_event_payload(entry), _runner_event_id(run_id, entry))
                emitted = True
                if event in ("stream_end", "error", "cancel"):
                    terminal = True
            next_cursor = getattr(event_stream, "cursor", None)
            if next_cursor not in (None, ""):
                cursor_value = str(next_cursor)
            if terminal:
                break
            if not emitted:
                status = None
                try:
                    status = adapter.get_run(run_id)
                except Exception:
                    status = None
                state = str(getattr(status, "terminal_state", None) or getattr(status, "status", "") or "").lower()
                if state in ("completed", "complete", "failed", "error", "cancelled", "canceled"):
                    _sse(handler, "stream_end", {"run_id": run_id, "status": state})
                    break
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
                time.sleep(_SSE_HEARTBEAT_INTERVAL_SECONDS)
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    return True


def _handle_sse_stream(handler, parsed):
    qs = parse_qs(parsed.query)
    stream_id = qs.get("stream_id", [""])[0]
    if not _stream_id_visible_to_request_profile(handler, stream_id):
        return True
    stream = runtime_transport(stream_id)
    if stream is None:
        if _stream_runner_run_events(handler, stream_id, _runner_stream_cursor_from_query(qs)):
            return True
        try:
            journal_available = bool(find_run_summary(stream_id)) if stream_id else False
        except Exception:
            journal_available = False
        if not journal_available:
            return j(handler, {"error": "stream not found"}, status=404)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "close")
        end_sse_headers(handler)
        try:
            _replay_run_journal(handler, stream_id, _parse_run_journal_after_seq(qs, stream_id))
        except _CLIENT_DISCONNECT_ERRORS:
            pass
        return True
    if hasattr(stream, "subscribe_with_snapshot"):
        subscriber, stream_snapshot = stream.subscribe_with_snapshot()
    else:
        subscriber = stream.subscribe() if hasattr(stream, "subscribe") else stream
        stream_snapshot = {}
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread
    # Replay shares the drain loop's try/finally so every exit path unsubscribes.
    try:
        gap_recovered, replay_cutoff_seq = _sse_replay_run_journal_gap_checked(
            handler, qs, stream_id, stream_snapshot
        )
        if gap_recovered:
            return True
        while True:
            try:
                item = subscriber.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
                continue
            if len(item) >= 3:
                event, data, queued_event_id = item[0], item[1], item[2]
            else:
                event, data = item
                queued_event_id = runtime_last_event_id(stream_id)
            # Stage-364: emit `id:` from the runtime cursor snapshot so
            # the frontend's `_lastRunJournalSeq` cursor advances during live
            # streaming. Without this, mid-stream error→replay would arrive
            # with after_seq=0 and double-render every journaled event.
            event_id = queued_event_id or runtime_last_event_id(stream_id)
            event_seq = _run_journal_same_run_seq(event_id, stream_id)
            if replay_cutoff_seq is not None and event_seq is not None and event_seq <= replay_cutoff_seq:
                continue
            if event_id:
                _sse_with_id(handler, event, data, event_id)
            else:
                _sse(handler, event, data)
            if event in ("stream_end", "error", "cancel"):
                break
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        if subscriber is not stream and hasattr(stream, "unsubscribe"):
            try:
                stream.unsubscribe(subscriber)
            except Exception:
                pass
    return True


def _handle_session_run_journal_stream_for_session(handler, parsed, session_id):
    if not _session_id_visible_to_request_profile(handler, session_id):
        return True
    try:
        session = get_session(session_id, metadata_only=True)
    except KeyError:
        return j(handler, {"error": "Session not found"}, status=404)

    # Parse the resume cursor and baseline the journal BEFORE committing SSE headers
    # (and thus before any run could complete mid-handler). Capturing after
    # end_sse_headers() leaves a window where a run finishing between header commit
    # and baseline is absorbed into the baseline and silently lost. Both operations
    # are side-effect-free (header read + stat-only fingerprint), safe pre-response.
    resume_event_id = _session_events_resume_event_id(handler, parsed)
    _idle_journal_fp = session_journal_fingerprint(session_id)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    # #3103: see _handle_gateway_sse_stream — `Connection: close` causes
    # EventSource reconnect storms in browsers on long-lived SSE.
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)

    active_stream_id = _active_run_stream_for_session(session_id)
    subscriber = None
    subscriber_stream = None
    replay_cutoff_seq = None
    sent_event_ids: set[str] = set()
    sent_event_order = deque()

    def note_sent_event_id(event_id):
        if not event_id:
            return
        sent_event_ids.add(event_id)
        sent_event_order.append(event_id)
        while len(sent_event_order) > _SESSION_SSE_SENT_EVENT_ID_LIMIT:
            sent_event_ids.discard(sent_event_order.popleft())

    def attach_active_stream():
        stream_id = _active_run_stream_for_session(session_id)
        stream = runtime_transport(stream_id) if stream_id else None
        if stream is None:
            return None, None, None, stream_id
        if hasattr(stream, "subscribe_with_snapshot"):
            queue_, snapshot = stream.subscribe_with_snapshot()
        else:
            queue_ = stream.subscribe() if hasattr(stream, "subscribe") else stream
            snapshot = {}
        return queue_, stream, snapshot, stream_id

    def emit_replay(events, stream_id, cutoff_seq):
        for entry in events:
            event_id = str(entry.get("event_id") or "")
            event_seq = _run_journal_same_run_seq(event_id, stream_id)
            if cutoff_seq is not None and event_seq is not None and event_seq > cutoff_seq:
                continue
            if event_id and event_id in sent_event_ids:
                continue
            _sse_with_id(handler, entry.get("event") or entry.get("type") or "message", entry.get("payload"), event_id)
            if event_id:
                note_sent_event_id(event_id)

    def emit_session_snapshot(active_stream_id):
        try:
            fresh_session = get_session(session_id, metadata_only=True)
        except KeyError:
            fresh_session = session
        _sse(handler, "session_snapshot", _session_snapshot_payload(fresh_session, active_stream_id=active_stream_id))

    try:
        replay_events = []
        replay_ok = False
        if resume_event_id:
            replay = read_session_run_events(session_id, after_event_id=resume_event_id)
            if replay.get("status") != "ok":
                emit_session_snapshot(active_stream_id)
            else:
                replay_ok = True
                replay_events = replay.get("events") or []
        subscriber, subscriber_stream, stream_snapshot, active_stream_id = attach_active_stream()
        if subscriber is None:
            if replay_ok:
                emit_replay(replay_events, active_stream_id, None)
            while True:
                subscriber, subscriber_stream, stream_snapshot, active_stream_id = attach_active_stream()
                if subscriber is not None:
                    break
                # Journal advanced with no live stream to attach → a run completed
                # entirely within the wait (or the first attach). Re-sync via a
                # snapshot boundary (the same honest-recovery contract used for a
                # failed reconciliation), then re-baseline so we only re-sync on
                # genuinely new advances.
                _current_journal_fp = session_journal_fingerprint(session_id)
                if _current_journal_fp != _idle_journal_fp:
                    _idle_journal_fp = _current_journal_fp
                    emit_session_snapshot(active_stream_id)
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                time.sleep(_SSE_HEARTBEAT_INTERVAL_SECONDS)
        if subscriber is None:
            return True
        if replay_ok:
            replay_cutoff_seq = _run_journal_same_run_seq(str(stream_snapshot.get("last_event_id") or ""), active_stream_id)
            reconciled = read_session_run_events(session_id, after_event_id=resume_event_id)
            if reconciled.get("status") == "ok":
                emit_replay(reconciled.get("events") or [], active_stream_id, replay_cutoff_seq)
            else:
                emit_session_snapshot(active_stream_id)
        try:
            while True:
                try:
                    item = subscriber.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
                except queue.Empty:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
                    continue
                if len(item) >= 3:
                    event, data, queued_event_id = item[0], item[1], item[2]
                else:
                    event, data = item
                    queued_event_id = runtime_last_event_id(active_stream_id)
                event_id = queued_event_id or runtime_last_event_id(active_stream_id)
                event_seq = _run_journal_same_run_seq(event_id, active_stream_id)
                _is_terminal = event in ("stream_end", "error", "cancel")
                _already_sent = (
                    (replay_cutoff_seq is not None and event_seq is not None and event_seq <= replay_cutoff_seq)
                    or (event_id and event_id in sent_event_ids)
                )
                if _already_sent:
                    # Already delivered via replay/reconciliation (cutoff or dedup).
                    # A terminal event still has to end this loop — otherwise, when
                    # reconciliation replayed the active run's terminal at the cutoff,
                    # the live copy would be skipped here and the handler would stay
                    # blocked on a dead run's queue and miss subsequent session runs.
                    if _is_terminal:
                        break
                    continue
                if event_id:
                    _sse_with_id(handler, event, data, event_id)
                    note_sent_event_id(event_id)
                else:
                    _sse(handler, event, data)
                if _is_terminal:
                    break
        except _CLIENT_DISCONNECT_ERRORS:
            pass
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        if subscriber is not None and subscriber is not subscriber_stream and hasattr(subscriber_stream, "unsubscribe"):
            try:
                subscriber_stream.unsubscribe(subscriber)
            except Exception:
                pass
    return True


_handle_session_sse_stream_for_session = _handle_session_run_journal_stream_for_session


def _gateway_sse_probe_payload(settings, watcher):
    enabled = bool(settings.get('show_cli_sessions'))
    # Use the public is_alive() accessor where available (current GatewayWatcher);
    # fall back to the private _thread check for any older in-memory instance
    # that might still be hanging around mid-upgrade, and for test doubles that
    # don't implement the full public API.
    if watcher is None:
        watcher_alive = False
    elif hasattr(watcher, 'is_alive') and callable(getattr(watcher, 'is_alive')):
        watcher_alive = bool(watcher.is_alive())
    else:
        _t = getattr(watcher, '_thread', None)
        watcher_alive = _t is not None and _t.is_alive()
    payload = {
        'enabled': enabled,
        'fallback_poll_ms': 30000,
        'ok': enabled and watcher_alive,
        'watcher_running': watcher_alive,
    }
    if not enabled:
        payload['error'] = 'agent sessions not enabled'
        return payload, 404
    if not watcher_alive:
        payload['error'] = 'watcher not started'
        return payload, 503
    return payload, 200


def _handle_gateway_sse_stream(handler, parsed):
    """SSE endpoint for real-time gateway session updates.
    Streams change events from the gateway watcher background thread.
    Only active when show_cli_sessions (show_agent_sessions) setting is enabled.
    """
    settings = load_settings()

    from api.gateway_watcher import get_watcher
    watcher = get_watcher()

    probe = parse_qs(parsed.query).get('probe', [''])[0].lower() in {'1', 'true', 'yes'}
    if probe:
        payload, status = _gateway_sse_probe_payload(settings, watcher)
        return j(handler, payload, status=status)

    # Check if the feature is enabled
    if not settings.get('show_cli_sessions'):
        return j(handler, {'error': 'agent sessions not enabled'}, status=404)

    # Same watcher_alive semantics as the probe path — centralised via
    # the helper so both branches stay in sync.
    _probe_body, _probe_status = _gateway_sse_probe_payload(settings, watcher)
    if not _probe_body['watcher_running']:
        return j(handler, {'error': 'watcher not started'}, status=503)

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    # #3103: do NOT emit `Connection: close` on long-lived SSE streams.
    # The python BaseHTTPServer worker only handles one request per
    # connection anyway, but browsers (Chrome/Firefox) treat the close
    # header as a hard signal that the EventSource lifecycle has ended
    # and trigger an instant reconnect, producing a tight loop of
    # connect/sessions_changed snapshot/disconnect that thrashes the
    # session list every ~1s. Letting the server close the socket
    # naturally after the stream ends is sufficient.
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    q = watcher.subscribe()
    try:
        # Send initial snapshot immediately
        from api.models import get_cli_sessions
        initial = get_cli_sessions()
        _sse(handler, 'sessions_changed', {'sessions': initial})

        while True:
            try:
                event_data = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if event_data is None:
                break  # watcher is stopping
            _sse(handler, event_data.get('type', 'sessions_changed'), event_data)
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        watcher.unsubscribe(q)
    return True


def _handle_session_events_stream(handler):
    """SSE endpoint for lightweight session-list invalidation events."""
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    # #3103: see _handle_gateway_sse_stream — `Connection: close` causes
    # EventSource reconnect storms in browsers on long-lived SSE.
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    q = subscribe_session_events()
    try:
        while True:
            try:
                event_data = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            _sse(handler, event_data.get('type', 'sessions_changed'), event_data)
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        unsubscribe_session_events(q)
    return True


__routes_exports__ = (
    "_sse_with_id",
    "_session_events_path_session_id",
    "_session_events_resume_event_id",
    "_session_snapshot_payload",
    "_parse_run_journal_event_id",
    "_parse_run_journal_after_seq",
    "_replay_run_journal",
    "_run_journal_same_run_seq",
    "_run_journal_covers_offline_gap",
    "_sse_replay_run_journal_gap_checked",
    "_sse_offline_gap_recovery",
    "_runner_stream_cursor_from_query",
    "_runner_event_name",
    "_runner_event_payload",
    "_runner_event_id",
    "_stream_runner_run_events",
    "_handle_sse_stream",
    "_handle_session_run_journal_stream_for_session",
    "_handle_session_sse_stream_for_session",
    "_gateway_sse_probe_payload",
    "_handle_gateway_sse_stream",
    "_handle_session_events_stream",
)
