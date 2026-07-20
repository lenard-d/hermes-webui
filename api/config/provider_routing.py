"""Primitive provider/model routing helpers outside the main resolver."""

from typing import Protocol

from api import config as _config_module


class ProviderRoutingAPI(Protocol):
    cfg: dict
    _LOCAL_SERVER_PROVIDERS: set[str]
    _PROVIDER_DISPLAY: dict
    _PROVIDER_MODELS: dict

    def _configured_model_ids(self, raw_models: object) -> list[str]: ...
    def _custom_provider_entries(self, config_obj: dict | None = None) -> list[dict]: ...
    def _custom_provider_slug_from_name(self, name: object) -> str: ...
    def _custom_slug_rest_looks_like_host_port(self, rest: str) -> bool: ...
    def _get_provider_cfg(self, provider_id: object) -> dict: ...
    def _get_providers_cfg(self) -> dict: ...


def _is_local_server_provider(provider_id: str) -> bool:
    """Return whether provider_id names a known local model server."""
    provider = str(provider_id or "").strip().lower()
    local_providers = _config_module._LOCAL_SERVER_PROVIDERS
    if provider in local_providers:
        return True
    if provider.startswith("custom:"):
        return provider.removeprefix("custom:") in local_providers
    return False


def _model_id_declared_in_config(model_id: str, config_provider: str | None) -> bool:
    """Return whether user config declares the full model id verbatim."""
    model = str(model_id or "").strip()
    if not model:
        return False
    api = _config_module
    model_cfg = api.cfg.get("model", {})
    if isinstance(model_cfg, dict):
        if str(model_cfg.get("default") or "").strip() == model:
            return True
        if model in api._configured_model_ids(model_cfg.get("models")):
            return True
    provider = str(config_provider or "").strip().lower()
    if provider.startswith("custom:"):
        raw_suffix = provider.removeprefix("custom:")
        for entry in api._custom_provider_entries():
            slug = api._custom_provider_slug_from_name(entry.get("name"))
            entry_name = str(entry.get("name") or "").strip().lower()
            if not (
                provider in {entry_name, slug}
                or (slug and raw_suffix == slug.removeprefix("custom:"))
            ):
                continue
            if str(entry.get("model") or "").strip() == model:
                return True
            if model in api._configured_model_ids(entry.get("models")):
                return True
    return False


def _is_first_party_model(provider_id: str, model_id: str) -> bool:
    """Return whether a model appears in its provider's static catalog."""
    provider = str(provider_id or "").strip().lower()
    model = str(model_id or "").strip()
    if not provider or not model:
        return False
    catalog = _config_module._PROVIDER_MODELS.get(provider)
    if not isinstance(catalog, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("id") == model
        for entry in catalog
    )


def _base_url_points_at_local_server(base_url: str) -> bool:
    """Return whether a URL host is localhost or a private IP literal."""
    if not base_url:
        return False
    try:
        import ipaddress
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower()
        if not host:
            return False
        if host in ("localhost", "ip6-localhost", "ip6-loopback"):
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return addr.is_loopback or addr.is_private or addr.is_link_local
    except Exception:
        return False


def _custom_slug_rest_looks_like_host_port(rest: str) -> bool:
    """Return whether ``custom:<rest>`` uses an endpoint-style host and port."""
    rest = str(rest or "").strip()
    if ":" not in rest:
        return False
    host, port_s = rest.rsplit(":", 1)
    if not host or ":" in host or not port_s.isdigit():
        return False
    try:
        port_n = int(port_s)
    except ValueError:
        return False
    if not 1 <= port_n <= 65535:
        return False
    try:
        import ipaddress

        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    host_lower = host.lower()
    if host_lower == "localhost":
        return True
    return "." in host


def _parse_provider_qualified_model_id(model_id: str) -> tuple[str, str] | None:
    """Parse ``@provider:model`` while preserving colons in slugs and tags."""
    candidate = str(model_id or "").strip()
    if not candidate.startswith("@") or ":" not in candidate:
        return None
    inner = candidate[1:]
    provider_hint, bare_model = inner.rsplit(":", 1)
    api = _config_module
    if provider_hint.startswith("custom:") and provider_hint.count(":") >= 2:
        slug_rest = provider_hint[len("custom:") :]
        if not api._custom_slug_rest_looks_like_host_port(slug_rest):
            provider_hint, extra = provider_hint.rsplit(":", 1)
            bare_model = f"{extra}:{bare_model}"
    elif (
        provider_hint not in api._PROVIDER_MODELS
        and provider_hint not in api._PROVIDER_DISPLAY
        and not provider_hint.startswith("custom:")
    ):
        provider_hint, bare_model = inner.split(":", 1)
    return bare_model, provider_hint


def _get_provider_base_url(provider_id):
    """Return a provider-specific or active-model base URL when configured."""
    api = _config_module
    provider_cfg = api._get_provider_cfg(provider_id)
    explicit = (provider_cfg.get("base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    model_cfg = api.cfg.get("model", {}) or {}
    if isinstance(model_cfg, dict):
        model_provider = str(model_cfg.get("provider") or "").strip().lower()
        if model_provider == str(provider_id).strip().lower():
            model_base = (model_cfg.get("base_url") or "").strip().rstrip("/")
            if model_base:
                return model_base
    return None


def _get_providers_cfg() -> dict:
    providers_cfg = _config_module.cfg.get("providers")
    return providers_cfg if isinstance(providers_cfg, dict) else {}


def _get_provider_cfg(provider_id) -> dict:
    provider_cfg = _config_module._get_providers_cfg().get(provider_id, {})
    return provider_cfg if isinstance(provider_cfg, dict) else {}
