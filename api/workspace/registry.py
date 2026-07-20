"""Workspace identity, profile-scoped registry, and trust resolution.

Workspace lists and last-used workspace are stored per profile. Filesystem
trust policy is applied through the path-safety module before a registered
workspace becomes an active file or Git context.
"""

import json
import logging
import os
import stat
from pathlib import Path, PurePosixPath

from api.config import (
    WORKSPACES_FILE as _GLOBAL_WS_FILE,
    LAST_WORKSPACE_FILE as _GLOBAL_LW_FILE,
    DEFAULT_WORKSPACE as _BOOT_DEFAULT_WORKSPACE,
)
from .path_safety import (
    _expanduser_path,
    _home_path,
    _is_blocked_workspace_path,
    _is_within,
    _normalize_posix_path,
    _posix_is_within,
    _resolve_path,
    _safe_resolve,
    _strip_surrounding_quotes,
)

logger = logging.getLogger(__name__)

def _profile_state_dir() -> Path:
    """Return the webui_state directory for the active profile.

    For the default profile, returns the global STATE_DIR (respects
    HERMES_WEBUI_STATE_DIR env var for test isolation).
    For named profiles, returns {profile_home}/webui_state/.
    """
    try:
        from api.profiles import get_active_profile_name, get_active_hermes_home
        name = get_active_profile_name()
        if name and name != 'default':
            d = get_active_hermes_home() / 'webui_state'
            d.mkdir(parents=True, exist_ok=True)
            return d
    except ImportError:
        logger.debug("Failed to import profiles module, using global state dir")
    return _GLOBAL_WS_FILE.parent


def _workspaces_file() -> Path:
    """Return the workspaces.json path for the active profile."""
    return _profile_state_dir() / 'workspaces.json'


def _last_workspace_file() -> Path:
    """Return the last_workspace.txt path for the active profile."""
    return _profile_state_dir() / 'last_workspace.txt'



def is_remote_terminal_backend(terminal_cfg: dict | None) -> bool:
    """Return True when the active terminal backend runs outside this WebUI host."""
    if not isinstance(terminal_cfg, dict):
        return False
    backend = str(terminal_cfg.get('backend') or '').strip().lower()
    return backend not in ('', 'local')


# Temporary compatibility name for existing workspace callers. New domains use
# the public interface above instead of importing a private workspace helper.
_is_remote_terminal_backend = is_remote_terminal_backend


def _remote_terminal_cwd() -> str | None:
    """Return target-side terminal cwd for remote profiles, without local stat()."""
    try:
        from api.config import get_config

        terminal_cfg = get_config().get('terminal', {})
        if not _is_remote_terminal_backend(terminal_cfg):
            return None
        cwd = str(terminal_cfg.get('cwd') or '').strip()
        if not cwd or cwd == '.':
            return None
        return cwd
    except Exception:
        logger.debug("Failed to read remote terminal cwd", exc_info=True)
        return None


def _remote_terminal_workspace_candidate(path: str | Path) -> Path | None:
    """Return a non-stat'ed target-side Path when it is under terminal.cwd."""
    cwd = _remote_terminal_cwd()
    if not cwd:
        return None
    raw = _strip_surrounding_quotes(str(path)).strip()
    if not raw:
        return None
    if '\x00' in raw or '\x00' in cwd:
        return None
    normalized_raw = _normalize_posix_path(raw)
    normalized_cwd = _normalize_posix_path(cwd)
    if normalized_raw is not None and normalized_cwd is not None:
        posix_candidate = PurePosixPath(normalized_raw)
        posix_base = PurePosixPath(normalized_cwd)
        if _is_blocked_workspace_path(Path(normalized_raw), normalized_raw) or _is_blocked_workspace_path(Path(normalized_cwd), normalized_cwd):
            return None
        if posix_candidate == posix_base or _posix_is_within(posix_candidate, posix_base):
            return _resolve_path(normalized_raw)
        return None
    candidate = _resolve_path(raw)
    base = _resolve_path(cwd)
    if _is_blocked_workspace_path(candidate, raw) or _is_blocked_workspace_path(base, cwd):
        return None
    if candidate == base or _is_within(candidate, base):
        return candidate
    return None


def _profile_default_workspace() -> str:
    """Read the profile's default workspace from its config.yaml.

    Checks keys in priority order:
      1. 'workspace'         — explicit webui workspace key
      2. 'default_workspace' — alternate explicit key
      3. 'terminal.cwd'      — hermes-agent terminal working dir (most common)

    For remote/SSH terminal profiles, ``terminal.cwd`` lives on the target
    machine, not on the WebUI server. In that case return it without a
    server-local existence check so WebUI can send the correct workspace hint
    to the agent/tool backend.

    Falls back to the live DEFAULT_WORKSPACE from api.config.
    """
    try:
        from api.config import get_config
        cfg = get_config()
        terminal_cfg = cfg.get('terminal', {})
        remote_terminal = _is_remote_terminal_backend(terminal_cfg)
        # Explicit webui workspace keys first
        for key in ('workspace', 'default_workspace'):
            ws = cfg.get(key)
            if ws:
                if remote_terminal:
                    return str(ws).strip()
                p = _resolve_path(str(ws))
                if remote_terminal or p.is_dir():
                    return str(p)
        # Fall through to terminal.cwd — the agent's configured working directory
        if isinstance(terminal_cfg, dict):
            cwd = terminal_cfg.get('cwd', '')
            if cwd and str(cwd) not in ('.', ''):
                if remote_terminal:
                    return str(cwd).strip()
                p = _resolve_path(str(cwd))
                if remote_terminal or p.is_dir():
                    return str(p)
    except (ImportError, Exception):
        logger.debug("Failed to load profile default workspace config")
    try:
        from api.config import DEFAULT_WORKSPACE as _LIVE_DEFAULT_WORKSPACE

        return str(_resolve_path(_LIVE_DEFAULT_WORKSPACE))
    except Exception:
        return str(_resolve_path(_BOOT_DEFAULT_WORKSPACE))


# ── Public API ──────────────────────────────────────────────────────────────

def _clean_workspace_list(workspaces: list) -> list:
    """Sanitize a workspace list:
    - Preserve saved paths even when they are currently missing or inaccessible;
      picker state must not be destroyed by a transient stat/permission failure.
    - Remove entries whose paths live inside another profile's directory
      (e.g. ~/.hermes/profiles/X/... should not appear on a different profile).
    - Rename any entry whose name is literally 'default' to 'Home' (avoids
      confusion with the 'default' profile name).
    Returns the cleaned list (may be empty).
    """
    hermes_profiles = (_home_path() / '.hermes' / 'profiles').resolve()
    result = []
    for w in workspaces:
        path = w.get('path', '')
        name = w.get('name', '')
        if not path:
            continue
        p = _safe_resolve(_expanduser_path(path))
        # Skip paths inside a DIFFERENT profile's directory (cross-profile leak).
        # Allow paths inside the CURRENT profile's own directory (e.g. test workspaces
        # created under ~/.hermes/profiles/webui/webui-mvp-test/).
        try:
            p.relative_to(hermes_profiles)
            # p is under ~/.hermes/profiles/ — only skip if it's under a DIFFERENT profile
            try:
                from api.profiles import get_active_hermes_home
                own_profile_dir = get_active_hermes_home().resolve()
                p.relative_to(own_profile_dir)
                # p is under our own profile dir — keep it
            except (ValueError, Exception):
                continue  # under profiles/ but not our own — cross-profile leak, skip
        except ValueError:
            pass  # not under profiles/ at all — keep it
        # Rename confusing 'default' label to 'Home'
        if name.lower() == 'default':
            name = 'Home'
        result.append({'path': str(p), 'name': name})
    return result


def _workspace_access_error(candidate: Path, *, missing_label: str = "Path does not exist") -> str | None:
    """Return a user-facing validation error for an unusable workspace path.

    ``Path.exists()`` can collapse permission/stat failures into a generic falsey
    result on some Python/OS combinations, which produced misleading "does not
    exist" messages for macOS/TCC-denied directories.  Probe with ``stat()`` so
    missing paths, non-directories, and permission-denied paths can be reported
    separately.
    """
    try:
        st = candidate.stat()
    except FileNotFoundError:
        return f"{missing_label}: {candidate}"
    except ValueError as exc:
        # Embedded null byte (or similar invalid path) — .stat() raises ValueError,
        # not OSError. Report as an access error rather than letting it surface as
        # an uncaught 500.
        return f"Cannot access path: {candidate!r}. Invalid path ({exc})."
    except PermissionError as exc:
        return (
            f"Cannot access path: {candidate}. The server process could not inspect "
            f"this directory ({exc}). On macOS, grant Full Disk Access or Files and "
            f"Folders permission to the Hermes/WebUI app or server process, then try again."
        )
    except OSError as exc:
        return f"Cannot access path: {candidate}. The server process could not inspect this path ({exc})."
    if not stat.S_ISDIR(st.st_mode):
        return f"Path is not a directory: {candidate}"
    return None


def _migrate_global_workspaces() -> list:
    """Read the legacy global workspaces.json, clean it, and return the result.

    This is the migration path for users upgrading from a pre-profile version:
    their global file may contain cross-profile entries, test artifacts, and
    stale paths accumulated over time.  We clean it in-place and rewrite it.
    """
    if not _GLOBAL_WS_FILE.exists():
        return []
    try:
        raw = json.loads(_GLOBAL_WS_FILE.read_text(encoding='utf-8'))
        cleaned = _clean_workspace_list(raw)
        if len(cleaned) != len(raw):
            # Rewrite the cleaned version so future reads are already clean
            _GLOBAL_WS_FILE.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        return cleaned
    except Exception:
        return []


def load_workspaces() -> list:
    ws_file = _workspaces_file()
    if ws_file.exists():
        try:
            raw = json.loads(ws_file.read_text(encoding='utf-8'))
            cleaned = _clean_workspace_list(raw)
            if len(cleaned) != len(raw):
                # Persist the cleaned version so stale entries don't keep reappearing
                try:
                    ws_file.write_text(
                        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
                    )
                except Exception:
                    logger.debug("Failed to persist cleaned workspace list")
            return cleaned or [{'path': _profile_default_workspace(), 'name': 'Home'}]
        except Exception:
            logger.debug("Failed to load workspaces from %s", ws_file)
    # No profile-local file yet.
    # For the DEFAULT profile: migrate from the legacy global file (one-time cleanup).
    # For NAMED profiles: always start clean with just their own workspace.
    try:
        from api.profiles import get_active_profile_name
        is_default = get_active_profile_name() in ('default', None)
    except ImportError:
        is_default = True
    if is_default:
        migrated = _migrate_global_workspaces()
        if migrated:
            return migrated
    # Fresh start: single entry from the profile's configured workspace, labeled "Home"
    return [{'path': _profile_default_workspace(), 'name': 'Home'}]


def save_workspaces(workspaces: list) -> None:
    ws_file = _workspaces_file()
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps(workspaces, ensure_ascii=False, indent=2), encoding='utf-8')


def get_profile_default_workspace() -> str:
    """Resolve the ACTIVE PROFILE's default workspace, never the global file.

    Like get_last_workspace() but WITHOUT the global ``_GLOBAL_LW_FILE``
    fallback: for a named profile that has not yet written its own
    profile-scoped ``last_workspace.txt``, that global fallback would leak the
    *global* last-workspace instead of the profile's configured workspace —
    which is exactly the #5169 bug (the composer chip on a blank new-chat page
    showing the wrong/global workspace for a named profile). Used by
    ``GET /api/profile/active`` so a cold boot under a profile cookie reflects
    the profile's own configured working directory.

    Priority: profile-scoped ``last_workspace.txt`` -> profile ``config.yaml``
    ``workspace``/``default_workspace`` -> ``terminal.cwd`` -> process default.
    """
    remote_cwd = _remote_terminal_cwd()

    def _valid(raw: str) -> str | None:
        if not raw:
            return None
        if remote_cwd:
            if _remote_terminal_workspace_candidate(raw) is not None:
                return raw
            return None
        if Path(raw).is_dir():
            return raw
        return None

    lw_file = _last_workspace_file()
    if lw_file.exists():
        try:
            p = _valid(lw_file.read_text(encoding='utf-8').strip())
            if p:
                return p
        except Exception:
            logger.debug("Failed to read profile last workspace from %s", lw_file)
    return _profile_default_workspace()


def get_last_workspace() -> str:
    remote_cwd = _remote_terminal_cwd()

    def valid_last_workspace(raw: str) -> str | None:
        if not raw:
            return None
        if remote_cwd:
            # For remote/SSH profiles, last_workspace is target-side state. Do
            # not accept stale server-local paths merely because they exist on
            # the WebUI host; require the value to stay under terminal.cwd.
            if _remote_terminal_workspace_candidate(raw) is not None:
                return raw
            return None
        if Path(raw).is_dir():
            return raw
        return None

    lw_file = _last_workspace_file()
    if lw_file.exists():
        try:
            p = valid_last_workspace(lw_file.read_text(encoding='utf-8').strip())
            if p:
                return p
        except Exception:
            logger.debug("Failed to read last workspace from %s", lw_file)
    # Fallback: try global file
    if _GLOBAL_LW_FILE.exists():
        try:
            p = valid_last_workspace(_GLOBAL_LW_FILE.read_text(encoding='utf-8').strip())
            if p:
                return p
        except Exception:
            logger.debug("Failed to read global last workspace")
    return _profile_default_workspace()


def set_last_workspace(path: str) -> None:
    try:
        lw_file = _last_workspace_file()
        lw_file.parent.mkdir(parents=True, exist_ok=True)
        lw_file.write_text(str(path), encoding='utf-8')
    except Exception:
        logger.debug("Failed to set last workspace")



def _trusted_workspace_roots() -> list[Path]:
    roots: list[Path] = []

    def add(candidate: str | Path | None) -> None:
        if candidate in (None, ""):
            return
        try:
            p = _resolve_path(candidate)
        except Exception:
            return
        if not p.exists() or not p.is_dir():
            return
        if _is_blocked_workspace_path(p, candidate):
            return
        if p not in roots:
            roots.append(p)

    add(_home_path())
    add(_BOOT_DEFAULT_WORKSPACE)
    for w in load_workspaces():
        add(w.get("path"))
    roots.sort(key=lambda p: len(str(p)))
    return roots


def list_workspace_suggestions(prefix: str = "", limit: int = 12) -> list[str]:
    """Return workspace path suggestions under trusted roots only.

    Suggestions are limited to directories under one of:
      - Path.home()
      - the boot default workspace
      - already-saved workspace roots

    Arbitrary system prefixes return an empty list rather than an error so the
    UI can safely autocomplete while the user types.
    """
    roots = _trusted_workspace_roots()
    if not roots:
        return []

    raw = (prefix or "").strip()
    if not raw:
        return [str(p) for p in roots[:limit]]

    if raw.startswith("~"):
        target = _expanduser_path(raw)
    elif Path(raw).is_absolute():
        target = Path(raw)
    else:
        target = _home_path() / raw

    try:
        match_target = _resolve_path(target)
    except Exception:
        match_target = target

    normalized = str(match_target)
    normalized_lower = normalized.lower()
    preserve_tilde = raw.startswith("~")
    home_root: Path | None = None
    if preserve_tilde:
        try:
            home_root = _home_path()
        except Exception:
            home_root = None
    suggestions: list[str] = []

    def format_suggestion(path: Path) -> str:
        if preserve_tilde and home_root is not None:
            try:
                rel = path.resolve().relative_to(home_root)
                if str(rel) == ".":
                    return "~"
                return "~/" + rel.as_posix()
            except (OSError, ValueError):
                pass
        return str(path)

    def add(path: Path) -> None:
        value = format_suggestion(path)
        if value not in suggestions:
            suggestions.append(value)

    # If the user is typing a partial trusted root like /Users/xuef..., suggest
    # the matching trusted roots without scanning arbitrary system parents.
    for root in roots:
        if str(root).lower().startswith(normalized_lower):
            add(root)

    in_root = [
        root
        for root in roots
        if normalized == str(root) or normalized.startswith(str(root) + os.sep)
    ]
    if not in_root:
        return suggestions[:limit]

    anchor_root = max(in_root, key=lambda p: len(str(p)))
    ends_with_sep = raw.endswith(os.sep) or raw.endswith('/')
    parent = target if ends_with_sep else target.parent
    leaf = '' if ends_with_sep else target.name
    show_hidden = leaf.startswith('.')

    try:
        parent_resolved = _resolve_path(parent)
    except Exception:
        return suggestions[:limit]

    if not parent_resolved.exists() or not parent_resolved.is_dir():
        return suggestions[:limit]
    if not _is_within(parent_resolved, anchor_root):
        return suggestions[:limit]

    leaf_lower = leaf.lower()
    try:
        children = sorted(parent_resolved.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return suggestions[:limit]

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith('.') and not show_hidden:
            continue
        if leaf_lower and not child.name.lower().startswith(leaf_lower):
            continue
        add(child.resolve())
        if len(suggestions) >= limit:
            break
    return suggestions[:limit]


def resolve_trusted_workspace(path: str | Path | None = None) -> Path:
    """Resolve and validate a workspace path.

    A path is trusted if it satisfies at least one of:
      (A) It is under the user's home directory (Path.home()).
          Works cross-platform: ~/... on Linux/macOS, C:\\Users\\... on Windows.
      (B) It is already in the profile's saved workspace list.
          This covers self-hosted deployments where workspaces live outside home
          (e.g. /data/projects, /opt/workspace) — once a workspace is saved by
          an admin, it can be reused without re-validation.

    Additionally enforced regardless of (A)/(B):
      1. The path must exist.
      2. The path must be a directory.
      3. The path must not be a known system root (/etc, /usr, /var, /bin, /sbin,
         /boot, /proc, /sys, /dev, /root on Linux/macOS; Windows system dirs).
         This prevents even admin-saved workspaces from pointing at OS internals.

    None/empty path falls back to the boot-time DEFAULT_WORKSPACE, which is always
    trusted (it was validated at server startup).
    """
    if path in (None, ""):
        return _resolve_path(_BOOT_DEFAULT_WORKSPACE)

    candidate = _resolve_path(path)

    access_error = _workspace_access_error(candidate)
    remote_candidate = _remote_terminal_workspace_candidate(path)
    if access_error:
        # For remote terminal profiles, workspace paths belong to the target
        # machine. Allow paths under terminal.cwd so session switching can
        # update the workspace hint even though this WebUI host cannot stat
        # the target-side path.
        if remote_candidate is None:
            raise ValueError(access_error)

    if remote_candidate is not None:
        return remote_candidate

    # (A) Trusted if under the user's home directory — cross-platform via Path.home()
    # Must be checked before system roots to allow symlinks like /var/home.
    _home = _home_path()
    if _home != Path("/"):
        try:
            candidate.relative_to(_home)
            return candidate
        except ValueError:
            pass

    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

    # (B) Trusted if already in the saved workspace list — covers non-home installs
    try:
        saved = load_workspaces()
        saved_paths = {_resolve_path(w["path"]) for w in saved if w.get("path")}
        if candidate in saved_paths:
            return candidate
    except Exception:
        pass

    # (C) Trusted if it is equal to or under the boot-time DEFAULT_WORKSPACE.
    #     In Docker deployments HERMES_WEBUI_DEFAULT_WORKSPACE is often set to a
    #     volume mount outside the user's home (e.g. /data/workspace).  That path
    #     was already validated at server startup, so any sub-path of it is safe
    #     without requiring the user to add it to the workspace list manually.
    try:
        boot_default = _resolve_path(_BOOT_DEFAULT_WORKSPACE)
        candidate.relative_to(boot_default)
        return candidate
    except ValueError:
        pass

    raise ValueError(
        f"Path is outside the user home directory, not in the saved workspace "
        f"list, and not under the default workspace: {candidate}. "
        f"Add it via Settings → Workspaces first."
    )





def validate_workspace_to_add(path: str) -> Path:
    """Validate a path for *adding* to the workspace list (less restrictive than resolve_trusted_workspace).

    When a user explicitly adds a new workspace path, we trust their intent — they
    have console or filesystem access to that path and are consciously registering it.
    We only block: non-existent paths, non-directories, and known system roots.

    The stricter ``resolve_trusted_workspace`` is used when *using* an existing workspace
    (file reads/writes) to prevent path traversal after the list is built.

    Surrounding quotes (single or double) are stripped before validation —
    macOS Finder's "Copy as Pathname" wraps paths in single quotes by default,
    and users routinely paste those into the Add Space input.
    """
    path = _strip_surrounding_quotes(path)
    candidate = _resolve_path(path)

    access_error = _workspace_access_error(candidate)
    remote_candidate = _remote_terminal_workspace_candidate(path)
    if access_error:
        # Remote terminal profiles validate workspace existence on the target
        # machine, not on the WebUI server. Permit target-side paths under
        # terminal.cwd.
        if remote_candidate is None:
            raise ValueError(access_error)

    if remote_candidate is not None:
        return remote_candidate

    # Home directory is always trusted regardless of where it lives on disk
    # (e.g. /var/home/... on systemd-homed Fedora/RHEL).
    _home = _home_path()
    if _home != Path("/") and _is_within(candidate, _home):
        return candidate

    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

    return candidate
