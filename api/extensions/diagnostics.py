"""Sanitized extension diagnostics that never echo rejected input."""

from typing import Any, Dict, Optional


def new_diagnostics() -> Dict[str, Any]:
    return {"warnings": []}


def add_diagnostic_warning(
    diagnostics: Optional[Dict[str, Any]], code: str, source: str
) -> None:
    """Record one stable warning code and coarse source without path leaks."""
    if diagnostics is None:
        return
    warnings = diagnostics.setdefault("warnings", [])
    if not isinstance(warnings, list):
        return
    warning = {"code": code, "source": source}
    if warning not in warnings:
        warnings.append(warning)


__all__ = ["add_diagnostic_warning", "new_diagnostics"]
