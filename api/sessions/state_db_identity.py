"""Stable state database fingerprints and cache identity."""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


def _path_stat_cache_key(path):
    if path is None:
        return None
    try:
        stat = Path(path).stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def state_db_content_fingerprint(db_path: Path):
    """Return a commit-reliable content fingerprint for a state.db.

    The stat-only key below (mtime_ns + size of the .db/-wal/-shm files) is NOT
    reliable for cache invalidation: in WAL mode a commit lands in the -wal file,
    and under fast sequential writes the (mtime_ns, size) of the sidecars can
    COLLIDE with a previously cached stamp (same nanosecond bucket + a WAL frame
    that lands at the same offset/size after a prior checkpoint truncation), so a
    freshly-committed gateway/CLI session is intermittently served from the stale
    Python cache. PRAGMA data_version does NOT help here either — read from a
    fresh per-request connection it always reports that connection's own initial
    value and never advances (verified). A cheap content fingerprint over the
    sessions/messages tables, read on a fresh connection, DOES advance on every
    commit (incl. external gateway writes) and is immune to mtime granularity.
    Cost is a pair of indexed COUNT/MAX queries (sub-ms), far cheaper than the
    full uncached session scan this key gates.
    """
    try:
        if not Path(db_path).exists():
            return None
    except OSError:
        return None
    try:
        import sqlite3
        # Read-only + a tiny busy timeout: a fingerprint read must NEVER stall the
        # /api/sessions hot path when state.db is briefly locked by a writer.
        # On lock (or any error) we return None and the caller falls back to the
        # cheap file-stat stamp, so correctness degrades gracefully to the prior
        # behavior rather than blocking for the default multi-second busy timeout.
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=0.05
            )
        except Exception:
            return None
        try:
            conn.execute("PRAGMA busy_timeout=50")
            parts = []
            for table in ("sessions", "messages"):
                try:
                    # MAX(rowid) is an O(1) index lookup (no table scan) and
                    # advances on every INSERT. Pair it with the table's largest
                    # rowid + a count-free total: we deliberately avoid COUNT(*)
                    # which forces a full SCAN on large messages tables (~tens of
                    # ms per sidebar refresh on a big store). MAX(rowid) misses a
                    # pure DELETE-without-insert, but the file-stat fallback in
                    # _sqlite_file_stat_cache_key still moves on a delete commit,
                    # and a delete never makes a MISSING row appear (the flake we
                    # fix is an ADDED row not showing up). It also misses a plain
                    # `UPDATE sessions SET title/message_count` with no message
                    # insert (state_sync.py sync) — those fall back to the stat
                    # stamp + 5s TTL, i.e. the prior behavior (a title-only rename
                    # can lag <=5s); no regression vs the old stat-only key.
                    row = conn.execute(
                        f"SELECT MAX(rowid) FROM {table}"
                    ).fetchone()
                    parts.append(row[0] if row else None)
                except Exception:
                    parts.append(None)
            return tuple(parts)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def state_db_cache_key(db_path: Path):
    """Return a commit-reliable invalidation key for a SQLite DB.

    Combines a content fingerprint (the authoritative signal — advances on every
    commit, immune to mtime-granularity collisions that flaked the gateway_sync
    test) with the cheap file stat stamps as a belt-and-suspenders fallback for
    the case where the fingerprint can't be read.
    """
    return (
        state_db_content_fingerprint(db_path),
        _path_stat_cache_key(db_path),
        _path_stat_cache_key(Path(f"{db_path}-wal")),
        _path_stat_cache_key(Path(f"{db_path}-shm")),
    )
