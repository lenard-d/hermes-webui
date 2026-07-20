"""Per-turn environment and context-local session identity."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def _build_agent_thread_env(profile_runtime_env: dict | None, workspace: str, session_id: str, profile_home: str) -> dict:
    """Build thread-local agent env with per-run values overriding profile defaults.

    Profile runtime env may include TERMINAL_CWD from config.yaml. Passing it as
    **kwargs alongside an explicit TERMINAL_CWD raises TypeError before the
    agent starts, so merge into one dict first and let the active workspace win.
    """
    env = dict(profile_runtime_env or {})
    env.update({
        'TERMINAL_CWD': str(workspace),
        'HERMES_EXEC_ASK': '1',
        'HERMES_SESSION_KEY': session_id,
        'HERMES_SESSION_ID': session_id,
        'HERMES_SESSION_PLATFORM': 'webui',
        # process_complete agent-wakeup wiring (ours-original, Option B): the
        # terminal_tool watcher routing gate (terminal_tool.py:~1940) reads
        # HERMES_SESSION_CHAT_ID to populate pending_watchers for WebUI
        # sessions so notify_on_complete completions enqueue and the agent
        # can be woken. HERMES_SESSION_ID/PLATFORM come from upstream #2279.
        'HERMES_SESSION_CHAT_ID': str(session_id),
        'HERMES_HOME': profile_home,
    })
    return env

def _set_turn_session_identity(session_id: str):
    """Bind THIS turn's session identity to the current (task/thread-local)
    context and return an opaque token for _reset_turn_session_identity.

    Binds three context-locals so every session-key / UI-owner consumer is
    covered without a race:
      * ``tools.approval._approval_session_key`` — checked FIRST by
        ``get_current_session_key`` (the exact call terminal_tool.py makes for
        a notify_on_complete background spawn: the bug path).
      * ``gateway.session_context._SESSION_KEY`` — read by direct
        ``get_session_env("HERMES_SESSION_KEY")`` consumers.
      * ``gateway.session_context._SESSION_UI_SESSION_ID`` — exact browser-tab
        return address stamped onto ProcessSession.origin_ui_session_id and
        completion events by modern hermes-agent builds. Authoritative for
        wakeup routing when present (see ``_resolve_completion_target``).

    It deliberately does NOT call ``gateway.session_context.set_session_vars``:
    that blanket setter also zeroes the platform/chat_id/user contextvars,
    flipping ``HERMES_SESSION_PLATFORM`` from its env fallback (``'webui'``,
    still written to os.environ at turn-start) to an explicit ``""`` — which
    would break the ``notify_on_complete`` watcher registration gate.
    """
    sid = str(session_id or "")
    tokens: dict = {}
    try:
        from tools.approval import set_current_session_key
        tokens["approval"] = set_current_session_key(sid)
    except Exception:
        logger.debug("per-turn approval session-key bind failed", exc_info=True)
    try:
        from gateway.session_context import _SESSION_KEY as _SK
        tokens["session_key"] = _SK.set(sid)
    except Exception:
        logger.debug("per-turn _SESSION_KEY bind failed", exc_info=True)
    try:
        from gateway.session_context import _SESSION_UI_SESSION_ID as _UI_SID
        tokens["ui_session_id"] = _UI_SID.set(sid)
    except Exception:
        logger.debug("per-turn _SESSION_UI_SESSION_ID bind failed", exc_info=True)
    return tokens

def _reset_turn_session_identity(tokens) -> None:
    """Restore the context-locals bound by ``_set_turn_session_identity`` via
    contextvars reset-token semantics.

    Reset-token (not a blanket clear) is the canonical idiom: it composes
    correctly under nesting and restores ``_UNSET`` for the top-level turn so
    a reused thread-pool worker leaks no identity and CLI/cron env fallback
    resumes. Order mirrors the bind in reverse.
    """
    if not tokens:
        return
    tok = tokens.get("ui_session_id")
    if tok is not None:
        try:
            from gateway.session_context import _SESSION_UI_SESSION_ID as _UI_SID
            _UI_SID.reset(tok)
        except Exception:
            logger.debug("per-turn _SESSION_UI_SESSION_ID reset failed", exc_info=True)
    tok = tokens.get("session_key")
    if tok is not None:
        try:
            from gateway.session_context import _SESSION_KEY as _SK
            _SK.reset(tok)
        except Exception:
            logger.debug("per-turn _SESSION_KEY reset failed", exc_info=True)
    tok = tokens.get("approval")
    if tok is not None:
        try:
            from tools.approval import reset_current_session_key
            reset_current_session_key(tok)
        except Exception:
            logger.debug("per-turn approval session-key reset failed", exc_info=True)

def _bind_turn_session_identity(session_id: str):
    """Context-manager form of the per-turn session-identity binding.

    The ``_run_agent_streaming`` worker uses the explicit ``_set``/``_reset``
    pair directly because its single ``try/finally`` already spans the whole
    turn (~2k lines) and the binding must cover every mid-turn background
    spawn; this wrapper is the canonical single-call API for other callers and
    for tests, and shares the exact same code path.
    """
    tokens = _set_turn_session_identity(session_id)
    try:
        yield
    finally:
        _reset_turn_session_identity(tokens)
