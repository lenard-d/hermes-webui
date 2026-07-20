"""
Hermes Web UI -- Profile state management.
Wraps hermes_cli.profiles to provide profile switching for the web UI.

The web UI maintains a process-level "active profile" that determines which
HERMES_HOME directory is used for config, skills, memory, cron, and API keys.
Profile switches update os.environ['HERMES_HOME'] and monkey-patch module-level
cached paths in hermes-agent modules (skills_tool, skill_manager_tool,
cron/jobs) that snapshot HERMES_HOME at import time.
"""
import json  # noqa: F401 - historical compatibility-facade export
import logging
import os
import re
import shutil  # noqa: F401 - historical compatibility-facade export
import sys
import threading
import time  # noqa: F401 - rebound catalog implementation dependency
from contextlib import contextmanager  # noqa: F401 - compatibility export
from pathlib import Path
from typing import Optional  # noqa: F401 - compatibility export

import yaml  # noqa: F401 - historical compatibility-facade export

from api.sessions import publish_session_list_changed  # noqa: F401 - cron adapter seam
logger = logging.getLogger(__name__)

# ── Constants (match hermes_cli.profiles upstream) ─────────────────────────
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_PROFILE_DIRS = [
    'memories', 'sessions', 'skills', 'skins',
    'logs', 'plans', 'workspace', 'cron',
]
_CLONE_CONFIG_FILES = ['config.yaml', '.env', 'SOUL.md']

# ── Snapshot startup env before profile init / dotenv reload mutates it ───────
# _is_isolated_profile_mode() needs startup HERMES_HOME, not the value after
# init_profile_state() rewrites it. The opt-in flag is also an operator-level
# startup control: a pinned profile's .env may be loaded into live os.environ
# later, but must not be able to change whether the process is isolated.
_INITIAL_HERMES_HOME = os.getenv('HERMES_HOME', '').strip()
_INITIAL_ISOLATED_PROFILE_OPT_IN = os.getenv('HERMES_WEBUI_ISOLATED_PROFILE', '').strip().lower()
_ISOLATED_SYMLINK_WARNING_EMITTED = False
_ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED = False
_ISOLATED_PROFILE_TRUTHY_VALUES = frozenset({'1', 'true', 'yes', 'on'})

# ── Module state ────────────────────────────────────────────────────────────
_active_profile = 'default'
_profile_lock = threading.Lock()
_loaded_profile_env_keys: set[str] = set()

# Thread-local profile context: set per-request by server.py, cleared after.
# Enables per-client profile isolation (issue #798) — each HTTP request thread
# reads its own profile from the hermes_profile cookie instead of the
# process-global _active_profile.
_tls = threading.local()

_SKILL_HOME_MODULES = ("tools.skills_tool", "tools.skill_manager_tool")


from api.profiles import runtime as _runtime_scope

snapshot_skill_home_modules = _runtime_scope.snapshot_skill_home_modules


def patch_skill_home_modules(home: Path) -> None:
    """Patch already-imported skill modules without importing under env locks."""
    for module_name in _SKILL_HOME_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        try:
            module.HERMES_HOME = home
            module.SKILLS_DIR = home / "skills"
        except AttributeError:
            logger.debug("Failed to patch %s module", module_name)


restore_skill_home_modules = _runtime_scope.restore_skill_home_modules


def _unwrap_profile_home_to_base(home: Path) -> Path:
    """Return the base Hermes home when *home* is already a named profile dir."""
    if home.parent.name == 'profiles':
        return home.parent.parent
    return home


# Env keys a pinned profile's .env may NOT override via _reload_dotenv() — these
# are operator/deployment-level postures, not per-profile toggles. Letting a
# profile .env set HERMES_WEBUI_ISOLATED_PROFILE=0 would let a contained user
# escape isolation (#4589).
_PROTECTED_ENV_KEYS = frozenset({'HERMES_WEBUI_ISOLATED_PROFILE'})


def _isolated_profile_opt_in() -> bool:
    """Return True only when isolated single-profile mode is EXPLICITLY enabled.
    Isolated mode is an intentional multi-user deployment posture (each user is
    pinned to one profile and cross-profile operations are rejected). It must be
    opted into with ``HERMES_WEBUI_ISOLATED_PROFILE`` — it is NEVER inferred from
    the ``HERMES_HOME`` shape alone, because a normal single-user who runs under a
    named profile produces the byte-identical ``*/profiles/<name>`` shape (the
    Hermes Agent launcher exports ``HERMES_HOME=~/.hermes/profiles/<name>`` for any
    active named profile). Keying isolation off the shape alone therefore breaks
    profile switching for ordinary single-user deployments (#4586).

    Accepts the usual truthy values; default (unset/empty/falsey) is OFF.

    Security: this reads the startup snapshot, not live ``os.environ``. A pinned
    profile's ``.env`` is loaded after import, so live env can be profile-owned;
    the opt-in must remain the operator/launcher posture captured at process
    start (#4590). ``_reload_dotenv()`` and the runtime env paths still filter the
    key as defense-in-depth, but detection does not depend on that filtering.
    """
    return _INITIAL_ISOLATED_PROFILE_OPT_IN in _ISOLATED_PROFILE_TRUTHY_VALUES


def _warn_if_profile_shape_without_isolated_opt_in() -> None:
    """Log once when HERMES_HOME looks pinned but startup opt-in is absent."""
    global _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED
    if _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED:
        return
    hermes_home = _INITIAL_HERMES_HOME
    if not hermes_home:
        return
    p = Path(hermes_home).expanduser()
    if p.parent.name != 'profiles' or not p.name:
        return
    logger.warning(
        "HERMES_HOME points at a profile directory (%s), but "
        "HERMES_WEBUI_ISOLATED_PROFILE was not enabled at startup; isolated "
        "profile mode stays off and normal multi-profile switching remains enabled.",
        p,
    )
    _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED = True


def _is_isolated_profile_mode() -> bool:
    """Detect isolated single-profile mode.

    Returns True only when BOTH conditions hold:
      1. ``HERMES_WEBUI_ISOLATED_PROFILE`` is explicitly enabled (the PRIMARY
         gate — see _isolated_profile_opt_in), AND
      2. HERMES_HOME at startup points at a concrete profile subdirectory
         (e.g., ~/.hermes/profiles/user1) rather than the base home.

    Why the explicit flag is required (#4586 regression fix): the
    ``*/profiles/<name>`` shape alone CANNOT distinguish an intentional
    multi-user isolation deployment from an ordinary single-user running under a
    named profile — the Hermes Agent launcher sets
    ``HERMES_HOME=~/.hermes/profiles/<name>`` for any active named profile, so the
    two cases are byte-identical at the env-var level. Inferring isolation from
    the shape alone (the v0.51.528 behaviour from #2698) wrongly pinned ordinary
    single-user deployments to one profile and disabled profile switching. The
    multi-user wrapper that genuinely wants isolation now sets the explicit flag;
    everyone else is never caught. The shape stays as a secondary requirement so
    a stray flag without a profile-shaped HERMES_HOME does not engage isolation.

    Uses _INITIAL_HERMES_HOME (snapshotted at import time) to detect the shape,
    not the current os.environ value. init_profile_state() overwrites HERMES_HOME
    at startup, which would disable detection if we read it here.
    """
    # PRIMARY gate: explicit startup opt-in. Default OFF → a normal named-profile
    # launch is never treated as isolated, so profile switching keeps working
    # (#4586). Read the snapshot, not live os.environ, so profile .env reloads
    # cannot silently flip the deployment posture (#4590).
    if not _isolated_profile_opt_in():
        _warn_if_profile_shape_without_isolated_opt_in()
        return False

    hermes_home = _INITIAL_HERMES_HOME
    if not hermes_home:
        return False

    p = Path(hermes_home).expanduser()
    # SECONDARY requirement: HERMES_HOME must look like ~/.hermes/profiles/<name>
    # i.e., parent dir is named 'profiles' and grandparent exists.
    if p.parent.name == 'profiles' and p.parent.parent.exists():
        return True
    if p.is_symlink():
        global _ISOLATED_SYMLINK_WARNING_EMITTED
        if not _ISOLATED_SYMLINK_WARNING_EMITTED:
            logger.warning(
                "HERMES_WEBUI_ISOLATED_PROFILE is set but HERMES_HOME %s does not "
                "literally match */profiles/<name>; isolated profile mode stays off "
                "unless the literal profile path is used.",
                p,
            )
            _ISOLATED_SYMLINK_WARNING_EMITTED = True
    return False


def _isolated_profile_name() -> str:
    """Return the profile directory name from _INITIAL_HERMES_HOME."""
    return Path(_INITIAL_HERMES_HOME).expanduser().name


def _resolve_base_hermes_home() -> Path:
    """Return the BASE ~/.hermes directory — the root that contains profiles/.

    This is intentionally distinct from HERMES_HOME, which tracks the *active
    profile's* home and changes on every profile switch.  The base dir must
    always point to the top-level .hermes regardless of which profile is active.

    Resolution order:
      1. HERMES_BASE_HOME env var (set explicitly, highest priority)
      2. HERMES_HOME env var — but only if it does NOT look like a profile subdir
         (i.e. its parent is not named 'profiles').  This handles test isolation
         where HERMES_HOME is set to an isolated test state dir.
      3. ~/.hermes (always-correct default)

    The bug this prevents: if HERMES_HOME has already been mutated to
    /home/user/.hermes/profiles/webui (by init_profile_state at startup),
    reading it here would make _DEFAULT_HERMES_HOME point to that subdir,
    causing switch_profile('webui') to look for
    /home/user/.hermes/profiles/webui/profiles/webui — which doesn't exist.

    HERMES_BASE_HOME normally points at the base home already, but isolated
    single-profile WebUI deployments can provide /base/profiles/<name> there as
    well.  Normalize both env vars through the same helper so active-profile
    and per-request resolution share one base-root contract (#749).
    """
    # Explicit override for tests or unusual setups
    base_override = os.getenv('HERMES_BASE_HOME', '').strip()
    if base_override:
        return _unwrap_profile_home_to_base(Path(base_override).expanduser())

    hermes_home = os.getenv('HERMES_HOME', '').strip()
    if hermes_home:
        p = Path(hermes_home).expanduser()
        # If HERMES_HOME points to a profiles/ subdir, walk up two levels to the base
        return _unwrap_profile_home_to_base(p)

    # Platform default. On Windows this includes the #2905 migration-safety
    # fallback (prefer the populated legacy %USERPROFILE%\.hermes over an
    # empty %LOCALAPPDATA%\hermes). Import the shared path helper directly
    # instead of importing api.config here; api.config imports profiles during
    # startup, so going through config creates a partial-module circular import
    # when api.profiles is imported first.
    from api.paths import _platform_default_hermes_home

    return _platform_default_hermes_home()

_DEFAULT_HERMES_HOME = _resolve_base_hermes_home()


def _read_active_profile_file() -> str:
    """Read the sticky active profile from ~/.hermes/active_profile."""
    ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
    if ap_file.exists():
        try:
            name = ap_file.read_text(encoding="utf-8").strip()
            if name:
                return name
        except Exception:
            logger.debug("Failed to read active profile file")
    return 'default'


# ── Public API ──────────────────────────────────────────────────────────────

# ── Root-profile resolution (#1612) ────────────────────────────────────────
#
# Hermes Agent allows the root/default profile (~/.hermes itself) to have a
# display name other than the legacy literal 'default'.  When that happens,
# WebUI must NOT resolve the display name as ~/.hermes/profiles/<name> — that
# directory doesn't exist, and every site that does `if name == 'default':`
# will fall through to the wrong filesystem path.
#
# `_is_root_profile(name)` answers "does this name resolve to ~/.hermes?" and
# is the canonical replacement for scattered `if name == 'default':` checks
# in switch_profile, get_active_hermes_home, _validate_profile_name, etc.
#
# Cost note: list_profiles_api() shells out via hermes_cli (non-trivial), so
# we memoize the lookup. The cache is invalidated whenever profiles are
# created, deleted, renamed, or cloned — i.e. on every mutation site we
# control.
_root_profile_name_cache: set[str] = {'default'}
_root_profile_name_cache_lock = threading.Lock()
_root_profile_name_cache_loaded = False


def _invalidate_root_profile_cache() -> None:
    """Drop the memoized root-profile-name set.

    Called whenever profile metadata might have changed: create, clone,
    delete, rename. The next _is_root_profile() call repopulates from
    list_profiles_api().
    """
    global _root_profile_name_cache_loaded
    with _root_profile_name_cache_lock:
        _root_profile_name_cache.clear()
        _root_profile_name_cache.add('default')
        _root_profile_name_cache_loaded = False


def _is_root_profile(name: str) -> bool:
    """True if *name* resolves to the Hermes Agent root profile (~/.hermes).

    Matches the legacy 'default' alias plus any name where list_profiles_api()
    reports is_default=True. Memoized; call _invalidate_root_profile_cache()
    after mutating profile metadata.
    """
    global _root_profile_name_cache_loaded
    if not name:
        return False
    if name == 'default':
        return True
    with _root_profile_name_cache_lock:
        if _root_profile_name_cache_loaded:
            return name in _root_profile_name_cache
    # Cache miss — populate from list_profiles_api(). Done outside the lock to
    # avoid holding it across a hermes_cli subprocess call.
    try:
        infos = list_profiles_api()
    except Exception:
        logger.debug("Failed to list profiles for root-profile lookup", exc_info=True)
        return False
    with _root_profile_name_cache_lock:
        _root_profile_name_cache.clear()
        _root_profile_name_cache.add('default')
        for p in infos:
            try:
                if p.get('is_default') and p.get('name'):
                    _root_profile_name_cache.add(p['name'])
            except (AttributeError, TypeError):
                continue
        _root_profile_name_cache_loaded = True
        return name in _root_profile_name_cache


def is_root_profile(name: str) -> bool:
    """Return whether a logical profile name refers to the base Hermes home."""
    return _is_root_profile(name)


def is_valid_profile_id(name: object) -> bool:
    """Return whether *name* is a syntactically valid named-profile identifier."""
    return isinstance(name, str) and bool(_PROFILE_ID_RE.fullmatch(name))


def _profiles_match(row_profile, active_profile) -> bool:
    """Return True if a session/project row's profile matches the active profile.

    Treats both the literal alias 'default' and any renamed-root display name
    (per _is_root_profile) as equivalent, so legacy rows tagged 'default'
    still surface when the user has renamed the root profile to e.g. 'kinni',
    and vice versa.

    A row with no profile (`None` or empty string) is treated as belonging to
    the root profile — that's the convention used by the legacy backfill at
    api/sessions/store.py::all_sessions, and matches the default seen in
    `static/sessions.js` (`S.activeProfile||'default'`).

    Originally lived in api/routes.py; relocated here so both routes.py and
    out-of-process consumers (mcp_server.py) can import the canonical helper
    instead of duplicating the body. See #1614 for the visibility model.
    """
    row = row_profile or 'default'
    active = active_profile or 'default'
    if row == active:
        return True
    # Cross-alias the renamed root.
    if _is_root_profile(row) and _is_root_profile(active):
        return True
    return False


profiles_match = _profiles_match


def get_active_profile_name() -> str:
    """Return the currently active profile name.

    Priority:
      1. Isolated-profile deployment name from the configured HERMES_HOME path
      2. Thread-local (set per-request from hermes_profile cookie) — issue #798
      3. Process-level default (_active_profile)
    """
    if _is_isolated_profile_mode():
        return _isolated_profile_name()
    tls_name = getattr(_tls, 'profile', None)
    if tls_name is not None:
        return tls_name
    return _active_profile


def set_request_profile(name: str) -> None:
    """Set the per-request profile context for this thread.

    Called by server.py at the start of each request when a hermes_profile
    cookie is present.  Always paired with clear_request_profile() in a
    finally block so the thread-local is released after the request.
    """
    _tls.profile = name


def clear_request_profile() -> None:
    """Clear the per-request profile context for this thread.

    Called by server.py in the finally block of do_GET / do_POST.
    Safe to call even if set_request_profile() was never called.
    """
    _tls.profile = None


def _resolve_profile_home_for_name(name: str) -> Path:
    """Resolve a logical profile name to its Hermes home path.

    Root/default aliases resolve to _DEFAULT_HERMES_HOME.  Valid named profiles
    resolve to _DEFAULT_HERMES_HOME/profiles/<name> even when the directory has
    not been created yet; the agent layer may create it on first use.  Invalid
    names fall back to the base home so traversal-shaped cookie values cannot
    influence filesystem paths.
    """
    # In isolated mode, every logical profile lookup clamps to the configured
    # startup HERMES_HOME so callers cannot resolve a foreign profile path.
    if _is_isolated_profile_mode():
        isolated_name = _isolated_profile_name()
        isolated_home = Path(_INITIAL_HERMES_HOME).expanduser()
        if name and not _profiles_match(name, isolated_name):
            logger.warning(
                "Ignoring profile lookup %r in isolated profile mode; using pinned profile %r",
                name, isolated_name,
            )
        return isolated_home
    if not name or _is_root_profile(name):
        return _DEFAULT_HERMES_HOME
    if not _PROFILE_ID_RE.fullmatch(name):
        return _DEFAULT_HERMES_HOME
    return _resolve_named_profile_home(name)


def get_active_hermes_home() -> Path:
    """Return the HERMES_HOME path for the currently active profile.

    Uses get_active_profile_name() so per-request TLS context (issue #798)
    is respected, not just the process-level global.
    """
    if _is_isolated_profile_mode():
        return Path(_INITIAL_HERMES_HOME).expanduser()
    return _resolve_profile_home_for_name(get_active_profile_name())



# Cron libraries cache process-global Hermes paths. The adapter owns their
# serialized setup/restore lifecycle while this facade retains the lock seam.
_cron_env_lock = threading.Lock()

from api.profiles import cron as _cron_scope

_cron_profile_context_depth = _cron_scope._cron_profile_context_depth
_push_cron_profile_context_depth = _cron_scope._push_cron_profile_context_depth
_pop_cron_profile_context_depth = _cron_scope._pop_cron_profile_context_depth
_home_for_scheduled_cron_job = _cron_scope._home_for_scheduled_cron_job
install_cron_scheduler_profile_isolation = _cron_scope.install_cron_scheduler_profile_isolation


class cron_profile_context_for_home(_cron_scope.CronProfileContextForHome):
    """Compatibility facade for an explicit-home cron scope."""

    def __init__(self, home: Path):
        super().__init__(home)


class cron_profile_context(_cron_scope.CronProfileContext):
    """Compatibility facade for the request-active cron scope."""

    def __init__(self):
        super().__init__()


def get_hermes_home_for_profile(name: str) -> Path:
    """Return the HERMES_HOME Path for *name* without mutating any process state.

    Safe to call from per-request context (streaming, session creation) because
    it reads only the filesystem — it never touches os.environ, module-level
    cached paths, or the process-level _active_profile global.

    Falls back to _DEFAULT_HERMES_HOME (same as 'default') when *name* is None,
    empty, 'default', or does not match the profile-name format (rejects path
    traversal such as '../../etc').
    """
    return _resolve_profile_home_for_name(name)


_TERMINAL_ENV_MAPPINGS = {
    "backend": "TERMINAL_ENV",
    "env_type": "TERMINAL_ENV",
    "cwd": "TERMINAL_CWD",
    "timeout": "TERMINAL_TIMEOUT",
    "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
    "modal_mode": "TERMINAL_MODAL_MODE",
    "docker_image": "TERMINAL_DOCKER_IMAGE",
    "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
    "docker_env": "TERMINAL_DOCKER_ENV",
    "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
    "modal_image": "TERMINAL_MODAL_IMAGE",
    "daytona_image": "TERMINAL_DAYTONA_IMAGE",
    "container_cpu": "TERMINAL_CONTAINER_CPU",
    "container_memory": "TERMINAL_CONTAINER_MEMORY",
    "container_disk": "TERMINAL_CONTAINER_DISK",
    "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
    "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
    "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
    "ssh_host": "TERMINAL_SSH_HOST",
    "ssh_user": "TERMINAL_SSH_USER",
    "ssh_port": "TERMINAL_SSH_PORT",
    "ssh_key": "TERMINAL_SSH_KEY",
    "ssh_persistent": "TERMINAL_SSH_PERSISTENT",
    "local_persistent": "TERMINAL_LOCAL_PERSISTENT",
}

_BLOCKED_RUNTIME_ENV_KEYS = {
    "HOME",
    "PATH",
    "PWD",
    "SHELL",
    "USER",
    "LOGNAME",
    "SHLVL",
    "OLDPWD",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "LD_LIBRARY_PATH",
    "HERMES_WEBUI_ISOLATED_PROFILE",
}

# Fail-closed credential floor beyond hermes_cli.auth.PROVIDER_REGISTRY.
_NON_REGISTRY_AGENT_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "CUSTOM_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AZURE_ANTHROPIC_KEY",
    "AZURE_FOUNDRY_API_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "MSI_ENDPOINT",
    "MSI_SECRET",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)

# Capability probes and loaded-key ownership remain facade state so reloads and
# historical monkeypatches observe the same authoritative values.
_secret_scope_available = None
_hermes_home_override_available = None

_stringify_env_value = _runtime_scope._stringify_env_value
get_profile_runtime_env = _runtime_scope.get_profile_runtime_env
filter_runtime_env_for_gateway_parity = _runtime_scope.filter_runtime_env_for_gateway_parity
_agent_registry_credential_env_names = _runtime_scope._agent_registry_credential_env_names
_profile_secret_env_names = _runtime_scope._profile_secret_env_names
profile_secret_env_names = _profile_secret_env_names
_apply_profile_env_to_process = _runtime_scope._apply_profile_env_to_process
_resolve_secret_scope_module = _runtime_scope._resolve_secret_scope_module
_resolve_hermes_home_override = _runtime_scope._resolve_hermes_home_override
_profile_env_for_background_worker_impl = _runtime_scope.profile_env_for_background_worker
_profile_env_for_active_request_readonly_impl = _runtime_scope.profile_env_for_active_request_readonly
_profile_env_for_active_request_impl = _runtime_scope.profile_env_for_active_request
_profile_scope_for_detached_worker_impl = _runtime_scope.profile_scope_for_detached_worker


@contextmanager
def profile_env_for_background_worker(
    session,
    purpose: str = "background worker",
    logger_override: Optional[logging.Logger] = None,
):
    """Temporarily route detached worker config reads through a profile."""
    with _profile_env_for_background_worker_impl(
        session, purpose, logger_override=logger_override
    ):
        yield


@contextmanager
def profile_env_for_active_request_readonly(
    purpose: str = "provider/model read",
    logger_override: Optional[logging.Logger] = None,
):
    """Apply the active profile only to context-local read paths."""
    with _profile_env_for_active_request_readonly_impl(
        purpose, logger_override=logger_override
    ):
        yield


@contextmanager
def profile_env_for_active_request(
    purpose: str = "active request",
    logger_override: Optional[logging.Logger] = None,
):
    """Apply the active profile through the legacy process-mirrored path."""
    with _profile_env_for_active_request_impl(
        purpose, logger_override=logger_override
    ):
        yield


@contextmanager
def profile_scope_for_detached_worker(
    profile_name,
    purpose: str = "detached worker",
    logger_override: Optional[logging.Logger] = None,
):
    """Bind request TLS and profile env on a detached worker thread."""
    with _profile_scope_for_detached_worker_impl(
        profile_name, purpose, logger_override=logger_override
    ):
        yield


_set_hermes_home = _runtime_scope._set_hermes_home
_reload_dotenv = _runtime_scope._reload_dotenv


def init_profile_state() -> None:
    """Initialize profile state at server startup.

    Reads ~/.hermes/active_profile, sets HERMES_HOME env var, patches
    module-level cached paths.  Called once from config.py after imports.
    """
    global _active_profile
    if _is_isolated_profile_mode():
        _active_profile = _isolated_profile_name()
        home = Path(_INITIAL_HERMES_HOME).expanduser()
    else:
        _active_profile = _read_active_profile_file()
        home = get_active_hermes_home()
    _set_hermes_home(home)
    install_cron_scheduler_profile_isolation()
    _reload_dotenv(home)


def switch_profile(name: str, *, process_wide: bool = True) -> dict:
    """Switch the active profile.

    Validates the profile exists, updates process state, patches module caches,
    reloads .env, and reloads config.yaml.

    In isolated profile mode, switching to a different profile is rejected (403).
    Switching to the isolated profile itself is allowed (idempotent).

    Args:
        name: Profile name to switch to.
        process_wide: If True (default), updates the process-global
            _active_profile.  Set to False for per-client switches from the
            WebUI where the profile is managed via cookie + thread-local (#798).

    Returns: {'profiles': [...], 'active': name}
    Raises ValueError when profile doesn't exist, RuntimeError when agent is running,
    PermissionError in isolated mode for cross-profile switches.
    """
    global _active_profile

    # In isolated profile mode, reject switching to other profiles
    if _is_isolated_profile_mode():
        active = _isolated_profile_name()
        if name != active:
            raise PermissionError(
                f"Profile switching is not allowed in isolated profile mode. "
                f"Currently pinned to profile '{active}'."
            )

    # Import here to avoid circular import at module load
    from api.config import STREAMS, STREAMS_LOCK, reload_config

    # Process-wide profile switches mutate HERMES_HOME, module-level path caches,
    # os.environ-backed .env keys, and the global config cache. Keep those blocked
    # while any agent stream is active. Per-client WebUI switches are cookie/TLS
    # scoped (process_wide=False) and do not mutate those globals, so users can
    # leave a running session in one profile and start work in another (#1700).
    if process_wide:
        with STREAMS_LOCK:
            if len(STREAMS) > 0:
                raise RuntimeError(
                    'Cannot switch profiles while an agent is running. '
                    'Cancel or wait for it to finish.'
                )

    # Resolve profile directory
    if _is_isolated_profile_mode():
        home = Path(_INITIAL_HERMES_HOME).expanduser()
    elif _is_root_profile(name):
        home = _DEFAULT_HERMES_HOME
    else:
        home = _resolve_named_profile_home(name)
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")

    with _profile_lock:
        _SKILLS_STATS_CACHE.clear()
        if process_wide:
            global _active_profile
            _active_profile = name
            _set_hermes_home(home)
            _reload_dotenv(home)

    if process_wide:
        # Write sticky default for CLI consistency
        try:
            ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
            ap_file.write_text('' if _is_root_profile(name) else name, encoding='utf-8')
        except Exception:
            logger.debug("Failed to write active profile file")

        # Reload config.yaml from the new profile
        reload_config()

    # Return profile-specific defaults so frontend can apply them.
    # For process_wide=False (per-client switch), read the target profile's
    # config.yaml directly from disk rather than from _cfg_cache (process-global),
    # since reload_config() was intentionally skipped.
    if process_wide:
        from api.config import get_config
        cfg = get_config()
    else:
        # Direct disk read — does not touch _cfg_cache
        try:
            import yaml as _yaml
            cfg_path = home / 'config.yaml'
            cfg = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    model_cfg = cfg.get('model', {})
    default_model = None
    default_model_provider = None
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        default_model = model_cfg.get('default')
        default_model_provider = model_cfg.get('provider')

    # Read the target profile's workspace directly from *home* rather than via
    # get_last_workspace() which routes through the thread-local/process-global active
    # profile — both of which still point to the OLD profile during process_wide=False
    # switches (the Set-Cookie has been sent but hasn't been processed by a new request
    # yet).  We derive workspace in priority order:
    #   1. {home}/webui_state/last_workspace.txt  (previously chosen workspace for this profile)
    #   2. cfg terminal.cwd / workspace / default_workspace keys
    #   3. Boot-time DEFAULT_WORKSPACE constant
    # Use the module-level ``Path`` (imported at line 17) rather than re-importing
    # it locally — keeps the exception fallback simple and avoids a latent NameError
    # if a future refactor moves the inner imports.
    default_workspace = None
    try:
        from api.config import DEFAULT_WORKSPACE as _DW
        lw_file = home / 'webui_state' / 'last_workspace.txt'
        if lw_file.exists():
            _p = lw_file.read_text(encoding='utf-8').strip()
            if _p:
                _pp = Path(_p).expanduser()
                if _pp.is_dir():
                    default_workspace = str(_pp.resolve())
        if default_workspace is None:
            for _key in ('workspace', 'default_workspace'):
                _v = cfg.get(_key)
                if _v:
                    _pp = Path(str(_v)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
                        break
        if default_workspace is None:
            _tc = cfg.get('terminal', {})
            if isinstance(_tc, dict):
                _cwd = _tc.get('cwd', '')
                if _cwd and str(_cwd) not in ('.', ''):
                    _pp = Path(str(_cwd)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
        if default_workspace is None:
            default_workspace = str(_DW)
    except Exception:
        try:
            from api.config import DEFAULT_WORKSPACE as _DW2
            default_workspace = str(_DW2)
        except Exception:
            default_workspace = str(Path.home())

    return {
        'profiles': list_profiles_api(),
        'active': name,
        'is_default': _is_root_profile(name),
        'default_model': default_model,
        'default_model_provider': default_model_provider,
        'default_workspace': default_workspace,
    }


# Profile catalog cache state remains facade-owned for compatibility.  The
# catalog implementation resolves this namespace at call time, so direct cache
# clears and historical monkeypatches remain observable.
_SKILLS_STATS_CACHE: dict[Path, tuple[int, int, int, float]] = {}
_SKILLS_STATS_CACHE_TTL = 300.0
_SKILLS_STATS_LOCKS: dict[Path, threading.Lock] = {}
_SKILLS_STATS_LOCKS_GUARD = threading.Lock()

_LIST_PROFILES_CACHE: tuple[list, float] | None = None
# Profile-row mutations beyond create/delete do not all invalidate this cache;
# keep the short freshness contract established by the original implementation.
_LIST_PROFILES_CACHE_TTL = 4.0
_LIST_PROFILES_CACHE_LOCK = threading.Lock()

from api.profiles import catalog as _catalog

_skills_stats_lock_for = _catalog._skills_stats_lock_for
_skill_tree_max_mtime_ns = _catalog._skill_tree_max_mtime_ns
_compute_profile_skills_stats = _catalog._compute_profile_skills_stats
_get_profile_skills_stats = _catalog._get_profile_skills_stats
_invalidate_list_profiles_cache = _catalog._invalidate_list_profiles_cache
_build_profile_rows_fast = _catalog._build_profile_rows_fast
list_profiles_api = _catalog.list_profiles_api
_profile_visible_from_meta = _catalog._profile_visible_from_meta
_default_profile_dict = _catalog._default_profile_dict


# Provider-to-secret mapping is part of the profile-management persistence
# contract. API keys are written only to the profile's .env, never config.yaml.
_PROVIDER_ENV_MAP: dict[str, str] = {
    "kimi-coding": "KIMI_API_KEY",
    "kimi-coding-cn": "KIMI_CN_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "zai": "ZAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "kilocode": "KILOCODE_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "github-copilot": "COPILOT_GITHUB_TOKEN",
    "nous": "NOUS_API_KEY",
}

from api.profiles import management as _management

_validate_profile_name = _management._validate_profile_name
validate_profile_name = _management._validate_profile_name
_profiles_root = _management._profiles_root
profiles_root = _profiles_root
_resolve_named_profile_home = _management._resolve_named_profile_home
_create_profile_fallback = _management._create_profile_fallback
_resolve_env_var_for_provider = _management._resolve_env_var_for_provider
_upsert_dotenv_line = _management._upsert_dotenv_line
_write_api_key_to_dotenv = _management._write_api_key_to_dotenv
_write_endpoint_to_config = _management._write_endpoint_to_config
_clean_profile_config_value = _management._clean_profile_config_value
_split_webui_provider_model_value = _management._split_webui_provider_model_value
split_webui_provider_model_value = _split_webui_provider_model_value
_strip_webui_provider_prefix = _management._strip_webui_provider_prefix
_profile_model_selection_exists = _management._profile_model_selection_exists
_get_available_models_for_profile_validation = (
    _management._get_available_models_for_profile_validation
)
_validate_profile_model_selection = _management._validate_profile_model_selection
_write_model_defaults_to_config = _management._write_model_defaults_to_config
create_profile_api = _management.create_profile_api
delete_profile_api = _management.delete_profile_api


def reload_profile_environment(home: Path) -> None:
    """Reload process environment values from a known profile home."""
    _reload_dotenv(home)


def _install_config_profile_hooks() -> None:
    import importlib

    from api.config import install_config_runtime_hooks

    install_config_runtime_hooks(
        active_profile_name=lambda: importlib.import_module(__name__).get_active_profile_name(),
        active_profile_home=lambda: importlib.import_module(__name__).get_active_hermes_home(),
        active_profile_scope=profile_env_for_active_request,
        detached_profile_scope=profile_scope_for_detached_worker,
    )


_install_config_profile_hooks()


__all__ = tuple(name for name in globals() if not name.startswith("__"))
