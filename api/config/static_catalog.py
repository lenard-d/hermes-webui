"""Static provider names, aliases, and fallback model catalogs."""

from __future__ import annotations

# Hardcoded fallback models (used when no config.yaml or agent is available)
# Also used as the OpenRouter model list — keep this curated to current, widely-used models.
FALLBACK_MODELS = [
    # OpenAI
    {"provider": "OpenAI",    "id": "openai/gpt-5.4-mini",                "label": "GPT-5.4 Mini"},
    {"provider": "OpenAI",    "id": "openai/gpt-5.4",                     "label": "GPT-5.4"},
    # Anthropic — 4.6 flagship + 4.5 generation
    {"provider": "Anthropic", "id": "anthropic/claude-opus-4.7",          "label": "Claude Opus 4.7"},
    {"provider": "Anthropic", "id": "anthropic/claude-opus-4.6",          "label": "Claude Opus 4.6"},
    {"provider": "Anthropic", "id": "anthropic/claude-sonnet-4.6",        "label": "Claude Sonnet 4.6"},
    {"provider": "Anthropic", "id": "anthropic/claude-sonnet-4-5",        "label": "Claude Sonnet 4.5"},
    {"provider": "Anthropic", "id": "anthropic/claude-haiku-4-5",         "label": "Claude Haiku 4.5"},
    # Google — 3.x (latest preview) + 2.5 (stable GA)
    {"provider": "Google",    "id": "google/gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
    {"provider": "Google",    "id": "google/gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
    {"provider": "Google",    "id": "google/gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
    {"provider": "Google",    "id": "google/gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
    {"provider": "Google",    "id": "google/gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
    # DeepSeek
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-v4-flash",          "label": "DeepSeek V4 Flash"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-v4-pro",            "label": "DeepSeek V4 Pro"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-chat-v3-0324",      "label": "DeepSeek V3 (legacy)"},
    {"provider": "DeepSeek",  "id": "deepseek/deepseek-r1",                "label": "DeepSeek R1 (legacy)"},
    # Qwen (Alibaba) — strong coding and general models
    {"provider": "Qwen",      "id": "qwen/qwen3-coder",                   "label": "Qwen3 Coder"},
    {"provider": "Qwen",      "id": "qwen/qwen3.6-plus",                  "label": "Qwen3.6 Plus"},
    # xAI
    {"provider": "xAI",       "id": "x-ai/grok-4.20",                    "label": "Grok 4.20"},
    # Mistral
    {"provider": "Mistral",   "id": "mistralai/mistral-large-latest",     "label": "Mistral Large"},
    # MiniMax
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M3",               "label": "MiniMax M3"},
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7",             "label": "MiniMax M2.7"},
    {"provider": "MiniMax",   "id": "minimax/MiniMax-M2.7-highspeed",   "label": "MiniMax M2.7 Highspeed"},
    # Z.AI / GLM
    {"provider": "Z.AI",      "id": "zai/glm-5.2",                      "label": "GLM-5.2"},
    {"provider": "Z.AI",      "id": "zai/glm-5.1",                      "label": "GLM-5.1"},
    {"provider": "Z.AI",      "id": "zai/glm-5",                        "label": "GLM-5"},
    {"provider": "Z.AI",      "id": "zai/glm-5-turbo",                  "label": "GLM-5 Turbo"},
    {"provider": "Z.AI",      "id": "zai/glm-4.7",                      "label": "GLM-4.7"},
    {"provider": "Z.AI",      "id": "zai/glm-4.5",                      "label": "GLM-4.5"},
    {"provider": "Z.AI",      "id": "zai/glm-4.5-flash",                "label": "GLM-4.5 Flash"},
    # OpenRouter free-tier models — must appear in fallback list so they
    # are visible even when the tool-support filter in hermes_cli strips
    # them out of the live catalog (see #1426).
    {"provider": "OpenRouter", "id": "openrouter/elephant-alpha",                   "label": "Elephant Alpha (free)"},
    {"provider": "OpenRouter", "id": "openrouter/owl-alpha",                        "label": "Owl Alpha (free)"},
    {"provider": "OpenRouter", "id": "tencent/hy3-preview:free",                    "label": "Hy3 Preview (free)"},
    {"provider": "OpenRouter", "id": "nvidia/nemotron-3-super-120b-a12b:free",      "label": "Nemotron 3 Super (free)"},
    {"provider": "OpenRouter", "id": "arcee-ai/trinity-large-preview:free",         "label": "Trinity Large Preview (free)"},
]

# Provider display names for known Hermes provider IDs
PROVIDER_DISPLAY = {
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai-api": "OpenAI API",
    "openai-codex": "OpenAI Codex",
    "xai-oauth": "xAI Grok OAuth",
    "copilot": "GitHub Copilot",
    "moa": "Mixture of Agents",
    "cursor-acp": "Cursor ACP",
    "zai": "Z.AI / GLM",
    "kimi-coding": "Kimi / Moonshot",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
    "minimax-cn": "MiniMax (China)",
    "google": "Google",
    "meta-llama": "Meta Llama",
    "huggingface": "HuggingFace",
    "alibaba": "Alibaba",
    "ollama": "Ollama",
    "ollama-cloud": "Ollama Cloud",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "lmstudio": "LM Studio",
    "mistralai": "Mistral",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "nvidia": "NVIDIA NIM",
    "xiaomi": "Xiaomi",
    "bedrock": "AWS Bedrock",
}

# Provider alias → canonical slug.  Users configure providers using the
# dotted/hyphenated form they see on the provider website (``z.ai``,
# ``x.ai``, ``google``) but the internal ``PROVIDER_MODELS`` catalog
# uses slugs without punctuation (``zai``, ``xai``, ``gemini``).  Without
# normalisation the provider lands in the ``else`` branch of the group
# builder and no models are returned — the bug behind #815.
#
# This table is authoritative for the WebUI.  When ``hermes_cli.models``
# is importable we also merge its aliases on top so any
# new aliases added to the agent automatically apply.  Keeping the local
# copy means the fix works even in environments where the agent tree is
# not on ``sys.path`` (CI, installs without hermes-agent cloned
# alongside the WebUI).
PROVIDER_ALIASES = {
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "github": "copilot",
    "github-copilot": "copilot",
    "github-models": "copilot",
    "github-model": "copilot",
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "deep-seek": "deepseek",
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",
    "opencode": "opencode-zen",
    "grok": "xai",
    "x-ai": "xai",
    "x.ai": "xai",
    "aws": "bedrock",
    "aws-bedrock": "bedrock",
    "amazon": "bedrock",
    "amazon-bedrock": "bedrock",
    "qwen": "alibaba",
    "aliyun": "alibaba",
    "dashscope": "alibaba",
    "alibaba-cloud": "alibaba",
    "nim": "nvidia",
    "nvidia-nim": "nvidia",
    "build-nvidia": "nvidia",
    "nemotron": "nvidia",
    "mimo": "xiaomi",
    "xiaomi-mimo": "xiaomi",
    # Legacy alias — earlier WebUI builds wrote ``provider: local`` for unknown
    # loopback endpoints, but ``local`` is not registered in
    # ``hermes_cli.auth.PROVIDER_REGISTRY``. Routing it through ``custom``
    # lets the agent's auxiliary client take the ``no-key-required``
    # OpenAI-compat path. See #1384.
    "local": "custom",
}

# Well-known models per provider (used to populate dropdown for direct API providers)
PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4.7", "label": "Claude Opus 4.7"},
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5.5",      "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4",      "label": "GPT-5.4"},
    ],
    "openai-api": [
        {"id": "gpt-5.5",      "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4",      "label": "GPT-5.4"},
    ],
    "openai-codex": [
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
        {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
        {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        {"id": "codex-mini-latest", "label": "Codex Mini (latest)"},
    ],
    "google": [
        {"id": "gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
    ],
    "deepseek": [
        {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
        {"id": "deepseek-chat-v3-0324", "label": "DeepSeek V3 (legacy)"},
        {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner (legacy)"},
    ],
    "nous": [
        {"id": "@nous:anthropic/claude-opus-4.6",     "label": "Claude Opus 4.6 (via Nous)"},
        {"id": "@nous:anthropic/claude-sonnet-4.6",   "label": "Claude Sonnet 4.6 (via Nous)"},
        {"id": "@nous:openai/gpt-5.4-mini",           "label": "GPT-5.4 Mini (via Nous)"},
        {"id": "@nous:google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview (via Nous)"},
    ],
    "zai": [
        {"id": "glm-5.2", "label": "GLM-5.2"},
        {"id": "glm-5.1", "label": "GLM-5.1"},
        {"id": "glm-5", "label": "GLM-5"},
        {"id": "glm-5-turbo", "label": "GLM-5 Turbo"},
        {"id": "glm-4.7", "label": "GLM-4.7"},
        {"id": "glm-4.5", "label": "GLM-4.5"},
        {"id": "glm-4.5-flash", "label": "GLM-4.5 Flash"},
    ],
    "kimi-coding": [
        {"id": "moonshot-v1-8k", "label": "Moonshot v1 8k"},
        {"id": "moonshot-v1-32k", "label": "Moonshot v1 32k"},
        {"id": "moonshot-v1-128k", "label": "Moonshot v1 128k"},
        {"id": "kimi-latest", "label": "Kimi Latest"},
        {"id": "kimi-k2.5", "label": "Kimi K2.5"},
    ],
    "minimax": [
        {"id": "MiniMax-M3", "label": "MiniMax M3"},
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
        {"id": "MiniMax-M2.7-highspeed", "label": "MiniMax M2.7 Highspeed"},
    ],
    "minimax-cn": [
        {"id": "MiniMax-M3", "label": "MiniMax M3"},
        {"id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
    ],
    # GitHub Copilot — model IDs served via the Copilot API
    # Fallback ONLY — the live GitHub Copilot catalog
    # (hermes_cli.models.provider_model_ids("copilot")) is authoritative and is
    # tried first by _read_live_provider_model_ids(). This static list is the
    # safety net shown when the live probe fails (cold start / token blip). Keep
    # it in sync with the real integrator allowlist so a probe miss never renders
    # legacy junk (GPT-4o / gpt-3.5-turbo). Last synced 2026-06-30 from the live
    # copilot-4-cli catalog (16 models).
    "copilot": [
        {"id": "claude-opus-4.8", "label": "Claude Opus 4.8"},
        {"id": "claude-opus-4.7", "label": "Claude Opus 4.7"},
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4.5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4.5", "label": "Claude Haiku 4.5"},
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5-mini", "label": "GPT-5 Mini"},
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "mai-code-1-flash-picker", "label": "MAI Code 1 Flash"},
        {"id": "gpt-4o", "label": "GPT-4o"},
    ],
    # Cursor ACP — models served via Cursor CLI agent acp
    "cursor-acp": [
        {"id": "cursor/composer-2.5", "label": "Composer 2.5"},
        {"id": "cursor/composer-2", "label": "Composer 2"},
        {"id": "cursor/default", "label": "Default"},
        {"id": "cursor-acp", "label": "Cursor ACP"},
    ],
    # OpenCode Zen — curated models via opencode.ai/zen (pay-as-you-go credits)
    "opencode-zen": [
        {"id": "gpt-5.4-pro", "label": "GPT-5.4 Pro"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"id": "gpt-5.4-nano", "label": "GPT-5.4 Nano"},
        {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"},
        {"id": "gpt-5.2", "label": "GPT-5.2"},
        {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
        {"id": "gpt-5.1", "label": "GPT-5.1"},
        {"id": "gpt-5.1-codex", "label": "GPT-5.1 Codex"},
        {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
        {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        {"id": "gpt-5", "label": "GPT-5"},
        {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
        {"id": "gpt-5-nano", "label": "GPT-5 Nano"},
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
        {"id": "claude-opus-4-6", "label": "Claude Opus 4.6"},
        {"id": "claude-opus-4-5", "label": "Claude Opus 4.5"},
        {"id": "claude-opus-4-1", "label": "Claude Opus 4.1"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        {"id": "claude-3-5-haiku", "label": "Claude 3.5 Haiku"},
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {"id": "glm-5.1", "label": "GLM-5.1"},
        {"id": "glm-5", "label": "GLM-5"},
        {"id": "kimi-k2.5", "label": "Kimi K2.5"},
        {"id": "minimax-m2.5", "label": "MiniMax M2.5"},
        {"id": "minimax-m2.5-free", "label": "MiniMax M2.5 Free"},
        {"id": "nemotron-3-super-free", "label": "Nemotron 3 Super Free"},
        {"id": "big-pickle", "label": "Big Pickle"},
    ],
    # OpenCode Go — flat-rate models via opencode.ai/go ($10/month).
    # Synced 2026-07-08 from the public Go docs and documented models endpoint.
    # Keep preview/free-only Zen models out of this Go picker snapshot.
    "opencode-go": [
        {"id": "minimax-m3",       "label": "MiniMax M3"},
        {"id": "minimax-m2.7",     "label": "MiniMax M2.7"},
        {"id": "minimax-m2.5",     "label": "MiniMax M2.5"},
        {"id": "kimi-k2.7-code",   "label": "Kimi K2.7 Code"},
        {"id": "kimi-k2.6",        "label": "Kimi K2.6"},
        {"id": "kimi-k2.5",        "label": "Kimi K2.5"},
        {"id": "glm-5.2",          "label": "GLM-5.2"},
        {"id": "glm-5.1",          "label": "GLM-5.1"},
        {"id": "glm-5",            "label": "GLM-5"},
        {"id": "deepseek-v4-pro",  "label": "DeepSeek V4 Pro"},
        {"id": "deepseek-v4-flash","label": "DeepSeek V4 Flash"},
        {"id": "qwen3.7-max",      "label": "Qwen3.7 Max"},
        {"id": "qwen3.7-plus",     "label": "Qwen3.7 Plus"},
        {"id": "qwen3.6-plus",     "label": "Qwen3.6 Plus"},
        {"id": "qwen3.5-plus",     "label": "Qwen3.5 Plus"},
        {"id": "mimo-v2-pro",      "label": "MiMo V2 Pro"},
        {"id": "mimo-v2-omni",     "label": "MiMo V2 Omni"},
        {"id": "mimo-v2.5-pro",    "label": "MiMo V2.5 Pro"},
        {"id": "mimo-v2.5",        "label": "MiMo V2.5"},
    ],
    # 'gemini' is the hermes_cli provider ID for Google AI Studio
    # Model IDs are bare — sent directly to:
    #   https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
    "gemini": [
        {"id": "gemini-3.1-pro-preview",            "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3-flash-preview",            "label": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite-preview",     "label": "Gemini 3.1 Flash Lite Preview"},
        {"id": "gemini-2.5-pro",                    "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash",                  "label": "Gemini 2.5 Flash"},
    ],
    # Mistral — prefix used in OpenRouter model IDs (mistralai/mistral-large-latest)
    "mistralai": [
        {"id": "mistral-large-latest", "label": "Mistral Large"},
        {"id": "mistral-small-latest", "label": "Mistral Small"},
    ],
    # Qwen (Alibaba) — prefix used in OpenRouter model IDs (qwen/qwen3-coder)
    "qwen": [
        {"id": "qwen3-coder",   "label": "Qwen3 Coder"},
        {"id": "qwen3.6-plus",  "label": "Qwen3.6 Plus"},
    ],
    # NVIDIA NIM — NVIDIA's inference platform
    "nvidia": [
        {"id": "nvidia/nemotron-3-super-120b-a12b", "label": "Nemotron 3 Super 120B"},
        {"id": "nvidia/nemotron-3-nano-30b-a3b", "label": "Nemotron 3 Nano 30B"},
        {"id": "nvidia/llama-3.3-nemotron-super-49b-v1.5", "label": "Llama 3.3 Nemotron Super 49B"},
        {"id": "qwen/qwen3-next-80b-a3b-instruct", "label": "Qwen3 Next 80B"},
    ],
    # Xiaomi MiMo — direct API via api.xiaomimimo.com
    "xiaomi": [
        {"id": "mimo-v2.5-pro",    "label": "MiMo V2.5 Pro"},
        {"id": "mimo-v2.5",        "label": "MiMo V2.5"},
        {"id": "mimo-v2-pro",      "label": "MiMo V2 Pro"},
        {"id": "mimo-v2-omni",     "label": "MiMo V2 Omni"},
        {"id": "mimo-v2-flash",    "label": "MiMo V2 Flash"},
    ],
    # xAI — prefix used in OpenRouter model IDs (x-ai/grok-4-20)
    "x-ai": [
        {"id": "grok-4.20", "label": "Grok 4.20"},
    ],
    "xai-oauth": [
        {"id": "grok-4.20", "label": "Grok 4.20"},
    ],
    # AWS Bedrock — static fallback list; live model list is fetched via
    # hermes_cli.models.provider_model_ids("bedrock") when available (#2720).
    "bedrock": [
        {"id": "global.anthropic.claude-opus-4-7",                 "label": "Global Anthropic Claude Opus 4.7"},
        {"id": "global.anthropic.claude-opus-4-6-v1",              "label": "Global Anthropic Claude Opus 4.6"},
        {"id": "global.anthropic.claude-sonnet-4-6",               "label": "Global Anthropic Claude Sonnet 4.6"},
        {"id": "global.anthropic.claude-opus-4-5-20251101-v1:0",   "label": "GLOBAL Anthropic Claude Opus 4.5"},
        {"id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0", "label": "Global Claude Sonnet 4.5"},
        {"id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",  "label": "Global Anthropic Claude Haiku 4.5"},
    ],
}
