"""Public interface for first-run onboarding."""

from .catalog import (
    PROVIDER_CATEGORIES as _PROVIDER_CATEGORIES,
    SUPPORTED_PROVIDER_SETUPS as _SUPPORTED_PROVIDER_SETUPS,
    UNSUPPORTED_PROVIDER_NOTE as _UNSUPPORTED_PROVIDER_NOTE,
    extract_current_base_url as _extract_current_base_url,
    extract_current_model as _extract_current_model,
    extract_current_provider as _extract_current_provider,
    normalize_base_url as _normalize_base_url,
    normalize_model_for_provider as _normalize_model_for_provider,
)
from .persistence import (
    get_active_hermes_home as _get_active_hermes_home,
    load_env_file as _load_env_file,
    load_yaml_config as _load_yaml_config,
    oauth_payload_has_token as _oauth_payload_has_token,
    provider_api_key_present as _provider_api_key_present,
    provider_oauth_authenticated as _provider_oauth_authenticated,
    save_yaml_config as _save_yaml_config,
    write_env_values as _write_env_file,
)
from .persistence import load_env_file
from .probe import (
    PROBE_ERROR_CODES,
    PROBE_MAX_BYTES,
    PROBE_TIMEOUT_SECONDS,
    probe_provider_endpoint,
)
from .setup import (
    apply_onboarding_setup,
    apply_self_hosted_provider_setup,
    complete_onboarding,
)
from .status import (
    get_onboarding_status,
    setup_catalog as _build_setup_catalog,
    status_from_runtime as _status_from_runtime,
)

__all__ = (
    "PROBE_ERROR_CODES",
    "PROBE_MAX_BYTES",
    "PROBE_TIMEOUT_SECONDS",
    "apply_onboarding_setup",
    "apply_self_hosted_provider_setup",
    "complete_onboarding",
    "get_onboarding_status",
    "load_env_file",
    "probe_provider_endpoint",
    # Transitional compatibility for established callers. New code should
    # import the owning submodule when it needs an internal test seam.
    "_PROVIDER_CATEGORIES",
    "_SUPPORTED_PROVIDER_SETUPS",
    "_UNSUPPORTED_PROVIDER_NOTE",
    "_build_setup_catalog",
    "_extract_current_base_url",
    "_extract_current_model",
    "_extract_current_provider",
    "_get_active_hermes_home",
    "_load_env_file",
    "_load_yaml_config",
    "_normalize_base_url",
    "_normalize_model_for_provider",
    "_oauth_payload_has_token",
    "_provider_api_key_present",
    "_provider_oauth_authenticated",
    "_save_yaml_config",
    "_status_from_runtime",
    "_write_env_file",
)
