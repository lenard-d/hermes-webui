"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    bad = ctx["bad"]
    j = ctx["j"]
    logger = ctx["logger"]

    if parsed.path == "/api/rollback/restore":
        if not body:
            return bad(handler, "request body is required")
        workspace = body.get("workspace", "")
        checkpoint = body.get("checkpoint", "")
        if not workspace or not checkpoint:
            return bad(handler, "workspace and checkpoint are required")
        try:
            from api.rollback import restore_checkpoint

            return j(handler, restore_checkpoint(workspace, checkpoint))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/restore failed")
            return bad(handler, str(e), status=500)
    return UNHANDLED
