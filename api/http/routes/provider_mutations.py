"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    _clear_live_models_cache = ctx["_clear_live_models_cache"]
    _handle_sessions_cleanup = ctx["_handle_sessions_cleanup"]
    bad = ctx["bad"]
    j = ctx["j"]
    remove_provider_key = ctx["remove_provider_key"]
    set_hermes_default_model = ctx["set_hermes_default_model"]
    set_provider_key = ctx["set_provider_key"]
    set_reasoning_display = ctx["set_reasoning_display"]
    set_reasoning_effort = ctx["set_reasoning_effort"]

    if parsed.path == "/api/default-model":
        try:
            advanced = body.get("advanced") if isinstance(body, dict) else None
            provider = body.get("provider") if isinstance(body, dict) else None
            if str(provider or "").strip().lower() == "auto":
                provider = None
            return j(
                handler,
                set_hermes_default_model(
                    body.get("model"), provider=provider, advanced=advanced
                ),
            )
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    # ── Auxiliary model set (POST) ──
    if parsed.path == "/api/model/set":
        scope = str(body.get("scope") or "").strip()
        task = str(body.get("task") or "").strip()
        provider = str(body.get("provider") or "auto").strip()
        model = str(body.get("model") or "").strip()
        advanced = body.get("advanced") if isinstance(body, dict) else None
        if scope == "auxiliary":
            from api.config import set_auxiliary_model

            try:
                return j(
                    handler,
                    set_auxiliary_model(task, provider, model, advanced=advanced),
                )
            except Exception as exc:
                return bad(handler, str(exc), status=400)
        if scope == "main":
            try:
                main_provider = provider if provider != "auto" else None
                return j(
                    handler,
                    set_hermes_default_model(
                        model, provider=main_provider, advanced=advanced
                    ),
                )
            except ValueError as exc:
                return bad(handler, str(exc), status=400)
        return bad(handler, f"unknown scope: {scope}", status=400)

    # ── Providers (POST) ──
    if parsed.path == "/api/providers":
        provider_id = (body.get("provider") or "").strip().lower()
        api_key = body.get("api_key")
        if not provider_id:
            return bad(handler, "provider is required")
        if api_key is not None:
            api_key = str(api_key).strip() or None
        result = set_provider_key(provider_id, api_key)
        if not result.get("ok"):
            return bad(handler, result.get("error", "Unknown error"))
        return j(handler, result)

    if parsed.path == "/api/providers/delete":
        provider_id = (body.get("provider") or "").strip().lower()
        if not provider_id:
            return bad(handler, "provider is required")
        result = remove_provider_key(provider_id)
        if not result.get("ok"):
            return bad(handler, result.get("error", "Unknown error"))
        return j(handler, result)

    if parsed.path == "/api/providers/self-hosted":
        try:
            from api.onboarding import apply_self_hosted_provider_setup

            return j(handler, apply_self_hosted_provider_setup(body))
        except ValueError as exc:
            return bad(handler, str(exc), 400)

    if parsed.path == "/api/models/refresh":
        provider_id = (body.get("provider") or "").strip().lower()
        if not provider_id:
            return bad(handler, "provider is required")
        from api.config import invalidate_provider_models_cache

        invalidate_provider_models_cache(provider_id)
        _clear_live_models_cache()
        return j(handler, {"ok": True, "provider": provider_id})

    if parsed.path == "/api/reasoning":
        # CLI-parity /reasoning handler — writes to the same config.yaml keys
        # the CLI uses (display.show_reasoning, agent.reasoning_effort) so a
        # preference set via WebUI is honoured in the terminal REPL and vice
        # versa.  Body is one of:
        #   {"display": "show"|"hide"|"on"|"off"}   → display.show_reasoning
        #   {"effort":  "none"|"minimal"|"low"|"medium"|"high"|"xhigh"}
        #                                            → agent.reasoning_effort
        try:
            display = body.get("display")
            effort = body.get("effort")
            if display is not None:
                flag = str(display).strip().lower()
                if flag in ("show", "on", "true", "1"):
                    return j(handler, set_reasoning_display(True))
                if flag in ("hide", "off", "false", "0"):
                    return j(handler, set_reasoning_display(False))
                return bad(
                    handler, f"display must be show|hide|on|off (got '{display}')"
                )
            if effort is not None:
                model_id = str(body.get("model") or "").strip() or None
                provider_id = str(body.get("provider") or "").strip() or None
                base_url = str(body.get("base_url") or "").strip() or None
                return j(
                    handler,
                    set_reasoning_effort(
                        effort,
                        model_id=model_id,
                        provider_id=provider_id,
                        base_url=base_url,
                    ),
                )
            return bad(handler, "reasoning: must supply 'display' or 'effort'")
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/admin/reload":
        # Hot-reload the session store to pick up code changes without restart.
        from api.sessions import reload_store_interface

        # Refresh the explicit compatibility context without importing back
        # into the legacy route facade from the HTTP implementation.
        get_session, session_type = reload_store_interface()
        ctx["get_session"] = get_session
        ctx["Session"] = session_type
        return j(handler, {"status": "ok", "reloaded": "api.sessions.store"})

    if parsed.path == "/api/sessions/cleanup":
        return _handle_sessions_cleanup(handler, body, zero_only=False)

    if parsed.path == "/api/sessions/cleanup_zero_message":
        return _handle_sessions_cleanup(handler, body, zero_only=True)
    return UNHANDLED
