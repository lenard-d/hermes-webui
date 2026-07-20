"""Architecture contracts for the explicit HTTP composition root."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
HTTP_ROOT = ROOT / "api" / "http"
ROUTE_ROOT = HTTP_ROOT / "routes"
OWNER_MODULES = (
    "api.http.observability",
    "api.http.plugins",
    "api.http.project_context",
    "api.http.project_os",
    "api.http.shell",
)


def test_route_groups_are_independently_importable_without_legacy_facade():
    modules = sorted(
        f"api.http.routes.{path.stem}"
        for path in ROUTE_ROOT.glob("*.py")
        if path.stem != "__init__"
    )
    script = (
        "import importlib, sys; "
        f"modules = {modules!r}; "
        "[importlib.import_module(name) for name in modules]; "
        "assert 'api.routes' not in sys.modules"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_http_owners_do_not_eagerly_load_router_or_legacy_facade():
    for module in OWNER_MODULES:
        script = (
            "import importlib, sys; "
            f"importlib.import_module({module!r}); "
            "assert 'api.http.router' not in sys.modules; "
            "assert 'api.routes' not in sys.modules"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def test_http_modules_do_not_use_legacy_binding_or_source_composition():
    forbidden_names = {"exec", "FunctionType", "install_routes_part"}
    for path in HTTP_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assert "import api.routes" not in source
        assert "from api.routes" not in source
        assert "sys.modules" not in source
        loaded_names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        assert not (loaded_names & forbidden_names), path


def test_legacy_facade_passes_its_live_compatibility_context(monkeypatch):
    import api.routes as routes
    from api.http import router

    seen = {}

    def dispatch(handler, parsed, context):
        seen["handler"] = handler
        seen["parsed"] = parsed
        seen["context"] = context
        return "handled"

    handler = object()
    parsed = urlsplit("/health")
    monkeypatch.setattr(router, "handle_get", dispatch)

    assert routes.handle_get(handler, parsed) == "handled"
    assert seen == {"handler": handler, "parsed": parsed, "context": routes.__dict__}


def test_route_inventory_lives_with_the_new_owners():
    owner_source = "\n".join(
        path.read_text(encoding="utf-8") for path in ROUTE_ROOT.glob("*.py")
    )
    for path in (
        "/api/session",
        "/api/session/branch",
        "/api/chat/start",
        "/api/terminal/start",
        "/api/models",
        "/api/providers",
        "/api/updates/force",
        "/api/workspaces",
        "/api/mcp/servers",
        "/api/auth/login",
    ):
        assert path in owner_source


def test_admin_reload_fails_closed_for_modular_session_state():
    from api.http.routes import provider_mutations

    responses = []
    context = {
        "_clear_live_models_cache": lambda: None,
        "_handle_sessions_cleanup": lambda *_args, **_kwargs: None,
        "bad": lambda *_args, **_kwargs: None,
        "j": lambda _handler, payload, status=200: (
            responses.append((status, payload)) or True
        ),
        "remove_provider_key": lambda *_args, **_kwargs: None,
        "set_hermes_default_model": lambda *_args, **_kwargs: None,
        "set_provider_key": lambda *_args, **_kwargs: None,
        "set_reasoning_display": lambda *_args, **_kwargs: None,
        "set_reasoning_effort": lambda *_args, **_kwargs: None,
    }

    assert provider_mutations.handle_post(
        object(), urlsplit("/api/admin/reload"), {}, None, context
    )
    assert responses == [
        (
            409,
            {
                "status": "restart_required",
                "error": "Session modules cannot be hot-reloaded safely; restart Hermes WebUI.",
            },
        )
    ]
