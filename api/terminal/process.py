"""POSIX process supervision and teardown for embedded terminals.

This module owns shell process creation and process-group cleanup.  It has no
knowledge of WebUI sessions, HTTP requests, SSE streams, or workspace policy.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field


TERMINAL_SUPPORTED = sys.platform != "win32"
_TERMINAL_DESCENDANT_REAPER_LIMIT = 64
_terminal_descendant_reaper_lock = threading.Lock()


@dataclass
class _SpawnRequest:
    kwargs: dict
    done: threading.Event = field(default_factory=threading.Event)
    timed_out: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen | None = None
    error: BaseException | None = None


def _reap_abandoned_spawn(proc: subprocess.Popen) -> bool:
    """Terminate a process whose caller timed out before receiving ownership."""
    if proc.poll() is not None:
        return True
    try:
        os.killpg(proc.pid, signal.SIGHUP)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            pass
    if proc.poll() is None:
        print("terminal abandoned spawn cleanup failed", flush=True)
        return False
    return True


def reap_terminal_descendants(
    terminal_pgid: int,
    limit: int = _TERMINAL_DESCENDANT_REAPER_LIMIT,
) -> int:
    """Reap exited descendants that remain in a terminal-owned process group."""
    if not TERMINAL_SUPPORTED:
        return 0
    try:
        terminal_pgid = abs(int(terminal_pgid))
    except (TypeError, ValueError):
        return 0
    if terminal_pgid <= 0:
        return 0
    reaped = 0
    with _terminal_descendant_reaper_lock:
        for _ in range(max(0, int(limit))):
            try:
                pid, _status = os.waitpid(-terminal_pgid, os.WNOHANG)
            except (ChildProcessError, OSError):
                break
            if pid == 0:
                break
            reaped += 1
    return reaped


class _SpawnSupervisor:
    """Keep ``Popen`` off short-lived HTTP request threads on Linux."""

    def __init__(self) -> None:
        self.queue: queue.Queue[_SpawnRequest] = queue.Queue()
        self._start_lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def ensure_started(self) -> None:
        with self._start_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            thread = threading.Thread(target=self._entry, daemon=True)
            thread.start()
            self.thread = thread

    def spawn(self, kwargs: dict, *, timeout: float = 5.0) -> subprocess.Popen:
        self.ensure_started()
        request = _SpawnRequest(kwargs)
        self.queue.put(request)
        if not request.done.wait(timeout=timeout):
            timed_out = False
            with request.lock:
                if not request.done.is_set():
                    request.timed_out.set()
                    timed_out = True
            if timed_out:
                raise TimeoutError("terminal spawn timeout - supervisor unresponsive")
        if request.error:
            raise request.error
        if request.proc is None:
            raise RuntimeError("terminal spawn failed without process")
        return request.proc

    def _loop(self) -> None:
        while True:
            request = None
            try:
                request = self.queue.get()
                try:
                    proc = subprocess.Popen(**request.kwargs)
                    with request.lock:
                        if request.timed_out.is_set():
                            _reap_abandoned_spawn(proc)
                        else:
                            request.proc = proc
                        request.done.set()
                except BaseException as exc:
                    with request.lock:
                        try:
                            request.error = exc
                        except BaseException:
                            pass
                        request.done.set()
            except BaseException as exc:
                if request is not None:
                    try:
                        request.error = exc
                    except BaseException:
                        pass
                    try:
                        request.done.set()
                    except BaseException:
                        pass
                time.sleep(0.01)

    def _entry(self) -> None:
        while True:
            try:
                self._loop()
            except BaseException:
                time.sleep(0.01)


_SPAWN_SUPERVISOR = _SpawnSupervisor()
if TERMINAL_SUPPORTED:
    _SPAWN_SUPERVISOR.ensure_started()


def spawn_terminal_process(kwargs: dict, *, timeout: float = 5.0) -> subprocess.Popen:
    return _SPAWN_SUPERVISOR.spawn(kwargs, timeout=timeout)


def terminate_terminal_process(proc: subprocess.Popen) -> None:
    """Terminate and reap a terminal-owned process group, escalating if needed."""
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    pass
    finally:
        reap_terminal_descendants(proc.pid)
