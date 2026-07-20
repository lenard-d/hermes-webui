"""Provider metadata and pure normalization helpers for onboarding."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from api.config import FALLBACK_MODELS, PROVIDER_MODELS


SUPPORTED_PROVIDER_SETUPS = {
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.6",
        "requires_base_url": False,
        "models": [
            {"id": model["id"], "label": model["label"]}
            for model in FALLBACK_MODELS
        ],
        "category": "easy_start",
        "quick": True,
    },
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4.6",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("anthropic", [])),
        "category": "easy_start",
        "oauth_provider": "anthropic",
        "oauth_label": "Claude Code OAuth",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "default_base_url": "https://api.openai.com/v1",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("openai", [])),
        "category": "easy_start",
    },
    "ollama": {
        "label": "Ollama",
        "env_var": "OLLAMA_API_KEY",
        "default_model": "qwen3:32b",
        "default_base_url": "http://localhost:11434/v1",
        "requires_base_url": True,
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    "lmstudio": {
        "label": "LM Studio",
        "env_var": "LM_API_KEY",
        "env_var_aliases": ["LMSTUDIO_API_KEY"],
        "default_model": "gpt-4o-mini",
        "default_base_url": "http://localhost:1234/v1",
        "requires_base_url": True,
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "requires_base_url": True,
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    "gemini": {
        "label": "Google Gemini",
        "env_var": "GOOGLE_API_KEY",
        "default_model": "gemini-3.1-pro-preview",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("google", [])),
        "category": "specialized",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",
        "default_base_url": "https://api.deepseek.com",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("deepseek", [])),
        "category": "specialized",
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "env_var": "XIAOMI_API_KEY",
        "default_model": "mimo-v2.5-pro",
        "default_base_url": "https://api.xiaomimimo.com/v1",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("xiaomi", [])),
        "category": "specialized",
    },
    "zai": {
        "label": "Z.AI / GLM (智谱)",
        "env_var": "GLM_API_KEY",
        "default_model": "glm-5.1",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("zai", [])),
        "category": "specialized",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "env_var": "NVIDIA_API_KEY",
        "default_model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "default_base_url": "https://integrate.api.nvidia.com/v1",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("nvidia", [])),
        "category": "specialized",
    },
    "mistralai": {
        "label": "Mistral",
        "env_var": "MISTRAL_API_KEY",
        "default_model": "mistral-large-latest",
        "default_base_url": "https://api.mistral.ai/v1",
        "requires_base_url": False,
        "models": list(PROVIDER_MODELS.get("mistralai", [])),
        "category": "specialized",
    },
    "x-ai": {
        "label": "xAI (Grok)",
        "env_var": "XAI_API_KEY",
        "default_model": "grok-4.20",
        "default_base_url": "https://api.x.ai/v1",
        "requires_base_url": False,
        "models": list(
            PROVIDER_MODELS.get("xai", [])
            or PROVIDER_MODELS.get("x-ai", [])
        ),
        "category": "specialized",
    },
}

PROVIDER_CATEGORIES = (
    {"id": "easy_start", "label": "Easy start", "order": 0},
    {"id": "self_hosted", "label": "Open / self-hosted", "order": 1},
    {"id": "specialized", "label": "Specialized", "order": 2},
)

UNSUPPORTED_PROVIDER_NOTE = (
    "Advanced provider flows such as Nous Portal and GitHub Copilot are still "
    "terminal-first. OpenAI Codex and Anthropic Claude Code can be authenticated "
    "in this onboarding flow when your Hermes config selects the corresponding provider."
)


def normalize_model_for_provider(provider: str, model: str) -> str:
    clean = (model or "").strip()
    if provider in {"anthropic", "openai"} and clean.startswith(provider + "/"):
        return clean.split("/", 1)[1]
    return clean


def normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def extract_current_provider(config: dict) -> str:
    model = config.get("model", {})
    if isinstance(model, dict):
        return str(model.get("provider") or "").strip().lower()
    return ""


def extract_current_model(config: dict) -> str:
    model = config.get("model", {})
    if isinstance(model, str):
        return model.strip()
    if isinstance(model, dict):
        return str(model.get("default") or "").strip()
    return ""


def extract_current_base_url(config: dict) -> str:
    model = config.get("model", {})
    if isinstance(model, dict):
        return normalize_base_url(str(model.get("base_url") or ""))
    return ""


def build_setup_catalog(
    config: dict,
    *,
    hermes_home: Path,
    oauth_authenticated: Callable[[str, Path], bool],
) -> dict:
    current_provider = extract_current_provider(config) or "openrouter"
    current_model = extract_current_model(config)
    current_base_url = extract_current_base_url(config)

    providers = [
        {
            "id": provider_id,
            "label": meta["label"],
            "env_var": meta["env_var"],
            "default_model": meta["default_model"],
            "default_base_url": meta.get("default_base_url") or "",
            "requires_base_url": bool(meta.get("requires_base_url")),
            "key_optional": bool(meta.get("key_optional")),
            "models": list(meta.get("models", [])),
            "category": meta.get("category", "easy_start"),
            "quick": meta.get("quick", False),
            "oauth_provider": meta.get("oauth_provider") or "",
            "oauth_label": meta.get("oauth_label") or "",
        }
        for provider_id, meta in SUPPORTED_PROVIDER_SETUPS.items()
    ]
    category_order = {item["id"]: item["order"] for item in PROVIDER_CATEGORIES}
    providers.sort(
        key=lambda item: (category_order.get(item["category"], 99), item["label"])
    )
    categories = [
        {
            "id": category["id"],
            "label": category["label"],
            "providers": [
                item["id"]
                for item in providers
                if item["category"] == category["id"]
            ],
        }
        for category in sorted(PROVIDER_CATEGORIES, key=lambda item: item["order"])
    ]
    current_is_oauth = (
        current_provider not in SUPPORTED_PROVIDER_SETUPS and bool(current_provider)
    ) or oauth_authenticated(current_provider, hermes_home)

    return {
        "providers": providers,
        "categories": categories,
        "unsupported_note": UNSUPPORTED_PROVIDER_NOTE,
        "current_is_oauth": current_is_oauth,
        "current": {
            "provider": current_provider,
            "model": current_model
            or SUPPORTED_PROVIDER_SETUPS.get(current_provider, {}).get(
                "default_model", ""
            ),
            "base_url": current_base_url,
        },
    }
