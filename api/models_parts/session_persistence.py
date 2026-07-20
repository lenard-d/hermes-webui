"""Session disk persistence and metadata loading mixin.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

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

__all__ = []
