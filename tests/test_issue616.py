from collections import defaultdict
from types import SimpleNamespace

from tests.frontend_asset_contract import family_source


def test_workspace_suggest_endpoint_is_wired():
    from api.http.routes import workspace_queries

    captured = {}
    def unused(*_args, **_kwargs):
        return None

    context = defaultdict(lambda: unused)
    context.update(
        {
            "parse_qs": lambda _query: {"prefix": ["/tmp/her"]},
            "list_workspace_suggestions": lambda prefix: [prefix + "mes"],
            "j": lambda _handler, payload, **_kwargs: captured.update(payload) or True,
        }
    )

    assert workspace_queries.handle_get(
        object(),
        SimpleNamespace(path="/api/workspaces/suggest", query="prefix=/tmp/her"),
        context,
    ) is True
    assert captured == {
        "suggestions": ["/tmp/hermes"],
        "prefix": "/tmp/her",
    }


def test_spaces_panel_uses_workspace_suggest_autocomplete():
    src = family_source("panels")
    assert "/api/workspaces/suggest" in src
    assert "workspaceFormPathSuggestions" in src
    assert "scheduleWorkspacePathSuggestions" in src
    assert "if(!prefix)" in src
    assert "dataset.path" in src
    assert "scrollIntoView" in src
    assert "_wsSuggestIndex=0" in src
