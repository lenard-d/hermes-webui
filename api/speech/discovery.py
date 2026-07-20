"""Passive speech-to-text provider discovery without installation side effects."""

from __future__ import annotations

import os


def transcription_provider_capability_from_module(stt) -> tuple[bool, str]:
    try:
        load_config = getattr(stt, "_load_stt_config", None)
        stt_config = load_config() if callable(load_config) else {}
        config = stt_config if isinstance(stt_config, dict) else {}
        is_enabled = getattr(stt, "is_stt_enabled", None)
        if callable(is_enabled) and not is_enabled(stt_config):
            return False, "none"

        has_internal_flags = any(
            hasattr(stt, name)
            for name in ("_HAS_FASTER_WHISPER", "_HAS_OPENAI", "_HAS_MISTRAL")
        )
        get_provider = getattr(stt, "_get_provider", None)
        if callable(get_provider) and not has_internal_flags:
            provider = str(get_provider(stt_config) or "none")
            return provider not in ("", "none"), provider or "none"

        def env(name):
            getter = getattr(stt, "get_env_value", None)
            try:
                if callable(getter):
                    return str(getter(name) or "").strip()
            except Exception:
                return ""
            return os.getenv(name, "").strip()

        def available(helper_name):
            helper = getattr(stt, helper_name, None)
            try:
                return bool(helper()) if callable(helper) else False
            except Exception:
                return False

        def local_command_available():
            return available("_has_local_command") and available("_find_ffmpeg_binary")

        def command_provider_available(provider):
            resolver = getattr(stt, "_resolve_command_stt_provider_config", None)
            try:
                return callable(resolver) and resolver(provider, config) is not None
            except Exception:
                return False

        def resolve(provider):
            if provider == "local":
                if bool(getattr(stt, "_HAS_FASTER_WHISPER", False)):
                    return "local"
                return "local_command" if local_command_available() else "none"
            if provider == "local_command":
                if local_command_available():
                    return "local_command"
                return "local" if bool(getattr(stt, "_HAS_FASTER_WHISPER", False)) else "none"
            if provider == "groq":
                return "groq" if bool(getattr(stt, "_HAS_OPENAI", False)) and bool(env("GROQ_API_KEY")) else "none"
            if provider == "openai":
                has_audio = available("_has_openai_audio_backend")
                return "openai" if bool(getattr(stt, "_HAS_OPENAI", False)) and has_audio else "none"
            if provider == "mistral":
                return "mistral" if bool(getattr(stt, "_HAS_MISTRAL", False)) and bool(env("MISTRAL_API_KEY")) else "none"
            if provider == "xai":
                try:
                    from tools.xai_http import resolve_xai_http_credentials

                    return "xai" if resolve_xai_http_credentials().get("api_key") else "none"
                except Exception:
                    return "none"
            if provider == "elevenlabs":
                return "elevenlabs" if bool(env("ELEVENLABS_API_KEY")) else "none"
            return provider if command_provider_available(provider) else "none"

        if "provider" in config:
            configured = str(config.get("provider") or "local")
            provider = resolve(configured)
            return provider != "none", provider if provider != "none" else configured

        for candidate in (
            "local",
            "local_command",
            "groq",
            "openai",
            "mistral",
            "xai",
            "elevenlabs",
        ):
            provider = resolve(candidate)
            if provider != "none":
                return True, provider
        return False, "none"
    except Exception:
        return False, "none"


def discover_transcription_provider() -> tuple[bool, str]:
    try:
        import tools.transcription_tools as stt
    except ImportError:
        return False, "none"
    return transcription_provider_capability_from_module(stt)
