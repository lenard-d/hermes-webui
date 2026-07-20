"""Secure repository I/O primitives for update discovery and application."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

_GIT_DIAGNOSTIC_MAX_CHARS = 300
_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|api[_-]?key|token|password|secret|auth|key)=)[^&\s'\"]+", re.IGNORECASE)
_FETCH_NETWORK_FAILURE_SIGNATURES = (
    'could not resolve host',
    'failed to connect',
    'network is unreachable',
    'no route to host',
    'connection timed out',
    'timed out after',
    'connection reset by peer',
    'remote end hung up unexpectedly',
    'tls connection was non-properly terminated',
    'ssl certificate problem',
)
_GIT_LOCK_SIGNATURES = (
    "index.lock': file exists",
    ".lock': file exists",
    'another git process seems to be running',
    'unable to create .git/index.lock',
)


def _sanitize_git_diagnostic(output: str, *, limit: int = _GIT_DIAGNOSTIC_MAX_CHARS) -> str:
    """Return a user-facing git diagnostic with credentials removed.

    Git can echo remote URLs in failure output.  Keep the actionable error text,
    but strip URL userinfo, common GitHub token shapes, and secret-looking query
    parameter values before any message reaches the update-check API/UI.
    """
    if not output:
        return ""
    sanitized = _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", str(output))
    sanitized = _GITHUB_TOKEN_RE.sub("<redacted>", sanitized)
    sanitized = _QUERY_SECRET_RE.sub(r"\1<redacted>", sanitized)
    sanitized = sanitized.strip()
    if len(sanitized) > limit:
        sanitized = sanitized[:limit].rstrip() + "…"
    return sanitized


def _apply_fetch_failure_message(fetch_out: str, network_message: str) -> str:
    """Return the apply-path fetch failure message for the given stderr."""
    detail = _sanitize_git_diagnostic(fetch_out)
    if not detail:
        return network_message
    detail_lower = detail.lower()
    if any(signature in detail_lower for signature in _FETCH_NETWORK_FAILURE_SIGNATURES):
        return network_message
    return f'fetch failed: {detail}'


def _run_git(args, cwd, timeout=10):
    """Run a git command and return (useful output, ok).

    On failure, returns stderr (or stdout as fallback) so callers can
    surface actionable git error messages instead of empty strings.
    """
    git_executable = _resolve_git_executable()
    if not git_executable:
        return 'git executable not found', False
    try:
        r = subprocess.run(
            [git_executable] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
        # On non-UTF-8 locales (e.g. Chinese Windows GBK), a binary git
        # output that fails to decode used to leave r.stdout = None and crash
        # the whole import with AttributeError. Guard against None defensively.
        stdout = (r.stdout or '').strip()
        stderr = (r.stderr or '').strip()
        if r.returncode == 0:
            return stdout, True
        return stderr or stdout or f"git exited with status {r.returncode}", False
    except subprocess.TimeoutExpired as exc:
        detail = (getattr(exc, 'stderr', None) or getattr(exc, 'stdout', None) or '').strip()
        return detail or f"git {' '.join(args)} timed out after {timeout}s", False
    except FileNotFoundError:
        return 'git executable not found', False
    except OSError as exc:
        return f'git failed to start: {exc}', False


def _is_git_lock_error(output: str) -> bool:
    if not output:
        return False
    lower_out = output.lower()
    return any(sig in lower_out for sig in _GIT_LOCK_SIGNATURES)


def _inventory_locks(path: Path) -> dict:
    """Return a snapshot of lock files currently present under ``path/.git``.

    v2.2: replaced v2's `_is_lock_held` + `_try_remove_lock` machinery with
    pure inventory. Round-2 cert (gate-fail) proved that `fcntl.flock`
    cannot detect a live git lock, because git uses `O_CREAT|O_EXCL` and
    `rename(2)`, NOT advisory locking. Any auto-delete path can therefore
    race against a running `git add` and corrupt the index. v2.2 stops
    deleting locks from the server entirely: the only thing that removes
    a lock is the user, on the host, via the manual command surfaced in
    the response. Once the lock is gone, the user re-clicks Update Now
    and the normal non-destructive apply path runs.
    """
    git_dir = path / '.git'
    out = {
        'well_known_lock_present': False,  # ``.git/index.lock`` exists?
        'well_known_lock_path': None,      # absolute path of ``.git/index.lock``
        'other_locks': [],                  # any other lock files, by relative path
    }
    if not git_dir.exists():
        return out
    well_known = git_dir / 'index.lock'
    try:
        out['well_known_lock_present'] = well_known.exists()
    except OSError:
        # Permission problem reading the directory -- treat conservatively.
        out['well_known_lock_present'] = True
    out['well_known_lock_path'] = str(well_known)

    # Enumerate every other lock file under .git/ for diagnostic reporting.
    # We never touch them; this is purely an inventory.
    try:
        for entry in sorted(git_dir.rglob('*.lock')):
            try:
                rel = entry.relative_to(git_dir).as_posix()
            except ValueError:
                continue
            if rel == 'index.lock':
                continue
            out['other_locks'].append(rel)
    except OSError:
        # rglob can fail on unreadable subtrees; skip quietly.
        pass
    return out

def _windows_git_from_registry():
    """Best-effort resolve git.exe from the Git-for-Windows registry key.

    Git for Windows records its install root at
    ``HKLM\\SOFTWARE\\GitForWindows\\InstallPath`` (and the WOW6432Node mirror
    for a 32-bit install on 64-bit Windows). ``git.exe`` lives under
    ``<InstallPath>\\cmd\\git.exe``. This is the reliable way to find git when
    it is installed but NOT on the launching process's PATH — e.g. the WebUI
    server started from a venv python whose environment does not inherit the
    interactive shell PATH, which otherwise degrades WEBUI_VERSION to
    ``'unknown'`` and freezes the ``?v=`` static-asset cache-busting stamp.
    """
    try:
        import winreg
    except ImportError:
        return None
    for hive, flag in (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    ):
        try:
            with winreg.OpenKey(
                hive, r'SOFTWARE\GitForWindows', 0,
                winreg.KEY_READ | flag,
            ) as key:
                install_path, _ = winreg.QueryValueEx(key, 'InstallPath')
        except OSError:
            continue
        if not install_path:
            continue
        candidate = os.path.join(install_path, 'cmd', 'git.exe')
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_git_executable():
    git_executable = shutil.which('git')
    if git_executable:
        return git_executable
    if sys.platform == 'darwin' and os.path.exists('/usr/bin/git'):
        return '/usr/bin/git'
    if sys.platform == 'win32':
        from_registry = _windows_git_from_registry()
        if from_registry:
            return from_registry
        for candidate in (
            os.path.expandvars(r'%ProgramFiles%\Git\cmd\git.exe'),
            os.path.expandvars(r'%ProgramFiles(x86)%\Git\cmd\git.exe'),
            os.path.expandvars(r'%LocalAppData%\Programs\Git\cmd\git.exe'),
        ):
            if candidate and os.path.exists(candidate):
                return candidate
    return None

def _normalize_remote_url(remote_url):
    """Return the browser-facing repository URL for update compare links.

    Git remotes may be HTTPS or SSH and may include a literal ``.git`` suffix.
    Strip only that literal suffix — never use ``str.rstrip('.git')`` because it
    treats the argument as a character set and can truncate ``hermes-webui`` to
    ``hermes-webu``.
    """
    if not remote_url:
        return remote_url
    remote_url = remote_url.strip()
    if remote_url.startswith('git@'):
        remote_url = remote_url.replace(':', '/', 1).replace('git@', 'https://', 1)
    remote_url = remote_url.rstrip('/')
    if remote_url.endswith('.git'):
        remote_url = remote_url[:-4]
    return remote_url.rstrip('/')


def _build_compare_url(repo_url, current_sha, latest_sha):
    """Return a safe browser compare URL, or None when any piece is missing."""
    if not (repo_url and current_sha and latest_sha):
        return None
    parsed = urlparse(repo_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return f"{repo_url}/compare/{current_sha}...{latest_sha}"


def _split_remote_ref(ref):
    """Split 'origin/branch-name' into ('origin', 'branch-name').

    Returns (None, ref) if ref contains no slash.
    """
    if '/' not in ref:
        return None, ref
    remote, branch = ref.split('/', 1)
    return remote, branch


def _detect_default_branch(path):
    """Detect the remote default branch (master or main)."""
    out, ok = _run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'], path)
    if ok and out:
        # refs/remotes/origin/master -> master
        return out.split('/')[-1]
    # Fallback: try master, then main
    for branch in ('master', 'main'):
        _, ok = _run_git(['rev-parse', '--verify', f'origin/{branch}'], path)
        if ok:
            return branch
    return 'master'
