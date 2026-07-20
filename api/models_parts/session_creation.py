"""Compression-child lookup and session creation.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

_COMPRESSION_RECOVERY_PROFILE_UNSET = object()


def _compression_recovery_child_matches(
    session,
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
) -> bool:
    if source_profile is not _COMPRESSION_RECOVERY_PROFILE_UNSET:
        try:
            from api.profiles import _profiles_match
        except (ImportError, AttributeError):
            logger.debug("Failed to profile-check compression recovery session", exc_info=True)
            return False
        if not _profiles_match(getattr(session, "profile", None), source_profile):
            return False
    return (
        str(getattr(session, "compression_recovery_source_session_id", "") or "").strip() == source_session_id
        and str(getattr(session, "compression_recovery_action", "") or "").strip() == action
    )


def find_compression_recovery_session(
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
):
    """Return an existing focused recovery child for ``source_session_id``.

    The recovery-start endpoint is a retryable UI action. A persisted marker on
    the child session makes double-clicks, repeated calls, and cache reloads
    converge on the same continuation instead of creating duplicate siblings.
    """

    source_sid = str(source_session_id or "").strip()
    recovery_action = str(action or "").strip()
    if not source_sid or not recovery_action:
        return None

    matches = []
    seen_ids: set[str] = set()
    try:
        with LOCK:
            memory_sessions = list(SESSIONS.values())
        for session in memory_sessions:
            sid = str(getattr(session, "session_id", "") or "").strip()
            if sid:
                seen_ids.add(sid)
            if _compression_recovery_child_matches(session, source_sid, recovery_action, source_profile):
                matches.append(session)
    except Exception:
        logger.debug("Failed to scan cached compression recovery sessions", exc_info=True)

    try:
        persisted_ids = _persisted_session_ids_snapshot()
    except Exception:
        persisted_ids = frozenset()
    for sid in persisted_ids:
        if sid in seen_ids:
            continue
        try:
            meta = Session.load_metadata_only(sid)
        except Exception:
            logger.debug("Failed to inspect compression recovery session %s", sid, exc_info=True)
            continue
        if not meta or not _compression_recovery_child_matches(meta, source_sid, recovery_action, source_profile):
            continue
        try:
            matches.append(get_session(sid))
        except Exception:
            matches.append(meta)

    if not matches:
        return None

    def _sort_key(session):
        try:
            created_at = float(getattr(session, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        try:
            updated_at = float(getattr(session, "updated_at", 0) or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return (created_at, updated_at, str(getattr(session, "session_id", "") or ""))

    return sorted(matches, key=_sort_key)[0]


def _profile_default_model_state(profile=None):
    """Return the default model/provider configured for *profile*."""
    default_model = ""
    default_provider = None
    try:
        from api.profiles import get_hermes_home_for_profile
        config_path = Path(get_hermes_home_for_profile(profile)) / "config.yaml"
        config_data = _cfg._load_yaml_config_file(config_path)
    except Exception:
        config_data = {}

    model_cfg = config_data.get("model", {}) if isinstance(config_data, dict) else {}
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        default_model = str(model_cfg.get("default") or "").strip()
        default_provider = str(model_cfg.get("provider") or "").strip() or None

    return default_model or get_effective_default_model(), default_provider


def new_session(workspace=None, model=None, profile=None, model_provider=None, project_id=None, worktree_info=None, enabled_toolsets=None):
    """Create a new in-memory session.

    The session lives in the SESSIONS dict only — no disk write happens until
    the first message is appended (#1171 follow-up).  This avoids the
    "ghost Untitled session on disk" pile-up that occurred when users clicked
    New Conversation, reloaded the page, or completed onboarding without ever
    sending a message.  Subsequent code paths that populate state immediately
    (btw / background agent at api/routes.py) call ``s.save()`` themselves
    after setting title/messages, and ``_handle_chat_start`` saves the
    session as soon as the user actually sends a message — both are the
    natural first-write moments for a real session.

    Crash-safety: if the process exits between session creation and first
    message, the session is lost.  Since it had no messages, there is
    nothing to lose.  Worktree-backed sessions are the exception: they are
    saved immediately because creating the session also creates real
    filesystem state that must remain discoverable after restart.

    *profile* — when supplied by the caller (e.g. from the request body sent
    by the active browser tab), it is used directly so that concurrent clients
    on different profiles don't fight over a shared process-global.  If not
    supplied, we fall back to the process-level active profile (the pre-#798
    behaviour, preserved for calls that originate outside a request context).
    """
    if profile is None:
        # Fallback: read process-level global (single-client or startup path)
        try:
            from api.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except ImportError:
            profile = None
    if model:
        effective_model = model
        effective_model_provider = model_provider
    else:
        effective_model, effective_model_provider = _profile_default_model_state(profile)
        if model_provider:
            effective_model_provider = model_provider

    wt = worktree_info if isinstance(worktree_info, dict) else None
    workspace_path = (wt.get('path') if wt and wt.get('path') else workspace) if wt else workspace
    s = Session(
        workspace=workspace_path or get_last_workspace(),
        model=effective_model,
        model_provider=effective_model_provider,
        profile=profile,
        project_id=project_id,
        personality=None,
        worktree_path=wt.get('path') if wt else None,
        worktree_branch=wt.get('branch') if wt else None,
        worktree_repo_root=wt.get('repo_root') if wt else None,
        worktree_created_at=wt.get('created_at') if wt else None,
        enabled_toolsets=enabled_toolsets,
    )
    # #4985: defensive — auto-generated uuids don't collide with the
    # tombstone, but if a future caller ever passes an explicit id that
    # was previously pruned, clear the entry so the new session isn't
    # shadowed on the next poll. Wrapped because a tombstone failure
    # must never block new-session creation.
    try:
        _clear_webui_zero_message_orphan_tombstone(s.session_id)
        _clear_webui_deleted_session_tombstone(s.session_id)
    except Exception:
        logger.debug(
            "Failed to clear webui tombstone for %s",
            s.session_id,
            exc_info=True,
        )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
    if wt:
        s.save()
    return s

__all__ = ['_COMPRESSION_RECOVERY_PROFILE_UNSET', '_compression_recovery_child_matches', 'find_compression_recovery_session', '_profile_default_model_state', 'new_session']
