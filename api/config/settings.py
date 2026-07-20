"""Settings validation, migration, and durable persistence policies."""

import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from api import config as _config_module

# ── Settings persistence ─────────────────────────────────────────────────────


def _build_settings_defaults(default_workspace: Path | str) -> dict:
    """Build defaults whose path value is resolved by the compatibility facade."""
    return {
        "default_workspace": str(default_workspace),
        "onboarding_completed": False,
        "send_key": "enter",  # 'enter', 'ctrl+enter', or 'shift+enter'
        "show_token_usage": False,  # show input/output token badge below assistant messages
        "show_quota_chip": False,  # show ambient provider quota chip in composer footer (default off; wide desktop only when enabled, see style.css @media)
        "show_conversation_outline": False,  # show opt-in desktop jump-to-question outline panel
        "show_busy_placeholder_hint": False,  # opt-in busy composer placeholder hint
        "hide_empty_state_suggestions": False,  # hide the default new-chat suggestion buttons
        "new_chat_on_workspace_switch": False,  # #5473 opt-in: switching to a DIFFERENT workspace starts a new chat (leaving the current conversation on its original workspace) instead of mutating the current session's workspace in place. Default OFF preserves the shipped in-place-switch behavior.
        "virtualize_transcript": False,  # #4343: virtualize long (>80 msg) transcripts. EXPERIMENTAL, opt-IN (default OFF). Was opt-out/default-on in #4325 but caused scroll-up flicker on long sessions with tall tool-call rows (variable-height anchor oscillation) — flipped off for everyone in #4343; re-enabling requires an explicit opt-in (see virtualize_transcript_optin migration in load_settings).
        "virtualize_transcript_optin": False,  # #4343 migration marker: True only once the user explicitly enables virtualize_transcript AFTER the default-off flip. A stored virtualize_transcript=True WITHOUT this marker is a stale pre-flip value and is reset to False on load (force-off-for-everyone migration).
        "show_tps": False,  # show tokens-per-second chip in assistant message headers
        "fade_text_effect": False,  # animate newly streamed words with a lightweight fade-in effect
        "show_cli_sessions": True,  # merge CLI/TUI/messaging sessions from state.db into the sidebar by default (#3988); established installs are grandfathered OFF by the load_settings backfill
        "show_claude_code_sessions": True,  # allow filtering Claude Code rows without hiding other imported sources
        "show_cron_sessions": False,  # surface cron sessions in the sidebar (subordinate to show_cli_sessions)
        "show_webhook_sessions": False,  # surface webhook sessions in the sidebar (subordinate to show_cli_sessions)
        "show_previous_messaging_sessions": False,  # show older Telegram/Discord/etc. reset segments
        "sync_to_insights": False,  # mirror WebUI token usage to state.db for /insights
        "check_for_updates": True,  # check if webui/agent repos are behind upstream
        "update_channel": "stable",  # stable | experimental — which release stream to track (stable = soaked/promoted; experimental = every batch)
        "ignore_agent_updates": False,  # keep WebUI update notices but suppress Agent update checks
        "whats_new_summary_enabled": False,  # show an LLM-written What's New summary before diff links
        "tts_enabled": False,
        "tts_auto_read": False,
        "tts_engine": "browser",
        "tts_voice": "",
        "tts_rate": 1.0,
        "tts_pitch": 1.0,
        "voice_mode_button": False,
        "voice_continuous": False,
        "voice_silence_ms": 1800,
        "raw_audio_mode": False,
        "theme": "dark",  # light | dark | system
        "skin": "default",  # accent color skin: default | ares | mono | graphite | slate | poseidon | sisyphus | charizard | sienna | catppuccin | nous
        "font_size": "default",  # small | default | large | xlarge
        "session_jump_buttons": False,  # show Start/End transcript jump pills
        "render_user_markdown": False,  # opt-in: render full markdown in user messages (#3870)
        "large_text_paste_as_attachment": True,  # convert very large composer text pastes into .md attachments by default
        "project_quick_create_buttons": False,  # opt-in: show per-project "+" quick-create buttons on sidebar project chips (#4676)
        "structured_code_default_view": "auto",  # JSON/YAML fenced-block default render: auto | on | off (#484 follow-up). auto => Tree when line count >= structured_code_auto_tree_lines, else Raw.
        "structured_code_auto_tree_lines": 10,  # in 'auto' mode, minimum line count to default a JSON/YAML block to Tree view (preserves the original hardcoded >=10 behavior)
        "session_endless_scroll": False,  # auto-load older transcript pages while scrolling upward
        "chat_activity_display_mode": "compact_worklog",  # compact_worklog | transparent_stream | hide_all_activity
        "transparent_stream_event_timestamps": True,  # show per-event timestamp chips inside Transparent Stream
        "auto_scroll_follow": True,  # follow new output to the bottom while streaming (Codex/Claude-Code-style sticky bottom); the user scrolling up unpins and is respected
        "worklog_details_expanded_default": False,  # opt-in: expand Worklog details by default; default remains folded
        "hide_composer_attach": False,  # hide attach button in composer footer
        "hide_composer_saved_prompts": False,  # hide saved prompts button in composer footer
        "hide_composer_mic": False,  # hide dictation mic button in composer footer
        "show_titlebar_profile": False,  # show profile switcher in app titlebar (opt-in)
        "hide_composer_voice_mode": False,  # hide hands-free voice-mode button in composer footer
        "hide_composer_yolo": False,  # hide YOLO chip in composer footer
        "hide_composer_profile": False,  # hide profile chip in composer footer
        "hide_composer_workspace": False,  # hide workspace controls in composer footer/mobile config panel
        "hide_composer_mobile_config": False,  # hide mobile composer config button
        "hide_composer_model": False,  # hide model chip in composer footer/mobile config panel
        "hide_composer_quota_chip": False,  # hide provider quota chip in composer footer
        "hide_composer_reasoning": False,  # hide reasoning chip in composer footer/mobile config panel
        "hide_composer_toolsets": False,  # hide toolsets chip in composer footer
        "hide_composer_status": False,  # hide status text in composer footer
        "hide_composer_context": False,  # hide context indicator in composer footer/mobile config panel
        "hide_composer_bg_badge": False,  # hide background-jobs badge in composer footer
        "pinned_sessions_limit": 3,  # maximum active pinned sessions shown in the sidebar
        "inflight_state_max_sessions": 8,  # max active-stream recovery snapshots kept in browser localStorage
        "inflight_state_max_messages": 24,  # max recent messages kept per recovery snapshot
        "inflight_state_max_tool_calls": 48,  # max recent tool-call records kept per recovery snapshot
        "inflight_state_max_string_chars": 60000,  # max string length kept inside a recovery snapshot field
        "inflight_state_max_json_chars": 1500000,  # max serialized recovery snapshot payload before pruning
        "hidden_tabs": [],  # sidebar tab panel names hidden by user (e.g. ["tasks","kanban"]); chat and settings are always visible
        "tab_order": [],  # user-defined sidebar/rail tab order for reorderable tabs; chat/settings stay fixed
        "composer_control_order": [],  # user-defined composer footer control order; invalid/duplicate keys are ignored
        "language": "en",  # UI locale code; must match a key in static/i18n.js LOCALES
        "bot_name": os.getenv(
            "HERMES_WEBUI_BOT_NAME", "Hermes"
        ),  # display name for the assistant
        "sound_enabled": False,  # play notification sound when assistant finishes
        "rtl": False,  # right-to-left chat layout (chat messages + composer only)
        "notifications_enabled": False,  # browser notification when tab is in background
        "show_thinking": True,  # show/hide thinking/reasoning blocks in chat view
        "simplified_tool_calling": True,  # legacy compatibility; Worklog renderer remains enabled
        "terminal_auto_expand_on_output": False,  # auto-expand terminal panel when output arrives while collapsed
        "workspace_todos_tab": False,  # show a Todos tab in the workspace panel (right side)
        "api_redact_enabled": True,  # redact sensitive data (API keys, secrets) from API responses
        "dashboard_plugins": {},  # plugin_name -> bool, opt-in per plugin (default off per PF-10b)
        "sidebar_density": "compact",  # compact | detailed
        "auto_title_refresh_every": "0",  # adaptive title refresh: 0=off, 5/10/20=every N exchanges
        "default_message_mode": "steer",  # behavior when sending while agent is running: queue | interrupt | steer
        "password_hash": None,  # PBKDF2-HMAC-SHA256 hash; None = auth disabled
        "auth_disabled_acknowledged": False,  # user acknowledged unauthenticated risk
        "provider_cost_budget": None,
    }


# Schema template used to derive static allowlists without resolving the facade
# during import. The facade owns the authoritative mutable defaults dictionary,
# rebuilt with the current workspace on every import/reload.
_SETTINGS_SCHEMA_DEFAULTS = _build_settings_defaults("")
_SETTINGS_SPEECH_KEYS = {
    "tts_enabled",
    "tts_auto_read",
    "tts_engine",
    "tts_voice",
    "tts_rate",
    "tts_pitch",
    "voice_mode_button",
    "voice_continuous",
    "voice_silence_ms",
    "raw_audio_mode",
}
_SETTINGS_PERSISTED_SPEECH_KEYS_FIELD = "persisted_speech_keys"
_SETTINGS_LEGACY_DROP_KEYS = {
    "assistant_language",
    "bubble_layout",
    "default_model",
    "activity_feed_expanded_default",
    "simplified_tool_calling",
}
_COMPOSER_CONTROL_ORDER_KEYS = {
    key for key in _SETTINGS_SCHEMA_DEFAULTS if key.startswith("hide_composer_")
}
_SETTINGS_THEME_VALUES = {"light", "dark", "system"}
_SETTINGS_SKIN_VALUES = {
    "default",
    "ares",
    "mono",
    "graphite",
    "slate",
    "poseidon",
    "sisyphus",
    "charizard",
    "sienna",
    "catppuccin",
    "nous",
    "geist-contrast",
    "zeus",
    "verdigris",
    "neon-soft",
    "neon-paint",
}
_SETTINGS_LEGACY_THEME_MAP = {
    # Legacy full themes now map onto the closest supported theme + accent skin pair.
    "slate": ("dark", "slate"),
    "solarized": ("dark", "poseidon"),
    "monokai": ("dark", "sisyphus"),
    "nord": ("dark", "slate"),
    "oled": ("dark", "default"),
}


def _normalize_appearance(theme, skin) -> tuple[str, str]:
    """Normalize a (theme, skin) pair, migrating legacy theme names.

    Legacy migration table (from `_SETTINGS_LEGACY_THEME_MAP`):

        slate     → ("dark", "slate")
        solarized → ("dark", "poseidon")
        monokai   → ("dark", "sisyphus")
        nord      → ("dark", "slate")
        oled      → ("dark", "default")

    Unknown / custom theme names fall back to ("dark", "default").  This is a
    behavior change vs. the pre-PR-#627 state, where the `theme` field was
    open-ended ("no enum gate -- allows custom themes").  Users who set a
    custom CSS theme via `data-theme` will need to re-apply via skin or
    custom CSS — see CHANGELOG entry for details.

    The same mapping is mirrored in `static/boot.js` (`_LEGACY_THEME_MAP`)
    so client and server normalize identically; keep them in sync.
    """
    raw_theme = theme.strip().lower() if isinstance(theme, str) else ""
    raw_skin = skin.strip().lower() if isinstance(skin, str) else ""
    cfg = _config_module
    legacy = cfg._SETTINGS_LEGACY_THEME_MAP.get(raw_theme)
    if legacy:
        next_theme, legacy_skin = legacy
    elif raw_theme in cfg._SETTINGS_THEME_VALUES:
        next_theme, legacy_skin = raw_theme, "default"
    else:
        # Unknown themes used to exist; default to dark so upgrades stay visually stable.
        next_theme, legacy_skin = "dark", "default"
    next_skin = raw_skin if raw_skin in cfg._SETTINGS_SKIN_VALUES else legacy_skin
    return next_theme, next_skin


def _read_raw_settings_file() -> dict:
    """Read settings.json without applying defaults."""
    cfg = _config_module
    settings_file = cfg.SETTINGS_FILE
    try:
        if not settings_file.exists():
            return {}
    except OSError:
        # PermissionError or other OS-level error (e.g. UID mismatch in Docker)
        # Treat as missing rather than failing startup.
        cfg.logger.debug("Cannot stat settings file %s (inaccessible?)", settings_file)
        return {}

    try:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception:
        cfg.logger.debug("Failed to load settings from %s", settings_file)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _extract_persisted_speech_keys(stored: dict) -> set[str]:
    if not isinstance(stored, dict):
        return set()
    return {key for key in _config_module._SETTINGS_SPEECH_KEYS if key in stored}


def persisted_speech_settings_keys() -> list[str]:
    cfg = _config_module
    return sorted(cfg._extract_persisted_speech_keys(cfg._read_raw_settings_file()))


def _settings_payload_for_write(
    settings: dict, persisted_speech_keys: set[str]
) -> dict:
    cfg = _config_module
    persisted = {
        k: v
        for k, v in settings.items()
        if k not in {"default_model", cfg._SETTINGS_PERSISTED_SPEECH_KEYS_FIELD}
    }
    for speech_key in cfg._SETTINGS_SPEECH_KEYS:
        if speech_key not in persisted_speech_keys:
            persisted.pop(speech_key, None)
    return persisted


def load_settings() -> dict:
    """Load settings from disk, merging with defaults for any missing keys."""
    cfg = _config_module
    settings = dict(cfg._SETTINGS_DEFAULTS)
    stored = cfg._read_raw_settings_file()
    if isinstance(stored, dict):
        if (
            "worklog_details_expanded_default" not in stored
            and "activity_feed_expanded_default" in stored
        ):
            settings["worklog_details_expanded_default"] = bool(
                stored.get("activity_feed_expanded_default")
            )
        settings.update(
            {
                k: v
                for k, v in stored.items()
                if k not in cfg._SETTINGS_LEGACY_DROP_KEYS
                and k != cfg._SETTINGS_PERSISTED_SPEECH_KEYS_FIELD
            }
        )
        if "default_message_mode" not in stored and "busy_input_mode" in stored:
            settings["default_message_mode"] = stored.get("busy_input_mode")
        settings.pop("busy_input_mode", None)
        # Grandfather established installs OFF for show_cli_sessions (#3988).
        # The default flipped True so NEW users see CLI/TUI/messaging
        # sessions without hunting for the toggle — but an existing user
        # who never opted in should not have their sidebar silently change.
        # Treat the install as established (and pin the old False default)
        # when show_cli_sessions is absent AND the file already carries
        # real user state — either onboarding was completed, or some
        # setting OTHER than a not-yet-completed onboarding flag has been
        # persisted. Keying on "has saved user state" (not just
        # onboarding_completed) also covers a CLI-configured user who
        # tweaked a WebUI setting before running the wizard. A genuinely
        # new / still-mid-onboarding file falls through to the True default.
        _established_keys = [
            k for k in stored if k not in ("show_cli_sessions", "onboarding_completed")
        ]
        if "show_cli_sessions" not in stored and (
            bool(stored.get("onboarding_completed")) or _established_keys
        ):
            settings["show_cli_sessions"] = False
        # Force-off-for-everyone migration for virtualize_transcript (#4343).
        # The feature shipped opt-OUT/default-on in #4325, then proved to
        # cause scroll-up flicker on long sessions (variable-height anchor
        # oscillation). It is now EXPERIMENTAL/opt-IN (default off). Any
        # stored virtualize_transcript=True from the #4325 window is a stale
        # pre-flip value and must be reset to off, so 100% of existing users
        # land on off — re-enabling requires an explicit opt-in made AFTER
        # the flip, which writes virtualize_transcript_optin=True alongside.
        # Honor a stored True only when that marker is present.
        if not bool(stored.get("virtualize_transcript_optin")):
            settings["virtualize_transcript"] = False
    settings["theme"], settings["skin"] = cfg._normalize_appearance(
        stored.get("theme") if isinstance(stored, dict) else settings.get("theme"),
        stored.get("skin") if isinstance(stored, dict) else settings.get("skin"),
    )
    settings["default_model"] = cfg.get_effective_default_model()
    try:
        model_cfg = cfg.get_config().get("model", {})
        if isinstance(model_cfg, dict) and model_cfg.get("provider"):
            settings["default_model_provider"] = str(model_cfg.get("provider"))
    except Exception:
        cfg.logger.debug("Failed to resolve default model provider for settings")
    return settings


_SETTINGS_ALLOWED_KEYS = set(_SETTINGS_SCHEMA_DEFAULTS.keys()) - {
    "password_hash",
    "default_model",
    "simplified_tool_calling",
}
_SETTINGS_ENUM_VALUES = {
    "send_key": {"enter", "ctrl+enter", "shift+enter"},
    "sidebar_density": {"compact", "detailed"},
    "update_channel": {"stable", "experimental"},
    "font_size": {"small", "default", "large", "xlarge"},
    "auto_title_refresh_every": {"0", "5", "10", "20"},
    "default_message_mode": {"queue", "interrupt", "steer"},
    "chat_activity_display_mode": {
        "compact_worklog",
        "transparent_stream",
        "hide_all_activity",
    },
    "structured_code_default_view": {"auto", "on", "off"},
}
_SETTINGS_INT_RANGES = {
    "pinned_sessions_limit": (1, 99),
    "inflight_state_max_sessions": (1, 25),
    "inflight_state_max_messages": (1, 100),
    "inflight_state_max_tool_calls": (1, 200),
    "inflight_state_max_string_chars": (1000, 500000),
    "inflight_state_max_json_chars": (100000, 4000000),
    "structured_code_auto_tree_lines": (1, 1000),
    "voice_silence_ms": (200, 60000),
}
_SETTINGS_FLOAT_RANGES = {
    "tts_rate": (0.5, 2.0),
    "tts_pitch": (0.0, 2.0),
}
_SETTINGS_BOOL_KEYS = {
    "onboarding_completed",
    "show_token_usage",
    "show_quota_chip",
    "show_conversation_outline",
    "show_busy_placeholder_hint",
    "hide_empty_state_suggestions",
    "new_chat_on_workspace_switch",
    "virtualize_transcript",
    "virtualize_transcript_optin",
    "show_tps",
    "fade_text_effect",
    "show_cli_sessions",
    "show_claude_code_sessions",
    "show_cron_sessions",
    "show_webhook_sessions",
    "show_previous_messaging_sessions",
    "sync_to_insights",
    "check_for_updates",
    "ignore_agent_updates",
    "whats_new_summary_enabled",
    "tts_enabled",
    "tts_auto_read",
    "voice_mode_button",
    "voice_continuous",
    "raw_audio_mode",
    "sound_enabled",
    "rtl",
    "notifications_enabled",
    "show_thinking",
    "terminal_auto_expand_on_output",
    "workspace_todos_tab",
    "api_redact_enabled",
    "session_jump_buttons",
    "render_user_markdown",
    "large_text_paste_as_attachment",
    "project_quick_create_buttons",
    "session_endless_scroll",
    "transparent_stream_event_timestamps",
    "auto_scroll_follow",
    "worklog_details_expanded_default",
    "auth_disabled_acknowledged",
    "hide_composer_attach",
    "hide_composer_saved_prompts",
    "hide_composer_mic",
    "show_titlebar_profile",
    "hide_composer_voice_mode",
    "hide_composer_yolo",
    "hide_composer_profile",
    "hide_composer_workspace",
    "hide_composer_mobile_config",
    "hide_composer_model",
    "hide_composer_quota_chip",
    "hide_composer_reasoning",
    "hide_composer_toolsets",
    "hide_composer_status",
    "hide_composer_context",
    "hide_composer_bg_badge",
}
# Language codes are validated as short alphanumeric BCP-47-like tags (e.g. 'en', 'zh', 'fr')
_SETTINGS_LANG_RE = re.compile(r"^[a-zA-Z]{2,10}(-[a-zA-Z0-9]{2,8})?$")
_SETTINGS_TTS_ENGINE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _atomic_write_settings_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + fsync + os.replace).

    ``settings.json`` was rewritten with a plain ``Path.write_text``, which
    truncates the file in place: a crash or full disk mid-write leaves it
    truncated/empty, so the next start loses every persisted setting (theme,
    workspace, tab order, and the login ``password_hash``). Writing to a
    sibling temp file, fsyncing, then ``os.replace`` keeps the old contents
    intact until the rename commits the new ones in one step.  Mirrors the
    tempfile+fsync+os.replace pattern already used by
    ``webui_session_db.WebUIJsonSessionDB._atomic_write``.

    The existing file's mode is carried onto the replacement: ``os.replace``
    swaps in the temp file's inode, and a plain ``open`` respects the umask
    (typically 0644), so without this an operator-hardened ``settings.json``
    (chmod 0600 because it holds the password hash) would be silently loosened
    on the next save.  New files fall back to the umask-adjusted default.

    A symlinked target is written through to its referent (same follow-through
    as the ``Path.write_text`` this replaces), rather than replacing the link
    itself with a regular file.
    """
    path = Path(path)
    write_path = path.resolve(strict=False) if path.is_symlink() else path
    tmp = write_path.with_name(
        f".{write_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        mode = os.stat(write_path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o666 & ~_config_module._current_umask()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, write_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _current_umask() -> int:
    """Read the process umask without leaving it changed.

    ``os.umask`` has no read-only form (it sets and returns the prior value),
    so we set-then-restore. Called only on the new-``settings.json`` path, which
    is rare; the tiny set-to-0 window is acceptable here (unlike a per-write
    hot path).
    """
    umask = os.umask(0)
    os.umask(umask)
    return umask


def _coerce_provider_cost_budget(value: Any) -> float | None:
    """Normalize a monthly budget to the persisted two-decimal representation."""
    try:
        rounded = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    if not (0 < rounded < 1e9) or not math.isfinite(rounded):
        return None
    return rounded


def save_settings(settings: dict) -> dict:
    """Save settings to disk. Returns the merged settings. Ignores unknown keys."""
    cfg = _config_module
    raw_settings = cfg._read_raw_settings_file()
    persisted_speech_keys = cfg._extract_persisted_speech_keys(raw_settings)
    current = cfg.load_settings()
    applied_speech_keys: set[str] = set()
    if (
        "worklog_details_expanded_default" not in settings
        and "activity_feed_expanded_default" in settings
    ):
        settings["worklog_details_expanded_default"] = settings.get(
            "activity_feed_expanded_default"
        )
    settings.pop("activity_feed_expanded_default", None)
    if "default_message_mode" not in settings and "busy_input_mode" in settings:
        settings["default_message_mode"] = settings.get("busy_input_mode")
    settings.pop("busy_input_mode", None)
    settings.pop("simplified_tool_calling", None)
    pending_theme = current.get("theme")
    pending_skin = current.get("skin")
    theme_was_explicit = False
    skin_was_explicit = False
    # Handle _set_password: hash and store as password_hash
    _password_changed = False
    raw_pw = settings.pop("_set_password", None)
    if raw_pw and isinstance(raw_pw, str) and raw_pw.strip():
        # Use PBKDF2 from auth module (600k iterations) -- never raw SHA-256
        from api.auth import hash_password

        current["password_hash"] = hash_password(raw_pw.strip())
        _password_changed = True
    # Handle _clear_password: explicitly disable auth
    if settings.pop("_clear_password", False):
        current["password_hash"] = None
        _password_changed = True
    # Deep-merge dashboard_plugins dict (plugin_name -> bool)
    _dashboard_plugins = settings.get("dashboard_plugins")
    if isinstance(_dashboard_plugins, dict):
        current_dash = current.get("dashboard_plugins", {})
        if isinstance(current_dash, dict):
            # Coerce values to bool + keep only str keys so settings.json can't be
            # polluted with non-bool/non-str junk from a crafted POST.
            current_dash.update(
                {
                    k: bool(v)
                    for k, v in _dashboard_plugins.items()
                    if isinstance(k, str)
                }
            )
            current["dashboard_plugins"] = current_dash
    for k, v in settings.items():
        key_is_speech = k in cfg._SETTINGS_SPEECH_KEYS
        # dashboard_plugins is deep-merged above (not a flat allowlisted scalar).
        if k == "dashboard_plugins":
            continue
        if k in cfg._SETTINGS_ALLOWED_KEYS:
            if k == "theme":
                if isinstance(v, str) and v.strip():
                    pending_theme = v
                    theme_was_explicit = True
                continue
            if k == "skin":
                if isinstance(v, str) and v.strip():
                    pending_skin = v
                    skin_was_explicit = True
                continue
            # Validate enum-constrained keys
            if k in cfg._SETTINGS_ENUM_VALUES and v not in cfg._SETTINGS_ENUM_VALUES[k]:
                continue
            # Validate bounded integer settings.
            if k in cfg._SETTINGS_INT_RANGES:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                min_value, max_value = cfg._SETTINGS_INT_RANGES[k]
                if v < min_value or v > max_value:
                    continue
            if k in cfg._SETTINGS_FLOAT_RANGES:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                min_value, max_value = cfg._SETTINGS_FLOAT_RANGES[k]
                if not math.isfinite(v) or v < min_value or v > max_value:
                    continue
            if k == "tts_engine":
                if not isinstance(v, str):
                    continue
                v = v.strip()
                if not cfg._SETTINGS_TTS_ENGINE_RE.match(v):
                    continue
            if k == "tts_voice":
                if not isinstance(v, str) or len(v) > 200 or "\x00" in v:
                    continue
            # Validate language codes (BCP-47-like: 'en', 'zh', 'fr', 'zh-CN')
            if k == "language" and (
                not isinstance(v, str) or not cfg._SETTINGS_LANG_RE.match(v)
            ):
                continue
            # Validate list-valued ordering settings. Chat/settings stay fixed
            # for tabs; composer ordering only accepts known control keys.
            # Duplicates are collapsed while preserving the first requested order.
            if k in {"hidden_tabs", "tab_order", "composer_control_order"}:
                if not isinstance(v, list):
                    continue
                seen = set()
                cleaned = []
                for s in v:
                    if not isinstance(s, str):
                        continue
                    s = s.strip()
                    if not s or s in seen:
                        continue
                    if k in {"hidden_tabs", "tab_order"} and s in {"chat", "settings"}:
                        continue
                    if (
                        k == "composer_control_order"
                        and s not in cfg._COMPOSER_CONTROL_ORDER_KEYS
                    ):
                        continue
                    seen.add(s)
                    cleaned.append(s)
                v = cleaned
            if k == "provider_cost_budget":
                if v is None or v == "":
                    current[k] = None
                    continue
                budget = cfg._coerce_provider_cost_budget(v)
                if budget is None:
                    continue
                current[k] = budget
                continue
            # Coerce bool keys
            if k in cfg._SETTINGS_BOOL_KEYS:
                v = bool(v)
            current[k] = v
            if key_is_speech:
                applied_speech_keys.add(k)
    theme_value = pending_theme
    skin_value = pending_skin
    if theme_was_explicit and not skin_was_explicit:
        raw_theme = (
            pending_theme.strip().lower() if isinstance(pending_theme, str) else ""
        )
        if raw_theme not in cfg._SETTINGS_THEME_VALUES:
            skin_value = None
    current["theme"], current["skin"] = cfg._normalize_appearance(
        theme_value, skin_value
    )

    current["default_workspace"] = str(
        cfg.resolve_default_workspace(current.get("default_workspace"))
    )
    effective_persisted_speech_keys = set(persisted_speech_keys)
    effective_persisted_speech_keys.update(applied_speech_keys)
    persisted = cfg._settings_payload_for_write(
        current, effective_persisted_speech_keys
    )
    cfg.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg._atomic_write_settings_text(
        cfg.SETTINGS_FILE,
        json.dumps(persisted, ensure_ascii=False, indent=2),
    )
    with cfg._SETTINGS_WRITE_LOCK:
        cfg._SETTINGS_WRITE_VERSION += 1
    # Invalidate the in-memory password hash cache so the next call to
    # get_password_hash() picks up the new value from disk immediately.
    if _password_changed:
        from api.auth import invalidate_password_hash_cache

        invalidate_password_hash_cache()
    # Update runtime defaults so new sessions use them immediately
    if "default_workspace" in current:
        cfg.DEFAULT_WORKSPACE = cfg.resolve_default_workspace(
            current["default_workspace"]
        )
    current["default_model"] = cfg.get_effective_default_model()
    return current


def _apply_startup_settings() -> None:
    """Apply persisted workspace settings after the facade is fully populated.

    An explicit ``HERMES_WEBUI_DEFAULT_WORKSPACE`` remains authoritative over
    persisted state so Docker operators always retain an environment override.
    """
    cfg = _config_module
    startup_settings = cfg.load_settings()
    try:
        settings_file_exists = cfg.SETTINGS_FILE.exists()
    except OSError:
        settings_file_exists = False
    if not settings_file_exists:
        return
    if not os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE"):
        cfg.DEFAULT_WORKSPACE = cfg.resolve_default_workspace(
            startup_settings.get("default_workspace")
        )
    # Always drop stale value; the model comes from config.yaml.
    startup_settings.pop("default_model", None)
    if startup_settings.get("default_workspace") == str(cfg.DEFAULT_WORKSPACE):
        return
    startup_settings["default_workspace"] = str(cfg.DEFAULT_WORKSPACE)
    try:
        startup_persisted_speech_keys = cfg._extract_persisted_speech_keys(
            cfg._read_raw_settings_file()
        )
        cfg._atomic_write_settings_text(
            cfg.SETTINGS_FILE,
            json.dumps(
                cfg._settings_payload_for_write(
                    startup_settings, startup_persisted_speech_keys
                ),
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception:
        pass
