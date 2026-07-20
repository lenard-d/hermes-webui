"""Static provider credential metadata shared by profile and provider layers."""

_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "x-ai": "XAI_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "neuralwatt": "NEURALWATT_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
    "lmstudio": "LM_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

_PROVIDER_ENV_VAR_ALIASES: dict[str, tuple[str, ...]] = {
    "lmstudio": ("LMSTUDIO_API_KEY",),
    "opencode-zen": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_API_KEY",),
}

_SELF_HOSTED_PROVIDER_IDS = frozenset({"ollama", "lmstudio"})
_OAUTH_PROVIDERS = frozenset(
    {"copilot", "copilot-acp", "nous", "openai-codex", "qwen-oauth", "xai-oauth"}
)


def provider_credential_env_vars() -> tuple[str, ...]:
    names = {name for name in _PROVIDER_ENV_VAR.values() if name}
    for aliases in _PROVIDER_ENV_VAR_ALIASES.values():
        names.update(alias for alias in aliases if alias)
    return tuple(sorted(names))


_PROVIDER_CREDENTIAL_ENV_VARS = provider_credential_env_vars()
