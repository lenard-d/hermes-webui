"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    _handle_session_import_cli = ctx["_handle_session_import_cli"]
    bad = ctx["bad"]
    ensure_agent_runtime_current = ctx["ensure_agent_runtime_current"]
    j = ctx["j"]
    logger = ctx["logger"]
    require_ai_agent_class = ctx["require_ai_agent_class"]
    uuid = ctx["uuid"]

    if parsed.path == "/api/updates/apply":
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        # Honor an explicit validated body channel (the client sends the channel
        # the banner was offering) so a channel switch whose debounced autosave
        # hasn't landed can't make apply read the OLD saved channel (Codex gate).
        # Fall back to the saved setting when absent/invalid.
        _apply_channel = body.get("channel") if isinstance(body, dict) else None
        if _apply_channel not in ("stable", "experimental"):
            _apply_channel = None
        from api.updates import apply_update

        return j(handler, apply_update(target, _apply_channel))

    if parsed.path == "/api/updates/force":
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        _force_channel = body.get("channel") if isinstance(body, dict) else None
        if _force_channel not in ("stable", "experimental"):
            _force_channel = None
        from api.updates import apply_force_update

        return j(handler, apply_force_update(target, _force_channel))

    if parsed.path == "/api/updates/clear_lock":
        # Manual-instruction recovery for the .git/index.lock case. The
        # endpoint NEVER removes a lock file from the server -- it returns
        # the diagnostic + the exact 'rm' command for the operator, and on
        # a re-click with the lock already gone, it re-runs the normal
        # non-destructive apply path. See apply_clear_lock for the v2.2
        # design rationale (round-2 gate cert: fcntl-flock cannot detect
        # git's O_CREAT|O_EXCL locks, so any auto-delete path races).
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        from api.updates import apply_clear_lock

        return j(handler, apply_clear_lock(target))

    if parsed.path == "/api/updates/summary":
        from api.updates import summarize_update_payload

        updates = body.get("updates") if isinstance(body, dict) else {}
        target = body.get("target") if isinstance(body, dict) else None

        def _llm_update_summary(system_prompt: str, user_prompt: str) -> str:
            from api import profiles as profiles_api

            active_profile = profiles_api.get_active_profile_name() or "default"

            with profiles_api.profile_env_for_background_worker(
                active_profile,
                "update summary",
                logger_override=logger,
            ):
                from api.config import (
                    get_effective_default_model,
                    resolve_model_provider,
                    resolve_custom_provider_connection,
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]

                _main_model, _main_provider, _main_base_url = resolve_model_provider(
                    get_effective_default_model()
                )
                _main_api_key = None
                try:
                    from api.auth import (
                        resolve_runtime_provider_with_anthropic_env_lock,
                    )
                    from hermes_cli.runtime_provider import resolve_runtime_provider

                    _rt = resolve_runtime_provider_with_anthropic_env_lock(
                        resolve_runtime_provider,
                        requested=_main_provider,
                    )
                    _main_api_key = _rt.get("api_key")
                    if not _main_provider:
                        _main_provider = _rt.get("provider")
                    if not _main_base_url:
                        _main_base_url = _rt.get("base_url")
                except Exception as _e:
                    logger.debug(
                        "update summary runtime provider resolution failed: %s", _e
                    )
                if isinstance(_main_provider, str) and _main_provider.startswith(
                    "custom:"
                ):
                    _cp_key, _cp_base = resolve_custom_provider_connection(
                        _main_provider
                    )
                    if not _main_api_key and _cp_key:
                        _main_api_key = _cp_key
                    if not _main_base_url and _cp_base:
                        _main_base_url = _cp_base

                main_runtime = {
                    "provider": _main_provider,
                    "model": _main_model,
                    "base_url": _main_base_url,
                    "api_key": _main_api_key,
                }

                ensure_agent_runtime_current()
                try:
                    from agent.auxiliary_client import get_text_auxiliary_client

                    # Update summaries are a short text-compression/summarization task.
                    # Reuse the documented auxiliary.compression slot instead of
                    # inventing a WebUI-only auxiliary task name that users cannot
                    # discover in the Hermes Agent setup/config UI.
                    aux_client, aux_model = get_text_auxiliary_client(
                        "compression",
                        main_runtime=main_runtime,
                    )
                    if aux_client is not None and aux_model:
                        response = aux_client.chat.completions.create(
                            model=aux_model,
                            messages=messages,
                        )
                        return str(response.choices[0].message.content or "").strip()
                except Exception as _e:
                    logger.debug(
                        "update summary auxiliary model failed; falling back to main model: %s",
                        _e,
                    )

                AIAgent = require_ai_agent_class()

                agent = AIAgent(
                    model=_main_model,
                    provider=_main_provider,
                    base_url=_main_base_url,
                    api_key=_main_api_key,
                    platform="webui",
                    quiet_mode=True,
                    enabled_toolsets=[],
                    session_id=f"updates-summary-{uuid.uuid4().hex[:8]}",
                )
                result = agent.run_conversation(
                    user_message=user_prompt,
                    system_message=system_prompt,
                    conversation_history=[],
                    task_id=f"updates-summary-{uuid.uuid4().hex[:8]}",
                )
                return str(result.get("final_response") or "").strip()

        return j(
            handler,
            summarize_update_payload(
                updates, llm_callback=_llm_update_summary, target=target
            ),
        )

    # ── CLI session import (POST) ──
    if parsed.path == "/api/session/import_cli":
        return _handle_session_import_cli(handler, body)
    return UNHANDLED
