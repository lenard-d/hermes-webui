import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_profiles_module(base_home: Path):
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)

    # Save the original module references so we can restore them after the test.
    # Permanently deleting api.config / api.profiles from sys.modules breaks
    # subsequent tests that import these modules and expect consistent state.
    _saved = {name: sys.modules[name] for name in ["api.config", "api.profiles"]
              if name in sys.modules}

    for name in ["api.config", "api.profiles"]:
        if name in sys.modules:
            del sys.modules[name]

    profiles = importlib.import_module("api.profiles")

    # Restore original modules and package attributes so the cache stays
    # consistent for the rest of the suite.
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)

    return profiles


def test_switch_profile_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        base = temp_root / ".hermes"
        (base / "profiles").mkdir(parents=True)
        (temp_root / "escape-target").mkdir()

        profiles = _reload_profiles_module(base)

        with pytest.raises(ValueError):
            profiles.switch_profile("../../escape-target")


def test_delete_profile_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        base = temp_root / ".hermes"
        (base / "profiles").mkdir(parents=True)
        (temp_root / "escape-target").mkdir()

        profiles = _reload_profiles_module(base)

        with pytest.raises(ValueError):
            profiles.delete_profile_api("../../escape-target")


def test_delete_fallback_observes_facade_shutil_patch(monkeypatch, tmp_path):
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    profile_dir = base / "profiles" / "demo"
    profile_dir.mkdir(parents=True)
    removed = []

    assert profiles.shutil is not None
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", None)
    monkeypatch.setattr(profiles.shutil, "rmtree", removed.append)

    assert profiles.delete_profile_api("demo") == {"ok": True, "name": "demo"}
    assert removed == [str(profile_dir)]


def test_reloaded_profile_facades_keep_their_own_runtime_policy(monkeypatch, tmp_path):
    """Part implementations must resolve the facade that exported each call."""
    import api.profiles as original

    fresh = _reload_profiles_module(tmp_path / ".hermes")
    monkeypatch.setattr(original, "_BLOCKED_RUNTIME_ENV_KEYS", {"ORIGINAL_ONLY"})
    monkeypatch.setattr(fresh, "_BLOCKED_RUNTIME_ENV_KEYS", {"FRESH_ONLY"})
    candidate = {"ORIGINAL_ONLY": "old", "FRESH_ONLY": "new", "SHARED": "ok"}

    assert original.filter_runtime_env_for_gateway_parity(candidate) == {
        "FRESH_ONLY": "new",
        "SHARED": "ok",
    }
    assert fresh.filter_runtime_env_for_gateway_parity(candidate) == {
        "ORIGINAL_ONLY": "old",
        "SHARED": "ok",
    }


def test_switch_profile_allows_valid_profile_name():
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        base = temp_root / ".hermes"
        profile_dir = base / "profiles" / "demo"
        profile_dir.mkdir(parents=True)

        profiles = _reload_profiles_module(base)
        result = profiles.switch_profile("demo")

        assert result["active"] == "demo"
        assert Path(os.environ["HERMES_HOME"]).resolve() == profile_dir.resolve()
