"""Disk I/O, validation, fingerprinting, and invalidation for model catalogs."""

import copy
import hashlib
import json
import logging
import os
from pathlib import Path

from api import config as _config_module
from api.config.catalog_state import (
    MODEL_CATALOG_STATE,  # noqa: F401 - public canonical-owner re-export
    import_legacy_model_catalog_state,
    publish_legacy_model_catalog_state,
)


def _sync_legacy_state() -> None:
    import_legacy_model_catalog_state(_config_module)


def _sync_models_cache_provenance_owner() -> None:
    from api.config.model_catalog import _sync_models_cache_provenance

    _sync_models_cache_provenance()


def _get_models_cache_path() -> Path:
    _sync_legacy_state()
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

    The named-profile path is derived from ``MODEL_CATALOG_STATE.models_cache_path`` (the
    module-level default), not from ``STATE_DIR`` directly, so the path stays
    correct if the default is repointed (e.g. tests monkeypatch
    ``MODEL_CATALOG_STATE.models_cache_path`` to an isolated tmp file).
    """
    from api.config.snapshot import resolve_config_snapshot

    return resolve_config_snapshot(config_data={}).models_cache_path


def _get_auth_store_path() -> Path:
    """Return the auth.json path for the active Hermes profile."""
    from api.config.snapshot import resolve_config_snapshot

    return resolve_config_snapshot(config_data={}).auth_store_path


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
    api = _config_module
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
    api = _config_module
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
    stripped = _config_module._strip_volatile_auth_fields(raw)
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
    api = _config_module
    return {
        "config_yaml": api._models_cache_file_fingerprint(api._get_config_path()),
        "auth_json": api._auth_store_semantic_fingerprint(api._get_auth_store_path()),
        "catalog": api._models_cache_catalog_fingerprint(),
    }


def _delete_models_cache_on_disk() -> None:
    try:
        os.unlink(str(_config_module._get_models_cache_path()))
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
      1. ``_schema_version`` matches `MODEL_CATALOG_STATE.models_cache_schema_version`. A bumped
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
    api = _config_module
    if not api._is_valid_models_cache(cache):
        return False
    if not isinstance(cache, dict):  # appease type-narrowing — already guarded above
        return False
    cached_schema = cache.get("_schema_version")
    if cached_schema != api.MODEL_CATALOG_STATE.models_cache_schema_version:
        # DEBUG telemetry per stage-294 absorption: makes "why did my cache
        # rebuild" investigations one log-grep away.
        api.logger.debug(
            "models cache rejected: schema=%r vs runtime=%r",
            cached_schema,
            api.MODEL_CATALOG_STATE.models_cache_schema_version,
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
    _sync_legacy_state()
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

        api = _config_module
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
        raw_aliases = _config_module.cfg.get("model", {}).get("aliases", {})
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

        api = _config_module
        cache_path = api._get_models_cache_path()
        if not cache_path.exists():
            return None
        with open(cache_path, encoding="utf-8") as f:
            cache = _j.load(f)
        if not api._is_valid_models_cache(cache):
            return None
        if cache.get("_schema_version") != api.MODEL_CATALOG_STATE.models_cache_schema_version:
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
    _sync_legacy_state()
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
        api = _config_module
        if not api._is_valid_models_cache(cache):
            return
        payload = {
            "_schema_version": api.MODEL_CATALOG_STATE.models_cache_schema_version,
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
    _sync_legacy_state()
    """Return a valid fresh in-memory /api/models cache, or clear stale shapes."""
    api = _config_module
    if api.MODEL_CATALOG_STATE.available_models_cache is None:
        return None
    if (now - api.MODEL_CATALOG_STATE.available_models_cache_ts) >= api.MODEL_CATALOG_STATE.available_models_cache_ttl:
        return None
    current_sources = api._models_cache_source_fingerprint()
    if api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint != current_sources:
        api.logger.debug(
            "models memory cache rejected: source_fingerprint=%r vs runtime=%r",
            api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint,
            current_sources,
        )
        api.MODEL_CATALOG_STATE.available_models_cache = None
        api.MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
        _sync_models_cache_provenance_owner()
        return None
    if api._is_valid_models_cache(api.MODEL_CATALOG_STATE.available_models_cache):
        return api._annotate_fast_tier_model_groups(
            copy.deepcopy(api.MODEL_CATALOG_STATE.available_models_cache)
        )
    api.MODEL_CATALOG_STATE.available_models_cache = None
    api.MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
    api.MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
    api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
    _sync_models_cache_provenance_owner()
    return None


def _models_cache_file_age_seconds(cache_path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - cache_path.stat().st_mtime)
    except OSError:
        return None


def warm_models_catalog_provenance_if_cold() -> None:
    _sync_legacy_state()
    """Best-effort, NON-BLOCKING, disk-only publish of catalog provenance.

    The send path (``api/streaming.py``) resolves the wire model via
    ``resolve_model_provider`` without ever building the models catalog, and the
    #1855 chat/start fast path deliberately skips the catalog when a session
    already carries a persisted model+provider — so "cold at send" is the
    designed behaviour after any process restart, memory-TTL expiry, or cache
    invalidation, not a rare race. In that state the custom-proxy provenance
    signal (``_endpoint_advertised_model_ids``) is ``None`` and resolution falls
    to the cold-preserve default; this helper restores the endpoint-advertised
    signal from the durable disk cache so the #433 bare-only-strip stays exact.

    Deliberately does NOT call ``get_available_models(prefer_cache=True)``: even
    in prefer-cache mode that acquires ``MODEL_CATALOG_STATE.available_models_cache_lock`` and can
    block up to ~60s waiting on an in-flight rebuild (unbounded in synchronous
    rebuild mode) — unacceptable on the send hot path. Instead this:
      * tries the cache lock NON-BLOCKING and returns immediately if it's busy
        (a concurrent rebuild will publish provenance itself);
      * reads ONLY the on-disk cache (no network, no live probe, no rebuild);
      * publishes only the snapshot + source fingerprint provenance pair used
        by the resolver, leaving the full in-memory catalog cold so a later
        request can still perform its normal live rebuild.
    Publishing the fingerprint from the CURRENT runtime is correct: the disk
    cache is validated by schema/version/source-fingerprint on load
    (``_is_loadable_disk_cache``), so a load success means it belongs to this
    profile. Callers must not hold ``_cfg_lock`` (this reads config for the
    fingerprint); the send worker satisfies that.

    Profile isolation: the fast no-op is taken ONLY when the resident provenance
    fingerprint matches the CURRENT profile's runtime fingerprint. The catalog
    globals are process-wide, so a concurrently-active profile B could have left
    its own (or a stale) provenance resident; an unconditional non-``None``
    early return would let B's catalog block profile A from loading A's own valid
    disk cache (A would then resolve against B's advertised ids). Comparing the
    published fingerprint to the current one before short-circuiting closes that
    hole — a mismatch falls through to load THIS profile's disk snapshot.
    """
    api = _config_module

    def _provenance_is_current() -> bool:
        prov = api.MODEL_CATALOG_STATE.models_cache_provenance
        if prov is None:
            return False
        try:
            return prov[1] == api._models_cache_source_fingerprint()
        except Exception:
            return False

    if _provenance_is_current():
        return  # already warm for THIS profile — one global read, no work
    got = api.MODEL_CATALOG_STATE.available_models_cache_lock.acquire(blocking=False)
    if not got:
        return  # a concurrent build/publish holds the lock; it will publish
    try:
        if _provenance_is_current():
            return  # published for this profile while we waited for the lock
        try:
            disk_groups = api._load_models_cache_from_disk()
        except Exception:
            disk_groups = None
        if disk_groups is None:
            return  # no durable cache for this profile → stay cold, preserve verbatim
        current_fingerprint = api._models_cache_source_fingerprint()
        api.MODEL_CATALOG_STATE.models_cache_provenance = (disk_groups, current_fingerprint)
        api.MODEL_CATALOG_STATE.advertised_model_ids_memo = None
        publish_legacy_model_catalog_state(api)
    except Exception:
        api.logger.debug("models catalog provenance warm failed", exc_info=True)
    finally:
        api.MODEL_CATALOG_STATE.available_models_cache_lock.release()


def get_available_models_for_session_visit() -> dict:
    _sync_legacy_state()
    """Return /api/models with a short session-visit freshness horizon.

    perf(session-load-latency) Phase 0: this function is the source of the
    multi-second `/api/models?freshness=session_visit` latency. Stage markers
    feed into RequestDiagnostics when called from /api/models; standalone
    callers get the same envelope via the local _stagelog dict.
    """
    import time as _time

    api = _config_module
    _stagelog: list[tuple[str, float]] = [("enter", _time.monotonic())]

    def _mark(name: str) -> None:
        _stagelog.append((name, _time.monotonic()))

    _logger = logging.getLogger("api.config")
    # HERMES_DEBUG_SLOW: a numeric value sets the slow-log threshold in ms; any
    # other non-empty (truthy) value — e.g. the documented `HERMES_DEBUG_SLOW=1`
    # / `=true` — means "always log stage timing" (0ms threshold); unset/empty
    # keeps the default 500ms. Must be non-throwing: a nonnumeric truthy value
    # like `true` previously raised ValueError here and 500'd this hot path.
    _slow_raw = (api.os.environ.get("HERMES_DEBUG_SLOW", "") or "").strip()
    if not _slow_raw:
        _slow_threshold_ms = 500.0
    else:
        try:
            _slow_threshold_ms = float(_slow_raw) or 500.0
        except ValueError:
            # Non-numeric truthy flag (e.g. "true"): always emit stage timing.
            _slow_threshold_ms = 0.0

    cache_path = api._get_models_cache_path()
    cache_age = api._models_cache_file_age_seconds(cache_path, _time.time())
    _mark(f"disk_age_check:{cache_age}")
    disk_cached = None
    if (
        cache_age is not None
        and cache_age < api.MODEL_CATALOG_STATE.session_visit_models_freshness_seconds
    ):
        _mark("cache_age_within_ttl")
        now_mono = _time.monotonic()
        with api.MODEL_CATALOG_STATE.available_models_cache_lock:
            cached = api._get_fresh_memory_models_cache(now_mono)
            if cached is not None:
                _mark("memory_cache_hit")
                api._maybe_log_slow_stages(
                    _logger,
                    _stagelog,
                    _slow_threshold_ms,
                    "models.session_visit",
                )
                return cached
        _mark("memory_cache_miss_loading_disk")
        disk_cached = api._load_models_cache_from_disk()
        if disk_cached is not None:
            with api.MODEL_CATALOG_STATE.available_models_cache_lock:
                cached = api._get_fresh_memory_models_cache(_time.monotonic())
                if cached is not None:
                    _mark("disk_then_memory_cache_hit")
                    api._maybe_log_slow_stages(
                        _logger,
                        _stagelog,
                        _slow_threshold_ms,
                        "models.session_visit",
                    )
                    return cached
                api.MODEL_CATALOG_STATE.available_models_cache = api.copy.deepcopy(disk_cached)
                api.MODEL_CATALOG_STATE.available_models_cache_ts = _time.monotonic()
                api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = (
                    api._models_cache_source_fingerprint()
                )
                _sync_models_cache_provenance_owner()
            _mark("disk_cache_returned")
            api._maybe_log_slow_stages(
                _logger,
                _stagelog,
                _slow_threshold_ms,
                "models.session_visit",
            )
            return api.copy.deepcopy(disk_cached)

    _mark("cache_age_stale_or_missing")
    stale_cached = disk_cached or api._load_stale_models_cache_from_disk()
    _mark(f"stale_cached_loaded:{bool(stale_cached)}")
    try:
        _mark("force_refresh_start")
        result = api.get_available_models(force_refresh=True)
        _mark("force_refresh_done")
        api._maybe_log_slow_stages(
            _logger, _stagelog, _slow_threshold_ms, "models.session_visit"
        )
        return result
    except Exception:
        _mark("force_refresh_failed")
        api.logger.debug("session-visit models refresh failed", exc_info=True)
        if stale_cached is not None:
            _mark("stale_fallback_return")
            api._maybe_log_slow_stages(
                _logger,
                _stagelog,
                _slow_threshold_ms,
                "models.session_visit",
            )
            return api.copy.deepcopy(stale_cached)
        _mark("prefer_cache_fallback")
        api._maybe_log_slow_stages(
            _logger, _stagelog, _slow_threshold_ms, "models.session_visit"
        )
        return api.get_available_models(prefer_cache=True)


def _maybe_log_slow_stages(
    logger_obj: "logging.Logger",
    stagelog: "list[tuple[str, float]]",
    threshold_ms: float,
    tag: str,
) -> None:
    """perf(session-load-latency) Phase 0: per-stage timing reporter.

    Emits a single log line listing every stage with its delta in ms when
    the function's total wall time crosses ``threshold_ms``. Lives next to
    the model cache code so it has zero coupling with the WebUI request
    layer; called from both ``get_available_models_for_session_visit`` and
    (Phase 1) the chat session load path.
    """
    if len(stagelog) < 2:
        return
    total_ms = (stagelog[-1][1] - stagelog[0][1]) * 1000.0
    if total_ms < threshold_ms:
        return
    parts: list[str] = []
    for i in range(1, len(stagelog)):
        prev_t = stagelog[i - 1][1]
        cur_t = stagelog[i][1]
        parts.append(f"{stagelog[i][0]}={((cur_t - prev_t) * 1000.0):.1f}ms")
    try:
        logger_obj.warning(
            "[SLOW] %s total=%.1fms stages: %s",
            tag,
            total_ms,
            " ".join(parts),
        )
    except Exception:
        # Logging must never break a response path.
        pass


def invalidate_models_cache():
    _sync_legacy_state()
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
    api = _config_module
    with api.MODEL_CATALOG_STATE.available_models_cache_lock:
        api.MODEL_CATALOG_STATE.available_models_cache = None
        api.MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
        _sync_models_cache_provenance_owner()
        api._invalidate_models_build_locked()
        # Clear the credential pool cache too (all profiles). Without this,
        # tests (and live provider key edits) see a stale CredentialPool from a
        # prior auth_store payload — the test_credential_pool_providers suite was
        # hitting this directly. A full reset is intentionally profile-wide.
        api.MODEL_CATALOG_STATE.credential_pool_cache.clear()
    # Also delete the disk cache so the next cold build starts fresh.
    # Disk delete is outside the lock — file I/O shouldn't block other readers.
    api._delete_models_cache_on_disk()
    publish_legacy_model_catalog_state(api)
    try:
        from api.config.plugin_providers import invalidate_plugin_model_provider_cache

        invalidate_plugin_model_provider_cache()
    except Exception:
        pass


def invalidate_credential_pool_cache(provider_id: str):
    _sync_legacy_state()
    """Invalidate the credential pool cache for a specific provider.

    Used by the streaming layer's credential self-heal logic (#1401) to
    force a fresh credential pool load after re-reading auth.json.
    """
    api = _config_module
    with api.MODEL_CATALOG_STATE.available_models_cache_lock:
        _cp_tag = api._credential_pool_profile_tag()
        api.MODEL_CATALOG_STATE.credential_pool_cache.pop((_cp_tag, provider_id), None)
        api.MODEL_CATALOG_STATE.credential_pool_cache.pop(
            (_cp_tag, api._resolve_provider_alias(provider_id)), None
        )
    from api.config.hooks import get_config_runtime_hooks

    get_config_runtime_hooks().credential_cache_invalidated(provider_id)


def invalidate_provider_models_cache(provider_id: str):
    _sync_legacy_state()
    """Invalidate cached models for a single provider.

    Also invalidates the full cache so that the next get_available_models()
    call rebuilds all groups cleanly.

    Args:
        provider_id: canonical provider id (e.g. 'openai', 'anthropic', 'custom:my-key')
    """
    api = _config_module
    with api.MODEL_CATALOG_STATE.available_models_cache_lock:
        api.MODEL_CATALOG_STATE.available_models_cache = None
        api.MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
        api.MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
        _sync_models_cache_provenance_owner()
        api._invalidate_models_build_locked()
        # Also evict the credential pool so the next cold path re-loads it.
        # Must evict both the original key and its canonical form (load_pool
        # may be called with either, and both paths cache under their own key),
        # scoped to the active profile's cache key.
        _cp_tag = api._credential_pool_profile_tag()
        api.MODEL_CATALOG_STATE.credential_pool_cache.pop((_cp_tag, provider_id), None)
        api.MODEL_CATALOG_STATE.credential_pool_cache.pop(
            (_cp_tag, api._resolve_provider_alias(provider_id)), None
        )
    api._delete_models_cache_on_disk()
    publish_legacy_model_catalog_state(api)
