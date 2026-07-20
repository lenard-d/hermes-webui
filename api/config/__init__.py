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

import copy
import importlib
import logging
import os
import sys
import threading
from pathlib import Path

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
_DEFAULT_STATE_HOME = Path(
    os.getenv("HERMES_HOME") or _DEFAULT_HERMES_HOME
).expanduser()

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

# Imported before config.yaml loading because env expansion consults this state
# during the package entrypoint's import-time reload.
from api.config.environment import (
    _clear_thread_env,
    _set_thread_env,
    _thread_ctx,
    clear_thread_env,
    environment_mutation_lock,
    is_process_env_fallback_blocked,
    set_thread_env,
    thread_env_scope,
)

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
DEFAULT_MODEL = os.getenv(
    "HERMES_WEBUI_DEFAULT_MODEL", ""
)  # Empty = use provider default; avoids showing unavailable OpenAI model to non-OpenAI users (#646)


# ── Shared file and toolset policies ─────────────────────────────────────────
from api.config.media_types import (
    CODE_EXTS,
    IMAGE_EXTS,
    MAX_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    MD_EXTS,
    MIME_MAP,
)
from api.config.toolsets import (
    DEFAULT_TOOLSETS as _DEFAULT_TOOLSETS,
    LEGACY_CLI_TOOLSET_ALIASES as _LEGACY_CLI_TOOLSET_ALIASES,
    normalize_cli_toolsets as _normalize_cli_toolsets,
    resolve_cli_toolsets as _resolve_cli_toolsets,
)

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
_legacy_custom_api_key_env_name = _provider_discovery._legacy_custom_api_key_env_name
_lookup_custom_api_key_env = _provider_discovery._lookup_custom_api_key_env
lookup_custom_api_key_env = _lookup_custom_api_key_env
_named_custom_provider_slug_for_base_url = (
    _provider_discovery._named_custom_provider_slug_for_base_url
)


_provider_is_known_or_configured = _provider_discovery._provider_is_known_or_configured


_seed_provider_models_from_core = _provider_discovery._seed_provider_models_from_core
_AMBIENT_GH_CLI_MARKERS = _provider_discovery._AMBIENT_GH_CLI_MARKERS
_AMBIENT_GH_ENV_SOURCES = _provider_discovery._AMBIENT_GH_ENV_SOURCES
_is_ambient_gh_cli_entry = _provider_discovery._is_ambient_gh_cli_entry
_format_ollama_label = _provider_discovery._format_ollama_label
format_ollama_model_label = _format_ollama_label
_format_nous_label = _provider_discovery._format_nous_label
_NOUS_FEATURED_THRESHOLD = _provider_discovery._NOUS_FEATURED_THRESHOLD
_NOUS_FEATURED_TARGET = _provider_discovery._NOUS_FEATURED_TARGET
_MODEL_PICKER_OVERFLOW_THRESHOLD = _provider_discovery._MODEL_PICKER_OVERFLOW_THRESHOLD
_MODEL_PICKER_VISIBLE_TARGET = _provider_discovery._MODEL_PICKER_VISIBLE_TARGET
MODEL_PICKER_OVERFLOW_THRESHOLD = _MODEL_PICKER_OVERFLOW_THRESHOLD
MODEL_PICKER_VISIBLE_TARGET = _MODEL_PICKER_VISIBLE_TARGET
_OPENROUTER_FREE_TIER_AUGMENT_CAP = (
    _provider_discovery._OPENROUTER_FREE_TIER_AUGMENT_CAP
)
_NOUS_VENDOR_PRIORITY = _provider_discovery._NOUS_VENDOR_PRIORITY
_build_nous_featured_set = _provider_discovery._build_nous_featured_set
_strip_picker_provider_hint = _provider_discovery._strip_picker_provider_hint
_model_matches_picker_selection = _provider_discovery._model_matches_picker_selection
_split_picker_overflow_models = _provider_discovery._split_picker_overflow_models
_apply_provider_prefix = _provider_discovery._apply_provider_prefix
_deduplicate_model_ids = _provider_discovery._deduplicate_model_ids

# Public provider-catalog interface.  Historical underscored names remain for
# compatibility, while cross-package callers use these semantic owner exports.
custom_provider_slug_from_name = _custom_provider_slug_from_name
format_nous_model_label = _format_nous_label
build_nous_featured_models = _build_nous_featured_set


#      api/config.py for SSRF host trust.
_LOCAL_SERVER_PROVIDERS = {
    "lmstudio",  # canonical (in hermes_cli.models.CANONICAL_PROVIDERS)
    "lm-studio",  # alias used in some custom_providers configs (#1625 Opus NIT)
    "ollama",  # via custom_providers, common pattern
    "llamacpp",  # via custom_providers
    "llama-cpp",  # alias
    "vllm",  # via custom_providers
    "tabby",  # via custom_providers (TabbyAPI)
    "tabbyapi",  # alias
    "koboldcpp",  # local llama.cpp UI fork
    "textgen",  # text-generation-webui (oobabooga) OpenAI-compat extension
    "localai",  # LocalAI project (#1625 Opus NIT)
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


from api.config.model_resolution import (
    canonical_model_provider_lane,
    get_effective_default_model,
    model_with_provider_context,
    provider_display_name,
    resolve_custom_provider_connection,
    resolve_model_provider,
)


# ── Reasoning config (CLI parity for /reasoning) ─────────────────────────────

from api.config import reasoning as _model_reasoning

VALID_REASONING_EFFORTS = _model_reasoning.VALID_REASONING_EFFORTS
_NESTED_ROUTE_PATTERN = _model_reasoning._NESTED_ROUTE_PATTERN
_KNOWN_REASONING_PROVIDERS = _model_reasoning._KNOWN_REASONING_PROVIDERS

parse_reasoning_effort = _model_reasoning.parse_reasoning_effort
_strip_provider_hint_for_reasoning = _model_reasoning._strip_provider_hint_for_reasoning
_reasoning_name_candidates = _model_reasoning._reasoning_name_candidates
_candidate_supports_reasoning = _model_reasoning._candidate_supports_reasoning
_nested_route_reasoning_denied = _model_reasoning._nested_route_reasoning_denied
_nested_gateway_route_reasoning = _model_reasoning._nested_gateway_route_reasoning
_zai_glm_classification = _model_reasoning._zai_glm_classification
_zai_glm_reasoning_efforts_supported = (
    _model_reasoning._zai_glm_reasoning_efforts_supported
)
_zai_glm_thinking_toggle_supported = _model_reasoning._zai_glm_thinking_toggle_supported
_filter_reasoning_efforts_for_provider = (
    _model_reasoning._filter_reasoning_efforts_for_provider
)
_provider_known_reasoning_capable = _model_reasoning._provider_known_reasoning_capable
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
_lmstudio_model_reasoning_options = _model_reasoning._lmstudio_model_reasoning_options
resolve_model_reasoning_efforts = _model_reasoning.resolve_model_reasoning_efforts
_resolve_model_reasoning_efforts_impl = (
    _model_reasoning._resolve_model_reasoning_efforts_impl
)
coerce_reasoning_effort_for_model = _model_reasoning.coerce_reasoning_effort_for_model
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
_main_model_supports_service_tier = _model_settings._main_model_supports_service_tier
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
_load_stale_models_cache_from_disk = (
    _models_cache_impl._load_stale_models_cache_from_disk
)
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

# ── Process-local session coordination ───────────────────────────────────────
# Max compact Session objects held in the in-memory LRU (issue #3506, #4765).
# Lighter than the agent cache (no live agent runtime), but still bounded so a
# long-running self-hosted install cannot accumulate every session it ever
# touched in RAM and eventually segfault (the #4765/#2233/#4633 crash cluster).
#
# Precedence for the effective cap is resolved by get_sessions_cache_max():
#   1. config.yaml  webui.sessions_cache_max   (preferred, no new env var)
#   2. HERMES_WEBUI_SESSIONS_MAX env var        (legacy operator override)
#   3. DEFAULT_SESSIONS_CACHE_MAX               (sane bounded default)
from api.config.session_limits import (
    DEFAULT_SESSIONS_CACHE_MAX,
    SESSIONS_MAX,
    get_sessions_cache_max,
)
from api.session_state import (
    BG_TASK_COMPLETE_EVENTS_SEEN,
    BG_TASK_COMPLETE_EVENTS_SEEN_LOCK,
    CHAT_LOCK,
    DEFERRED_PROCESS_WAKEUPS,
    DEFERRED_PROCESS_WAKEUPS_LOCK,
    LOCK,
    PENDING_BG_TASK_COMPLETIONS,
    PENDING_GOAL_CONTINUATION,
    PROCESS_SESSION_INDEX,
    PROCESS_SESSION_INDEX_LOCK,
    SERVER_START_TIME,
    SESSIONS,
    SESSION_AGENT_LOCKS,
    SESSION_AGENT_LOCKS_LOCK,
    SESSION_CHANNEL_IDLE_TTL_SECS,
    SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS,
    alias_session_agent_lock,
    session_agent_lock,
)

_get_session_agent_lock = session_agent_lock


from api.stream_channel import StreamChannel, create_stream_channel  # noqa: F401 - compatibility exports
from api.runtime_state import (
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

# Gateway approval routing consumes this small public Interface.  Cache storage
# remains private to its semantic owner.
from api.config.gateway_capabilities import (
    gateway_approval_unavailable_reason,
    gateway_supports_approval,
    get_gateway_caps,
    invalidate_gateway_caps,
)

LAST_RUN_FINISHED_AT: float | None = None


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

# The reusable-agent owner spans run and session packages.  Compatibility
# exports below are aliases to the canonical objects, not copied state.
from api.agent_cache import (
    SESSION_AGENT_CACHE,
    SESSION_AGENT_CACHE_LOCK,
    SESSION_AGENT_CACHE_MAX,
    evict_session_agent,
)

_evict_session_agent = evict_session_agent


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
_extract_persisted_speech_keys = _settings_persistence._extract_persisted_speech_keys
persisted_speech_settings_keys = _settings_persistence.persisted_speech_settings_keys
_settings_payload_for_write = _settings_persistence._settings_payload_for_write
load_settings = _settings_persistence.load_settings
_atomic_write_settings_text = _settings_persistence._atomic_write_settings_text
_current_umask = _settings_persistence._current_umask
_coerce_provider_cost_budget = _settings_persistence._coerce_provider_cost_budget
coerce_provider_cost_budget = _coerce_provider_cost_budget
save_settings = _settings_persistence.save_settings

_settings_persistence._apply_startup_settings()

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
