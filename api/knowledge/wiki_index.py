"""LLM Wiki path resolution, allowlisted indexing, and private-safe status."""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters import active_config_snapshot, active_hermes_home

LLM_WIKI_DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/research/research-llm-wiki"
LLM_WIKI_PAGE_DIRS = ("entities", "concepts", "comparisons", "queries")
LLM_WIKI_MAX_FILES = 10_000
LLM_WIKI_MAX_PAGE_BYTES = 2 * 1024 * 1024
LLM_WIKI_FORBIDDEN_ROOTS = frozenset(
    str(Path(path).expanduser().resolve())
    for path in ("/", "/etc", "/usr", "/var", "/opt", "/sys", "/proc")
)
WIKI_ALLOWLIST_TTL = 5.0

_page_cache: dict[str, dict[str, object]] = {}
_page_cache_lock = threading.Lock()


def env_file_wiki_path(hermes_home: Path) -> str | None:
    env_path = hermes_home / ".env"
    if not env_path.exists() or not env_path.is_file():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "WIKI_PATH":
                value = value.strip().strip('"').strip("'")
                return value or None
    except Exception:
        return None
    return None


def config_path_value(config: dict, dotted_key: str) -> str | None:
    if not isinstance(config, dict):
        return None
    if dotted_key in config and config.get(dotted_key):
        return str(config.get(dotted_key))
    current = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return str(current) if current else None


def config_wiki_path(config: dict | None = None) -> str | None:
    resolved = config if isinstance(config, dict) else active_config_snapshot()
    return config_path_value(resolved, "skills.config.wiki.path") or config_path_value(
        resolved, "wiki.path"
    )


def resolve_wiki_path(
    *,
    hermes_home: Path | None = None,
    config: dict | None = None,
    environ=None,
) -> tuple[Path, str, bool]:
    resolved_home = hermes_home or active_hermes_home()
    resolved_environ = environ if environ is not None else os.environ
    raw = resolved_environ.get("WIKI_PATH") or env_file_wiki_path(resolved_home)
    source = "WIKI_PATH" if raw else "default"
    configured = bool(raw)
    if not raw:
        raw = config_wiki_path(config)
        if raw:
            source = "skills.config.wiki.path"
            configured = True
    if not raw:
        raw = "~/wiki"
    return Path(os.path.expandvars(raw)).expanduser(), source, configured


def safe_iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    try:
        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        return None


def count_files(root: Path) -> int:
    if not root.exists() or not root.is_dir():
        return 0
    try:
        if str(root.resolve()) in LLM_WIKI_FORBIDDEN_ROOTS:
            return 0
    except Exception:
        return 0
    count = 0
    for iterated, item in enumerate(root.rglob("*"), start=1):
        if iterated > LLM_WIKI_MAX_FILES:
            break
        try:
            if item.is_file() and not any(
                part.startswith(".") for part in item.relative_to(root).parts
            ):
                count += 1
        except Exception:
            continue
    return count


def page_cache_signature(wiki_path: Path) -> tuple:
    signature = []
    for section in LLM_WIKI_PAGE_DIRS:
        try:
            entry = (wiki_path / section).lstat()
        except OSError:
            signature.append((section, None))
            continue
        signature.append((section, entry.st_dev, entry.st_ino, entry.st_mtime_ns))
    return tuple(signature)


def page_files_uncached(wiki_path: Path) -> list[Path]:
    pages: list[Path] = []
    try:
        wiki_real = wiki_path.resolve()
        if str(wiki_real) in LLM_WIKI_FORBIDDEN_ROOTS:
            return pages
    except Exception:
        return pages

    def clean_relative_path(relative: Path) -> bool:
        return not any(part.startswith(".") for part in relative.parts)

    iterated = 0
    for directory_name in LLM_WIKI_PAGE_DIRS:
        section = wiki_path / directory_name
        if not section.exists() or not section.is_dir():
            continue
        try:
            section_real = section.resolve()
            section_real.relative_to(wiki_real)
        except (OSError, ValueError):
            continue
        for item in section.rglob("*.md"):
            iterated += 1
            if iterated > LLM_WIKI_MAX_FILES:
                return pages
            try:
                relative = item.relative_to(section)
                if not item.is_file() or not clean_relative_path(relative):
                    continue
                if item.lstat().st_nlink > 1:
                    continue
                item_real = item.resolve()
                item_real.relative_to(section_real)
                resolved_relative = item_real.relative_to(wiki_real)
                if not clean_relative_path(resolved_relative):
                    continue
                pages.append(item)
            except (OSError, ValueError):
                continue
    return pages


def page_files(wiki_path: Path) -> list[Path]:
    """Return allowlisted pages, cached by root identity/signature and a short TTL.

    The index rejects multi-link page files.  Identity checks at read time close
    path/symlink swaps, while rejecting hardlinks prevents an arbitrary inode
    from being introduced under a clean page name by a writer with access to
    the otherwise operator-controlled wiki directory.
    """
    try:
        wiki_root = wiki_path.resolve()
    except OSError:
        wiki_root = wiki_path
    key = str(wiki_root)
    signature = page_cache_signature(wiki_root)
    now = time.monotonic()
    with _page_cache_lock:
        cached = _page_cache.get(key)
        if (
            cached is not None
            and cached.get("signature") == signature
            and now < cached.get("expires_at", 0)
        ):
            return list(cached["files"])
    pages = page_files_uncached(wiki_root)
    with _page_cache_lock:
        _page_cache[key] = {
            "signature": signature,
            "expires_at": now + WIKI_ALLOWLIST_TTL,
            "files": tuple(pages),
        }
    return list(pages)


def clear_wiki_page_cache() -> None:
    with _page_cache_lock:
        _page_cache.clear()


def allowlisted_entries(wiki_path: Path) -> dict[str, tuple[Path, tuple[int, int]]]:
    try:
        wiki_real = wiki_path.resolve()
    except OSError:
        return {}

    def clean_read_path(relative: Path) -> bool:
        relative_text = relative.as_posix()
        return (
            bool(relative.parts)
            and "\\" not in relative_text
            and not any(part.startswith(".") for part in relative.parts)
        )

    entries: dict[str, tuple[Path, tuple[int, int]]] = {}
    try:
        for listed_path in page_files(wiki_real):
            try:
                listed_relative = listed_path.relative_to(wiki_real)
                if not clean_read_path(listed_relative):
                    continue
                section_real = (wiki_real / listed_relative.parts[0]).resolve()
                section_real.relative_to(wiki_real)
                resolved_target = listed_path.resolve()
                resolved_target.relative_to(section_real)
                if not resolved_target.is_file() or resolved_target.stat().st_nlink > 1:
                    continue
                resolved_relative = resolved_target.relative_to(wiki_real)
                if not clean_read_path(resolved_relative):
                    continue
                initial = resolved_target.stat()
                if listed_path.resolve() != resolved_target:
                    continue
                entries[listed_relative.as_posix()] = (
                    resolved_target,
                    (initial.st_dev, initial.st_ino),
                )
            except (OSError, ValueError):
                continue
    except Exception:
        return {}
    return entries


def status_file_entry_stat(wiki_path: Path, path: Path) -> os.stat_result | None:
    try:
        wiki_root = wiki_path.resolve()
    except OSError:
        wiki_root = wiki_path
    try:
        path.resolve().relative_to(wiki_root)
        entry = path.lstat()
    except (OSError, ValueError):
        return None
    return entry if stat.S_ISREG(entry.st_mode) else None


def verified_status_file_stat(wiki_path: Path, path: Path) -> os.stat_result | None:
    entry = status_file_entry_stat(wiki_path, path)
    if entry is None:
        return None
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            return None
        return opened
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def last_writer(
    wiki_path: Path,
    page_entries: list[Path] | list[tuple[Path, tuple[int, int]]],
) -> str:
    """Read only bounded frontmatter/log headings to derive a safe writer label."""
    try:
        wiki_root = wiki_path.resolve()
    except Exception:
        wiki_root = wiki_path

    def within_wiki(path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(wiki_root)
        except Exception:
            return False

    latest_page: Path | None = None
    latest_identity: tuple[int, int] | None = None
    latest_mtime = -1.0
    for item in page_entries:
        candidate, identity = item if isinstance(item, tuple) else (item, None)
        if not within_wiki(candidate):
            continue
        try:
            candidate_stat = candidate.stat()
        except Exception:
            continue
        if identity is not None and (
            candidate_stat.st_dev,
            candidate_stat.st_ino,
        ) != identity:
            continue
        if candidate_stat.st_mtime > latest_mtime:
            latest_mtime = candidate_stat.st_mtime
            latest_page = candidate
            latest_identity = identity
    if latest_page is not None:
        fd = None
        try:
            fd = os.open(str(latest_page), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            if latest_identity is not None:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != latest_identity:
                    raise FileNotFoundError("wiki page changed after allowlist snapshot")
            with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
                fd = None
                if handle.readline().strip() == "---":
                    for _ in range(200):
                        line = handle.readline()
                        if line == "" or line.strip() == "---":
                            break
                        stripped = line.strip()
                        lower = stripped.lower()
                        for key in ("updated_by", "writer", "author"):
                            if lower.startswith(f"{key}:"):
                                value = stripped.split(":", 1)[1].strip()
                                if value:
                                    return value
        except Exception:
            pass
        finally:
            if fd is not None:
                os.close(fd)

    log_path = wiki_path / "log.md"
    log_entry = status_file_entry_stat(wiki_path, log_path)
    if log_entry is not None:
        fd = None
        try:
            fd = os.open(str(log_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (log_entry.st_dev, log_entry.st_ino):
                raise FileNotFoundError("wiki log changed before open")
            with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
                fd = None
                for _ in range(5000):
                    line = handle.readline()
                    if line == "":
                        break
                    stripped = line.strip()
                    if not stripped.startswith("## [") or "|" not in stripped:
                        continue
                    tail = stripped.split("]", 1)[1].strip() if "]" in stripped else ""
                    action = tail.split()[0] if tail else "update"
                    return f"ai-agent ({action})"
        except Exception:
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return "ai-agent"


@dataclass(frozen=True)
class WikiIndex:
    """One resolved wiki root and its identity-checked page index."""

    root: Path

    def entries(self) -> dict[str, tuple[Path, tuple[int, int]]]:
        return allowlisted_entries(self.root)


def build_wiki_status(
    *,
    resolved_path: tuple[Path, str, bool] | None = None,
) -> dict:
    """Return private-safe wiki metadata without reading page bodies."""
    try:
        wiki_path, path_source, path_configured = resolved_path or resolve_wiki_path()
        base = {
            "available": False,
            "enabled": False,
            "status": "missing",
            "entry_count": 0,
            "page_count": 0,
            "raw_source_count": 0,
            "last_updated": None,
            "last_writer": "ai-agent",
            "path_configured": path_configured,
            "path_source": path_source,
            "toggle_available": False,
            "toggle_reason": "Hermes Agent exposes WIKI_PATH/wiki.path for location, but no stable on/off config flag is currently available.",
            "docs_url": LLM_WIKI_DOCS_URL,
        }
        if not wiki_path.exists():
            return base
        if not wiki_path.is_dir():
            base["status"] = "not_directory"
            return base
        entries = WikiIndex(wiki_path).entries()
        verified_pages: list[tuple[Path, tuple[int, int], os.stat_result]] = []
        for target, identity in entries.values():
            try:
                target_stat = target.stat()
            except Exception:
                continue
            if (target_stat.st_dev, target_stat.st_ino) == identity:
                verified_pages.append((target, identity, target_stat))
        page_entries = [(target, identity) for target, identity, _ in verified_pages]
        status_files: list[tuple[Path, float]] = []
        for path in (wiki_path / "SCHEMA.md", wiki_path / "index.md", wiki_path / "log.md"):
            status_stat = verified_status_file_stat(wiki_path, path)
            if status_stat is not None:
                status_files.append((path, status_stat.st_mtime))
        status_files.extend((target, item_stat.st_mtime) for target, _, item_stat in verified_pages)
        latest = max((mtime for _, mtime in status_files), default=None)
        base.update(
            {
                "available": True,
                "enabled": True,
                "status": "ready" if verified_pages else "empty",
                "entry_count": len(verified_pages),
                "page_count": len(page_entries),
                "raw_source_count": count_files(wiki_path / "raw"),
                "last_updated": safe_iso(latest),
                "last_writer": last_writer(wiki_path, page_entries),
            }
        )
        return base
    except Exception as exc:
        return {
            "available": False,
            "enabled": False,
            "status": "error",
            "entry_count": 0,
            "page_count": 0,
            "raw_source_count": 0,
            "last_updated": None,
            "last_writer": "ai-agent",
            "path_configured": False,
            "path_source": "unknown",
            "toggle_available": False,
            "toggle_reason": "Unable to inspect LLM Wiki status safely.",
            "docs_url": LLM_WIKI_DOCS_URL,
            "error": type(exc).__name__,
        }
