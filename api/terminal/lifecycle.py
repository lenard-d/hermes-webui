"""Session, PTY, output, resize, and cleanup ownership for embedded terminals."""

from __future__ import annotations

import atexit
import codecs
import errno
import os
import queue
import signal
import shutil
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import process


if process.TERMINAL_SUPPORTED:
    import fcntl
    import select
    import termios
else:
    fcntl = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]


_SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LANGUAGE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


def _winsize(rows: int, cols: int) -> bytes:
    rows = max(8, min(int(rows or 24), 80))
    cols = max(20, min(int(cols or 80), 240))
    return struct.pack("HHHH", rows, cols, 0, 0)


def _safe_close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _shell_path() -> str:
    shell = os.environ.get("SHELL") or ""
    if shell and Path(shell).exists():
        return shell
    return shutil.which("zsh") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"


def _shell_argv(shell: str) -> list[str]:
    return [shell, "-i"] if Path(shell).name in {"zsh", "bash", "sh"} else [shell]


def _decode_terminal_output(decoder, data: bytes) -> str:
    """Decode PTY bytes without stripping terminal control sequences."""
    return decoder.decode(data)


@dataclass
class TerminalSession:
    session_id: str
    workspace: str
    proc: object
    master_fd: int
    rows: int = 24
    cols: int = 80
    output: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2000))
    closed: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    # Serializes writes/resizes against close so a recycled fd cannot receive
    # input intended for a previous terminal.
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        return not self.closed.is_set() and self.proc.poll() is None

    def put_output(self, event: str, payload: dict) -> None:
        self.last_activity = time.time()
        try:
            self.output.put_nowait((event, payload))
        except queue.Full:
            try:
                self.output.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output.put_nowait((event, payload))
            except queue.Full:
                pass


@dataclass(frozen=True)
class TerminalEvent:
    name: str
    payload: dict


class TerminalOutputSubscription:
    """Stable output handle retained by one SSE request across terminal teardown."""

    def __init__(self, terminal: TerminalSession) -> None:
        self._terminal = terminal

    def next_event(self, timeout: float) -> TerminalEvent | None:
        try:
            event, payload = self._terminal.output.get(timeout=timeout)
        except queue.Empty:
            return None
        return TerminalEvent(str(event), payload)

    def is_closed_and_drained(self) -> bool:
        return self._terminal.closed.is_set() and self._terminal.output.empty()

    def exit_code(self):
        return self._terminal.proc.poll()


class TerminalRuntime:
    """Own all live terminal sessions and every lifecycle transition."""

    def __init__(self, *, max_terminals: int = 32) -> None:
        self._terminals: dict[str, TerminalSession] = {}
        self._lock = threading.RLock()
        self.max_terminals = max_terminals

    def _set_size(self, terminal: TerminalSession, rows: int, cols: int) -> None:
        terminal.rows = max(8, min(int(rows or terminal.rows or 24), 80))
        terminal.cols = max(20, min(int(cols or terminal.cols or 80), 240))
        with terminal.io_lock:
            if not terminal.closed.is_set():
                try:
                    fcntl.ioctl(
                        terminal.master_fd,
                        termios.TIOCSWINSZ,
                        _winsize(terminal.rows, terminal.cols),
                    )
                except OSError:
                    pass
        try:
            if terminal.proc.poll() is None:
                os.killpg(terminal.proc.pid, signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def _reader_loop(self, terminal: TerminalSession) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while not terminal.closed.is_set():
                if terminal.proc.poll() is not None:
                    break
                try:
                    ready, _, _ = select.select([terminal.master_fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    data = os.read(terminal.master_fd, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not data:
                    break
                text = _decode_terminal_output(decoder, data)
                if text:
                    terminal.put_output("output", {"text": text})
        except Exception as exc:
            terminal.put_output("terminal_error", {"error": str(exc)})
        finally:
            terminal.closed.set()
            code = terminal.proc.poll()
            process.reap_terminal_descendants(terminal.proc.pid)
            terminal.put_output("terminal_closed", {"exit_code": code})
            # Identity guard prevents an old reader from closing a replacement.
            self.close(terminal.session_id, expected=terminal)

    def _enforce_cap(self, *, exclude_sid: str | None = None) -> None:
        if not process.TERMINAL_SUPPORTED:
            return
        for _ in range(self.max_terminals + 1):
            victim_sid = None
            victim_terminal = None
            with self._lock:
                if exclude_sid in self._terminals:
                    return
                if len(self._terminals) < self.max_terminals:
                    return
                candidates = [
                    (sid, terminal)
                    for sid, terminal in self._terminals.items()
                    if sid != exclude_sid
                ]
                if not candidates:
                    return
                dead = [(sid, terminal) for sid, terminal in candidates if not terminal.is_alive()]
                victim_sid, victim_terminal = (
                    dead[0]
                    if dead
                    else min(candidates, key=lambda item: item[1].last_activity)
                )
            self.close(victim_sid, expected=victim_terminal)

    def start(
        self,
        session_id: str,
        workspace: Path,
        rows: int = 24,
        cols: int = 80,
        restart: bool = False,
    ) -> TerminalSession:
        if not process.TERMINAL_SUPPORTED:
            raise NotImplementedError("Embedded terminal is not supported on Windows")
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        cwd = str(Path(workspace).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError("workspace is not a directory")

        self._enforce_cap(exclude_sid=sid)
        with self._lock:
            current = self._terminals.get(sid)
            if current and current.is_alive() and not restart and current.workspace == cwd:
                self._set_size(current, rows, cols)
                return current
            if current:
                self.close(sid)

            master_fd, slave_fd = os.openpty()
            env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
            env.update(
                {
                    "TERM": "xterm-256color",
                    "COLORTERM": "truecolor",
                    "COLUMNS": str(cols),
                    "LINES": str(rows),
                    "PWD": cwd,
                    "HERMES_WEBUI_TERMINAL": "1",
                }
            )
            shell = _shell_path()
            try:
                proc = process.spawn_terminal_process(
                    {
                        "args": _shell_argv(shell),
                        "cwd": cwd,
                        "env": env,
                        "stdin": slave_fd,
                        "stdout": slave_fd,
                        "stderr": slave_fd,
                        "close_fds": True,
                        "start_new_session": True,
                    }
                )
            except BaseException:
                _safe_close_fd(master_fd)
                _safe_close_fd(slave_fd)
                raise
            os.close(slave_fd)
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            terminal = TerminalSession(
                session_id=sid,
                workspace=cwd,
                proc=proc,
                master_fd=master_fd,
                rows=rows,
                cols=cols,
            )
            self._set_size(terminal, rows, cols)
            terminal.reader = threading.Thread(
                target=self._reader_loop,
                args=(terminal,),
                daemon=True,
            )
            terminal.reader.start()
            self._terminals[sid] = terminal
            return terminal

    def get(self, session_id: str) -> TerminalSession | None:
        if not process.TERMINAL_SUPPORTED:
            return None
        with self._lock:
            return self._terminals.get(str(session_id or ""))

    def attach_output(self, session_id: str) -> TerminalOutputSubscription | None:
        terminal = self.get(session_id)
        if terminal is None:
            return None
        return TerminalOutputSubscription(terminal)

    def write(self, session_id: str, data: str) -> None:
        if not process.TERMINAL_SUPPORTED:
            raise NotImplementedError("Embedded terminal is not supported on Windows")
        terminal = self.get(session_id)
        if not terminal or not terminal.is_alive():
            raise KeyError("terminal not running")
        with terminal.io_lock:
            if terminal.closed.is_set():
                raise KeyError("terminal not running")
            os.write(terminal.master_fd, str(data or "").encode("utf-8", errors="replace"))
        terminal.last_activity = time.time()

    def resize(self, session_id: str, rows: int, cols: int) -> None:
        if not process.TERMINAL_SUPPORTED:
            raise NotImplementedError("Embedded terminal is not supported on Windows")
        terminal = self.get(session_id)
        if not terminal:
            raise KeyError("terminal not running")
        self._set_size(terminal, rows, cols)

    def close(
        self,
        session_id: str,
        *,
        expected: TerminalSession | None = None,
    ) -> bool:
        if not process.TERMINAL_SUPPORTED:
            return False
        sid = str(session_id or "")
        with self._lock:
            if expected is not None and self._terminals.get(sid) is not expected:
                return False
            terminal = self._terminals.pop(sid, None)
        if not terminal:
            return False
        terminal.closed.set()
        try:
            process.terminate_terminal_process(terminal.proc)
        finally:
            with terminal.io_lock:
                _safe_close_fd(terminal.master_fd)
        return True

    def close_all(self) -> None:
        with self._lock:
            session_ids = list(self._terminals)
        for session_id in session_ids:
            self.close(session_id)


_RUNTIME = TerminalRuntime()
atexit.register(_RUNTIME.close_all)
