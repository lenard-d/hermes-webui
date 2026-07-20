"""Compatibility contracts for the split ``api.models`` facade."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import api.config as config
import api.models as models


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
