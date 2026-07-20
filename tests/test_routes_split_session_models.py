"""Compatibility checks for session model/provider/context resolution ownership."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import session_models
from api.sessions import (
    model_compatibility,
    model_identity,
    profile_model_config,
    session_model_context,
    session_model_state,
)


REPO = Path(__file__).resolve().parents[1]


def test_session_model_functions_keep_the_routes_facade_as_compatibility_seam():
    for name in session_models.__routes_exports__:
        value = getattr(routes, name)
        if callable(value) and name != "_ContextLengthLookupInputs":
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)

    assert routes._PROFILE_CONFIG_CACHE is session_models._PROFILE_CONFIG_CACHE
    assert routes._PROFILE_CONFIG_CACHE_LOCK is session_models._PROFILE_CONFIG_CACHE_LOCK
    assert routes._ContextLengthLookupInputs.__module__ == "api.routes"


def test_session_models_owner_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.routes_parts.session_models as owner; "
                "assert owner.__routes_exports__; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_session_models_implementation_is_file_backed_and_facade_stays_thin():
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    owners = (
        model_compatibility,
        model_identity,
        profile_model_config,
        session_model_context,
        session_model_state,
    )
    for owner in owners:
        assert Path(owner.__file__).is_file()
        assert "exec(" not in Path(owner.__file__).read_text(encoding="utf-8")

    assert inspect.getmodule(
        model_compatibility._resolve_compatible_session_model_state
    ) is model_compatibility
    assert inspect.getmodule(
        session_model_context._resolve_context_length_for_session_model
    ) is session_model_context
    assert inspect.getmodule(
        session_model_state._session_model_state_from_request
    ) is session_model_state
    assert "def _resolve_compatible_session_model_state(" not in facade_source
