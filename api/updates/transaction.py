"""Atomic update application, rollback recovery, and restart ownership."""

import logging
import shutil
import threading
import time
from pathlib import Path

from api.agent_ops import (
    get_active_profile_gateway_running_pid as _default_gateway_pid,
)
from api.config import REPO_ROOT as _DEFAULT_REPO_ROOT, STREAMS, STREAMS_LOCK

try:
    from api.config import _AGENT_DIR as _DEFAULT_AGENT_DIR
except ImportError:
    _DEFAULT_AGENT_DIR = None
from api.agent_ops import restart_active_profile_gateway as _default_restart_gateway
from api.profiles import get_active_profile_name as _default_active_profile_name
from .policy import (
    DEFAULT_UPDATE_CHANNEL,
    _can_fast_forward_to,
    _head_contains_ref,
    _normalize_channel,
    _read_update_channel,
    _select_apply_compare_ref,
)
from . import repository as _repository
from .repository import (
    _apply_fetch_failure_message,
    _inventory_locks,
    _is_git_lock_error,
    _split_remote_ref,
)

logger = logging.getLogger(__name__)
_AGENT_GATEWAY_RESTART_RETRY_DELAY_S = 1.0
REPO_ROOT = _DEFAULT_REPO_ROOT
_AGENT_DIR = _DEFAULT_AGENT_DIR
_apply_lock = threading.Lock()
_status_cache_lock = threading.Lock()
_status_cache = {'checked_at': 0}


def _configure_status_cache(*, cache: dict, lock) -> None:
    """Use the status owner's cache for post-transaction invalidation."""
    global _status_cache, _status_cache_lock
    _status_cache = cache
    _status_cache_lock = lock


def _git(args, cwd, timeout=10):
    return _repository._run_git(args, cwd, timeout=timeout)


def _repo_root() -> Path:
    return REPO_ROOT


def _agent_dir():
    return _AGENT_DIR


def _apply_lock_current():
    return _apply_lock


def _cache_lock_current():
    return _status_cache_lock


def _update_cache_current():
    return _status_cache


def _inventory_locks_current(path):
    return _inventory_locks(path)


def _restart_snapshot_current():
    return _restart_blocker_snapshot()


def _read_channel():
    return _read_update_channel()


def _select_compare_ref(path, channel, target):
    return _select_apply_compare_ref(path, channel, target)


def _head_contains_ref_current(path, ref):
    return _head_contains_ref(path, ref)


def _can_fast_forward_current(path, ref):
    return _can_fast_forward_to(path, ref)


def _schedule_current():
    return _schedule_restart()


def _apply_inner(target, channel):
    return _apply_update_inner(target, channel)


def _purge_current(path):
    return _purge_agent_pycache(path)


def _wait_current():
    return _wait_until_restart_safe()


def _active_profile_name():
    return _default_active_profile_name()


def _gateway_pid(*, profile):
    return _default_gateway_pid(profile=profile)


def _restart_gateway(*, profile):
    return _default_restart_gateway(profile=profile)


def _restart_blocker_snapshot() -> dict:
    """Return active chat work that should block a self-restart."""
    with STREAMS_LOCK:
        stream_ids = [str(k) for k in STREAMS.keys()]
    run_ids: list[str] = []
    try:
        from api import config as _config

        active_runs = getattr(_config, 'ACTIVE_RUNS', {})
        active_runs_lock = getattr(_config, 'ACTIVE_RUNS_LOCK', None)
        if active_runs_lock is not None:
            with active_runs_lock:
                run_ids = [str(k) for k in active_runs.keys()]
        else:
            run_ids = [str(k) for k in active_runs.keys()]
    except Exception:
        run_ids = []
    return {
        'active_streams': len(stream_ids),
        'active_runs': len(run_ids),
        'blocking_stream_ids': stream_ids[:10],
        'blocking_run_ids': run_ids[:10],
        'restart_blocked': bool(stream_ids or run_ids),
    }


def _active_stream_count() -> int:
    """Return the current in-memory chat stream count.

    Kept for compatibility with older tests/helpers; restart safety should use
    ``_restart_blocker_snapshot()`` so detached worker runs also block updates.
    """
    return int(_restart_snapshot_current().get('active_streams') or 0)


def _restart_blocked_response(target: str, blocker_snapshot: dict | int) -> dict:
    if isinstance(blocker_snapshot, int):
        blocker_snapshot = {
            'active_streams': blocker_snapshot,
            'active_runs': 0,
            'blocking_stream_ids': [],
            'blocking_run_ids': [],
            'restart_blocked': bool(blocker_snapshot),
        }
    active_streams = int(blocker_snapshot.get('active_streams') or 0)
    active_runs = int(blocker_snapshot.get('active_runs') or 0)
    parts = []
    if active_streams:
        parts.append(
            f"{active_streams} active chat stream{'s' if active_streams != 1 else ''}"
        )
    if active_runs:
        parts.append(f"{active_runs} active agent run{'s' if active_runs != 1 else ''}")
    detail = ' and '.join(parts) or 'active chat work'
    return {
        'ok': False,
        'message': (
            f'Cannot update {target} while {detail} is running. '
            'Wait for the response to finish, then retry the update.'
        ),
        'target': target,
        'restart_blocked': True,
        'active_streams': active_streams,
        'active_runs': active_runs,
        'blocking_stream_ids': blocker_snapshot.get('blocking_stream_ids') or [],
        'blocking_run_ids': blocker_snapshot.get('blocking_run_ids') or [],
    }


def _wait_until_restart_safe(
    poll_seconds: float = 2.0, max_wait_seconds: float = 300.0
) -> dict:
    """Wait for active work to finish before self-reexec.

    Bounded by ``max_wait_seconds`` so a long-running (or stuck/orphaned) agent
    run can't soft-jam the self-update indefinitely. If the deadline is reached
    while work is still in flight, the snapshot is returned with
    ``wait_timed_out=True`` so the caller can proceed with the re-exec anyway
    (preserving the pre-#3105 "execv preempts in-flight work" fallback) rather
    than holding ``_apply_lock`` for the run's full lifetime.
    """
    snapshot = _restart_snapshot_current()
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    while snapshot.get('restart_blocked'):
        if time.monotonic() >= deadline:
            logger.warning(
                "restart-safety wait exceeded %.0fs with work still in flight (%s); "
                "proceeding with re-exec anyway",
                max_wait_seconds,
                snapshot,
            )
            snapshot = dict(snapshot)
            snapshot['wait_timed_out'] = True
            return snapshot
        time.sleep(max(0.1, poll_seconds))
        snapshot = _restart_snapshot_current()
    return snapshot


def apply_clear_lock(target: str) -> dict:
    """Manual-instruction lock recovery for ``target``.

    v2.2: NEVER removes a lock file. Strategy:

      - If ``.git/index.lock`` is absent: re-run the normal non-destructive
        apply path so the user lands on the latest version without ever
        touching destructive git operations.
      - If ``.git/index.lock`` is present: do NOT touch it -- the server
        has no reliable proof that no live git process is still using
        it (round-2 cert showed `fcntl.flock` does not detect git's
        actual ``O_CREAT|O_EXCL`` locking). Return a response with the
        exact manual command the operator can run, plus the inventory of
        any other lock files so they can investigate. The frontend then
        surfaces a copyable ``rm`` line and a "I've removed the lock --
        try update again" button that re-invokes this endpoint, which
        (now that the lock is gone) will take the success branch and
        re-run the normal apply.
    """
    blocker_snapshot = _restart_snapshot_current()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(
            target,
            blocker_snapshot,
        )

    apply_lock = _apply_lock_current()
    if not apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}

    try:
        if target == 'webui':
            path = _repo_root()
        elif target == 'agent':
            path = _agent_dir()
        else:
            return {'ok': False, 'message': f'Unknown target: {target}'}

        if path is None or not (path / '.git').exists():
            return {'ok': False, 'message': 'Not a git repository'}

        inv = _inventory_locks_current(path)
        manual_command = f"rm -f {inv['well_known_lock_path']}"

        if not inv['well_known_lock_present']:
            # Lock is gone. Run the normal non-destructive update flow and
            # annotate the response with what we found for the user's
            # records. Pass the configured channel through — otherwise an
            # experimental-channel WebUI lock-recovery retry silently falls back
            # to stable (Codex gate: _apply_update_inner defaults to stable).
            with _cache_lock_current():
                _update_cache_current()['checked_at'] = 0
            retry_result = _apply_inner(target, _read_channel())
            retry_result = dict(retry_result)
            retry_result['lock_recovery'] = {
                'action': 'no-lock-found',
                'manual_command': manual_command,
                'other_locks': inv['other_locks'],
            }
            return retry_result

        # Lock is present. The server cannot prove it's safe to delete;
        # the only safe path is to ask the operator.
        message = (
            'A git lock file (.git/index.lock) is present. The server does '
            'not delete locks automatically -- git uses O_CREAT|O_EXCL '
            'locking, which cannot be detected with advisory probes. To '
            'recover: confirm no other git process is running against '
            f'this checkout, then run: {manual_command}  '
            'Click "Retry update" once you have removed it.'
        )
        return {
            'ok': False,
            'message': message,
            'lock_held': True,
            'target': target,
            'manual_command': manual_command,
            'well_known_lock_path': inv['well_known_lock_path'],
            'other_locks': inv['other_locks'],
        }
    finally:
        apply_lock.release()


def _purge_agent_pycache(repo_dir: Path) -> None:
    """Delete all __pycache__ dirs under *repo_dir* so the next import
    recompiles from source, avoiding stale-bytecode errors after git pull.

    ``os.execv()`` replaces the process image but does not touch the
    on-disk bytecode cache.  When a ``git pull`` writes new ``.py`` files
    whose mtime lands within the same second as the pre-existing ``.pyc``
    files, CPython may trust the stale cache and serve an old class
    definition.  The mismatch between cached class symbols and newly-imported
    supporting modules causes ``AttributeError`` (e.g. a method added in
    the same update is missing from the cached ``AIAgent`` class).

    This is safe to call right before ``os.execv()`` because the current
    process is about to be replaced — losing the bytecode cache is harmless
    and forces a clean recompilation on the next startup.
    """
    if repo_dir is None or not repo_dir.exists():
        return
    try:
        for pycache in repo_dir.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def _schedule_restart(delay: float = 2.0) -> None:
    """Re-exec this process after *delay* seconds.

    Called after a successful update so that the freshly-pulled code is
    loaded on the next request, rather than running with a mix of old and
    new Python modules in sys.modules.

    os.execv() replaces the current process image with a fresh interpreter
    running the same argv — sessions are preserved on disk, the HTTP port
    is reclaimed within the delay window, and the client's own
    ``setTimeout(() => location.reload(), 2500)`` lands after the restart.

    Coordinates with ``_apply_lock``: when the user updates both webui
    and agent, the client POSTs them sequentially.  Without coordination
    the restart timer scheduled by the first update's success would fire
    while the second update's git-pull is still running, killing it mid-
    stream and leaving the second repo in an unknown partial state.
    Blocking on ``_apply_lock`` before ``os.execv`` means a pending
    second update always completes before the restart happens.
    """
    import os
    import sys

    def _do():
        import time

        time.sleep(delay)
        # Hold _apply_lock through os.execv so no new update can start between
        # the lock-release and the process replacement.  Any in-flight update
        # finishes first (since it holds the lock), and then the process is
        # replaced while still holding the lock — meaning no new update can
        # sneak in during the brief TOCTOU window that existed with the
        # original acquire-release-execv sequence.
        # Threads die when execv replaces the process image, so the lock is
        # released atomically by the kernel.
        with _apply_lock_current():
            _wait_current()
            # Purge bytecode caches so the new process imports from
            # current source.  Without this, Python may serve stale .pyc
            # files whose mtime matches the just-pulled .py files,
            # causing AttributeError when new methods are missing from
            # cached class definitions.
            if _agent_dir() is not None:
                _purge_current(Path(_agent_dir()))
            _purge_current(_repo_root())
            try:
                # Re-exec into the just-pulled image.
                #
                # sys.argv[0]'s meaning depends on how the server was launched:
                #
                #   * Source checkout (`python server.py` via bootstrap.py /
                #     ctl.sh / start.sh): sys.argv[0] is the SCRIPT path
                #     (e.g. "/root/hermes-webui/server.py"), sys.executable is
                #     the interpreter. CPython treats argv[1] as the script to
                #     run, so we must pass [sys.executable] + sys.argv.
                #
                #   * Frozen/packaged build (PyInstaller, embedded zipapp,
                #     etc.): sys.argv[0] == sys.executable == <binary>. Passing
                #     [sys.executable] + sys.argv would re-insert the binary as
                #     argv[1] — the kernel launches it, the interpreter treats
                #     the binary itself as the "script" to run, and execv
                #     effectively becomes a recursive no-op that never reaches
                #     bind(), leaving the WebUI stuck "offline" after every
                #     self-update. Pass argv as-is instead.
                #
                # Distinguish the two cases with sys.frozen (set by
                # PyInstaller / zipapp / similar). For source checkouts the
                # `[sys.executable] + sys.argv` form is the canonical CPython
                # re-exec idiom (same shape Flask/Django reloaders use) and
                # is the correct path.
                #
                # IMPORTANT: On Windows, os.execv() does NOT replace the
                # current process — it spawns a new process while the old
                # one keeps running.  This causes "address already in use"
                # because the old process still holds the port.  On Windows
                # we use subprocess.Popen() + os._exit() instead.
                if sys.platform == 'win32':
                    import subprocess

                    if getattr(sys, "frozen", False):
                        args = sys.argv
                    else:
                        args = [sys.executable] + sys.argv
                    # Prefer pythonw.exe over python.exe so the restarted
                    # server does not create a visible console window.
                    # sys.executable may point at python.exe (console
                    # subsystem); substitute pythonw.exe if it exists
                    # next to python.exe.
                    _exe = sys.executable
                    if _exe.lower().endswith('python.exe'):
                        _w_exe = _exe[:-4] + 'w.exe'  # python.exe -> pythonw.exe
                        if os.path.isfile(_w_exe):
                            if getattr(sys, "frozen", False):
                                args = sys.argv
                            else:
                                args = [_w_exe] + sys.argv
                    # Start new process fully detached with NO console
                    # window.  DETACHED_PROCESS alone is not sufficient
                    # on modern Windows — without CREATE_NO_WINDOW a
                    # python.exe (console-subsystem) child still flashes
                    # an empty terminal window, which the user then
                    # manually kills (taking the WebUI with it).
                    subprocess.Popen(
                        args,
                        cwd=os.getcwd(),
                        creationflags=(
                            subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.CREATE_NO_WINDOW
                        ),
                        close_fds=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    # Exit immediately — the port is released as soon as
                    # this process dies, allowing the new process to bind.
                    os._exit(0)
                else:
                    if getattr(sys, "frozen", False):
                        os.execv(sys.executable, sys.argv)
                    else:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                # Last-resort: if execv fails for any reason, just exit so the
                # process supervisor (start.sh / Docker) restarts us.
                os._exit(0)

    # The worker receives the concrete callback directly; it does not resolve
    # an update interface after the thread starts.
    threading.Thread(
        target=_do,
        daemon=True,
    ).start()


def _ensure_gateway_restart_for_agent_update() -> tuple[bool, dict]:
    """Run the active-profile gateway restart when agent checkout changed.

    Returns:
        (ok, restart_payload) where:
        - ok is False when restart did not complete and callers must abort success.
        - restart_payload contains helper status fields for response shaping.
    """
    target_profile = str(_active_profile_name() or "default").strip() or "default"
    gateway_pid_before_restart = _gateway_pid(profile=target_profile)
    restart_result = _restart_gateway(profile=target_profile)
    status = str(restart_result.get("status") or "")
    if status in {"completed", "in_progress"}:
        return True, restart_result
    if status != "failed":
        return False, restart_result

    # launchd can briefly fail to spawn the replacement gateway while it is
    # rotating the supervised process (#6045). Retry exactly once after a
    # bounded delay so an already-applied Agent update is not reported as a
    # complete failure because of that transient process handoff.
    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    retry_result = _restart_gateway(profile=target_profile)
    retry_status = str(retry_result.get("status") or "")
    if retry_status in {"completed", "in_progress"}:
        return True, {
            **retry_result,
            "retry_attempted": True,
            "initial_failure": restart_result.get("message"),
        }
    if retry_status != "failed":
        return False, {
            **retry_result,
            "retry_attempted": True,
            "initial_failure": restart_result.get("message"),
        }

    # A restart command can still exit non-zero after launchd has recovered the
    # service. Only accept that recovery when the confirmed local PID changed;
    # a merely-alive old gateway has not loaded the updated Agent checkout.
    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    gateway_pid_after_retry = _gateway_pid(profile=target_profile)
    if (
        gateway_pid_before_restart is not None
        and gateway_pid_after_retry is not None
        and gateway_pid_after_retry != gateway_pid_before_restart
    ):
        return True, {
            "status": "completed",
            "message": "Gateway service recovered after a transient restart failure",
            "retry_attempted": True,
            "process_replaced": True,
            "initial_failure": restart_result.get("message"),
            "retry_failure": retry_result.get("message"),
        }

    initial_message = str(restart_result.get("message") or "Restart failed")
    retry_message = str(retry_result.get("message") or "retry did not complete")
    return False, {
        **retry_result,
        "message": f"{initial_message}; recovery retry did not complete: {retry_message}",
        "retry_attempted": True,
        "initial_failure": restart_result.get("message"),
    }


def _agent_gateway_restart_failure_message(target: str, restart_result: dict) -> str:
    if restart_result.get("message"):
        return (
            f'{target} updated, but gateway restart did not complete: '
            f'{restart_result["message"]}. Run `hermes gateway restart` manually.'
        )
    return (
        f'{target} updated, but gateway restart did not complete. '
        'Run `hermes gateway restart` manually.'
    )


def apply_force_update(target: str, channel=None) -> dict:
    """Force-reset the target repo to the latest remote HEAD.

    Unlike apply_update() which requires a clean working tree and refuses
    merge conflicts, this discards all local modifications (checkout .) and
    resets to origin/<branch> — equivalent to what the diverged/conflict
    error messages ask the user to run manually.

    Should only be called when apply_update() has already returned a
    response with ``conflict: True`` or ``diverged: True`` and the user
    has confirmed they want to discard local changes.

    CHANNEL SAFETY (rewind guard): ``reset --hard`` is destructive. When the
    selected channel resolves to a ref that is an ANCESTOR of HEAD (i.e. the
    checkout is already ahead of the channel — e.g. an ex-experimental install
    switching back to stable), resetting to it would REWIND code and on-disk
    state. We refuse and return a clear message instead of silently downgrading.
    A deliberate rollback would be a separate, explicit feature.
    """
    if channel is None:
        channel = _read_channel()
    channel = _normalize_channel(channel)
    blocker_snapshot = _restart_snapshot_current()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(
            target,
            blocker_snapshot,
        )

    apply_lock = _apply_lock_current()
    if not apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}
    try:
        if target == 'webui':
            path = _repo_root()
        elif target == 'agent':
            path = _agent_dir()
            # Channel is WebUI-only — the Agent always uses the default channel.
            channel = DEFAULT_UPDATE_CHANNEL
        else:
            return {'ok': False, 'message': f'Unknown target: {target}'}

        if path is None or not (path / '.git').exists():
            return {'ok': False, 'message': 'Not a git repository'}

        # NOTE: v2 of PR #5688 removed the prior stale-lock cleanup loop from
        # this entry point. The mtime-based heuristic was empirically proven
        # unsafe (a live `git add` was shown to hold .git/index.lock past 31 s
        # with unchanged mtime) and unconditional pre-cleanup clobbered locks
        # for force-update retries that had nothing to do with a lock error.
        # Lock cleanup is now ONLY performed by the explicit
        # /api/updates/clear_lock endpoint, where the user has opted in to
        # a non-destructive retry.

        # --force so a remote re-tag (e.g. squash-merge that re-points an
        # existing release tag) doesn't jam the apply path with "would clobber
        # existing tag". See #2756.
        fetch_out, fetch_ok = _git(
            ['fetch', 'origin', '--quiet', '--tags', '--force'], path, timeout=15
        )
        if not fetch_ok:
            return {
                'ok': False,
                'message': _apply_fetch_failure_message(
                    fetch_out,
                    'Could not reach the remote repository. Check your connection.',
                ),
            }

        compare_ref = _select_compare_ref(path, channel, target)
        # Stable channel, already up to date on the promoted subset: nothing to
        # force to. Do NOT fall back to origin/master (firehose). See
        # _select_apply_compare_ref channel semantics.
        if compare_ref is None:
            return {
                'ok': True,
                'message': f'{target} is already up to date on the {channel} channel.',
                'target': target,
                'up_to_date': True,
                'channel': channel,
            }

        # Rewind guard (Codex CORE #3): refuse to reset --hard onto a ref that
        # is an ANCESTOR of HEAD — that would downgrade the checkout. This is the
        # switch-back-to-stable-while-ahead case. A ref that is a descendant of
        # HEAD (normal update / opt-in to experimental) fast-forwards fine and is
        # allowed. Refs on a divergent line (neither ancestor nor descendant) are
        # the legitimate force-update case (conflict/diverged recovery) and are
        # also allowed — the guard fires ONLY on a pure-ancestor rewind.
        if _head_contains_ref_current(
            path, compare_ref
        ) and not _can_fast_forward_current(path, compare_ref):
            return {
                'ok': False,
                'message': (
                    f'{target} is already ahead of the {channel} channel '
                    f'({compare_ref}); refusing to rewind the checkout. '
                    'Switching to a slower channel keeps your current version '
                    'until that channel catches up.'
                ),
                'target': target,
                'channel': channel,
                'refused_rewind': True,
            }
        # Discard local modifications and untracked colliders before resetting.
        # Do not use -x: ignored build/cache artifacts should survive force update.
        _git(['checkout', '.'], path)
        # Best-effort clean: a `git clean -fd` failure is NOT fatal. The
        # following `reset --hard` overwrites any tracked-file collisions
        # regardless, and residual untracked files that git can't delete are
        # harmless. In particular, on Windows a file named after a reserved
        # device name (nul, con, prn, aux, com1-9, lpt1-9) — which can appear
        # in the working tree when a shell command redirects to `> nul` under
        # Git Bash — cannot be removed via the normal Win32 path that git uses,
        # so `clean` exits non-zero. Aborting the whole force update over that
        # left users stuck (issue #4914). Log the stderr for diagnostics and
        # proceed to the reset, which is what actually applies the update.
        clean_out, clean_ok = _git(['clean', '-fd'], path)
        if not clean_ok:
            logger.warning(
                'force_apply_update: `git clean -fd` failed (non-fatal, '
                'continuing to reset --hard): %s',
                clean_out,
            )
        _, ok = _git(['reset', '--hard', compare_ref], path)
        if not ok:
            return {'ok': False, 'message': f'Force reset to {compare_ref} failed'}

        with _cache_lock_current():
            _update_cache_current()['checked_at'] = 0

        if target == 'agent':
            gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
            if not gateway_ok:
                return {
                    'ok': False,
                    'message': _agent_gateway_restart_failure_message(
                        target,
                        gateway_result,
                    ),
                    'target': target,
                    'gateway_restart': gateway_result.get('status'),
                }

        _schedule_current()

        response = {
            'ok': True,
            'message': f'{target} force-updated to {compare_ref}',
            'target': target,
            'restart_scheduled': True,
        }
        if target == 'agent':
            response['gateway_restart'] = gateway_result.get('status')
        return response
    finally:
        apply_lock.release()


def apply_update(target, channel=None):
    """Stash, pull --ff-only, pop for the given target repo."""
    if channel is None:
        channel = _read_channel()
    channel = _normalize_channel(channel)
    blocker_snapshot = _restart_snapshot_current()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(
            target,
            blocker_snapshot,
        )

    apply_lock = _apply_lock_current()
    if not apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}
    try:
        return _apply_inner(target, channel)
    finally:
        apply_lock.release()


def _restore_stash_after_pull_failure(
    target: str,
    path: Path,
    pull_out: str,
) -> str:
    """Best-effort re-apply of a stash pushed earlier in `_apply_update_inner`.

    Called when `git pull` failed with a lock error and we had already pushed
    a stash for the user's local modifications. Without this, the user's
    modifications remain in git stash with the working tree clean -- the
    wrong user experience because the failure was a lock conflict, not a
    stash-apply conflict, and the stash should re-apply cleanly.

    Returns a human-readable note for inclusion in the response message.
    """
    _, pop_ok = _git(['stash', 'pop'], path)
    if pop_ok:
        return 'Local modifications were restored from the temporary stash.'

    # `git stash pop` failed -- could be that the working tree changed under
    # us. Try apply + drop to keep the change separation explicit.
    _, apply_ok = _git(['stash', 'apply'], path)
    if apply_ok:
        _, _ = _git(['stash', 'drop'], path)
        return 'Local modifications were restored from the temporary stash.'

    detail = (pull_out or '').strip()[:200]
    return (
        'Your local modifications could not be restored automatically '
        f'(stash pop failed after pull error: {detail or "no detail"}). '
        'They remain safely in `git stash list`; run `git -C '
        + str(path)
        + ' stash pop` once the lock is cleared.'
    )


def _apply_update_inner(target, channel=DEFAULT_UPDATE_CHANNEL):
    """Inner implementation of apply_update, called under _apply_lock."""
    channel = _normalize_channel(channel)
    if target == 'webui':
        path = _repo_root()
    elif target == 'agent':
        path = _agent_dir()
        # Channel is WebUI-only — the Agent always uses the default channel
        # regardless of the user's WebUI selection (see check_for_updates).
        channel = DEFAULT_UPDATE_CHANNEL
    else:
        return {'ok': False, 'message': f'Unknown target: {target}'}

    if path is None or not (path / '.git').exists():
        return {'ok': False, 'message': 'Not a git repository'}

    # Fetch before attempting pull, so the remote ref is current.
    # --force so a remote re-tag doesn't block the update path (see #2756).
    fetch_out, fetch_ok = _git(
        ['fetch', 'origin', '--quiet', '--tags', '--force'], path, timeout=15
    )
    if not fetch_ok:
        if _is_git_lock_error(fetch_out):
            return {
                'ok': False,
                'message': f'Fetch failed due to a repository lock: {fetch_out.strip()}',
                'lock_conflict': True,
            }
        return {
            'ok': False,
            'message': _apply_fetch_failure_message(
                fetch_out,
                'Could not reach the remote repository. Check your internet connection and try again.',
            ),
        }

    compare_ref = _select_compare_ref(path, channel, target)
    # On the stable channel a None ref means HEAD already contains the latest
    # promoted stable tag (up-to-date on the promoted subset). Do NOT fall back
    # to origin/master — that would advance the user onto the experimental
    # firehose. Report success/no-op instead. See _select_apply_compare_ref.
    if compare_ref is None:
        return {
            'ok': True,
            'message': f'{target} is already up to date on the {channel} channel.',
            'target': target,
            'up_to_date': True,
            'channel': channel,
        }

    # Check for dirty working tree (ignore untracked files — git stash
    # doesn't include them, so stashing on '??' alone leaves nothing to pop)
    status_out, status_ok = _git(
        ['status', '--porcelain', '--untracked-files=no'], path
    )
    if not status_ok:
        if _is_git_lock_error(status_out):
            return {
                'ok': False,
                'message': f'Failed to inspect repo status due to a repository lock: {status_out.strip()}',
                'lock_conflict': True,
            }
        return {
            'ok': False,
            'message': f'Failed to inspect repo status: {status_out[:200]}',
        }
    # Fail early on unresolved merge conflicts
    if any(
        line[:2] in {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}
        for line in status_out.splitlines()
    ):
        return {
            'ok': False,
            'message': (
                f'The local {target} repo has unresolved merge conflicts. '
                'To reset to the latest remote version run: '
                'git -C ' + str(path) + ' checkout . && '
                'git -C ' + str(path) + ' pull --ff-only'
            ),
            'conflict': True,
        }
    stashed = False
    if status_out:
        _, ok = _git(['stash', 'push', '-m', 'hermes-update-autostash'], path)
        if not ok:
            return {'ok': False, 'message': 'Failed to stash local changes'}
        stashed = True

    # Pull with ff-only (no merge commits).
    # Split tracking refs like 'origin/main' into separate remote + branch
    # arguments — git treats 'origin/main' as a repository name otherwise.
    remote, branch = _split_remote_ref(compare_ref)
    pull_args = ['pull', '--ff-only']
    if remote:
        pull_args.extend([remote, branch])
    else:
        pull_args.extend(['origin', compare_ref])
    pull_out, pull_ok = _git(pull_args, path, timeout=30)
    if not pull_ok:
        if _is_git_lock_error(pull_out):
            # Lock conflict during pull. If a stash was pushed for the local
            # modifications, attempt to restore it before returning so the
            # user's working tree is not silently left empty with changes
            # stranded in the stash (Greptile P1 on PR #5688).
            stash_recovery_note = ''
            if stashed:
                stash_recovery_note = _restore_stash_after_pull_failure(
                    target,
                    path,
                    pull_out,
                )
            message = f'Pull failed due to a repository lock: {pull_out.strip()}'
            if stash_recovery_note:
                message = f'{message} {stash_recovery_note}'
            return {
                'ok': False,
                'message': message,
                'lock_conflict': True,
            }
        pull_lower = pull_out.lower()
        detail = pull_out.strip()[:300] if pull_out.strip() else '(no output from git)'
        untracked_collision = (
            'untracked working tree files would be overwritten' in pull_lower
        )
        diverged_failure = (
            'not possible to fast-forward' in pull_lower or 'diverged' in pull_lower
        )
        restored_stash = False
        stash_drop_failed = False
        if stashed:
            _, apply_ok = _git(['stash', 'apply'], path)
            if apply_ok:
                _, drop_ok = _git(['stash', 'drop'], path)
                restored_stash = True
                stash_drop_failed = not drop_ok
            else:
                _, reset_ok = _git(['reset', '--hard', 'HEAD'], path)
                if not reset_ok:
                    response = {
                        'ok': False,
                        'message': (
                            'Pull failed, and failed to clean up a stash-apply '
                            'conflict while restoring local changes. Manual '
                            'intervention needed: run git -C ' + str(path) + ' '
                            'reset --hard HEAD to remove conflict markers. Your '
                            'changes remain in the git stash. Pull error: ' + detail
                        ),
                        'stash_conflict': True,
                    }
                    if diverged_failure:
                        response['diverged'] = True
                    return response
                response = {
                    'ok': False,
                    'message': (
                        f'Pull failed, and your local {target} modifications '
                        'conflicted while restoring from stash. The index and '
                        'tracked files were restored to HEAD, and your changes '
                        'remain in the git stash. To inspect: git -C '
                        + str(path)
                        + ' stash show -p. '
                        'To re-apply: git -C ' + str(path) + ' stash apply, then '
                        'resolve conflicts. Pull error: ' + detail
                    ),
                    'stash_conflict': True,
                }
                if diverged_failure:
                    response['diverged'] = True
                return response

        restored_note_parts = []
        if restored_stash:
            restored_note_parts.append(
                f'Local {target} modifications were restored to the working '
                'tree; save or stash them before running destructive recovery '
                'commands.'
            )
            if stash_drop_failed:
                restored_note_parts.append(
                    'The temporary stash entry may still be present because '
                    'git stash drop failed.'
                )
        restored_note = ' '.join(restored_note_parts)

        # Diagnose the most common failure modes and surface actionable messages.
        if diverged_failure:
            message_parts = [
                f'The local {target} repo has commits that are not on the remote '
                'branch, so a fast-forward update is not possible.'
            ]
            if restored_note:
                message_parts.append(restored_note)
            message_parts.append(
                'Run: git -C ' + str(path) + ' fetch origin && '
                'git -C ' + str(path) + ' reset --hard ' + compare_ref
            )
            return {
                'ok': False,
                'message': ' '.join(message_parts),
                'diverged': True,
            }
        if 'does not track' in pull_lower or 'no tracking information' in pull_lower:
            message_parts = [
                f'The local {target} branch has no upstream tracking branch configured.'
            ]
            if restored_note:
                message_parts.append(restored_note)
            message_parts.append(
                'Run: git -C ' + str(path) + ' branch --set-upstream-to=' + compare_ref
            )
            return {
                'ok': False,
                'message': ' '.join(message_parts),
            }
        # Generic fallback — include the raw git output for debugging.
        message_parts = [f'Pull failed: {detail}']
        if restored_note:
            message_parts.append(restored_note)
        response = {'ok': False, 'message': ' '.join(message_parts)}
        if untracked_collision:
            response['conflict'] = True
        return response

    # Re-apply stash if we stashed.
    stash_drop_failed = False
    if stashed:
        _, apply_ok = _git(['stash', 'apply'], path)
        if apply_ok:
            _, drop_ok = _git(['stash', 'drop'], path)
            stash_drop_failed = not drop_ok
        else:
            _, reset_ok = _git(['reset', '--hard', 'HEAD'], path)
            if not reset_ok:
                return {
                    'ok': False,
                    'message': (
                        'Updated successfully, but failed to clean up a '
                        'stash-apply conflict. Manual intervention needed: '
                        'run git -C ' + str(path) + ' reset --hard HEAD to '
                        'remove conflict markers. Your changes remain in the '
                        'git stash.'
                    ),
                    'stash_conflict': True,
                }
            with _cache_lock_current():
                _update_cache_current()['checked_at'] = 0

            if target == 'agent':
                gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
                if not gateway_ok:
                    return {
                        'ok': False,
                        'message': _agent_gateway_restart_failure_message(
                            target,
                            gateway_result,
                        ),
                        'target': target,
                        'gateway_restart': gateway_result.get('status'),
                    }
            _schedule_current()
            response = {
                'ok': True,
                'message': (
                    f'{target} updated to the latest version. Your local '
                    'modifications conflicted with upstream changes and were '
                    'set aside in a git stash. To inspect: '
                    'git -C ' + str(path) + ' stash show -p. To re-apply: '
                    'git -C ' + str(path) + ' stash apply, then resolve '
                    'conflicts. Drop the stash after you are satisfied.'
                ),
                'target': target,
                'restart_scheduled': True,
                'stash_conflict': True,
            }
            if target == 'agent':
                response['gateway_restart'] = gateway_result.get('status')
            return response

    # Invalidate cache
    with _cache_lock_current():
        _update_cache_current()['checked_at'] = 0

    if target == 'agent':
        gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
        if not gateway_ok:
            return {
                'ok': False,
                'message': _agent_gateway_restart_failure_message(
                    target,
                    gateway_result,
                ),
                'target': target,
                'gateway_restart': gateway_result.get('status'),
            }

    # Schedule a self-restart so the updated code is loaded fresh.  A plain
    # git pull leaves stale Python modules in sys.modules — agent imports that
    # reference new symbols (functions, classes) added in the update will fail
    # on the next request with AttributeError / ImportError.  os.execv() re-
    # execs the same interpreter with the same argv, picking up the new code
    # cleanly without requiring the user to restart manually.
    #
    # The 2 s delay gives the HTTP response time to flush to the client before
    # the process replaces itself.  The client already does
    # setTimeout(() => location.reload(), 1500) on success, so the page reload
    # and the restart land at roughly the same time.
    _schedule_current()
    message = f'{target} updated successfully'
    if stash_drop_failed:
        message += (
            '. Local modifications were restored, but the temporary stash '
            'entry may still be present because git stash drop failed.'
        )

    response = {
        'ok': True,
        'message': message,
        'target': target,
        'restart_scheduled': True,
    }
    if target == 'agent':
        response['gateway_restart'] = gateway_result.get('status')
    return response
