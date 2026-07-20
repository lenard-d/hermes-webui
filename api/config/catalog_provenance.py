"""Model-catalog publication provenance and rebuild coordination."""

from __future__ import annotations

import logging
import time

from api import config as _config_module
from api.config.catalog_state import MODEL_CATALOG_STATE, publish_legacy_model_catalog_state

logger = logging.getLogger(__name__)

def _invalidate_models_build_locked() -> None:
    """Revoke publication rights from any detached catalog rebuild."""
    MODEL_CATALOG_STATE.models_cache_build_generation += 1
    MODEL_CATALOG_STATE.active_models_cache_build_generation = None
    MODEL_CATALOG_STATE.cache_build_in_progress = False
    MODEL_CATALOG_STATE.cache_build_cv.notify_all()
    publish_legacy_model_catalog_state(_config_module)


def _sync_models_cache_provenance() -> None:
    """Republish the atomic (snapshot, fingerprint) provenance pair.

    MUST be called at every site that assigns ``MODEL_CATALOG_STATE.available_models_cache`` and
    ``MODEL_CATALOG_STATE.available_models_cache_source_fingerprint`` (publish and invalidate),
    AFTER both have been set. It snapshots the current pair into one immutable
    tuple so ``_endpoint_advertised_model_ids`` reads both consistently with a
    single lock-free load. A reader that races between an underlying assignment
    and this call sees the PREVIOUS consistent tuple (never a torn pair); once
    this runs, readers see the new consistent pair.
    """
    snap = MODEL_CATALOG_STATE.available_models_cache
    MODEL_CATALOG_STATE.models_cache_provenance = (
        (snap, MODEL_CATALOG_STATE.available_models_cache_source_fingerprint) if snap is not None else None
    )
    publish_legacy_model_catalog_state(_config_module)


def _endpoint_advertised_model_ids(provider_id: str | None) -> frozenset | None:
    """Model ids the given provider's group advertised in the current catalog.

    Reads ONLY the already-published in-memory catalog snapshot
    (``MODEL_CATALOG_STATE.available_models_cache``) — it never builds, live-probes, or touches
    disk, so it is safe to call on the per-turn send hot path. Returns:

      * a ``frozenset`` of the ids advertised by ``provider_id``'s own group
        (bare ids for the active provider, e.g. ``x-ai/grok-4.5``), or
      * ``None`` when the catalog is cold/unbuilt OR the provider has no group.

    ``None`` means "no provenance signal available" — callers MUST treat that as
    "preserve the model id verbatim" so a cache miss never silently strips a
    vendor namespace off an id the user actively selected (#5979). Scoping to
    the provider's OWN group prevents a same-named id in a sibling group (e.g.
    an ``openai/gpt-5.4`` sitting in the OpenRouter group) from masquerading as
    something this custom endpoint advertised.
    """
    # Single lock-free atomic read of the immutable (snapshot, fingerprint) pair
    # published by _sync_models_cache_provenance(). Reading one tuple can never
    # tear, and acquiring no lock means this per-send check adds no lock-ordering
    # edge (no _cfg_lock ↔ MODEL_CATALOG_STATE.available_models_cache_lock deadlock) and never waits
    # behind a catalog rebuild.
    provenance = MODEL_CATALOG_STATE.models_cache_provenance
    if provenance is None:
        return None
    snapshot, published_fp = provenance
    if snapshot is None:
        return None
    # Profile-isolation fail-safe (profiles are islands): the catalog cache is a
    # process global, so a concurrently-active profile could have published the
    # snapshot we're now reading. Only trust it for provenance when the
    # fingerprint captured AT PUBLISH TIME still matches the current runtime
    # fingerprint — the ``config_yaml`` axis of that fingerprint is the
    # PROFILE-SPECIFIC config path (_config_module._get_config_path -> get_active_hermes_home),
    # so a match guarantees the snapshot belongs to the profile asking. Any
    # mismatch (foreign profile, config edit, stale) returns None so the caller
    # preserves the id verbatim rather than stripping against another profile's
    # catalog.
    try:
        if published_fp != _config_module._models_cache_source_fingerprint():
            return None
    except Exception:
        return None  # fingerprint unavailable → no trustworthy provenance
    memo = MODEL_CATALOG_STATE.advertised_model_ids_memo
    # Identity check (``is``), not id(): holding the snapshot reference in the
    # memo keeps it alive, so a freed-then-reused id() can't cause a false hit.
    if memo is None or memo[0] is not snapshot:
        by_slug: dict[str, frozenset] = {}
        try:
            groups = snapshot.get("groups", []) or []
        except AttributeError:
            return None
        for group in groups:
            if not isinstance(group, dict):
                continue
            slug = str(group.get("provider_id") or "").strip().lower()
            if not slug:
                continue
            # Union BOTH catalog buckets: a provider's models can be split across
            # ``models`` (visible) and ``extra_models`` (overflow) by the picker,
            # so an id the endpoint genuinely advertised may live in either. Only
            # reading ``models`` would miss it and mis-resolve (e.g. leave the
            # #433 bare id unstripped because it sits in extra_models).
            ids = frozenset(
                str(m.get("id"))
                for bucket in ("models", "extra_models")
                for m in (group.get(bucket) or [])
                if isinstance(m, dict) and m.get("id")
            )
            by_slug[slug] = by_slug.get(slug, frozenset()) | ids
        memo = (snapshot, by_slug)
        MODEL_CATALOG_STATE.advertised_model_ids_memo = memo
    slug = str(provider_id or "").strip().lower()
    return memo[1].get(slug)


def _should_warn_budget(reason: str, cooldown_s: float | None = None) -> bool:
    """Return True iff the budget warning for ``reason`` should log at
    warning level (first hit, or last warn-level emit was more than
    ``cooldown_s`` seconds ago). Otherwise False — the caller should demote
    to info for the same payload so the signal is retained but the noise is
    capped. Thread-safe; the cooldown is shared across all live-rebuild
    callers in this process.
    """
    cooldown = (
        MODEL_CATALOG_STATE.budget_warn_cooldown_seconds if cooldown_s is None else float(cooldown_s)
    )
    now = time.monotonic()
    with MODEL_CATALOG_STATE.budget_warn_lock:
        last = MODEL_CATALOG_STATE.budget_warn_state.get(reason)
        if last is None or (now - last) >= cooldown:
            MODEL_CATALOG_STATE.budget_warn_state[reason] = now
            return True
        return False


def _invoke_models_rebuild(builder):
    """Indirection seam around the cold catalog rebuild.

    Production simply calls ``builder()``. Exists so tests can simulate a
    slow / hanging provider probe without having to reach the closure that
    actually does the per-provider network calls.
    """
    return builder()

__all__ = (
    "_invalidate_models_build_locked",
    "_sync_models_cache_provenance",
    "_endpoint_advertised_model_ids",
    "_should_warn_budget",
    "_invoke_models_rebuild",
)
