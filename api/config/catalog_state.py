"""Canonical mutable owner for model-catalog and cache lifecycle state."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class ModelCatalogState:
    available_models_cache: dict | None = None
    available_models_cache_ts: float = 0.0
    available_models_live_rebuild_ts: float = 0.0
    available_models_cache_source_fingerprint: dict | None = None
    available_models_cache_ttl: float = 86400.0
    session_visit_models_freshness_seconds: float = 300.0
    available_models_cache_lock: threading.RLock = field(default_factory=threading.RLock)
    cache_build_in_progress: bool = False
    models_cache_build_generation: int = 0
    active_models_cache_build_generation: int | None = None
    advertised_model_ids_memo: tuple | None = None
    models_cache_provenance: tuple | None = None
    live_rebuild_budget_seconds: float = field(
        default_factory=lambda: _env_float("HERMES_WEBUI_MODELS_REBUILD_BUDGET", 4.0)
    )
    budget_warn_cooldown_seconds: float = field(
        default_factory=lambda: _env_float("HERMES_WEBUI_BUDGET_WARN_COOLDOWN", 300.0)
    )
    budget_warn_state: dict[str, float] = field(default_factory=dict)
    budget_warn_lock: threading.Lock = field(default_factory=threading.Lock)
    credential_pool_cache: dict[tuple[str, str], tuple[float, Any]] = field(
        default_factory=dict
    )
    models_cache_schema_version: int = 3
    models_cache_path: Path | None = None

    def __post_init__(self) -> None:
        self.cache_build_cv = threading.Condition(self.available_models_cache_lock)

    cache_build_cv: threading.Condition = field(init=False)


MODEL_CATALOG_STATE = ModelCatalogState()

_LEGACY_STATE_NAMES = {
    "_available_models_cache": "available_models_cache",
    "_available_models_cache_ts": "available_models_cache_ts",
    "_available_models_live_rebuild_ts": "available_models_live_rebuild_ts",
    "_available_models_cache_source_fingerprint": "available_models_cache_source_fingerprint",
    "_AVAILABLE_MODELS_CACHE_TTL": "available_models_cache_ttl",
    "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS": "session_visit_models_freshness_seconds",
    "_available_models_cache_lock": "available_models_cache_lock",
    "_cache_build_cv": "cache_build_cv",
    "_cache_build_in_progress": "cache_build_in_progress",
    "_models_cache_build_generation": "models_cache_build_generation",
    "_active_models_cache_build_generation": "active_models_cache_build_generation",
    "_advertised_model_ids_memo": "advertised_model_ids_memo",
    "_models_cache_provenance": "models_cache_provenance",
    "_LIVE_REBUILD_BUDGET_SECONDS": "live_rebuild_budget_seconds",
    "_BUDGET_WARN_COOLDOWN_SECONDS": "budget_warn_cooldown_seconds",
    "_BUDGET_WARN_STATE": "budget_warn_state",
    "_BUDGET_WARN_LOCK": "budget_warn_lock",
    "_CREDENTIAL_POOL_CACHE": "credential_pool_cache",
    "_MODELS_CACHE_SCHEMA_VERSION": "models_cache_schema_version",
    "_models_cache_path": "models_cache_path",
}


def import_legacy_model_catalog_state(module) -> None:
    """Adopt evidenced legacy overrides at an API boundary."""
    for legacy_name, field_name in _LEGACY_STATE_NAMES.items():
        if hasattr(module, legacy_name):
            setattr(MODEL_CATALOG_STATE, field_name, getattr(module, legacy_name))


def publish_legacy_model_catalog_state(module) -> None:
    """Publish the canonical owner through the retained legacy export seam."""
    for legacy_name, field_name in _LEGACY_STATE_NAMES.items():
        setattr(module, legacy_name, getattr(MODEL_CATALOG_STATE, field_name))
