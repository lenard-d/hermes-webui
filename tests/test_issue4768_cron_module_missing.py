"""Regression test for #4768.

In split-container / minimal Docker deployments the WebUI image may not ship the
agent's ``cron`` package on its import path. Before the fix, ``GET /api/crons``
(the Tasks tab's initial load) did ``from cron.jobs import list_jobs`` with no
guard, so a ``ModuleNotFoundError`` bubbled up as a 500 and broke the whole tab.

The fix degrades gracefully: an empty job list with a ``cron_unavailable`` flag.
"""
from collections import defaultdict
from types import SimpleNamespace

import pytest


def _cron_route_context(**overrides):
    def unused(*_args, **_kwargs):
        return None

    context = defaultdict(lambda: unused)
    context.update(
        {
            "_ensure_agent_cron_import_path": unused,
            "_get_active_profile_name": lambda: "default",
            "_all_profiles_enabled": lambda _parsed: False,
            **overrides,
        }
    )
    return context


def test_api_crons_guards_missing_cron_module():
    """The /api/crons GET branch must wrap the cron helper in a guard that
    returns a graceful payload instead of letting ModuleNotFoundError 500 — but
    only for a genuinely-absent cron package, not an internal import bug."""
    from api.http.routes import automation_queries

    captured = {}

    def missing_cron(_profile):
        raise ModuleNotFoundError("No module named 'cron'", name="cron")

    result = automation_queries.handle_get(
        object(),
        SimpleNamespace(path="/api/crons"),
        _cron_route_context(
            _cron_jobs_cross_profile=missing_cron,
            j=lambda _handler, payload, **_kwargs: captured.update(payload) or True,
        ),
    )

    assert result is True
    assert captured == {"jobs": [], "cron_unavailable": True}


def test_api_crons_does_not_hide_internal_import_errors():
    from api.http.routes import automation_queries

    def missing_internal_dependency(_profile):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    with pytest.raises(ModuleNotFoundError, match="yaml"):
        automation_queries.handle_get(
            object(),
            SimpleNamespace(path="/api/crons"),
            _cron_route_context(_cron_jobs_cross_profile=missing_internal_dependency),
        )


def test_api_crons_still_lists_jobs_on_happy_path():
    """The guard must not remove the normal behavior: when cron imports fine, the
    branch still calls list_jobs(include_disabled=True) under cron_profile_context."""
    from api.http.routes import automation_queries

    active_job = {"id": "active"}
    foreign_job = {"id": "foreign"}
    captured = {}
    result = automation_queries.handle_get(
        object(),
        SimpleNamespace(path="/api/crons"),
        _cron_route_context(
            _cron_jobs_cross_profile=lambda _profile: ([active_job], [foreign_job]),
            j=lambda _handler, payload, **_kwargs: captured.update(payload) or True,
        ),
    )

    assert result is True
    assert captured == {
        "jobs": [active_job],
        "all_profiles": False,
        "active_profile": "default",
        "other_profile_count": 1,
    }
