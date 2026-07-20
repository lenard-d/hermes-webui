"""Disk I/O, validation, fingerprinting, and invalidation for model catalogs."""

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from api.config_parts.facade import config_api


def _get_models_cache_path() -> Path:
    """Return the /api/models disk-cache path for the *active* profile (#3957).

    WebUI profile switching is per-client/cookie scoped (issue #798), but the
    models disk cache used to be a single import-time ``STATE_DIR /
    "models_cache.json"`` shared across every profile.  The cache's
    ``_source_fingerprint`` is profile-specific (it hashes the active profile's
    config.yaml + auth.json), so a non-default profile rejected the shared
    snapshot on every read and cold-rebuilt the catalog — the serial live
    provider probes behind that cold build are what pushed ``/api/models`` (and
    the Settings → Providers panel) past the 30s frontend timeout.

    Profile-key the filename so each profile keeps its own warm cache:
      - default / root profile  → ``models_cache.json``  (unchanged path; no
        migration of the existing file)
      - named profile ``<name>`` → ``models_cache.<name>.json``

    The active profile is resolved per-request via ``get_active_profile_name()``
    (thread-local cookie context), falling back to the module-level default
    path if the profiles module is unavailable (very early boot / import cycle).

    The named-profile path is derived from ``_models_cache_path`` (the
    module-level default), not from ``STATE_DIR`` directly, so the path stays
    correct if the default is repointed (e.g. tests monkeypatch
    ``_models_cache_path`` to an isolated tmp file).
    """
    api = config_api()
    try:
        from api.profiles import get_active_profile_name, _is_root_profile

        name = (get_active_profile_name() or "").strip()
        if not name or _is_root_profile(name):
            return api._models_cache_path
        # Defensive filename sanitization: the cookie-derived profile name is
        # already validated by _PROFILE_ID_RE at the request boundary, but keep
        # the on-disk filename safe regardless of how the name was resolved.
        safe = re.sub(r"[^a-z0-9_-]", "_", name.lower())[:64]
        if not safe:
            return api._models_cache_path
        # Splice the profile into the default filename: models_cache.json →
        # models_cache.<safe>.json, keeping the default's parent dir + suffix.
        base = api._models_cache_path
        return base.with_name(f"{base.stem}.{safe}{base.suffix}")
    except Exception:
        return api._models_cache_path


def _get_auth_store_path() -> Path:
    """Return the auth.json path for the active Hermes profile."""
    try:
        from api.profiles import get_active_hermes_home as _gah

        return _gah() / "auth.json"
    except ImportError:
        return config_api()._DEFAULT_HERMES_HOME / "auth.json"


def _models_cache_file_fingerprint(path: Path) -> dict:
    """Return non-secret identity metadata for a cache dependency file.

    The /api/models response depends on config.yaml (model/provider defaults)
    and auth.json (active_provider + credential_pool).  The cache only needs
    cheap invalidation signals here, not file contents; never include secrets.
    """
    fingerprint = {"path": str(Path(path).expanduser())}
    try:
        st = Path(path).stat()
    except OSError:
        fingerprint["missing"] = True
        return fingerprint
    fingerprint["mtime_ns"] = st.st_mtime_ns
    fingerprint["size"] = st.st_size
    return fingerprint


def _models_cache_catalog_fingerprint() -> dict:
    """Return non-secret model-catalog identity metadata for cache invalidation.

    The /api/models payload is not only a function of user config/auth files.
    It also depends on the provider/model catalog baked into this module and on
    small local catalogs such as Codex's models_cache.json. Keep this cheap and
    deterministic so a server restart after catalog changes does not keep
    serving an otherwise-valid persisted models_cache.json until the 24h TTL
    expires (#2443).
    """
    api = config_api()
    catalog_payload = {
        "provider_models": api._PROVIDER_MODELS,
        "provider_display": api._PROVIDER_DISPLAY,
    }
    try:
        encoded = json.dumps(
            catalog_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        provider_catalog_sha = hashlib.sha256(encoded).hexdigest()
    except Exception:
        provider_catalog_sha = "unavailable"

    codex_home = Path(
        os.getenv("CODEX_HOME", "").strip() or (api.HOME / ".codex")
    ).expanduser()
    return {
        "provider_catalog_sha256": provider_catalog_sha,
        "codex_models_cache": api._models_cache_file_fingerprint(
            codex_home / "models_cache.json"
        ),
    }


# Credential-rotation fields inside auth.json that churn on a ~14-minute
# period (credential-pool / OAuth token refresh rewrites the whole file) but
# DO NOT change the set of available providers or models that /api/models
# returns. mtime/size-based fingerprinting (#1699's _models_cache_file_
# fingerprint) treats every one of these rewrites as a cache-invalidating
# change, so the 24h models cache is effectively dead — every few minutes a
# tab pays a full cold get_available_models() rebuild (see RCA t_d127953d /
# t_16551f61). We strip ONLY these known-inert fields and fingerprint the
# rest of auth.json by content, so token rotation no longer busts the cache.
#
# This is a DENY-list, not an allow-list, on purpose: a field we don't know
# about stays IN the fingerprint, so any genuine change to provider
# enablement / endpoint / api-base / model-allow (active_provider, a NEW
# credential_pool entry id, base_url, source, label, key_source, auth_type,
# priority, the providers{} block, …) still correctly invalidates the cache.
# The safety invariant is one-directional: excluding these fields can only
# ever make the fingerprint MORE stable, never make it miss a real
# provider/model-set change — because none of these fields feed
# detected_providers / the catalog in _build_available_models_uncached().
_AUTH_FINGERPRINT_VOLATILE_KEYS = frozenset(
    {
        # Secret material — rotates on refresh, never gates the provider/model set.
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "secret",
        "client_secret",  # rotation-only on purpose; not a model-cache differentiator
        # Expiry / liveness — bumped every refresh, derived from the token above.
        "expires_at",
        "expires_at_ms",
        "expires_in",
        # Per-credential status/telemetry — churns on every request, not config.
        "last_status",
        "last_status_at",
        "last_error_code",
        "last_error_reason",
        "last_error_message",
        "last_error_reset_at",
        "request_count",
        # Whole-file save timestamp — rewritten on every _save_auth_store().
        "updated_at",
    }
)


def _strip_volatile_auth_fields(obj):
    """Recursively drop credential-rotation-only keys from an auth.json tree.

    Pure structural transform; never mutates the input. Any key NOT in the
    deny-list is preserved verbatim so real provider/endpoint changes still
    show through in the fingerprint.
    """
    api = config_api()
    if isinstance(obj, dict):
        return {
            k: api._strip_volatile_auth_fields(v)
            for k, v in obj.items()
            if k not in api._AUTH_FINGERPRINT_VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [api._strip_volatile_auth_fields(v) for v in obj]
    return obj


def _auth_store_semantic_fingerprint(path: Path) -> dict:
    """Return a content fingerprint of auth.json that ignores token churn.

    Unlike _models_cache_file_fingerprint() (mtime_ns + size), this hashes
    the JSON content with the credential-rotation fields stripped, so the
    ~14-min token-refresh rewrite of auth.json does NOT invalidate the 24h
    /api/models cache. A change to anything that actually affects the
    provider/model set (active_provider, a new credential_pool entry, a
    changed base_url/source/label/auth_type, the providers{} block, …)
    still changes the hash and correctly busts the cache.

    Failure modes are deliberately conservative — if the file is missing we
    record that, and if it can't be read/parsed we fall back to the old
    mtime/size fingerprint so behaviour is never *less* safe than #1699.
    """
    p = Path(path).expanduser()
    fp: dict = {"path": str(p)}
    try:
        st = p.stat()
    except OSError:
        fp["missing"] = True
        return fp
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Unreadable / corrupt / mid-write: fall back to the stat-based
        # fingerprint. Strictly no less safe than the pre-fix behaviour
        # (every write still invalidates) for this rare path only.
        fp["mtime_ns"] = st.st_mtime_ns
        fp["size"] = st.st_size
        fp["semantic"] = "unparsed-fallback"
        return fp
    stripped = config_api()._strip_volatile_auth_fields(raw)
    try:
        encoded = json.dumps(
            stripped,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        fp["semantic_sha256"] = hashlib.sha256(encoded).hexdigest()
    except Exception:
        fp["mtime_ns"] = st.st_mtime_ns
        fp["size"] = st.st_size
        fp["semantic"] = "encode-fallback"
    return fp


def _models_cache_source_fingerprint() -> dict:
    """Return the current config/auth/catalog fingerprint for /api/models cache.

    The auth.json axis uses a *content* fingerprint that excludes pure
    credential-rotation fields (see _auth_store_semantic_fingerprint): the
    auth store is rewritten roughly every 14 minutes by token refresh, and
    a stat-based (mtime/size) fingerprint made the 24h cache churn on every
    one of those rewrites (RCA t_16551f61). config.yaml keeps the cheap
    mtime/size fingerprint because it is only rewritten on deliberate user
    edits (which can change anything) and does not churn on a timer.
    """
    api = config_api()
    return {
        "config_yaml": api._models_cache_file_fingerprint(api._get_config_path()),
        "auth_json": api._auth_store_semantic_fingerprint(api._get_auth_store_path()),
        "catalog": api._models_cache_catalog_fingerprint(),
    }


def _delete_models_cache_on_disk() -> None:
    try:
        os.unlink(str(config_api()._get_models_cache_path()))
    except OSError:
        pass  # already absent


def _is_valid_models_cache(cache: object) -> bool:
    """Return True when a cache payload has the full /api/models shape.

    SHAPE-only check: validates structural correctness of an in-memory or
    on-disk cache. Use _is_loadable_disk_cache() for the strictness needed
    when reading from disk (it adds version-stamp invalidation per #1633).

    Kept loose so in-memory cache writes (which never touch disk and so don't
    need version stamping) can use this validator unchanged.
    """
    if not isinstance(cache, dict):
        return False
    if not {
        "active_provider",
        "default_model",
        "configured_model_badges",
        "groups",
    }.issubset(cache):
        return False
    active_provider = cache.get("active_provider")
    return (
        (active_provider is None or isinstance(active_provider, str))
        and isinstance(cache.get("default_model"), str)
        and isinstance(cache.get("configured_model_badges"), dict)
        and isinstance(cache.get("groups"), list)
    )


def _is_loadable_disk_cache(cache: object) -> bool:
    """Return True when an on-disk cache is safe to use after a process boot.

    Adds two checks on top of _is_valid_models_cache (#1633):
      1. ``_schema_version`` matches `_MODELS_CACHE_SCHEMA_VERSION`. A bumped
         schema version unconditionally invalidates older cache files.
      2. ``_webui_version`` matches the current runtime version. Forces a
         rebuild after every release so users see picker-shape fixes
         immediately, instead of waiting up to 24 hours for the TTL to expire.
         If the runtime version cannot be resolved (early-init edge case),
         skip this check rather than wedge the boot.

    Note: ``_webui_version`` is a string equality check, not a semver compare —
    two debug builds with the same `WEBUI_VERSION` string but different actual
    code wouldn't invalidate via this axis. ``_schema_version`` is the
    independent invalidation axis for breaking changes that lack a tag bump;
    bump it whenever the cache shape changes incompatibly.
    """
    api = config_api()
    if not api._is_valid_models_cache(cache):
        return False
    if not isinstance(cache, dict):  # appease type-narrowing — already guarded above
        return False
    cached_schema = cache.get("_schema_version")
    if cached_schema != api._MODELS_CACHE_SCHEMA_VERSION:
        # DEBUG telemetry per stage-294 absorption: makes "why did my cache
        # rebuild" investigations one log-grep away.
        api.logger.debug(
            "models cache rejected: schema=%r vs runtime=%r",
            cached_schema,
            api._MODELS_CACHE_SCHEMA_VERSION,
        )
        return False
    runtime_version = api._current_webui_version()
    if runtime_version is not None:
        cached_version = cache.get("_webui_version")
        if not isinstance(cached_version, str) or cached_version != runtime_version:
            api.logger.debug(
                "models cache rejected: webui_version=%r vs runtime=%r",
                cached_version,
                runtime_version,
            )
            return False
    cached_sources = cache.get("_source_fingerprint")
    runtime_sources = api._models_cache_source_fingerprint()
    if cached_sources != runtime_sources:
        api.logger.debug(
            "models cache rejected: source_fingerprint=%r vs runtime=%r",
            cached_sources,
            runtime_sources,
        )
        return False
    return True


def _load_models_cache_from_disk() -> dict | None:
    """Load /api/models cache from disk if it exists and has current metadata.

    Adds the per-release version check from #1633: a cache stamped with a
    different WebUI version is treated as missing, forcing a fresh rebuild
    that picks up any picker-shape fixes shipped in the new release. The
    returned dict is the SHAPE-only cache (without the `_webui_version` /
    `_schema_version` stamps) so callers don't have to know about the
    on-disk metadata fields.
    """
    try:
        import json as _j

        api = config_api()
        cache_path = api._get_models_cache_path()
        if not cache_path.exists():
            return None
        with open(cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not api._is_loadable_disk_cache(cache):
            return None
        # Strip the disk-only metadata before returning, so the in-memory
        # cache shape stays exactly what the rest of the code expects. The
        # disk save path does not persist `aliases`, so reconstruct them from
        # current config to keep the /api/models.aliases contract intact (a
        # disk-cache hit must not silently drop `/model <alias>` resolution).
        return api._annotate_fast_tier_model_groups(
            {
                "active_provider": cache["active_provider"],
                "default_model": cache["default_model"],
                "configured_model_badges": cache["configured_model_badges"],
                "groups": cache["groups"],
                "aliases": (
                    cache["aliases"]
                    if isinstance(cache.get("aliases"), dict)
                    else api._model_aliases_from_config()
                ),
            }
        )
    except Exception:
        return None


def _model_aliases_from_config() -> dict[str, str]:
    """Build the normalized model-alias map from current config.

    Mirrors the alias construction used by the live and static catalog paths so
    the `/api/models.aliases` contract is consistent across every catalog source
    (live, static, and the stale-disk fallback, which can't read aliases from a
    disk cache that never persisted them).
    """
    try:
        raw_aliases = config_api().cfg.get("model", {}).get("aliases", {})
        if isinstance(raw_aliases, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in raw_aliases.items()
                if k and v
            }
    except Exception:
        pass
    return {}


def _load_stale_models_cache_from_disk() -> dict | None:
    """Load a shape-valid stale /api/models disk cache for timeout fallback only.

    The main cache loader enforces metadata stamps for a full cold-path cache hit.
    This helper intentionally does not apply that stricter policy, so we can still
    recover a useful fallback payload when the strict loader rejected cache because
    metadata or fingerprint fields are stale. It DOES still enforce the schema
    version: a cross-schema cache can have an incompatible groups/badge shape, so
    serving it to the picker could surface a broken catalog — schema mismatch is a
    hard reject even on the fallback path.
    """
    try:
        import json as _j

        api = config_api()
        cache_path = api._get_models_cache_path()
        if not cache_path.exists():
            return None
        with open(cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not api._is_valid_models_cache(cache):
            return None
        if cache.get("_schema_version") != api._MODELS_CACHE_SCHEMA_VERSION:
            return None
        aliases = cache.get("aliases")
        if not isinstance(aliases, dict):
            # The disk cache save path does not persist `aliases`, so a cache
            # read back from disk lacks them. Defaulting to {} would silently
            # break `/model <alias>` slash-command resolution (static/commands.js
            # resolves slash aliases only from /api/models.aliases) for the
            # duration of the over-budget stale fallback. Reconstruct from
            # current config, mirroring the live/static catalog alias build.
            aliases = api._model_aliases_from_config()
        return api._annotate_fast_tier_model_groups(
            {
                "active_provider": cache["active_provider"],
                "default_model": cache["default_model"],
                "configured_model_badges": cache["configured_model_badges"],
                "groups": cache["groups"],
                "aliases": aliases,
            }
        )
    except Exception:
        return None


def _save_models_cache_to_disk(cache: dict) -> None:
    """Save cache to disk so it survives server restarts.

    Stamps the payload with `_webui_version` and `_schema_version` (#1633) so
    a subsequent process running a different WebUI version, or a future
    release that bumps the schema, will treat the file as invalid and
    rebuild from live provider data on its first /api/models call.

    The version stamp is omitted (not the literal None — the field is just
    skipped) when the runtime version cannot be resolved at the moment of
    save, which would happen only in a very early boot path before
    api.updates is loaded. _is_loadable_disk_cache treats a missing field as
    a mismatch (since runtime_version is non-None on every subsequent call),
    so this is safe — at worst we write one cache file that gets rejected
    once on the next boot.
    """
    try:
        api = config_api()
        if not api._is_valid_models_cache(cache):
            return
        payload = {
            "_schema_version": api._MODELS_CACHE_SCHEMA_VERSION,
            "_source_fingerprint": api._models_cache_source_fingerprint(),
            "active_provider": cache["active_provider"],
            "default_model": cache["default_model"],
            "configured_model_badges": cache["configured_model_badges"],
            "groups": cache["groups"],
        }
        runtime_version = api._current_webui_version()
        if runtime_version is not None:
            payload["_webui_version"] = runtime_version
        cache_path = api._get_models_cache_path()
        tmp = str(cache_path) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp, str(cache_path))
    except Exception:
        pass  # Non-fatal -- cache will rebuild on next call


def _get_fresh_memory_models_cache(now: float) -> dict | None:
    """Return a valid fresh in-memory /api/models cache, or clear stale shapes."""
    api = config_api()
    if api._available_models_cache is None:
        return None
    if (now - api._available_models_cache_ts) >= api._AVAILABLE_MODELS_CACHE_TTL:
        return None
    current_sources = api._models_cache_source_fingerprint()
    if api._available_models_cache_source_fingerprint != current_sources:
        api.logger.debug(
            "models memory cache rejected: source_fingerprint=%r vs runtime=%r",
            api._available_models_cache_source_fingerprint,
            current_sources,
        )
        api._available_models_cache = None
        api._available_models_cache_ts = 0.0
        api._available_models_live_rebuild_ts = 0.0
        api._available_models_cache_source_fingerprint = None
        api._sync_models_cache_provenance()
        return None
    if api._is_valid_models_cache(api._available_models_cache):
        return api._annotate_fast_tier_model_groups(
            copy.deepcopy(api._available_models_cache)
        )
    api._available_models_cache = None
    api._available_models_cache_ts = 0.0
    api._available_models_live_rebuild_ts = 0.0
    api._available_models_cache_source_fingerprint = None
    api._sync_models_cache_provenance()
    return None


def invalidate_models_cache():
    """Force the TTL cache for get_available_models() to be cleared.

    Call this after modifying config.cfg in-memory (e.g. in tests) so
    the next call to get_available_models() picks up the changes rather
    than returning a stale cached result.

    Also deletes the on-disk cache so that a subsequent cold build does
    not immediately reload a stale disk snapshot and skip the fresh build.
    This is essential for test isolation: without the disk delete, tests
    that call invalidate_models_cache() still get back the previous test's
    result from the disk cache because the disk hit is checked before the memory
    cache rebuild runs.
    """
    api = config_api()
    with api._available_models_cache_lock:
        api._available_models_cache = None
        api._available_models_cache_ts = 0.0
        api._available_models_live_rebuild_ts = 0.0
        api._available_models_cache_source_fingerprint = None
        api._sync_models_cache_provenance()
        api._invalidate_models_build_locked()
        # Clear the credential pool cache too (all profiles). Without this,
        # tests (and live provider key edits) see a stale CredentialPool from a
        # prior auth_store payload — the test_credential_pool_providers suite was
        # hitting this directly. A full reset is intentionally profile-wide.
        api._CREDENTIAL_POOL_CACHE.clear()
    # Also delete the disk cache so the next cold build starts fresh.
    # Disk delete is outside the lock — file I/O shouldn't block other readers.
    api._delete_models_cache_on_disk()
    try:
        from api.plugin_providers import invalidate_plugin_model_provider_cache

        invalidate_plugin_model_provider_cache()
    except Exception:
        pass


def invalidate_credential_pool_cache(provider_id: str):
    """Invalidate the credential pool cache for a specific provider.

    Used by the streaming layer's credential self-heal logic (#1401) to
    force a fresh credential pool load after re-reading auth.json.
    """
    api = config_api()
    with api._available_models_cache_lock:
        _cp_tag = api._credential_pool_profile_tag()
        api._CREDENTIAL_POOL_CACHE.pop((_cp_tag, provider_id), None)
        api._CREDENTIAL_POOL_CACHE.pop(
            (_cp_tag, api._resolve_provider_alias(provider_id)), None
        )
    try:
        # api.providers imports from api.config; keep this lazy to avoid
        # import-cycle/module-initialization issues.
        from api.providers import invalidate_account_usage_status_cache

        invalidate_account_usage_status_cache(provider_id)
        invalidate_account_usage_status_cache(api._resolve_provider_alias(provider_id))
    except Exception:
        api.logger.debug(
            "Failed to invalidate account usage status cache", exc_info=True
        )


def invalidate_provider_models_cache(provider_id: str):
    """Invalidate cached models for a single provider.

    Also invalidates the full cache so that the next get_available_models()
    call rebuilds all groups cleanly.

    Args:
        provider_id: canonical provider id (e.g. 'openai', 'anthropic', 'custom:my-key')
    """
    api = config_api()
    with api._available_models_cache_lock:
        api._available_models_cache = None
        api._available_models_cache_ts = 0.0
        api._available_models_live_rebuild_ts = 0.0
        api._available_models_cache_source_fingerprint = None
        api._sync_models_cache_provenance()
        api._invalidate_models_build_locked()
        # Also evict the credential pool so the next cold path re-loads it.
        # Must evict both the original key and its canonical form (load_pool
        # may be called with either, and both paths cache under their own key),
        # scoped to the active profile's cache key.
        _cp_tag = api._credential_pool_profile_tag()
        api._CREDENTIAL_POOL_CACHE.pop((_cp_tag, provider_id), None)
        api._CREDENTIAL_POOL_CACHE.pop(
            (_cp_tag, api._resolve_provider_alias(provider_id)), None
        )
    api._delete_models_cache_on_disk()
