"""Profile-isolated account-usage probe workers, lifecycle, and cache."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from api.providers.account_usage_environment import (
    _account_usage_preexec_fn,
    _account_usage_subprocess_env,
)
from api.providers.credentials import _provider_env_var_for
from api.providers.usage_projection import _account_usage_payload_to_snapshot

logger = logging.getLogger(__name__)

_ACCOUNT_USAGE_PROBE_CHILD = Path(__file__).with_name("account_usage_probe_child.py")

_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS = 35.0
_ACCOUNT_USAGE_CACHE_TTL_SECONDS = 45.0
_ACCOUNT_USAGE_CACHE_MAX_ENTRIES = 64
_ACCOUNT_USAGE_WORKER_IDLE_SECONDS = 5 * 60
_MAX_CONCURRENT_ACCOUNT_USAGE_PROBES = 2
_ACCOUNT_USAGE_WORKERS_PER_HOME = 2

_account_usage_probe_semaphore: threading.BoundedSemaphore | None = None
_account_usage_status_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_account_usage_status_cache_lock = threading.Lock()
_account_usage_worker_pool: dict[str, list["_AccountUsageProbeWorker"]] = {}
_account_usage_worker_pool_lock = threading.Lock()


def _get_hermes_home() -> Path:
    from api import profiles

    return profiles.get_active_hermes_home()


def _get_account_usage_probe_semaphore() -> threading.BoundedSemaphore:
    global _account_usage_probe_semaphore
    if _account_usage_probe_semaphore is None:
        _account_usage_probe_semaphore = threading.BoundedSemaphore(
            _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES
        )
    return _account_usage_probe_semaphore


def _agent_fetch_account_usage(
    provider: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    from agent.account_usage import fetch_account_usage

    return fetch_account_usage(provider, base_url=base_url, api_key=api_key)


def _python_executable() -> str:
    try:
        from api.config import PYTHON_EXE

        return PYTHON_EXE
    except Exception:
        return sys.executable or "python3"


def _launch_account_usage_worker_process(
    home: Path,
    provider: str,
    *,
    stdin: Any = subprocess.PIPE,
    stdout: Any = subprocess.PIPE,
) -> subprocess.Popen[str] | None:
    kwargs: dict[str, Any] = {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,
    }
    if hasattr(os, "fork"):
        kwargs["preexec_fn"] = _account_usage_preexec_fn
    try:
        return subprocess.Popen(
            [
                _python_executable(),
                str(_ACCOUNT_USAGE_PROBE_CHILD),
                "--worker",
            ],
            env=_account_usage_subprocess_env(home, provider, None),
            **kwargs,
        )
    except Exception:
        logger.debug(
            "Account usage worker for %s failed to launch", provider, exc_info=True
        )
        return None


class _AccountUsageProbeWorker:
    """One serialized JSON-lines connection to a profile-isolated child."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.last_used = time.monotonic()
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._closed = False

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._closed = True
        self._close_process(proc)

    @staticmethod
    def _close_process(proc: subprocess.Popen[str] | None) -> None:
        if proc is None:
            return
        for stream_name in ("stdin", "stdout"):
            stream = getattr(proc, stream_name, None)
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def fetch(self, provider: str, *, api_key: str | None = None) -> Any:
        if not self._lock.acquire(blocking=False):
            return _fetch_account_usage_once_for_home(
                provider, self.home, api_key=api_key
            )
        try:
            return self._fetch_locked(provider, api_key=api_key)
        finally:
            self._lock.release()

    def _fetch_locked(self, provider: str, *, api_key: str | None = None) -> Any:
        self.last_used = time.monotonic()
        proc = self._ensure_process(provider)
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        request = (
            json.dumps(
                {
                    "provider": provider,
                    "api_key": api_key or "",
                    "env_var": _provider_env_var_for((provider or "").strip().lower()),
                }
            )
            + "\n"
        )
        result: dict[str, Any] = {}

        def round_trip() -> None:
            try:
                proc.stdin.write(request)
                proc.stdin.flush()
                result["line"] = proc.stdout.readline()
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=round_trip, daemon=True)
        thread.start()
        thread.join(_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS)
        self.last_used = time.monotonic()
        if thread.is_alive():
            self.close()
            thread.join(timeout=1.0)
            logger.debug("Account usage worker for %s timed out", provider)
            return None
        if result.get("error") is not None:
            exc = result["error"]
            self.close()
            logger.debug(
                "Account usage worker for %s failed",
                provider,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return None
        line = str(result.get("line") or "").strip()
        if not line:
            self.close()
            logger.debug(
                "Account usage worker for %s exited before responding", provider
            )
            return None
        try:
            return _account_usage_payload_to_snapshot(json.loads(line))
        except json.JSONDecodeError:
            self.close()
            logger.debug("Account usage worker for %s returned invalid JSON", provider)
            return None

    def _ensure_process(self, provider: str) -> subprocess.Popen[str] | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        old_proc = self._proc
        self._proc = None
        self._close_process(old_proc)
        self._proc = _launch_account_usage_worker_process(self.home, provider)
        self._closed = self._proc is None
        return self._proc


def _fetch_account_usage_once_for_home(
    provider: str,
    home: Path,
    *,
    api_key: str | None = None,
) -> Any:
    proc = _launch_account_usage_worker_process(Path(home), provider)
    if proc is None or proc.stdin is None or proc.stdout is None:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    request = (
        json.dumps(
            {
                "provider": provider,
                "api_key": api_key or "",
                "env_var": _provider_env_var_for((provider or "").strip().lower()),
            }
        )
        + "\n"
    )
    try:
        stdout, _stderr = proc.communicate(
            request, timeout=_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    except Exception:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    try:
        return _account_usage_payload_to_snapshot(
            json.loads(str(stdout or "").splitlines()[0])
        )
    except (json.JSONDecodeError, IndexError):
        return None


def _get_account_usage_probe_worker(home: Path) -> _AccountUsageProbeWorker | None:
    """Return a worker with its lock held; the caller must release it."""
    key = str(Path(home))
    stale: list[_AccountUsageProbeWorker] = []
    claimed = None
    with _account_usage_worker_pool_lock:
        existing_workers = _account_usage_worker_pool.get(key)
        if not existing_workers:
            workers = [
                _AccountUsageProbeWorker(Path(home))
                for _ in range(_ACCOUNT_USAGE_WORKERS_PER_HOME)
            ]
        else:
            stale = [worker for worker in existing_workers if worker._closed]
            workers = [worker for worker in existing_workers if not worker._closed]
            while len(workers) < _ACCOUNT_USAGE_WORKERS_PER_HOME:
                workers.append(_AccountUsageProbeWorker(Path(home)))
        _account_usage_worker_pool[key] = workers
        for worker in workers:
            if worker._lock.acquire(blocking=False):
                claimed = worker
                break
    for worker in stale:
        worker.close()
    return claimed


def _cleanup_account_usage_probe_workers(
    *,
    now: float | None = None,
    idle_seconds: float = _ACCOUNT_USAGE_WORKER_IDLE_SECONDS,
) -> None:
    cutoff = time.monotonic() if now is None else now
    stale: list[tuple[str, _AccountUsageProbeWorker]] = []
    with _account_usage_worker_pool_lock:
        for key, workers in list(_account_usage_worker_pool.items()):
            for worker in workers:
                if worker._lock.acquire(blocking=False):
                    try:
                        proc = worker._proc
                        dead = worker._closed or (
                            proc is not None and proc.poll() is not None
                        )
                        if dead or cutoff - worker.last_used >= idle_seconds:
                            stale.append((key, worker))
                    finally:
                        worker._lock.release()
            remaining = [
                worker
                for worker in workers
                if not any(
                    stale_key == key and stale_worker is worker
                    for stale_key, stale_worker in stale
                )
            ]
            if not remaining:
                _account_usage_worker_pool.pop(key, None)
            else:
                while len(remaining) < _ACCOUNT_USAGE_WORKERS_PER_HOME:
                    remaining.append(_AccountUsageProbeWorker(Path(key)))
                _account_usage_worker_pool[key] = remaining
    for _key, worker in stale:
        worker.close()


def _close_account_usage_probe_worker_list(
    workers: list[_AccountUsageProbeWorker],
) -> None:
    for worker in workers:
        worker.close()


def _close_account_usage_probe_workers() -> None:
    with _account_usage_worker_pool_lock:
        workers = [
            worker
            for values in _account_usage_worker_pool.values()
            for worker in values
        ]
        _account_usage_worker_pool.clear()
    _close_account_usage_probe_worker_list(workers)


def _close_account_usage_probe_workers_async(
    *,
    provider_id: str | None = None,
    active_home: Path | None = None,
) -> None:
    with _account_usage_worker_pool_lock:
        if provider_id:
            home_key = str(active_home or _get_hermes_home())
            workers_to_close = _account_usage_worker_pool.pop(home_key, [])
        else:
            workers_to_close = [
                worker
                for values in _account_usage_worker_pool.values()
                for worker in values
            ]
            _account_usage_worker_pool.clear()
    if workers_to_close:
        threading.Thread(
            target=_close_account_usage_probe_worker_list,
            args=(workers_to_close,),
            daemon=True,
            name="account-usage-worker-close",
        ).start()


def _account_usage_cache_key(
    provider: str,
    home: Path,
    api_key: str | None,
) -> tuple[str, str, str]:
    fingerprint = (
        hashlib.sha256(api_key.encode("utf-8", "ignore")).hexdigest() if api_key else ""
    )
    return ((provider or "").strip().lower(), str(Path(home)), fingerprint)


def _get_cached_account_usage(cache_key: tuple[str, str, str]) -> tuple[bool, Any]:
    now = time.monotonic()
    with _account_usage_status_cache_lock:
        cached = _account_usage_status_cache.get(cache_key)
        if cached is None:
            return False, None
        fetched_at, snapshot = cached
        if now - fetched_at <= _ACCOUNT_USAGE_CACHE_TTL_SECONDS:
            return True, snapshot
        _account_usage_status_cache.pop(cache_key, None)
    return False, None


def _set_cached_account_usage(cache_key: tuple[str, str, str], snapshot: Any) -> None:
    now = time.monotonic()
    with _account_usage_status_cache_lock:
        if snapshot is None:
            cached = _account_usage_status_cache.get(cache_key)
            if cached is None or cached[1] is None:
                _account_usage_status_cache.pop(cache_key, None)
            return
        _account_usage_status_cache[cache_key] = (now, snapshot)
        for key, (fetched_at, _snapshot) in list(_account_usage_status_cache.items()):
            if now - fetched_at > _ACCOUNT_USAGE_CACHE_TTL_SECONDS:
                _account_usage_status_cache.pop(key, None)
        while len(_account_usage_status_cache) > _ACCOUNT_USAGE_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _account_usage_status_cache,
                key=lambda key: _account_usage_status_cache[key][0],
            )
            _account_usage_status_cache.pop(oldest_key, None)


def invalidate_account_usage_status_cache(
    provider_id: str | None = None,
    *,
    active_home: Path | None = None,
) -> None:
    normalized = str(provider_id or "").strip().lower()
    with _account_usage_status_cache_lock:
        if not normalized:
            _account_usage_status_cache.clear()
        else:
            for key in list(_account_usage_status_cache):
                if key[0] == normalized:
                    _account_usage_status_cache.pop(key, None)
    _close_account_usage_probe_workers_async(
        provider_id=normalized or None,
        active_home=active_home,
    )


def _agent_fetch_account_usage_for_home(
    provider: str,
    home: Path,
    *,
    api_key: str | None = None,
) -> Any:
    try:
        _cleanup_account_usage_probe_workers()
        worker = _get_account_usage_probe_worker(home)
        if worker is not None:
            try:
                return worker._fetch_locked(provider, api_key=api_key)
            finally:
                worker._lock.release()
        return _fetch_account_usage_once_for_home(provider, home, api_key=api_key)
    except Exception:
        logger.debug("Account usage probe for %s failed", provider, exc_info=True)
        return None


def fetch_account_usage_with_profile_context(
    provider: str,
    *,
    refresh: bool = False,
    home_resolver: Callable[[], Path] = _get_hermes_home,
    key_resolver: Callable[[str], str | None],
    fetcher: Callable[..., Any] = _agent_fetch_account_usage_for_home,
) -> Any:
    """Fetch through the profile, credential, concurrency, and cache owners."""
    home = home_resolver()
    api_key = key_resolver(provider)
    cache_key = _account_usage_cache_key(provider, home, api_key)
    if not refresh:
        cache_hit, cached = _get_cached_account_usage(cache_key)
        if cache_hit:
            return cached
    try:
        with _get_account_usage_probe_semaphore():
            snapshot = fetcher(provider, home, api_key=api_key)
            _set_cached_account_usage(cache_key, snapshot)
            return snapshot
    except Exception:
        logger.debug("Failed to fetch account usage for %s", provider, exc_info=True)
        _set_cached_account_usage(cache_key, None)
        return None


__all__ = (
    "_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS",
    "_ACCOUNT_USAGE_CACHE_TTL_SECONDS",
    "_ACCOUNT_USAGE_CACHE_MAX_ENTRIES",
    "_ACCOUNT_USAGE_WORKER_IDLE_SECONDS",
    "_MAX_CONCURRENT_ACCOUNT_USAGE_PROBES",
    "_ACCOUNT_USAGE_WORKERS_PER_HOME",
    "_account_usage_probe_semaphore",
    "_account_usage_status_cache",
    "_account_usage_status_cache_lock",
    "_account_usage_worker_pool",
    "_account_usage_worker_pool_lock",
    "_get_account_usage_probe_semaphore",
    "_agent_fetch_account_usage",
    "_AccountUsageProbeWorker",
    "_account_usage_cache_key",
    "_launch_account_usage_worker_process",
    "_fetch_account_usage_once_for_home",
    "_get_account_usage_probe_worker",
    "_cleanup_account_usage_probe_workers",
    "_close_account_usage_probe_workers",
    "_close_account_usage_probe_worker_list",
    "_close_account_usage_probe_workers_async",
    "_get_cached_account_usage",
    "_set_cached_account_usage",
    "invalidate_account_usage_status_cache",
    "_agent_fetch_account_usage_for_home",
    "fetch_account_usage_with_profile_context",
)
