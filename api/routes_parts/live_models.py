"""Live provider-model discovery, guarded upstream transport, and TTL caching."""

from __future__ import annotations

import copy
import errno
import http.client
import json
import logging
import os
import socket as _socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from api.config import CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS
from api.helpers import j

logger = logging.getLogger(__name__)


def _routes_facade_override(name: str, fallback):
    """Keep class-method dependency lookups compatible with ``api.routes`` patches."""
    facade = sys.modules.get("api.routes")
    return getattr(facade, name, fallback) if facade is not None else fallback


# OpenAI-compatible /v1/models endpoints for live model discovery.
# Used as fallback when hermes_cli.provider_model_ids() is unavailable or
# returns [] for a provider (#871).  Kept at module level so the dict is
# built once, not reconstructed per request.
_OPENAI_COMPAT_ENDPOINTS = {
    "zai": "https://api.z.ai/v1",
    "minimax": "https://api.minimax.chat/v1",
    "mistralai": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}
# NOTE: "openai-codex" is excluded because it maps to the same endpoint as
# the base "openai" provider (api.openai.com/v1).  When both are configured
# the openai provider is already wired through provider_model_ids(); codex-
# specific model filtering happens downstream in hermes_cli.
#
_LIVE_MODELS_CACHE_TTL = 60.0
_LIVE_MODELS_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_LIVE_MODELS_CACHE_LOCK = threading.RLock()
_LIVE_MODELS_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _live_models_address_is_global(address: str) -> bool:
    import ipaddress

    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    candidate = getattr(candidate, "ipv4_mapped", None) or candidate
    return bool(candidate.is_global)


def _live_models_address_is_loopback(address: str) -> bool:
    import ipaddress

    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    candidate = getattr(candidate, "ipv4_mapped", None) or candidate
    return bool(candidate.is_loopback)


def _resolve_live_models_addresses(
    hostname: str,
    port: int,
    *,
    allow_configured_private: bool,
    require_loopback: bool = False,
) -> list[str]:
    """Resolve once and return only addresses authorized for the eventual dial."""
    try:
        infos = _socket.getaddrinfo(hostname, port, type=_socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError("could not resolve live-model endpoint") from exc

    addresses = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if require_loopback:
            if not _live_models_address_is_loopback(address):
                raise ValueError("live-model endpoint did not resolve to loopback")
        elif not allow_configured_private and not _live_models_address_is_global(address):
            raise ValueError("live-model endpoint resolved to a non-global address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("could not resolve live-model endpoint")
    return addresses


def _prepare_live_models_target(
    base_url: object,
    *,
    append_v1: bool,
    allow_configured_private: bool,
) -> tuple[str, list[str]]:
    """Validate a models base URL and bind it to one vetted DNS result.

    Public endpoints are HTTPS-only and must resolve entirely to globally
    routable addresses. Explicit ``custom_providers[]`` entries retain the
    existing local/LAN/Docker contract, including HTTP, but are still pinned
    to the address set resolved here. The legacy ``model.base_url`` path only
    gets the documented localhost-over-HTTP exception.
    """
    raw = str(base_url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("live-model endpoint must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("live-model endpoint must not contain credentials")
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("invalid live-model endpoint URL")
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("live-model endpoint URL was missing a hostname")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("invalid live-model endpoint port") from exc

    require_loopback = False
    if parsed.scheme == "http":
        if allow_configured_private:
            # An explicitly configured custom provider may be a LAN, Docker,
            # Ollama, or LM Studio endpoint. Never send a bearer over cleartext
            # to a public address, even when the URL is configuration-owned.
            require_loopback = False
        elif hostname in _LIVE_MODELS_LOOPBACK_HOSTS:
            require_loopback = True
        else:
            raise ValueError("public live-model endpoints must use https")

    addresses = _resolve_live_models_addresses(
        hostname,
        port,
        allow_configured_private=allow_configured_private,
        require_loopback=require_loopback,
    )
    if parsed.scheme == "http" and allow_configured_private:
        if any(_live_models_address_is_global(address) for address in addresses):
            raise ValueError("public live-model endpoints must use https")

    path = parsed.path.rstrip("/")
    if append_v1 and not path.endswith("/v1"):
        path += "/v1"
    path += "/models"
    return parsed._replace(path=path, query="", fragment="").geturl(), addresses


class _NoRedirectLiveModelsHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("live-model endpoint attempted a redirect")


class _PinnedLiveModelsHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, pinned_addresses, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses)
        super().__init__(host, **kwargs)

    def connect(self):
        sys.audit("http.client.connect", self, self.host, self.port)
        last_error = None
        socket_module = _routes_facade_override("_socket", _socket)
        for address in self._pinned_addresses:
            try:
                self.sock = socket_module.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
            raise OSError("no vetted live-model endpoint address")
        if self._tunnel_host:
            self._tunnel()


class _PinnedLiveModelsHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, pinned_addresses, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses)
        super().__init__(host, **kwargs)

    def connect(self):
        sys.audit("http.client.connect", self, self.host, self.port)
        last_error = None
        socket_module = _routes_facade_override("_socket", _socket)
        for address in self._pinned_addresses:
            try:
                self.sock = socket_module.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
            raise OSError("no vetted live-model endpoint address")
        try:
            self.sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedLiveModelsHTTPHandler(HTTPHandler):
    def __init__(self, pinned_addresses):
        super().__init__()
        self._pinned_addresses = tuple(pinned_addresses)

    def http_open(self, req):
        def connection(host, **kwargs):
            connection_class = _routes_facade_override(
                "_PinnedLiveModelsHTTPConnection",
                _PinnedLiveModelsHTTPConnection,
            )
            return connection_class(
                host, pinned_addresses=self._pinned_addresses, **kwargs
            )

        return self.do_open(connection, req)


class _PinnedLiveModelsHTTPSHandler(HTTPSHandler):
    def __init__(self, pinned_addresses):
        super().__init__()
        self._pinned_addresses = tuple(pinned_addresses)

    def https_open(self, req):
        def connection(host, **kwargs):
            connection_class = _routes_facade_override(
                "_PinnedLiveModelsHTTPSConnection",
                _PinnedLiveModelsHTTPSConnection,
            )
            return connection_class(
                host, pinned_addresses=self._pinned_addresses, **kwargs
            )

        return self.do_open(connection, req, context=self._context)


def _open_live_models_request(req, *, pinned_addresses, timeout):
    parsed = urlsplit(req.full_url)
    pinned_handler = (
        _PinnedLiveModelsHTTPSHandler(pinned_addresses)
        if parsed.scheme == "https"
        else _PinnedLiveModelsHTTPHandler(pinned_addresses)
    )
    opener = build_opener(
        ProxyHandler({}),
        _NoRedirectLiveModelsHandler(),
        pinned_handler,
    )
    return opener.open(req, timeout=timeout)


def _fetch_live_models_payload(
    base_url: object,
    api_key: object,
    *,
    timeout: float,
    append_v1: bool,
    allow_configured_private: bool,
):
    models_url, pinned_addresses = _prepare_live_models_target(
        base_url,
        append_v1=append_v1,
        allow_configured_private=allow_configured_private,
    )
    key = str(api_key or "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = Request(models_url, headers=headers)
    with _open_live_models_request(
        req,
        pinned_addresses=pinned_addresses,
        timeout=timeout,
    ) as resp:
        return json.loads(resp.read())


def _active_profile_for_live_models_cache() -> str:
    try:
        from api.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception as _e:
        # A transient profile-resolution error mis-scopes the cache for up to
        # 60s ("default" gets the wrong payload). Log so we can detect it; the
        # blast radius stays small because the TTL caps the bad-cache window.
        logger.debug("_active_profile_for_live_models_cache fell back to 'default': %s", _e)
        return "default"


def _live_models_cache_key(provider: str) -> tuple[str, str]:
    return (_active_profile_for_live_models_cache(), provider)


def _get_cached_live_models(key: tuple[str, str]) -> dict | None:
    now = time.monotonic()
    with _LIVE_MODELS_CACHE_LOCK:
        cached = _LIVE_MODELS_CACHE.get(key)
        if not cached:
            return None
        ts, payload = cached
        if now - ts >= _LIVE_MODELS_CACHE_TTL:
            _LIVE_MODELS_CACHE.pop(key, None)
            return None
        return copy.deepcopy(payload)


def _set_cached_live_models(key: tuple[str, str], payload: dict) -> None:
    with _LIVE_MODELS_CACHE_LOCK:
        _LIVE_MODELS_CACHE[key] = (time.monotonic(), copy.deepcopy(payload))


def _clear_live_models_cache() -> None:
    with _LIVE_MODELS_CACHE_LOCK:
        _LIVE_MODELS_CACHE.clear()


def _handle_live_models(handler, parsed):
    """Return the live model list for a provider.

    Delegates to the agent's provider_model_ids() which handles:
    - OpenRouter: live fetch from /api/v1/models
    - Anthropic: live fetch from /v1/models (API key or OAuth token)
    - Copilot: live fetch from api.githubcopilot.com/models with correct headers
    - openai-codex: Codex OAuth endpoint + local ~/.codex/ cache fallback
    - Nous: live fetch from inference-api.nousresearch.com/v1/models
    - DeepSeek, kimi-coding, opencode-zen/go, custom: generic OpenAI-compat /v1/models
    - ZAI, MiniMax, Google/Gemini: fall back to static list (non-standard endpoints)
    - All others: static _PROVIDER_MODELS fallback

    The agent already maintains all provider-specific auth and endpoint logic
    in one place; the WebUI inherits it rather than duplicating it.

    Query params:
        provider  (optional) — provider ID; defaults to active profile provider
    """
    qs = parse_qs(parsed.query)
    provider = (qs.get("provider", [""])[0] or "").lower().strip()

    try:
        from api.config import get_config as _gc
        cfg = _gc()
        if not provider:
            provider = cfg.get("model", {}).get("provider") or ""
        if not provider:
            return j(handler, {"error": "no_provider", "models": []})

        # Normalize provider alias so 'z.ai' -> 'zai', 'x.ai' -> 'xai', etc.
        # The browser sends whatever active_provider the static endpoint returned;
        # without normalization, provider_model_ids() misses the alias and returns [].
        # Uses the WebUI-owned table (api/config._resolve_provider_alias) which
        # works even when hermes_cli is not on sys.path.
        from api.config import _resolve_provider_alias
        provider = _resolve_provider_alias(provider)

        cache_key = _live_models_cache_key(provider)
        cached = _get_cached_live_models(cache_key)
        if cached is not None:
            return j(handler, cached)

        def _finish(payload: dict):
            _set_cached_live_models(cache_key, payload)
            return j(handler, payload)

        # Delegate to the agent's live-fetch + fallback resolver.
        # provider_model_ids() tries live endpoints first and falls back to
        # the static _PROVIDER_MODELS list — it never raises.
        try:
            import sys as _sys
            import os as _os
            _agent_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                       "..", "..", ".hermes", "hermes-agent")
            _agent_dir = _os.path.normpath(_agent_dir)
            if _agent_dir not in _sys.path:
                _sys.path.insert(0, _agent_dir)
            from hermes_cli.models import provider_model_ids as _pmi
            ids = _pmi(provider)
        except Exception as _import_err:
            logger.debug("provider_model_ids import failed for %s: %s", provider, _import_err)
            ids = []

        if not ids:
            custom_provider_entry = None

            def _custom_provider_entries_for_request():
                if not (provider == "custom" or provider.startswith("custom:")):
                    return []
                try:
                    from api.config import _custom_provider_slug_from_name
                    _cp_entries = cfg.get("custom_providers", [])
                    if not isinstance(_cp_entries, list):
                        return []
                    _matches = []
                    for _cp in _cp_entries:
                        if not isinstance(_cp, dict):
                            continue
                        _slug = _custom_provider_slug_from_name(_cp.get("name", ""))
                        if provider.startswith("custom:"):
                            if _slug == provider:
                                _matches.append(_cp)
                        elif provider == "custom" and not _slug:
                            _matches.append(_cp)
                    return _matches
                except Exception:
                    return []

            def _custom_provider_model_ids(_cp):
                _ids = []

                def _append(_mid):
                    _mid = str(_mid or "").strip()
                    if _mid and _mid not in _ids:
                        _ids.append(_mid)

                _append(_cp.get("model", ""))
                _models = _cp.get("models")
                if isinstance(_models, dict):
                    for _mid in _models:
                        if isinstance(_mid, str):
                            _append(_mid)
                elif isinstance(_models, list):
                    for _item in _models:
                        if isinstance(_item, str):
                            _append(_item)
                        elif isinstance(_item, dict):
                            _append(_item.get("id") or _item.get("model") or _item.get("name"))
                return _ids

            def _custom_provider_api_key(_cp):
                _raw = _cp.get("api_key")
                if _raw is not None:
                    _key = str(_raw).strip()
                    if _key.startswith("${") and _key.endswith("}") and len(_key) > 3:
                        _key = os.getenv(_key[2:-1], "").strip()
                    if _key:
                        return _key
                _env = str(_cp.get("key_env") or "").strip()
                return os.getenv(_env, "").strip() if _env else ""

            # For 'custom' and 'custom:*' providers, provider_model_ids()
            # returns [] because they aren't real hermes_cli endpoints.
            # Fall back to the custom_providers entries from config.yaml so
            # the live-model enrichment step can add any models that weren't
            # already in the static list (issue #1619).
            # Collect config-specified model IDs separately so they don't
            # prevent the live fetch below from running (#3718).
            _config_ids = []
            if provider == "custom" or provider.startswith("custom:"):
                for _cp in _custom_provider_entries_for_request():
                    if custom_provider_entry is None:
                        custom_provider_entry = _cp
                    _config_ids.extend(_custom_provider_model_ids(_cp))

            # Always try live fetch for custom providers — config entries are a
            # fallback, not a replacement.  The live endpoint should return ALL
            # models the key has access to, not just what's listed in config.yaml.
            if provider == "custom" or provider.startswith("custom:"):
                _base_url = None
                _api_key = None
                if custom_provider_entry:
                    _base_url = custom_provider_entry.get("base_url")
                    _api_key = _custom_provider_api_key(custom_provider_entry)
                else:
                    _model_cfg = cfg.get("model", {})
                    _base_url = _model_cfg.get("base_url")
                    _api_key = _model_cfg.get("api_key")
                # Fallback: try credential pool for base_url + api_key
                if (not _base_url or not _api_key) and provider.startswith("custom:"):
                    try:
                        from api.config import _has_explicit_pool_credentials

                        if _has_explicit_pool_credentials(provider):
                            from agent.credential_pool import load_pool as _lpool
                            _resolved = _resolve_provider_alias(provider)
                            _lm_pool = _lpool(_resolved)
                            if _lm_pool:
                                _lm_entry = _lm_pool.select()
                                if _lm_entry:
                                    if not _api_key:
                                        _api_key = getattr(_lm_entry, "runtime_api_key", "") or ""
                                    if not _base_url:
                                        _base_url = str(getattr(_lm_entry, "base_url", "") or "").strip()
                    except ImportError:
                        pass
                if _base_url and _api_key:
                    try:
                        _body = _fetch_live_models_payload(
                            _base_url,
                            _api_key,
                            timeout=CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS,
                            append_v1=True,
                            # Existing product contract: a matching named custom
                            # provider is an explicit trust decision and may target
                            # a LAN/Docker endpoint. Legacy model.base_url is not.
                            allow_configured_private=custom_provider_entry is not None,
                        )

                        # Parse response: {"data": [{"id": "model1", ...}, ...]}
                        if isinstance(_body, dict):
                            _data = _body.get("data", [])
                            if isinstance(_data, list):
                                ids = [m.get("id", "") for m in _data if m.get("id")]
                        elif isinstance(_body, list):
                            ids = [m.get("id", m) if isinstance(m, dict) else m for m in _body]

                        if ids:
                            logger.debug("Live-fetched %d models from custom provider %s", len(ids), _base_url)
                        else:
                            logger.debug("Custom provider returned no models from %s", _base_url)

                    except Exception as _fetch_err:
                        logger.debug("Live fetch from custom provider failed: %s", _fetch_err)

                # If live fetch succeeded, merge with config entries (live takes
                # priority).  If live fetch failed, fall back to config-only list.
                if ids:
                    _live_set = set(ids)
                    for _cid in _config_ids:
                        if _cid not in _live_set:
                            ids.append(_cid)
                else:
                    ids = list(_config_ids)

        # ── OpenAI-compat live fetch fallback ──────────────────────────────────
        # When provider_model_ids() is unavailable or returns [] for a provider
        # that exposes a standard /v1/models endpoint, fetch directly.  This
        # eliminates the need to keep _PROVIDER_MODELS in sync for providers
        # that have a discoverable API (#871).
        #
        # WARNING: This uses synchronous urllib.request which blocks the worker
        # thread for up to 8 seconds on timeout. This is acceptable because:
        #  (a) the server uses threading (not async), so other requests continue;
        #  (b) the frontend shows the static list immediately and enriches in
        #      the background via _fetchLiveModels(), so the user never waits.
        if not ids:
            _ep = _OPENAI_COMPAT_ENDPOINTS.get(provider)
            if _ep:
                try:
                    _providers_cfg = cfg.get("providers") or {}
                    _prov = _providers_cfg.get(provider, {}) if isinstance(_providers_cfg, dict) else {}
                    # Only use a provider-scoped key.  A top-level model.api_key
                    # is safe here only when it belongs to the requested provider;
                    # otherwise /api/models/live?provider=<other> could forward
                    # the active provider's credential to the wrong third party.
                    _key = _prov.get("api_key") if isinstance(_prov, dict) else None
                    if not _key:
                        _model_cfg = cfg.get("model", {})
                        if isinstance(_model_cfg, dict):
                            _active_provider = _resolve_provider_alias(
                                (_model_cfg.get("provider") or "").strip().lower()
                            )
                            if _active_provider == provider:
                                _key = _model_cfg.get("api_key")
                    if _key:
                        _body = _fetch_live_models_payload(
                            _ep,
                            _key,
                            timeout=8,
                            append_v1=False,
                            allow_configured_private=False,
                        )
                        ids = [m.get("id", "") for m in _body.get("data", []) if m.get("id")]
                        logger.debug("Live-fetched %d models from %s /v1/models", len(ids), provider)
                except Exception as _fetch_err:
                    logger.debug("Live fetch from %s failed: %s", provider, _fetch_err)
                    # Fall through to static list below

        # Static fallback — only reached when live fetch also failed.
        if not ids:
            from api.config import _PROVIDER_MODELS as _pm
            ids = [m["id"] for m in _pm.get(provider, [])]
        if not ids:
            return _finish({"provider": provider, "models": [], "count": 0})

        # Match the same dropdown visibility budget that /api/models uses so
        # background enrichment via _fetchLiveModels() does not re-append an
        # uncapped catalog after the initial picker render. The full catalog
        # still comes from /api/models via extra_models for search/show-all;
        # this endpoint is only a dropdown-enrichment surface. (#1567, #3691)
        if provider == "nous":
            try:
                from api.config import _build_nous_featured_set
                _default_model = (cfg.get("model", {}) or {}).get("model") if isinstance(cfg.get("model"), dict) else None
                _featured, _ = _build_nous_featured_set(ids, selected_model_id=_default_model)
                ids = _featured
            except Exception:
                logger.debug("Failed to apply Nous featured-set cap for /api/models/live")
        else:
            from api.config import _MODEL_PICKER_OVERFLOW_THRESHOLD, _MODEL_PICKER_VISIBLE_TARGET
            if len(ids) > _MODEL_PICKER_OVERFLOW_THRESHOLD:
                ids = ids[:_MODEL_PICKER_VISIBLE_TARGET]

        # Normalise to {id, label} — provider_model_ids() returns plain string IDs.
        # For ollama-cloud use the shared Ollama formatter (handles `:variant` suffix).
        # For all other providers use a simpler hyphen-split capitaliser.
        from api.config import (
            _format_ollama_label as _fmt_ollama,
            _is_openai_family_provider as _is_fast_tier_provider,
            _model_supports_fast_tier_for_provider,
        )

        def _make_label(mid):
            """Best-effort human label from a model ID string."""
            if provider in ("ollama", "ollama-cloud"):
                return _fmt_ollama(mid)
            # Preserve slashes for router IDs like "anthropic/claude-sonnet-4.6"
            display = mid.split("/")[-1] if "/" in mid else mid
            parts = display.split("-")
            result = []
            for p in parts:
                pl = p.lower()
                if pl == "gpt":
                    result.append("GPT")
                elif pl in ("claude", "gemini", "gemma", "llama", "mistral",
                            "qwen", "deepseek", "grok", "kimi", "glm"):
                    result.append(p.capitalize())
                elif p[:1].isdigit():
                    result.append(p)  # version numbers: 5.4, 3.5, 4.6 — unchanged
                else:
                    result.append(p.capitalize())
            label = " ".join(result)
            # Restore well-known uppercase tokens that title-casing breaks
            for orig in ("GPT", "GLM", "API", "AI", "XL", "MoE"):
                label = label.replace(orig.title(), orig)
            return label

        annotate_fast_tier = _is_fast_tier_provider(provider)
        models_out = []
        for mid in ids:
            if not mid:
                continue
            entry = {"id": mid, "label": _make_label(mid)}
            if annotate_fast_tier:
                entry["supports_fast_tier"] = _model_supports_fast_tier_for_provider(mid, provider)
            models_out.append(entry)
        return _finish({"provider": provider, "models": models_out,
                        "count": len(models_out)})

    except Exception as _e:
        logger.debug("_handle_live_models failed for %s: %s", provider, _e)
        return j(handler, {"error": str(_e), "models": []})

__routes_exports__ = (
    "_OPENAI_COMPAT_ENDPOINTS",
    "_LIVE_MODELS_CACHE_TTL",
    "_LIVE_MODELS_CACHE",
    "_LIVE_MODELS_CACHE_LOCK",
    "_LIVE_MODELS_LOOPBACK_HOSTS",
    "_live_models_address_is_global",
    "_live_models_address_is_loopback",
    "_resolve_live_models_addresses",
    "_prepare_live_models_target",
    "_NoRedirectLiveModelsHandler",
    "_PinnedLiveModelsHTTPConnection",
    "_PinnedLiveModelsHTTPSConnection",
    "_PinnedLiveModelsHTTPHandler",
    "_PinnedLiveModelsHTTPSHandler",
    "_open_live_models_request",
    "_fetch_live_models_payload",
    "_active_profile_for_live_models_cache",
    "_live_models_cache_key",
    "_get_cached_live_models",
    "_set_cached_live_models",
    "_clear_live_models_cache",
    "_handle_live_models",
)
