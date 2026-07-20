"""Kanban board collection lifecycle operations."""

from __future__ import annotations

from .integration import _kb
from .validation import _bool_query


def _board_meta_dict(meta):
    """Coerce the library's board metadata dict into a JSON-serialisable
    form. ``list_boards`` returns dicts with Path values for ``directory``;
    json.dumps would refuse those without help."""
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    for key in ("directory", "db_path", "path"):
        if key in out and out[key] is not None:
            out[key] = str(out[key])
    return out


def _board_counts_for_slug(slug):
    """Per-status task counts for a board, used to populate the board
    switcher with a live "12 tasks" badge. Mirrors the agent dashboard's
    ``_board_counts`` helper. Returns an empty dict for boards whose
    sqlite file has not been materialized yet (freshly-created boards
    with no tasks)."""
    kb = _kb()
    if not kb.board_exists(slug):
        return {}
    try:
        conn = kb.connect(board=slug)
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks "
            "WHERE status != 'archived' GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"] or 0) for row in rows}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _list_boards_payload(parsed):
    """GET /api/kanban/boards — return all boards on disk + active slug.

    Each entry includes per-status counts and an ``is_current`` flag so the
    UI can render the switcher in a single round-trip.
    """
    kb = _kb()
    include_archived = _bool_query(parsed, "include_archived", False)
    boards = kb.list_boards(include_archived=include_archived)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    visible_slugs = {(_board_meta_dict(meta).get("slug")) for meta in boards}
    default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    if current not in visible_slugs:
        # The on-disk active-board pointer can outlive an archived/deleted board
        # when another CLI/WebUI process removes it. Surface a valid current
        # board instead of letting the frontend pin every subsequent request to
        # a ghost slug and fail with an opaque 404.
        try:
            kb.clear_current_board()
        except Exception:
            pass
        current = default_slug
    out = []
    for raw_meta in boards:
        meta = _board_meta_dict(raw_meta)
        slug = meta.get("slug")
        if slug is None:
            continue
        meta["is_current"] = slug == current
        meta["counts"] = _board_counts_for_slug(slug)
        meta["total"] = sum(meta["counts"].values()) if meta["counts"] else 0
        out.append(meta)
    return {"boards": out, "current": current, "read_only": False}


def _create_board_payload(body):
    """POST /api/kanban/boards — create a new board.

    Body fields: ``slug`` (required), ``name``, ``description``, ``icon``,
    ``color``, ``switch`` (bool — set as active after creation, default false).
    Idempotent on slug — repeating returns the existing board metadata.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    slug = str(body.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    try:
        meta = kb.create_board(
            slug,
            name=body.get("name") or None,
            description=body.get("description") or None,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError(str(exc)) from exc
    if body.get("switch"):
        try:
            kb.set_current_board(meta["slug"])
        except (ValueError, AttributeError) as exc:
            raise ValueError(str(exc)) from exc
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    return {"board": _board_meta_dict(meta), "current": current, "read_only": False}


def _update_board_payload(slug, body):
    """PATCH /api/kanban/boards/<slug> — update a board's display metadata.

    The slug itself is immutable (changing it would mean moving the on-disk
    directory and re-pointing every saved active-board cookie). Only
    ``name``, ``description``, ``icon``, ``color``, and ``archived`` are
    mutable here; the slug travels in the URL path.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    archived = body.get("archived")
    if isinstance(archived, str):
        archived = archived.strip().lower() in {"1", "true", "yes", "on"}
    meta = kb.write_board_metadata(
        normed,
        name=body.get("name"),
        description=body.get("description"),
        icon=body.get("icon"),
        color=body.get("color"),
        archived=archived if isinstance(archived, bool) else None,
    )
    return {"board": _board_meta_dict(meta), "read_only": False}


def _delete_board_payload(slug, parsed):
    """DELETE /api/kanban/boards/<slug> — archive (default) or hard-delete.

    ``?delete=1`` is required to actually remove on-disk artefacts; without
    it the board is just marked archived in its metadata and remains
    enumerable via ``?include_archived=1`` on /boards.
    """
    kb = _kb()
    hard_delete = _bool_query(parsed, "delete", False)
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    # Refuse to delete the default board — that would leave the system
    # without a fallback active board on next CLI / dashboard call.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed == default_slug:
        raise ValueError("cannot remove the default board")
    res = kb.remove_board(normed, archive=not hard_delete)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    # If we just removed the active board, the library auto-falls-back to
    # default on the next get_current_board() — surface that explicitly so
    # the UI can re-fetch /board on the new active slug.
    return {
        "result": _board_meta_dict(res) if isinstance(res, dict) else res,
        "current": current,
        "read_only": False,
    }


def _switch_board_payload(slug):
    """POST /api/kanban/boards/<slug>/switch — set this board as active.

    The active-board pointer is stored on disk under ``<root>/kanban/current``
    and is shared by the CLI, gateway, dashboard, and WebUI — switching
    here switches everywhere. The UI also keeps a localStorage hint so
    that opening a fresh tab doesn't always have to round-trip to discover
    the active slug, but the on-disk pointer is the source of truth.
    """
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    kb.set_current_board(normed)
    return {"current": normed, "read_only": False}
