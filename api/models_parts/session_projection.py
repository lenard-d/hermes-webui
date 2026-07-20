"""Session compact/sidebar projection mixin.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

class _SessionProjectionMixin:
    @staticmethod
    def _compute_user_message_count(messages) -> int:
        """perf(session-load-latency) Priority 1: bounded in-memory count.

        Returns the number of messages with role='user' in ``messages``.
        Pre-patch compact() did the same O(N) walk inline; the walk is
        extracted here so it can be measured and bounded independently.

        On the test corpus (a 2,400-message sidecar) this walk runs in
        tens of milliseconds on a Celeron N3350 with eMMC. Cost is
        proportional to the sidecar length the caller already loaded, not
        to anything new we read from disk.

        Critical: this walks ``messages`` (the sidecar) and NOT state.db.
        A previous version of this helper queried state.db for the same
        count, but the two sources can diverge by hundreds of messages
        during recovery / mid-flight writes / pending_user_message, and
        the sidebar's stale-row detection (see
        ``_looks_like_stale_zero_message_row`` and
        ``_row_may_need_sidecar_metadata_refresh``) consumes this field as
        if the sidecar were the source of truth. Mixing the two sources
        would silently flip the field's semantics.
        """
        if not isinstance(messages, list):
            return 0
        n = 0
        for m in messages:
            if isinstance(m, dict):
                # Inline role check to avoid the _message_role helper call
                # on every iteration. dict.get('role') with default '' is
                # materially faster than a function call for the hot loop.
                role = m.get('role')
                if isinstance(role, str) and role == 'user':
                    n += 1
        return n

    def compact(self, include_runtime=False, active_stream_ids=None) -> dict:
        active_stream_ids = active_stream_ids if active_stream_ids is not None else set()
        has_pending_user_message = bool(self.pending_user_message)
        message_count = (
            self._metadata_message_count
            if self._metadata_message_count is not None
            else len(self.messages)
        )
        if has_pending_user_message:
            message_count = max(message_count, 1)
        last_message_at = _last_message_timestamp(self.messages) or self.updated_at
        if has_pending_user_message and self.pending_started_at:
            last_message_at = self.pending_started_at
        return {
            'session_id': self.session_id,
            'title': self.title,
            'workspace': self.workspace,
            'model': self.model,
            'model_provider': self.model_provider,
            'message_count': message_count,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'last_message_at': last_message_at,
            'pinned': self.pinned,
            'archived': self.archived,
            'project_id': self.project_id,
            'profile': self.profile,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost': self.estimated_cost,
            'cache_read_tokens': self.cache_read_tokens,
            'cache_write_tokens': self.cache_write_tokens,
            'cache_hit_percent': prompt_cache_hit_percent(self.cache_read_tokens, self.input_tokens),
            'personality': self.personality,
            'compression_anchor_visible_idx': self.compression_anchor_visible_idx,
            'compression_anchor_message_key': self.compression_anchor_message_key,
            'compression_anchor_summary': self.compression_anchor_summary,
            'pre_compression_snapshot': self.pre_compression_snapshot,
            'context_engine': self.context_engine,
            'compression_anchor_engine': self.compression_anchor_engine,
            'compression_anchor_mode': self.compression_anchor_mode,
            'compression_anchor_details': self.compression_anchor_details,
            'context_engine_state': self.context_engine_state,
            'context_length': self.context_length,
            'threshold_tokens': self.threshold_tokens,
            'last_prompt_tokens': self.last_prompt_tokens,
            'post_compression_context_tokens_estimate': self.post_compression_context_tokens_estimate,
            'compression_recovery': self.compression_recovery,
            'recommended_recovery_action': self.recommended_recovery_action,
            'gateway_routing': self.gateway_routing,
            'gateway_routing_history': self.gateway_routing_history,
            'manual_title': self.manual_title,
            # Only emit 'parent_session_id' when set (the /branch fork link, #1342).
            # Sessions without a fork must not leak None — see test_session_lineage_metadata_api.
            **({'parent_session_id': self.parent_session_id} if self.parent_session_id else {}),
            **({
                'compression_recovery_source_session_id': self.compression_recovery_source_session_id,
                'compression_recovery_action': self.compression_recovery_action,
            } if (self.compression_recovery_source_session_id or self.compression_recovery_action) else {}),
            **({
                'worktree_path': self.worktree_path,
                'worktree_branch': self.worktree_branch,
                'worktree_repo_root': self.worktree_repo_root,
                'worktree_created_at': self.worktree_created_at,
            } if self.worktree_path else {}),
            'user_message_count': Session._compute_user_message_count(self.messages),
            'active_stream_id': self.active_stream_id,
            'pending_user_message': self.pending_user_message,
            'has_pending_user_message': has_pending_user_message,
            'is_cli_session': self.is_cli_session,
            'source_tag': self.source_tag,
            'raw_source': self.raw_source,
            'session_source': self.session_source,
            'source_label': self.source_label,
            'read_only': self.read_only,
            'enabled_toolsets': self.enabled_toolsets,
            'composer_draft': self.composer_draft if isinstance(self.composer_draft, dict) else {},
            'process_wakeup_pause': self.process_wakeup_pause if isinstance(self.process_wakeup_pause, dict) else {},
            'share_token': self.share_token,
            'share_created_at': self.share_created_at,
            'is_streaming': _is_streaming_session(
                self.active_stream_id, active_stream_ids
            ) if include_runtime else False,
        }

__all__ = []
