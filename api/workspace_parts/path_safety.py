"""Workspace path validation and race-safe filesystem primitives.

This module owns the filesystem trust rules shared by workspace registration,
browsing, uploads, rollback, and Git mutations. Path containment checks and
the anchored file-descriptor operations intentionally live together: callers
must use the same resolved value for validation and the eventual filesystem
action.
"""

import os
import posixpath
import shutil
from pathlib import Path, PurePosixPath

from api.workspace_parts.bindings import workspace_api


def _expanduser_path(path: str | Path) -> Path:
    """Return *path* after shell-style home expansion.

    ``Path.expanduser()`` on Windows does not consistently honor a monkeypatched
    ``HOME`` in tests, which makes host-native replay diverge from the repo's
    portability expectations. Use ``os.path.expanduser`` as the expansion source,
    then wrap it back into ``Path``.
    """
    raw = str(path)
    if raw.startswith('~'):
        home = (
            os.environ.get('HOME')
            or os.environ.get('USERPROFILE')
            or (
                (os.environ.get('HOMEDRIVE') or '') + (os.environ.get('HOMEPATH') or '')
            )
            or str(Path.home())
        )
        if raw in ('~', '~/', '~\\'):
            return Path(home)
        if raw.startswith('~/') or raw.startswith('~\\'):
            return Path(home) / raw[2:]
        # NOTE: ``~user`` / ``~root`` forms are intentionally NOT expanded here.
        # Master deliberately does not block ``/root`` (#510/#521 — Hermes commonly
        # runs as root, where ``/root`` is the legitimate home and is allowed via
        # the home carve-out). Expanding ``~root`` -> ``/root`` for a NON-root
        # deployment would let it register root's home; leaving the literal form
        # (resolved relative to cwd, then rejected if it escapes) is the safer
        # behavior and matches the current security model.
    return Path(raw)


def _resolve_path(path: str | Path) -> Path:
    """Resolve *path* after env-aware home expansion, without raising."""
    api = workspace_api()
    return api._safe_resolve(api._expanduser_path(path))


def _home_path() -> Path:
    """Return the current effective home directory with env-aware expansion."""
    return workspace_api()._resolve_path("~")


def _as_posix_path(path: str | Path | None) -> PurePosixPath | None:
    if path in (None, ""):
        return None
    raw = workspace_api()._strip_surrounding_quotes(str(path)).strip().replace('\\', '/')
    # Reject embedded null bytes here rather than letting them survive normpath
    # and crash later at .resolve() with an uncaught ValueError (surfaces as a
    # 500). Fail-closed: treat as an invalid path.
    if '\x00' in raw:
        return None
    if not raw.startswith('/'):
        return None
    return PurePosixPath(posixpath.normpath(raw))


def _posix_is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_posix_path(path: str | Path | None) -> str | None:
    candidate = workspace_api()._as_posix_path(path)
    if candidate is None:
        return None
    return candidate.as_posix()



def _safe_resolve(p: Path) -> Path:
    """Path.resolve() that never raises — falls back to the input path on error."""
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError covers embedded-null-byte paths, which .resolve() raises on
        # — fail-closed to the raw path so the downstream block-list gate rejects
        # it cleanly instead of surfacing a 500.
        return p


# Per-user temp directories that sit nominally under a "system" prefix but are
# actually user-writable scratch space.  Workspaces registered here (e.g. by
# pytest's ``tmp_path_factory`` on macOS, which uses ``/var/folders/<hash>/T/``)
# must remain accepted even though their parent (``/var``) is blocked.  These
# carve-outs apply to BOTH workspace registration and runtime file ops so a
# symlink target inside the carve-out is also reachable.
_USER_TMP_PREFIXES: tuple[Path, ...] = (
    Path('/var/folders'),         # macOS per-user tmp (literal form)
    Path('/private/var/folders'),  # macOS per-user tmp (resolved form)
    Path('/var/tmp'),               # Linux/macOS system-wide tmp (user-writable)
    Path('/private/var/tmp'),       # macOS resolved form
)


def _workspace_blocked_roots() -> tuple[Path, ...]:
    """System roots that must never be accepted as workspace candidates.

    Returns both the literal path and its symlink-resolved canonical form,
    deduped.  This matters on macOS where ``/etc``, ``/var``, and ``/tmp``
    are symlinks to ``/private/etc`` etc.  Without the resolved forms,
    callers that pass a ``.resolve()``-d candidate (every caller does)
    would compare ``/private/etc`` against literal ``Path('/etc')`` and the
    ``relative_to`` check would miss — letting ``/etc`` through as a
    registered workspace on macOS.

    Carve-outs for legitimate user-tmp paths nominally under these roots
    (e.g. ``/var/folders/.../T/`` on macOS) are handled by
    :func:`_is_blocked_system_path`, not by exclusion from this list.
    """
    _raw = (
        # Linux / macOS
        '/etc',
        '/usr',
        '/var',
        '/bin',
        '/sbin',
        '/boot',
        '/proc',
        '/sys',
        '/dev',
        '/lib',
        '/lib64',
        '/opt/homebrew',
        '/System',
        '/Library',
    )
    _seen: set[Path] = set()
    _out: list[Path] = []
    for _p in _raw:
        for _form in (Path(_p), workspace_api()._safe_resolve(Path(_p))):
            if _form not in _seen:
                _seen.add(_form)
                _out.append(_form)
    return tuple(_out)


def _is_blocked_posix_workspace_path(raw_path: str | Path | None) -> bool:
    """Detect blocked POSIX-style system roots even on non-POSIX hosts."""
    api = workspace_api()
    candidate = api._as_posix_path(raw_path)
    if candidate is None:
        return False
    if candidate == PurePosixPath('/'):
        return True
    carveouts = (
        PurePosixPath('/var/folders'),
        PurePosixPath('/private/var/folders'),
        PurePosixPath('/var/tmp'),
        PurePosixPath('/private/var/tmp'),
    )
    for tmp in carveouts:
        if api._posix_is_within(candidate, tmp):
            return False
    blocked_roots = (
        PurePosixPath('/etc'),
        PurePosixPath('/usr'),
        PurePosixPath('/var'),
        PurePosixPath('/bin'),
        PurePosixPath('/sbin'),
        PurePosixPath('/boot'),
        PurePosixPath('/proc'),
        PurePosixPath('/sys'),
        PurePosixPath('/dev'),
        PurePosixPath('/lib'),
        PurePosixPath('/lib64'),
        PurePosixPath('/opt/homebrew'),
        PurePosixPath('/System'),
        PurePosixPath('/Library'),
        PurePosixPath('/private/etc'),
        PurePosixPath('/private/var'),
    )
    for blocked in blocked_roots:
        if api._posix_is_within(candidate, blocked):
            return True
    return False


def _is_blocked_system_path(candidate: Path) -> bool:
    """Return True if *candidate* falls under a blocked system root.

    Honours :data:`_USER_TMP_PREFIXES` carve-outs so per-user tmp directories
    nominally under ``/var`` (``/var/folders`` on macOS, ``/var/tmp`` on
    Linux/macOS) remain valid workspace candidates and reachable file targets.
    """
    api = workspace_api()
    for tmp in api._USER_TMP_PREFIXES:
        if api._is_within(candidate, tmp):
            return False
    for blocked in api._workspace_blocked_roots():
        if api._is_within(candidate, blocked):
            return True
    return False


def _workspace_blocked_resolved_subtrees() -> tuple[Path, ...]:
    roots = list(workspace_api()._workspace_blocked_roots()) + [Path('/private/etc')]
    resolved: list[Path] = []
    for root in roots:
        try:
            p = root.expanduser().resolve()
        except Exception:
            p = root
        if p not in resolved:
            resolved.append(p)
    return tuple(resolved)


def _workspace_blocked_exact_roots() -> tuple[Path, ...]:
    roots = [Path('/'), Path('/private/var')]
    for root in workspace_api()._workspace_blocked_roots():
        try:
            roots.append(root.expanduser().resolve())
        except Exception:
            roots.append(root)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def _is_blocked_workspace_path(candidate: Path, raw_path: str | Path | None = None) -> bool:
    """Return True when candidate points at a known OS/system directory.

    Compare both the original spelling and the resolved path.  This closes the
    macOS /etc -> /private/etc bypass without globally banning temporary pytest
    paths under /private/var/folders.
    """
    api = workspace_api()
    raw = None
    if raw_path not in (None, ""):
        try:
            normalized_posix = api._normalize_posix_path(raw_path)
            raw = Path(normalized_posix) if normalized_posix is not None else api._expanduser_path(raw_path)
        except Exception:
            raw = None

    posix_probe = raw_path if raw_path not in (None, "") else candidate.as_posix()
    if api._is_blocked_posix_workspace_path(posix_probe):
        return True

    exact = api._workspace_blocked_exact_roots()
    if candidate in exact or (raw is not None and raw in api._workspace_blocked_roots()):
        return True

    for tmp in api._USER_TMP_PREFIXES:
        if api._is_within(candidate, tmp) or (raw is not None and api._is_within(raw, tmp)):
            return False

    # Raw paths under literal roots (e.g. /etc/ssh, /var/db) are always blocked.
    if raw is not None:
        for blocked in api._workspace_blocked_roots():
            if api._is_within(raw, blocked):
                return True

    # Resolved subtree checks catch symlink aliases such as /private/etc.  The
    # macOS temp root /private/var/folders is intentionally allowed for pytest
    # and per-user temporary workspaces; other direct /private/var system data
    # such as /private/var/db and /private/var/log remains blocked.
    allowed_private_var = (Path('/private/var/folders'), Path('/private/var/tmp'))
    for blocked in api._workspace_blocked_resolved_subtrees():
        if blocked == Path('/private/var'):
            if candidate == blocked:
                return True
            if any(api._is_within(candidate, allowed) for allowed in allowed_private_var):
                continue
            if api._is_within(candidate, blocked):
                return True
            continue
        if api._is_within(candidate, blocked):
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False



def _strip_surrounding_quotes(path: str) -> str:
    """Strip a single pair of surrounding single or double quotes from a path string.

    macOS Finder's "Copy as Pathname" (Cmd+Option+C) returns paths wrapped in
    single quotes, e.g. ``'/Users/x/Documents/foo'``. Other shells and OS file
    managers do similar things with double quotes. Users routinely paste these
    quoted strings into the Add Space input expecting them to "just work" —
    the only reason they didn't was a missing strip.

    Only paired quotes are stripped (matching opener and closer). One-sided quotes
    are preserved on the slim chance a path legitimately contains a literal quote
    character.
    """
    s = path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s



def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal.

    Both raw ``..`` traversal and symlink escapes are blocked.  Workspace file
    APIs can be reached by browser UI actions and agent/tool calls, so a symlink
    inside the workspace must not expand the trusted workspace boundary to an
    arbitrary host path.
    """
    root_resolved = root.resolve()
    resolved = (root / requested).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {requested}")
    return resolved


# ── Race-safe (TOCTOU) anchored open ─────────────────────────────────────────
# safe_resolve_ws() validates a path, but if callers then re-open by pathname a
# symlink swapped in AFTER the check could still escape the workspace. To close
# that window we open the (already symlink-resolved) target component-by-component
# from the workspace root using openat (dir_fd) + O_NOFOLLOW: every component must
# be a real, non-symlink entry, so a component swapped to a symlink mid-flight is
# refused. Legit in-workspace symlinks still work because safe_resolve_ws() has
# already collapsed them to their real in-workspace target, and we walk that real
# (symlink-free) path. Portable: uses os.supports_dir_fd where available (Linux,
# macOS); on platforms without dir_fd support (Windows — where creating symlinks
# also requires admin) we fall back to a plain pathname open, matching the prior
# behaviour with no regression.

_DIR_FD_OK = os.open in getattr(os, "supports_dir_fd", set())
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def open_anchored_fd(workspace: Path, target: Path, *, want_dir: bool) -> int:
    """Open ``target`` race-safely and return an owned file descriptor.

    ``target`` must be the symlink-resolved path returned by safe_resolve_ws()
    (i.e. already verified to live under the workspace). Raises FileNotFoundError
    if a component is missing / wrong-type, or ValueError if a component was
    swapped to a symlink (escape attempt). Caller owns and must close the fd.
    """
    api = workspace_api()
    root_resolved = workspace.resolve()
    # Relative, symlink-free component list (resolve() already collapsed any links).
    try:
        rel_parts = target.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None

    if not api._DIR_FD_OK:
        # Windows / no openat: fall back to a plain pathname open. No new race
        # protection, but no regression vs the prior path-based behaviour, and
        # symlink creation needs admin on Windows anyway.
        flags = os.O_RDONLY | (api._O_DIRECTORY if want_dir else 0) | api._O_NOFOLLOW
        try:
            return os.open(str(target), flags)
        except OSError:
            raise FileNotFoundError(f"Not found: {target}") from None

    # Open the (trusted) workspace root. root_resolved is canonical (resolve()
    # collapsed any symlinks to REACH it, e.g. macOS /tmp -> /private/tmp), so its
    # final component is legitimately a real directory — O_NOFOLLOW here only fires
    # if the root itself was raced into a symlink after resolve() (escape attempt).
    fd = os.open(str(root_resolved), os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW)
    try:
        for i, part in enumerate(rel_parts):
            is_last = i == len(rel_parts) - 1
            want_directory = (not is_last) or want_dir
            flags = os.O_RDONLY | api._O_NOFOLLOW | (api._O_DIRECTORY if want_directory else 0)
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError:
                # ELOOP (component is a symlink — swapped in) or missing/wrong type.
                raise FileNotFoundError(f"Not found: {target}") from None
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_anchored_create_fd(root: Path, dest: Path) -> int:
    """Create ``dest`` for exclusive writing race-safely, anchored under ``root``.

    Walks from ``root`` via openat + O_NOFOLLOW (creating missing intermediate
    directories with mkdir(dir_fd=...)), then creates the leaf with
    O_CREAT|O_EXCL|O_NOFOLLOW so a symlink raced into any component cannot
    redirect the write outside ``root``. ``dest`` must be the resolved path and
    must not already exist (callers dedup first). Raises ValueError if ``dest``
    is not under ``root``, FileExistsError if it exists, FileNotFoundError if a
    component was swapped to a symlink. Caller owns and must close the returned
    write fd. On platforms without dir_fd support (Windows) falls back to a plain
    exclusive create — no new race protection but no regression.
    """
    api = workspace_api()
    root_resolved = root.resolve()
    try:
        rel_parts = dest.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None
    if not rel_parts:
        raise ValueError(f"Invalid destination: {dest}")

    if not api._DIR_FD_OK:
        # Windows / no openat: create parent dirs then exclusively create the leaf.
        dest.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | api._O_NOFOLLOW, 0o644)

    fd = os.open(str(root_resolved), os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW)
    try:
        for part in rel_parts[:-1]:
            try:
                nfd = os.open(part, os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW, dir_fd=fd)
            except OSError:
                # ELOOP — component swapped to a symlink (escape attempt).
                raise FileNotFoundError(f"Not found: {dest}") from None
            os.close(fd)
            fd = nfd
        return os.open(
            rel_parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | api._O_NOFOLLOW,
            0o644,
            dir_fd=fd,
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def make_anchored_dir(root: Path, dest: Path) -> None:
    """Create directory ``dest`` (and any missing parents) race-safely under ``root``.

    Walks from ``root`` via openat + O_NOFOLLOW, creating each missing component
    with mkdir(dir_fd=...), so a symlink raced into any component cannot make the
    server create directories outside ``root``. Idempotent (existing dirs are
    fine). Raises ValueError if ``dest`` is not under ``root``, FileNotFoundError
    if a component was swapped to a symlink. On platforms without dir_fd support
    (Windows) falls back to a plain Path.mkdir — no regression.
    """
    api = workspace_api()
    root_resolved = root.resolve()
    dest_resolved = dest.resolve()
    if dest_resolved == root_resolved:
        return
    try:
        rel_parts = dest_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None

    if not api._DIR_FD_OK:
        dest.mkdir(parents=True, exist_ok=True)
        return

    fd = os.open(str(root_resolved), os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW)
    try:
        for part in rel_parts:
            try:
                nfd = os.open(part, os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | api._O_DIRECTORY | api._O_NOFOLLOW, dir_fd=fd)
            except OSError:
                # ELOOP — component swapped to a symlink (escape attempt).
                raise FileNotFoundError(f"Not found: {dest}") from None
            os.close(fd)
            fd = nfd
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def open_anchored_write_fd(root: Path, target: Path) -> int:
    """Open existing ``target`` for truncating writes anchored under ``root``."""
    api = workspace_api()
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    flags = os.O_WRONLY | os.O_TRUNC | api._O_NOFOLLOW
    if not api._DIR_FD_OK:
        return os.open(str(target_resolved), flags)

    parent_fd = api.open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        return os.open(rel_parts[-1], flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def unlink_anchored(root: Path, target: Path) -> None:
    """Unlink an existing file anchored under ``root``."""
    api = workspace_api()
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    if not api._DIR_FD_OK:
        target_resolved.unlink()
        return

    parent_fd = api.open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        os.unlink(rel_parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def rmtree_anchored(root: Path, target: Path) -> None:
    """Remove a directory tree anchored under ``root`` without following symlink swaps."""
    api = workspace_api()
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    if not api._DIR_FD_OK:
        shutil.rmtree(target_resolved)
        return

    parent_fd = api.open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        shutil.rmtree(rel_parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def rename_anchored(root: Path, source: Path, dest: Path) -> None:
    """Rename ``source`` to ``dest`` using anchored parent directory fds."""
    api = workspace_api()
    root_resolved = root.resolve()
    source_resolved = source.resolve()
    dest_parent_resolved = dest.parent.resolve()
    try:
        source_parts = source_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {source}") from None
    try:
        dest_parent_resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None
    if not source_parts:
        raise ValueError(f"Invalid source: {source}")
    dest_leaf = dest.name
    if not dest_leaf:
        raise ValueError(f"Invalid destination: {dest}")

    if not api._DIR_FD_OK:
        source_resolved.rename(dest)
        return

    src_parent_fd = api.open_anchored_fd(root_resolved, source_resolved.parent, want_dir=True)
    try:
        dst_parent_fd = api.open_anchored_fd(root_resolved, dest_parent_resolved, want_dir=True)
        try:
            try:
                os.stat(dest_leaf, dir_fd=dst_parent_fd, follow_symlinks=False)
                raise FileExistsError(dest_leaf)
            except FileNotFoundError:
                pass
            os.rename(
                source_parts[-1],
                dest_leaf,
                src_dir_fd=src_parent_fd,
                dst_dir_fd=dst_parent_fd,
            )
        finally:
            os.close(dst_parent_fd)
    finally:
        os.close(src_parent_fd)
