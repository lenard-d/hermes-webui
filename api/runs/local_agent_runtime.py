"""Provider resolution and agent construction for a local browser turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from api.config import (
    main_model_request_overrides,
    model_with_provider_context,
    warm_models_catalog_provenance_if_cold,
)

from .local_agent_cache import acquire_local_agent
from .local_agent_config import build_local_agent_configuration
from .runtime_resolution import (
    _resolve_custom_provider_runtime_overrides,
    _runtime_preferred_base_url,
)
from .webui_prefill import _public_prefill_context_status


@dataclass(frozen=True)
class LocalAgentRequest:
    session: object
    session_id: str
    model: str
    provider: str | None
    profile_home: str
    profile_name: str | None
    ephemeral: bool
    callbacks: object
    clarify_callback: object
    publish: object
    get_ai_agent: object
    resolve_model_provider: object
    build_session_db: object
    load_prefill_context: object
    build_prefill_messages: object
    normalize_prefill_messages: object


@dataclass
class PreparedLocalAgent:
    """The complete provider/config/cache decision for one local turn."""

    agent: object
    agent_class: type
    signature: str
    session_db: object
    state_db_path: Path | None
    model: str
    provider: str | None
    base_url: str | None
    configured_base_url: str | None
    api_key: str | None
    runtime: dict
    config: dict
    parameters: set[str]
    kwargs: dict
    prefill_context: object

    @classmethod
    def prepare(
        cls,
        request: LocalAgentRequest,
        *,
        logger: logging.Logger,
    ) -> "PreparedLocalAgent":
        agent_class = request.get_ai_agent()
        if agent_class is None:
            from .terminal_outcomes import _aiagent_import_error_detail

            raise ImportError(_aiagent_import_error_detail())

        state_db_path = (
            Path(request.profile_home) / "state.db" if request.profile_home else None
        )
        session_db = request.build_session_db(state_db_path)
        model, provider, base_url = _resolve_catalog_model(request, logger=logger)
        configured_base_url = base_url
        runtime, provider, api_key, base_url = _resolve_runtime_provider(
            model=model,
            provider=provider,
            configured_base_url=configured_base_url,
        )
        provider, api_key, base_url = _resolve_custom_provider_runtime_overrides(
            provider,
            api_key,
            base_url,
        )

        config = _profile_config(request.profile_home)
        prefill_context = request.load_prefill_context(config)
        prefill_messages = request.normalize_prefill_messages(
            request.build_prefill_messages(prefill_context, config)
        )
        request_overrides = main_model_request_overrides(
            config,
            effective_model=model,
            effective_provider=provider,
        )
        request.publish(
            "context_status",
            {
                "session_id": request.session_id,
                "prefill": _public_prefill_context_status(prefill_context),
            },
        )
        toolsets = _toolsets_for_session(
            config,
            session_id=request.session_id,
            logger=logger,
        )
        configuration = build_local_agent_configuration(
            agent_class=agent_class,
            config=config,
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            toolsets=toolsets,
            session_id=request.session_id,
            session_db=session_db,
            prefill_messages=prefill_messages,
            callbacks=request.callbacks,
            clarify_callback=request.clarify_callback,
            runtime=runtime,
            request_overrides=request_overrides,
        )
        cached = acquire_local_agent(
            agent_class=agent_class,
            session_id=request.session_id,
            ephemeral=request.ephemeral,
            kwargs=configuration.kwargs,
            session_db=session_db,
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            runtime=runtime,
            max_iterations=configuration.max_iterations,
            max_tokens=configuration.max_tokens,
            fallback_models=configuration.fallback_models,
            toolsets=toolsets,
            reasoning=configuration.reasoning,
            request_overrides=request_overrides,
            prefill_status=_public_prefill_context_status(prefill_context),
            profile_home=request.profile_home,
        )
        return cls(
            agent=cached.agent,
            agent_class=agent_class,
            signature=cached.signature,
            session_db=cached.session_db,
            state_db_path=state_db_path,
            model=model,
            provider=provider,
            base_url=base_url,
            configured_base_url=configured_base_url,
            api_key=api_key,
            runtime=runtime,
            config=config,
            parameters=configuration.parameters,
            kwargs=configuration.kwargs,
            prefill_context=prefill_context,
        )


def _resolve_catalog_model(
    request: LocalAgentRequest,
    *,
    logger: logging.Logger,
) -> tuple[str, str | None, str | None]:
    from api import profiles as profiles_api
    from api.sessions import model_explicit_pick_signature

    picked_signature = getattr(request.session, "model_explicit_pick_signature", None)
    selected_model = getattr(request.session, "model", None) or request.model
    selected_provider = (
        getattr(request.session, "model_provider", None) or request.provider
    )
    current_signature = model_explicit_pick_signature(
        selected_model,
        selected_provider,
    )
    explicitly_picked = bool(picked_signature) and picked_signature == current_signature
    with profiles_api.profile_scope_for_detached_worker(
        request.profile_name,
        "model resolution",
        logger_override=logger,
    ):
        warm_models_catalog_provenance_if_cold()
        return request.resolve_model_provider(
            model_with_provider_context(request.model, request.provider),
            explicitly_picked=explicitly_picked,
        )


def _resolve_runtime_provider(
    *,
    model: str,
    provider: str | None,
    configured_base_url: str | None,
) -> tuple[dict, str | None, str | None, str | None]:
    runtime: dict = {}
    api_key = None
    base_url = configured_base_url
    try:
        from api.auth import resolve_runtime_provider_with_anthropic_env_lock
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider,
            requested=provider,
            target_model=model,
        )
        api_key = runtime.get("api_key")
        provider = provider or runtime.get("provider")
        base_url = _runtime_preferred_base_url(
            runtime,
            provider,
            configured_base_url,
        )
    except Exception as exc:
        print(f"[webui] WARNING: resolve_runtime_provider failed: {exc}", flush=True)
    return runtime, provider, api_key, base_url


def _profile_config(profile_home: str) -> dict:
    try:
        from api.config import get_config_for_profile_home

        return get_config_for_profile_home(profile_home)
    except Exception:
        from api.config import get_config

        return get_config()


def _toolsets_for_session(config: dict, *, session_id: str, logger: logging.Logger):
    from api.config import resolve_cli_toolsets

    toolsets = resolve_cli_toolsets(config)
    try:
        from api.sessions import SESSION_DIR, Session

        session_path = SESSION_DIR / f"{session_id}.json"
        if session_path.exists():
            metadata = Session.load_metadata_only(session_id)
            override = getattr(metadata, "enabled_toolsets", None) if metadata else None
            if override:
                toolsets = override
    except Exception as exc:
        logger.warning(
            "Failed to read per-session toolsets for %s: %s",
            session_id,
            exc,
        )
    return toolsets
