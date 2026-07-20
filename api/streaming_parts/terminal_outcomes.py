"""Synthetic controls, tool-limit outcomes, and cancelled-turn finalization."""

from __future__ import annotations

import time
from types import ModuleType


def is_synthetic_max_iteration_summary_request(api: ModuleType, message) -> bool:
    """Return True for Hermes Agent's internal max-iteration summary prompt."""
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    text = " ".join(api._message_text(message.get('content', '')).split())
    expected = " ".join(api._MAX_ITERATION_SUMMARY_REQUEST.split())
    return text == expected


def drop_synthetic_max_iteration_summary_requests(api: ModuleType, messages, *, enabled: bool = True):
    """Remove Agent-internal max-iteration summary prompts from WebUI state."""
    if not enabled:
        return list(messages or [])
    return [
        msg
        for msg in list(messages or [])
        if not api._is_synthetic_max_iteration_summary_request(msg)
    ]


def is_synthetic_control_message(api: ModuleType, message) -> bool:
    """Return True for an Agent-internal synthetic scaffolding turn flagged by marker."""
    return isinstance(message, dict) and any(
        message.get(flag) for flag in api._SYNTHETIC_CONTROL_MESSAGE_FLAGS
    )


def drop_synthetic_control_messages(api: ModuleType, messages):
    """Remove Agent-internal synthetic scaffolding turns from the WebUI transcript.

    Honors the structured ``_verification_stop_synthetic`` / ``_pre_verify_synthetic``
    markers the agent already sets, rather than string-matching the nudge copy.
    """
    return [
        msg
        for msg in list(messages or [])
        if not api._is_synthetic_control_message(msg)
    ]


def agent_result_tool_limit_reached(api: ModuleType, result) -> bool:
    """Return True when current-turn metadata says the tool iteration cap fired."""
    if not isinstance(result, dict):
        return False
    fields = [
        result.get('turn_exit_reason'),
        result.get('terminal_reason'),
        result.get('status'),
        result.get('state'),
        result.get('error'),
    ]
    haystack = " ".join(str(value or '') for value in fields).lower()
    if (
        'max_iterations_reached' in haystack
        or 'maximum number of tool-calling iterations' in haystack
        or ('tool-calling iterations' in haystack and 'maximum' in haystack)
    ):
        return True
    return False


def maybe_inject_max_iteration_summary_fallback(api: ModuleType, messages, result) -> list:
    """Append the agent's graceful summary text as an assistant turn when one is missing.

    When ``AIAgent`` exhausts its iteration budget, ``agent.handle_max_iterations``
    always returns a non-empty ``final_response`` — either the model-generated
    summary or a graceful fallback (e.g. ``"I reached the iteration limit and
    couldn't generate a summary."``). Hermes Agent surfaces that string as the
    final answer to the user; the WebUI, by contrast, reads only ``messages``,
    so an empty summary (common with reasoning-only responses) left the user
    with a bare ``tool_limit_reached`` error instead of any closure text.

    When ``_tool_limit_reached`` is true and ``messages`` ends without a final
    assistant answer, inject ``result['final_response']`` as a new assistant
    turn so ``api._mark_latest_assistant_tool_limit_status`` can attach the status
    card in the normal flow and the user sees the same closure text as
    hermes-agent. Returns the (possibly new) messages list; does nothing when
    a usable assistant answer already exists or when ``result`` carries no
    graceful fallback text.
    """
    if not isinstance(result, dict):
        return list(messages or [])
    fallback = result.get('final_response')
    if not isinstance(fallback, str) or not fallback.strip():
        return list(messages or [])
    out = list(messages or [])
    if not api._session_lacks_final_assistant_answer(out):
        return out
    # Append a synthetic summary turn. Tag it so downstream consumers can
    # distinguish it from model-emitted assistant turns if needed; mirrors the
    # synthetic-scaffolding flag convention already used elsewhere (#5334).
    out.append({"role": "assistant", "content": fallback, "_max_iteration_summary_fallback": True})
    return out


def mark_latest_assistant_tool_limit_status(api: ModuleType, messages) -> bool:
    """Annotate the latest usable assistant final answer as limit-stopped."""
    for msg in reversed(list(messages or [])):
        if not isinstance(msg, dict):
            continue
        if msg.get('_error') or msg.get('role') != 'assistant':
            continue
        content = msg.get('content')
        if isinstance(content, list):
            text = '\n'.join(
                str(part.get('text') or part.get('content') or '')
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = str(content or '')
        if msg.get('tool_calls') or not text.strip():
            continue
        msg['_terminal_state'] = 'tool_limit_reached'
        msg['_terminal_reason'] = 'max_iterations'
        msg.setdefault('_statusCard', {
            'title': 'Tool iteration limit reached',
            'subtitle': 'Stopped because the tool iteration limit was reached.',
            'rows': [
                {'label': 'State', 'value': 'Limit reached'},
                {'label': 'Next step', 'value': 'Start a new turn to continue.'},
            ],
        })
        return True
    return False


def session_has_cancel_marker(api: ModuleType, session) -> bool:
    """Return True if a visible cancel/interrupted marker is already persisted."""
    for msg in reversed(getattr(session, 'messages', None) or []):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'user':
            return False
        if msg.get('role') != 'assistant':
            continue
        content = msg.get('content')
        text = ''
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get('text') or part.get('content') or ''))
            text = '\n'.join(parts)
        normalized = text.strip().lower()
        if any(pattern in normalized for pattern in api._CANCEL_MARKER_PATTERNS):
            return True
    return False


def cancelled_turn_content(api: ModuleType, message: str = 'Task cancelled.', agent_name: str | None = None) -> str:
    """Return cancelled-turn copy matching the verbose provider-error layout."""
    _message = str(message or 'Task cancelled.').strip()
    if not _message.endswith('.'):
        _message += '.'
    return (
        f"**Task cancelled:** {_message}\n\n"
        f"*{api._cancelled_turn_hint(agent_name)}*"
    )


def persist_cancelled_turn(api: ModuleType, session, *, message: str = 'Task cancelled.') -> None:
    """Persist a user-cancelled terminal state without provider-error wording.

    cancel_stream() usually writes this marker first, but the streaming thread can
    later unwind through the silent-failure or exception path. Those paths must
    not append a misleading provider no-response error after an explicit cancel.
    """
    api._materialize_pending_user_turn_before_error(session)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    if not api._session_has_cancel_marker(session):
        agent_name = api._preferred_agent_display_name_for_session(session)
        session.messages.append({
            'role': 'assistant',
            'content': api._cancelled_turn_content(message, agent_name),
            '_error': True,
            'provider_details': str(message or 'Task cancelled.').strip(),
            'provider_details_label': 'Cancellation details',
            'timestamp': int(time.time()),
        })


def cleanup_ephemeral_cancelled_turn(api: ModuleType, session) -> None:
    """Remove transient /btw session state after a cancel without saving it."""
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    try:
        import pathlib
        pathlib.Path(session.path).unlink(missing_ok=True)
    except Exception:
        api.logger.debug("Failed to clean up ephemeral cancelled session", exc_info=True)


def finalize_cancelled_turn(api: ModuleType, session, *, ephemeral: bool = False, message: str = 'Task cancelled.') -> None:
    """Finalize a cancelled turn for persistent or ephemeral sessions."""
    if ephemeral:
        api._cleanup_ephemeral_cancelled_turn(session)
        return
    api._persist_cancelled_turn(session, message=message)
    try:
        session.save()
    except Exception:
        api.logger.debug("Failed to persist cancelled turn", exc_info=True)


def aiagent_import_error_detail(api: ModuleType) -> str:
    """Return a multi-line diagnostic string for the "AIAgent not available" path.

    The bare ImportError ("AIAgent not available -- check that hermes-agent is
    on sys.path") leaves users guessing at which python is running, where it's
    looking, and what to fix. We assemble the same evidence a maintainer would
    ask for first (issue #1695): the python that's running, the agent_dir env
    var if set, the sys.path entries that mention 'hermes', and the most-common
    fix (`pip install -e .` in the agent dir).

    Kept as a separate helper so it stays out of the hot path until we actually
    need to raise — building it on every successful import would be wasted work.
    """
    import os as _os
    import sys as _sys

    lines = ["AIAgent not available -- check that hermes-agent is on sys.path"]
    lines.append("")
    lines.append(f"  python:  {_sys.executable}")
    agent_dir = _os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if agent_dir:
        lines.append(f"  HERMES_WEBUI_AGENT_DIR: {agent_dir}")
    else:
        lines.append("  HERMES_WEBUI_AGENT_DIR: (not set)")

    # Show only the sys.path entries that look relevant — full sys.path is noisy.
    relevant = [p for p in _sys.path if "hermes" in p.lower() or "agent" in p.lower()]
    if relevant:
        lines.append("  sys.path entries mentioning hermes/agent:")
        for entry in relevant[:6]:
            lines.append(f"    - {entry}")
        if len(relevant) > 6:
            lines.append(f"    ... and {len(relevant) - 6} more")
    else:
        lines.append("  sys.path: (no entries mention hermes or agent)")

    lines.append("")
    lines.append("  Most common fix: install the agent in editable mode so its modules")
    lines.append("  appear on sys.path:")
    lines.append("")
    lines.append("    cd /path/to/hermes-agent")
    lines.append("    pip install -e .")
    lines.append("")
    lines.append("  Then restart the WebUI.")
    lines.append("")
    lines.append('  Full troubleshooting: docs/troubleshooting.md ("AIAgent not available")')
    return "\n".join(lines)
