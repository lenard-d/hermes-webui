"""Session package ownership and legacy import-seam contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import api.config as config
import api.models as legacy_models
import api.sessions as sessions
import api.sessions.cache as session_cache
import api.sessions.external as session_external
import api.sessions.records as session_records
import api.sessions.store as store


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_session_package_keeps_config_store_and_lock_identity():
    assert store.SESSIONS is config.SESSIONS
    assert store.LOCK is config.LOCK


def test_session_cache_objects_have_one_semantic_owner():
    assert session_external._CLI_SESSIONS_CACHE is session_external.get_cli_sessions.__globals__["_CLI_SESSIONS_CACHE"]
    assert session_external._CLAUDE_CODE_PARSE_CACHE is session_external.get_claude_code_sessions.__globals__["_CLAUDE_CODE_PARSE_CACHE"]
    assert session_external._SIDECAR_METADATA_CACHE is session_external._state_projection_sidecar_metadata.__globals__["_SIDECAR_METADATA_CACHE"]
    assert store._CLI_SESSIONS_CACHE is session_external._CLI_SESSIONS_CACHE


def test_package_interface_points_at_semantic_owners():
    assert sessions.Session is session_records.Session
    assert sessions.get_session is session_cache.get_session
    assert sessions.new_session is session_cache.new_session
    assert store.Session is session_records.Session
    assert store.get_session is session_cache.get_session


def test_legacy_models_is_a_state_free_adapter():
    assert legacy_models.Session is store.Session
    assert legacy_models.get_session is store.get_session
    assert legacy_models._CLI_SESSIONS_CACHE is store._CLI_SESSIONS_CACHE
    assert "_CLI_SESSIONS_CACHE" not in vars(legacy_models)
    assert "_MODELS_FACADE_PARTS" not in vars(legacy_models)


def test_legacy_adapter_does_not_import_removed_models_parts():
    code = """
import sys
import api.models as legacy
import api.sessions.store as store
assert legacy.Session is store.Session
assert not any(name.startswith('api.models_parts') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_session_domain_does_not_import_http_routes():
    for path in (REPO_ROOT / "api" / "sessions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import api.routes" not in source
        assert "from api.routes" not in source


def test_session_domain_modules_do_not_depend_on_compatibility_store():
    for path in (REPO_ROOT / "api" / "sessions").glob("*.py"):
        if path.name == "store.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "import api.sessions.store" not in source
        assert "from api.sessions import store" not in source
        assert "from .store" not in source
