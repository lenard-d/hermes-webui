"""
Hermes Web UI -- Shared configuration, constants, and global state.
Imported by all other api/* modules and by server.py.

Discovery order for all paths:
  1. Explicit environment variable
  2. Filesystem heuristics (sibling checkout, parent dir, common install locations)
  3. Hardened defaults relative to $HOME
4. Fail loudly with a human-readable fix-it message if required modules are missing
"""

# The entrypoint intentionally preserves the historical public import surface
# while implementation owners live in focused sibling modules.
# ruff: noqa: F401, F405

import collections
import copy
import hashlib
import importlib
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from urllib.parse import parse_qs, urlparse

# ── Basic layout ──────────────────────────────────────────────────────────────
import api.paths as _paths
from api.config.plugin_providers import (
    effective_provider_display_name as _effective_provider_display_name,
    effective_provider_env_var,
    is_plugin_model_provider as _is_plugin_model_provider,
    plugin_model_provider_ids,
    plugin_model_provider_profiles as _plugin_model_provider_profiles,
)

effective_provider_display_name = _effective_provider_display_name
is_plugin_model_provider = _is_plugin_model_provider
plugin_model_provider_profiles = _plugin_model_provider_profiles

HOME = _paths.HOME
_hermes_home_has_webui_state = _paths._hermes_home_has_webui_state
_platform_default_hermes_home = _paths._platform_default_hermes_home

# REPO_ROOT is the directory above the nested api/config package.
REPO_ROOT = Path(__file__).parent.parent.parent.resolve()

# ── Network config (env-overridable) ─────────────────────────────────────────
HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))


# ── TLS/HTTPS config (optional, env-overridable) ────────────────────────────
TLS_CERT = os.getenv("HERMES_WEBUI_TLS_CERT", "").strip() or None
TLS_KEY = os.getenv("HERMES_WEBUI_TLS_KEY", "").strip() or None
TLS_ENABLED = TLS_CERT is not None and TLS_KEY is not None

# ── State directory (env-overridable, never inside repo) ──────────────────────
_DEFAULT_HERMES_HOME = _platform_default_hermes_home()
_DEFAULT_STATE_HOME = Path(os.getenv("HERMES_HOME") or _DEFAULT_HERMES_HOME).expanduser()

STATE_DIR = (
    Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(_DEFAULT_STATE_HOME / "webui")))
    .expanduser()
    .resolve()
)

SESSION_DIR = STATE_DIR / "sessions"
WORKSPACES_FILE = STATE_DIR / "workspaces.json"
SESSION_INDEX_FILE = SESSION_DIR / "_index.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
LAST_WORKSPACE_FILE = STATE_DIR / "last_workspace.txt"
PROJECTS_FILE = STATE_DIR / "projects.json"

logger = logging.getLogger(__name__)

# Keep custom provider /v1/models probes below the frontend's generic request
# timeout even when one upstream is slow or unreachable. The models cache rebuild
# path probes configured custom endpoints serially, so each provider needs a
# short hard cap and graceful degradation.
CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS = 5.0


# ── Environment and installation discovery implementation ───────────────────
from api.config import paths as _path_env

_env_int = _path_env._env_int
_env_mb_bytes = _path_env._env_mb_bytes
_discover_agent_dir = _path_env._discover_agent_dir
_looks_like_agent_source_root = _path_env._looks_like_agent_source_root
_looks_like_pip_style_agent_source_root = (
    _path_env._looks_like_pip_style_agent_source_root
)
_discover_python = _path_env._discover_python
_workspace_candidates = _path_env._workspace_candidates
_ensure_workspace_dir = _path_env._ensure_workspace_dir
resolve_default_workspace = _path_env.resolve_default_workspace
_discover_default_workspace = _path_env._discover_default_workspace
_warn_state_dir_divergence = _path_env._warn_state_dir_divergence
print_startup_config = _path_env.print_startup_config
verify_hermes_imports = _path_env.verify_hermes_imports


# Run discovery
_AGENT_DIR = _discover_agent_dir()
PYTHON_EXE = _discover_python(_AGENT_DIR)


def get_agent_source_dir() -> Path | None:
    """Return the discovered Hermes Agent source directory, when available."""
    return _AGENT_DIR


def is_hermes_agent_available() -> bool:
    """Return whether Hermes Agent source was discovered during startup."""
    return _HERMES_FOUND

# ── Inject agent dir into sys.path so Hermes modules are importable ──────────

# When users (or CI builds) run `pip install --target .` or
# `pip install -t .` inside the hermes-agent checkout, third-party
# package directories (openai/, pydantic/, requests/, etc.) end up
# alongside real Hermes source files.  Putting _AGENT_DIR at the
# FRONT of sys.path means Python resolves `import pydantic` from that
# local directory — which breaks whenever the host platform differs
# from the container (e.g. macOS .so files inside a Linux image).
#
# Fix: insert _AGENT_DIR at the END of sys.path.  Python searches
# entries in order, so site-packages resolves pip packages correctly,
# and Hermes-specific modules (run_agent, hermes/, etc.) still
# resolve because they do not exist in site-packages.

if _AGENT_DIR is not None:
    if str(_AGENT_DIR) not in sys.path:
        sys.path.append(str(_AGENT_DIR))
    _HERMES_FOUND = True
else:
    _HERMES_FOUND = False

# ── Thread-local env context ─────────────────────────────────────────────────
# Defined BEFORE the config-file section because _expand_env_vars() (below) calls
# _thread_local_env_value() and the import-time reload_config() runs during module
# load — a forward reference here would NameError on any startup config.yaml that
# uses a ${VAR} reference. Depends only on os + threading (both imported above).
_thread_ctx = threading.local()

# Process-wide environment writes are shared by profile switching, OAuth
# onboarding, provider credential updates, and local run setup.  Configuration
# owns this lock because those writers all mutate the same configuration input;
# live/SSE transport must not own process environment synchronization.
environment_mutation_lock = threading.Lock()

# ── Config file state (reloadable -- supports profile switching) ─────────────
from api.config import io as _config_io

_thread_local_env_value = _config_io._thread_local_env_value
profile_env_value = _thread_local_env_value
_expand_env_vars = _config_io._expand_env_vars


_cfg_cache = {}
_cfg_lock = threading.Lock()
_cfg_mtime: float = 0.0  # last known mtime of config.yaml; 0 = never loaded
_cfg_path: Path | None = None  # active config.yaml path for the disk-loaded cache
_cfg_fingerprint: str | None = None  # serialized snapshot from the last disk load


_fingerprint_config = _config_io._fingerprint_config
_cfg_has_in_memory_overrides = _config_io._cfg_has_in_memory_overrides
_get_config_path = _config_io._get_config_path


def get_config_path() -> Path:
    """Return the active profile's authoritative ``config.yaml`` path."""
    return _get_config_path()


_WEBUI_SESSION_SAVE_MODES = {"deferred", "eager"}
_DEFAULT_WEBUI_SESSION_SAVE_MODE = "deferred"
_DEFAULT_EXPERIMENTAL_CONFIG = {
    # Dormant first slice for the unified SessionDB migration. Runtime WebUI
    # session call sites must continue using the existing JSON paths unless a
    # later PR deliberately enables and wires this flag.
    "unified_session_db": False,
}
_DEFAULT_AGENT_PERSONALITIES = {
    # Mirrors the Hermes Agent CLI built-ins so WebUI's config-derived
    # /personality path is not empty for fresh profiles.
    "helpful": "You are a helpful, friendly AI assistant.",
    "concise": "You are a concise assistant. Keep responses brief and to the point.",
    "technical": "You are a technical expert. Provide detailed, accurate technical information.",
    "creative": "You are a creative assistant. Think outside the box and offer innovative solutions.",
    "teacher": "You are a patient teacher. Explain concepts clearly with examples.",
    "kawaii": "You are a kawaii assistant! Use cute expressions like (◕‿◕), ★, ♪, and ~! Add sparkles and be super enthusiastic about everything! Every response should feel warm and adorable desu~! ヽ(>∀<☆)ノ",
    "catgirl": "You are Neko-chan, an anime catgirl AI assistant, nya~! Add 'nya' and cat-like expressions to your speech. Use kaomoji like (=^･ω･^=) and ฅ^•ﻌ•^ฅ. Be playful and curious like a cat, nya~!",
    "pirate": "Arrr! Ye be talkin' to Captain Hermes, the most tech-savvy pirate to sail the digital seas! Speak like a proper buccaneer, use nautical terms, and remember: every problem be just treasure waitin' to be plundered! Yo ho ho!",
    "shakespeare": "Hark! Thou speakest with an assistant most versed in the bardic arts. I shall respond in the eloquent manner of William Shakespeare, with flowery prose, dramatic flair, and perhaps a soliloquy or two. What light through yonder terminal breaks?",
    "surfer": "Duuude! You're chatting with the chillest AI on the web, bro! Everything's gonna be totally rad. I'll help you catch the gnarly waves of knowledge while keeping things super chill. Cowabunga!",
    "noir": "The rain hammered against the terminal like regrets on a guilty conscience. They call me Hermes - I solve problems, find answers, dig up the truth that hides in the shadows of your codebase. In this city of silicon and secrets, everyone's got something to hide. What's your story, pal?",
    "uwu": "hewwo! i'm your fwiendwy assistant uwu~ i wiww twy my best to hewp you! *nuzzles your code* OwO what's this? wet me take a wook! i pwomise to be vewy hewpful >w<",
    "philosopher": "Greetings, seeker of wisdom. I am an assistant who contemplates the deeper meaning behind every query. Let us examine not just the 'how' but the 'why' of your questions. Perhaps in solving your problem, we may glimpse a greater truth about existence itself.",
    "hype": "YOOO LET'S GOOOO!!! I am SO PUMPED to help you today! Every question is AMAZING and we're gonna CRUSH IT together! This is gonna be LEGENDARY! ARE YOU READY?! LET'S DO THIS!",
}


_apply_config_defaults = _config_io._apply_config_defaults
reload_config_if_stale = _config_io.reload_config_if_stale
get_config = _config_io.get_config
get_webui_session_save_mode = _config_io.get_webui_session_save_mode
is_unified_session_db_enabled = _config_io.is_unified_session_db_enabled


_refresh_config_cache = _config_io._refresh_config_cache
reload_config = _config_io.reload_config
update_config = _config_io.update_config


# Memoized parse cache for _load_yaml_config_file, keyed on (resolved path,
# st_mtime_ns, st_size). yaml.safe_load on an ~800-line / 24KB config.yaml costs
# ~125ms of pure-Python parsing, and hot read paths (e.g. GET /api/reasoning ->
# get_reasoning_status) call this on every request. Without a cache, a UI sync
# storm turns into a YAML-reparse storm (#4650). We cache the RAW parsed dict and
# re-run _expand_env_vars() on every call: env expansion is cheap, always returns
# a fresh structure (so callers that read-modify-save the result never corrupt the
# cache), and keeps ${VAR} references live against the current os.environ. The
# (mtime_ns, size) key means any on-disk edit (including by _save_yaml_config_file)
# is picked up on the next read.
_yaml_file_cache: dict[str, tuple] = {}
_yaml_file_cache_lock = threading.Lock()


_load_yaml_config_file_raw = _config_io._load_yaml_config_file_raw
_load_yaml_config_file = _config_io._load_yaml_config_file
load_yaml_config_file = _load_yaml_config_file


get_config_for_profile_home = _config_io.get_config_for_profile_home


_config_for_yaml_save = _config_io._config_for_yaml_save
_save_yaml_config_file = _config_io._save_yaml_config_file


# Initial load
reload_config()
cfg = _cfg_cache  # alias for backward compat with existing references


# ── Default workspace discovery ───────────────────────────────────────────────
DEFAULT_WORKSPACE = _discover_default_workspace()
DEFAULT_MODEL = os.getenv("HERMES_WEBUI_DEFAULT_MODEL", "")  # Empty = use provider default; avoids showing unavailable OpenAI model to non-OpenAI users (#646)


# ── Limits ───────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 400_000
MAX_UPLOAD_BYTES = _env_mb_bytes("HERMES_WEBUI_MAX_UPLOAD_MB", 20)

# ── File type maps ───────────────────────────────────────────────────────────
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}
MD_EXTS = {".md", ".markdown", ".mdown"}
CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".txt",
    ".log",
    ".env",
    ".csv",
    ".xml",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
}
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
}

# ── Toolsets (from config.yaml or hardcoded default) ─────────────────────────
_DEFAULT_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "cronjob",
    "delegation",
    "file",
    "image_gen",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "web",
    "webhook",
]

_LEGACY_CLI_TOOLSET_ALIASES = {
    # Older Hermes configs used "hermes" as the CLI composite toolset. Modern
    # Hermes Agent exposes that split as these two registered composites; keep
    # WebUI sessions usable when pointed at an older shared config.yaml.
    "hermes": ("hermes-cli", "hermes-api-server"),
}


def _normalize_cli_toolsets(toolsets):
    """Expand legacy CLI toolset aliases while preserving order and de-duping."""
    normalized = []
    seen = set()
    for name in toolsets or []:
        replacements = _LEGACY_CLI_TOOLSET_ALIASES.get(name, (name,))
        for replacement in replacements:
            if replacement and replacement not in seen:
                seen.add(replacement)
                normalized.append(replacement)
    return normalized


def _resolve_cli_toolsets(cfg=None):
    """Resolve CLI toolsets using the agent's _get_platform_tools() so that
    MCP server toolsets are automatically included, matching CLI behaviour."""
    if cfg is None:
        cfg = get_config()
    try:
        from hermes_cli.tools_config import _get_platform_tools
        return _normalize_cli_toolsets(_get_platform_tools(cfg, "cli"))
    except Exception:
        # Fallback: read raw list from config (MCP toolsets will be missing)
        return _normalize_cli_toolsets(cfg.get("platform_toolsets", {}).get("cli", _DEFAULT_TOOLSETS))


resolve_cli_toolsets = _resolve_cli_toolsets

# ── Model / provider discovery ───────────────────────────────────────────────

from api.config.static_catalog import (
    FALLBACK_MODELS,
    PROVIDER_ALIASES,
    PROVIDER_DISPLAY,
    PROVIDER_MODELS,
)
from api.config.provider_credentials import (
    _OAUTH_PROVIDERS as OAUTH_PROVIDER_IDS,
    _PROVIDER_CREDENTIAL_ENV_VARS as PROVIDER_CREDENTIAL_ENV_VARS,
    _PROVIDER_ENV_VAR as PROVIDER_ENV_VARS,
    _PROVIDER_ENV_VAR_ALIASES as PROVIDER_ENV_VAR_ALIASES,
    _SELF_HOSTED_PROVIDER_IDS as SELF_HOSTED_PROVIDER_IDS,
    provider_credential_env_vars,
)
from api.config.hooks import install_config_runtime_hooks

_FALLBACK_MODELS = copy.deepcopy(FALLBACK_MODELS)
_PROVIDER_ALIASES = dict(PROVIDER_ALIASES)
_PROVIDER_DISPLAY = dict(PROVIDER_DISPLAY)
_PROVIDER_MODELS = copy.deepcopy(PROVIDER_MODELS)
from api.config import provider_discovery as _provider_discovery


_get_anthropic_fallback_env_vars = _provider_discovery._get_anthropic_fallback_env_vars
_resolve_provider_alias = _provider_discovery._resolve_provider_alias
resolve_provider_alias = _resolve_provider_alias


_is_known_model_provider = _provider_discovery._is_known_model_provider


_custom_provider_slug_from_name = _provider_discovery._custom_provider_slug_from_name
_custom_provider_entries = _provider_discovery._custom_provider_entries
custom_provider_entries = _custom_provider_entries
_configured_model_ids = _provider_discovery._configured_model_ids
_configured_model_options = _provider_discovery._configured_model_options
_named_custom_provider_slugs = _provider_discovery._named_custom_provider_slugs
_named_custom_provider_slug_for_provider = (
    _provider_discovery._named_custom_provider_slug_for_provider
)


_resolve_configured_provider_id = _provider_discovery._resolve_configured_provider_id


_canonicalise_provider_id = _provider_discovery._canonicalise_provider_id
_normalize_base_url_for_match = _provider_discovery._normalize_base_url_for_match
_custom_endpoint_slugs_for_base_url = (
    _provider_discovery._custom_endpoint_slugs_for_base_url
)


_LEGACY_CUSTOM_API_KEY_ENV_WARNED: set[str] = set()


_api_key_env_name = _provider_discovery._api_key_env_name
_legacy_custom_api_key_env_name = (
    _provider_discovery._legacy_custom_api_key_env_name
)
_lookup_custom_api_key_env = _provider_discovery._lookup_custom_api_key_env
lookup_custom_api_key_env = _lookup_custom_api_key_env
_named_custom_provider_slug_for_base_url = (
    _provider_discovery._named_custom_provider_slug_for_base_url
)


_provider_is_known_or_configured = (
    _provider_discovery._provider_is_known_or_configured
)




_seed_provider_models_from_core = _provider_discovery._seed_provider_models_from_core
_AMBIENT_GH_CLI_MARKERS = _provider_discovery._AMBIENT_GH_CLI_MARKERS
_AMBIENT_GH_ENV_SOURCES = _provider_discovery._AMBIENT_GH_ENV_SOURCES
_is_ambient_gh_cli_entry = _provider_discovery._is_ambient_gh_cli_entry
_format_ollama_label = _provider_discovery._format_ollama_label
format_ollama_model_label = _format_ollama_label
_format_nous_label = _provider_discovery._format_nous_label
_NOUS_FEATURED_THRESHOLD = _provider_discovery._NOUS_FEATURED_THRESHOLD
_NOUS_FEATURED_TARGET = _provider_discovery._NOUS_FEATURED_TARGET
_MODEL_PICKER_OVERFLOW_THRESHOLD = (
    _provider_discovery._MODEL_PICKER_OVERFLOW_THRESHOLD
)
_MODEL_PICKER_VISIBLE_TARGET = _provider_discovery._MODEL_PICKER_VISIBLE_TARGET
MODEL_PICKER_OVERFLOW_THRESHOLD = _MODEL_PICKER_OVERFLOW_THRESHOLD
MODEL_PICKER_VISIBLE_TARGET = _MODEL_PICKER_VISIBLE_TARGET
_OPENROUTER_FREE_TIER_AUGMENT_CAP = (
    _provider_discovery._OPENROUTER_FREE_TIER_AUGMENT_CAP
)
_NOUS_VENDOR_PRIORITY = _provider_discovery._NOUS_VENDOR_PRIORITY
_build_nous_featured_set = _provider_discovery._build_nous_featured_set
_strip_picker_provider_hint = _provider_discovery._strip_picker_provider_hint
_model_matches_picker_selection = (
    _provider_discovery._model_matches_picker_selection
)
_split_picker_overflow_models = (
    _provider_discovery._split_picker_overflow_models
)
_apply_provider_prefix = _provider_discovery._apply_provider_prefix
_deduplicate_model_ids = _provider_discovery._deduplicate_model_ids

# Public provider-catalog interface.  Historical underscored names remain for
# compatibility, while cross-package callers use these semantic owner exports.
custom_provider_slug_from_name = _custom_provider_slug_from_name
format_nous_model_label = _format_nous_label
build_nous_featured_models = _build_nous_featured_set


def provider_display_name(provider_id: str) -> str:
    """Return a stable display label for a provider identifier."""
    normalized = str(provider_id or "").strip().lower()
    return _PROVIDER_DISPLAY.get(normalized, normalized.replace("-", " ").title())

#      api/config.py for SSRF host trust.
_LOCAL_SERVER_PROVIDERS = {
    "lmstudio",     # canonical (in hermes_cli.models.CANONICAL_PROVIDERS)
    "lm-studio",    # alias used in some custom_providers configs (#1625 Opus NIT)
    "ollama",       # via custom_providers, common pattern
    "llamacpp",     # via custom_providers
    "llama-cpp",    # alias
    "vllm",         # via custom_providers
    "tabby",        # via custom_providers (TabbyAPI)
    "tabbyapi",     # alias
    "koboldcpp",    # local llama.cpp UI fork
    "textgen",      # text-generation-webui (oobabooga) OpenAI-compat extension
    "localai",      # LocalAI project (#1625 Opus NIT)
}
from api.config import provider_routing as _provider_routing


_is_local_server_provider = _provider_routing._is_local_server_provider


_model_id_declared_in_config = _provider_routing._model_id_declared_in_config


_is_first_party_model = _provider_routing._is_first_party_model


_base_url_points_at_local_server = _provider_routing._base_url_points_at_local_server


_custom_slug_rest_looks_like_host_port = (
    _provider_routing._custom_slug_rest_looks_like_host_port
)


_parse_provider_qualified_model_id = (
    _provider_routing._parse_provider_qualified_model_id
)


_get_provider_base_url = _provider_routing._get_provider_base_url
_get_providers_cfg = _provider_routing._get_providers_cfg
_get_provider_cfg = _provider_routing._get_provider_cfg

configured_provider_base_url = _get_provider_base_url


def resolve_model_provider(model_id: str, *, explicitly_picked: bool = False) -> tuple:
    """Resolve model name, provider, and base_url for AIAgent.

    Model IDs from the dropdown can be in several formats:
      - 'claude-sonnet-4.6'            (bare name, uses config default provider)
      - 'anthropic/claude-sonnet-4.6'  (OpenRouter-style provider/model)
      - '@minimax:MiniMax-M2.7'        (explicit provider hint from dropdown)

    The @provider:model format is used for models from non-default provider
    groups in the dropdown, so we can route them through the correct provider
    via resolve_runtime_provider(requested=provider) instead of the default.

    Custom OpenAI-compatible endpoints are special: their model IDs often look
    like provider/model (for example ``google/gemma-4-26b-a4b``), which would be
    mistaken for an OpenRouter model if we only looked at the slash. To avoid
    that, first check whether the selected model matches an entry in
    config.yaml -> custom_providers and route it through that named custom
    provider.

    Returns (model, provider, base_url) where provider and base_url may be None.

    ``explicitly_picked``: True when the caller knows the user DELIBERATELY
    selected ``model_id`` this session (persisted from an ``explicit_model_pick``
    UI action), as opposed to it being a stale session leftover. Used ONLY for
    the custom-proxy COLD-catalog decision (#5979): with no provenance available,
    a deliberately-picked ``vendor/model`` is preserved verbatim (the user chose
    it, the proxy routes on it), while an UNMARKED id (a stale cross-provider
    leftover, e.g. #433's ``openai/gpt-5.4`` on a bare-only relay) still gets the
    legacy redundant-prefix strip so it keeps routing when cold. Warm provenance
    (endpoint-advertised ids) always takes precedence over this flag.
    """
    config_provider = None
    config_base_url = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        config_base_url = model_cfg.get("base_url")
        config_provider = _resolve_configured_provider_id(
            model_cfg.get("provider"),
            cfg,
            base_url=config_base_url,
            resolve_alias=False,
        )

    # Heal legacy ``provider: local`` entries (written by WebUI < v0.50.252)
    # at read time. ``local`` is not a registered provider, so passing it
    # downstream raises a ``LOCAL_API_KEY`` error from the auxiliary client
    # mid-conversation when compression/vision/web-extract fires. Route
    # through ``custom`` instead — it takes the ``no-key-required``
    # OpenAI-compat path that local servers (Ollama, LM Studio, llama.cpp,
    # vLLM, TabbyAPI) actually use. See #1384.
    if isinstance(config_provider, str) and config_provider.strip().lower() == "local":
        config_provider = "custom"

    model_id = (model_id or "").strip()
    if not model_id:
        return model_id, config_provider, config_base_url

    # Custom providers declared in config.yaml should win over slash-based
    # OpenRouter heuristics. Their model IDs commonly contain '/' too.
    # However, when the active provider is an explicit non-custom provider and
    # the requested model_id is the configured default model, that active
    # provider takes precedence over overlapping custom_providers[] entries.
    # Otherwise WebUI routes to custom:<name> instead of the intended endpoint
    # and can surface a 401 from the wrong provider (#1922).
    # For all other cases, preserve custom_providers[] routing for explicitly
    # selected custom provider models.
    _is_explicit_non_custom_provider = (
        config_provider is not None
        and config_provider != 'custom'
        and not config_provider.startswith('custom:')
    )
    _default_model = model_cfg.get('default') if isinstance(model_cfg, dict) else None
    # Owns model if it appears in the static catalog for the configured provider.
    # _PROVIDER_MODELS is keyed by CANONICAL slug (e.g. 'zai', not the 'z-ai'
    # alias a user may write in config), so canonicalise config_provider before
    # the lookup — otherwise an aliased active provider gets an empty ownership
    # set and _skip_custom_providers guard-2 silently fails, letting another
    # providers.<slug>.models entry hijack an active-owned model (#5511).
    _canon_config_provider = _canonicalise_provider_id(config_provider) if config_provider else ""
    _provider_models_set: set[str] = set()
    if (
        _canon_config_provider
        and _canon_config_provider in _PROVIDER_MODELS
        and isinstance(_PROVIDER_MODELS[_canon_config_provider], list)
    ):
        _provider_models_set = {
            m.get('id', '') for m in _PROVIDER_MODELS[_canon_config_provider]
            if isinstance(m, dict) and isinstance(m.get('id'), str)
        }
    # The active provider may be defined ENTIRELY via config.yaml `providers:`
    # (no static _PROVIDER_MODELS entry) with its own `models:` allowlist. Fold
    # that allowlist into the ownership set too, so the active provider owns its
    # own declared models and can't be hijacked by another providers.<slug>
    # entry that happens to list the same bare id earlier in config order (#5511).
    if _canon_config_provider:
        _providers_cfg_own = cfg.get('providers', {})
        if isinstance(_providers_cfg_own, dict):
            for _slug, _pdef in _providers_cfg_own.items():
                if not isinstance(_pdef, dict):
                    continue
                if _canonicalise_provider_id(_slug) != _canon_config_provider:
                    continue
                if _canon_config_provider == "copilot":
                    continue  # copilot.models is a settings map, not an allowlist
                _provider_models_set.update(_configured_model_ids(_pdef.get('models')))
    _skip_custom_providers = (
        _is_explicit_non_custom_provider
        and (
            # Guard 1: model is the configured default (existing behaviour).
            (_default_model is not None and model_id == _default_model)
            # Guard 2: model is owned by the configured non-custom provider.
            or model_id in _provider_models_set
        )
    )
    custom_providers = cfg.get('custom_providers', [])
    if isinstance(custom_providers, list) and not _skip_custom_providers:
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_model = (entry.get('model') or '').strip()
            entry_name = (entry.get('name') or '').strip()
            entry_base_url = (entry.get('base_url') or '').strip()
            entry_model_ids = set()
            if entry_model:
                entry_model_ids.add(entry_model)
            entry_model_ids.update(_configured_model_ids(entry.get('models')))
            if entry_name and model_id in entry_model_ids:
                provider_hint = _custom_provider_slug_from_name(entry_name)
                return model_id, provider_hint, entry_base_url or None

    # Check user-defined providers (config.yaml → providers:).
    # Mirrors the custom_providers scan above — exact match against each
    # entry's declared models list (case-sensitive to match custom_providers).
    providers_cfg = cfg.get('providers', {})
    if isinstance(providers_cfg, dict):
        target = model_id.strip()
        # Honor the same active/default ownership guard as the custom_providers
        # scan (_skip_custom_providers, config.py:2535): when the active provider
        # explicitly owns this model (it's the configured default or in the
        # active provider's model set), another provider's overlapping
        # `providers.<slug>.models` entry must NOT hijack routing away from the
        # active provider (#5511 gate finding — e.g. active ai-gateway + default
        # gpt-5 was being pulled to providers.openai.models.gpt-5). In that case
        # restrict the scan to the active provider's own canonical slug.
        _active_slug = _canon_config_provider
        for slug, pdef in providers_cfg.items():
            if not isinstance(pdef, dict):
                continue
            # Copilot is the documented exception: `providers.copilot.models` is
            # a per-model SETTINGS map (reasoning_effort, limits, etc.), NOT a
            # routable allowlist (see the exception at the catalog-build site).
            # Scanning it here would let a Copilot per-model settings entry
            # hijack that model's routing away from its real provider (#5511).
            if _canonicalise_provider_id(slug) == "copilot":
                continue
            # Ownership guard: when the active provider owns this model, only its
            # own providers: entry may match; skip all other slugs.
            if _skip_custom_providers and _canonicalise_provider_id(slug) != _active_slug:
                continue
            if target in _configured_model_ids(pdef.get('models')):
                p_base_url = str(pdef.get('base_url') or '').strip()
                return model_id, slug, p_base_url or None

    # @provider:model format — explicit provider hint from the dropdown.
    # Route through that provider directly (resolve_runtime_provider will
    # resolve credentials in streaming.py).
    # Use rsplit to handle provider_ids that contain ':' (e.g. custom:my-key).
    # With rsplit, "@custom:my-key:model" → provider="custom:my-key", model="model".
    # BUT: model IDs that end in :free / :beta / :thinking collide with the
    # rsplit grammar (e.g. "@openrouter:tencent/hy3-preview:free" would split
    # into provider="openrouter:tencent/hy3-preview", model="free").  Guard
    # against that by falling back to split(":") when the rsplit result is not
    # a recognised provider (#1744).
    #
    # Edge case (#1776): for custom providers with the same suffix
    # ("@custom:my-key:some-model:free"), rsplit yields
    # provider_hint="custom:my-key:some-model", bare_model="free", and the
    # custom-prefix guard below skips the split-fallback. Detect the
    # over-split structurally — custom hints normally carry one slug segment
    # after ``custom:``. If ``provider_hint`` has extra ``:`` tokens because the
    # model ID contained tags like ``:free``, peel one segment back (#1776).
    #
    # Exception: ``custom:<ip-or-host>:<port>`` is a single logical slug derived
    # from OpenAI ``base_url`` authority and contains no eaten model segments.
    parsed_provider_hint = _parse_provider_qualified_model_id(model_id)
    if parsed_provider_hint is not None:
        bare_model, provider_hint = parsed_provider_hint
        if (
            provider_hint.startswith("custom:")
            and config_base_url
            and _is_local_server_provider(config_provider)
            and provider_hint.lower() in _custom_endpoint_slugs_for_base_url(config_base_url)
        ):
            return bare_model, config_provider, config_base_url
        return bare_model, provider_hint, _get_provider_base_url(provider_hint)

    if "/" in model_id:
        prefix, bare = model_id.split("/", 1)
        # OpenRouter always needs the full provider/model path (e.g. openrouter/free,
        # anthropic/claude-sonnet-4.6). Never strip the prefix for OpenRouter.
        if config_provider == "openrouter":
            return model_id, "openrouter", config_base_url
        # Portal providers (Nous, OpenCode, NVIDIA NIM) serve models from multiple
        # upstream namespaces — check them BEFORE the prefix-strip branch so that
        # a model id whose prefix happens to equal the config_provider (e.g.
        # nvidia/nemotron-... on NVIDIA NIM) still keeps the full namespaced path.
        # The earlier ordering ran this guard AFTER the prefix-strip, so it never
        # fired in the prefix==config_provider case, causing HTTP 404 from the
        # portal which requires the full provider/model id (#2177; sibling of
        # #854 / #894 for Nous, where this guard was originally added).
        _PORTAL_PROVIDERS = {"nous", "opencode-zen", "opencode-go", "nvidia"}
        if config_provider in _PORTAL_PROVIDERS:
            return model_id, config_provider, config_base_url
        # If prefix matches config provider exactly, strip it and use that provider directly.
        # e.g. config=anthropic, model=anthropic/claude-... → bare name to anthropic API
        if config_provider and prefix == config_provider:
            return bare, config_provider, config_base_url
        # The OpenAI Codex provider uses a real base_url, but its default
        # ChatGPT endpoint cannot serve OpenRouter-style provider/model IDs.
        # Keep that narrow exception before the custom endpoint protection so
        # selecting openai/gpt-5.5 from OpenRouter under active Codex still
        # routes through OpenRouter. Other base_url-backed real providers may be
        # custom/proxy endpoints, so they must fall through to the branch below.
        if (
            config_provider == "openai-codex"
            and str(config_base_url or "").strip().rstrip("/")
            == "https://chatgpt.com/backend-api/codex"
            and prefix in _PROVIDER_MODELS
            and prefix != config_provider
        ):
            return model_id, "openrouter", None
        # Cross-provider via custom_providers: if the prefix matches a named custom
        # provider entry (e.g. "ollama-local/glm-4.7-flash:q4_k_m"), route through it
        # instead of falling back to the default config provider. MUST come BEFORE
        # the config_base_url branch because many providers have a base_url set.
        if prefix and config_provider and prefix != config_provider:
            _custom_cfg = cfg.get("custom_providers", [])
            if isinstance(_custom_cfg, list):
                for _entry in _custom_cfg:
                    if isinstance(_entry, dict) and _entry.get("name", "").strip() == prefix:
                        _slug = _custom_provider_slug_from_name(prefix)
                        _base = (_entry.get("base_url") or "").strip()
                        return model_id, _slug, _base or None

        # If a custom endpoint base_url is configured, don't reroute through OpenRouter
        # just because the model name contains a slash (e.g. google/gemma-4-26b-a4b).
        # The user has explicitly pointed at a base_url, so trust their routing config.
        if config_base_url:
            # Local model servers (LM Studio, Ollama, llama.cpp, vLLM, TabbyAPI)
            # register models under their full HuggingFace-style id. Stripping the
            # prefix breaks the lookup and causes a fresh instance to load with
            # default settings, ignoring user-tuned context length / parallel slots.
            # See #1625. Detect either by canonical provider name OR by base_url
            # pointing at a loopback/private host.
            if (_is_local_server_provider(config_provider)
                    or _base_url_points_at_local_server(config_base_url)):
                return model_id, config_provider, config_base_url
            # Strip the provider prefix only when it's a known provider namespace
            # AND stripping is the right call for this configured provider:
            #
            #  * A real first-party provider pointed at an OpenAI-compatible proxy
            #    (e.g. provider=openai + base_url=litellm) expects the bare id —
            #    "openai/gpt-5.4" → "gpt-5.4", "google/gemma-…" → "gemma-…". This
            #    is the #433 behaviour and applies whenever config_provider is not
            #    the bare "custom" pseudo-provider.
            #
            #  * A *bare* ``custom`` provider (or a named ``custom:<slug>``) is a
            #    vendor-routing proxy (LiteLLM, Bedrock gateway, OpenRouter-style
            #    multi-vendor endpoint). There we strip ONLY a prefix that is
            #    redundant with the model's own first-party namespace
            #    ("openai/gpt-5.4" → gpt-5.4, since gpt-5.4 is genuinely an OpenAI
            #    model — #433). An intrinsic routing prefix whose bare id is NOT a
            #    first-party model of that namespace is kept whole, because the
            #    proxy routes on the full string and truncating it 403s "model not
            #    allowed": "bedrock/opus-4-6" stays intact (opus-4-6 ∉ bedrock
            #    catalog — #3872).
            #
            # Unknown prefixes (e.g. "zai-org/GLM-5.1" on DeepInfra) are intrinsic
            # to the model ID and always preserved (#548). The redundant-prefix
            # strip that matches the *configured* provider's own family is handled
            # earlier by the ``prefix == config_provider`` branch.
            _cp_lower = (config_provider or "").strip().lower()
            _is_custom = _cp_lower == "custom" or _cp_lower.startswith("custom:")
            if _is_custom:
                # Vendor-routing proxy: the reliable signal for whether the
                # endpoint wants the full ``vendor/model`` id or the bare id is
                # what its own catalog actually advertised (the ids the user
                # picked from the dropdown, populated by the endpoint's live
                # ``/v1/models`` probe or a ``custom_providers[].models``
                # allowlist). The catalog-FAMILY heuristic (_is_first_party_model)
                # is the wrong question: it answered "is this bare id a first-
                # party model of the prefix's home vendor?" which is True for BOTH
                # ``x-ai/grok-4.5`` (proxy advertised it whole — must preserve,
                # #5979) and ``openai/gpt-5.4`` (a stale leftover on a relay that
                # only serves bare ``gpt-5.4`` — must strip, #433). Those two are
                # structurally identical to the family heuristic, so a model
                # graduating into a first-party catalog (agent commit 62ada5175
                # adding grok-4.5) silently flipped a working custom-proxy id from
                # preserved to stripped. Tri-state provenance tells them apart:
                #
                # (1) Config declares the full id verbatim (model.default /
                #     model.models / custom_providers[].models). Authoritative and
                #     network-free, so #5979 survives a cold restart — preserve.
                if _model_id_declared_in_config(model_id, config_provider):
                    return model_id, config_provider, config_base_url
                # (2) The endpoint's live/cached catalog advertised it.
                # Bound from the catalog owner below after facade state exists.
                _advertised = _endpoint_advertised_model_ids(config_provider)  # noqa: F821
                if _advertised:
                    # Full id advertised → route on it verbatim (#5979/#3872/#548).
                    if model_id in _advertised:
                        return model_id, config_provider, config_base_url
                    # ONLY the bare id advertised → the prefix is a redundant
                    # leftover the relay rejects; strip it (#433). Keep the
                    # ``prefix in _PROVIDER_MODELS`` belt so an adversarial catalog
                    # advertising a bare id can't strip an unknown-vendor prefix.
                    if bare in _advertised and prefix in _PROVIDER_MODELS:
                        return bare, config_provider, config_base_url
                    # Advertised but neither exact shape matched → intrinsic /
                    # unknown prefix the proxy routes on; preserve it whole.
                    return model_id, config_provider, config_base_url
                # (3) Provenance genuinely unavailable (cold/unbuilt or
                #     fingerprint-mismatched catalog AND not config-declared).
                #     Distinguish a DELIBERATE selection from a stale leftover:
                #
                #     * explicitly_picked → PRESERVE verbatim. The user chose this
                #       exact ``vendor/model`` in the UI this session; the proxy
                #       routes on it. A wrong strip destroys a namespace the proxy
                #       needs (recurs every turn, unrepairable short of declaring
                #       every model in config) — this is b3nw's #5979 case: a
                #       non-default pick on a custom:<slug> proxy, cold catalog.
                #     * NOT explicitly_picked → legacy redundant-prefix strip. An
                #       unmarked id here is a stale cross-provider leftover (the
                #       user switched providers and the old session model lingers,
                #       e.g. #433's ``openai/gpt-5.4`` on a relay that only serves
                #       bare ``gpt-5.4``); stripping keeps it routing while cold.
                #
                #     Warm provenance (case 2, endpoint-advertised ids) always
                #     wins over this flag; the send path also warms provenance
                #     network-free from the disk cache first
                #     (warm_models_catalog_provenance_if_cold), so this branch is
                #     reached only in the narrow no-disk-cache window. The flag
                #     removes the data-driven flaw where a model graduating into
                #     the static first-party catalog silently flipped routing
                #     (exactly how #5979 regressed).
                if explicitly_picked:
                    return model_id, config_provider, config_base_url
                if prefix in _PROVIDER_MODELS and _is_first_party_model(prefix, bare):
                    return bare, config_provider, config_base_url
                return model_id, config_provider, config_base_url
            # Non-custom first-party provider pointed at an OpenAI-compatible
            # proxy (e.g. provider=openai + base_url=litellm): the bare id is
            # what it expects — "openai/gpt-5.4" → "gpt-5.4" (#433).
            if prefix in _PROVIDER_MODELS:
                return bare, config_provider, config_base_url
            # Intrinsic / unknown prefix — pass the full model_id through unchanged.
            return model_id, config_provider, config_base_url

        # If prefix does NOT match config provider, the user picked a cross-provider model
        # from the OpenRouter dropdown (e.g. config=anthropic but picked openai/gpt-5.4-mini).
        # In this case always route through openrouter with the full provider/model string.
        # Exception (#4210): a custom provider (bare ``custom`` or named ``custom:<slug>``)
        # is a vendor-routing proxy, not a first-party provider — its model ids commonly
        # contain a known-provider prefix that the proxy uses for upstream routing, not
        # an OpenRouter dropdown selection. Keep the request on the custom provider; the
        # base_url-set sibling of this exception lives earlier in the ``config_base_url``
        # branch (#3872).
        _cp_lower_cross = (config_provider or "").strip().lower()
        _is_custom_cross = _cp_lower_cross == "custom" or _cp_lower_cross.startswith("custom:")
        if prefix in _PROVIDER_MODELS and prefix != config_provider and not _is_custom_cross:
            return model_id, "openrouter", None

    return model_id, config_provider, config_base_url


def resolve_custom_provider_connection(provider_id: str) -> tuple[str | None, str | None]:
    """Return (api_key, base_url) for a named ``custom:*`` provider.

    Supports ``custom_providers[].api_key`` as either a literal key or
    ``${ENV_VAR}``, and ``custom_providers[].key_env`` as an env-var hint.
    Returns ``(None, None)`` when no named custom provider matches.
    """
    pid = str(provider_id or "").strip().lower()
    if not pid.startswith("custom:"):
        return None, None

    def _slugify(value: str) -> str:
        s = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    slug = _slugify(pid.split(":", 1)[1].strip())
    if not slug:
        return None, None

    # Read the live config snapshot to avoid stale module-level cache edge
    # cases after profile switches or runtime config edits.
    cfg_data = get_config()

    def _resolve_key(raw_api_key, raw_key_env, provider_hint=None) -> str | None:
        api_key = None
        if raw_api_key is not None:
            key_text = str(raw_api_key).strip()
            if key_text.startswith("${") and key_text.endswith("}") and len(key_text) > 3:
                api_key = _thread_local_env_value(key_text[2:-1]).strip() or None
            elif key_text:
                api_key = key_text
        if not api_key:
            key_env = str(raw_key_env or "").strip()
            if key_env:
                api_key = _thread_local_env_value(key_env).strip() or None
        if not api_key and provider_hint:
            api_key = _lookup_custom_api_key_env(provider_hint)
        return api_key

    custom_providers = cfg_data.get("custom_providers", [])
    if not isinstance(custom_providers, list):
        custom_providers = []

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        entry_slug = _slugify(name)
        if entry_slug != slug:
            continue

        base_url = str(entry.get("base_url") or "").strip() or None
        api_key = _resolve_key(entry.get("api_key"), entry.get("key_env"), pid)
        return api_key, base_url

    # If exactly one custom provider is configured, use it as a pragmatic
    # fallback for mismatched slugs (e.g. punctuation differences).
    if len(custom_providers) == 1 and isinstance(custom_providers[0], dict):
        entry = custom_providers[0]
        return (
            _resolve_key(entry.get("api_key"), entry.get("key_env"), pid),
            str(entry.get("base_url") or "").strip() or None,
        )

    # Fallbacks for setups that don't use custom_providers names directly.
    providers_cfg = cfg_data.get("providers", {})
    provider_specific = providers_cfg.get(pid, {}) if isinstance(providers_cfg, dict) else {}
    provider_custom = providers_cfg.get("custom", {}) if isinstance(providers_cfg, dict) else {}

    model_cfg = cfg_data.get("model", {})
    model_provider = str(model_cfg.get("provider") or "").strip().lower() if isinstance(model_cfg, dict) else ""

    fallback_base = None
    for candidate in (provider_specific, provider_custom, model_cfg):
        if isinstance(candidate, dict):
            _base = str(candidate.get("base_url") or "").strip()
            if _base:
                fallback_base = _base
                break

    fallback_key = None
    if isinstance(provider_specific, dict):
        fallback_key = _resolve_key(provider_specific.get("api_key"), provider_specific.get("key_env"), pid)
    if not fallback_key and isinstance(provider_custom, dict):
        fallback_key = _resolve_key(provider_custom.get("api_key"), provider_custom.get("key_env"), pid)
    if not fallback_key and isinstance(model_cfg, dict) and model_provider in {"custom", pid, slug}:
        fallback_key = _resolve_key(model_cfg.get("api_key"), model_cfg.get("key_env"), pid)

    if fallback_key or fallback_base:
        return fallback_key, fallback_base or None

    return None, None


# Subprocess ACP transports (Cursor/Copilot CLI). Model IDs often contain '/'
# but must still route via explicit @provider:model so they do not fall through
# to the configured default HTTP provider (e.g. openai-codex).
_ACP_SUBPROCESS_PROVIDERS = frozenset({"cursor-acp", "copilot-acp"})


def model_with_provider_context(model_id: str, model_provider: str | None = None) -> str:
    """Return the model string to pass to ``resolve_model_provider()``.

    Session persistence keeps the user's selected provider in ``model_provider``
    instead of forcing every selected model into ``@provider:model`` form. At
    runtime, however, ``resolve_model_provider()`` still understands that
    internal disambiguation form, so use it only when the provider context is
    needed to route away from the current default provider.
    """
    model = str(model_id or "").strip()
    provider = str(model_provider or "").strip().lower()
    if not model or not provider or provider == "default" or model.startswith("@"):
        return model

    model_cfg = cfg.get("model", {})
    config_provider = None
    if isinstance(model_cfg, dict):
        config_provider = str(model_cfg.get("provider") or "").strip().lower()

    # ACP subprocess providers always need the explicit hint — their slash IDs
    # are not OpenRouter paths and must not inherit config_provider routing.
    if provider in _ACP_SUBPROCESS_PROVIDERS:
        return f"@{provider}:{model}"

    # Plugin-only model providers (e.g. 9router, and other model plugins whose
    # slugs are not in the static provider tables) route through the plugin, not
    # the default provider. This MUST come before the `provider == config_provider`
    # bare-passthrough below: when a plugin provider is ALSO the configured
    # provider, returning a bare model would drop the '@plugin:' hint and the model
    # would be sent to the wrong backend. Emit the explicit hint so it stays
    # routable to the plugin that surfaced it. (#5909 gate finding)
    if _is_plugin_model_provider(provider):
        return f"@{provider}:{model}"

    # If the selected provider is already the configured provider, leaving the
    # model bare preserves provider-specific base_url/proxy settings.
    if provider == config_provider:
        return model

    # OpenRouter selections with slash IDs are explicit provider/model paths.
    if provider == "openrouter":
        return f"@{provider}:{model}"

    # Explicit providers configured in config.yaml (for example local llama.cpp,
    # Ollama, LM Studio, vLLM, or other OpenAI-compatible endpoints) must keep
    # their provider hint even when the model ID is HuggingFace-style and
    # contains '/'. Otherwise a selected local model such as
    # 'unsloth/gemma-4-12b-it-GGUF:UD-Q4_K_XL' inherits the default provider
    # (e.g. openai-codex) and is sent to the wrong backend.
    providers_cfg = cfg.get("providers") if isinstance(cfg, dict) else {}
    if isinstance(providers_cfg, dict) and provider in providers_cfg:
        return f"@{provider}:{model}"

    # (Plugin-only provider routing handled above, before the config_provider
    # bare-passthrough.)

    # For non-OpenRouter slash IDs without an explicit configured provider,
    # keep the ID intact so existing custom/proxy base_url routing and
    # portal-provider handling remain in charge.
    if "/" in model:
        return model

    return f"@{provider}:{model}"


def canonical_model_provider_lane(model_id: str, model_provider: str | None = None) -> tuple[str, str | None]:
    """Return the runtime-resolved model/provider pair used for lane comparisons."""
    model = str(model_id or "").strip()
    provider = str(model_provider or "").strip() or None
    if not model:
        return "", provider
    resolved_model, resolved_provider, _ = resolve_model_provider(
        model_with_provider_context(model, provider)
    )
    resolved_provider = str(resolved_provider or "").strip() or None
    return str(resolved_model or "").strip(), resolved_provider


def get_effective_default_model(config_data: dict | None = None) -> str:
    """Resolve the effective Hermes default model from config, then env overrides."""
    active_cfg = config_data if config_data is not None else cfg
    default_model = DEFAULT_MODEL

    model_cfg = active_cfg.get("model", {})
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        cfg_default = str(model_cfg.get("default") or "").strip()
        if cfg_default:
            default_model = cfg_default

    env_model = (
        os.getenv("HERMES_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL")
    )
    if env_model:
        default_model = env_model.strip()
    return default_model


# ── Reasoning config (CLI parity for /reasoning) ─────────────────────────────

from api.config import reasoning as _model_reasoning

VALID_REASONING_EFFORTS = _model_reasoning.VALID_REASONING_EFFORTS
_NESTED_ROUTE_PATTERN = _model_reasoning._NESTED_ROUTE_PATTERN
_KNOWN_REASONING_PROVIDERS = _model_reasoning._KNOWN_REASONING_PROVIDERS

parse_reasoning_effort = _model_reasoning.parse_reasoning_effort
_strip_provider_hint_for_reasoning = (
    _model_reasoning._strip_provider_hint_for_reasoning
)
_reasoning_name_candidates = _model_reasoning._reasoning_name_candidates
_candidate_supports_reasoning = _model_reasoning._candidate_supports_reasoning
_nested_route_reasoning_denied = _model_reasoning._nested_route_reasoning_denied
_nested_gateway_route_reasoning = _model_reasoning._nested_gateway_route_reasoning
_zai_glm_classification = _model_reasoning._zai_glm_classification
_zai_glm_reasoning_efforts_supported = (
    _model_reasoning._zai_glm_reasoning_efforts_supported
)
_zai_glm_thinking_toggle_supported = (
    _model_reasoning._zai_glm_thinking_toggle_supported
)
_filter_reasoning_efforts_for_provider = (
    _model_reasoning._filter_reasoning_efforts_for_provider
)
_provider_known_reasoning_capable = (
    _model_reasoning._provider_known_reasoning_capable
)
_is_pre_adaptive_anthropic = _model_reasoning._is_pre_adaptive_anthropic
_heuristic_reasoning_efforts = _model_reasoning._heuristic_reasoning_efforts
_models_dev_reasoning_efforts = _model_reasoning._models_dev_reasoning_efforts
_NoRedirectHandler = _model_reasoning._NoRedirectHandler
_get_lmstudio_reasoning_probe_api_key = (
    _model_reasoning._get_lmstudio_reasoning_probe_api_key
)
_lmstudio_reasoning_probe_options_fallback = (
    _model_reasoning._lmstudio_reasoning_probe_options_fallback
)
_lmstudio_model_reasoning_options = (
    _model_reasoning._lmstudio_model_reasoning_options
)
resolve_model_reasoning_efforts = _model_reasoning.resolve_model_reasoning_efforts
_resolve_model_reasoning_efforts_impl = (
    _model_reasoning._resolve_model_reasoning_efforts_impl
)
coerce_reasoning_effort_for_model = (
    _model_reasoning.coerce_reasoning_effort_for_model
)
get_reasoning_status = _model_reasoning.get_reasoning_status


from api.config import model_settings as _model_settings

_parse_positive_int_config_value = _model_settings._parse_positive_int_config_value
get_max_tokens_status = _model_settings.get_max_tokens_status
set_max_tokens = _model_settings.set_max_tokens
set_reasoning_display = _model_settings.set_reasoning_display
set_reasoning_effort = _model_settings.set_reasoning_effort
_public_advanced_model_options = _model_settings._public_advanced_model_options
_is_openai_family_provider = _model_settings._is_openai_family_provider
is_openai_family_provider = _is_openai_family_provider
_normalize_openai_family_model_id = _model_settings._normalize_openai_family_model_id
_legacy_openai_service_tier_overrides = (
    _model_settings._legacy_openai_service_tier_overrides
)
_resolve_main_model_fast_mode_overrides = (
    _model_settings._resolve_main_model_fast_mode_overrides
)
_main_model_supports_service_tier = (
    _model_settings._main_model_supports_service_tier
)
_model_supports_fast_tier_for_provider = (
    _model_settings._model_supports_fast_tier_for_provider
)
model_supports_fast_tier_for_provider = _model_supports_fast_tier_for_provider
_annotate_fast_tier_model_groups = _model_settings._annotate_fast_tier_model_groups
_public_main_service_tier = _model_settings._public_main_service_tier
_main_model_request_overrides = _model_settings._main_model_request_overrides
main_model_request_overrides = _main_model_request_overrides
_apply_advanced_model_options = _model_settings._apply_advanced_model_options
set_hermes_default_model = _model_settings.set_hermes_default_model
AUXILIARY_TASK_CATALOG = _model_settings.AUXILIARY_TASK_CATALOG
AUX_TASK_SLOTS = _model_settings.AUX_TASK_SLOTS
RETIRED_AUX_TASK_SLOTS = _model_settings.RETIRED_AUX_TASK_SLOTS
_aux_task_payload = _model_settings._aux_task_payload
_iter_auxiliary_task_rows = _model_settings._iter_auxiliary_task_rows
get_auxiliary_models = _model_settings.get_auxiliary_models
_coerce_optional_positive_int = _model_settings._coerce_optional_positive_int
set_auxiliary_model = _model_settings.set_auxiliary_model


# Disk-backed in-memory cache for get_available_models().
# Written to disk on every cache population so the cache survives server restarts.
# Invalidated (file deleted) whenever a provider is added/changed/removed or
# config.yaml changes.  A TTL is still used as a fallback in case the invalidation
# signal is somehow missed, but the cache will always be warm after the first
# page load following a server start.
# Cache file lives inside STATE_DIR so each server instance (different
# HERMES_WEBUI_STATE_DIR / port) has its own file and test runs never
# pollute the production server's cache. Also works on macOS and Windows
# where /dev/shm does not exist.

# Disk-cache schema version (#1633).
#
# Bumped any time the disk cache shape changes in a backward-incompatible way
# (e.g. new required field, renamed key). Independent of the WebUI version
# stamp — _webui_version forces a rebuild on every release; _schema_version
# guarantees that even if a future release accidentally reuses the same
# WebUI version string (or a debug build doesn't have a version), a structural
# change still invalidates the cache.
from api.config.catalog_state import (
    MODEL_CATALOG_STATE,
    publish_legacy_model_catalog_state,
)

MODEL_CATALOG_STATE.models_cache_path = STATE_DIR / "models_cache.json"
publish_legacy_model_catalog_state(importlib.import_module(__name__))


from api.config import model_cache as _models_cache_impl

_get_models_cache_path = _models_cache_impl._get_models_cache_path
_get_auth_store_path = _models_cache_impl._get_auth_store_path
_models_cache_file_fingerprint = _models_cache_impl._models_cache_file_fingerprint
_models_cache_catalog_fingerprint = _models_cache_impl._models_cache_catalog_fingerprint
_AUTH_FINGERPRINT_VOLATILE_KEYS = _models_cache_impl._AUTH_FINGERPRINT_VOLATILE_KEYS
_strip_volatile_auth_fields = _models_cache_impl._strip_volatile_auth_fields
_auth_store_semantic_fingerprint = _models_cache_impl._auth_store_semantic_fingerprint
_models_cache_source_fingerprint = _models_cache_impl._models_cache_source_fingerprint
_delete_models_cache_on_disk = _models_cache_impl._delete_models_cache_on_disk
# Resolving CLI toolsets calls ``get_config()``, which may detect a profile-path
# change and invalidate the on-disk model cache.  Do that only after the cache
# owner above has installed its invalidation operation on the compatibility
# facade; otherwise a cold import can observe a partially initialized module.
CLI_TOOLSETS = _resolve_cli_toolsets()
_is_valid_models_cache = _models_cache_impl._is_valid_models_cache
_is_loadable_disk_cache = _models_cache_impl._is_loadable_disk_cache
_load_models_cache_from_disk = _models_cache_impl._load_models_cache_from_disk
_model_aliases_from_config = _models_cache_impl._model_aliases_from_config
_load_stale_models_cache_from_disk = _models_cache_impl._load_stale_models_cache_from_disk
_save_models_cache_to_disk = _models_cache_impl._save_models_cache_to_disk
_get_fresh_memory_models_cache = _models_cache_impl._get_fresh_memory_models_cache
invalidate_models_cache = _models_cache_impl.invalidate_models_cache
invalidate_credential_pool_cache = _models_cache_impl.invalidate_credential_pool_cache
invalidate_provider_models_cache = _models_cache_impl.invalidate_provider_models_cache
_models_cache_file_age_seconds = _models_cache_impl._models_cache_file_age_seconds
warm_models_catalog_provenance_if_cold = (
    _models_cache_impl.warm_models_catalog_provenance_if_cold
)
get_available_models_for_session_visit = (
    _models_cache_impl.get_available_models_for_session_visit
)
_maybe_log_slow_stages = _models_cache_impl._maybe_log_slow_stages

# These callables historically lived in this public compatibility module.
# Preserve both facade identity and the introspection/pickling module contract
# while their implementation remains in the cohesive cache owner.
for _models_cache_export in (
    _models_cache_file_age_seconds,
    warm_models_catalog_provenance_if_cold,
    get_available_models_for_session_visit,
    _maybe_log_slow_stages,
):
    _models_cache_export.__module__ = __name__
del _models_cache_export

from api.config.model_catalog import *  # noqa: F403 - package compatibility exports

# Public model-catalog interface used by the provider package.  Aliasing the
# owner callables preserves the established monkeypatch identities without
# copying catalog state into a second module.
credential_pool_entries = _pool_entry_payloads
provider_has_explicit_pool_credentials = _has_explicit_pool_credentials
model_label = _get_label_for_model
live_provider_model_ids = _read_live_provider_model_ids
models_from_live_provider_ids = _models_from_live_provider_ids
visible_codex_cache_model_ids = _read_visible_codex_cache_model_ids

_sync_models_cache_provenance_impl = _sync_models_cache_provenance


def _sync_models_cache_provenance() -> None:
    """Adopt explicit compatibility overrides before publishing provenance."""
    from api.config.catalog_state import import_legacy_model_catalog_state

    import_legacy_model_catalog_state(importlib.import_module(__name__))
    _sync_models_cache_provenance_impl()









# ── Static file path ─────────────────────────────────────────────────────────


def get_static_root() -> Path:
    return REPO_ROOT / "static"


def get_index_html_path() -> Path:
    return get_static_root() / "index.html"


_INDEX_HTML_PATH = get_index_html_path()

# ── Thread synchronisation ───────────────────────────────────────────────────
LOCK = threading.Lock()
# Max compact Session objects held in the in-memory LRU (issue #3506, #4765).
# Lighter than the agent cache (no live agent runtime), but still bounded so a
# long-running self-hosted install cannot accumulate every session it ever
# touched in RAM and eventually segfault (the #4765/#2233/#4633 crash cluster).
#
# Precedence for the effective cap is resolved by get_sessions_cache_max():
#   1. config.yaml  webui.sessions_cache_max   (preferred, no new env var)
#   2. HERMES_WEBUI_SESSIONS_MAX env var        (legacy operator override)
#   3. DEFAULT_SESSIONS_CACHE_MAX               (sane bounded default)
DEFAULT_SESSIONS_CACHE_MAX = 300
SESSIONS_MAX = _env_int("HERMES_WEBUI_SESSIONS_MAX", DEFAULT_SESSIONS_CACHE_MAX)


def get_sessions_cache_max(config_data: dict | None = None) -> int:
    """Return the effective in-memory SESSIONS cache cap (issue #4765).

    The bound is configurable through ``webui.sessions_cache_max`` in
    ``config.yaml`` so operators of large self-hosted installs can size the
    cache without editing source or adding a new ``HERMES_*`` env var (this
    project forbids new env vars for non-secret config). A missing, empty,
    non-numeric, or below-1 value falls back to the legacy
    ``HERMES_WEBUI_SESSIONS_MAX`` env override, then to
    ``DEFAULT_SESSIONS_CACHE_MAX`` — a typo can never disable the bound and
    reintroduce unbounded memory growth.
    """
    active_cfg = config_data if isinstance(config_data, dict) else get_config()
    webui_cfg = active_cfg.get("webui", {}) if isinstance(active_cfg, dict) else {}
    if isinstance(webui_cfg, dict):
        raw = webui_cfg.get("sessions_cache_max")
        if raw is not None:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = None
            if value is not None and value >= 1:
                return value
    # config.yaml did not specify a valid cap: honor the legacy env override
    # (already parsed into SESSIONS_MAX) and finally the hardened default.
    if isinstance(SESSIONS_MAX, int) and SESSIONS_MAX >= 1:
        return SESSIONS_MAX
    return DEFAULT_SESSIONS_CACHE_MAX
CHAT_LOCK = threading.Lock()


from api.runs.channels import StreamChannel, create_stream_channel  # noqa: F401 - compatibility exports
from api.runs.runtime_state import (
    ACTIVE_RUNS,
    ACTIVE_RUNS_LOCK,
    AGENT_INSTANCES,  # noqa: F401 - compatibility export
    CANCEL_FLAGS,  # noqa: F401 - compatibility export
    RUNTIME_STATE,  # noqa: F401 - compatibility export
    STREAM_GOAL_RELATED,  # noqa: F401 - compatibility export
    STREAM_LAST_EVENT_ID,  # noqa: F401 - compatibility export
    STREAM_LIVE_TOOL_CALLS,  # noqa: F401 - compatibility export
    STREAM_PARTIAL_TEXT,  # noqa: F401 - compatibility export
    STREAM_REASONING_TEXT,  # noqa: F401 - compatibility export
    STREAM_SESSION_OWNERS,  # noqa: F401 - compatibility export
    STREAM_SESSION_OWNERS_LOCK,  # noqa: F401 - compatibility export
    STREAMS,  # noqa: F401 - compatibility export
    STREAMS_LOCK,  # noqa: F401 - compatibility export
)


PENDING_GOAL_CONTINUATION: set = set()  # session_ids awaiting a goal continuation turn (#1932)


# ── Gateway capability cache ─────────────────────────────────────────────────
# Probes /v1/capabilities once per base_url/api-key pair and caches the result
# for 60 s so guarded-turn routing decisions do not add latency on every chat
# turn.
_GATEWAY_CAPS_CACHE: dict[tuple[str, str], dict] = {}
_GATEWAY_CAPS_LOCK = threading.Lock()
_GATEWAY_CAPS_TTL_S: float = 60.0


def _gateway_caps_probe_timed_out(exc: BaseException) -> bool:
    """Keep slow capability probes on the legacy reachable-but-unsupported path."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    reason_text = str(reason).lower()
    return "timed out" in reason_text or "timeout" in reason_text


def get_gateway_caps(base_url: str, api_key: str = "") -> dict:
    """Return cached gateway capability flags, probing /v1/capabilities if stale."""
    base_url = str(base_url or "").rstrip("/")
    cache_key = (base_url, str(api_key or ""))
    now = time.time()
    probe_started_at = now
    with _GATEWAY_CAPS_LOCK:
        cached = _GATEWAY_CAPS_CACHE.get(cache_key)
        if cached and now - cached.get("fetched_at", 0) < _GATEWAY_CAPS_TTL_S:
            return cached
    caps = {
        "approval_events": False,
        "run_approval_response": False,
        "capabilities_reachable": False,
        "probe_error": None,
        "fetched_at": 0.0,
    }
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(f"{base_url}/v1/capabilities", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            caps["capabilities_reachable"] = True
            body = json.loads(resp.read(65536))
        features = body.get("features") if isinstance(body, dict) else {}
        if not isinstance(features, dict):
            features = {}
        caps["approval_events"] = bool(features.get("approval_events"))
        caps["run_approval_response"] = bool(features.get("run_approval_response"))
    except urllib.error.HTTPError as exc:
        caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except urllib.error.URLError as exc:
        if _gateway_caps_probe_timed_out(exc):
            caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except (TimeoutError, socket.timeout) as exc:
        caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    with _GATEWAY_CAPS_LOCK:
        current = _GATEWAY_CAPS_CACHE.get(cache_key)
        if current and current.get("fetched_at", 0) > probe_started_at:
            return current
        caps["fetched_at"] = time.time()
        _GATEWAY_CAPS_CACHE[cache_key] = caps
    return caps


def gateway_approval_unavailable_reason(base_url: str, api_key: str = "") -> str | None:
    """Return why approval support is unavailable, if it is unavailable."""
    caps = get_gateway_caps(base_url, api_key)
    if bool(caps.get("approval_events") and caps.get("run_approval_response")):
        return None
    if not caps.get("capabilities_reachable"):
        return "unreachable"
    return "unsupported"


def gateway_supports_approval(base_url: str, api_key: str = "") -> bool:
    """True only when the gateway advertises both approval_events and run_approval_response."""
    caps = get_gateway_caps(base_url, api_key)
    return bool(caps.get("approval_events") and caps.get("run_approval_response"))


def invalidate_gateway_caps(base_url: str | None = None) -> None:
    """Evict capability cache for base_url, or all entries when base_url is None."""
    with _GATEWAY_CAPS_LOCK:
        if base_url is None:
            _GATEWAY_CAPS_CACHE.clear()
        else:
            normalized = str(base_url or "").rstrip("/")
            for cache_key in [key for key in _GATEWAY_CAPS_CACHE if key[0] == normalized]:
                _GATEWAY_CAPS_CACHE.pop(cache_key, None)


# ── notify_on_complete agent-wakeup wiring ─────────────────────────────────
# When terminal(notify_on_complete=true, background=true) fires, the process
# registry pushes a completion event onto tools.process_registry.completion_queue.
# A drain task spawned at WebUI startup (api.background_process) reads that
# queue and emits an SSE `process_complete` event to the matching session.
# PROCESS_SESSION_INDEX maps the per-process "session_key" (set in the spawned
# subprocess via HERMES_SESSION_KEY) back to the WebUI session_id that owns it,
# so the drain task can route the event to the right SSE channel.
# PENDING_BG_TASK_COMPLETIONS mirrors PENDING_GOAL_CONTINUATION: server-side
# marker discarded atomically by routes.py when the frontend re-POSTs the
# wakeup_prompt as the next user turn. (process_complete event, agent wakeup fix)
PROCESS_SESSION_INDEX: dict = {}  # process_registry session_key -> WebUI session_id
PROCESS_SESSION_INDEX_LOCK = threading.Lock()
PENDING_BG_TASK_COMPLETIONS: set = set()  # session_ids awaiting a process_complete wakeup turn
BG_TASK_COMPLETE_EVENTS_SEEN: dict = {}  # session_id -> set[process_id] for idempotency
BG_TASK_COMPLETE_EVENTS_SEEN_LOCK = threading.Lock()

# Defer-path fix (fast-bg-task wakeup race): when a completion arrives while a
# turn is active, Option Z's drain branch CANNOT start a turn (would 409). The
# pre-existing PENDING_BG_TASK_COMPLETIONS marker was a bare session_id flag —
# the wakeup_prompt was DISCARDED, and the only consumer (PR #2279 next-turn
# drain) reads completion_queue, which the Option Z drain thread already
# emptied. So for an autonomous agent (no next user turn) the deferred wakeup
# was lost forever. DEFERRED_PROCESS_WAKEUPS persists the actual prompt(s) so a
# turn-teardown idle-hook (api/streaming) can redeliver them once the session
# goes idle — symmetric with the idle branch (idle now → fire now; busy now →
# fire at turn-end). Atomic claim (pop under lock) guarantees single delivery:
# whoever claims first (teardown hook OR next-turn drain) fires; the other
# finds nothing → no double-fire, no wakeup loop.
DEFERRED_PROCESS_WAKEUPS: dict = {}  # session_id -> list[{"process_id", "wakeup_prompt"}]
DEFERRED_PROCESS_WAKEUPS_LOCK = threading.Lock()

# ── Persistent per-session SSE channel (Option X) ──────────────────────────
# A long-lived SSE channel scoped to a WebUI session_id rather than a single
# agent turn (stream_id). Subscribed to by the frontend on session mount,
# torn down on session unmount, and refcounted across tabs. Used to deliver
# events (currently process_complete) that fire while no agent turn is
# active — bridging the gap that PR #2242 + #2279 left when STREAMS has
# already been torn down. The registry lives in api.background_process; this
# constant is the idle-cap before the reaper collects an unsubscribed
# channel. 4h is a defensive ceiling against zombie connections; the
# subscribers-empty grace path (60s) handles ordinary tab-close traffic.
SESSION_CHANNEL_IDLE_TTL_SECS: int = 14400  # 4 hours
SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS: int = 60  # subscribers-empty grace

LAST_RUN_FINISHED_AT: float | None = None
SERVER_START_TIME = time.time()


# Keep the long-standing ``api.config`` import and monkeypatch surface while the
# implementations live in a cohesive leaf module.  The resolver is late-bound
# so patched facade state remains authoritative for every adapter call.
from api.config import runtime as _runtime_registry

register_stream_owner = _runtime_registry.register_stream_owner
stream_owner_session_id = _runtime_registry.stream_owner_session_id
unregister_stream_owner = _runtime_registry.unregister_stream_owner
register_active_run = _runtime_registry.register_active_run
update_active_run = _runtime_registry.update_active_run
unregister_active_run = _runtime_registry.unregister_active_run
register_runtime_stream = _runtime_registry.register_runtime_stream
blocking_runtime_stream = _runtime_registry.blocking_runtime_stream
runtime_stream_alive = _runtime_registry.runtime_stream_alive
runtime_worker_alive = _runtime_registry.runtime_worker_alive
runtime_transport = _runtime_registry.runtime_transport
runtime_transport_items = _runtime_registry.runtime_transport_items
runtime_transport_count = _runtime_registry.runtime_transport_count
runtime_worker_items = _runtime_registry.runtime_worker_items
runtime_last_run_finished_at = _runtime_registry.runtime_last_run_finished_at
runtime_active_run_ids = _runtime_registry.runtime_active_run_ids
runtime_run_session_id = _runtime_registry.runtime_run_session_id
runtime_last_event_id = _runtime_registry.runtime_last_event_id
note_runtime_last_event_id = _runtime_registry.note_runtime_last_event_id
initialize_runtime_execution = _runtime_registry.initialize_runtime_execution
append_runtime_partial_text = _runtime_registry.append_runtime_partial_text
replace_runtime_partial_text = _runtime_registry.replace_runtime_partial_text
append_runtime_reasoning_text = _runtime_registry.append_runtime_reasoning_text
replace_runtime_reasoning_text = _runtime_registry.replace_runtime_reasoning_text
start_runtime_tool_call = _runtime_registry.start_runtime_tool_call
finish_runtime_tool_call = _runtime_registry.finish_runtime_tool_call
attach_runtime_agent = _runtime_registry.attach_runtime_agent
runtime_progress_snapshot = _runtime_registry.runtime_progress_snapshot
begin_runtime_cancel = _runtime_registry.begin_runtime_cancel
finish_runtime_run = _runtime_registry.finish_runtime_run

# Agent cache: reuse AIAgent across messages in the same WebUI session so that
# _user_turn_count survives between turns.  This mirrors the gateway's
# _agent_cache pattern and is required for injectionFrequency: "first-turn".
# LRU cache with size limit to prevent memory bloat.
# All cache operations (get, set, move_to_end, popitem) are protected by
# SESSION_AGENT_CACHE_LOCK for thread safety in multi-threaded ASGI servers.
SESSION_AGENT_CACHE: collections.OrderedDict = collections.OrderedDict()  # LRU cache
# Each cached agent pins a full conversation transcript in RAM, so this cap is
# the dominant lever on WebUI resident memory (issue #3506). The default is kept
# deliberately modest -- large/long sessions can each weigh tens of MB, so 50
# live agents could pin >1 GB on a heavily multiplexed install. Operators can
# tune it via HERMES_WEBUI_AGENT_CACHE_MAX without editing source.
SESSION_AGENT_CACHE_MAX = _env_int("HERMES_WEBUI_AGENT_CACHE_MAX", 25)
SESSION_AGENT_CACHE_LOCK = threading.Lock()


def _evict_session_agent(session_id: str) -> None:
    """Remove a cached agent for a session (on delete, clear, or model switch).

    Attempts a lifecycle commit before dropping the agent handle so that
    batch-extraction memory providers can extract any pending work.  If the
    commit fails or there is uncommitted work with no successful commit, the
    lifecycle entry is preserved (not unregistered) so a future commit can
    retry.
    """
    agent = None
    with SESSION_AGENT_CACHE_LOCK:
        entry = SESSION_AGENT_CACHE.pop(session_id, None)
        if entry is not None:
            agent = entry[0] if isinstance(entry, tuple) else None
    if agent is None:
        return
    # A live run for this session may still hold this agent's _session_db (the
    # worker assigns agent._session_db at run start). Never close it out from
    # under an in-flight turn — ACTIVE_RUNS is the authoritative liveness signal
    # (mirrors the worker's own LRU-eviction guard in streaming.py). When a run
    # is live we still drop the cache handle above (harmless — the worker holds
    # a local ref), but skip the lifecycle commit + _session_db.close() so the
    # running turn can finish persisting. Hardens /clear + model-switch eviction
    # too, not just truncate (#5096 Bug D).
    _run_active = False
    try:
        with ACTIVE_RUNS_LOCK:
            for _entry in (ACTIVE_RUNS or {}).values():
                if (_entry or {}).get("session_id") == session_id:
                    _run_active = True
                    break
    except Exception:
        _run_active = False
    if _run_active:
        return
    should_close = True
    try:
        from api.sessions.lifecycle import commit_session_memory, discard_session, has_uncommitted_work, unregister_agent
        if has_uncommitted_work(session_id):
            commit_session_memory(session_id, agent=agent, wait=True)
        if not has_uncommitted_work(session_id):
            unregister_agent(session_id)
            # Bound the lifecycle dict: drop the entry now that the session has
            # no uncommitted work and the agent handle is gone (issue #3506).
            discard_session(session_id)
        else:
            should_close = False
    except Exception:
        should_close = False
        logger.debug("Lifecycle commit on eviction failed for %s", session_id, exc_info=True)
    if should_close and getattr(agent, '_session_db', None) is not None:
        try:
            agent._session_db.close()
        except Exception:
            logger.debug("Failed to close _session_db on eviction for %s", session_id, exc_info=True)

evict_session_agent = _evict_session_agent

# ── Thread-local env context ─────────────────────────────────────────────────
# (_thread_ctx + _thread_local_env_value are defined near the top of this module,
# above the config-file section, so _expand_env_vars can reference them at the
# import-time reload_config() without a forward-reference NameError.)


def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs


def _clear_thread_env():
    _thread_ctx.env = {}


def set_thread_env(env: dict[str, str]) -> None:
    """Install a normalized thread-local environment through the config owner."""
    _set_thread_env(**dict(env))


def clear_thread_env() -> None:
    """Clear the current thread-local environment through the config owner."""
    _clear_thread_env()


def is_process_env_fallback_blocked() -> bool:
    """Return whether the current config scope rejects process-env fallback."""
    return bool(getattr(_thread_ctx, "block_process_env_fallback", False))


@contextmanager
def thread_env_scope(
    env: dict[str, str], *, block_process_env_fallback: bool = False
):
    """Temporarily install one thread-local configuration environment.

    The config foundation owns both the mutable thread-local state and its
    restoration invariant.  Profile orchestration supplies only the resolved
    environment and cannot retain or directly mutate the owner's storage.
    """
    previous_env = dict(getattr(_thread_ctx, "env", {}))
    previous_block = bool(
        getattr(_thread_ctx, "block_process_env_fallback", False)
    )
    _set_thread_env(**dict(env))
    _thread_ctx.block_process_env_fallback = bool(block_process_env_fallback)
    try:
        yield
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env:
            _set_thread_env(**previous_env)
        else:
            _clear_thread_env()


# ── Per-session agent locks ───────────────────────────────────────────────────
SESSION_AGENT_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)
SESSION_AGENT_LOCKS_LOCK = threading.Lock()


def _get_session_agent_lock(session_id: str) -> threading.Lock:
    """Return the per-session Lock used to serialize all Session mutations.

    Lock lifecycle invariant:
      - A Lock is created lazily and remains discoverable while any holder or
        waiter keeps a strong reference. Weak registry entries disappear only
        after the last overlapping operation releases its reference.
      - During context compression the agent may rotate session_id.  The
        streaming thread migrates the lock entry atomically under
        SESSION_AGENT_LOCKS_LOCK: it aliases the new session_id to the *same*
        Lock object (see streaming.py compression block). Both aliases remain
        weakly discoverable while old- or new-id waiters still exist.
      - Lock contract: hold for the in-memory mutation + s.save() only; never
        across network I/O (LLM calls, HTTP requests).
    """
    with SESSION_AGENT_LOCKS_LOCK:
        lock = SESSION_AGENT_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            SESSION_AGENT_LOCKS[session_id] = lock
        return lock


session_agent_lock = _get_session_agent_lock


def alias_session_agent_lock(
    old_session_id: str,
    new_session_id: str,
    held_lock: threading.Lock,
) -> None:
    """Bind a compression continuation to the already-held session owner."""
    with SESSION_AGENT_LOCKS_LOCK:
        old_owner = SESSION_AGENT_LOCKS.get(old_session_id)
        new_owner = SESSION_AGENT_LOCKS.get(new_session_id)
        if old_owner is not None and old_owner is not held_lock:
            raise RuntimeError("old session id has another lock owner")
        if new_owner is not None and new_owner is not held_lock:
            raise RuntimeError("new session id has another lock owner")
        SESSION_AGENT_LOCKS[old_session_id] = held_lock
        SESSION_AGENT_LOCKS[new_session_id] = held_lock


# ── Settings persistence ─────────────────────────────────────────────────────

from api.config import settings as _settings_persistence

_SETTINGS_DEFAULTS = _settings_persistence._build_settings_defaults(DEFAULT_WORKSPACE)
_SETTINGS_SPEECH_KEYS = _settings_persistence._SETTINGS_SPEECH_KEYS
_SETTINGS_PERSISTED_SPEECH_KEYS_FIELD = (
    _settings_persistence._SETTINGS_PERSISTED_SPEECH_KEYS_FIELD
)
_SETTINGS_LEGACY_DROP_KEYS = _settings_persistence._SETTINGS_LEGACY_DROP_KEYS
_COMPOSER_CONTROL_ORDER_KEYS = _settings_persistence._COMPOSER_CONTROL_ORDER_KEYS
_SETTINGS_THEME_VALUES = _settings_persistence._SETTINGS_THEME_VALUES
_SETTINGS_SKIN_VALUES = _settings_persistence._SETTINGS_SKIN_VALUES
_SETTINGS_LEGACY_THEME_MAP = _settings_persistence._SETTINGS_LEGACY_THEME_MAP
_SETTINGS_ALLOWED_KEYS = _settings_persistence._SETTINGS_ALLOWED_KEYS
_SETTINGS_ENUM_VALUES = _settings_persistence._SETTINGS_ENUM_VALUES
_SETTINGS_INT_RANGES = _settings_persistence._SETTINGS_INT_RANGES
_SETTINGS_FLOAT_RANGES = _settings_persistence._SETTINGS_FLOAT_RANGES
_SETTINGS_BOOL_KEYS = _settings_persistence._SETTINGS_BOOL_KEYS
_SETTINGS_LANG_RE = _settings_persistence._SETTINGS_LANG_RE
_SETTINGS_TTS_ENGINE_RE = _settings_persistence._SETTINGS_TTS_ENGINE_RE

# Mutable publication state remains owned by this compatibility facade because
# route_session_list_cache reads the version dynamically from api.config.
_SETTINGS_WRITE_VERSION = 0
_SETTINGS_WRITE_LOCK = threading.Lock()


def get_settings_write_version() -> int:
    """Return the process-local settings publication version."""
    with _SETTINGS_WRITE_LOCK:
        return _SETTINGS_WRITE_VERSION

_normalize_appearance = _settings_persistence._normalize_appearance
_read_raw_settings_file = _settings_persistence._read_raw_settings_file
_extract_persisted_speech_keys = (
    _settings_persistence._extract_persisted_speech_keys
)
persisted_speech_settings_keys = (
    _settings_persistence.persisted_speech_settings_keys
)
_settings_payload_for_write = _settings_persistence._settings_payload_for_write
load_settings = _settings_persistence.load_settings
_atomic_write_settings_text = _settings_persistence._atomic_write_settings_text
_current_umask = _settings_persistence._current_umask
_coerce_provider_cost_budget = _settings_persistence._coerce_provider_cost_budget
coerce_provider_cost_budget = _coerce_provider_cost_budget
save_settings = _settings_persistence.save_settings

_settings_persistence._apply_startup_settings()
# ── SESSIONS in-memory cache (LRU OrderedDict) ───────────────────────────────
SESSIONS: collections.OrderedDict = collections.OrderedDict()

# Run the provider-model seeder once at import time. Must be at the END of the
# module because _seed_provider_models_from_core() calls _get_label_for_model,
# which is defined ~3000 lines above. Placing the invocation earlier (e.g. right
# after the seeder's def) caused a NameError that the bare except silently
# swallowed — exactly when the seeder had real work to do (#4413).
try:
    _seed_provider_models_from_core()
except ImportError:
    pass  # hermes_cli not available (standalone deployment)
except Exception:
    logger.warning("provider-model seeder failed", exc_info=True)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
