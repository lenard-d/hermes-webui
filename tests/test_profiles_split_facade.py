"""Architecture contracts for the real :mod:`api.profiles` package."""

from pathlib import Path

import api.profiles as profiles
from api.profiles import catalog, cron, management, runtime


def test_package_entrypoint_has_no_binding_dispatch():
    source = Path(profiles.__file__).read_text(encoding="utf-8")
    assert "FunctionType" not in source
    assert "ContextVar" not in source
    assert "_binding" not in source


def test_profile_exports_are_the_real_owner_functions():
    assert profiles.list_profiles_api is catalog.list_profiles_api
    assert profiles.create_profile_api is management.create_profile_api
    assert profiles.get_profile_runtime_env is runtime.get_profile_runtime_env
    assert (
        profiles.install_cron_scheduler_profile_isolation
        is cron.install_cron_scheduler_profile_isolation
    )


def test_profile_layer_does_not_import_provider_layer():
    root = Path(profiles.__file__).parent
    sources = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "from api.providers" not in sources


def test_cron_context_uses_explicit_owner_instead_of_facade_argument():
    context = cron.CronProfileContext()
    assert not hasattr(context, "_api")
