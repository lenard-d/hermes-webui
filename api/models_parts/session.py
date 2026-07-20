"""Canonical WebUI Session class.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals
from api.models_parts.session_persistence import _SessionPersistenceMixin
from api.models_parts.session_projection import _SessionProjectionMixin

seed_module_globals(globals())

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

__all__ = ['Session']
