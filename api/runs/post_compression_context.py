"""Post-compression context budgeting and display-metadata restoration."""

from __future__ import annotations

import copy

import logging

from api.sessions.records import (
    _is_empty_partial_activity_message as is_empty_partial_activity_message,
)

from .message_sanitization import (
    _API_SAFE_MSG_KEYS,
    _api_safe_message_positions,
    _deduplicate_context_messages,
    _is_reasoning_only_assistant_message,
    _strip_oob_blocks,
)
from .stale_user_context import _raw_message_text


logger = logging.getLogger(__name__)


POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS = 4096
POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS = 256
POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG = "_webui_pruned_tool_result_summary"
POST_COMPRESSION_TOOL_RESULT_MARKER = "[WebUI compressed-context budget:"
ROUGH_TOKEN_CHARS = 4

_POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS = POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS
_POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS = POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS
_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG = POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG
_POST_COMPRESSION_TOOL_RESULT_MARKER = POST_COMPRESSION_TOOL_RESULT_MARKER
_ROUGH_TOKEN_CHARS = ROUGH_TOKEN_CHARS


def _positive_int_value(value, default: int = 0) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _rough_text_token_count(text: str) -> int:
    text = str(text or "")
    if not text:
        return 0
    return max(1, (len(text) + (_ROUGH_TOKEN_CHARS - 1)) // _ROUGH_TOKEN_CHARS)


def _post_compression_tool_result_budget(compressor) -> int:
    budget = _positive_int_value(
        getattr(compressor, 'tail_token_budget', None),
        _POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS,
    )
    threshold = _positive_int_value(getattr(compressor, 'threshold_tokens', None), 0)
    if threshold:
        budget = min(budget, max(512, threshold // 2))
    return max(512, budget)


def _compressed_context_tool_result_summary(
    text: str,
    *,
    original_tokens: int,
    keep_tokens: int,
) -> str:
    text = str(text or "")
    keep_chars = max(0, int(keep_tokens or 0) * _ROUGH_TOKEN_CHARS)
    note_prefix = f"{_POST_COMPRESSION_TOOL_RESULT_MARKER} omitted "
    note_suffix = (
        f" (~{int(original_tokens or 0)} rough tokens) from this tool result; "
        "the full output remains in the visible transcript/tool log.]"
    )
    if keep_chars < (
        _POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS * _ROUGH_TOKEN_CHARS
    ):
        return f"{note_prefix}{len(text)} chars{note_suffix}"
    note_len_for_budget = len(f"{note_prefix}{len(text)} of {len(text)} chars{note_suffix}")
    snippet_limit = max(0, keep_chars - note_len_for_budget - 2)
    snippet = text[:snippet_limit].rstrip()
    if not snippet:
        return f"{note_prefix}{len(text)} chars{note_suffix}"
    omitted_chars = max(0, len(text) - len(snippet))
    note = f"{note_prefix}{omitted_chars} of {len(text)} chars{note_suffix}"
    return f"{snippet}\n\n{note}"


def _is_compressed_context_tool_result_summary_message(msg) -> bool:
    if not isinstance(msg, dict) or msg.get('role') != 'tool':
        return False
    return msg.get(_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG) is True


def _hard_prune_post_compression_tool_results(messages, *, compressor=None):
    if not messages:
        return messages, 0
    budget = _post_compression_tool_result_budget(compressor)
    raw_tool_tokens = 0
    replacements = []
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get('role') != 'tool':
            continue
        content = msg.get('content', '')
        text = _raw_message_text(content)
        if not text.strip():
            continue
        token_count = _rough_text_token_count(text)
        if _is_compressed_context_tool_result_summary_message(msg):
            raw_tool_tokens += token_count
            continue
        remaining = max(0, budget - raw_tool_tokens)
        if token_count <= remaining:
            raw_tool_tokens += token_count
            continue
        replacements.append((idx, text, token_count, remaining))
        # Once a tool row exceeds the residual budget, older tool rows should
        # not be allowed to spend that same residual budget again.
        raw_tool_tokens = budget

    if not replacements:
        return messages, 0

    pruned = copy.deepcopy(list(messages))
    for idx, text, token_count, keep_tokens in replacements:
        msg = pruned[idx]
        msg['content'] = _compressed_context_tool_result_summary(
            text,
            original_tokens=token_count,
            keep_tokens=keep_tokens,
        )
        msg[_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG] = True
    return pruned, len(replacements)


def _prune_context_tool_results_after_compression(agent, context_messages):
    """Run the active compressor's cheap tool-result pruning on model context.

    Auto-compression can happen mid-turn and then the agent may run more tools
    before producing the final answer. Those completed tail tool results are
    model-facing context, but they were produced after the compression pass and
    therefore did not go through the compressor's tool-output pruning. Apply the
    same cheap pruning once more after a confirmed compression event, then apply
    a WebUI hard cap to retained tool-result payloads. This keeps the visible
    transcript untouched while preventing the next turn from seeing raw
    post-compression tool dumps that the compressor protected as recent tail.
    """
    if not context_messages:
        return context_messages
    compressor = getattr(agent, 'context_compressor', None)
    prune = getattr(compressor, '_prune_old_tool_results', None)
    pruned_messages = context_messages
    if callable(prune):
        try:
            candidate_messages, pruned_count = prune(
                copy.deepcopy(context_messages),
                protect_tail_count=getattr(compressor, 'protect_last_n', 20),
                protect_tail_tokens=getattr(compressor, 'tail_token_budget', None),
            )
            if pruned_count:
                pruned_messages = _deduplicate_context_messages(candidate_messages)
        except Exception:
            logger.debug("post-compression context tool-result pruning failed", exc_info=True)

    hard_pruned_messages, hard_pruned_count = _hard_prune_post_compression_tool_results(
        pruned_messages,
        compressor=compressor,
    )
    if hard_pruned_count:
        return _deduplicate_context_messages(hard_pruned_messages)
    return pruned_messages


def _estimate_post_compression_context_tokens(
    agent,
    context_messages,
    system_message,
):
    """Return a display-only estimate for the prepared post-compression request."""
    try:
        from agent import model_metadata

        messages = context_messages or []
        tools = getattr(agent, 'tools', None) or None
        request_estimator = getattr(model_metadata, 'estimate_request_tokens_rough', None)
        if callable(request_estimator):
            estimate = request_estimator(messages, system_prompt=system_message or '', tools=tools)
        else:
            message_estimator = getattr(model_metadata, 'estimate_messages_tokens_rough', None)
            if callable(message_estimator):
                estimate = message_estimator(messages)
                if system_message:
                    estimate += message_estimator([{'role': 'system', 'content': system_message}])
                if tools:
                    estimate += message_estimator([{'role': 'system', 'content': str(tools)}])
            else:
                try:
                    from agent.context_compressor import _estimate_msg_budget_tokens
                except ImportError:
                    return None

                estimate = sum(
                    _estimate_msg_budget_tokens(message)
                    for message in messages
                    if isinstance(message, dict)
                )
                if system_message:
                    estimate += _estimate_msg_budget_tokens(
                        {'role': 'system', 'content': system_message}
                    )
                if tools:
                    estimate += _estimate_msg_budget_tokens(
                        {'role': 'system', 'content': str(tools)}
                    )
        return estimate if isinstance(estimate, int) and estimate > 0 else None
    except Exception:
        logger.debug("post-compression context estimate failed", exc_info=True)
        return None


def _restore_reasoning_metadata(previous_messages, updated_messages):
    """Carry forward display-only metadata lost during API-safe history sanitization.

    The provider-facing history strips WebUI-only fields like `reasoning`. When the
    agent returns its new full message history, prior assistant messages come back
    without that metadata unless we merge it back in by API-history position.

    This also preserves existing timestamps for unchanged historical messages.
    Without that, older turns that come back from the agent without `_ts` /
    `timestamp` can be re-stamped with the current time on every new assistant
    response, making prior messages appear to "move" in time.
    """
    if not previous_messages or not updated_messages:
        return updated_messages
    updated_messages = list(updated_messages)
    prev_safe = _api_safe_message_positions(previous_messages)

    def _safe_projection(msg):
        if not isinstance(msg, dict):
            return None
        projected = {
            k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS and msg.get('role')
        }
        # Mirror the empty-tool_calls drop applied by _api_safe_message_positions
        # (#5737) so this projection matches the API-safe positions it's aligned
        # against — otherwise a row stored with tool_calls: [] projects
        # differently here than in prev_safe and loses its metadata carry-forward.
        if 'tool_calls' in projected and not projected['tool_calls']:
            del projected['tool_calls']
        if 'content' in projected:
            projected['content'] = _strip_oob_blocks(projected['content'])
        return projected

    safe_pos = 0
    while safe_pos < len(prev_safe):
        prev_idx, _ = prev_safe[safe_pos]
        prev_msg = previous_messages[prev_idx]
        cur_msg = updated_messages[safe_pos] if safe_pos < len(updated_messages) else None

        if (
            isinstance(prev_msg, dict)
            and isinstance(cur_msg, dict)
            and _safe_projection(prev_msg) == _safe_projection(cur_msg)
        ):
            if (
                prev_msg.get('role') == 'assistant'
                and prev_msg.get('reasoning')
                and not cur_msg.get('reasoning')
            ):
                cur_msg['reasoning'] = prev_msg['reasoning']
            # Carry the stable per-message id (#context-message-stable-id) forward
            # the same way timestamp is carried. The agent rebuilds result rows
            # without our id every turn; without this, historical context rows
            # would be re-minted a fresh id each turn and drift out of alignment
            # with their display counterpart.
            if prev_msg.get('id') is not None and cur_msg.get('id') is None:
                cur_msg['id'] = prev_msg['id']
            if (
                prev_msg.get(_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG) is True
                and cur_msg.get(_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG) is not True
            ):
                cur_msg[_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG] = True
            if prev_msg.get('timestamp') and not cur_msg.get('timestamp'):
                cur_msg['timestamp'] = prev_msg['timestamp']
            elif prev_msg.get('_ts') and not cur_msg.get('_ts') and not cur_msg.get('timestamp'):
                cur_msg['_ts'] = prev_msg['_ts']
            safe_pos += 1
            continue

        safe_pos += 1

    return updated_messages


def _restore_display_reasoning_metadata(previous_messages, updated_messages):
    """Restore display-only thinking rows for visible transcript persistence."""
    updated_messages = _restore_reasoning_metadata(previous_messages, updated_messages)
    if not previous_messages or not updated_messages:
        return updated_messages
    prev_safe = _api_safe_message_positions(previous_messages)
    safe_indices = {idx for idx, _ in prev_safe}
    inserted_reasoning_only = 0
    for prev_idx, prev_msg in enumerate(previous_messages):
        if is_empty_partial_activity_message(prev_msg):
            continue
        if prev_idx in safe_indices or not _is_reasoning_only_assistant_message(prev_msg):
            continue
        safe_pos = sum(1 for idx, _ in prev_safe if idx < prev_idx) + inserted_reasoning_only
        existing = updated_messages[safe_pos] if safe_pos < len(updated_messages) else None
        if isinstance(existing, dict) and _is_reasoning_only_assistant_message(existing):
            continue
        updated_messages.insert(safe_pos, copy.deepcopy(prev_msg))
        inserted_reasoning_only += 1
    return updated_messages
