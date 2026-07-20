"""Kanban request-value normalization and domain validation."""

from __future__ import annotations

from urllib.parse import parse_qs

from .integration import _kb

BOARD_COLUMNS = ["triage", "todo", "ready", "running", "blocked", "done"]
TASK_PREFIX = "/api/kanban/tasks/"


def _resolve_board(parsed):
    """Validate and normalise a ?board=<slug> query param.

    Returns the normalised slug, or ``None`` when the caller omitted the
    param. Raises ValueError on a malformed slug so the bridge surfaces a
    clean 400 instead of a 500 from deeper in the library.
    """
    raw = (parse_qs(parsed.query or "").get("board") or [None])[0]
    return _normalise_board_or_raise(raw)


def _resolve_board_from_body(body):
    """Same contract as :func:`_resolve_board` but reads ``board`` from a
    parsed JSON body (POST / PATCH / DELETE handlers receive a dict, not
    a parsed URL). Returns ``None`` when the body did not specify a board.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("board")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    return _normalise_board_or_raise(raw)


def _normalise_board_or_raise(raw):
    """Shared normalisation + existence check for board slugs."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(raw)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {raw!r}") from exc
    if not normed:
        return None
    # Allow the default board even if it has not been materialised yet
    # (kb.init_db will create it lazily). For non-default boards, require
    # the directory exists or _conn would fail with a confusing OperationalError.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed != default_slug and not kb.board_exists(normed):
        raise LookupError(f"board {normed!r} does not exist")
    return normed


def _bool_query(parsed, name: str, default: bool = False) -> bool:
    """Extract a boolean query param, treating 1/true/yes/on (case-insensitive) as True."""
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _str_query(parsed, name: str):
    """Extract a string query param, returning None when the param is absent or blank."""
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    return str(raw).strip() or None if raw is not None else None


def _int_query(parsed, name: str, default=None, *, minimum=None, maximum=None):
    """Extract an integer query param, clamped to [minimum, maximum] when those bounds are provided."""
    raw = _str_query(parsed, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _validate_status(status: str) -> str:
    """Validate a status string against BOARD_COLUMNS, raising ValueError for unrecognised values."""
    value = str(status or "").strip().lower()
    allowed = set(BOARD_COLUMNS) | {"archived"}
    if value not in allowed:
        raise ValueError(f"invalid status: {value}")
    return value
