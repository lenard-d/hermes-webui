"""
Hermes Web UI -- Shared configuration, constants, and global state.
Imported by all other api/* modules and by server.py.

Discovery order for all paths:
  1. Explicit environment variable
  2. Filesystem heuristics (sibling checkout, parent dir, common install locations)
  3. Hardened defaults relative to $HOME
  4. Fail loudly with a human-readable fix-it message if required modules are missing
"""

import collections
import copy
import hashlib
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
from pathlib import Path
from typing import Any

from api.runtime_state import ProcessRuntimeState
from urllib.parse import parse_qs, urlparse

from api.config_parts.facade import bind_config_api


# One late-bound compatibility facade serves every extracted config module.
# Resolve the module object at call time so reloads and monkeypatches remain
# observable instead of being captured during import.
bind_config_api(lambda: sys.modules[__name__])

# ── Basic layout ──────────────────────────────────────────────────────────────
import api.paths as _paths
from api.plugin_providers import (
    effective_provider_display_name as _effective_provider_display_name,
    is_plugin_model_provider as _is_plugin_model_provider,
    plugin_model_provider_profiles as _plugin_model_provider_profiles,
)

HOME = _paths.HOME
_hermes_home_has_webui_state = _paths._hermes_home_has_webui_state
_platform_default_hermes_home = _paths._platform_default_hermes_home

# REPO_ROOT is the directory that contains this file's parent (api/ -> repo root)
REPO_ROOT = Path(__file__).parent.parent.resolve()

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
from api.config_parts import path_env as _path_env

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

# ── Config file state (reloadable -- supports profile switching) ─────────────
from api.config_parts import config_io as _config_io

_thread_local_env_value = _config_io._thread_local_env_value
_expand_env_vars = _config_io._expand_env_vars


_cfg_cache = {}
_cfg_lock = threading.Lock()
_cfg_mtime: float = 0.0  # last known mtime of config.yaml; 0 = never loaded
_cfg_path: Path | None = None  # active config.yaml path for the disk-loaded cache
_cfg_fingerprint: str | None = None  # serialized snapshot from the last disk load


_fingerprint_config = _config_io._fingerprint_config
_cfg_has_in_memory_overrides = _config_io._cfg_has_in_memory_overrides
_get_config_path = _config_io._get_config_path


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

CLI_TOOLSETS = _resolve_cli_toolsets()

# ── Model / provider discovery ───────────────────────────────────────────────

from api.model_catalog import (
    FALLBACK_MODELS as _FALLBACK_MODELS,
    PROVIDER_ALIASES as _PROVIDER_ALIASES,  # noqa: F401 - facade-owned state
    PROVIDER_DISPLAY as _PROVIDER_DISPLAY,
    PROVIDER_MODELS as _PROVIDER_MODELS,
)
from api.config_parts import provider_discovery as _provider_discovery


_get_anthropic_fallback_env_vars = _provider_discovery._get_anthropic_fallback_env_vars
_resolve_provider_alias = _provider_discovery._resolve_provider_alias


_is_known_model_provider = _provider_discovery._is_known_model_provider


_custom_provider_slug_from_name = _provider_discovery._custom_provider_slug_from_name
_custom_provider_entries = _provider_discovery._custom_provider_entries
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
_named_custom_provider_slug_for_base_url = (
    _provider_discovery._named_custom_provider_slug_for_base_url
)


_provider_is_known_or_configured = (
    _provider_discovery._provider_is_known_or_configured
)




def _seed_provider_models_from_core() -> None:
    """Enrich existing provider model lists with missing IDs from hermes_cli.

    The core's _PROVIDER_MODELS is the authoritative curated list of agent-capable
    models per provider.  The WebUI's static dict above is a display-oriented copy
    (with {id, label} entries) that can go stale when new models are added to the
    core without a matching WebUI update.  This function bridges the gap by
    injecting any missing model IDs from the core into **existing** WebUI provider
    entries.

    Constrains seeding to providers already in the WebUI catalog — does NOT add
    brand-new providers.  Adding new vendors is a maintainer curation decision.
    Respects per-provider ID conventions (e.g. nous uses @nous:-prefixed IDs).

    Safe to call multiple times; only missing entries are added.  Silently no-ops
    if hermes_cli is not importable (standalone WebUI deployments).

    Must be called AFTER ``_get_label_for_model`` is defined (module-level
    invocation is at the bottom of this module, not here).
    """
    try:
        from hermes_cli.models import _PROVIDER_MODELS as _core_pm
    except ImportError:
        return

    # Build a canonical-id → WebUI-key lookup so that providers whose canonical
    # form differs between core and WebUI (e.g. core uses "xai" but WebUI
    # indexes by "x-ai") merge into the existing entry instead of creating a
    # duplicate (#4413).
    _webui_key_by_canonical: dict[str, str] = {}
    for _wk in _PROVIDER_MODELS:
        try:
            _canon = _resolve_provider_alias(_wk)
        except Exception:
            _canon = _wk
        if _canon not in _webui_key_by_canonical:
            _webui_key_by_canonical[_canon] = _wk

    for provider_id, core_models in _core_pm.items():
        if not isinstance(core_models, list):
            continue

        # Resolve the core's provider_id to the WebUI's key for this provider.
        webui_key = provider_id
        webui_list = _PROVIDER_MODELS.get(provider_id)
        if webui_list is None:
            try:
                _canon_pid = _resolve_provider_alias(provider_id)
            except Exception:
                _canon_pid = provider_id
            webui_key = _webui_key_by_canonical.get(_canon_pid, provider_id)
            webui_list = _PROVIDER_MODELS.get(webui_key)

        if webui_list is None:
            # Provider exists in core but not in the WebUI catalog.
            # Do NOT seed — adding new vendors is a maintainer curation
            # decision, not something the seeder should do implicitly (#4413).
            continue
        if not isinstance(webui_list, list):
            continue
        # Provider exists in both — inject missing model IDs.
        # Detect per-provider ID prefix convention (e.g. nous uses @nous:).
        # The merge must respect each provider's existing ID format rather
        # than injecting the core's raw IDs (#4413).
        _existing_ids_raw: list[str] = [
            (m.get("id") if isinstance(m, dict) else str(m)) or ""
            for m in webui_list
            if isinstance(m, dict) and m.get("id")
        ]
        _prefix = ""
        if _existing_ids_raw and all(i.startswith("@") and ":" in i for i in _existing_ids_raw):
            _prefix = _existing_ids_raw[0].split(":", 1)[0] + ":"

        def _strip_prefix(mid: str, prefix: str = _prefix) -> str:
            if prefix and mid.startswith(prefix):
                return mid[len(prefix):]
            return mid

        existing_ids = {
            _strip_prefix(mid).replace("-", ".").lower()
            for mid in _existing_ids_raw
        }
        for mid in core_models:
            if not isinstance(mid, str) or not mid.strip():
                continue
            normed = mid.strip().replace("-", ".").lower()
            if normed not in existing_ids:
                inject_id = (_prefix + mid.strip()) if _prefix else mid.strip()
                webui_list.append({
                    "id": inject_id,
                    "label": _get_label_for_model(mid.strip(), []),
                })


_AMBIENT_GH_CLI_MARKERS = frozenset({"gh_cli", "gh auth token"})

# Environment variable sources that are auto-detected and should be filtered
# when the token is a classic PAT (ghp_*) that Copilot API doesn't support.
# Note: COPILOT_GITHUB_TOKEN is NOT included here - it's user-specific config.
_AMBIENT_GH_ENV_SOURCES = frozenset({"env:github_token", "env:gh_token"})


def _is_ambient_gh_cli_entry(source: str, label: str, key_source: str) -> bool:
    """True when a credential-pool entry is a seeded gh-cli token rather than
    one the user added explicitly. Filter these so Copilot doesn't appear in
    the dropdown just because `gh` is installed on the system.

    Also filters GITHUB_TOKEN and GH_TOKEN env var entries, which are
    auto-detected from the environment and should not cause Copilot to appear
    in the picker when the token is a classic PAT (ghp_*) that Copilot API
    doesn't support.

    Note: COPILOT_GITHUB_TOKEN is NOT filtered - it's user-specific config
    that should always be respected.
    """
    source_lower = source.strip().lower()
    return (
        source_lower in _AMBIENT_GH_CLI_MARKERS
        or source_lower in _AMBIENT_GH_ENV_SOURCES
        or label.strip().lower() == "gh auth token"
        or key_source.strip().lower() == "gh auth token"
    )


def _format_ollama_label(mid: str) -> str:
    """Turn an Ollama model id (Ollama tag format) into a readable display label.

    Examples: 'kimi-k2.5' → 'Kimi K2.5', 'qwen3-vl:235b-instruct' → 'Qwen3 VL (235B Instruct)'
    """
    name_part, _, variant = mid.partition(":")

    def _fmt(s: str) -> str:
        tokens = s.replace("-", " ").replace("_", " ").split()
        out = []
        for t in tokens:
            alpha_only = t.replace(".", "")
            if alpha_only.isalpha() and len(t) <= 3:
                out.append(t.upper())  # short acronym: glm → GLM, vl → VL, gpt → GPT
            elif alpha_only.isalnum() and alpha_only and alpha_only[0].isdigit():
                out.append(t.upper())  # size param: 235b → 235B, 1t → 1T
            else:
                out.append(t[0].upper() + t[1:] if t else t)  # capitalize: kimi → Kimi
        return " ".join(out)

    label = _fmt(name_part)
    if variant:
        label += f" ({_fmt(variant)})"
    return label


def _format_nous_label(mid: str) -> str:
    """Turn a Nous Portal model id into a readable display label.

    Nous IDs are ``<vendor>/<model>[:<variant>]`` (e.g. ``anthropic/claude-opus-4.7``);
    drop the vendor namespace, prettify the model name with the same token
    rules as :func:`_format_ollama_label` (short acronyms uppercase, size
    suffixes uppercase, capitalize the rest), then append ``" (via Nous)"``
    so the entry is visually distinct from same-named models in other
    provider groups (e.g. direct Anthropic).

    Examples (matches the helper's actual output — labels are produced by
    :func:`_format_ollama_label`'s token rules, so 3-letter tokens like
    ``GPT`` and ``PRO`` render uppercase)::

        anthropic/claude-opus-4.7         -> Claude Opus 4.7 (via Nous)
        openai/gpt-5.4-mini               -> GPT 5.4 Mini (via Nous)
        google/gemini-3.1-pro-preview     -> Gemini 3.1 PRO Preview (via Nous)
        moonshotai/kimi-k2.6              -> Kimi K2.6 (via Nous)
        qwen/qwen3.5-plus-02-15           -> Qwen3.5 Plus 02 15 (via Nous)
        nvidia/nemotron-3-super-120b-a12b -> Nemotron 3 Super 120B A12b (via Nous)
        minimax/minimax-m2.5:free         -> MiniMax M2.5 (Free) (via Nous)
    """
    name_part = mid.split("/", 1)[-1] if "/" in mid else mid
    # MiniMax-CN ids come back lowercase on the live wire (`minimax-m2.5`) but
    # the curated label convention is mixed-case "MiniMax M2.5" — match that.
    if name_part.lower().startswith("minimax"):
        name_part = "MiniMax" + name_part[len("minimax"):]
    base = _format_ollama_label(name_part)
    return f"{base} (via Nous)"


# Soft cap on how many Nous Portal models surface in the picker dropdown.
# Above this count, _build_nous_featured_set() trims the visible list to
# ~_NOUS_FEATURED_TARGET entries; the full catalog is still returned to the
# client under ``extra_models`` so /model autocomplete covers everything.
# Caps reflect human scannability — a 25-row dropdown is the practical UX
# ceiling, and per-vendor sampling at 15 keeps the flagship shape visible
# without one vendor dominating.
_NOUS_FEATURED_THRESHOLD = 25
_NOUS_FEATURED_TARGET = 15
_MODEL_PICKER_OVERFLOW_THRESHOLD = _NOUS_FEATURED_THRESHOLD
_MODEL_PICKER_VISIBLE_TARGET = _NOUS_FEATURED_TARGET
_OPENROUTER_FREE_TIER_AUGMENT_CAP = 30

# Vendor-prefix priority order for featured selection. Lower index = picked
# earlier when sampling the live catalog. Reflects which vendors users have
# historically reached for first via Nous Portal (driven by the curated
# static list maintained in _PROVIDER_MODELS["nous"] and Discord feedback).
_NOUS_VENDOR_PRIORITY = (
    "anthropic", "openai", "google", "moonshotai", "z-ai",
    "minimax", "qwen", "x-ai", "deepseek", "stepfun",
    "xiaomi", "tencent", "nvidia", "arcee-ai",
)


def _build_nous_featured_set(
    live_ids: list[str],
    *,
    selected_model_id: str | None = None,
    target: int = _NOUS_FEATURED_TARGET,
) -> tuple[list[str], list[str]]:
    """Trim a Nous Portal catalog into a (featured, extras) split.

    ``featured`` is what the picker dropdown renders. ``extras`` is everything
    else — kept available so the slash-command `/model` autocomplete and the
    ``_dynamicModelLabels`` map cover the full catalog.

    Selection rules (in order, deterministic):

    1. Always include the user's currently-selected model if it's in the
       catalog (preserves selection stickiness — no orphan IDs in the
       dropdown after a refresh).
    2. Always include every entry from the curated static
       ``_PROVIDER_MODELS["nous"]`` list whose id maps onto a live id —
       those four are explicitly maintained as flagship picks.
    3. Top up to ``target`` by walking ``_NOUS_VENDOR_PRIORITY`` round-robin
       (one model per vendor each pass) so no vendor monopolises the slot
       budget. Within a vendor, the original ``live_ids`` order is preserved
       — that's the order Nous Portal returned, which approximates recency.

    Returns ``(featured_ids, extras_ids)`` — both lists are subsets of
    ``live_ids`` with disjoint membership and union equal to ``live_ids``.

    For catalogs ≤ ``_NOUS_FEATURED_THRESHOLD`` entries the function is a
    no-op: ``featured == live_ids``, ``extras == []``.
    """
    if not live_ids:
        return [], []
    if len(live_ids) <= _NOUS_FEATURED_THRESHOLD:
        return list(live_ids), []

    chosen: list[str] = []  # preserves insertion order
    chosen_set: set[str] = set()

    def _add(mid: str) -> None:
        if mid and mid not in chosen_set:
            chosen.append(mid)
            chosen_set.add(mid)

    # Rule 1: sticky selection. Strip "@nous:" prefix if present so we can
    # match against the live id space (which is bare "vendor/model").
    if selected_model_id:
        sel = selected_model_id
        if sel.startswith("@nous:"):
            sel = sel[len("@nous:"):]
        if sel in live_ids:
            _add(sel)

    # Rule 2: curated flagships. Extract the bare ids from the static list
    # entries (which are stored as "@nous:vendor/model").
    for static in _PROVIDER_MODELS.get("nous", []):
        sid = static.get("id", "")
        if sid.startswith("@nous:"):
            sid = sid[len("@nous:"):]
        if sid in live_ids:
            _add(sid)

    # Rule 3: vendor-priority round-robin top-up.
    by_vendor: dict[str, list[str]] = {}
    for mid in live_ids:
        if mid in chosen_set:
            continue
        vendor = mid.split("/", 1)[0] if "/" in mid else ""
        by_vendor.setdefault(vendor, []).append(mid)

    # Walk vendors in priority order, then any leftover vendors alphabetically.
    priority = list(_NOUS_VENDOR_PRIORITY)
    leftover = sorted(v for v in by_vendor if v not in set(priority))
    vendor_order = priority + leftover

    # Round-robin: one model per vendor per pass until we hit the target or
    # exhaust every bucket.
    while len(chosen) < target:
        added_this_pass = 0
        for vendor in vendor_order:
            if len(chosen) >= target:
                break
            bucket = by_vendor.get(vendor)
            if not bucket:
                continue
            _add(bucket.pop(0))
            added_this_pass += 1
        if added_this_pass == 0:
            break  # all buckets empty

    # Anything not chosen becomes extras (full-catalog completion surface).
    extras = [m for m in live_ids if m not in chosen_set]
    return chosen, extras


def _strip_picker_provider_hint(model_id: str) -> str:
    mid = str(model_id or "").strip()
    if mid.startswith("@") and ":" in mid:
        return mid[mid.index(":") + 1 :]
    return mid


def _model_matches_picker_selection(
    model_id: str,
    selected_model_id: str | None,
    provider_id: str | None = None,
) -> bool:
    selected = str(selected_model_id or "").strip()
    candidate = str(model_id or "").strip()
    if not selected or not candidate:
        return False
    if candidate == selected:
        return True

    selected_bare = _strip_picker_provider_hint(selected)
    candidate_bare = _strip_picker_provider_hint(candidate)
    if selected_bare != candidate_bare:
        return False

    selected_provider = ""
    if selected.startswith("@") and ":" in selected:
        selected_provider = selected[1 : selected.index(":")].lower()
    candidate_provider = str(provider_id or "").strip().lower()
    if candidate.startswith("@") and ":" in candidate:
        candidate_provider = candidate[1 : candidate.index(":")].lower()

    return not selected_provider or not candidate_provider or selected_provider == candidate_provider


def _split_picker_overflow_models(
    ordered_models: list[dict],
    *,
    selected_model_id: str | None = None,
    provider_id: str | None = None,
    threshold: int = _MODEL_PICKER_OVERFLOW_THRESHOLD,
    target: int = _MODEL_PICKER_VISIBLE_TARGET,
) -> tuple[list[dict], list[dict]]:
    """Split an ordered picker catalog into visible rows plus an overflow tail."""
    models = [copy.deepcopy(m) for m in (ordered_models or []) if isinstance(m, dict) and m.get("id")]
    if len(models) <= threshold:
        return models, []

    visible = models[:target]
    extras = models[target:]
    if not selected_model_id:
        return visible, extras

    if any(_model_matches_picker_selection(m.get("id", ""), selected_model_id, provider_id) for m in visible):
        return visible, extras

    for idx, model in enumerate(extras):
        if not _model_matches_picker_selection(model.get("id", ""), selected_model_id, provider_id):
            continue
        displaced = visible[-1]
        visible[-1] = model
        extras[idx] = displaced
        break
    return visible, extras


def _apply_provider_prefix(
    raw_models: list[dict],
    provider_id: str,
    active_provider: str | None,
) -> list[dict]:
    """Return *raw_models* with @provider: prefixes applied when needed.

    Prefixing is skipped when (a) the provider is already the active one, or
    (b) a model id already starts with '@' or contains '/' (already routable).
    """
    _active = (active_provider or "").lower()
    if not _active or provider_id == _active:
        return list(raw_models)
    result = []
    for m in raw_models:
        mid = m["id"]
        entry = dict(m)
        if mid.startswith("@") or "/" in mid:
            result.append(entry)
        else:
            entry["id"] = f"@{provider_id}:{mid}"
            result.append(entry)
    return result


def _deduplicate_model_ids(groups: list[dict]) -> None:
    """Ensure every model ID across groups is globally unique.

    When multiple providers expose the same model ID (either bare names like
    ``gpt-5.4`` or slash-qualified IDs like ``google/gemma-4-27b``), the
    dropdown cannot distinguish them. This post-process detects such
    collisions and prefixes colliding entries with ``@provider_id:`` so the
    frontend can treat them as distinct options.

    The first occurrence (in provider-id order) is left unchanged for backward
    compatibility with sessions that already store the original bare/slash
    model name. If that provider is later removed from the config, the next
    cache rebuild re-runs dedup — the remaining provider becomes the sole
    occurrence and is left unchanged, so the session still matches.

    .. note::
       The "first occurrence wins" rule means the unchanged ID is not stable
       across config changes (adding, removing, or reordering providers).
       This is acceptable because the dedup runs on every cache rebuild,
       so sessions always resolve to the current canonical unchanged ID.

    The ``@provider_id:model`` format is consistent with the existing
    ``_apply_provider_prefix()`` function and is handled by
    ``resolve_model_provider()`` (rsplits on the last ``:`` to handle
    provider_ids that themselves contain ``:``).

    Operates in-place on *groups*.
    """
    if not groups:
        return

    # Collect {model_id: [(group_idx, bucket_name, model_idx), ...]} in
    # alphabetical provider_id order so that the "first occurrence stays
    # unchanged" rule is deterministic across config edits
    # (adding/removing/reordering providers). Include ``extra_models`` too:
    # slash-command resolution and picker filtering consume the full catalog.
    sorted_group_indices = sorted(
        range(len(groups)),
        key=lambda i: groups[i].get("provider_id", ""),
    )
    id_map: dict[str, list[tuple[int, str, int]]] = {}
    for gi in sorted_group_indices:
        group = groups[gi]
        for bucket_name in ("models", "extra_models"):
            for mi, model in enumerate(group.get(bucket_name, []) or []):
                mid = str(model.get("id", "") or "").strip()
                # Skip IDs that are already provider-qualified.
                if not mid or mid.startswith("@"):
                    continue
                id_map.setdefault(mid, []).append((gi, bucket_name, mi))

    # For any ID appearing in 2+ groups, prefix all but the first occurrence.
    # This handles N>2 providers correctly: the loop iterates over all
    # occurrences after the first, prefixing each with its own provider_id.
    for original_id, locations in id_map.items():
        if len(locations) < 2:
            continue
        for gi, bucket_name, mi in locations[1:]:
            group = groups[gi]
            model = group[bucket_name][mi]
            pid = group.get("provider_id", "")
            model["id"] = f"@{pid}:{original_id}"
            provider_name = group.get("provider", pid)
            if model.get("label") != original_id:
                model["label"] = f"{model['label']} ({provider_name})"
            else:
                model["label"] = f"{original_id} ({provider_name})"


# ── Local-server provider preservation (#1625) ─────────────────────────────
#
# LM Studio, Ollama, llama.cpp, vLLM, TabbyAPI etc. are inference servers,
# not OpenAI-compatible proxies. They register models under their FULL path
# as the registry key (the HuggingFace-style "namespace/model" id, e.g.
# "qwen/qwen3.6-27b"). Stripping the namespace prefix would cause a registry
# miss and the server loads a brand-new instance with default settings,
# silently ignoring the user's tuned context length / parallel slots.
#
# This is distinct from OpenAI-compatible proxies (LiteLLM, OpenRouter relays)
# where stripping "openai/gpt-5.4" → "gpt-5.4" is the correct behavior.
#
# Detection has two layers:
#   1. Static set of known local-server provider names (canonical + common
#      custom-provider naming).
#   2. Loopback / private-host base_url heuristic: an OpenAI-compatible URL
#      pointing at 127.0.0.1, localhost, or a private IP block is almost
#      certainly a local model server, regardless of the provider name.
#      Reuses the same private-IP detection logic used elsewhere in
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
from api.config_parts import provider_routing as _provider_routing


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
                _advertised = _endpoint_advertised_model_ids(config_provider)
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

from api.config_parts import model_reasoning as _model_reasoning

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


from api.config_parts import model_settings as _model_settings

_parse_positive_int_config_value = _model_settings._parse_positive_int_config_value
get_max_tokens_status = _model_settings.get_max_tokens_status
set_max_tokens = _model_settings.set_max_tokens
set_reasoning_display = _model_settings.set_reasoning_display
set_reasoning_effort = _model_settings.set_reasoning_effort
_public_advanced_model_options = _model_settings._public_advanced_model_options
_is_openai_family_provider = _model_settings._is_openai_family_provider
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
_annotate_fast_tier_model_groups = _model_settings._annotate_fast_tier_model_groups
_public_main_service_tier = _model_settings._public_main_service_tier
_main_model_request_overrides = _model_settings._main_model_request_overrides
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


# ── TTL cache for get_available_models() ─────────────────────────────────────
_available_models_cache: dict | None = None
_available_models_cache_ts: float = 0.0
_available_models_live_rebuild_ts: float = 0.0
_available_models_cache_source_fingerprint: dict | None = None
_AVAILABLE_MODELS_CACHE_TTL: float = 86400.0  # 24 hours
_SESSION_VISIT_MODELS_FRESHNESS_SECONDS: float = 300.0
_available_models_cache_lock = threading.RLock()  # must be RLock: cold path refactoring moved slow work inside this lock, requiring re-entry
_cache_build_cv = threading.Condition(_available_models_cache_lock)  # shares underlying RLock so notify_all() is safe inside with _available_models_cache_lock
_cache_build_in_progress = False  # True while a cold path is actively building
_models_cache_build_generation = 0
_active_models_cache_build_generation: int | None = None


def _invalidate_models_build_locked() -> None:
    """Revoke publication rights from any detached catalog rebuild."""
    global _models_cache_build_generation, _active_models_cache_build_generation
    global _cache_build_in_progress
    _models_cache_build_generation += 1
    _active_models_cache_build_generation = None
    _cache_build_in_progress = False
    _cache_build_cv.notify_all()

# Memoized (snapshot_ref, {provider_slug: frozenset(model_ids)}) derived from
# the published models-catalog snapshot. Used by _endpoint_advertised_model_ids
# to answer "did this endpoint actually advertise this exact id?" in O(1) per
# send without rebuilding. Keyed on the snapshot object identity so it is
# recomputed exactly once per catalog publish (the cache is replaced wholesale,
# never mutated) and can never serve stale ids from a superseded catalog.
_advertised_model_ids_memo: tuple | None = None

# Atomic provenance pair: an immutable (snapshot, publisher_fingerprint) tuple
# published together at every catalog publish/invalidate site via
# _sync_models_cache_provenance(). The resolver reads THIS single global with one
# lock-free load so it can never observe a torn snapshot/fingerprint pair (the
# two underlying globals are assigned as separate statements). Reading a tuple is
# atomic under the GIL and, crucially, acquires NO lock — so the per-send
# provenance check introduces no lock-ordering edge (avoids the _cfg_lock ↔
# _available_models_cache_lock deadlock) and never waits behind a catalog rebuild.
_models_cache_provenance: tuple | None = None


def _sync_models_cache_provenance() -> None:
    """Republish the atomic (snapshot, fingerprint) provenance pair.

    MUST be called at every site that assigns ``_available_models_cache`` and
    ``_available_models_cache_source_fingerprint`` (publish and invalidate),
    AFTER both have been set. It snapshots the current pair into one immutable
    tuple so ``_endpoint_advertised_model_ids`` reads both consistently with a
    single lock-free load. A reader that races between an underlying assignment
    and this call sees the PREVIOUS consistent tuple (never a torn pair); once
    this runs, readers see the new consistent pair.
    """
    global _models_cache_provenance
    snap = _available_models_cache
    _models_cache_provenance = (
        (snap, _available_models_cache_source_fingerprint) if snap is not None else None
    )


def _endpoint_advertised_model_ids(provider_id: str | None) -> frozenset | None:
    """Model ids the given provider's group advertised in the current catalog.

    Reads ONLY the already-published in-memory catalog snapshot
    (``_available_models_cache``) — it never builds, live-probes, or touches
    disk, so it is safe to call on the per-turn send hot path. Returns:

      * a ``frozenset`` of the ids advertised by ``provider_id``'s own group
        (bare ids for the active provider, e.g. ``x-ai/grok-4.5``), or
      * ``None`` when the catalog is cold/unbuilt OR the provider has no group.

    ``None`` means "no provenance signal available" — callers MUST treat that as
    "preserve the model id verbatim" so a cache miss never silently strips a
    vendor namespace off an id the user actively selected (#5979). Scoping to
    the provider's OWN group prevents a same-named id in a sibling group (e.g.
    an ``openai/gpt-5.4`` sitting in the OpenRouter group) from masquerading as
    something this custom endpoint advertised.
    """
    global _advertised_model_ids_memo
    # Single lock-free atomic read of the immutable (snapshot, fingerprint) pair
    # published by _sync_models_cache_provenance(). Reading one tuple can never
    # tear, and acquiring no lock means this per-send check adds no lock-ordering
    # edge (no _cfg_lock ↔ _available_models_cache_lock deadlock) and never waits
    # behind a catalog rebuild.
    provenance = _models_cache_provenance
    if provenance is None:
        return None
    snapshot, published_fp = provenance
    if snapshot is None:
        return None
    # Profile-isolation fail-safe (profiles are islands): the catalog cache is a
    # process global, so a concurrently-active profile could have published the
    # snapshot we're now reading. Only trust it for provenance when the
    # fingerprint captured AT PUBLISH TIME still matches the current runtime
    # fingerprint — the ``config_yaml`` axis of that fingerprint is the
    # PROFILE-SPECIFIC config path (_get_config_path -> get_active_hermes_home),
    # so a match guarantees the snapshot belongs to the profile asking. Any
    # mismatch (foreign profile, config edit, stale) returns None so the caller
    # preserves the id verbatim rather than stripping against another profile's
    # catalog.
    try:
        if published_fp != _models_cache_source_fingerprint():
            return None
    except Exception:
        return None  # fingerprint unavailable → no trustworthy provenance
    memo = _advertised_model_ids_memo
    # Identity check (``is``), not id(): holding the snapshot reference in the
    # memo keeps it alive, so a freed-then-reused id() can't cause a false hit.
    if memo is None or memo[0] is not snapshot:
        by_slug: dict[str, frozenset] = {}
        try:
            groups = snapshot.get("groups", []) or []
        except AttributeError:
            return None
        for group in groups:
            if not isinstance(group, dict):
                continue
            slug = str(group.get("provider_id") or "").strip().lower()
            if not slug:
                continue
            # Union BOTH catalog buckets: a provider's models can be split across
            # ``models`` (visible) and ``extra_models`` (overflow) by the picker,
            # so an id the endpoint genuinely advertised may live in either. Only
            # reading ``models`` would miss it and mis-resolve (e.g. leave the
            # #433 bare id unstripped because it sits in extra_models).
            ids = frozenset(
                str(m.get("id"))
                for bucket in ("models", "extra_models")
                for m in (group.get(bucket) or [])
                if isinstance(m, dict) and m.get("id")
            )
            by_slug[slug] = by_slug.get(slug, frozenset()) | ids
        memo = (snapshot, by_slug)
        _advertised_model_ids_memo = memo
    slug = str(provider_id or "").strip().lower()
    return memo[1].get(slug)


# Hard wall-clock budget for a COLD live provider-catalog rebuild when it is
# run from a foreground request path. The live rebuild does one network probe
# per detected provider (Copilot token-exchange HTTPS, OpenRouter /v1/models,
# Nous /models, ...). On a flaky / corp / WSL network any single probe can
# stall for its full per-call timeout (Copilot urllib timeout=10s) and, summed
# across N providers, block the request thread for tens of seconds. This bounds
# the time a foreground caller will wait: past the budget it returns a usable
# fallback (last-known disk cache or a network-free minimal catalog) and lets
# the rebuild finish out-of-band and populate the cache for the next call.
# Set HERMES_WEBUI_MODELS_REBUILD_BUDGET=0 to restore the legacy synchronous
# (unbounded) behaviour.
try:
    _LIVE_REBUILD_BUDGET_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_MODELS_REBUILD_BUDGET", "4") or "4"
    )
except (TypeError, ValueError):
    _LIVE_REBUILD_BUDGET_SECONDS = 4.0


# ── Budget-exceeded warning rate-limit ───────────────────────────────────────
# Q-2979-A3 / Copilot discussion_r3305864400: the live-rebuild-budget-exceeded
# warning at _invoke_models_rebuild's slow-path is potentially high-volume —
# every provider catalog refresh that runs past _LIVE_REBUILD_BUDGET_SECONDS
# emits one, so a hung upstream probe (or a sustained burst of cold callers)
# could flood the log at warning level. Rate-limit per reason: the FIRST
# occurrence in a cooldown window logs at warning; subsequent occurrences in
# the same window log at info (so log signal stays useful but volume bounded).
# Override the default cooldown via HERMES_WEBUI_BUDGET_WARN_COOLDOWN (seconds).
try:
    _BUDGET_WARN_COOLDOWN_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_BUDGET_WARN_COOLDOWN", "300") or "300"
    )
except (TypeError, ValueError):
    _BUDGET_WARN_COOLDOWN_SECONDS = 300.0

_BUDGET_WARN_STATE: dict[str, float] = {}
_BUDGET_WARN_LOCK = threading.Lock()


def _should_warn_budget(reason: str, cooldown_s: float | None = None) -> bool:
    """Return True iff the budget warning for ``reason`` should log at
    warning level (first hit, or last warn-level emit was more than
    ``cooldown_s`` seconds ago). Otherwise False — the caller should demote
    to info for the same payload so the signal is retained but the noise is
    capped. Thread-safe; the cooldown is shared across all live-rebuild
    callers in this process.
    """
    cooldown = (
        _BUDGET_WARN_COOLDOWN_SECONDS if cooldown_s is None else float(cooldown_s)
    )
    now = time.monotonic()
    with _BUDGET_WARN_LOCK:
        last = _BUDGET_WARN_STATE.get(reason)
        if last is None or (now - last) >= cooldown:
            _BUDGET_WARN_STATE[reason] = now
            return True
        return False


def _invoke_models_rebuild(builder):
    """Indirection seam around the cold catalog rebuild.

    Production simply calls ``builder()``. Exists so tests can simulate a
    slow / hanging provider probe without having to reach the closure that
    actually does the per-provider network calls.
    """
    return builder()


def _configured_model_badges_from_static_catalog(
    groups: list[dict],
    *,
    active_provider: str | None,
    default_model: str,
) -> dict[str, dict[str, str]]:
    configured_entries: list[dict[str, str]] = []
    if active_provider and default_model:
        configured_entries.append(
            {
                "provider": active_provider,
                "model": default_model,
                "role": "primary",
                "label": "Primary",
            }
        )

    fallback_cfg = cfg.get("fallback_providers", []) if isinstance(cfg, dict) else []
    if isinstance(fallback_cfg, list):
        for idx, entry in enumerate(fallback_cfg, start=1):
            if not isinstance(entry, dict):
                continue
            provider = _resolve_provider_alias(entry.get("provider"))
            model = str(entry.get("model") or "").strip()
            if not provider or not model:
                continue
            configured_entries.append(
                {
                    "provider": provider,
                    "model": model,
                    "role": "fallback",
                    "label": f"Fallback {idx}",
                }
            )

    option_ids = [
        m.get("id", "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    ]
    option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
    option_provider_lookup = {
        str(m.get("id")): str(g.get("provider_id") or "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    }

    def _norm_static_model_id(model_id: str) -> str:
        s = str(model_id or "").strip().lower()
        stripped_at_provider = False
        if s.startswith("@") and ":" in s:
            colon_idx = s.index(":", 1)
            candidate = s[colon_idx + 1:]
            stripped_at_provider = bool(candidate)
            s = candidate or s
        if "://" not in s:
            if (
                not stripped_at_provider
                and "/" in s
                and ":" in s
                and s.index(":") < s.index("/")
            ):
                s = s[s.index("/") + 1 :] or s
            if "/" in s:
                stripped = s.split("/", 1)[1]
                s = stripped or s
        return s.replace("-", ".")

    norm_lookup: dict[str, list[str]] = {}
    for opt_id in option_ids:
        norm_lookup.setdefault(_norm_static_model_id(opt_id), []).append(opt_id)

    badges: dict[str, dict[str, str]] = {}
    for entry in configured_entries:
        provider = entry["provider"]
        model = entry["model"]
        raw_candidates = []
        for candidate in (model, f"{provider}/{model}", f"@{provider}:{model}"):
            if candidate and candidate not in raw_candidates:
                raw_candidates.append(candidate)

        match_id = None
        for candidate in raw_candidates:
            if (
                candidate in option_lookup
                and option_provider_lookup.get(candidate) == provider
            ):
                match_id = option_lookup[candidate]
                break
        if match_id is None:
            for candidate in raw_candidates:
                normalized = _norm_static_model_id(candidate)
                matches = norm_lookup.get(normalized, [])
                if not matches:
                    continue
                provider_match = next(
                    (m for m in matches if option_provider_lookup.get(m) == provider),
                    None,
                )
                match_id = provider_match or matches[0]
                if match_id:
                    break

        badge_payload = {
            "role": entry["role"],
            "label": entry["label"],
            "provider": provider,
        }
        for candidate in raw_candidates:
            candidate_provider = option_provider_lookup.get(candidate)
            if candidate_provider and candidate_provider != provider:
                continue
            badges[candidate] = badge_payload
        if match_id:
            badges[match_id] = badge_payload

    return badges


def _minimal_static_models_catalog() -> dict:
    """Return the emergency one-model fallback for /api/models."""
    try:
        active_provider = None
        cfg_base_url = ""
        model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _resolve_configured_provider_id(
                    active_provider, cfg, base_url=cfg_base_url
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None
        if not active_provider:
            try:
                _ap = _get_auth_store_path()
                if _ap.exists():
                    _store = json.loads(_ap.read_text(encoding="utf-8"))
                    active_provider = (
                        _resolve_configured_provider_id(
                            _store.get("active_provider"), cfg, base_url=cfg_base_url
                        )
                        or None
                    )
            except Exception:
                pass
        default_model = get_effective_default_model(cfg)
        groups: list[dict] = []
        if default_model:
            try:
                label = _get_label_for_model(default_model, [])
            except Exception:
                label = default_model
            groups.append(
                {
                    "provider": "Default",
                    "provider_id": active_provider or "default",
                    "models": [{"id": default_model, "label": label}],
                }
            )
        return _annotate_fast_tier_model_groups({
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": {},
            "groups": groups,
            "aliases": {},
        })
    except Exception:
        logger.debug("minimal static models catalog build failed", exc_info=True)
        return {
            "active_provider": None,
            "default_model": "",
            "configured_model_badges": {},
            "groups": [],
            "aliases": {},
        }


def _static_models_catalog_without_live_probes() -> dict:
    """Return a network-free /api/models catalog from local config/auth only."""
    try:
        from api.providers import _provider_has_key

        active_provider = None
        cfg_base_url = ""
        model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _resolve_configured_provider_id(
                    active_provider,
                    cfg,
                    base_url=cfg_base_url,
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None

        auth_store: dict = {}
        try:
            auth_store_path = _get_auth_store_path()
            if auth_store_path.exists():
                auth_store = json.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = (
                        _resolve_configured_provider_id(
                            auth_store.get("active_provider"),
                            cfg,
                            base_url=cfg_base_url,
                        )
                        or None
                    )
        except Exception:
            logger.debug("Failed to load auth store for static models catalog", exc_info=True)

        default_model = get_effective_default_model(cfg)
        detected_providers: set[str] = set()
        configured_model_ids: dict[str, list[str]] = {}
        named_custom_groups: dict[str, dict[str, object]] = {}
        custom_group_models: list[dict] = []
        canonical_to_raw_provider_key: dict[str, str] = {}
        providers_cfg = _get_providers_cfg()

        def _append_model_id(provider_id: str | None, model_id: object) -> None:
            pid = _canonicalise_provider_id(provider_id)
            mid = str(model_id or "").strip()
            if not pid or not mid:
                return
            configured_model_ids.setdefault(pid, [])
            if mid not in configured_model_ids[pid]:
                configured_model_ids[pid].append(mid)

        if active_provider:
            detected_providers.add(active_provider)
            _append_model_id(active_provider, default_model)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid, _entries in _pool.items():
                    if not isinstance(_entries, list) or not _entries:
                        continue
                    if any(
                        isinstance(_entry, dict)
                        and not _is_ambient_gh_cli_entry(
                            str(_entry.get("source", "") or ""),
                            str(_entry.get("label", "") or ""),
                            str(_entry.get("key_source", "") or ""),
                        )
                        for _entry in _entries
                    ):
                        detected_providers.add(_resolve_provider_alias(str(_pid)))
        except Exception:
            logger.debug("Failed to inspect auth-store credential pool", exc_info=True)

        if isinstance(providers_cfg, dict):
            for provider_key, provider_cfg in providers_cfg.items():
                canonical = _canonicalise_provider_id(provider_key)
                if not canonical:
                    continue
                is_known_provider = (
                    canonical in _PROVIDER_MODELS
                    or canonical in _PROVIDER_DISPLAY
                    or _is_plugin_model_provider(canonical)
                )
                is_provider_config = isinstance(provider_cfg, dict)
                if not (is_known_provider or is_provider_config):
                    continue
                canonical_to_raw_provider_key.setdefault(canonical, provider_key)
                if isinstance(provider_cfg, dict):
                    has_local_signal = any(
                        str(provider_cfg.get(key) or "").strip()
                        for key in ("api_key", "key_env", "base_url")
                    )
                    provider_models = provider_cfg.get("models")
                    for model_id in _configured_model_ids(provider_models):
                        _append_model_id(canonical, model_id)
                        has_local_signal = True
                    if has_local_signal:
                        detected_providers.add(canonical)

        for provider_id in set(_PROVIDER_MODELS) | set(_PROVIDER_DISPLAY):
            canonical = _canonicalise_provider_id(provider_id)
            if canonical and _provider_has_key(canonical):
                detected_providers.add(canonical)

        # Plugin-only providers (e.g. 9router) are not in the static
        # _PROVIDER_MODELS / _PROVIDER_DISPLAY tables and are detected above
        # only when the user puts them in `providers.<slug>`.  Plugins ship
        # with their own env-var wiring, so an installed-and-keyed plugin
        # provider should also enter the static catalog even without a
        # `providers:` block — otherwise the picker silently drops the
        # group when the live-rebuild cache is cold.
        try:
            for _plugin_pid in list(_plugin_model_provider_profiles().keys()):
                if not _plugin_pid or not _provider_has_key(_plugin_pid):
                    continue
                _canonical = _canonicalise_provider_id(_plugin_pid) or _plugin_pid
                if _canonical:
                    detected_providers.add(_canonical)
        except Exception:
            logger.debug("Plugin provider detection failed in static catalog", exc_info=True)

        fallback_cfg = cfg.get("fallback_providers", []) if isinstance(cfg, dict) else []
        if isinstance(fallback_cfg, list):
            for entry in fallback_cfg:
                if not isinstance(entry, dict):
                    continue
                provider = _resolve_provider_alias(entry.get("provider"))
                if provider:
                    detected_providers.add(provider)
                    _append_model_id(provider, entry.get("model"))

        for entry in _custom_provider_entries(cfg):
            provider_name = str(entry.get("name") or "").strip()
            provider_slug = _custom_provider_slug_from_name(provider_name) or "custom"
            if provider_slug != "custom":
                named_custom_groups.setdefault(
                    provider_slug,
                    {"name": provider_name, "models": []},
                )
            detected_providers.add(provider_slug)

            configured_ids: list[str] = []
            model_id = str(entry.get("model") or "").strip()
            if model_id:
                configured_ids.append(model_id)
            for configured_id in _configured_model_ids(entry.get("models")):
                if configured_id not in configured_ids:
                    configured_ids.append(configured_id)

            for configured_id in configured_ids:
                label = _get_label_for_model(configured_id, [])
                if provider_slug == "custom":
                    custom_group_models.append({"id": configured_id, "label": label})
                else:
                    named_custom_groups[provider_slug]["models"].append(
                        {"id": configured_id, "label": label}
                    )
                _append_model_id(provider_slug, configured_id)

        if cfg_base_url:
            detected_providers.add(
                _named_custom_provider_slug_for_base_url(cfg_base_url, cfg)
                or active_provider
                or "custom"
            )

        if detected_providers:
            detected_providers = {
                _canonicalise_provider_id(provider_id) or provider_id
                for provider_id in detected_providers
                if provider_id
            }

        groups: list[dict] = []
        for pid in sorted(detected_providers):
            if pid.startswith("custom:"):
                custom_group = named_custom_groups.get(pid, {})
                group_models = copy.deepcopy(custom_group.get("models", []))
                if group_models or pid == active_provider:
                    groups.append(
                        {
                            "provider": custom_group.get("name") or pid.replace("custom:", ""),
                            "provider_id": pid,
                            "models": _apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            if pid == "custom":
                group_models = copy.deepcopy(custom_group_models)
                for model_id in configured_model_ids.get(pid, []):
                    if not any(m.get("id") == model_id for m in group_models):
                        group_models.append(
                            {"id": model_id, "label": _get_label_for_model(model_id, [])}
                        )
                if group_models or cfg_base_url or pid == active_provider:
                    groups.append(
                        {
                            "provider": _PROVIDER_DISPLAY.get(pid, "Custom"),
                            "provider_id": pid,
                            "models": _apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            provider_name = _PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())
            raw_key = canonical_to_raw_provider_key.get(pid, pid)
            provider_cfg = _get_provider_cfg(raw_key)
            raw_models = []
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                raw_models = _configured_model_options(provider_cfg["models"])
            if not raw_models:
                raw_models = copy.deepcopy(_PROVIDER_MODELS.get(pid, []))
            # Plugin-only providers (e.g. 9router) are not in _PROVIDER_MODELS
            # and rarely ship a `models:` allowlist in providers.<slug>, so
            # the static catalog above would render them as empty groups that
            # the picker filters out. Fall back to the plugin's own
            # ProviderProfile.fallback_models so the provider surfaces a
            # curated, network-free subset on the cold path. The live
            # rebuild (_build_available_models_uncached) does a full
            # /v1/models fetch and supersedes this view on the next call.
            if not raw_models and _is_plugin_model_provider(pid):
                _plugin_profile = _plugin_model_provider_profiles().get(
                    (pid or "").strip().lower()
                )
                if _plugin_profile is not None:
                    _fallback = getattr(_plugin_profile, "fallback_models", ()) or ()
                    raw_models = [{"id": str(mid), "label": str(mid)} for mid in _fallback]
            for model_id in configured_model_ids.get(pid, []):
                if model_id and not any(m.get("id") == model_id for m in raw_models):
                    raw_models.append(
                        {"id": model_id, "label": _get_label_for_model(model_id, groups)}
                    )
            # Plugin-only providers (e.g. 9router) must enter `groups` even
            # when `raw_models` is empty so the post-loop filter sees them.
            # Without this, the earlier plugin-fallback pass only seeds
            # `raw_models` when `fallback_models` is non-empty; the cold-cache
            # picker still silently drops a keyed plugin with no models yet.
            if raw_models or _is_plugin_model_provider(pid):
                groups.append(
                    {
                        "provider": provider_name,
                        "provider_id": pid,
                        "models": _apply_provider_prefix(raw_models, pid, active_provider),
                    }
                )

        if default_model:
            all_model_ids = {
                str(model.get("id") or "")
                for group in groups
                for model in group.get("models", [])
            }
            if default_model not in all_model_ids and f"@{active_provider}:{default_model}" not in all_model_ids:
                label = _get_label_for_model(default_model, groups)
                target_group = next(
                    (group for group in groups if group.get("provider_id") == active_provider),
                    None,
                )
                if target_group is not None:
                    target_group.setdefault("models", []).insert(0, {"id": default_model, "label": label})
                elif groups:
                    groups.append(
                        {
                            "provider": "Default",
                            "provider_id": active_provider or "default",
                            "models": [{"id": default_model, "label": label}],
                        }
                    )

        _deduplicate_model_ids(groups)
        groups = [
            group
            for group in groups
            if group.get("models")
            or str(group.get("provider_id") or "").startswith("custom:")
            # Keep plugin-only providers visible even when no models surfaced
            # yet (e.g. plugin's fallback_models is empty and live rebuild
            # hasn't completed). Otherwise they silently drop from the
            # picker and look "not installed" — the same 9router-empty-group
            # regression this branch was added to fix.
            or _is_plugin_model_provider(str(group.get("provider_id") or ""))
        ]

        providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _canonical = _canonicalise_provider_id(_pid)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass
        try:
            for _pk, _pv in providers_cfg.items():
                if isinstance(_pv, dict) and (
                    _pv.get("api_key")
                    or _pv.get("key_env")
                    or _pv.get("base_url")
                ):
                    _canonical = _canonicalise_provider_id(_pk)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass

        def _group_sort_key(group: dict) -> tuple[int, str]:
            provider_id = str(group.get("provider_id") or "")
            if provider_id == active_provider:
                return (0, provider_id)
            if provider_id.startswith("custom:"):
                return (1, provider_id)
            if provider_id in providers_with_keys:
                return (2, provider_id)
            return (3, provider_id)

        groups.sort(key=_group_sort_key)

        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_aliases.items()
                    if k and v
                }
        except Exception:
            pass

        if not groups and default_model:
            return copy.deepcopy(_minimal_static_models_catalog())

        return _annotate_fast_tier_model_groups({
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _configured_model_badges_from_static_catalog(
                groups,
                active_provider=active_provider,
                default_model=default_model,
            ),
            "groups": groups,
            "aliases": model_aliases,
        })
    except Exception:
        logger.debug("static models catalog build failed", exc_info=True)
        return copy.deepcopy(_minimal_static_models_catalog())

# Cache for credential pool results -- calling load_pool() per-provider per-server
# session is expensive (~10s for zai due to endpoint probing).  The credential pool
# only changes when the user adds/removes credentials, which is rare; a 24h TTL
# is plenty safe and ensures get_available_models() cold paths are fast.
_CREDENTIAL_POOL_CACHE: dict[tuple[str, str], tuple[float, "CredentialPool"]] = {}  # noqa: F821  forward-ref string annotation, resolved at runtime  # (profile_tag, pid) -> (ts, pool)


def _credential_pool_profile_tag() -> str:
    """Active-profile identity for the credential-pool cache key.

    The credential pool is per-Hermes-profile (it lives in that profile's
    auth.json). Keying the process-global cache by provider id ALONE lets a
    pool loaded under profile A satisfy a lookup under profile B in the same
    server process — so a custom provider configured only in A would falsely
    report configured in B (and then 401 at request time). Scoping every
    cache key by the active profile's auth-store path keeps pools from
    crossing profile boundaries.
    """
    try:
        return str(_get_auth_store_path())
    except Exception:
        return ""


def _pool_entry_payloads(provider_id: str) -> list[dict[str, Any]]:
    """Return explicit credential-pool entry payloads for the active profile.

    Readonly profile scopes must not let ``load_pool()`` seed from process env,
    because that can materialize server-default credentials into a named
    profile's auth store. In that mode, read raw auth.json payloads only.
    """
    _pid = _resolve_provider_alias(provider_id)
    if bool(getattr(_thread_ctx, "block_process_env_fallback", False)):
        try:
            from hermes_cli.auth import read_credential_pool as _read_credential_pool

            raw_entries = _read_credential_pool(_pid)
        except ImportError:
            return []
        payloads: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if _is_ambient_gh_cli_entry(
                str(entry.get("source", "") or ""),
                str(entry.get("label", "") or ""),
                str(entry.get("key_source", "") or ""),
            ):
                continue
            payloads.append(dict(entry))
        return payloads

    try:
        from agent.credential_pool import load_pool as _load_pool

        _ck = (_credential_pool_profile_tag(), _pid)
        _cached = _CREDENTIAL_POOL_CACHE.get(_ck)
        if _cached is not None:
            _cp_ts, _cp_pool = _cached
            if (time.time() - _cp_ts) < 86400.0:
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
            else:
                _cp_pool = _load_pool(_pid)
                _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
        else:
            _cp_pool = _load_pool(_pid)
            _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
            _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
    except ImportError:
        return []

    payloads = []
    for entry in _all_entries:
        if _is_ambient_gh_cli_entry(
            str(getattr(entry, "source", "") or ""),
            str(getattr(entry, "label", "") or ""),
            str(getattr(entry, "key_source", "") or ""),
        ):
            continue
        if hasattr(entry, "to_dict") and callable(entry.to_dict):
            payload = entry.to_dict()
        elif isinstance(entry, dict):
            payload = dict(entry)
        else:
            try:
                payload = dict(vars(entry))
            except TypeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload.setdefault("source", str(getattr(entry, "source", "") or ""))
        payload.setdefault("label", str(getattr(entry, "label", "") or ""))
        payload.setdefault("key_source", str(getattr(entry, "key_source", "") or ""))
        runtime_api_key = getattr(entry, "runtime_api_key", None)
        if runtime_api_key:
            payload["runtime_api_key"] = runtime_api_key
        base_url = getattr(entry, "base_url", None)
        if base_url:
            payload["base_url"] = base_url
        inference_base_url = getattr(entry, "inference_base_url", None)
        if inference_base_url:
            payload["inference_base_url"] = inference_base_url
        payloads.append(payload)
    return payloads


def _has_explicit_pool_credentials(provider_id: str) -> bool:
    """Return True when the credential pool has at least one non-ambient entry
    for *provider_id* (i.e. not a gh-cli / GITHUB_TOKEN auto-detect).

    Reuses ``_CREDENTIAL_POOL_CACHE`` so that callers on hot paths (provider
    detection, model listing, live-model fetch) don't pay the ~10s load_pool
    cost more than once per TTL window.
    """
    return bool(_pool_entry_payloads(provider_id))
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
def _current_webui_version() -> str | None:
    """Lazy resolver for the WebUI version, used to stamp the disk cache (#1633).

    `api.updates` imports `api.config` at module-load time, so we cannot
    `from api.updates import WEBUI_VERSION` at the top of this module without a
    circular import. Instead we resolve lazily on each cache load/save.

    Returns the runtime version string (e.g. ``v0.50.293``) when api.updates
    has been imported, or None if it isn't loaded yet (boot-time corner case
    before the server has finished initializing). A None return is treated as
    "do not stamp / do not validate" by the cache layer so cache reads/writes
    that happen during early init still work — the next call after init will
    stamp normally.
    """
    try:
        # Read attribute via dotted lookup so we don't add an import-time edge.
        import sys as _sys
        mod = _sys.modules.get('api.updates')
        if mod is None:
            return None
        v = getattr(mod, 'WEBUI_VERSION', None)
        return str(v) if v else None
    except Exception:
        return None


# Disk-cache schema version (#1633).
#
# Bumped any time the disk cache shape changes in a backward-incompatible way
# (e.g. new required field, renamed key). Independent of the WebUI version
# stamp — _webui_version forces a rebuild on every release; _schema_version
# guarantees that even if a future release accidentally reuses the same
# WebUI version string (or a debug build doesn't have a version), a structural
# change still invalidates the cache.
_MODELS_CACHE_SCHEMA_VERSION = 3


_models_cache_path = STATE_DIR / "models_cache.json"


from api.config_parts import models_cache as _models_cache_impl

_get_models_cache_path = _models_cache_impl._get_models_cache_path
_get_auth_store_path = _models_cache_impl._get_auth_store_path
_models_cache_file_fingerprint = _models_cache_impl._models_cache_file_fingerprint
_models_cache_catalog_fingerprint = _models_cache_impl._models_cache_catalog_fingerprint
_AUTH_FINGERPRINT_VOLATILE_KEYS = _models_cache_impl._AUTH_FINGERPRINT_VOLATILE_KEYS
_strip_volatile_auth_fields = _models_cache_impl._strip_volatile_auth_fields
_auth_store_semantic_fingerprint = _models_cache_impl._auth_store_semantic_fingerprint
_models_cache_source_fingerprint = _models_cache_impl._models_cache_source_fingerprint
_delete_models_cache_on_disk = _models_cache_impl._delete_models_cache_on_disk
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


def _get_label_for_model(model_id: str, existing_groups: list) -> str:
    """Return a human-friendly label for *model_id*.

    Resolution order:
    1. If the model already appears in *existing_groups* with a label, use it.
    2. Strip @provider: prefix and namespace prefix, then title-case.

    This ensures the injected default model entry in the dropdown always shows
    the same label as the live-fetched or static-catalog version, rather than
    the raw lowercase ID string (#909).
    """
    # Strip @provider: prefix for lookup
    lookup_id = model_id
    if lookup_id.startswith("@") and ":" in lookup_id:
        lookup_id = lookup_id.split(":", 1)[1]

    # Check existing groups for a matching label.
    # Skip slash stripping for URI-scheme IDs (e.g. gpt://folder/model) (#3429).
    _has_scheme = lambda s: "://" in s
    _norm = lambda s: (s.split("/", 1)[-1] if ("/" in s and not _has_scheme(s)) else s).replace("-", ".").lower()
    norm_lookup = _norm(lookup_id)
    for g in existing_groups:
        for m in g.get("models", []):
            if m.get("label") and _norm(str(m.get("id", ""))) == norm_lookup:
                return m["label"]

    # Fall back: strip only the first slash-segment (provider prefix),
    # preserving vendor hierarchy for multi-slash IDs (#3360).
    # Skip for URI-scheme IDs whose slashes are path separators (#3429).
    bare = lookup_id.split("/", 1)[1] if ("/" in lookup_id and not _has_scheme(lookup_id)) else lookup_id
    return " ".join(
        w.upper() if (len(w) <= 3 and w.replace(".", "").isalnum() and not w.isdigit()) else w.capitalize()
        for w in bare.replace("_", "-").split("-")
    )


def _read_live_provider_model_ids(provider_id: str) -> list[str]:
    """Return live model IDs from Hermes CLI for a provider, or [] on failure.

    WebUI's static ``_PROVIDER_MODELS`` table is only a fallback.  The agent CLI
    owns the provider registry and catalog-discovery logic, so ordinary picker
    groups should ask ``hermes_cli.models.provider_model_ids()`` first (#1240).
    Provider aliases are tried as a secondary lookup because WebUI keeps a few
    display-facing IDs (for example ``google`` / ``x-ai``) that Hermes CLI may
    normalize internally.
    """
    pid = str(provider_id or "").strip()
    if not pid:
        return []
    try:
        from hermes_cli.models import provider_model_ids as _provider_model_ids
    except Exception:
        return []

    candidates = [pid]
    try:
        alias = _resolve_provider_alias(pid)
    except Exception:
        alias = ""
    if alias and alias not in candidates:
        candidates.append(alias)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            live_ids = _provider_model_ids(candidate) or []
        except Exception:
            logger.debug("Failed to load %s models from hermes_cli", candidate)
            continue
        result: list[str] = []
        for mid in live_ids:
            mid_s = str(mid or "").strip()
            if mid_s and mid_s not in seen:
                seen.add(mid_s)
                result.append(mid_s)
        if result:
            return result
    return []


def _models_from_live_provider_ids(provider_id: str, live_ids: list[str]) -> list[dict]:
    """Convert Hermes CLI model ids into WebUI picker model entries."""
    formatter = _format_ollama_label if provider_id in ("ollama", "ollama-cloud") else None
    models: list[dict] = []
    seen: set[str] = set()
    for mid in live_ids:
        mid_s = str(mid or "").strip()
        if not mid_s or mid_s in seen:
            continue
        seen.add(mid_s)
        label = formatter(mid_s) if formatter else _get_label_for_model(mid_s, [])
        models.append({"id": mid_s, "label": label})
    return models


def _moa_preset_models_from_config(config_obj: dict | None = None) -> list[dict]:
    """Return enabled MoA presets from local config as picker model entries."""
    source = config_obj if isinstance(config_obj, dict) else cfg
    moa_cfg = source.get("moa") if isinstance(source, dict) else None
    if not isinstance(moa_cfg, dict) or not bool(moa_cfg.get("enabled", True)):
        return []
    presets = moa_cfg.get("presets")
    if not isinstance(presets, dict):
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for name, preset_cfg in presets.items():
        preset_name = str(name or "").strip()
        if not preset_name or preset_name in seen:
            continue
        if isinstance(preset_cfg, dict) and preset_cfg.get("enabled") is False:
            continue
        seen.add(preset_name)
        models.append({"id": preset_name, "label": preset_name})
    return models


def _read_visible_codex_cache_model_ids() -> list[str]:
    """Return visible model slugs from Codex's local models_cache.json.

    The agent's provider_model_ids('openai-codex') intentionally filters IDs
    with ``supported_in_api: false``. Codex CLI still lists some of those models
    in its picker (notably ``gpt-5.3-codex-spark`` from #1680), so the WebUI
    merges this visible local catalog to stay in sync with Codex itself.
    """
    codex_home = Path(os.getenv("CODEX_HOME", "").strip() or (HOME / ".codex")).expanduser()
    cache_path = codex_home / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    sortable: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in ("hide", "hidden"):
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))

    sortable.sort(key=lambda item: (item[0], item[1]))
    ordered: list[str] = []
    for _, slug in sortable:
        if slug not in ordered:
            ordered.append(slug)
    return ordered


def get_available_models(*, prefer_cache: bool = False, force_refresh: bool = False) -> dict:
    """
    Return available models grouped by provider.

    Discovery order:
      1. Read config.yaml 'model' section for active provider info
      2. Check for known API keys in env or ~/.hermes/.env
      3. Fetch models from custom endpoint if base_url is configured
      4. Fall back to hardcoded model list (OpenRouter-style)

    Returns: {
        'active_provider': str|None,
        'default_model': str,
        'groups': [{'provider': str, 'models': [{'id': str, 'label': str}]}]
    }

    ``prefer_cache=True`` resolves WITHOUT ever triggering a live provider
    probe: it serves the warm in-memory cache, then the last-known on-disk
    cache, and only as a last resort a network-free minimal catalog
    (config/auth derived). It NEVER does the per-provider live rebuild (the
    Copilot token-exchange HTTPS call et al.). This is the path a
    server-initiated wakeup turn (Option Z) takes so a cold catalog can never
    block the wakeup chat/start on a flaky network. A normal human request
    leaves this False and keeps the full live-discovery behaviour.

    ``force_refresh=True`` is an internal escape hatch for bounded freshness
    checks that need a real live rebuild while preserving the default cache
    contract for every existing caller.
    """
    global _cache_build_in_progress, _available_models_cache, _available_models_cache_ts
    global _available_models_live_rebuild_ts, _available_models_cache_source_fingerprint, _cache_build_cv
    global _models_cache_build_generation, _active_models_cache_build_generation
    # Config mtime check — must come before any config reads.
    # (Test #585 verifies _current_mtime appears before active_provider = None)
    try:
        _current_path = _get_config_path()
        _current_mtime = _current_path.stat().st_mtime
    except OSError:
        _current_path = _get_config_path()
        _current_mtime = 0.0
    path_changed = _current_path != _cfg_path
    mtime_stale = _current_mtime != _cfg_mtime
    if path_changed or (mtime_stale and not _cfg_has_in_memory_overrides()):
        reload_config_if_stale()
    # ── COLD PATH helper ─────────────────────────────────────────────────────
    # Extracted so it runs inside _available_models_cache_lock (RLock) to
    # prevent thundering-herd: only one thread rebuilds while others wait.
    def _build_available_models_uncached() -> dict:
        active_provider = None
        default_model = get_effective_default_model(cfg)
        groups = []

        def _norm_model_id(model_id: str) -> str:
            s = str(model_id or "").strip().lower()
            stripped_at_provider = False
            # Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> GLM-5).
            # Defensive: if the last segment is empty (trailing colon, malformed
            # config), keep the original to avoid collapsing distinct IDs to ''.
            if s.startswith("@") and ":" in s:
                # Strip @provider: prefix, preserving remaining hierarchy including
                # colon-suffixed model IDs like provider/model:free (#3959).
                colon_idx = s.index(":", 1)
                candidate = s[colon_idx + 1:]
                stripped_at_provider = bool(candidate)
                s = candidate or s
            # Skip slash-based stripping for URI-scheme IDs (e.g.
            # gpt://folder/model/latest) whose slashes are path separators,
            # not provider delimiters (#3429).
            if "://" not in s:
                if (
                    not stripped_at_provider
                    and "/" in s
                    and ":" in s
                    and s.index(":") < s.index("/")
                ):
                    s = s[s.index("/") + 1 :] or s
                # Strip only the first slash-segment (provider prefix), preserving
                # any remaining vendor hierarchy.  Using parts[-1] here previously
                # discarded ALL segments except the last, collapsing distinct
                # multi-slash IDs like 'vendor_a/deepseek-v4-pro' and
                # 'vendor_b/deepseek/deepseek-v4-pro' to the same key (#3360).
                if "/" in s:
                    stripped = s.split("/", 1)[1]
                    s = stripped or s
            return s.replace("-", ".")

        def _build_configured_model_badges() -> dict[str, dict[str, str]]:
            configured_entries: list[dict[str, str]] = []
            if active_provider and default_model:
                configured_entries.append(
                    {
                        "provider": active_provider,
                        "model": default_model,
                        "role": "primary",
                        "label": "Primary",
                    }
                )
            fallback_cfg = cfg.get("fallback_providers", [])
            if isinstance(fallback_cfg, list):
                for idx, entry in enumerate(fallback_cfg, start=1):
                    if not isinstance(entry, dict):
                        continue
                    provider = _resolve_provider_alias(entry.get("provider"))
                    model = str(entry.get("model") or "").strip()
                    if not provider or not model:
                        continue
                    configured_entries.append(
                        {
                            "provider": provider,
                            "model": model,
                            "role": "fallback",
                            "label": f"Fallback {idx}",
                        }
                    )

            option_ids = [m.get("id", "") for g in groups for m in g.get("models", []) if m.get("id")]
            option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
            option_provider_lookup = {
                str(m.get("id")): str(g.get("provider_id") or "")
                for g in groups
                for m in g.get("models", [])
                if m.get("id")
            }
            norm_lookup: dict[str, list[str]] = {}
            for opt_id in option_ids:
                norm_lookup.setdefault(_norm_model_id(opt_id), []).append(opt_id)

            badges: dict[str, dict[str, str]] = {}
            for entry in configured_entries:
                provider = entry["provider"]
                model = entry["model"]
                raw_candidates = []
                for candidate in (
                    model,
                    f"{provider}/{model}",
                    f"@{provider}:{model}",
                ):
                    if candidate and candidate not in raw_candidates:
                        raw_candidates.append(candidate)

                match_id = None
                exact_match = next((option_lookup[c] for c in raw_candidates if c in option_lookup), None)
                for candidate in raw_candidates:
                    if candidate in option_lookup and option_provider_lookup.get(candidate) == provider:
                        match_id = option_lookup[candidate]
                        break
                if match_id is None:
                    for candidate in raw_candidates:
                        normalized = _norm_model_id(candidate)
                        matches = norm_lookup.get(normalized, [])
                        if not matches:
                            continue
                        provider_match = next(
                            (m for m in matches if option_provider_lookup.get(m) == provider),
                            None,
                        )
                        match_id = provider_match or exact_match or matches[0]
                        if match_id:
                            break

                badge_payload = {"role": entry["role"], "label": entry["label"], "provider": provider}
                for candidate in raw_candidates:
                    candidate_provider = option_provider_lookup.get(candidate)
                    if candidate_provider and candidate_provider != provider:
                        continue
                    badges[candidate] = badge_payload
                if match_id:
                    badges[match_id] = badge_payload
            return badges

        # 1. Read config.yaml model section
        cfg_base_url = ""  # must be defined before conditional blocks (#117)
        model_cfg = cfg.get("model", {})
        cfg_base_url = ""
        if isinstance(model_cfg, str):
            pass  # default_model already set by get_effective_default_model
        elif isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_default = model_cfg.get("default", "")
            cfg_base_url = model_cfg.get("base_url", "")
            if cfg_default:
                default_model = cfg_default

        # Normalize active_provider to its canonical key.  Named custom
        # providers are first-class provider ids in WebUI routing; accept the
        # user-facing name from config.yaml (``provider: ollama-local``) and
        # route it through the same ``custom:<name>`` slug the picker emits.
        if active_provider:
            active_provider = _resolve_configured_provider_id(
                active_provider,
                cfg,
                base_url=cfg_base_url,
            )

        # 2. Read auth store (active_provider fallback + credential_pool inspection)
        auth_store = {}
        auth_store_path = _get_auth_store_path()
        if auth_store_path.exists():
            try:
                import json as _j

                auth_store = _j.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = _resolve_configured_provider_id(
                        auth_store.get("active_provider"),
                        cfg,
                        base_url=cfg_base_url,
                    )
            except Exception:
                logger.debug("Failed to load auth store from %s", auth_store_path)

        # 3. Detect available providers.
        detected_providers = set()
        if active_provider:
            detected_providers.add(active_provider)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict) and _pool:
                try:
                    from agent.credential_pool import load_pool as _load_pool

                    for _pid in list(_pool.keys()):
                        try:
                            _canonical_pid = _resolve_provider_alias(str(_pid))
                            # Check credential pool cache first (profile-scoped key
                            # so a pool loaded under another profile can't leak in).
                            _ck = (_credential_pool_profile_tag(), _pid)
                            _cached = _CREDENTIAL_POOL_CACHE.get(_ck)
                            if _cached is not None:
                                _cp_ts, _cp_pool = _cached
                                if (time.time() - _cp_ts) < 86400.0:
                                    _all_entries = _cp_pool.entries()
                                else:
                                    _lp_t0 = time.monotonic()
                                    _cp_pool = _load_pool(_pid)
                                    _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                                    _all_entries = _cp_pool.entries()
                            else:
                                _lp_t0 = time.monotonic()
                                _cp_pool = _load_pool(_pid)
                                _CREDENTIAL_POOL_CACHE[_ck] = (time.time(), _cp_pool)
                                _all_entries = _cp_pool.entries()
                            _explicit = [
                                e for e in _all_entries
                                if not _is_ambient_gh_cli_entry(
                                    str(getattr(e, "source", "") or ""),
                                    str(getattr(e, "label", "") or ""),
                                    str(getattr(e, "key_source", "") or ""),
                                )
                            ]
                            if _explicit and _is_known_model_provider(_canonical_pid):
                                detected_providers.add(_canonical_pid)
                        except Exception:
                            logger.debug("credential_pool.load_pool(%s) failed", _pid)
                except ImportError:
                    for _pid, _entries in _pool.items():
                        if not isinstance(_entries, list) or len(_entries) == 0:
                            continue
                        _has_explicit_cred = any(
                            isinstance(_entry, dict)
                            and not _is_ambient_gh_cli_entry(
                                str(_entry.get("source", "") or ""),
                                str(_entry.get("label", "") or ""),
                                str(_entry.get("key_source", "") or ""),
                            )
                            for _entry in _entries
                        )
                        if _has_explicit_cred:
                            _canonical_pid = _resolve_provider_alias(str(_pid))
                            if _is_known_model_provider(_canonical_pid):
                                detected_providers.add(_canonical_pid)
        except Exception:
            logger.debug("Failed to inspect credential_pool from auth store")

        all_env: dict = {}

        _hermes_auth_used = False
        try:
            from hermes_cli.models import list_available_providers as _lap
            from hermes_cli.auth import get_auth_status as _gas

            for _p in _lap():
                if not _p.get("authenticated"):
                    continue
                try:
                    _src = _gas(_p["id"]).get("key_source", "")
                    if _src == "gh auth token":
                        continue
                except Exception:
                    logger.debug("Failed to get key source for provider %s", _p.get("id", "unknown"))
                detected_providers.add(_p["id"])
            _hermes_auth_used = True

            # Belt-and-braces: list_available_providers() is the primary signal
            # for OAuth providers, but its `authenticated` field can disagree
            # with `get_auth_status(<id>).logged_in` on some hermes_cli versions
            # (the two fields are computed via different code paths). When the
            # disagreement happens for Nous Portal, the Settings → Providers
            # card renders the live catalog (because api/providers.py iterates
            # all OAuth providers regardless of authentication state) but the
            # picker dropdown comes up empty — a confusing asymmetry reported
            # in #1567. Add Nous explicitly when get_auth_status agrees so the
            # picker stays in sync with the providers card.
            try:
                if _gas("nous").get("logged_in"):
                    detected_providers.add("nous")
            except Exception:
                logger.debug("Failed to check Nous Portal auth status")
        except Exception:
            logger.debug("Failed to detect auth providers from hermes")

        if not _hermes_auth_used:
            try:
                from api.profiles import get_active_hermes_home as _gah2

                hermes_env_path = _gah2() / ".env"
            except ImportError:
                hermes_env_path = _DEFAULT_HERMES_HOME / ".env"
            env_keys = {}
            if hermes_env_path.exists():
                try:
                    for line in hermes_env_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env_keys[k.strip()] = v.strip().strip('"').strip("'")
                except Exception:
                    logger.debug("Failed to parse hermes env file")
            all_env = {**env_keys}
            _anthropic_env_vars = _get_anthropic_fallback_env_vars()
            for k in (
                *_anthropic_env_vars,
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "GLM_API_KEY",
                "KIMI_API_KEY",
                "DEEPSEEK_API_KEY",
                "XIAOMI_API_KEY",
                "OPENCODE_ZEN_API_KEY",
                "OPENCODE_GO_API_KEY",
                "OPENCODE_API_KEY",
                "MINIMAX_API_KEY",
                "MINIMAX_CN_API_KEY",
                "XAI_API_KEY",
                "MISTRAL_API_KEY",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
            ):
                val = _thread_local_env_value(k).strip()
                if val:
                    all_env[k] = val
            if any(all_env.get(env_var) for env_var in _anthropic_env_vars):
                detected_providers.add("anthropic")
            if all_env.get("OPENAI_API_KEY"):
                # hermes-agent registers its OPENAI_API_KEY/OPENAI_BASE_URL provider
                # under the slug `openai-api` (there is no bare `openai` in the agent
                # registry — only `openai-api` and `openai-codex`). Detecting `openai`
                # here would emit `@openai:` picker entries the agent can't resolve on
                # the send path, so detect `openai-api` to match the registry (#3443).
                detected_providers.add("openai-api")
                # openai-codex uses ChatGPT OAuth (not OPENAI_API_KEY) for its default endpoint.
                # Detecting it here lets users who have both credentials configured find it in the
                # picker without a manual config.yaml edit. Users without Codex OAuth will see
                # picker entries but hit auth errors at inference time (#1189 known limitation).
                detected_providers.add("openai-codex")
            if all_env.get("OPENROUTER_API_KEY"):
                detected_providers.add("openrouter")
            if all_env.get("GOOGLE_API_KEY"):
                detected_providers.add("google")
            if all_env.get("GEMINI_API_KEY"):
                detected_providers.add("gemini")
            if all_env.get("GLM_API_KEY"):
                detected_providers.add("zai")
            if all_env.get("KIMI_API_KEY"):
                detected_providers.add("kimi-coding")
            if all_env.get("MINIMAX_API_KEY"):
                detected_providers.add("minimax")
            if all_env.get("MINIMAX_CN_API_KEY"):
                detected_providers.add("minimax-cn")
            if all_env.get("DEEPSEEK_API_KEY"):
                detected_providers.add("deepseek")
            if all_env.get("XIAOMI_API_KEY"):
                detected_providers.add("xiaomi")
            if all_env.get("XAI_API_KEY"):
                detected_providers.add("x-ai")
            if all_env.get("MISTRAL_API_KEY"):
                detected_providers.add("mistralai")
            if all_env.get("OPENCODE_ZEN_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-zen")
            if all_env.get("OPENCODE_GO_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-go")
            # AWS Bedrock uses IAM credentials rather than a single API key.
            # Detect when both access key and secret are available (#2720).
            if all_env.get("AWS_ACCESS_KEY_ID") and all_env.get("AWS_SECRET_ACCESS_KEY"):
                detected_providers.add("bedrock")
            # LM Studio: detect via LM_API_KEY + LM_BASE_URL in ~/.hermes/.env
            if all_env.get("LM_API_KEY") and all_env.get("LM_BASE_URL"):
                detected_providers.add("lmstudio")

        # Also detect providers explicitly listed in config.yaml providers section.
        # A user may configure a provider key via config.yaml providers.<name>.api_key
        # without setting the corresponding env var. (#604)
        #
        # Gating: only seed picker groups for keys whose canonical id is known
        # to ``_PROVIDER_MODELS`` / ``_PROVIDER_DISPLAY``, or whose value is a
        # dict-shaped provider config (custom/local). Scalar siblings under
        # ``providers:`` (e.g. ``providers.only_configured: true``) are config
        # flags, not providers, and must not render as phantom picker groups
        # like ``Only-Configured`` (#2399).
        #
        # Canonicalise the id slug here so a user with ``providers.opencode_go``
        # (underscore variant) doesn't see TWO provider groups in the picker —
        # one for the canonical ``opencode-go`` from active_provider detection
        # and a phantom ``Opencode_Go`` group for the config-key form (#1568).
        # The same applies to mixed-case ids like ``OpenCode-Go`` and to
        # legitimate aliases like ``z-ai`` → ``zai``.
        _cfg_providers = _get_providers_cfg()
        # Map canonical provider IDs back to raw config keys so the
        # generic-provider branch can preserve mixed-case/underscore
        # provider_cfg values (#2245).
        _canonical_to_raw_provider_key: dict[str, str] = {}
        if isinstance(_cfg_providers, dict):
            for _pid_key, _provider_cfg in _cfg_providers.items():
                _canonical = _canonicalise_provider_id(_pid_key)
                if not _canonical:
                    continue

                # See the gating comment on the block above. ``_PROVIDER_MODELS``
                # / ``_PROVIDER_DISPLAY`` membership accepts known providers and
                # aliases; ``isinstance(_provider_cfg, dict)`` accepts custom
                # entries that supply their own models/api_key/base_url. (#2399)
                _is_known_provider = (
                    _canonical in _PROVIDER_MODELS
                    or _canonical in _PROVIDER_DISPLAY
                    or _is_plugin_model_provider(_canonical)
                )
                _is_provider_config = isinstance(_provider_cfg, dict)
                _has_provider_route = False
                if _is_provider_config:
                    _has_provider_route = any(
                        str(_provider_cfg.get(_route_key) or "").strip()
                        for _route_key in ("api", "base_url", "api_key", "key_env")
                    )
                # A models-only provider config (no api/base_url/api_key/key_env)
                # is only admitted as evidence when it's the active/configured
                # provider (e.g. the lmstudio-style custom shape from #1970).
                # This must NOT re-open the door for a spurious duplicate alias
                # of a known provider (e.g. ``copilot-2: {name: "copilot",
                # models: {...}}`` from #644/dedup regression) — that case is
                # still rejected because it isn't the active provider and it
                # isn't a route-bearing config in its own right.
                _has_models_only_active_route = (
                    not _has_provider_route
                    and _is_provider_config
                    and isinstance(_provider_cfg.get("models"), (dict, list))
                    and _provider_cfg["models"]
                    and _canonical == _canonicalise_provider_id(active_provider)
                )
                if not (_is_known_provider or _has_provider_route or _has_models_only_active_route):
                    continue

                _canonical_to_raw_provider_key.setdefault(_canonical, _pid_key)
                detected_providers.add(_canonical)

        def _configured_provider_for_base_url(base_url: object) -> str:
            target = _normalize_base_url_for_match(base_url)
            if not target:
                return ""

            if isinstance(model_cfg, dict):
                model_base_url = _normalize_base_url_for_match(model_cfg.get("base_url"))
                if model_base_url == target:
                    provider_hint = _resolve_configured_provider_id(
                        model_cfg.get("provider"),
                        cfg,
                        base_url=base_url,
                    )
                    if provider_hint:
                        return str(provider_hint).strip().lower()

            providers_cfg = cfg.get("providers", {})
            if isinstance(providers_cfg, dict):
                for provider_key, provider_cfg in providers_cfg.items():
                    if not isinstance(provider_cfg, dict):
                        continue
                    provider_base_url = _normalize_base_url_for_match(
                        provider_cfg.get("base_url")
                    )
                    if provider_base_url == target:
                        provider_hint = _resolve_provider_alias(provider_key)
                        if provider_hint:
                            return str(provider_hint).strip().lower()

            custom_providers_cfg = cfg.get("custom_providers", [])
            if isinstance(custom_providers_cfg, list):
                for entry in custom_providers_cfg:
                    if not isinstance(entry, dict):
                        continue
                    entry_base_url = _normalize_base_url_for_match(entry.get("base_url"))
                    if entry_base_url != target:
                        continue
                    entry_name = str(entry.get("name") or "").strip()
                    if entry_name:
                        return _custom_provider_slug_from_name(entry_name)
                    return "custom"

            return ""

        def _models_endpoint_for_base_url(base_url: str) -> str:
            base = str(base_url or "").strip().rstrip("/")
            if base.endswith("/v1"):
                return base + "/models"
            return base + "/v1/models"

        def _extract_model_entries_from_payload(data: object, provider: str) -> list[dict]:
            models_list = []
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    models_list = data["data"]
                elif "models" in data and isinstance(data["models"], list):
                    models_list = data["models"]
            models = []
            seen = set()
            for model in models_list:
                if not isinstance(model, dict):
                    continue
                model_id = (
                    model.get("id", "")
                    or model.get("name", "")
                    or model.get("model", "")
                )
                model_name = model.get("name", "") or model.get("model", "") or model_id
                model_id = str(model_id or "").strip()
                model_name = str(model_name or "").strip()
                if not model_id or not model_name or model_id in seen:
                    continue
                seen.add(model_id)
                label = _format_ollama_label(model_id) if provider in ("ollama", "ollama-cloud") else model_name
                models.append({"id": model_id, "label": label})
            return models

        def _custom_endpoint_error(
            provider: str,
            exc: Exception,
            *,
            code: int | None = None,
        ) -> dict:
            provider_label = str(provider or "custom").replace("custom:", "")
            status_code = code if code is not None else getattr(exc, "code", None)
            if status_code in (401, 403):
                return {
                    "kind": "auth",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} — check the API key for {provider_label}.",
                }
            if isinstance(status_code, int):
                return {
                    "kind": "http",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} for {provider_label}; see logs.",
                }
            return {
                "kind": "network",
                "code": None,
                "message": f"Models endpoint unreachable for {provider_label}; verify base_url.",
            }

        def _read_custom_endpoint_models(
            base_url: object,
            provider: str,
            *,
            api_key: object = "",
            trusted_base_urls: tuple[object, ...] = (),
        ) -> tuple[list[dict], dict | None]:
            base = str(base_url or "").strip()
            if not base:
                return [], None
            try:
                import ipaddress
                import urllib.error
                import urllib.request
                import socket

                endpoint_url = _models_endpoint_for_base_url(base)
                headers = {}
                key = str(api_key or "").strip()
                if key:
                    headers["Authorization"] = f"Bearer {key}"

                # User-configured custom provider endpoints are explicitly trusted,
                # but keep the same private-IP guard for non-matching targets used by
                # the legacy active model.base_url path.
                _ssrf_trusted_hosts: set[str] = set()
                for trusted in (base, *trusted_base_urls):
                    _cp_parsed = urlparse(
                        str(trusted) if "://" in str(trusted) else f"http://{trusted}"
                    )
                    if _cp_parsed.hostname:
                        _ssrf_trusted_hosts.add(_cp_parsed.hostname.lower())

                parsed_url = urlparse(endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}")
                if parsed_url.scheme not in ("", "http", "https"):
                    raise ValueError(f"Invalid URL scheme: {parsed_url.scheme}")
                if parsed_url.hostname:
                    try:
                        resolved_ips = socket.getaddrinfo(parsed_url.hostname, None)
                        for _, _, _, _, addr in resolved_ips:
                            addr_obj = ipaddress.ip_address(addr[0])
                            if addr_obj.is_private or addr_obj.is_loopback or addr_obj.is_link_local:
                                host_l = (parsed_url.hostname or "").lower()
                                is_known_local = any(
                                    k in host_l
                                    for k in ("ollama", "localhost", "127.0.0.1", "lmstudio", "lm-studio")
                                ) or host_l in _ssrf_trusted_hosts
                                if not is_known_local:
                                    raise ValueError(f"SSRF: resolved hostname to private IP {addr[0]}")
                    except socket.gaierror:
                        pass

                req = urllib.request.Request(endpoint_url, method="GET")
                req.add_header("User-Agent", "OpenAI/Python 1.0")
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS) as response:  # nosec B310
                    data = json.loads(response.read().decode("utf-8"))
                return _extract_model_entries_from_payload(data, provider), None
            except urllib.error.HTTPError as exc:
                error = _custom_endpoint_error(provider, exc, code=getattr(exc, "code", None))
                logger.debug("Custom endpoint models fetch failed for provider %s: %s", provider, error)
                return [], error
            except Exception as exc:
                error = _custom_endpoint_error(provider, exc)
                logger.debug("Custom endpoint unreachable or misconfigured for provider %s: %s", provider, error)
                return [], error

        # 4. Fetch models from custom endpoint if base_url is configured
        auto_detected_models = []
        auto_detected_models_by_provider: dict[str, list[dict]] = {}
        if cfg_base_url:
            base_url = cfg_base_url.strip()
            configured_provider = _configured_provider_for_base_url(base_url)
            provider = configured_provider or "custom"
            provider_from_config = bool(configured_provider)
            parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
            host = (parsed.netloc or parsed.path).lower()

            if parsed.hostname and not provider_from_config:
                try:
                    import ipaddress

                    addr = ipaddress.ip_address(parsed.hostname)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        if "ollama" in host or "127.0.0.1" in host or "localhost" in host:
                            provider = "ollama"
                        elif "lmstudio" in host or "lm-studio" in host:
                            provider = "lmstudio"
                        else:
                            # Unknown loopback/private endpoint: route through
                            # the generic ``custom`` provider so the agent's
                            # auxiliary client (compression, vision, web
                            # extraction) takes the OpenAI-compat custom path
                            # with ``no-key-required`` semantics. Writing
                            # ``provider: local`` here used to break
                            # compression mid-conversation because ``local``
                            # is not a registered provider in
                            # ``hermes_cli.auth.PROVIDER_REGISTRY`` — see #1384.
                            provider = "custom"
                except ValueError:
                    pass

            api_key = ""
            if isinstance(model_cfg, dict):
                api_key = (model_cfg.get("api_key") or "").strip()
            if not api_key:
                providers_cfg = cfg.get("providers", {})
                if isinstance(providers_cfg, dict):
                    for provider_key in filter(None, [active_provider, "custom"]):
                        provider_cfg = providers_cfg.get(provider_key, {})
                        if isinstance(provider_cfg, dict):
                            api_key = (provider_cfg.get("api_key") or "").strip()
                            if api_key:
                                break
            if not api_key:
                api_key_vars = (
                    "HERMES_API_KEY",
                    "HERMES_OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    "LOCAL_API_KEY",
                    "OPENROUTER_API_KEY",
                    "API_KEY",
                )
                for key in api_key_vars:
                    api_key = (all_env.get(key) or _thread_local_env_value(key) or "").strip()
                    if api_key:
                        break

            _trusted_custom_bases: list[object] = [cfg_base_url]
            _custom_providers_for_trust = cfg.get("custom_providers", [])
            if isinstance(_custom_providers_for_trust, list):
                _trusted_custom_bases.extend(
                    _cp.get("base_url")
                    for _cp in _custom_providers_for_trust
                    if isinstance(_cp, dict) and _cp.get("base_url")
                )
            _active_endpoint_models, _active_endpoint_error = _read_custom_endpoint_models(
                base_url,
                provider,
                api_key=api_key,
                trusted_base_urls=tuple(_trusted_custom_bases),
            )
            for auto_model in _active_endpoint_models:
                auto_detected_models.append(auto_model)
                provider_key = provider.lower()
                auto_detected_models_by_provider.setdefault(provider_key, []).append(auto_model)
                detected_providers.add(provider_key)

        _custom_providers_cfg = cfg.get("custom_providers", [])
        _named_custom_groups: dict = {}
        _named_custom_errors: dict[str, dict] = {}
        if isinstance(_custom_providers_cfg, list):
            _seen_custom_ids = set()
            for _cp in _custom_providers_cfg:
                if not isinstance(_cp, dict):
                    continue
                _cp_name = (_cp.get("name") or "").strip()
                _slug = _custom_provider_slug_from_name(_cp_name) if _cp_name else None
                if _slug and _slug not in _named_custom_groups:
                    _named_custom_groups[_slug] = (_cp_name, [])

                _cp_base_url = str(_cp.get("base_url") or "").strip()
                _cp_api_key = str(_cp.get("api_key") or "").strip()
                if not _cp_api_key:
                    _cp_key_env = str(_cp.get("key_env") or "").strip()
                    if _cp_key_env:
                        _cp_api_key = _thread_local_env_value(_cp_key_env).strip()
                # Fallback: check credential pool for both api_key and base_url
                if (not _cp_api_key or not _cp_base_url) and _slug:
                    try:
                        from api.config import _has_explicit_pool_credentials
                        if _has_explicit_pool_credentials(_slug):
                            from agent.credential_pool import load_pool
                            _resolved = _resolve_provider_alias(_slug)
                            _pool = load_pool(_resolved)
                            if _pool:
                                _entry = _pool.select()
                                if _entry:
                                    if not _cp_api_key:
                                        _cp_api_key = getattr(_entry, "runtime_api_key", "") or ""
                                    if not _cp_base_url:
                                        _cp_base_url = str(getattr(_entry, "base_url", "") or "").strip()
                    except ImportError:
                        pass

                if _slug and _cp_base_url:
                    # Check if user has configured models in config.yaml —
                    # configured models take priority over live /v1/models
                    # discovery (same as hermes-agent model_switch.py Section 4
                    # patch). Without this check, ZenMux and similar aggregator
                    # gateways would show hundreds of online models instead of
                    # the user's curated list.
                    _cp_configured_models = _cp.get("models")
                    _cp_has_configured_models = (
                        isinstance(_cp_configured_models, (dict, list))
                        and len(_cp_configured_models) > 0
                    )
                    _live_models = auto_detected_models_by_provider.get(_slug)
                    _live_error = None
                    if _cp_has_configured_models:
                        # Skip the live /v1/models probe when an allowlist
                        # exists — the curated list wins and probe failures
                        # should not surface as a user-facing diagnostic in
                        # that case. Still respect any pre-warm result that
                        # ``auto_detected_models_by_provider`` already
                        # populated (cheap to keep).
                        if _live_models is None:
                            _live_models = []
                    elif _live_models is None:
                        _live_models, _live_error = _read_custom_endpoint_models(
                            _cp_base_url,
                            _slug,
                            api_key=_cp_api_key,
                            trusted_base_urls=(_cp_base_url,),
                        )
                    if _live_error:
                        _named_custom_errors[_slug] = _live_error
                        detected_providers.add(_slug)
                    for _live_model in _live_models:
                        _live_id = str(_live_model.get("id") or "").strip()
                        if not _live_id:
                            continue
                        _dedup_key = f"{_slug}:{_live_id}"
                        if _dedup_key in _seen_custom_ids:
                            continue
                        _seen_custom_ids.add(_dedup_key)
                        detected_providers.add(_slug)
                        _cp_option_id = _live_id
                        if active_provider != _slug and not _cp_option_id.startswith("@"):
                            _cp_option_id = f"@{_slug}:{_cp_option_id}"
                        _named_custom_groups[_slug][1].append(
                            {"id": _cp_option_id, "label": _live_model.get("label") or _get_label_for_model(_live_id, [])}
                        )

                # Collect configured model IDs as a fallback/sticky entry after live discovery.
                _cp_model_ids: list[str] = []
                _cp_model = _cp.get("model", "")
                if _cp_model:
                    _cp_model_ids.append(_cp_model)
                for _cp_model_id in _configured_model_ids(_cp.get("models")):
                    if _cp_model_id not in _cp_model_ids:
                        _cp_model_ids.append(_cp_model_id)

                for _cp_model in _cp_model_ids:
                    _dedup_key = f"{_slug}:{_cp_model}" if _slug else _cp_model
                    if _cp_model and _dedup_key not in _seen_custom_ids:
                        _cp_label = _get_label_for_model(_cp_model, [])
                        _seen_custom_ids.add(_dedup_key)
                        if _slug:
                            detected_providers.add(_slug)
                            _cp_option_id = _cp_model
                            if active_provider != _slug and not _cp_option_id.startswith("@"):
                                _cp_option_id = f"@{_slug}:{_cp_option_id}"
                            _named_custom_groups[_slug][1].append(
                                {"id": _cp_option_id, "label": _cp_label}
                            )
                        else:
                            auto_detected_models.append({"id": _cp_model, "label": _cp_label})
                            detected_providers.add("custom")

        _has_custom_providers = isinstance(_custom_providers_cfg, list) and len(_custom_providers_cfg) > 0
        if active_provider and active_provider != "custom" and not _has_custom_providers:
            detected_providers.discard("custom")
            for _slug in list(detected_providers):
                if _slug.startswith("custom:") and not _has_custom_providers:
                    detected_providers.discard(_slug)
        elif active_provider == "custom" and _has_custom_providers:
            _has_unnamed = any(
                isinstance(_cp, dict) and not (_cp.get("name") or "").strip()
                for _cp in _custom_providers_cfg
            )
            if not _has_unnamed:
                detected_providers.discard("custom")

        _named_custom_slugs = _named_custom_provider_slugs(cfg)
        _base_matched_named_slug = _named_custom_provider_slug_for_base_url(cfg_base_url, cfg)
        if _base_matched_named_slug and _named_custom_slugs:
            for _pid in list(detected_providers):
                _pid_norm = str(_pid or "").strip().lower()
                if _pid_norm.startswith("custom:") and _pid_norm not in _named_custom_slugs:
                    detected_providers.discard(_pid)

        # Filter providers if providers.only_configured is set
        providers_cfg = cfg.get("providers", {})
        only_show_configured = providers_cfg.get("only_configured", False) if isinstance(providers_cfg, dict) else False
        if only_show_configured:
            configured_providers = set()
            if active_provider:
                configured_providers.add(active_provider)
            cfg_providers = cfg.get("providers", {})
            if isinstance(cfg_providers, dict):
                # Canonicalise here too — same rationale as #1568 detection
                # path. Without this, only_show_configured mode could
                # exclude detected ``opencode-go`` because configured_providers
                # only has the underscore-variant key from config.yaml.
                configured_providers.update(
                    _canonicalise_provider_id(k) or k for k in cfg_providers.keys()
                )
            # Only show providers that are both detected and configured
            detected_providers = detected_providers.intersection(configured_providers)

        # Post-collection dedup: re-canonicalise every entry so any path that
        # added a non-canonical id (mixed-case from auth-store, raw config-key,
        # legacy alias) gets folded onto the canonical key. Belt-and-braces for
        # #1568 — protects against future regressions in any of the ~25
        # `detected_providers.add(...)` callsites without auditing each one.
        # The fold is idempotent for already-canonical ids, so safe to run
        # unconditionally.
        if detected_providers:
            _canonicalised_detected = set()
            for _pid in detected_providers:
                _c = _canonicalise_provider_id(_pid) or _pid
                _canonicalised_detected.add(_c)
            detected_providers = _canonicalised_detected

        try:
            _moa_cfg = cfg.get("moa") if isinstance(cfg, dict) else None
            if isinstance(_moa_cfg, dict):
                _moa_enabled = bool(_moa_cfg.get("enabled", True))
                _moa_presets = _moa_cfg.get("presets")
                if _moa_enabled and isinstance(_moa_presets, dict) and _moa_presets:
                    detected_providers.add("moa")
        except Exception:
            logger.debug("Failed to inspect MoA presets for model picker", exc_info=True)

        # 5. Build model groups
        if detected_providers:
            _picker_selected_model_id = (
                (model_cfg.get("model") if isinstance(model_cfg, dict) else None)
                or default_model
                or None
            )

            def _append_picker_group(
                provider_label: str,
                provider_id: str,
                raw_models: list[dict] | None,
                *,
                models_endpoint_error: dict | None = None,
                apply_prefix: bool = True,
                decorate_overflow_label: bool = False,
                allow_empty: bool = False,
            ) -> None:
                picker_models = copy.deepcopy(raw_models or [])
                if _is_openai_family_provider(provider_id):
                    for _model in picker_models:
                        if not isinstance(_model, dict):
                            continue
                        _model_id = str(_model.get("id") or "").strip()
                        if not _model_id:
                            continue
                        _model["supports_fast_tier"] = (
                            str(
                                _resolve_main_model_fast_mode_overrides(_model_id, provider_id).get("service_tier", "")
                            ).strip().lower()
                            == "priority"
                        )
                if apply_prefix:
                    picker_models = _apply_provider_prefix(picker_models, provider_id, active_provider)
                visible_models, extra_models = _split_picker_overflow_models(
                    picker_models,
                    selected_model_id=_picker_selected_model_id,
                    provider_id=provider_id,
                )
                if not (visible_models or extra_models or models_endpoint_error or allow_empty):
                    return
                group_entry = {
                    "provider": provider_label,
                    "provider_id": provider_id,
                    "models": visible_models,
                }
                if decorate_overflow_label and extra_models:
                    group_entry["provider"] = (
                        f"{provider_label} ({len(visible_models)} of {len(visible_models) + len(extra_models)})"
                    )
                if extra_models:
                    group_entry["extra_models"] = extra_models
                if models_endpoint_error:
                    group_entry["models_endpoint_error"] = models_endpoint_error
                groups.append(group_entry)

            for pid in sorted(detected_providers):
                # Custom-provider PIDs are populated above via the
                # _named_custom_groups branch (or skipped intentionally).
                # They MUST NOT fall through to the auto_detected_models
                # fallback below, otherwise the active provider's models
                # get copied into a phantom Custom group with mismatched
                # provider prefixes (#1881).
                if pid.startswith("custom:"):
                    if pid in _named_custom_groups:
                        _nc_display, _nc_models = _named_custom_groups[pid]
                        # If all named-group models were deduped (already auto-detected
                        # from base_url /v1/models), fall back to auto-detected models
                        # instead of silently dropping the group (issue #1619).
                        #
                        # Per Opus advisor on stage-295: the load-bearing fix for the
                        # reporter's symptom is the api/routes.py:/api/models/live
                        # broadening to handle custom:* slugs. This block is defensive
                        # belt-and-braces — under current _named_custom_groups
                        # population logic (atomic add+append inside the same dedup
                        # guard at line ~2640), an empty list shouldn't reach here.
                        # Kept for future-proofing in case the population logic
                        # changes (e.g. supporting model-less custom_providers entries).
                        if not _nc_models:
                            _nc_models = auto_detected_models_by_provider.get(pid, [])
                        if _nc_models or pid in _named_custom_errors:
                            _append_picker_group(
                                _nc_display,
                                pid,
                                _nc_models,
                                models_endpoint_error=_named_custom_errors.get(pid),
                                apply_prefix=False,
                            )
                    continue
                provider_name = _effective_provider_display_name(pid, _PROVIDER_DISPLAY)
                if pid == "openrouter":
                    # OpenRouter has two model surfaces:
                    #   (1) curated tool-supporting catalog via hermes_cli.models.fetch_openrouter_models()
                    #       — the canonical agent-ready list, applies a tool-support filter
                    #       (Kilo-Org/kilocode#9068) that hides image/completion-only models
                    #   (2) free-tier `:free` variants — newly-added models OpenRouter ships
                    #       experimentally that may not yet advertise `tools` in supported_parameters
                    #       (see #1426). These get filtered out of (1) but users want them visible.
                    #
                    # Strategy: take the live curated list as the base, then augment with a
                    # separate live-fetch of OpenRouter's /v1/models filtered to free-tier-only.
                    # Free-tier entries get a "(free)" label suffix so the picker is honest about
                    # what the user is selecting. Falls back to the static _FALLBACK_MODELS list
                    # when both live fetches fail (offline, transient API error, test env).
                    raw_models = []
                    seen_ids = set()
                    try:
                        from hermes_cli.models import (
                            fetch_openrouter_models as _fetch_or_models,
                        )
                        live_curated = _fetch_or_models() or []
                        for mid, _desc in live_curated:
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                raw_models.append({"id": mid, "label": mid})
                    except Exception:
                        logger.warning("Failed to load OpenRouter curated catalog from hermes_cli")

                    # Free-tier live fetch — bypasses the tool-support filter so models
                    # OpenRouter has flagged free but hasn't yet annotated with tools=[]
                    # (or that have tools=[] but the user explicitly wants to try) appear.
                    try:
                        import urllib.request as _urlreq
                        _req = _urlreq.Request(
                            "https://openrouter.ai/api/v1/models",
                            headers={"Accept": "application/json"},
                        )
                        free_tier_models = []
                        selected_free_tier_model = None
                        with _urlreq.urlopen(_req, timeout=8.0) as _resp:
                            _payload = json.loads(_resp.read().decode())
                        for _item in _payload.get("data", []) or []:
                            if not isinstance(_item, dict):
                                continue
                            _mid = str(_item.get("id") or "").strip()
                            if not _mid or _mid in seen_ids:
                                continue
                            _pricing = _item.get("pricing")
                            _is_free = False
                            if (
                                isinstance(_pricing, dict)
                                and "prompt" in _pricing
                                and "completion" in _pricing
                            ):
                                try:
                                    _is_free = (
                                        float(_pricing["prompt"]) == 0
                                        and float(_pricing["completion"]) == 0
                                    )
                                except (TypeError, ValueError):
                                    _is_free = False
                            # Also include explicit `:free` suffix variants
                            _is_free = _is_free or _mid.endswith(":free")
                            if not _is_free:
                                continue
                            _name = (
                                str(_item.get("name") or "").strip() or _mid
                            )
                            # Strip provider prefix from name for display, append (free)
                            _label = _name.split("/")[-1] if "/" in _name else _name
                            if "(free)" not in _label.lower():
                                _label = f"{_label} (free)"
                            _entry = {"id": _mid, "label": _label}
                            free_tier_models.append(_entry)
                            if _model_matches_picker_selection(
                                _mid,
                                _picker_selected_model_id,
                                "openrouter",
                            ):
                                selected_free_tier_model = _entry
                        if len(free_tier_models) > _OPENROUTER_FREE_TIER_AUGMENT_CAP:
                            free_tier_models = free_tier_models[:_OPENROUTER_FREE_TIER_AUGMENT_CAP]
                            if (
                                selected_free_tier_model
                                and not any(
                                    m.get("id") == selected_free_tier_model.get("id")
                                    for m in free_tier_models
                                )
                            ):
                                free_tier_models[-1] = selected_free_tier_model
                        for _entry in free_tier_models:
                            seen_ids.add(_entry["id"])
                            raw_models.append(_entry)
                    except Exception:
                        logger.debug("OpenRouter free-tier live fetch unavailable; using fallback")

                    if not raw_models:
                        # Both live fetches failed — fall back to the curated static list.
                        # Deepcopy so dedup/prefix mutation downstream does not bleed
                        # into the module-level catalog.
                        raw_models = [
                            {"id": m["id"], "label": m["label"]}
                            for m in _FALLBACK_MODELS
                            if m.get("provider") == "OpenRouter"
                        ]

                    _append_picker_group("OpenRouter", "openrouter", raw_models)
                elif pid == "ollama-cloud":
                    raw_models = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        raw_models = [
                            {"id": mid, "label": _format_ollama_label(mid)}
                            for mid in (_provider_model_ids("ollama-cloud") or [])
                        ]
                    except Exception:
                        logger.warning("Failed to load Ollama Cloud models from hermes_cli")

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif pid == "openai-codex":
                    # Codex account catalogs drift faster than WebUI releases
                    # (for example gpt-5.3-codex-spark in #1680). Ask the
                    # agent's Codex resolver first so /api/models inherits the
                    # live Codex API / local ~/.codex cache / static fallback
                    # chain instead of freezing the picker to WebUI's curated
                    # _PROVIDER_MODELS snapshot.
                    raw_models = []
                    codex_ids = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        codex_ids = [mid for mid in (_provider_model_ids("openai-codex") or []) if mid]
                    except Exception:
                        logger.warning("Failed to load OpenAI Codex models from hermes_cli")

                    for mid in _read_visible_codex_cache_model_ids():
                        if mid not in codex_ids:
                            codex_ids.append(mid)

                    raw_models = [
                        {"id": mid, "label": _get_label_for_model(mid, [])}
                        for mid in codex_ids
                    ]

                    if not raw_models:
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get("openai-codex", []))

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif pid == "nous":
                    # Nous Portal exposes a curated catalog (~30 models on most
                    # accounts, up to several hundred for enterprise tiers) via
                    # inference-api.nousresearch.com. Like ollama-cloud, we
                    # live-fetch through hermes_cli.models.provider_model_ids()
                    # rather than relying on the static four-entry list, which
                    # chronically drifts out of date (#1538).
                    #
                    # When the catalog exceeds _NOUS_FEATURED_THRESHOLD (~25)
                    # the picker dropdown gets a curated subset to stay
                    # scannable — the full list is still returned under
                    # "extra_models" for the slash-command autocomplete and
                    # the dynamic-label map (#1567). The optgroup label is
                    # decorated with the truncation count so users know more
                    # exists.
                    raw_models = []
                    live_fetch_failed = False
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        live_ids = _provider_model_ids("nous") or []
                    except Exception:
                        logger.warning("Failed to load Nous Portal models from hermes_cli")
                        live_ids = []
                        live_fetch_failed = True

                    if live_ids:
                        featured_ids, extras_ids = _build_nous_featured_set(
                            live_ids,
                            selected_model_id=_picker_selected_model_id,
                        )
                        ordered_ids = featured_ids + extras_ids
                        raw_models = [
                            {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                            for mid in ordered_ids
                        ]
                    elif not live_fetch_failed:
                        # Live-fetch returned an empty list AND did not raise —
                        # the user is gated as authenticated by detection above
                        # but the catalog endpoint replied with no models.
                        # Showing the static 4-entry curated list here would
                        # contradict the providers card (which always shows
                        # the live catalog) — exactly the asymmetry #1567
                        # reports. Omit the Nous group entirely; the providers
                        # card already tells the truth, and a transient empty
                        # response will self-heal on the next cache rebuild.
                        logger.warning(
                            "Nous Portal authenticated but live-fetch returned empty — "
                            "omitting from picker (will retry on next cache rebuild)"
                        )
                    else:
                        # hermes_cli unavailable / raised — fall back to the
                        # curated 4-entry static list so the picker is never
                        # empty in this degraded state. This matches pre-#1538
                        # behaviour for environments without hermes_cli (test
                        # envs, package mismatches, isolated WebUI builds).
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get("nous", []))

                    if raw_models:
                        _append_picker_group(
                            provider_name,
                            pid,
                            raw_models,
                            apply_prefix=False,
                            decorate_overflow_label=True,
                        )
                elif pid == "lmstudio":
                    # LM Studio is a local server — fetch live loaded models via
                    # the OpenAI-compatible /v1/models endpoint (#WebUI).
                    #
                    # Two-tier lookup, each in its own try so a failure in one
                    # does not abort the other (the bug pattern that broke
                    # tests/test_issue1527_lmstudio_base_url_classification on
                    # CI environments where hermes_cli isn't importable —
                    # ImportError in the cli tier was hijacking the whole
                    # branch and silently skipping the urlopen fallback).
                    raw_models = []
                    lm_ids: list[str] = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids
                        lm_ids = _provider_model_ids("lmstudio") or []
                    except Exception:
                        logger.debug("hermes_cli LM Studio lookup unavailable; using urlopen fallback")

                    if lm_ids:
                        raw_models = [{"id": mid, "label": mid} for mid in lm_ids]
                    else:
                        # Fallback: fetch /models directly from the configured
                        # base URL. Looks for the URL in either
                        # `cfg["providers"]["lmstudio"]["base_url"]` or
                        # `cfg["model"]["base_url"]` (via _get_provider_base_url),
                        # so the historical model-block config shape still works.
                        lm_cfg = _get_provider_cfg("lmstudio")
                        lm_base_url = _get_provider_base_url("lmstudio") or ""
                        lm_api_key = str(lm_cfg.get("api_key") or "").strip()
                        if lm_base_url:
                            headers = {"User-Agent": "OpenAI/Python 1.0"}
                            if lm_api_key:
                                headers["Authorization"] = f"Bearer {lm_api_key}"
                            endpoint = (lm_base_url + "/models").rstrip("/")
                            try:
                                import urllib.request as _urlreq
                                req = _urlreq.Request(endpoint, method="GET", headers=headers)
                                with _urlreq.urlopen(req, timeout=5) as resp:
                                    lm_data = json.loads(resp.read().decode())
                                for m in (lm_data.get("data") or []):
                                    if isinstance(m, dict):
                                        mid = str(m.get("id") or "").strip()
                                        if mid and {"id": mid, "label": mid} not in raw_models:
                                            raw_models.append({"id": mid, "label": mid})
                            except Exception:
                                logger.debug("LM Studio /models fetch failed at %s", endpoint)

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif (
                    pid in _PROVIDER_MODELS
                    or pid in _PROVIDER_DISPLAY
                    or pid in _canonical_to_raw_provider_key
                    or _is_plugin_model_provider(pid)
                ):
                    # Look up provider_cfg using the original raw key from
                    # config.yaml so that mixed-case / underscore keys like
                    # ``CLIPpoxy`` or ``snake_case_provider`` still resolve
                    # (#2245).  Fall back to the canonical pid for providers
                    # that appear in _PROVIDER_MODELS but not in cfg.
                    _raw_key = _canonical_to_raw_provider_key.get(pid, pid)
                    provider_cfg = _get_provider_cfg(_raw_key)
                    raw_models = []

                    # User-configured model allowlists are explicit local
                    # source-of-truth for custom/plugin providers, AND for most
                    # built-in Hermes providers (e.g. providers.anthropic.models
                    # is a real picker allowlist — see #644). Copilot is the
                    # exception: it uses providers.copilot.models as a per-model
                    # settings map (reasoning_effort, limits, etc.), so treating
                    # that as an allowlist collapsed the Copilot picker to
                    # whichever model had local settings. Only Copilot skips the
                    # config-models allowlist branch and asks Hermes CLI for the
                    # live catalog first (static _PROVIDER_MODELS is fallback only).
                    _uses_models_as_settings_map = pid == "copilot"
                    if (
                        not _uses_models_as_settings_map
                        and isinstance(provider_cfg, dict)
                        and "models" in provider_cfg
                    ):
                        raw_models = _configured_model_options(provider_cfg["models"])

                    if not raw_models:
                        if pid == "moa":
                            raw_models = _moa_preset_models_from_config(cfg)
                        elif pid == "opencode-go":
                            # Skip live /v1/models probe for OpenCode Go — it
                            # returns models from the public catalog that are
                            # not enabled on the Go tier, causing 404 when
                            # selected. Use the curated static list only. (#5311)
                            pass
                        else:
                            raw_models = _models_from_live_provider_ids(
                                pid,
                                _read_live_provider_model_ids(pid),
                            )

                    if not raw_models:
                        raw_models = copy.deepcopy(_PROVIDER_MODELS.get(pid, []))

                    detected_models = auto_detected_models_by_provider.get(pid, [])
                    if detected_models and not raw_models:
                        raw_models = copy.deepcopy(detected_models)
                    _append_picker_group(provider_name, pid, raw_models)
                else:
                    detected_models = auto_detected_models_by_provider.get(pid)
                    if detected_models:
                        models_for_group = copy.deepcopy(detected_models)
                    elif auto_detected_models and (pid == "custom" or _is_known_model_provider(pid)):
                        # Don't fall back to the global auto_detected_models
                        # list for the bare "custom" PID when the active
                        # provider is something concrete (e.g. ai-gateway,
                        # openrouter). Those auto-detected entries already
                        # belong to the active provider's group — copying
                        # them into a Custom group too produces phantom
                        # duplicates with mismatched prefixes (#1881).
                        if pid == "custom" and active_provider and active_provider != "custom":
                            models_for_group = []
                        else:
                            models_for_group = copy.deepcopy(auto_detected_models)
                    else:
                        # An unrecognized provider id with no catalog of its
                        # own must NOT be painted with the global
                        # auto_detected_models list. Otherwise a non-model
                        # credential_pool key (the Photon plugin's
                        # photon/photon_project/photon_user entries, #4324) or
                        # any future unknown id renders as a phantom provider
                        # carrying the active endpoint's entire model catalog.
                        # Such ids are dropped upstream by
                        # _is_known_model_provider() in the pool-detection
                        # loop; this omission is belt-and-braces matching the
                        # #1572/#7372 "omit rather than misattribute" posture.
                        models_for_group = []
                    if models_for_group:
                        # Per-group deep copy so subsequent mutation by
                        # _deduplicate_model_ids() (which prefixes ids with
                        # @provider_id:) does not bleed into other groups
                        # that also fall through to this branch (#1511 root
                        # cause: multiple unconfigured providers all sharing
                        # the same auto_detected_models list reference would
                        # see every group's id rewritten to the FIRST
                        # provider's prefix, and labels accumulated every
                        # provider's name).
                        _append_picker_group(
                            provider_name,
                            pid,
                            models_for_group,
                            apply_prefix=False,
                        )
                    elif pid == "custom" and cfg_base_url:
                        # Anonymous custom endpoint: /v1/models probe may have
                        # failed (e.g. llama-server, lightweight relay), but the
                        # chat endpoint itself may still work. Add the group
                        # with an empty model list so the user can type a model
                        # ID manually rather than being blocked by a silent
                        # probe failure (#2542).
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": [],
                            }
                        )
        else:
            if default_model:
                label = _get_label_for_model(default_model, groups)
                groups.append(
                    {"provider": "Default", "provider_id": "default", "models": [{"id": default_model, "label": label}]}
                )

        if default_model:
            # Guard against provider-id values mistakenly stored in
            # ``model.default``. The injection logic below puts ANY string
            # into the picker as a fake option, so a stray provider id
            # surfaces as a self-referential phantom model labelled e.g.
            # ``Opencode GO`` — a 15th entry under the OpenCode Go group
            # (#1568). The user's misconfig is real, but the picker is
            # the wrong surface to surface it; we'd rather skip injection
            # and emit a warning so the underlying config issue is logged.
            _looks_like_provider_id = (
                str(default_model).strip().lower().replace("_", "-") in _PROVIDER_DISPLAY
                or _canonicalise_provider_id(default_model) in _PROVIDER_DISPLAY
            )
            if _looks_like_provider_id:
                logger.warning(
                    "Suspicious model.default value %r — looks like a provider id, "
                    "not a model id. Skipping picker injection. Check `model.default` "
                    "in config.yaml.",
                    default_model,
                )
            else:
                all_ids_norm = {
                    _norm_model_id(m["id"])
                    for g in groups
                    for bucket_name in ("models", "extra_models")
                    for m in g.get(bucket_name, [])
                }
                if _norm_model_id(default_model) not in all_ids_norm:
                    label = _get_label_for_model(default_model, groups)
                    target_display = (
                        _PROVIDER_DISPLAY.get(active_provider, active_provider or "").lower()
                        if active_provider
                        else ""
                    )
                    injected = False
                    for g in groups:
                        if target_display and g.get("provider", "").lower() == target_display:
                            g["models"].insert(0, {"id": default_model, "label": label})
                            injected = True
                            break
                    if not injected and groups:
                        groups.append(
                            {
                                "provider": "Default",
                                "provider_id": active_provider or "default",
                                "models": [{"id": default_model, "label": label}],
                            }
                        )

        # Post-process: ensure model IDs are globally unique across groups.
        # When multiple providers expose the same bare model ID, prefix
        # collisions with @provider_id: so the frontend can distinguish them.
        _deduplicate_model_ids(groups)

        # Defense-in-depth: drop any optgroup that ended up with zero models
        # — those are pure UI noise. A zero-model group typically means a
        # detection path added an id that has no static catalog AND the
        # live-fetch returned empty (#1568 — the user's
        # ``providers.opencode_go`` config-key path produced an empty
        # ``Opencode_Go`` group at the end of the picker before this fix).
        # Custom providers from ``custom_providers`` config are exempt —
        # they may legitimately render with zero entries when the user
        # hasn't filled in models yet but wants the card visible.
        groups = [
            g for g in groups
            if g.get("models")
            or (g.get("provider_id") or "").startswith("custom:")
        ]

        # Sort groups: active provider first, then custom:* providers,
        # then providers with configured keys, then the rest alphabetically.
        _providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _providers_with_keys.add(_resolve_provider_alias(str(_pid)))
        except Exception:
            pass
        try:
            _cfg_providers = cfg.get("providers", {})
            if isinstance(_cfg_providers, dict):
                for _pk, _pv in _cfg_providers.items():
                    if isinstance(_pv, dict) and (_pv.get("api_key") or _pv.get("key_env")):
                        _providers_with_keys.add(_resolve_provider_alias(str(_pk)))
        except Exception:
            pass

        def _group_sort_key(g):
            pid = g.get("provider_id") or ""
            if pid == active_provider:
                return (0, pid)
            if pid.startswith("custom:"):
                return (1, pid)
            if pid in _providers_with_keys:
                return (2, pid)
            return (3, pid)
        groups.sort(key=_group_sort_key)

        # 12. Include model aliases so the WebUI frontend can resolve them.
        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {str(k).strip(): str(v).strip() for k, v in raw_aliases.items() if k and v}
        except Exception:
            pass

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _build_configured_model_badges(),
            "groups": groups,
            "aliases": model_aliases,
        }

    # ── FAST PATH ─────────────────────────────────────────────────────────────
    # Mark that a build may be in progress BEFORE acquiring the lock.
    # If another thread has already started the cold path, we will wait for
    # its result rather than running the cold path concurrently.
    should_wait = _cache_build_in_progress
    force_refresh_started_at = time.monotonic() if force_refresh else None

    # Check config mtime OUTSIDE the lock so this cheap check doesn't serialize
    # concurrent requests.  Must come before any config reads in the cold path.
    try:
        _current_mtime = Path(_get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    _cfg_changed = _current_mtime != _cfg_mtime

    # Disk load BEFORE lock: ~0.1ms, lets concurrent requests skip entirely.
    # Then acquire lock and check memory cache.  Cold path runs inside the lock
    # so only one thread rebuilds while others wait.
    disk_groups = None
    stale_disk_groups = None
    if _available_models_cache is None and not force_refresh:
        disk_groups = _load_models_cache_from_disk()
        if disk_groups is None:
            stale_disk_groups = _load_stale_models_cache_from_disk()
    elif force_refresh:
        stale_disk_groups = _load_stale_models_cache_from_disk()

    with _available_models_cache_lock:
        # If another thread is already building, wait for its result instead
        # of re-entering the cold path (avoids duplicate 10s zai load_pool calls).
        if should_wait:
            wait_timeout = 60.0
            if force_refresh and force_refresh_started_at is not None:
                if _LIVE_REBUILD_BUDGET_SECONDS <= 0:
                    # The legacy synchronous path is explicitly unbounded. A
                    # forced refresh follower should keep coalescing behind
                    # that live rebuild instead of giving up after 60s and
                    # duplicating it.
                    wait_timeout = None
                else:
                    wait_timeout = max(
                        0.0,
                        _LIVE_REBUILD_BUDGET_SECONDS - (time.monotonic() - force_refresh_started_at),
                    )
            _cache_build_cv.wait_for(
                lambda: not _cache_build_in_progress,
                timeout=wait_timeout
            )
            cached = _get_fresh_memory_models_cache(time.monotonic())
            if (
                cached is not None
                and (
                    not force_refresh
                    or (
                        force_refresh_started_at is not None
                        and _available_models_live_rebuild_ts >= force_refresh_started_at
                    )
                )
            ):
                return cached
            if force_refresh and _LIVE_REBUILD_BUDGET_SECONDS > 0 and _cache_build_in_progress:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Reload config if changed
        if _cfg_changed:
            reload_config()
            _available_models_cache = None
            _available_models_cache_ts = 0.0
            _available_models_live_rebuild_ts = 0.0
            _available_models_cache_source_fingerprint = None
            _sync_models_cache_provenance()
            _invalidate_models_build_locked()
            disk_groups = None
            stale_disk_groups = None

        # Serve from memory cache if fresh
        now = time.monotonic()
        cached = _get_fresh_memory_models_cache(now)
        if cached is not None:
            if not force_refresh:
                return cached
            if (
                force_refresh_started_at is not None
                and _available_models_live_rebuild_ts >= force_refresh_started_at
            ):
                return cached

        # A concurrent forced refresh may have started after this caller sampled
        # should_wait but before it acquired the lock. Reuse that in-flight build
        # instead of launching another one, and preserve this caller's budget.
        if (
            force_refresh
            and force_refresh_started_at is not None
            and _cache_build_in_progress
        ):
            remaining_budget = None
            if _LIVE_REBUILD_BUDGET_SECONDS > 0:
                remaining_budget = max(
                    0.0,
                    _LIVE_REBUILD_BUDGET_SECONDS - (time.monotonic() - force_refresh_started_at),
                )
            if remaining_budget is None or remaining_budget > 0:
                _cache_build_cv.wait_for(
                    lambda: not _cache_build_in_progress,
                    timeout=remaining_budget,
                )
                cached = _get_fresh_memory_models_cache(time.monotonic())
                if (
                    cached is not None
                    and _available_models_live_rebuild_ts >= force_refresh_started_at
                ):
                    return cached
            if _cache_build_in_progress and _LIVE_REBUILD_BUDGET_SECONDS > 0:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None and not force_refresh:
            _available_models_cache = disk_groups
            _available_models_cache_ts = now
            _available_models_cache_source_fingerprint = _models_cache_source_fingerprint()
            _sync_models_cache_provenance()
            return copy.deepcopy(disk_groups)

        # ── prefer_cache: NEVER run the live provider rebuild ────────────────
        # Server-initiated wakeup turns (Option Z) reach here with a cold
        # cache (the drain thread fires while idle; the catalog warmed by a
        # human's /api/models has expired or was never built). The live
        # rebuild does a Copilot token-exchange HTTPS call per the proven
        # thread-stack; on this WSL/corp network it stalls the wakeup
        # chat/start indefinitely. A wakeup turn does NOT need the full live
        # catalog — _resolve_compatible_session_model_state only needs
        # default_model/active_provider and trusts the persisted session
        # model. Serve a network-free minimal catalog instead and let a later
        # human request do the real live rebuild.
        if prefer_cache:
            # NOTE (Greptile P1): do NOT touch _cache_build_in_progress here.
            # This branch never set the flag (only the cold path below does),
            # and `should_wait` is sampled outside the lock (line ~4964). A
            # concurrent cold-path caller can flip the flag to True after our
            # sample but before we acquire the lock; clearing it here would
            # prematurely release that rebuild's serialization, waking waiters
            # to an empty cache and triggering a second live rebuild. Just
            # serve the network-free minimal catalog and leave the flag alone.
            return copy.deepcopy(_minimal_static_models_catalog())

        # Cold path: full rebuild — only one thread reaches here at a time
        with _cache_build_cv:
            _models_cache_build_generation += 1
            build_generation = _models_cache_build_generation
            _active_models_cache_build_generation = build_generation
            _cache_build_in_progress = True

        # Capture the active per-request profile (#3957). The live provider
        # probe inside the rebuild resolves credentials from os.environ /
        # HERMES_HOME and the disk-cache path/fingerprint from the profile TLS;
        # the detached worker thread below inherits NEITHER, so it must be
        # captured here (on the request thread, where the TLS is valid) and
        # re-bound on the worker. Empty / default for single-profile installs.
        from contextlib import nullcontext as _nullcontext

        _active_profile_name = ""
        _prof_env_request = None
        _prof_scope_worker = None
        try:
            from api.profiles import (
                get_active_profile_name as _gapn,
                profile_env_for_active_request as _prof_env_request,
                profile_scope_for_detached_worker as _prof_scope_worker,
            )
            _active_profile_name = (_gapn() or "").strip()
        except Exception:
            _prof_env_request = None
            _prof_scope_worker = None

        # Legacy synchronous (unbounded) rebuild — opt-in via budget<=0.
        if _LIVE_REBUILD_BUDGET_SECONDS <= 0:
            try:
                # Foreground thread already carries the request-profile TLS;
                # apply the mirrored profile env (no-op for default) for the
                # live probe because provider_model_ids() still has raw
                # os.getenv()/HERMES_HOME readers on this synchronous path.
                _sync_scope = (
                    _prof_env_request("models rebuild (sync)")
                    if _prof_env_request is not None
                    else _nullcontext()
                )
                with _sync_scope:
                    result = _invoke_models_rebuild(_build_available_models_uncached)
            except BaseException:
                # Always reset the flag so waiting threads don't block for 60s
                with _cache_build_cv:
                    if _active_models_cache_build_generation == build_generation:
                        _active_models_cache_build_generation = None
                        _cache_build_in_progress = False
                        _cache_build_cv.notify_all()
                raise
            with _cache_build_cv:
                if _active_models_cache_build_generation == build_generation:
                    published_at = time.monotonic()
                    _available_models_cache = result
                    _available_models_cache_ts = published_at
                    _available_models_live_rebuild_ts = published_at
                    _available_models_cache_source_fingerprint = (
                        _models_cache_source_fingerprint()
                    )
                    _sync_models_cache_provenance()
                    try:
                        _save_models_cache_to_disk(result)
                    finally:
                        if _active_models_cache_build_generation == build_generation:
                            _active_models_cache_build_generation = None
                            _cache_build_in_progress = False
                            _cache_build_cv.notify_all()
            return copy.deepcopy(result)

        # ── Bounded rebuild (defense-in-depth) ───────────────────────────────
        # The live rebuild does a network probe per provider (Copilot token
        # exchange over HTTPS, OpenRouter/Nous /models, ...). On a flaky / corp
        # / WSL network any single probe can stall for its full per-call
        # timeout and, summed across providers, pin a foreground request
        # thread for tens of seconds (the wakeup-turn / chat-start hang).
        #
        # Run the rebuild on a daemon worker; the foreground waits at most
        # _LIVE_REBUILD_BUDGET_SECONDS.
        #
        # WITHIN budget (the normal fast case): the FOREGROUND publishes the
        # result synchronously and only then returns — preserving the exact
        # pre-existing contract (cache + on-disk file populated by the time
        # get_available_models() returns). The worker stays hands-off.
        #
        # OVER budget (a provider probe is slow/hung): the foreground returns
        # the best fallback immediately and the still-running worker publishes
        # its result out-of-band when it finally finishes, so the next caller
        # gets a warm cache instead of paying the cold rebuild again.
        #
        # ``_publish_models_result`` / ``box["published"]`` ensure exactly one
        # publisher even at the budget boundary (no double write, no lost
        # refresh). The worker only touches _cache_build_cv after the
        # foreground releases the RLock by returning, so no lock inversion.
        build_done = threading.Event()
        budget_exceeded = threading.Event()
        publish_lock = threading.Lock()
        box: dict = {}

        def _publish_models_result(result):
            global _cache_build_in_progress, _available_models_cache
            global _available_models_cache_ts, _available_models_live_rebuild_ts
            global _available_models_cache_source_fingerprint
            global _active_models_cache_build_generation
            with _cache_build_cv:
                if _active_models_cache_build_generation != build_generation:
                    return
                published_at = time.monotonic()
                _available_models_cache = result
                _available_models_cache_ts = published_at
                _available_models_live_rebuild_ts = published_at
                _available_models_cache_source_fingerprint = (
                    _models_cache_source_fingerprint()
                )
                _sync_models_cache_provenance()
                try:
                    _save_models_cache_to_disk(result)
                except Exception:
                    logger.debug("models cache disk save failed", exc_info=True)
                finally:
                    _active_models_cache_build_generation = None
                    _cache_build_in_progress = False
                    _cache_build_cv.notify_all()

        def _clear_build_in_progress():
            global _cache_build_in_progress, _active_models_cache_build_generation
            with _cache_build_cv:
                if _active_models_cache_build_generation != build_generation:
                    return
                _active_models_cache_build_generation = None
                _cache_build_in_progress = False
                _cache_build_cv.notify_all()

        def _claim_publish() -> bool:
            """Return True iff the caller won the right to publish."""
            with publish_lock:
                if box.get("published"):
                    return False
                box["published"] = True
                return True

        def _rebuild_worker():
            # Re-bind the captured per-request profile on THIS worker thread
            # (#3957): the daemon inherits neither the request-profile TLS nor
            # os.environ, so without this it would probe the default profile's
            # credentials and, over budget, publish the rebuilt catalog to the
            # DEFAULT profile's disk cache. No-op for the default profile.
            _worker_scope = (
                _prof_scope_worker(_active_profile_name, "models rebuild (worker)")
                if _prof_scope_worker is not None
                else _nullcontext()
            )
            with _worker_scope:
                try:
                    box["result"] = _invoke_models_rebuild(_build_available_models_uncached)
                except Exception as exc:  # noqa: BLE001 — propagated to caller
                    box["error"] = exc
                finally:
                    build_done.set()
                    # Only publish out-of-band if the foreground already gave up
                    # (over budget). Within budget the foreground publishes
                    # synchronously, so the worker must NOT touch the cache.
                    # NOTE: the publish (and its disk write + fingerprint) runs
                    # INSIDE this profile scope so the over-budget path writes
                    # the correct profile's cache file.
                    if budget_exceeded.is_set() and _claim_publish():
                        if "result" in box:
                            _publish_models_result(box["result"])
                        else:
                            _clear_build_in_progress()

        _worker = threading.Thread(
            target=_rebuild_worker,
            name="models-catalog-rebuild",
            daemon=True,
        )
        _worker.start()

        if build_done.wait(timeout=_LIVE_REBUILD_BUDGET_SECONDS):
            # Build finished within budget — foreground publishes
            # synchronously, exactly like the legacy path.
            if "error" in box:
                _clear_build_in_progress()
                raise box["error"]
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Budget elapsed. Mark it so the worker knows it owns out-of-band
        # publication. Handle the tiny race where the build completed between
        # wait() returning False and here: if so, still publish synchronously
        # so this caller honours the cache contract.
        budget_exceeded.set()
        if build_done.is_set() and "error" not in box and "result" in box:
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Genuinely slow/hung probe: serve the best fallback now; the worker
        # keeps going and refreshes the cache for the next caller.
        # Rate-limit the warning per Q-2979-A3 — see _should_warn_budget; a
        # sustained budget breach demotes to info after the first emit in
        # each cooldown window so log volume stays bounded.
        _budget_log_msg = (
            "live provider-catalog rebuild exceeded %.1fs budget — serving "
            "fallback, refreshing catalog out-of-band"
        )
        if _should_warn_budget("live_rebuild_budget_exceeded"):
            logger.warning(_budget_log_msg, _LIVE_REBUILD_BUDGET_SECONDS)
        else:
            logger.info(_budget_log_msg, _LIVE_REBUILD_BUDGET_SECONDS)
        # ``stale_disk_groups`` is shape-valid but failed the strict metadata
        # checks required for authoritative cold-path use. It was read before
        # acquiring _available_models_cache_lock so this over-budget fallback
        # does not extend the lock hold while the worker is ready to publish.
        if stale_disk_groups is not None:
            return copy.deepcopy(stale_disk_groups)
        return copy.deepcopy(_static_models_catalog_without_live_probes())


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


from api.stream_channel import StreamChannel, create_stream_channel


STREAMS: dict = {}
STREAMS_LOCK = threading.Lock()
# stream_id -> session_id owner, populated synchronously before worker startup so
# stream-id authorization does not depend on worker lifecycle registration.
STREAM_SESSION_OWNERS: dict = {}
STREAM_SESSION_OWNERS_LOCK = threading.Lock()
CANCEL_FLAGS: dict = {}
AGENT_INSTANCES: dict = {}  # stream_id -> AIAgent instance for interrupt propagation
STREAM_PARTIAL_TEXT: dict = {}  # stream_id -> partial assistant text accumulated during streaming
STREAM_REASONING_TEXT: dict = {}  # stream_id -> reasoning trace accumulated during streaming (#1361 §A)
STREAM_LIVE_TOOL_CALLS: dict = {}  # stream_id -> live tool calls accumulated during streaming (#1361 §B)
STREAM_GOAL_RELATED: dict = {}  # stream_id -> bool: only evaluate goal for goal-related turns (#1932)
STREAM_LAST_EVENT_ID: dict = {}  # stream_id -> latest journal event_id for `id:` field on live SSE frames (stage-364)
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
# A drain task spawned at WebUI startup (api/background_process.py) reads that
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

# Active agent-run registry. This intentionally tracks worker lifecycle rather
# than SSE lifecycle: cancel/reconnect may remove STREAMS while the worker is
# still unwinding, blocked in a provider call, or waiting for delegated work.
ACTIVE_RUNS: dict = {}
ACTIVE_RUNS_LOCK = threading.Lock()
LAST_RUN_FINISHED_AT: float | None = None
SERVER_START_TIME = time.time()


RUNTIME_STATE = ProcessRuntimeState(
    streams=STREAMS,
    stream_owners=STREAM_SESSION_OWNERS,
    cancel_flags=CANCEL_FLAGS,
    agent_instances=AGENT_INSTANCES,
    partial_text=STREAM_PARTIAL_TEXT,
    reasoning_text=STREAM_REASONING_TEXT,
    live_tool_calls=STREAM_LIVE_TOOL_CALLS,
    goal_related=STREAM_GOAL_RELATED,
    last_event_ids=STREAM_LAST_EVENT_ID,
    active_runs=ACTIVE_RUNS,
    streams_lock=STREAMS_LOCK,
    owners_lock=STREAM_SESSION_OWNERS_LOCK,
    active_runs_lock=ACTIVE_RUNS_LOCK,
)


# Keep the long-standing ``api.config`` import and monkeypatch surface while the
# implementations live in a cohesive leaf module.  The resolver is late-bound
# so patched facade state remains authoritative for every adapter call.
from api.config_parts import runtime_registry as _runtime_registry

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
import collections
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
        from api.session_lifecycle import commit_session_memory, discard_session, has_uncommitted_work, unregister_agent
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

# ── Thread-local env context ─────────────────────────────────────────────────
# (_thread_ctx + _thread_local_env_value are defined near the top of this module,
# above the config-file section, so _expand_env_vars can reference them at the
# import-time reload_config() without a forward-reference NameError.)


def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs


def _clear_thread_env():
    _thread_ctx.env = {}


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

from api.config_parts import settings_persistence as _settings_persistence

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
save_settings = _settings_persistence.save_settings

_settings_persistence._apply_startup_settings()
# ── SESSIONS in-memory cache (LRU OrderedDict) ───────────────────────────────
SESSIONS: collections.OrderedDict = collections.OrderedDict()

# ── Profile state initialisation ────────────────────────────────────────────
# Must run after all imports are resolved to correctly patch module-level caches
try:
    from api.profiles import init_profile_state

    init_profile_state()
except ImportError:
    pass  # hermes_cli not available -- default profile only


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
