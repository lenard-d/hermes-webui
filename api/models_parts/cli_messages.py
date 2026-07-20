"""Reconciled CLI messages and conversation rounds.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def reconciled_state_db_messages_for_session(
    session, *, prefer_context: bool = False, state_messages: list | None = None
) -> list:
    """Return append-only messages reconciled with state.db for a WebUI session."""
    if session is None:
        return []
    local_messages = []
    using_context_messages = False
    if prefer_context:
        context_messages = getattr(session, 'context_messages', None)
        if isinstance(context_messages, list) and context_messages:
            local_messages = context_messages
            using_context_messages = True
    if not local_messages:
        local_messages = getattr(session, 'messages', None) or []
    if state_messages is None:
        state_messages = get_state_db_session_messages(getattr(session, 'session_id', None))
    if prefer_context and local_messages:
        if using_context_messages:
            sidecar_messages = getattr(session, 'messages', None) or []
            if (
                getattr(session, 'is_cli_session', False)
                and not getattr(session, 'read_only', False)
                and sidecar_messages
                and len(sidecar_messages) > len(local_messages)
                and _session_messages_have_prefix(sidecar_messages, local_messages)
            ):
                # A claimed CLI sidecar can carry a stale context prefix while the
                # stitched CLI transcript already landed in session.messages. On the
                # first WebUI follow-up, prefer that longer authoritative transcript
                # unless context_messages intentionally diverged via compaction or
                # another non-prefix transform.
                local_messages = sidecar_messages
                using_context_messages = False
            if using_context_messages:
                compressed_context = _context_messages_include_compression_marker(local_messages)
                anchor_key = getattr(session, "compression_anchor_message_key", None)
                if compressed_context:
                    if not anchor_key:
                        logger.debug(
                            "Compressed context for session %s has no compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    anchor_index = _state_db_anchor_index(state_messages, anchor_key)
                    if anchor_index is None:
                        logger.debug(
                            "Compressed context for session %s has an unverifiable compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    state_messages = list(state_messages or [])[anchor_index + 1 :]
        state_messages = state_db_delta_after_context(local_messages, state_messages)
    return merge_session_messages_append_only(
        local_messages,
        state_messages,
        truncation_watermark=getattr(session, "truncation_watermark", None),
        truncation_boundary=getattr(session, "truncation_boundary", None),
    )


def get_cli_session_messages(sid, *, profile=None) -> list:
    """Read messages for a single CLI/external-agent session.

    Preserve tool-call/result and reasoning metadata from the agent state.db so
    CLI-origin transcripts render with the same tool cards as WebUI-native
    sessions. When the requested session is the tip of a compression/CLI-close
    continuation chain, return the stitched full transcript across all segments
    in chronological order. Returns empty list on any error.
    """
    if str(sid or '').startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return get_claude_code_session_messages(sid)
    return get_state_db_session_messages(sid, stitch_continuations=True, profile=profile)


def count_conversation_rounds(sid: str, since: float | None = None) -> int:
    """Count conversation rounds for a session from state.db.

    A "round" = one user message + one agent reply.  Consecutive user
    messages are merged into a single round so that multi-part questions
    don't inflate the count.

    Parameters
    ----------
    sid : str
        Gateway session ID (e.g. ``20260430_151231_7209a0``).
    since : float | None
        Unix timestamp.  If provided, only messages **after** this
        timestamp are counted.

    Returns
    -------
    int
        Number of complete conversation rounds.
    """
    import os, sqlite3, datetime

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return 0

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT role, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (sid,),
            )
            rows = cur.fetchall()
    except Exception:
        return 0

    rounds = 0
    seen_user = False          # have we seen a user msg in the current round?
    seen_agent_after_user = False  # have we seen an agent reply after that user msg?

    for row in rows:
        role = (row['role'] or '').strip().lower()
        ts_raw = row['timestamp']

        # Parse timestamp and apply the ``since`` filter.
        if since is not None and ts_raw is not None:
            try:
                if isinstance(ts_raw, (int, float)):
                    ts_val = float(ts_raw)
                else:
                    # ISO-8601 string
                    ts_val = datetime.datetime.fromisoformat(
                        str(ts_raw).replace('Z', '+00:00')
                    ).timestamp()
                if ts_val <= since:
                    continue
            except Exception:
                pass

        if role == 'user':
            if seen_user and not seen_agent_after_user:
                # Consecutive user message — merge into current round.
                pass
            elif seen_user and seen_agent_after_user:
                # Previous round completed, starting a new one.
                rounds += 1
                seen_agent_after_user = False
            seen_user = True
        elif role == 'assistant':
            if seen_user:
                seen_agent_after_user = True

    # Close the last round if it was completed.
    if seen_user and seen_agent_after_user:
        rounds += 1

    return rounds


CONVERSATION_ROUND_THRESHOLD = 10

__all__ = ['reconciled_state_db_messages_for_session', 'get_cli_session_messages', 'count_conversation_rounds', 'CONVERSATION_ROUND_THRESHOLD']
