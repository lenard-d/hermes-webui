"""HTTP composition and security-ordering root.

This module owns dispatch order only.  Domain route groups translate requests
to the existing domain interfaces and return ``UNHANDLED`` when their route set
does not match.
"""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.http.shell import (
    handle_health_restart,
    handle_shutdown,
    load_saved_prompts,
    save_saved_prompts,
)
from api.http.routes import (
    automation_mutations,
    automation_queries,
    auth_mutations,
    configuration_queries,
    observability_queries,
    platform_mutations,
    plugin_assets,
    profile_mutations,
    provider_mutations,
    public,
    rollback_mutations,
    session_creation_mutations,
    session_mutations,
    session_organization_mutations,
    session_queries,
    update_mutations,
    workspace_queries,
)


def _first_handled(calls):
    for call in calls:
        result = call()
        if result is not UNHANDLED:
            return result
    return UNHANDLED


def handle_get(handler, parsed, ctx: RouteContext) -> bool:
    """Dispatch GET while preserving public-route and visibility ordering."""
    proxy_result = ctx["_handle_extension_sidecar_proxy"](handler, parsed, "GET")
    if proxy_result is not False:
        return proxy_result

    result = public.handle_get(handler, parsed, ctx)
    if result is not UNHANDLED:
        return result

    if parsed.path.startswith("/api/") and not ctx["_guard_request_session_visibility"](
        handler,
        parsed,
        method="GET",
    ):
        return True

    result = _first_handled(
        (
            lambda: observability_queries.handle_get(handler, parsed, ctx),
            lambda: configuration_queries.handle_get(handler, parsed, ctx),
            lambda: session_queries.handle_get(handler, parsed, ctx),
            lambda: workspace_queries.handle_get(handler, parsed, ctx),
            lambda: automation_queries.handle_get(handler, parsed, ctx),
            lambda: plugin_assets.handle_get(handler, parsed, ctx),
        )
    )
    return False if result is UNHANDLED else result


def handle_post(handler, parsed, ctx: RouteContext) -> bool:
    """Dispatch POST while preserving CSRF, proxy, and visibility ordering."""
    diagnostics = ctx["RequestDiagnostics"]
    logger = ctx["logger"]
    j = ctx["j"]
    diag = diagnostics.maybe_start(
        "POST",
        parsed.path,
        logger=logger,
        print_fn=getattr(handler, "_safe_webui_print", None),
    )
    if parsed.path == "/api/csp-report":
        if diag:
            diag.stage("csp_report")
        try:
            return ctx["_handle_csp_report"](handler)
        finally:
            if diag:
                diag.finish()
    if parsed.path == "/api/process-complete-ack":
        if diag:
            diag.stage("process_complete_ack_deprecated")
        try:
            j(
                handler,
                {
                    "error": (
                        "gone: /api/process-complete-ack was replaced by "
                        "/api/bg-task-complete-ack as part of the "
                        "process_complete -> bg_task_complete event rename"
                    ),
                    "replaced_by": "/api/bg-task-complete-ack",
                },
                status=410,
                extra_headers={"X-Replaced-By": "/api/bg-task-complete-ack"},
            )
            return True
        finally:
            if diag:
                diag.finish()

    if diag:
        diag.stage("csrf")
    if not ctx["_csrf_exempt_path"](parsed.path) and not ctx["_check_csrf"](handler):
        try:
            return j(
                handler, {"error": ctx["_csrf_rejection_error"](handler)}, status=403
            )
        finally:
            if diag:
                diag.finish()

    proxy_result = ctx["_handle_extension_sidecar_proxy"](
        handler,
        parsed,
        "POST",
        read_request_body=True,
    )
    if proxy_result is not False:
        if diag:
            diag.finish()
        return proxy_result

    early_routes = {
        "/api/shutdown": lambda: handle_shutdown(handler),
        "/api/health/restart": lambda: handle_health_restart(handler),
        "/api/upload": lambda: ctx["handle_upload"](handler),
        "/api/upload/extract": lambda: ctx["handle_upload_extract"](handler),
        "/api/workspace/upload": lambda: ctx["handle_workspace_upload"](handler),
        "/api/transcribe": lambda: ctx["handle_transcribe"](handler),
        "/api/tts": lambda: ctx["_handle_tts"](handler, parsed),
    }
    early = early_routes.get(parsed.path)
    if early is not None:
        return early()
    if parsed.path == "/api/client-events/log":
        if diag:
            diag.stage("read_client_event_body")
        return ctx["_handle_client_event_log"](
            handler,
            ctx["_read_client_event_payload"](handler),
        )

    if diag:
        diag.stage("read_body")
    try:
        body = ctx["read_body"](handler)
    except ValueError as exc:
        if diag:
            diag.finish()
        status = 413 if "too large" in str(exc).lower() else 400
        return ctx["bad"](handler, str(exc), status=status)
    except Exception:
        if diag:
            diag.finish()
        raise

    if not ctx["_guard_request_session_visibility"](
        handler,
        parsed,
        body=body,
        method="POST",
    ):
        if diag:
            diag.finish()
        return True

    result = _first_handled(
        (
            lambda: platform_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: session_creation_mutations.handle_post(
                handler, parsed, body, diag, ctx
            ),
            lambda: provider_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: session_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: automation_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: profile_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: session_organization_mutations.handle_post(
                handler, parsed, body, diag, ctx
            ),
            lambda: update_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: auth_mutations.handle_post(handler, parsed, body, diag, ctx),
            lambda: rollback_mutations.handle_post(handler, parsed, body, diag, ctx),
        )
    )
    return False if result is UNHANDLED else result


def handle_patch(handler, parsed, ctx: RouteContext) -> bool:
    if not ctx["_check_csrf"](handler):
        return ctx["j"](
            handler,
            {"error": ctx["_csrf_rejection_error"](handler)},
            status=403,
        )
    proxy_result = ctx["_handle_extension_sidecar_proxy"](
        handler,
        parsed,
        "PATCH",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = ctx["read_body"](handler)
    if not ctx["_guard_request_session_visibility"](
        handler,
        parsed,
        body=body,
        method="PATCH",
    ):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/") :]
        return ctx["_handle_mcp_server_toggle"](handler, name, body)
    if parsed.path.startswith("/api/kanban/"):
        from api.kanban import handle_kanban_patch

        result = handle_kanban_patch(handler, parsed, body)
        if result is False:
            return ctx["_kanban_unknown_endpoint"](handler, parsed, "PATCH")
        return True
    return False


def handle_delete(handler, parsed, ctx: RouteContext) -> bool:
    if not ctx["_check_csrf"](handler):
        return ctx["j"](
            handler,
            {"error": ctx["_csrf_rejection_error"](handler)},
            status=403,
        )
    proxy_result = ctx["_handle_extension_sidecar_proxy"](
        handler,
        parsed,
        "DELETE",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = ctx["read_body"](handler)
    if not ctx["_guard_request_session_visibility"](
        handler,
        parsed,
        body=body,
        method="DELETE",
    ):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/") :]
        return ctx["_handle_mcp_server_delete"](handler, name)
    if parsed.path == "/api/prompts":
        prompt_id = str(body.get("id") or "").strip()
        if not prompt_id:
            return ctx["bad"](handler, "id is required")
        prompts = [
            prompt for prompt in load_saved_prompts() if prompt.get("id") != prompt_id
        ]
        save_saved_prompts(prompts)
        return ctx["j"](handler, {"ok": True})
    if parsed.path.startswith("/api/kanban/"):
        from api.kanban import handle_kanban_delete

        result = handle_kanban_delete(handler, parsed, body)
        if result is False:
            return ctx["_kanban_unknown_endpoint"](handler, parsed, "DELETE")
        return True
    return False


def handle_put(handler, parsed, ctx: RouteContext) -> bool:
    if not ctx["_check_csrf"](handler):
        return ctx["j"](handler, {"error": "Cross-origin request rejected"}, status=403)
    proxy_result = ctx["_handle_extension_sidecar_proxy"](
        handler,
        parsed,
        "PUT",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = ctx["read_body"](handler)
    if not ctx["_guard_request_session_visibility"](
        handler,
        parsed,
        body=body,
        method="PUT",
    ):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/") :]
        return ctx["_handle_mcp_server_update"](handler, name, body)
    return False
