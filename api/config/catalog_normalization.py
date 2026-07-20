"""Canonical model-id normalization for catalog matching and deduplication."""

from __future__ import annotations


def normalize_catalog_model_id(model_id: object) -> str:
    """Return the comparison key used by catalog badges and default insertion.

    Provider prefixes are removed without collapsing URI paths, vendor
    hierarchies, or malformed trailing separators into an empty identity.
    """
    normalized = str(model_id or "").strip().lower()
    stripped_at_provider = False
    if normalized.startswith("@") and ":" in normalized:
        colon_idx = normalized.index(":", 1)
        candidate = normalized[colon_idx + 1 :]
        stripped_at_provider = bool(candidate)
        normalized = candidate or normalized
    if "://" not in normalized:
        if (
            not stripped_at_provider
            and "/" in normalized
            and ":" in normalized
            and normalized.index(":") < normalized.index("/")
        ):
            normalized = normalized[normalized.index("/") + 1 :] or normalized
        if "/" in normalized:
            stripped = normalized.split("/", 1)[1]
            normalized = stripped or normalized
    return normalized.replace("-", ".")


__all__ = ("normalize_catalog_model_id",)
