"""Claude Code transcript discovery and parsing.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

CLAUDE_CODE_SOURCE = 'claude_code'
CLAUDE_CODE_SOURCE_LABEL = 'Claude Code'
CLAUDE_CODE_MAX_FILES = 200
CLAUDE_CODE_MAX_FILE_BYTES = 10 * 1024 * 1024
CLAUDE_CODE_MAX_MESSAGES_PER_FILE = 1000
CLAUDE_CODE_MAX_CONTENT_CHARS = 200_000


def _normalize_cli_session_source_filter(source_filter) -> str | None:
    normalized = str(source_filter or '').strip().lower()
    if not normalized or normalized in {'all', 'any', '*'}:
        return None
    if normalized == 'claude-code':
        return CLAUDE_CODE_SOURCE
    return normalized


def _default_claude_code_projects_dir() -> Path | None:
    """Resolve the Claude Code projects directory without touching real home in tests."""
    override = os.getenv('HERMES_WEBUI_CLAUDE_PROJECTS_DIR')
    if override:
        return Path(override).expanduser()
    if os.getenv('HERMES_WEBUI_TEST_STATE_DIR'):
        return None
    return Path.home() / '.claude' / 'projects'


def _claude_code_session_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.expanduser().resolve()).encode('utf-8')).hexdigest()[:24]
    return f'{CLAUDE_CODE_SOURCE}_{digest}'


def _parse_claude_code_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _extract_claude_code_text(content) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content[:CLAUDE_CODE_MAX_CONTENT_CHARS]
    if isinstance(content, list):
        parts = []
        used = 0
        for item in content:
            text = ''
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content') or ''
            if not text:
                continue
            text = str(text)
            remaining = CLAUDE_CODE_MAX_CONTENT_CHARS - used
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            used += len(parts[-1])
        return '\n'.join(parts)
    if isinstance(content, dict):
        return _extract_claude_code_text(content.get('text') or content.get('content'))
    return str(content)[:CLAUDE_CODE_MAX_CONTENT_CHARS]


def _parse_claude_code_jsonl(path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE) -> tuple[list[dict], str | None, float | None, float | None]:
    messages: list[dict] = []
    summary_title = None
    first_ts = None
    last_ts = None
    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if len(messages) >= max_messages:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                if not summary_title:
                    summary = raw.get('summary') or raw.get('title')
                    if isinstance(summary, str) and summary.strip():
                        summary_title = ' '.join(summary.split())[:80]
                records = raw.get('messages') if isinstance(raw.get('messages'), list) else None
                if records is None:
                    records = [raw.get('message') if isinstance(raw.get('message'), dict) else raw]
                for record in records:
                    if len(messages) >= max_messages:
                        break
                    if not isinstance(record, dict):
                        continue
                    msg = record.get('message') if isinstance(record.get('message'), dict) else record
                    role = str(msg.get('role') or record.get('role') or raw.get('role') or raw.get('type') or '').strip().lower()
                    if role == 'human':
                        role = 'user'
                    if role not in {'user', 'assistant', 'system', 'tool'}:
                        continue
                    content = _extract_claude_code_text(msg.get('content') if 'content' in msg else record.get('content'))
                    if not content.strip():
                        continue
                    ts = _parse_claude_code_timestamp(
                        msg.get('timestamp')
                        or record.get('timestamp')
                        or raw.get('timestamp')
                        or raw.get('created_at')
                    )
                    if ts is not None:
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    item = {'role': role, 'content': content}
                    if ts is not None:
                        item['timestamp'] = ts
                    messages.append(item)
    except Exception:
        return [], None, None, None
    return messages, summary_title, first_ts, last_ts


def _parse_claude_code_jsonl_cached(
    path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], str | None, float | None, float | None]:
    """``_parse_claude_code_jsonl`` memoized by the file's (path, mtime_ns, size, ctime_ns).

    The transcript files under ``~/.claude/projects`` are global and rarely
    change between sidebar builds, but parsing them dominates the cold
    /api/sessions latency (and repeats on every profile switch). Caching the
    parse result keyed by the file's stat signature collapses the warm cost to a
    single ``os.stat`` per file. A genuine append/edit bumps ``mtime_ns``/``size``
    /``ctime_ns`` and misses the cache, so staleness is impossible without
    re-parsing.

    ``max_messages`` is part of the key so a caller asking for a different cap
    never reads a result truncated to a smaller one.
    """
    try:
        st = path.stat()
        # Key on mtime_ns + size + ctime_ns: size is the strong discriminator for
        # append-only JSONL (any write changes it), and ctime_ns guards the rare
        # same-size, same-mtime in-place edit so a content change can never serve
        # a stale parse. A spurious ctime bump only costs one harmless re-parse.
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ctime_ns, int(max_messages))
    except OSError:
        # Can't stat -> fall back to a direct (uncached) parse; it will also
        # likely fail and return the empty tuple, matching prior behavior.
        return _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        hit = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if hit is not None:
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            messages, summary_title, first_ts, last_ts = hit
            # Return a shallow copy of the message list so a caller mutating it
            # can't corrupt the cached entry; the per-message dicts are treated
            # as read-only by all current callers.
            return list(messages), summary_title, first_ts, last_ts

    parsed = _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        existing = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if existing is None:
            _CLAUDE_CODE_PARSE_CACHE[key] = parsed
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            while len(_CLAUDE_CODE_PARSE_CACHE) > _CLAUDE_CODE_PARSE_CACHE_MAX:
                _CLAUDE_CODE_PARSE_CACHE.popitem(last=False)
    messages, summary_title, first_ts, last_ts = parsed
    return list(messages), summary_title, first_ts, last_ts


def clear_claude_code_parse_cache() -> None:
    """Drop all memoized Claude Code transcript parses (test/lifecycle hook)."""
    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        _CLAUDE_CODE_PARSE_CACHE.clear()


def _iter_claude_code_jsonl_files(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES):
    root = Path(projects_dir).expanduser() if projects_dir is not None else _default_claude_code_projects_dir()
    if root is None:
        return
    try:
        if root.is_symlink():
            return
        root = root.resolve(strict=False)
        if not root.exists() or not root.is_dir():
            return
        yielded = 0
        for project_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if yielded >= max_files:
                return
            try:
                if project_dir.is_symlink() or not project_dir.is_dir():
                    continue
                for path in sorted(project_dir.iterdir(), key=lambda p: p.name):
                    if yielded >= max_files:
                        return
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.jsonl':
                        continue
                    try:
                        if path.stat().st_size > max_file_bytes:
                            continue
                    except OSError:
                        continue
                    yielded += 1
                    yield path
            except OSError:
                continue
    except OSError:
        return


def _claude_code_title(messages: list[dict], summary_title: str | None) -> str:
    if summary_title:
        return summary_title
    for msg in messages:
        if msg.get('role') == 'user':
            text = ' '.join(str(msg.get('content') or '').split())
            if text:
                return text[:80]
    return 'Claude Code Session'


def get_claude_code_sessions(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES) -> list:
    """Read Claude Code JSONL sessions as read-only external-agent rows.

    The bridge is additive and defensive: it skips symlinks, oversized files,
    malformed lines, and per-file errors rather than crashing WebUI session
    listing. Tests pass ``projects_dir`` fixtures so Michael's real ~/.claude is
    never read during test runs.
    """
    sessions = []
    # ``get_last_workspace()`` is loop-invariant (the same active workspace for
    # every Claude Code row) but internally stats config.yaml + probes terminal
    # cwd, so calling it once per row was ~200 redundant stat()s on the cold
    # sidebar build (#4718). Resolve it a single time.
    cc_workspace = str(get_last_workspace())
    for path in _iter_claude_code_jsonl_files(projects_dir, max_files=max_files, max_file_bytes=max_file_bytes) or []:
        messages, summary_title, first_ts, last_ts = _parse_claude_code_jsonl_cached(path)
        if not messages:
            continue
        sid = _claude_code_session_id(path)
        # Match the truthiness fallback used in the assignments below: the old
        # inline code was ``first_ts or last_ts or path.stat().st_mtime``, which
        # also fell back to mtime for a falsy-but-not-None ``0.0`` timestamp
        # (epoch-0 / 1970 transcripts). An identity (``is None``) guard would
        # leave those rows with ``None`` instead of the file mtime, so use the
        # same ``not`` test the assignments use to stay bug-for-bug compatible.
        if not first_ts and not last_ts:
            try:
                _mtime = path.stat().st_mtime
            except OSError:
                _mtime = 0.0
        else:
            _mtime = None
        created_at = first_ts or last_ts or _mtime
        updated_at = last_ts or first_ts or _mtime
        sessions.append({
            'session_id': sid,
            'title': _claude_code_title(messages, summary_title),
            'workspace': cc_workspace,
            'model': 'claude-code',
            'message_count': len(messages),
            'created_at': created_at,
            'updated_at': updated_at,
            'last_message_at': updated_at,
            'pinned': False,
            'archived': False,
            'project_id': None,
            'profile': None,
            'source_tag': CLAUDE_CODE_SOURCE,
            'raw_source': CLAUDE_CODE_SOURCE,
            'session_source': 'external_agent',
            'source_label': CLAUDE_CODE_SOURCE_LABEL,
            'is_cli_session': True,
            'read_only': True,
        })
    sessions.sort(key=lambda s: s.get('last_message_at') or s.get('updated_at') or 0, reverse=True)
    return sessions


def get_claude_code_session_messages(sid, projects_dir: Path | str | None = None) -> list:
    """Return messages for one read-only Claude Code JSONL session."""
    sid = str(sid or '')
    if not sid.startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return []
    for path in _iter_claude_code_jsonl_files(projects_dir) or []:
        if _claude_code_session_id(path) != sid:
            continue
        messages, _summary_title, _first_ts, _last_ts = _parse_claude_code_jsonl_cached(path)
        return messages
    return []


def clear_cli_sessions_cache() -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        global _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        _CLI_SESSIONS_CACHE_INVALIDATION_VERSION += 1
        _publish_models_global(
            globals(),
            "_CLI_SESSIONS_CACHE_INVALIDATION_VERSION",
            _CLI_SESSIONS_CACHE_INVALIDATION_VERSION,
        )
        _CLI_SESSIONS_CACHE.clear()
    # The sidecar-metadata projection cache is stat-keyed (self-invalidating on
    # any file change), but clear it alongside the CLI cache so an explicit
    # reset — a mutating sidebar action or test isolation — starts fully cold.
    clear_sidecar_metadata_cache()

__all__ = ['CLAUDE_CODE_SOURCE', 'CLAUDE_CODE_SOURCE_LABEL', 'CLAUDE_CODE_MAX_FILES', 'CLAUDE_CODE_MAX_FILE_BYTES', 'CLAUDE_CODE_MAX_MESSAGES_PER_FILE', 'CLAUDE_CODE_MAX_CONTENT_CHARS', '_normalize_cli_session_source_filter', '_default_claude_code_projects_dir', '_claude_code_session_id', '_parse_claude_code_timestamp', '_extract_claude_code_text', '_parse_claude_code_jsonl', '_parse_claude_code_jsonl_cached', 'clear_claude_code_parse_cache', '_iter_claude_code_jsonl_files', '_claude_code_title', 'get_claude_code_sessions', 'get_claude_code_session_messages', 'clear_cli_sessions_cache']
