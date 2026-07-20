"""WebUI prefill loading, budgeting, status, and turn-boundary normalization."""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType
from typing import Optional


SECRET_SHAPED_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s]+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b|"
    r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
)
PREFILL_SCRIPT_OUTPUT_LIMIT = 262_144
PREFILL_CONTEXT_DEFAULT_MAX_CHARS = 12_000


def redact_prefill_status_text(api: ModuleType, text: str) -> str:
    """Return a short, non-secret diagnostic string for prefill status."""
    clean = api._SECRET_SHAPED_RE.sub("[REDACTED]", str(text or ""))
    return " ".join(clean.split())[:240]


def valid_prefill_messages(api: ModuleType, value) -> list[dict]:
    """Normalize a prefill payload to role/content messages."""
    if not isinstance(value, list):
        return []
    messages: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    return messages


def resolve_prefill_path(api: ModuleType, raw: str) -> Path:
    path = api.Path(str(raw)).expanduser()
    if not path.is_absolute():
        try:
            from api.config import _get_config_path
            path = _get_config_path().parent / path
        except Exception:
            path = api.Path.cwd() / path
    return path


def prefill_context_max_chars(api: ModuleType, config_data: dict) -> int:
    raw = api.os.getenv("HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS", "") or str(
        config_data.get("webui_prefill_context_max_chars") or ""
    )
    try:
        value = int(raw or api._PREFILL_CONTEXT_DEFAULT_MAX_CHARS)
    except Exception:
        value = api._PREFILL_CONTEXT_DEFAULT_MAX_CHARS
    return max(0, min(value, api._PREFILL_SCRIPT_OUTPUT_LIMIT))


def prefill_context_char_count(api: ModuleType, messages: list[dict]) -> int:
    return sum(
        len(str(message.get("content") or ""))
        for message in messages
        if isinstance(message, dict)
    )


def budget_compacted_prefill_context(
    api: ModuleType,
    context: dict,
    *,
    max_chars: int,
    char_count: int,
) -> dict:
    label = str(context.get("label") or "prefill context")
    message = (
        "A configured WebUI startup prefill source was available, but it exceeded "
        f"the WebUI prefill context budget ({char_count} chars > {max_chars} chars), "
        "so the note/body payload was omitted from this new chat. If the user's "
        "request depends on prior decisions, durable notes, runbooks, current "
        "context, or open issues, use the available retrieval/search/note tools "
        "to fetch only the relevant details before answering."
    )
    return {
        "status": "loaded",
        "source": "budget_compacted",
        "label": label,
        "messages": [{"role": "user", "content": message}],
        "message_count": 1,
        "compacted": True,
        "original_source": context.get("source", ""),
        "original_message_count": int(context.get("message_count") or 0),
        "original_char_count": char_count,
        "max_chars": max_chars,
    }


def apply_prefill_context_budget(api: ModuleType, context: dict, config_data: dict) -> dict:
    if context.get("status") != "loaded":
        return context
    max_chars = api._prefill_context_max_chars(config_data)
    if max_chars <= 0:
        return context
    messages = context.get("messages") or []
    char_count = api._prefill_context_char_count(
        messages if isinstance(messages, list) else []
    )
    if char_count <= max_chars:
        return context

    file_raw = api.os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or str(
        config_data.get("prefill_messages_file") or ""
    )
    if context.get("source") == "script" and file_raw:
        fallback = api._load_prefill_messages_file(file_raw, source="file_budget_fallback")
        fallback_messages = fallback.get("messages") if isinstance(fallback, dict) else []
        fallback_chars = api._prefill_context_char_count(
            fallback_messages if isinstance(fallback_messages, list) else []
        )
        if fallback.get("status") == "loaded" and fallback_chars <= max_chars:
            fallback["compacted"] = True
            fallback["original_source"] = context.get("source", "")
            fallback["original_label"] = context.get("label", "")
            fallback["original_message_count"] = int(context.get("message_count") or 0)
            fallback["original_char_count"] = char_count
            fallback["max_chars"] = max_chars
            return fallback

    return api._budget_compacted_prefill_context(
        context,
        max_chars=max_chars,
        char_count=char_count,
    )


def prefill_not_configured(api: ModuleType) -> dict:
    return {
        "status": "not_configured",
        "source": "none",
        "label": "",
        "messages": [],
        "message_count": 0,
    }


def load_prefill_messages_file(
    api: ModuleType,
    file_raw: str,
    *,
    source: str = "file",
    status: str = "loaded",
) -> dict:
    path = api._resolve_prefill_path(file_raw)
    label = path.name or "prefill file"
    if not path.exists():
        return {
            "status": "error",
            "source": source,
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": "prefill file not found",
        }
    try:
        messages = api._valid_prefill_messages(
            api.json.loads(path.read_text(encoding="utf-8"))
        )
        return {
            "status": status,
            "source": source,
            "label": label,
            "messages": messages,
            "message_count": len(messages),
        }
    except Exception as exc:
        return {
            "status": "error",
            "source": source,
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": api._redact_prefill_status_text(str(exc)),
        }


def prefill_script_timeout(api: ModuleType, config_data: dict) -> float:
    raw = api.os.getenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT", "") or str(
        config_data.get("webui_prefill_messages_script_timeout") or ""
    )
    try:
        return max(0.1, min(float(raw or 5), 30.0))
    except Exception:
        return 5.0


def prefill_script_command(api: ModuleType, raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw if str(part)]
    parts = api.shlex.split(str(raw or ""))
    if not parts:
        return []
    # A single script path mirrors prefill_messages_file path resolution.  More
    # complex commands keep their argv untouched so admins can pass arguments.
    if len(parts) == 1:
        parts[0] = str(api._resolve_prefill_path(parts[0]))
    return parts


def messages_from_prefill_script_output(api: ModuleType, text: str) -> list[dict]:
    stripped = str(text or "").strip()
    if not stripped:
        return []
    try:
        payload = api.json.loads(stripped)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        payload = payload.get("messages")
    messages = api._valid_prefill_messages(payload)
    if messages:
        return messages
    return [{"role": "user", "content": stripped}]


def load_prefill_messages_script(api: ModuleType, config_data: dict) -> dict:
    script_raw = api.os.getenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", "") or config_data.get(
        "webui_prefill_messages_script"
    )
    if not script_raw:
        return api._prefill_not_configured()
    command = api._prefill_script_command(script_raw)
    label = api.Path(command[0]).name if command else "prefill script"
    if not command:
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": "prefill script is empty",
        }
    try:
        proc = api.subprocess.run(
            command,
            text=True,
            stdout=api.subprocess.PIPE,
            stderr=api.subprocess.PIPE,
            timeout=api._prefill_script_timeout(config_data),
            check=False,
        )
    except api.subprocess.TimeoutExpired:
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": "prefill script timed out",
        }
    except Exception as exc:
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": api._redact_prefill_status_text(str(exc)),
        }
    if proc.returncode != 0:
        err = api._redact_prefill_status_text(
            proc.stderr or proc.stdout or f"prefill script exited {proc.returncode}"
        )
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": err,
        }
    if len(proc.stdout.encode("utf-8")) > api._PREFILL_SCRIPT_OUTPUT_LIMIT:
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": f"prefill script output exceeded {api._PREFILL_SCRIPT_OUTPUT_LIMIT} bytes",
        }
    messages = api._messages_from_prefill_script_output(proc.stdout)
    return {
        "status": "loaded",
        "source": "script",
        "label": label,
        "messages": messages,
        "message_count": len(messages),
    }


def load_webui_prefill_context(
    api: ModuleType,
    config_data: Optional[dict] = None,
) -> dict:
    """Load configured WebUI session prefill messages.

    Supports the same bounded JSON-file shape used by Hermes Agent.  WebUI also
    supports its own explicitly opt-in script hook so admins can bridge Joplin,
    Obsidian, Notion, llm-wiki, or another local notes source into ephemeral
    turn context without baking any one note provider into the WebUI.
    """
    cfg = config_data if isinstance(config_data, dict) else api.get_config()
    script_context = api._load_prefill_messages_script(cfg)
    file_raw = api.os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or str(
        cfg.get("prefill_messages_file") or ""
    )
    if script_context.get("status") == "not_configured":
        if file_raw:
            return api._apply_prefill_context_budget(
                api._load_prefill_messages_file(file_raw),
                cfg,
            )
        return api._prefill_not_configured()
    if script_context.get("status") == "error" and file_raw:
        file_context = api._load_prefill_messages_file(file_raw, source="file_fallback")
        if file_context.get("status") == "loaded":
            file_context["script_error"] = script_context.get("error", "")
            return api._apply_prefill_context_budget(file_context, cfg)
    return api._apply_prefill_context_budget(script_context, cfg)


def public_prefill_context_status(api: ModuleType, prefill_context: dict) -> dict:
    """Strip message bodies before sending context status to the browser."""
    return {
        "status": prefill_context.get("status", "not_configured"),
        "source": prefill_context.get("source", "none"),
        "label": prefill_context.get("label", ""),
        "message_count": int(prefill_context.get("message_count") or 0),
        **({"error": prefill_context.get("error", "")} if prefill_context.get("error") else {}),
        **({"compacted": True} if prefill_context.get("compacted") else {}),
        **({"original_source": prefill_context.get("original_source", "")} if prefill_context.get("original_source") else {}),
        **({"original_message_count": int(prefill_context.get("original_message_count") or 0)} if prefill_context.get("original_message_count") else {}),
        **({"original_char_count": int(prefill_context.get("original_char_count") or 0)} if prefill_context.get("original_char_count") else {}),
        **({"max_chars": int(prefill_context.get("max_chars") or 0)} if prefill_context.get("max_chars") else {}),
    }


def webui_delivery_context_prompt(
    api: ModuleType,
    config_data: Optional[dict] = None,
) -> str:
    """Return platform/delivery context for the ephemeral system prompt.

    Connected platforms, home channels, and scheduled-task delivery hints
    are injected into the system prompt (safe for role alternation) rather
    than as a prefill ``user`` message, which strict chat templates (Mistral,
    Gemma) reject.

    NOTE: This function only covers platform/delivery info.  The session
    framing ("Source: WebUI", "Session ID", "Profile", "Workspace") is
    emitted by ``_webui_surface_context_prompt()``, which is called from
    ``_webui_ephemeral_system_prompt()`` before this helper.  If you
    refactor this area, keep that surface call in place — the two helpers
    together produce the full session context block.
    """
    cfg = config_data if isinstance(config_data, dict) else api.get_config()
    lines: list[str] = []

    display_hermes_home = None
    try:
        from hermes_constants import get_hermes_home, display_hermes_home as _dh
        display_hermes_home = _dh
    except Exception:
        get_hermes_home = None  # type: ignore[assignment]

    connected = ["local (files on this machine)"]
    try:
        if get_hermes_home is not None:
            state_path = get_hermes_home() / "gateway_state.json"
            if state_path.exists():
                raw_state = api.json.loads(state_path.read_text(encoding="utf-8"))
                platforms = raw_state.get("platforms") if isinstance(raw_state, dict) else {}
                if isinstance(platforms, dict):
                    for name in sorted(platforms):
                        pdata = platforms.get(name) or {}
                        if isinstance(pdata, dict) and pdata.get("state") == "connected" and name != "local":
                            connected.append(f"{name}: Connected ✓")
    except Exception:
        pass
    lines.append(f"**Connected Platforms:** {', '.join(connected)}")

    home_channels = {}
    try:
        platforms_cfg = cfg.get("platforms", {}) if isinstance(cfg, dict) else {}
        if isinstance(platforms_cfg, dict):
            for name, pdata in platforms_cfg.items():
                if not isinstance(pdata, dict):
                    continue
                if pdata.get("enabled") is False:
                    continue
                home = pdata.get("home_channel")
                if isinstance(home, dict):
                    home_channels[str(name)] = str(home.get("name") or name)
    except Exception:
        home_channels = {}

    if home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, label in sorted(home_channels.items()):
            lines.append(f"  - {platform}: {label}")

    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")
    lines.append("- `\"origin\"` → Back to this WebUI/browser session when the WebUI runtime supports origin delivery; otherwise prefer an explicit platform target.")
    try:
        home_display = display_hermes_home() if display_hermes_home else "~/.hermes"
    except Exception:
        home_display = "~/.hermes"
    lines.append(f"- `\"local\"` → Save to local files only ({home_display}/cron/output/)")
    for platform, label in sorted(home_channels.items()):
        lines.append(f"- `\"{platform}\"` → Home channel ({label})")
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID. Do not invent private IDs.*")

    return "\n".join(lines)


def prefill_messages_with_webui_context(
    api: ModuleType,
    prefill_context: dict,
    config_data: Optional[dict] = None,
) -> list[dict]:
    """Combine recall prefill with WebUI session context.

    The session context (connected platforms, delivery hints) is injected
    via ``_webui_ephemeral_system_prompt`` / ``ephemeral_system_prompt``
    instead of as a prefill ``user`` message.  Adding it as a user message
    creates two consecutive user turns (prefill + actual) which strict chat
    templates (Mistral, Gemma) reject with a Jinja 500.
    """
    return list(prefill_context.get("messages") or [])


def normalize_prefill_messages_before_user_turn(
    api: ModuleType,
    prefill_messages: list[dict],
) -> list[dict]:
    """Ensure WebUI prefill does not end with user role before an appended turn.

    Some upstream prefill sources can end with `role: user` (for example,
    session context or recall snippets). WebUI always appends the current user
    turn after prefill in the streaming path, so a terminal user role creates an
    adjacent user/user sequence that strict chat templates (Gemma, Mistral/Jinja)
    reject.

    To keep behavior scoped, only consecutive terminal user messages are removed
    just before that boundary; earlier roles remain untouched.
    """
    sanitized = list(prefill_messages or [])
    n_dropped = 0
    while sanitized:
        last_message = sanitized[-1]
        if not isinstance(last_message, dict):
            break
        if str(last_message.get("role") or "").strip().lower() != "user":
            break
        sanitized.pop()
        n_dropped += 1
    if n_dropped:
        api.logger.debug("Dropped %d trailing user message(s) from prefill", n_dropped)
    return sanitized
