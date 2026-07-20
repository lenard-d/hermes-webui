"""Browse and read behavior over the LLM Wiki allowlisted index."""

from __future__ import annotations

import os
from pathlib import Path

from .adapters import KnowledgeFilesystemAdapter
from .wiki_index import LLM_WIKI_MAX_PAGE_BYTES, WikiIndex


class WikiReadError(Exception):
    """Base error translated by the HTTP adapter."""


class WikiNotFoundError(WikiReadError):
    pass


class WikiInvalidPathError(WikiReadError):
    pass


class WikiFileReadError(WikiReadError):
    pass


def browse_wiki_pages(index: WikiIndex) -> list[dict]:
    if not index.root or not os.path.isdir(index.root):
        raise WikiNotFoundError("Wiki not configured or directory not found")
    pages = []
    for relative_path, (file_path, identity) in sorted(
        index.entries().items(), key=lambda item: item[0].lower()
    ):
        try:
            item_stat = file_path.stat()
        except OSError:
            continue
        if (item_stat.st_dev, item_stat.st_ino) != identity:
            continue
        pages.append(
            {
                "name": Path(relative_path).name,
                "path": relative_path,
                "size": item_stat.st_size,
                "mtime": int(item_stat.st_mtime),
            }
        )
    return pages


def read_wiki_page(
    index: WikiIndex,
    page_path: str,
    *,
    filesystem: KnowledgeFilesystemAdapter | None = None,
) -> dict:
    if not index.root or not page_path:
        raise WikiInvalidPathError("Wiki not configured or path not provided")
    if "\\" in page_path:
        raise WikiInvalidPathError("Invalid path")
    requested_key = page_path.replace("\\", "/")
    parts = requested_key.split("/")
    if os.path.isabs(page_path) or any(part == ".." for part in parts):
        raise WikiInvalidPathError("Invalid path")
    if any(part in ("", ".") for part in parts):
        raise WikiInvalidPathError("Invalid path")

    full_path = Path(os.path.join(index.root, page_path))
    try:
        full_path.resolve().relative_to(index.root.resolve())
        resolved_target = full_path.resolve()
    except (OSError, ValueError):
        raise WikiInvalidPathError("Invalid path") from None

    requested_entry = index.entries().get(requested_key)
    if requested_entry is None:
        raise WikiNotFoundError("Page not found")
    allowlisted_target, allowlisted_identity = requested_entry
    if resolved_target != allowlisted_target:
        raise WikiNotFoundError("Page not found")

    try:
        raw = (filesystem or KnowledgeFilesystemAdapter()).read_identity_checked(
            resolved_target,
            allowlisted_identity,
            max_bytes=LLM_WIKI_MAX_PAGE_BYTES,
        )
        if len(raw) > LLM_WIKI_MAX_PAGE_BYTES:
            raw = raw[:LLM_WIKI_MAX_PAGE_BYTES]
        content = raw.decode("utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        raise WikiNotFoundError("Page not found") from None
    except OSError:
        raise WikiFileReadError("Could not read page") from None
    return {"content": content, "path": page_path}
