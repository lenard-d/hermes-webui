"""Public interface for Hermes WebUI update checks and transactions.

The historical import surface remains here. Repository I/O, release/version
policy, update transactions, and human-readable summaries each have a dedicated
package module. Internal modules use direct relative imports; this interface
keeps the established ``api.updates`` exports for production callers.
"""

# Standard-library module exports are compatibility surfaces for tests and
# integrations that patch process/OS behavior through api.updates.
import hashlib  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import threading
import time
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path
from urllib.parse import urlparse  # noqa: F401

from api.agent_ops import get_active_profile_gateway_running_pid  # noqa: F401
from api.config import REPO_ROOT, STREAMS, STREAMS_LOCK  # noqa: F401

try:
    from api.config import _AGENT_DIR
except ImportError:
    _AGENT_DIR = None
from api.agent_ops import restart_active_profile_gateway  # noqa: F401
from api.profiles import get_active_profile_name  # noqa: F401
from .policy import (  # noqa: F401
    DEFAULT_UPDATE_CHANNEL,
    _CHANNEL_TAG_GLOBS,
    _RELEASE_TAG_RE,
    _can_fast_forward_to,
    _channel_tag_glob,
    _channel_up_to_date_info,
    _check_repo,
    _check_repo_branch,
    _check_repo_release,
    _check_webui_published_release_update,
    _count_channel_tags_ahead,
    _current_release_tag,
    _describe_git_version,
    _detect_agent_version,
    _detect_agent_version_from_gateway_health,
    _detect_webui_version,
    _dirty_suffix,
    _gateway_health_base_url,
    _github_release_tags,
    _head_contains_ref,
    _head_is_past_latest_tag,
    _is_dirty,
    _is_stable_release_tag,
    _normalize_channel,
    _read_agent_source_version,
    _read_update_channel,
    _release_gap,
    _release_tag_sort_key,
    _release_tags,
    _select_apply_compare_ref,
    _version_from_gateway_health_payload,
    channel_version_badge,
)
from .repository import (  # noqa: F401
    _CREDENTIAL_IN_URL_RE,
    _FETCH_NETWORK_FAILURE_SIGNATURES,
    _GITHUB_TOKEN_RE,
    _GIT_DIAGNOSTIC_MAX_CHARS,
    _GIT_LOCK_SIGNATURES,
    _QUERY_SECRET_RE,
    _apply_fetch_failure_message,
    _build_compare_url,
    _detect_default_branch,
    _inventory_locks,
    _is_git_lock_error,
    _normalize_remote_url,
    _resolve_git_executable,
    _run_git,
    _sanitize_git_diagnostic,
    _split_remote_ref,
    _windows_git_from_registry,
)
from .summary import summarize_update_payload as _summarize_update_payload
from .transaction import (  # noqa: F401
    _AGENT_GATEWAY_RESTART_RETRY_DELAY_S,
    _active_stream_count,
    _agent_gateway_restart_failure_message,
    _apply_lock,
    _apply_update_inner,
    _ensure_gateway_restart_for_agent_update,
    _purge_agent_pycache,
    _restart_blocked_response,
    _restart_blocker_snapshot,
    _restore_stash_after_pull_failure,
    _schedule_restart,
    _wait_until_restart_safe,
    apply_clear_lock,
    apply_force_update,
    apply_update,
)

logger = logging.getLogger(__name__)

# Version identity is process-stable and is passed once to the policy owner.
WEBUI_VERSION: str = _detect_webui_version()

from api.config.hooks import install_config_runtime_hooks

install_config_runtime_hooks(webui_version=lambda: WEBUI_VERSION)
AGENT_VERSION: str = _detect_agent_version()

# The policy owner keeps process-stable version identity for no-git release
# checks without consulting this public interface at operation time.
from . import policy as _policy
from . import transaction as _transaction

_policy._RUNNING_WEBUI_VERSION = WEBUI_VERSION


def read_update_channel():
    """Return the configured update channel through the package interface."""
    return _read_update_channel()


_update_cache = {
    'webui': None,
    'agent': None,
    'checked_at': 0,
    'include_agent': True,
    'channel': DEFAULT_UPDATE_CHANNEL,
}
_cache_lock = threading.Lock()
_check_in_progress = False
CACHE_TTL = 1800
_transaction._configure_status_cache(cache=_update_cache, lock=_cache_lock)


def _ignored_agent_update_info() -> dict:
    """Return a stable update-check payload for intentionally ignored Agent updates."""
    return {'name': 'agent', 'behind': 0, 'ignored': True}


def cached_update_status(*, include_agent=True, channel=None):
    """Return cached update status without performing network or git mutations."""
    include_agent = bool(include_agent)
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    with _cache_lock:
        cached = dict(_update_cache)
    # If the cache was populated for a different channel, it is not a valid
    # answer for this channel — signal that so callers don't render stale
    # cross-channel data as authoritative.
    if cached.get('channel') != channel:
        cached['channel'] = channel
        cached['stale_channel'] = True
    if cached.get('include_agent') != include_agent:
        cached['include_agent'] = include_agent
        if not include_agent:
            cached['agent'] = _ignored_agent_update_info()
    cached['cached'] = True
    return cached


def check_for_updates(force=False, *, include_agent=True, channel=None):
    """Return cached update status for webui and agent repos."""
    global _check_in_progress
    include_agent = bool(include_agent)
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    with _cache_lock:
        # Cache is only valid when BOTH the channel AND include_agent match —
        # a channel switch must not serve the previous channel's answer, and an
        # in-progress check for the other channel must not short-circuit this one
        # with a stale cross-channel payload (Codex SILENT #5).
        cache_matches = (
            _update_cache.get('include_agent') == include_agent
            and _update_cache.get('channel') == channel
        )
        if (
            not force
            and cache_matches
            and time.time() - _update_cache['checked_at'] < CACHE_TTL
        ):
            return dict(_update_cache)
        if _check_in_progress and cache_matches:
            # Another thread is already checking this channel.
            return dict(_update_cache)
        _check_in_progress = True

    try:
        # Run checks outside the lock (network I/O)
        webui_info = _check_repo(REPO_ROOT, 'webui', channel)
        # The update channel is a WebUI-only concept. The Agent is a separate
        # project that tags plain v* and legitimately tracks master past its
        # tags; it must ALWAYS use the default channel regardless of the user's
        # WebUI channel selection. (Codex gate: passing 'experimental' here made
        # the Agent ignore its v* tags and fall back to origin/master.)
        agent_info = (
            _check_repo(_AGENT_DIR, 'agent', DEFAULT_UPDATE_CHANNEL)
            if include_agent
            else _ignored_agent_update_info()
        )

        with _cache_lock:
            _update_cache['webui'] = webui_info
            _update_cache['agent'] = agent_info
            _update_cache['checked_at'] = time.time()
            _update_cache['include_agent'] = include_agent
            _update_cache['channel'] = channel
            return dict(_update_cache)
    finally:
        _check_in_progress = False


def _repo_path_for_update_target(target: str):
    if target == 'webui':
        return REPO_ROOT
    if target == 'agent':
        return _AGENT_DIR
    return None


def _commit_subjects_for_update(info: dict, *, limit: int = 24) -> list[str]:
    """Return commit subjects for an update range, if the local git refs exist."""
    subjects, _truncated = _commit_subjects_for_update_with_limit(info, limit=limit)
    return subjects


def _commit_subjects_for_update_with_limit(
    info: dict, *, limit: int = 24
) -> tuple[list[str], bool]:
    """Return recent commit subjects plus whether the local list was capped."""
    if not isinstance(info, dict):
        return [], False
    target = info.get('name')
    if target not in ('webui', 'agent'):
        target = (
            'webui' if info.get('repo_url', '').endswith('hermes-webui') else target
        )
    path = _repo_path_for_update_target(target)
    if path is None or not (Path(path) / '.git').exists():
        return [], False
    current = str(info.get('current_sha') or '').strip()
    latest = str(info.get('latest_sha') or '').strip()
    if not (current and latest):
        return [], False
    probe_limit = max(1, int(limit)) + 1
    out, ok = _run_git(
        ['log', '--format=%s', f'{current}..{latest}', f'-n{probe_limit}'],
        path,
        timeout=5,
    )
    if not ok or not out:
        return [], False
    subjects = [line.strip() for line in out.splitlines() if line.strip()]
    truncated = len(subjects) > limit
    return subjects[:limit], truncated


def summarize_update_payload(
    updates: dict,
    llm_callback=None,
    *,
    target: str | None = None,
    use_cache: bool = True,
) -> dict:
    """Build a human-readable What's New summary without mutating repositories.

    Git-range discovery remains here with the update checker; presentation,
    prompt construction, normalization, and caching belong to
    :mod:`api.updates.summary`.
    """
    return _summarize_update_payload(
        updates,
        _commit_subjects_for_update_with_limit,
        llm_callback,
        target=target,
        use_cache=use_cache,
    )
