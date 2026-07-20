"""Profile-scoped TTS provider selection and credential resolution."""

from __future__ import annotations

from dataclasses import dataclass
import os

from .errors import InvalidSpeechRequest, SpeechUnavailable
from .transport import normalize_openai_tts_base_url
from .validation import validate_voice_id


@dataclass(frozen=True)
class EdgeTtsProvider:
    engine: str = "edge"


@dataclass(frozen=True)
class ElevenLabsTtsProvider:
    api_key: str
    voice_id: str
    model_id: str
    engine: str = "elevenlabs"


@dataclass(frozen=True)
class OpenAITtsProvider:
    api_key: str
    base_url: str
    model: str
    voice: str
    engine: str = "openai"


TtsProvider = EdgeTtsProvider | ElevenLabsTtsProvider | OpenAITtsProvider


def _active_profile_snapshot() -> tuple[dict, dict[str, str]]:
    """Resolve config and dotenv against one active profile home."""
    from api import config as config_module
    from api import onboarding as onboarding_module
    from api import profiles as profiles_module

    home = profiles_module.get_active_hermes_home()
    try:
        config = config_module.get_config_for_profile_home(home) or {}
    except Exception:
        config = {}
    if not isinstance(config, dict):
        config = {}
    try:
        env = onboarding_module._load_env_file(home / ".env")
    except Exception:
        env = {}
    return config, env if isinstance(env, dict) else {}


def _tts_config(config: dict, engine: str) -> dict:
    tts = config.get("tts", {})
    if not isinstance(tts, dict):
        return {}
    provider = tts.get(engine, {})
    return provider if isinstance(provider, dict) else {}


def resolve_tts_provider(engine: str) -> TtsProvider:
    normalized_engine = str(engine or "edge").strip().lower()
    if normalized_engine not in {"elevenlabs", "openai"}:
        return EdgeTtsProvider()

    config, profile_env = _active_profile_snapshot()
    provider_config = _tts_config(config, normalized_engine)

    if normalized_engine == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        api_key = api_key or str(profile_env.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            raise SpeechUnavailable("ELEVENLABS_API_KEY not configured")
        voice_id = validate_voice_id(
            provider_config.get("voice_id", "pNInz6obpgDQGcFmaJgB")
        )
        model_id = provider_config.get("model", "eleven_multilingual_v2") or (
            provider_config.get("model_id", "eleven_multilingual_v2")
        )
        return ElevenLabsTtsProvider(
            api_key=api_key,
            voice_id=voice_id,
            model_id=str(model_id),
        )

    api_key = os.getenv("VOICE_TOOLS_OPENAI_KEY", "").strip()
    api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
    api_key = api_key or str(profile_env.get("VOICE_TOOLS_OPENAI_KEY") or "").strip()
    api_key = api_key or str(profile_env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SpeechUnavailable("OpenAI API key not configured")
    try:
        base_url = normalize_openai_tts_base_url(
            provider_config.get("base_url") or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        raise InvalidSpeechRequest("invalid OpenAI base_url in config") from exc
    return OpenAITtsProvider(
        api_key=api_key,
        base_url=base_url,
        model=str(provider_config.get("model") or "gpt-4o-mini-tts"),
        voice=str(provider_config.get("voice") or "alloy"),
    )
