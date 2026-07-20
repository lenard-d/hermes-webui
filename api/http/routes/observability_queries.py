"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_get(handler, parsed, ctx: RouteContext):
    _handle_health = ctx["_handle_health"]
    _handle_insights = ctx["_handle_insights"]
    _handle_llm_wiki_browse = ctx["_handle_llm_wiki_browse"]
    _handle_llm_wiki_page = ctx["_handle_llm_wiki_page"]
    _handle_llm_wiki_status = ctx["_handle_llm_wiki_status"]
    _handle_logs = ctx["_handle_logs"]
    _handle_project_os_dashboard = ctx["_handle_project_os_dashboard"]
    _kanban_unknown_endpoint = ctx["_kanban_unknown_endpoint"]
    build_agent_health_payload = ctx["build_agent_health_payload"]
    build_system_health_payload = ctx["build_system_health_payload"]
    gateway_chat_config_status = ctx["gateway_chat_config_status"]
    j = ctx["j"]

    if parsed.path == "/api/insights":
        return _handle_insights(handler, parsed)
    if parsed.path == "/api/project-os/dashboard":
        return _handle_project_os_dashboard(handler, parsed)

    if parsed.path.startswith("/api/kanban/"):
        from api.kanban import handle_kanban_get

        # Only treat an explicit False as "no route matched". None means the
        # bridge already sent a response via bad()/j() — emitting our own 404
        # on top of that produces concatenated JSON bodies on the wire.
        result = handle_kanban_get(handler, parsed)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "GET")
        return True
    if parsed.path == "/api/wiki/status":
        return _handle_llm_wiki_status(handler, parsed)
    if parsed.path == "/api/wiki/browse":
        return _handle_llm_wiki_browse(handler, parsed)
    if parsed.path == "/api/wiki/page":
        return _handle_llm_wiki_page(handler, parsed)
    if parsed.path == "/api/logs":
        return _handle_logs(handler, parsed)

    if parsed.path == "/health":
        return _handle_health(handler, parsed)

    if parsed.path == "/api/health/agent":
        payload = build_agent_health_payload()
        payload["gateway_chat"] = gateway_chat_config_status()
        j(handler, payload)
        return True

    if parsed.path == "/api/system/health":
        j(handler, build_system_health_payload())
        return True
    return UNHANDLED
