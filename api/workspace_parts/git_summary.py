"""Small read-only Git summary used by the workspace header."""

import concurrent.futures
import subprocess
from pathlib import Path

from api.workspace_parts.bindings import workspace_api


def _run_git(args, cwd, timeout=3):
    """Run a git command and return stdout, or None on failure."""
    try:
        r = subprocess.run(
            ['git'] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def git_info_for_workspace(workspace: Path) -> dict:
    """Return git info for a workspace directory, or None if not a git repo."""
    if not (workspace / '.git').exists():
        return None
    api = workspace_api()
    branch = api._run_git(['rev-parse', '--abbrev-ref', 'HEAD'], workspace)
    if branch is None:
        return None
    # Run the remaining git commands in parallel via threads — they are
    # independent subprocess calls and together can take 50-200ms when run
    # serially.  Threading is safe here because each call blocks only on the
    # subprocess pipe, not on the GIL.
    def _ahead():
        r = api._run_git(['rev-list', '--count', '@{u}..HEAD'], workspace)
        return int(r) if r and r.isdigit() else 0
    def _behind():
        r = api._run_git(['rev-list', '--count', 'HEAD..@{u}'], workspace)
        return int(r) if r and r.isdigit() else 0
    def _status():
        out = api._run_git(['status', '--porcelain'], workspace) or ''
        lines = [l for l in out.splitlines() if l]
        modified = sum(1 for l in lines if len(l) >= 2 and (l[0] in 'MAR' or l[1] in 'MAR'))
        untracked = sum(1 for l in lines if l.startswith('??'))
        return len(lines), modified, untracked
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_status = pool.submit(_status)
        f_ahead  = pool.submit(_ahead)
        f_behind = pool.submit(_behind)
        dirty, modified, untracked = f_status.result()
        ahead  = f_ahead.result()
        behind = f_behind.result()
    return {
        'branch': branch,
        'dirty': dirty,
        'modified': modified,
        'untracked': untracked,
        'ahead': ahead,
        'behind': behind,
        'is_git': True,
    }
