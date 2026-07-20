"""Read-only workspace browsing through anchored filesystem handles."""

import hashlib
import json
import os
import stat
from pathlib import Path

from api.workspace_parts.bindings import workspace_api


def list_dir(workspace: Path, rel: str='.'):
    api = workspace_api()
    target = api.safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    ws_resolved = workspace.resolve()
    target_resolved = target.resolve()
    entries = []

    def _process(name, is_symlink, raw_link, lstat_result, reachable):
        """Append one directory entry. ``raw_link`` is the os.readlink() result
        for symlinks (else None); ``lstat_result`` is an os.stat_result obtained
        with follow_symlinks=False (else None); ``reachable`` is False when a
        follow_symlinks=True stat raised (broken target or symlink loop)."""
        if is_symlink:
            if raw_link is None:
                return
            # A symlink whose follow-stat raised (ELOOP / broken target) can never
            # be opened — filter it. This catches mutual/self loops portably across
            # Python versions where Path.resolve() loop handling differs (3.11
            # raises RuntimeError, 3.13 can return a path), so do not rely on
            # resolve() raising for cycle detection.
            if not reachable:
                return
            try:
                link_target = (target_resolved / raw_link).resolve()
            except (OSError, RuntimeError):
                return
            # Cycle detection: skip if symlink points back to current dir or root.
            if link_target == target_resolved or link_target == ws_resolved:
                return
            try:
                target_resolved.relative_to(link_target)
                return  # target is under link_target — ancestor → cycle
            except ValueError:
                pass
            # Tag symlinks whose resolved target escapes the workspace root.
            # Previously silently dropped; now emitted with target_outside_workspace=True
            # so the workspace tree can show the link exists (display-only — the
            # read/list gate in safe_resolve_ws / open_anchored_fd still blocks
            # navigation through it).
            target_outside_workspace = False
            try:
                link_target.relative_to(ws_resolved)
            except ValueError:
                target_outside_workspace = True
            if api._is_blocked_system_path(link_target):
                return
            display_path = name
            if rel and rel != '.':
                display_path = rel + '/' + display_path
            mtime_ns = lstat_result.st_mtime_ns if lstat_result is not None else None
            if target_outside_workspace:
                # #4581 hardening: a display-only escape-target symlink must NOT
                # disclose where it points. Emit ONLY display-safe fields — never
                # the resolved outside path, target-derived is_dir, or target size
                # (the row exists to show the link is present; navigation/read
                # through it stays blocked by safe_resolve_ws/open_anchored_fd).
                entry = {
                    'name': name,
                    'path': display_path,
                    'type': 'symlink',
                    'is_dir': False,
                    'target_outside_workspace': True,
                    'mtime_ns': mtime_ns,
                }
                entries.append(entry)
            else:
                is_dir = link_target.is_dir()
                entry = {
                    'name': name,
                    'path': display_path,
                    'type': 'symlink',
                    'target': str(link_target),
                    'is_dir': is_dir,
                    'target_outside_workspace': False,
                    'mtime_ns': mtime_ns,
                }
                if not is_dir:
                    try:
                        entry['size'] = link_target.stat().st_size
                    except OSError:
                        entry['size'] = None
                entries.append(entry)
        else:
            entry_path = name
            if rel and rel != '.':
                entry_path = rel + '/' + name
            if lstat_result is not None:
                is_file = stat.S_ISREG(lstat_result.st_mode)
                size = lstat_result.st_size if is_file else None
                mtime_ns = lstat_result.st_mtime_ns
                is_dir_entry = stat.S_ISDIR(lstat_result.st_mode)
            else:
                size = None
                mtime_ns = None
                is_dir_entry = False
            entries.append({
                'name': name,
                'path': entry_path,
                'type': 'dir' if is_dir_entry else 'file',
                'size': size,
                'mtime_ns': mtime_ns,
            })

    if api._DIR_FD_OK:
        # #3398 TOCTOU hardening (Linux/macOS): open the directory via an anchored
        # openat-walk (O_NOFOLLOW on every component) and enumerate via the verified
        # fd (os.scandir(fd) + fd-relative fstatat/readlinkat), so a path component
        # swapped to an escaping symlink after safe_resolve_ws() cannot redirect the
        # listing.
        def _sort_key_de(de):
            try:
                is_link = de.is_symlink()
            except OSError:
                is_link = False
            is_file = False
            if not is_link:
                try:
                    is_file = de.is_file()
                except OSError:
                    pass
            return (not is_link, is_file, de.name.lower())

        dir_fd = api.open_anchored_fd(workspace, target, want_dir=True)
        try:
            st = os.fstat(dir_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise FileNotFoundError(f"Not a directory: {rel}")
            with os.scandir(dir_fd) as scan:
                scandir_entries = sorted(scan, key=_sort_key_de)
            for de in scandir_entries:
                name = de.name
                is_symlink = de.is_symlink()
                raw_link = None
                if is_symlink:
                    try:
                        raw_link = os.readlink(name, dir_fd=dir_fd)
                    except OSError:
                        raw_link = None
                try:
                    lst = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    lst = None
                # reachable: follow-stat succeeds (filters ELOOP/broken symlinks).
                reachable = True
                if is_symlink:
                    try:
                        os.stat(name, dir_fd=dir_fd, follow_symlinks=True)
                    except OSError:
                        reachable = False
                _process(name, is_symlink, raw_link, lst, reachable)
                if len(entries) >= 200:
                    break
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass
    else:
        # Portability fallback (Windows / no dir_fd): path-based enumeration after
        # safe_resolve_ws(). No anchored-fd race protection on these platforms, but
        # no regression vs the prior behaviour (creating symlinks on Windows needs
        # admin anyway), and safe_resolve_ws() still blocks the static escape.
        def _sort_key_p(p: Path):
            is_link = p.is_symlink()
            is_file = False
            if not is_link:
                try:
                    is_file = p.is_file()
                except OSError:
                    pass
            return (not is_link, is_file, p.name.lower())

        for item in sorted(target.iterdir(), key=_sort_key_p):
            name = item.name
            is_symlink = item.is_symlink()
            raw_link = None
            if is_symlink:
                try:
                    raw_link = os.readlink(str(item))
                except OSError:
                    raw_link = None
            try:
                lst = item.lstat()
            except OSError:
                lst = None
            # reachable: follow-stat succeeds (filters ELOOP/broken symlinks).
            reachable = True
            if is_symlink:
                try:
                    os.stat(str(item), follow_symlinks=True)
                except OSError:
                    reachable = False
            _process(name, is_symlink, raw_link, lst, reachable)
            if len(entries) >= 200:
                break
    return entries


def dir_signature(workspace: Path, rel: str = '.', entries: list[dict] | None = None) -> str:
    """Return a cheap, stable signature for a listed workspace directory.

    The signature is based only on bounded directory-entry metadata already used
    by the workspace tree: names, displayed paths, entry type, file sizes,
    mtimes, and symlink targets. It intentionally does not read file contents.
    """
    if entries is None:
        entries = workspace_api().list_dir(workspace, rel)
    payload = []
    for entry in entries:
        payload.append({
            'name': entry.get('name'),
            'path': entry.get('path'),
            'type': entry.get('type'),
            'is_dir': entry.get('is_dir'),
            'size': entry.get('size'),
            'mtime_ns': entry.get('mtime_ns'),
            'target': entry.get('target'),
            'target_outside_workspace': entry.get('target_outside_workspace'),
        })
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def read_file_content(workspace: Path, rel: str) -> dict:
    api = workspace_api()
    target = api.safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    # #3398 TOCTOU hardening: open the resolved file via an anchored openat-walk
    # (O_NOFOLLOW on every component) so a path swapped to an escaping symlink
    # after safe_resolve_ws() cannot be followed, then read from the fd (not the
    # pathname) so the bytes returned are guaranteed to be the verified file.
    fd = api.open_anchored_fd(workspace, target, want_dir=False)
    with os.fdopen(fd, 'rb', closefd=True) as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(f"Not a file: {rel}")
        if st.st_size > api.MAX_FILE_BYTES:
            raise ValueError(f"File too large ({st.st_size} bytes, max {api.MAX_FILE_BYTES})")
        raw = fh.read(api.MAX_FILE_BYTES + 1)
    if Path(str(rel)).suffix.lower() in {".docx", ".xlsx", ".pptx"}:
        from api.office_documents import preview_office_document

        return preview_office_document(rel, raw)
    content = raw.decode('utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': len(raw), 'lines': content.count('\n') + 1}
