"""Durable Session record interface and sidecar serialization protocol.

The concrete persistence transaction stays here: metadata ordering, shrink
backups, atomic replacement, metadata-only write protection, and load-time
self-healing form one protocol. Derived indexes, tombstones, compact
projections, and sidecar fingerprints live with their dedicated owners.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import api.config as _cfg  # noqa: F401 - historical store facade compatibility
from api.config import (  # noqa: F401 - historical store facade compatibility
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    HOME,
    LOCK,
    PROJECTS_FILE,
    SESSIONS,
    SESSIONS_MAX,
    SESSION_DIR,
    SESSION_INDEX_FILE,
    get_effective_default_model,
    session_agent_lock as _get_session_agent_lock,
)  # noqa: F401 - historical store facade compatibility
from api.workspace import get_last_workspace  # noqa: F401 - historical facade

from . import session_index as _session_index
from . import sidecar_metadata as _sidecar
from . import tombstones as _tombstones
from .atomic_io import (  # noqa: F401 - historical store facade compatibility
    WINDOWS_REPLACE_INITIAL_DELAY as _WINDOWS_REPLACE_INITIAL_DELAY,
    WINDOWS_REPLACE_MAX_RETRIES as _WINDOWS_REPLACE_MAX_RETRIES,
    safe_replace as _safe_replace,
)  # noqa: F401 - historical store facade compatibility
from .record_projection import _SessionProjectionMixin
from .record_recovery import (  # noqa: F401 - record interface compatibility
    _active_stream_ids,
    _append_recovered_context_projection,
    _append_recovered_pending_turn,
    _append_recovered_turn_to_context,
    _content_has_reasoning_only_parts,
    _is_empty_partial_activity_message,
    _is_streaming_session,
    _last_message_timestamp,
    _latest_user_matches_pending_text,
    _message_matches_pending_checkpoint,
    _message_matches_pending_text,
    _message_role,
    _message_timestamp,
    _normalize_journal_recovery_text,
    _recovered_model_context_projection,
    _seed_recovered_context_from_messages,
    _session_sort_timestamp,
)  # noqa: F401 - record interface compatibility
from .sidecar_metadata import (  # noqa: F401 - record interface compatibility
    _anchor_scene_index_from_records,
    _collapse_adjacent_duplicate_partials,
    _disk_scene_fingerprint,
    _find_top_level_json_key,
    _parse_nonnegative_int,
    _partial_message_signature,
    _read_file_head,
    _read_metadata_json_prefix,
    _sidecar_stat_signature,
    is_safe_session_id,
    model_explicit_pick_signature,
)  # noqa: F401 - record interface compatibility

logger = logging.getLogger(__name__)

# Compatibility names remain on the record interface while ownership lives in
# the dedicated modules. All path-sensitive calls below pass the live record
# store paths explicitly, so profile switches and isolated tests cannot leak to
# the process defaults captured by another module.
_INDEX_WRITE_LOCK = _session_index._INDEX_WRITE_LOCK
_SESSION_INDEX_REBUILD_LOCK = _session_index._SESSION_INDEX_REBUILD_LOCK
_STALE_TMP_AGE_SECONDS = _session_index._STALE_TMP_AGE_SECONDS
_PERSISTED_SESSION_IDS_CACHE = _session_index._PERSISTED_SESSION_IDS_CACHE
_SAFE_SID_CHARS = _sidecar._SAFE_SID_CHARS
_LEGACY_SIDECAR_FACTS_LOCK = _sidecar.LEGACY_SIDECAR_FACTS_LOCK
_LEGACY_SIDECAR_FACTS = _sidecar.LEGACY_SIDECAR_FACTS
_LEGACY_SIDECAR_FACTS_MAX = _sidecar.LEGACY_SIDECAR_FACTS_MAX
_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK = _tombstones._WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK
_WEBUI_DELETED_SESSION_TOMBSTONE_LOCK = _tombstones._WEBUI_DELETED_SESSION_TOMBSTONE_LOCK
WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP = _tombstones.WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP
WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION = _tombstones.WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION
WEBUI_DELETED_SESSION_TOMBSTONE_CAP = _tombstones.WEBUI_DELETED_SESSION_TOMBSTONE_CAP
WEBUI_DELETED_SESSION_TOMBSTONE_VERSION = _tombstones.WEBUI_DELETED_SESSION_TOMBSTONE_VERSION


def _cleanup_stale_tmp_files() -> None:
    _session_index.cleanup_stale_tmp_files(SESSION_DIR)


def _persisted_session_ids_snapshot() -> frozenset[str]:
    return _session_index.persisted_session_ids_snapshot(SESSION_DIR)


def _session_dir_has_persisted_session_files() -> bool:
    return _session_index.session_dir_has_persisted_session_files(SESSION_DIR)


def _start_session_index_rebuild_thread() -> None:
    _session_index.start_session_index_rebuild_thread(SESSION_DIR, SESSION_INDEX_FILE)


def _rebuild_session_index_background(expected_session_dir: Path, expected_index_file: Path) -> None:
    _session_index._rebuild_session_index_background(expected_session_dir, expected_index_file)


def _index_entry_exists(session_id: str, in_memory_ids=None) -> bool:
    return _session_index.index_entry_exists(session_id, in_memory_ids, session_dir=SESSION_DIR)


def _write_session_index(updates=None, *, session_dir: Path | None = None, session_index_file: Path | None = None):
    return _session_index.write_session_index(
        updates=updates,
        session_dir=session_dir or SESSION_DIR,
        session_index_file=session_index_file or SESSION_INDEX_FILE,
    )


def prune_session_from_index(session_id: str) -> None:
    _session_index.prune_session_from_index(
        session_id,
        session_dir=SESSION_DIR,
        session_index_file=SESSION_INDEX_FILE,
    )


def _webui_zero_message_orphan_tombstone_file() -> Path:
    return _tombstones.zero_message_orphan_tombstone_file(SESSION_DIR)


def _load_webui_zero_message_orphan_tombstone() -> frozenset[str]:
    return _tombstones.load_zero_message_orphan_ids(SESSION_DIR)


def _save_webui_zero_message_orphan_tombstone(ids) -> None:
    _tombstones.save_zero_message_orphan_ids(SESSION_DIR, ids)


def _record_webui_zero_message_orphan_tombstone(sid: str) -> None:
    _tombstones.record_zero_message_orphan(SESSION_DIR, sid)


def _clear_webui_zero_message_orphan_tombstone(sid: str) -> None:
    _tombstones.clear_zero_message_orphan(SESSION_DIR, sid)


def _webui_deleted_session_tombstone_file() -> Path:
    return _tombstones.deleted_session_tombstone_file(SESSION_DIR)


def _load_webui_deleted_session_tombstone() -> frozenset[str]:
    return _tombstones.load_deleted_session_ids(SESSION_DIR)


def _save_webui_deleted_session_tombstone(ids) -> None:
    _tombstones.save_deleted_session_ids(SESSION_DIR, ids)


def _record_webui_deleted_session_tombstone(sid: str) -> None:
    _tombstones.record_deleted_session(SESSION_DIR, sid)


def _clear_webui_deleted_session_tombstone(sid: str) -> None:
    _tombstones.clear_deleted_session(SESSION_DIR, sid)


def _legacy_sidecar_facts_get(sid):
    return _sidecar.legacy_sidecar_facts_get(SESSION_DIR, sid)


def _legacy_sidecar_facts_put(sid, message_count, scene_index, *, expected_sig):
    return _sidecar.legacy_sidecar_facts_put(
        SESSION_DIR,
        sid,
        message_count,
        scene_index,
        expected_sig=expected_sig,
    )


def __getattr__(name):
    if name in {"_SESSION_INDEX_REBUILD_THREAD", "_SESSION_INDEX_REBUILD_THREAD_TARGET"}:
        return getattr(_session_index, name)
    raise AttributeError(name)

def _load_session_from_path(path: Path) -> "Session | None":
    """Load a session from an explicit JSON path without consulting SESSION_DIR."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
    return Session(**data)


def _lookup_index_message_count(session_id):
    """Return the indexed message count without loading the full session file."""
    return _index_message_count_map().get(str(session_id))


def _index_message_count_map(entries=None) -> dict[str, int]:
    """Return indexed message counts keyed by session id.

    ``load_metadata_only()`` is called in loops for stale lineage/sidebar rows.
    Reading and parsing ``_index.json`` once per row turns /api/sessions into an
    accidental O(n²) poll for old sidecars that predate persisted
    ``message_count``. Accepting already-loaded index rows lets callers reuse
    the index they just parsed.
    """
    if entries is None:
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return {}
    if not isinstance(entries, list):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('session_id') or '')
        if not sid:
            continue
        count = entry.get('message_count')
        if not isinstance(count, int):
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
        if count >= 0:
            counts[sid] = count
    return counts



class _SessionPersistenceMixin:
    @property
    def path(self):
        return SESSION_DIR / f'{self.session_id}.json'

    def save(
        self,
        touch_updated_at: bool = True,
        skip_index: bool = False,
        *,
        _admission_compensation: bool = False,
    ) -> None:
        if not is_safe_session_id(self.session_id):
            raise ValueError(f"Unsafe session_id {self.session_id!r}; refusing to write outside session store")
        # ── #1558 P0 guard ──────────────────────────────────────────────
        # Refuse to save a session that was loaded with metadata_only=True.
        # Such sessions have messages=[] (it's the whole point of the partial
        # load), and save() unconditionally writes self.messages to disk via
        # an atomic os.replace(). Saving a metadata-only stub thus wipes the
        # full conversation history — which is exactly the v0.50.279
        # _clear_stale_stream_state() regression that lost users 1000+
        # message conversations. Any caller that needs to mutate persisted
        # fields on a metadata-only session must reload with
        # metadata_only=False first.
        if getattr(self, '_loaded_metadata_only', False):
            raise RuntimeError(
                f"Refusing to save metadata-only session {self.session_id!r}: "
                f"would atomically overwrite on-disk messages with []. "
                f"Reload with metadata_only=False before mutating state. "
                f"See #1558."
            )
        if touch_updated_at:
            self.updated_at = time.time()
        # Write metadata fields first so load_metadata_only() can read them
        # without parsing the full messages array (which may be 400KB+).
        # Fields are listed in the order they should appear in the JSON file.
        METADATA_FIELDS = [
            'session_id', 'title', 'workspace', 'model', 'model_provider', 'model_explicit_pick_signature', 'created_at', 'updated_at',
            'pinned', 'archived', 'project_id', 'profile',
            'input_tokens', 'output_tokens', 'estimated_cost',
            'cache_read_tokens', 'cache_write_tokens',
            'personality', 'active_stream_id',
            'pending_user_message', 'pending_attachments', 'pending_started_at', 'pending_user_source',
            'compression_anchor_visible_idx', 'compression_anchor_message_key',
            'compression_anchor_summary', 'pre_compression_snapshot',
            'context_engine', 'compression_anchor_engine', 'compression_anchor_mode',
            'compression_anchor_details', 'context_engine_state',
            'context_length', 'threshold_tokens', 'last_prompt_tokens',
            'post_compression_context_tokens_estimate',
            'compression_recovery', 'recommended_recovery_action',
            'compression_recovery_source_session_id', 'compression_recovery_action',
            'truncation_watermark',
            'truncation_boundary',
            'clear_generation',
            'gateway_routing', 'gateway_routing_history', 'llm_title_generated', 'manual_title',
            'parent_session_id',
            'worktree_path', 'worktree_branch', 'worktree_repo_root', 'worktree_created_at',
            'is_cli_session', 'source_tag', 'raw_source', 'session_source', 'source_label',
            'user_id', 'chat_id', 'chat_type', 'thread_id', 'session_key', 'platform',
            'read_only',
            'enabled_toolsets', 'composer_draft',
            'process_wakeup_pause',
            'share_token', 'share_created_at',
        ]
        meta = {k: getattr(self, k, None) for k in METADATA_FIELDS}
        # #5854: message_count and a compact anchor-scene fingerprint go in the
        # metadata prefix (BEFORE messages) so load_metadata_only() and the
        # sidebar-poll freshness check never have to parse the full (250-480KB)
        # scene bodies. message_count is placed BEFORE anchor_scene_index so a
        # legacy-format reader that stops at a scene key still finds the count.
        # The full anchor_activity_scenes bodies serialize AFTER messages.
        meta['message_count'] = len(self.messages or [])
        meta['anchor_scene_index'] = _anchor_scene_index_from_records(self.anchor_activity_scenes)
        # Keep the in-memory fingerprint aligned with what we just persisted, so a
        # later metadata-only reload of THIS object (or any fingerprint reader)
        # sees the current value rather than a stale load-time snapshot (#5854
        # defense-in-depth; the cached-side freshness check reads real records,
        # not this, so this is belt-and-suspenders).
        self._anchor_scene_index = dict(meta['anchor_scene_index'])
        meta['messages'] = self.messages
        meta['tool_calls'] = self.tool_calls
        meta['anchor_activity_scenes'] = self.anchor_activity_scenes if isinstance(self.anchor_activity_scenes, dict) else {}
        # Fields not in METADATA_FIELDS (e.g. last_usage) go at the end. Exclude
        # the keys we placed explicitly above so they aren't emitted twice.
        _placed = {'message_count', 'anchor_scene_index', 'messages', 'tool_calls', 'anchor_activity_scenes'}
        extra = {k: v for k, v in self.__dict__.items()
                 if k not in METADATA_FIELDS and k not in _placed
                 and not k.startswith('_')}
        payload = json.dumps({**meta, **extra}, ensure_ascii=False, indent=2)

        # ── #1558 backup safeguard ──────────────────────────────────────
        # Before overwriting the session file, copy the previous version to
        # ``<sid>.json.bak`` IFF the previous file has more messages than the
        # incoming payload. The asymmetric guard means:
        #   * Normal grow-the-conversation saves never produce a backup
        #     (incoming messages >= existing) — keeps disk overhead near zero.
        #   * Any save that would shrink the messages array (the failure mode
        #     of #1558, plus anything similar in the future) leaves a recoverable
        #     snapshot of the pre-shrink state on disk.
        # The recovery path is api/session_recovery.py — at server startup and
        # via /api/session/recover, sessions whose JSON has fewer messages than
        # their .bak get restored automatically.
        try:
            if self.path.exists():
                existing_text = self.path.read_text(encoding='utf-8')
                try:
                    existing = json.loads(existing_text)
                    existing_msg_count = len(existing.get('messages') or [])
                except (json.JSONDecodeError, ValueError):
                    existing_msg_count = -1  # corrupt → always back up
                incoming_msg_count = len(self.messages or [])
                if (
                    existing_msg_count > 0
                    and incoming_msg_count == 0
                    and (self.active_stream_id or self.pending_user_message)
                    and not _admission_compensation
                ):
                    logger.warning(
                        "refusing to overwrite session %s messages with empty active/pending snapshot "
                        "(existing=%s, incoming=%s, stream=%s)",
                        self.session_id,
                        existing_msg_count,
                        incoming_msg_count,
                        self.active_stream_id,
                    )
                    return
                if existing_msg_count > incoming_msg_count and not _admission_compensation:
                    bak_path = self.path.with_suffix('.json.bak')
                    # SHOULD-FIX #2 (Opus): atomic write via tmp+replace,
                    # mirroring the main save() pattern below. Prevents a
                    # torn .bak from a crash mid-write or a concurrent
                    # backup-producing save. Recovery defends against a
                    # torn .bak (JSONDecodeError → no_action), so the
                    # failure mode pre-fix was "backup is lost"; with
                    # this fix the backup either lands cleanly or doesn't
                    # land at all.
                    try:
                        bak_tmp = bak_path.with_suffix(
                            f'.bak.tmp.{os.getpid()}.{threading.current_thread().ident}'
                        )
                        with open(bak_tmp, 'w', encoding='utf-8') as bf:
                            bf.write(existing_text)
                            bf.flush()
                            os.fsync(bf.fileno())
                        _safe_replace(bak_tmp, bak_path)
                    except OSError:
                        # Backup is best-effort; main save proceeds regardless.
                        try:
                            bak_tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
        except OSError:
            pass

        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            _safe_replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if not skip_index:
            _write_session_index(updates=[self])

        # #4985 belt-and-suspenders self-heal: a successful save with at
        # least one real message on the sidecar is unconditional proof the
        # row is alive (the #4985 "zero-message orphan" only ever exists
        # when ``len(self.messages) == 0``). Clear the tombstone so the
        # next ``/api/sessions`` poll does not need the prune helper to
        # run before the row re-appears — useful when the message-commit
        # happens on a poll that does not yet see state.db.messages rows
        # (e.g. the WebUI's own sidecar commit lands before the agent's
        # state.db append, or the helper is skipped via a different code
        # path). Wrapped because a tombstone failure must never block a
        # save. The helper's self-healing branch in
        # ``_prune_orphaned_webui_zero_message_sessions`` is the primary
        # fix; this is the belt.
        if self.messages:
            try:
                _clear_webui_zero_message_orphan_tombstone(self.session_id)
                _clear_webui_deleted_session_tombstone(self.session_id)
            except Exception:
                logger.debug(
                    "Failed to clear webui tombstone for %s",
                    self.session_id,
                    exc_info=True,
                )

    @classmethod
    def load(cls, sid):
        # Validate session ID format to prevent path traversal.  API/gateway
        # session ids may contain hyphens (for example ``api-*`` and
        # ``reachy-voice-*``); allow those but still reject dots/slashes.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        # #5854: snapshot the stat signature BEFORE reading so a legacy-facts
        # cache write is only committed if the file didn't change under us
        # during the parse (TOCTOU guard against an atomic replace mid-read).
        _pre_read_sig = _sidecar_stat_signature(p)
        data = json.loads(p.read_text(encoding='utf-8'))
        data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
        session = cls(**data)
        if _collapsed_partials:
            try:
                # Self-heal bloated sessions on first full load without touching
                # recency/index ordering; save() creates a .bak because this
                # intentionally shrinks the transcript (#2592).
                session.save(touch_updated_at=False, skip_index=True)
            except Exception:
                logger.debug("Failed to persist collapsed duplicate partials for %s", sid, exc_info=True)
        else:
            # #5854: for a LEGACY sidecar (no modern anchor_scene_index key), the
            # cheap metadata-prefix read cannot recover message_count/scenes when
            # scenes serialize before them, so cache the authoritative facts we
            # just parsed. This keeps the metadata-only path and the eviction
            # check from full-parsing this unchanged file again on every poll.
            # Keyed by stat signature, so any edit invalidates it; the next
            # save() rewrites the modern layout and the fallback stops firing.
            # expected_sig guards against an atomic replace during the read.
            # (When _collapsed_partials fired, save() above already rewrote the
            # modern layout, so no legacy caching is needed.)
            if 'anchor_scene_index' not in data:
                try:
                    _legacy_sidecar_facts_put(
                        sid,
                        len(getattr(session, 'messages', None) or []),
                        _anchor_scene_index_from_records(getattr(session, 'anchor_activity_scenes', None)),
                        expected_sig=_pre_read_sig,
                    )
                except Exception:
                    logger.debug("legacy sidecar facts cache populate failed for %s", sid, exc_info=True)
        return session

    @classmethod
    def load_metadata_only(cls, sid, *, index_message_counts=None):
        """Load only the compact metadata fields, skipping the messages array.

        Session JSON files have metadata fields (session_id, title, model, etc.)
        at the top level, before the large messages array. Read only up to the
        top-level "messages" field and synthesize a small metadata-only object.
        Falls back to load() for legacy or unexpected file layouts.
        """
        # Same path-safety contract as load(): hyphens are valid session ids,
        # path separators and traversal dots are not.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        try:
            prefix = _read_metadata_json_prefix(p)
            if not prefix:
                return cls.load(sid)
            parsed = json.loads(prefix)
            needed = {'session_id', 'title', 'created_at', 'updated_at'}
            if not needed.issubset(parsed.keys()):
                return cls.load(sid)
            parsed['messages'] = []
            parsed['tool_calls'] = []
            session = cls(**parsed)
            sidecar_message_count = _parse_nonnegative_int(parsed.get('message_count'))
            index_message_count = None
            if sidecar_message_count is None:
                if index_message_counts is not None:
                    index_message_count = index_message_counts.get(str(sid))
                else:
                    index_message_count = _lookup_index_message_count(sid)
            # #5854 legacy-layout recovery: a pre-#5854 sidecar serialized
            # anchor_activity_scenes BEFORE message_count, so on a large-scene
            # legacy file the cheap prefix now stops at the scenes key and
            # captures NO message_count. The sidebar _index.json count can lag
            # behind external sidecar appends, so trusting it here would report a
            # stale/zero count and could drop an unsaved user tail on the next
            # get_session cache-replace. When the prefix carries NEITHER
            # message_count NOR the modern anchor_scene_index key (⇒ a legacy
            # file whose count fell after the scenes), recover the authoritative
            # facts. To avoid re-parsing an unchanged legacy file on every poll
            # (which would recreate the #4633 churn for legacy sidecars that are
            # never re-saved), consult a bounded stat-signature cache first and
            # only full-load on a miss, caching the result. The next save()
            # rewrites the modern layout so the fallback stops firing entirely.
            # A MODERN file always carries message_count in the prefix, so it
            # never reaches here — a genuine 0 stays 0.
            if (
                sidecar_message_count is None
                and 'anchor_scene_index' not in parsed
            ):
                _facts = _legacy_sidecar_facts_get(sid)
                if _facts is not None:
                    parsed['anchor_scene_index'] = _facts.get('scene_index') or {}
                    session = cls(**parsed)
                    session._metadata_message_count = _parse_nonnegative_int(_facts.get('message_count'))
                    session._loaded_metadata_only = True
                    return session
                # Cache miss → full-load. cls.load() itself populates the legacy
                # facts cache with a TOCTOU-guarded write (expected_sig), so we
                # do NOT re-cache here (an unguarded second write could stamp
                # stale facts under a replacement file's signature — Codex r5).
                return cls.load(sid)
            # Modern sidecars carry an accurate message_count, so it is the
            # source of truth and we skip the per-row _index.json read in the
            # common case. The sidebar index is only a cache (it can lag behind
            # external sidecar appends/backfills), so consult it solely as a
            # fallback when the sidecar has no count. When both are present we
            # still take the largest known count as a defensive measure.
            known_counts = [
                count for count in (index_message_count, sidecar_message_count)
                if count is not None
            ]
            session._metadata_message_count = max(known_counts) if known_counts else None
            # Mark this session as a metadata-only stub. save() refuses to write
            # such a session because doing so would atomically replace the
            # on-disk JSON with messages=[], wiping the conversation. Any
            # caller that needs to mutate persisted state on a metadata-only
            # session must reload it with metadata_only=False first.
            # See #1558 — v0.50.279 _clear_stale_stream_state() data-loss bug.
            session._loaded_metadata_only = True
            return session
        except Exception:
            # Corrupt prefix or decode error — fall back to full load
            return cls.load(sid)



class Session(_SessionPersistenceMixin, _SessionProjectionMixin):
    def __init__(self, session_id: str=None, title: str='Untitled',
                 workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL,
                 model_provider=None,
                 messages=None, created_at=None, updated_at=None,
                 tool_calls=None, pinned: bool=False, archived: bool=False,
                 project_id: str=None, profile=None,
                 input_tokens: int=0, output_tokens: int=0, estimated_cost=None,
                 cache_read_tokens: int=0, cache_write_tokens: int=0,
                 personality=None,
                 active_stream_id: str=None,
                 pending_user_message: str=None,
                 pending_attachments=None,
                 pending_started_at=None,
                 pending_user_source: str=None,
                 context_messages=None,
                 compression_anchor_visible_idx=None,
                 compression_anchor_message_key=None,
                 compression_anchor_summary=None,
                 pre_compression_snapshot: bool=False,
                 context_engine=None,
                 compression_anchor_engine=None,
                 compression_anchor_mode=None,
                 compression_anchor_details=None,
                 context_engine_state=None,
                 context_length=None, threshold_tokens=None,
                 last_prompt_tokens=None,
                 post_compression_context_tokens_estimate=None,
                 compression_recovery=None,
                 recommended_recovery_action=None,
                 compression_recovery_source_session_id=None,
                 compression_recovery_action=None,
                 truncation_watermark=None,
                 truncation_boundary=None,
                 clear_generation=None,
                 gateway_routing=None, gateway_routing_history=None,
                 llm_title_generated: bool=False,
                 manual_title: bool=False,
                parent_session_id: str=None,
                worktree_path=None,
                worktree_branch=None,
                 worktree_repo_root=None,
                 worktree_created_at=None,
                 enabled_toolsets=None,
                 composer_draft=None,
                 anchor_activity_scenes=None,
                 process_wakeup_pause=None,
                 share_token=None,
                 share_created_at=None,
                 **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.model = model
        self.model_provider = str(model_provider).strip().lower() if model_provider else None
        # #5979: signature of the model the user DELIBERATELY picked this session
        # (``"<model>\x1f<provider>"``), or None. Used by the streaming resolver
        # to preserve a custom-proxy vendor namespace on a COLD catalog ONLY when
        # the current routing context still matches what was picked. Storing a
        # SIGNATURE (not a bare bool) means any later model/provider change — via
        # /api/chat/start, /api/session/update, normalization, or provider repair
        # — automatically invalidates the pick (the signatures no longer match),
        # so a stale first-party leftover (#433) is never wrongly preserved.
        # Restored from persisted metadata on load (arrives via **kwargs).
        self.model_explicit_pick_signature = kwargs.get('model_explicit_pick_signature') or None
        self.messages = messages or []
        self.tool_calls = tool_calls or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.pinned = bool(pinned)
        self.archived = bool(archived)
        self.project_id = project_id or None
        self.profile = profile
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.estimated_cost = estimated_cost
        self.cache_read_tokens = cache_read_tokens or 0
        self.cache_write_tokens = cache_write_tokens or 0
        self.personality = personality
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = pending_attachments or []
        self.pending_started_at = pending_started_at
        self.pending_user_source = pending_user_source
        self.context_messages = context_messages if isinstance(context_messages, list) else []
        self.compression_anchor_visible_idx = compression_anchor_visible_idx
        self.compression_anchor_message_key = compression_anchor_message_key
        self.compression_anchor_summary = compression_anchor_summary
        self.pre_compression_snapshot = bool(pre_compression_snapshot)
        self.context_engine = context_engine
        self.compression_anchor_engine = compression_anchor_engine
        self.compression_anchor_mode = compression_anchor_mode
        self.compression_anchor_details = compression_anchor_details if isinstance(compression_anchor_details, dict) else {}
        self.context_engine_state = context_engine_state if isinstance(context_engine_state, dict) else {}
        self.context_length = context_length
        self.threshold_tokens = threshold_tokens
        self.last_prompt_tokens = last_prompt_tokens
        _post_compression_tokens = _parse_nonnegative_int(post_compression_context_tokens_estimate)
        self.post_compression_context_tokens_estimate = (
            _post_compression_tokens if _post_compression_tokens and _post_compression_tokens > 0 else None
        )
        self.compression_recovery = compression_recovery if isinstance(compression_recovery, dict) else {}
        self.recommended_recovery_action = recommended_recovery_action
        self.compression_recovery_source_session_id = (
            str(compression_recovery_source_session_id).strip()
            if compression_recovery_source_session_id
            else None
        )
        self.compression_recovery_action = (
            str(compression_recovery_action).strip()
            if compression_recovery_action
            else None
        )
        self.truncation_watermark = truncation_watermark
        self.truncation_boundary = truncation_boundary
        self.clear_generation = clear_generation
        self.gateway_routing = gateway_routing if isinstance(gateway_routing, dict) else None
        self.gateway_routing_history = gateway_routing_history if isinstance(gateway_routing_history, list) else []
        self.llm_title_generated = bool(llm_title_generated)
        self.manual_title = bool(manual_title)
        self.parent_session_id = parent_session_id
        self.worktree_path = str(Path(worktree_path).expanduser().resolve()) if worktree_path else None
        self.worktree_branch = str(worktree_branch) if worktree_branch else None
        self.worktree_repo_root = str(Path(worktree_repo_root).expanduser().resolve()) if worktree_repo_root else None
        self.worktree_created_at = worktree_created_at
        self.is_cli_session = bool(kwargs.get('is_cli_session', False))
        self.source_tag = kwargs.get('source_tag')
        self.raw_source = kwargs.get('raw_source')
        self.session_source = kwargs.get('session_source')
        self.source_label = kwargs.get('source_label')
        self.user_id = kwargs.get('user_id')
        self.chat_id = kwargs.get('chat_id')
        self.chat_type = kwargs.get('chat_type')
        self.thread_id = kwargs.get('thread_id')
        self.session_key = kwargs.get('session_key')
        self.platform = kwargs.get('platform')
        self.read_only = bool(kwargs.get('read_only', False))
        self.enabled_toolsets = enabled_toolsets  # List[str] or None — per-session toolset override
        self.composer_draft = composer_draft if isinstance(composer_draft, dict) else {}
        self.anchor_activity_scenes = anchor_activity_scenes if isinstance(anchor_activity_scenes, dict) else {}
        self.process_wakeup_pause = process_wakeup_pause if isinstance(process_wakeup_pause, dict) else {}
        self.share_token = str(share_token).strip() if share_token else None
        self.share_created_at = share_created_at
        # #5854: a compact fingerprint of anchor_activity_scenes ({scene_key:
        # updated_at}) persisted BEFORE the messages array so the sidebar-poll
        # freshness check can compare scene freshness without parsing the full
        # (often 250-480KB) scene bodies, which serialize AFTER messages. None
        # on legacy sidecars (scenes-before-messages, no fingerprint) — callers
        # fall back to reading keys/updated_at off anchor_activity_scenes.
        _raw_scene_index = kwargs.get('anchor_scene_index')
        self._anchor_scene_index = _raw_scene_index if isinstance(_raw_scene_index, dict) else None
        raw_message_count = kwargs.get('message_count')
        parsed_message_count = None
        if raw_message_count is not None:
            try:
                parsed_message_count = int(raw_message_count)
            except (TypeError, ValueError):
                parsed_message_count = None
        self._metadata_message_count = parsed_message_count if parsed_message_count is not None and parsed_message_count >= 0 else None
