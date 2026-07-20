"""Public model-catalog interface and cache publication coordinator.

Catalog data sources, network-free assembly, live discovery, and publication
provenance are owned by focused sibling modules. This module preserves the
historical :mod:`api.config` interface and coordinates cache lifecycle only.
"""

# ruff: noqa: F401, F821 -- compatibility exports intentionally imported here

from __future__ import annotations

import copy
import logging
import threading
import time
from pathlib import Path

from api import config as _config_module
from api.config.catalog_live import build_available_models_uncached
from api.config.catalog_provenance import (
    _endpoint_advertised_model_ids,
    _invalidate_models_build_locked,
    _invoke_models_rebuild,
    _should_warn_budget,
    _sync_models_cache_provenance,
)
from api.config.catalog_sources import (
    _credential_pool_profile_tag,
    _current_webui_version,
    _get_label_for_model,
    _has_explicit_pool_credentials,
    _moa_preset_models_from_config,
    _models_from_live_provider_ids,
    _pool_entry_payloads,
    _read_live_provider_model_ids,
    _read_visible_codex_cache_model_ids,
)
from api.config.catalog_state import (
    MODEL_CATALOG_STATE,
    import_legacy_model_catalog_state,
    publish_legacy_model_catalog_state,
)
from api.config.catalog_static import (
    _configured_model_badges_from_static_catalog,
    _minimal_static_models_catalog,
    _static_models_catalog_without_live_probes,
)

logger = logging.getLogger(__name__)


def get_available_models(*, prefer_cache: bool = False, force_refresh: bool = False) -> dict:
    import_legacy_model_catalog_state(_config_module)
    """
    Return available models grouped by provider.

    Discovery order:
      1. Read config.yaml 'model' section for active provider info
      2. Check for known API keys in env or ~/.hermes/.env
      3. Fetch models from custom endpoint if base_url is configured
      4. Fall back to hardcoded model list (OpenRouter-style)

    Returns: {
        'active_provider': str|None,
        'default_model': str,
        'groups': [{'provider': str, 'models': [{'id': str, 'label': str}]}]
    }

    ``prefer_cache=True`` resolves WITHOUT ever triggering a live provider
    probe: it serves the warm in-memory cache, then the last-known on-disk
    cache, and only as a last resort a network-free minimal catalog
    (config/auth derived). It NEVER does the per-provider live rebuild (the
    Copilot token-exchange HTTPS call et al.). This is the path a
    server-initiated wakeup turn (Option Z) takes so a cold catalog can never
    block the wakeup chat/start on a flaky network. A normal human request
    leaves this False and keeps the full live-discovery behaviour.

    ``force_refresh=True`` is an internal escape hatch for bounded freshness
    checks that need a real live rebuild while preserving the default cache
    contract for every existing caller.
    """
    # Config mtime check — must come before any config reads.
    # (Test #585 verifies _current_mtime appears before active_provider = None)
    try:
        _current_path = _config_module._get_config_path()
        _current_mtime = _current_path.stat().st_mtime
    except OSError:
        _current_path = _config_module._get_config_path()
        _current_mtime = 0.0
    path_changed = _current_path != _config_module._cfg_path
    mtime_stale = _current_mtime != _config_module._cfg_mtime
    if path_changed or (mtime_stale and not _config_module._cfg_has_in_memory_overrides()):
        _config_module.reload_config_if_stale()
    # ── COLD PATH helper ─────────────────────────────────────────────────────
    # Extracted so it runs inside MODEL_CATALOG_STATE.available_models_cache_lock (RLock) to
    # prevent thundering-herd: only one thread rebuilds while others wait.

    def _build_available_models_uncached() -> dict:
        return build_available_models_uncached(
            live_provider_model_ids=_read_live_provider_model_ids,
        )

    # ── FAST PATH ─────────────────────────────────────────────────────────────
    # Mark that a build may be in progress BEFORE acquiring the lock.
    # If another thread has already started the cold path, we will wait for
    # its result rather than running the cold path concurrently.
    should_wait = MODEL_CATALOG_STATE.cache_build_in_progress
    force_refresh_started_at = time.monotonic() if force_refresh else None

    # Check config mtime OUTSIDE the lock so this cheap check doesn't serialize
    # concurrent requests.  Must come before any config reads in the cold path.
    try:
        _current_mtime = Path(_config_module._get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    _cfg_changed = _current_mtime != _config_module._cfg_mtime

    # Disk load BEFORE lock: ~0.1ms, lets concurrent requests skip entirely.
    # Then acquire lock and check memory cache.  Cold path runs inside the lock
    # so only one thread rebuilds while others wait.
    disk_groups = None
    stale_disk_groups = None
    if MODEL_CATALOG_STATE.available_models_cache is None and not force_refresh:
        disk_groups = _config_module._load_models_cache_from_disk()
        if disk_groups is None:
            stale_disk_groups = _config_module._load_stale_models_cache_from_disk()
    elif force_refresh:
        stale_disk_groups = _config_module._load_stale_models_cache_from_disk()

    with MODEL_CATALOG_STATE.available_models_cache_lock:
        # If another thread is already building, wait for its result instead
        # of re-entering the cold path (avoids duplicate 10s zai load_pool calls).
        if should_wait:
            wait_timeout = 60.0
            if force_refresh and force_refresh_started_at is not None:
                if MODEL_CATALOG_STATE.live_rebuild_budget_seconds <= 0:
                    # The legacy synchronous path is explicitly unbounded. A
                    # forced refresh follower should keep coalescing behind
                    # that live rebuild instead of giving up after 60s and
                    # duplicating it.
                    wait_timeout = None
                else:
                    wait_timeout = max(
                        0.0,
                        MODEL_CATALOG_STATE.live_rebuild_budget_seconds - (time.monotonic() - force_refresh_started_at),
                    )
            MODEL_CATALOG_STATE.cache_build_cv.wait_for(
                lambda: not MODEL_CATALOG_STATE.cache_build_in_progress,
                timeout=wait_timeout
            )
            cached = _config_module._get_fresh_memory_models_cache(time.monotonic())
            if (
                cached is not None
                and (
                    not force_refresh
                    or (
                        force_refresh_started_at is not None
                        and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
                    )
                )
            ):
                return cached
            if force_refresh and MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0 and MODEL_CATALOG_STATE.cache_build_in_progress:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Reload config if changed
        if _cfg_changed:
            _config_module.reload_config()
            MODEL_CATALOG_STATE.available_models_cache = None
            MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
            MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
            MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
            _sync_models_cache_provenance()
            _invalidate_models_build_locked()
            disk_groups = None
            stale_disk_groups = None

        # Serve from memory cache if fresh
        now = time.monotonic()
        cached = _config_module._get_fresh_memory_models_cache(now)
        if cached is not None:
            if not force_refresh:
                return cached
            if (
                force_refresh_started_at is not None
                and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
            ):
                return cached

        # A concurrent forced refresh may have started after this caller sampled
        # should_wait but before it acquired the lock. Reuse that in-flight build
        # instead of launching another one, and preserve this caller's budget.
        if (
            force_refresh
            and force_refresh_started_at is not None
            and MODEL_CATALOG_STATE.cache_build_in_progress
        ):
            remaining_budget = None
            if MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0:
                remaining_budget = max(
                    0.0,
                    MODEL_CATALOG_STATE.live_rebuild_budget_seconds - (time.monotonic() - force_refresh_started_at),
                )
            if remaining_budget is None or remaining_budget > 0:
                MODEL_CATALOG_STATE.cache_build_cv.wait_for(
                    lambda: not MODEL_CATALOG_STATE.cache_build_in_progress,
                    timeout=remaining_budget,
                )
                cached = _config_module._get_fresh_memory_models_cache(time.monotonic())
                if (
                    cached is not None
                    and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
                ):
                    return cached
            if MODEL_CATALOG_STATE.cache_build_in_progress and MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None and not force_refresh:
            MODEL_CATALOG_STATE.available_models_cache = disk_groups
            MODEL_CATALOG_STATE.available_models_cache_ts = now
            MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = _config_module._models_cache_source_fingerprint()
            _sync_models_cache_provenance()
            return copy.deepcopy(disk_groups)

        # ── prefer_cache: NEVER run the live provider rebuild ────────────────
        # Server-initiated wakeup turns (Option Z) reach here with a cold
        # cache (the drain thread fires while idle; the catalog warmed by a
        # human's /api/models has expired or was never built). The live
        # rebuild does a Copilot token-exchange HTTPS call per the proven
        # thread-stack; on this WSL/corp network it stalls the wakeup
        # chat/start indefinitely. A wakeup turn does NOT need the full live
        # catalog — _resolve_compatible_session_model_state only needs
        # default_model/active_provider and trusts the persisted session
        # model. Serve a network-free minimal catalog instead and let a later
        # human request do the real live rebuild.
        if prefer_cache:
            # NOTE (Greptile P1): do NOT touch MODEL_CATALOG_STATE.cache_build_in_progress here.
            # This branch never set the flag (only the cold path below does),
            # and `should_wait` is sampled outside the lock (line ~4964). A
            # concurrent cold-path caller can flip the flag to True after our
            # sample but before we acquire the lock; clearing it here would
            # prematurely release that rebuild's serialization, waking waiters
            # to an empty cache and triggering a second live rebuild. Just
            # serve the network-free minimal catalog and leave the flag alone.
            return copy.deepcopy(_minimal_static_models_catalog())

        # Cold path: full rebuild — only one thread reaches here at a time
        with MODEL_CATALOG_STATE.cache_build_cv:
            MODEL_CATALOG_STATE.models_cache_build_generation += 1
            build_generation = MODEL_CATALOG_STATE.models_cache_build_generation
            MODEL_CATALOG_STATE.active_models_cache_build_generation = build_generation
            MODEL_CATALOG_STATE.cache_build_in_progress = True
            publish_legacy_model_catalog_state(_config_module)

        # Capture the active per-request profile (#3957). The live provider
        # probe inside the rebuild resolves credentials from os.environ /
        # HERMES_HOME and the disk-cache path/fingerprint from the profile TLS;
        # the detached worker thread below inherits NEITHER, so it must be
        # captured here (on the request thread, where the TLS is valid) and
        # re-bound on the worker. Empty / default for single-profile installs.
        from api.config.hooks import get_config_runtime_hooks

        _runtime_hooks = get_config_runtime_hooks()
        _active_profile_name = (_runtime_hooks.active_profile_name() or "").strip()

        # Legacy synchronous (unbounded) rebuild — opt-in via budget<=0.
        if MODEL_CATALOG_STATE.live_rebuild_budget_seconds <= 0:
            try:
                # Foreground thread already carries the request-profile TLS;
                # apply the mirrored profile env (no-op for default) for the
                # live probe because provider_model_ids() still has raw
                # os.getenv()/HERMES_HOME readers on this synchronous path.
                _sync_scope = _runtime_hooks.active_profile_scope(
                    "models rebuild (sync)"
                )
                with _sync_scope:
                    result = _config_module._invoke_models_rebuild(
                        _build_available_models_uncached
                    )
            except BaseException:
                # Always reset the flag so waiting threads don't block for 60s
                with MODEL_CATALOG_STATE.cache_build_cv:
                    if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                        MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                        MODEL_CATALOG_STATE.cache_build_in_progress = False
                        MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                        publish_legacy_model_catalog_state(_config_module)
                raise
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                    published_at = time.monotonic()
                    MODEL_CATALOG_STATE.available_models_cache = result
                    MODEL_CATALOG_STATE.available_models_cache_ts = published_at
                    MODEL_CATALOG_STATE.available_models_live_rebuild_ts = published_at
                    MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = (
                        _config_module._models_cache_source_fingerprint()
                    )
                    _sync_models_cache_provenance()
                    try:
                        _config_module._save_models_cache_to_disk(result)
                    finally:
                        if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                            MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                            MODEL_CATALOG_STATE.cache_build_in_progress = False
                            MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                publish_legacy_model_catalog_state(_config_module)
            return copy.deepcopy(result)

        # ── Bounded rebuild (defense-in-depth) ───────────────────────────────
        # The live rebuild does a network probe per provider (Copilot token
        # exchange over HTTPS, OpenRouter/Nous /models, ...). On a flaky / corp
        # / WSL network any single probe can stall for its full per-call
        # timeout and, summed across providers, pin a foreground request
        # thread for tens of seconds (the wakeup-turn / chat-start hang).
        #
        # Run the rebuild on a daemon worker; the foreground waits at most
        # MODEL_CATALOG_STATE.live_rebuild_budget_seconds.
        #
        # WITHIN budget (the normal fast case): the FOREGROUND publishes the
        # result synchronously and only then returns — preserving the exact
        # pre-existing contract (cache + on-disk file populated by the time
        # get_available_models() returns). The worker stays hands-off.
        #
        # OVER budget (a provider probe is slow/hung): the foreground returns
        # the best fallback immediately and the still-running worker publishes
        # its result out-of-band when it finally finishes, so the next caller
        # gets a warm cache instead of paying the cold rebuild again.
        #
        # ``_publish_models_result`` / ``box["published"]`` ensure exactly one
        # publisher even at the budget boundary (no double write, no lost
        # refresh). The worker only touches MODEL_CATALOG_STATE.cache_build_cv after the
        # foreground releases the RLock by returning, so no lock inversion.
        build_done = threading.Event()
        budget_exceeded = threading.Event()
        publish_lock = threading.Lock()
        box: dict = {}

        def _publish_models_result(result):
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation != build_generation:
                    return
                published_at = time.monotonic()
                MODEL_CATALOG_STATE.available_models_cache = result
                MODEL_CATALOG_STATE.available_models_cache_ts = published_at
                MODEL_CATALOG_STATE.available_models_live_rebuild_ts = published_at
                MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = (
                    _config_module._models_cache_source_fingerprint()
                )
                _sync_models_cache_provenance()
                try:
                    _config_module._save_models_cache_to_disk(result)
                except Exception:
                    logger.debug("models cache disk save failed", exc_info=True)
                finally:
                    MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                    MODEL_CATALOG_STATE.cache_build_in_progress = False
                    MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                    publish_legacy_model_catalog_state(_config_module)

        def _clear_build_in_progress():
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation != build_generation:
                    return
                MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                MODEL_CATALOG_STATE.cache_build_in_progress = False
                MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                publish_legacy_model_catalog_state(_config_module)

        def _claim_publish() -> bool:
            """Return True iff the caller won the right to publish."""
            with publish_lock:
                if box.get("published"):
                    return False
                box["published"] = True
                return True

        def _rebuild_worker():
            # Re-bind the captured per-request profile on THIS worker thread
            # (#3957): the daemon inherits neither the request-profile TLS nor
            # os.environ, so without this it would probe the default profile's
            # credentials and, over budget, publish the rebuilt catalog to the
            # DEFAULT profile's disk cache. No-op for the default profile.
            _worker_scope = _runtime_hooks.detached_profile_scope(
                _active_profile_name, "models rebuild (worker)"
            )
            with _worker_scope:
                try:
                    box["result"] = _config_module._invoke_models_rebuild(
                        _build_available_models_uncached
                    )
                except Exception as exc:  # noqa: BLE001 — propagated to caller
                    box["error"] = exc
                finally:
                    build_done.set()
                    # Only publish out-of-band if the foreground already gave up
                    # (over budget). Within budget the foreground publishes
                    # synchronously, so the worker must NOT touch the cache.
                    # NOTE: the publish (and its disk write + fingerprint) runs
                    # INSIDE this profile scope so the over-budget path writes
                    # the correct profile's cache file.
                    if budget_exceeded.is_set() and _claim_publish():
                        if "result" in box:
                            _publish_models_result(box["result"])
                        else:
                            _clear_build_in_progress()

        _worker = threading.Thread(
            target=_rebuild_worker,
            name="models-catalog-rebuild",
            daemon=True,
        )
        _worker.start()

        if build_done.wait(timeout=MODEL_CATALOG_STATE.live_rebuild_budget_seconds):
            # Build finished within budget — foreground publishes
            # synchronously, exactly like the legacy path.
            if "error" in box:
                _clear_build_in_progress()
                raise box["error"]
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Budget elapsed. Mark it so the worker knows it owns out-of-band
        # publication. Handle the tiny race where the build completed between
        # wait() returning False and here: if so, still publish synchronously
        # so this caller honours the cache contract.
        budget_exceeded.set()
        if build_done.is_set() and "error" not in box and "result" in box:
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Genuinely slow/hung probe: serve the best fallback now; the worker
        # keeps going and refreshes the cache for the next caller.
        # Rate-limit the warning per Q-2979-A3 — see _should_warn_budget; a
        # sustained budget breach demotes to info after the first emit in
        # each cooldown window so log volume stays bounded.
        _budget_log_msg = (
            "live provider-catalog rebuild exceeded %.1fs budget — serving "
            "fallback, refreshing catalog out-of-band"
        )
        if _should_warn_budget("live_rebuild_budget_exceeded"):
            logger.warning(_budget_log_msg, MODEL_CATALOG_STATE.live_rebuild_budget_seconds)
        else:
            logger.info(_budget_log_msg, MODEL_CATALOG_STATE.live_rebuild_budget_seconds)
        # ``stale_disk_groups`` is shape-valid but failed the strict metadata
        # checks required for authoritative cold-path use. It was read before
        # acquiring MODEL_CATALOG_STATE.available_models_cache_lock so this over-budget fallback
        # does not extend the lock hold while the worker is ready to publish.
        if stale_disk_groups is not None:
            return copy.deepcopy(stale_disk_groups)
        return copy.deepcopy(_static_models_catalog_without_live_probes())


__config_exports__ = (
    "_invalidate_models_build_locked",
    "_sync_models_cache_provenance",
    "_endpoint_advertised_model_ids",
    "_should_warn_budget",
    "_invoke_models_rebuild",
    "_configured_model_badges_from_static_catalog",
    "_minimal_static_models_catalog",
    "_static_models_catalog_without_live_probes",
    "_credential_pool_profile_tag",
    "_pool_entry_payloads",
    "_has_explicit_pool_credentials",
    "_current_webui_version",
    "_get_label_for_model",
    "_read_live_provider_model_ids",
    "_models_from_live_provider_ids",
    "_moa_preset_models_from_config",
    "_read_visible_codex_cache_model_ids",
    "get_available_models",
)

__all__ = __config_exports__
