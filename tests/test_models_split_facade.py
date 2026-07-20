"""Compatibility contracts for the split ``api.models`` facade."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import api.config as config
import api.models as models
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_models_production_modules_stay_below_500_lines():
    production_files = [REPO_ROOT / "api" / "models.py"]
    production_files.extend(sorted((REPO_ROOT / "api" / "models_parts").glob("*.py")))

    oversized = {
        path.relative_to(REPO_ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in production_files
        if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }

    assert oversized == {}


def test_facade_keeps_config_store_and_lock_identity():
    assert models.SESSIONS is config.SESSIONS
    assert models.LOCK is config.LOCK


def test_facade_cache_objects_are_the_objects_used_by_implementations():
    assert (
        models._CLI_SESSIONS_CACHE
        is models.get_cli_sessions.__globals__["_CLI_SESSIONS_CACHE"]
    )
    assert (
        models._CLAUDE_CODE_PARSE_CACHE
        is models.get_claude_code_sessions.__globals__["_CLAUDE_CODE_PARSE_CACHE"]
    )
    assert (
        models._SIDECAR_METADATA_CACHE
        is models._state_projection_sidecar_metadata.__globals__["_SIDECAR_METADATA_CACHE"]
    )


def test_facade_assignment_reaches_session_persistence(monkeypatch, tmp_path):
    replacements = []
    real_replace = models._safe_replace

    def observed_replace(source, destination):
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "_safe_replace", observed_replace)

    session = models.Session(session_id="split-facade-save", workspace=tmp_path)
    session.save(skip_index=True)

    assert len(replacements) == 1
    assert replacements[0][0].name.startswith("split-facade-save.tmp.")
    assert replacements[0][1] == session.path
    assert isinstance(models.Session.load(session.session_id), models.Session)


def test_facade_delete_then_monkeypatch_restore_reaches_consumers(
    monkeypatch, tmp_path
):
    """A restored facade binding remains visible to existing function globals."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = models.Session(session_id="split-facade-restore", workspace=tmp_path)
    session.save(skip_index=True)

    monkeypatch.delattr(models, "SESSION_DIR")
    monkeypatch.undo()

    assert models.get_session.__globals__["SESSION_DIR"] is config.SESSION_DIR
    with pytest.raises(KeyError, match="split-facade-restore"):
        models.get_session("split-facade-restore")


def test_facade_hides_mixins_and_normalizes_session_method_metadata():
    assert "_SessionPersistenceMixin" not in vars(models)
    assert "_SessionProjectionMixin" not in vars(models)

    for method_name in ("__init__", "save", "load", "load_metadata_only", "compact"):
        method = getattr(models.Session, method_name)
        assert method.__module__ == "api.models"
        assert method.__qualname__ == f"Session.{method_name}"


def test_import_reload_and_pickle_contract_in_fresh_process():
    code = """
import importlib
import pickle
import api.config as config
import api.models as models

session = models.Session(session_id="split-pickle")
assert models.Session.__module__ == "api.models"
assert pickle.loads(pickle.dumps(session)).__class__ is models.Session
old_class = models.Session
old_cache = models._CLI_SESSIONS_CACHE
old_sessions = models.SESSIONS
old_lock = models.LOCK
models = importlib.reload(models)
assert models.Session is not old_class
assert models._CLI_SESSIONS_CACHE is not old_cache
assert models.SESSIONS is old_sessions is config.SESSIONS
assert models.LOCK is old_lock is config.LOCK
assert models.Session.__module__ == "api.models"
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_failed_reload_restores_usable_facade_in_fresh_process():
    code = """
import importlib
import tempfile
from pathlib import Path

import api.models as models

old_facade_class = models.__class__
old_session = models.Session
old_get_session = models.get_session
old_exports = tuple(models._MODELS_FACADE_EXPORTS)
old_session_dir = Path(tempfile.gettempdir()) / "models-reload-rollback"
models.SESSION_DIR = old_session_dir
real_reload = importlib.reload

def fail_on_session_part(module):
    if module.__name__ == "api.models_parts.session":
        raise RuntimeError("injected models part reload failure")
    return real_reload(module)

importlib.reload = fail_on_session_part
try:
    try:
        real_reload(models)
    except RuntimeError as exc:
        assert str(exc) == "injected models part reload failure"
    else:
        raise AssertionError("models reload unexpectedly succeeded")
finally:
    importlib.reload = real_reload

assert models.__class__ is old_facade_class
assert models.Session is old_session
assert models.get_session is old_get_session
assert tuple(models._MODELS_FACADE_EXPORTS) == old_exports
assert models.SESSION_DIR is old_session_dir
assert models.get_session.__globals__["SESSION_DIR"] is old_session_dir
assert models.Session(session_id="reload-rollback").session_id == "reload-rollback"
with tempfile.TemporaryDirectory() as directory:
    models.SESSION_DIR = Path(directory)
    try:
        models.get_session("missing-after-rollback")
    except KeyError:
        pass
    else:
        raise AssertionError("missing session unexpectedly resolved")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_individual_part_is_cold_importable():
    code = """
import importlib

part = importlib.import_module("api.models_parts.cli_listing")
models = importlib.import_module("api.models")
assert part.get_cli_sessions is models.get_cli_sessions
assert len(models._MODELS_FACADE_PARTS) == 32
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
