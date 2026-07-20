"""Live tool metering, persisted tool summaries, and partial identities."""

from __future__ import annotations

import json

from api.sessions import get_session


_TOOL_RESULT_SNIPPET_MAX = 4000
_TOOL_ARG_CONTENT_KEYS = frozenset({
    "command", "cmd", "script", "code", "patch", "diff",
    "old_string", "new_string", "content", "path", "file_path",
})
_TOOL_ARG_CONTENT_CAP = _TOOL_RESULT_SNIPPET_MAX
_LIVE_TOOL_PROMPT_DELTA_MAX = 12_000
_LIVE_TOOL_PROMPT_TURN_MAX = 24_000


def _bounded_live_tool_prompt_delta(messages, *, cap: int = _LIVE_TOOL_PROMPT_DELTA_MAX) -> int:
    """Return a bounded rough token delta for live tool metering.

    Tool-result callbacks can fire before the agent's next exact prompt accounting
    is available. The live usage ring should show a conservative in-flight hint,
    not replay a full large tool payload into `last_prompt_tokens`.
    """
    if not messages:
        return 0
    try:
        from agent.model_metadata import estimate_messages_tokens_rough
        delta = int(estimate_messages_tokens_rough(messages) or 0)
    except Exception:
        delta = 0
    if delta <= 0:
        return 0
    return min(delta, int(cap or 0))

def live_usage_prompt_estimate_after_tool_delta(
    *,
    base_prompt_tokens: int,
    exact_prompt_tokens: int = 0,
    messages=None,
    cap: int = _LIVE_TOOL_PROMPT_DELTA_MAX,
    turn_tool_prompt_tokens: int = 0,
    turn_cap: int = _LIVE_TOOL_PROMPT_TURN_MAX,
) -> dict:
    """Compute the live `last_prompt_tokens` estimate after a tool update.

    Exact compressor/provider prompt accounting wins. When no newer exact prompt
    is available, add only bounded live tool deltas to the persisted base.
    """
    base = int(base_prompt_tokens or 0)
    exact = int(exact_prompt_tokens or 0)
    if exact and exact != base:
        return {
            'last_prompt_tokens': exact,
            'estimated': False,
            'turn_tool_prompt_tokens': 0,
        }
    prior_turn_delta = max(0, int(turn_tool_prompt_tokens or 0))
    turn_ceiling = max(0, int(turn_cap or 0))
    next_turn_delta = min(
        prior_turn_delta + _bounded_live_tool_prompt_delta(messages, cap=cap),
        turn_ceiling,
    )
    return {
        'last_prompt_tokens': base + next_turn_delta,
        'estimated': True,
        'turn_tool_prompt_tokens': next_turn_delta,
    }

def _live_usage_session_snapshot(session_id, current_session, cache_ref, *, loader=get_session):
    """Return a session object for hot live-metering paths without repeated loads."""
    if current_session is not None:
        try:
            cache_ref[0] = current_session
        except Exception:
            pass
        return current_session
    try:
        cached = cache_ref[0]
    except Exception:
        cached = None
    if cached is not None:
        return cached
    try:
        loaded = loader(session_id)
    except Exception:
        return None
    try:
        cache_ref[0] = loaded
    except Exception:
        pass
    return loaded

def _tool_result_snippet(raw, limit: int = _TOOL_RESULT_SNIPPET_MAX) -> str:
    """Extract a bounded result preview from a stored tool message payload."""
    if limit <= 0:
        return ''
    text = str(raw or '')
    try:
        data = raw if isinstance(raw, dict) else json.loads(text)
        if isinstance(data, dict):
            preview = data.get('output') or data.get('result') or data.get('error') or text
            text = str(preview)
    except Exception:
        pass
    return text[:limit]

def _truncate_tool_args(args, limit: int = 6) -> dict:
    """Truncate tool args for compact session persistence.

    Incidental args keep a short 120-char cap, but content/diff-bearing keys
    (the command, code, and patch fields that tool cards and recovery-rebuilt
    diffs are reconstructed from) get a much larger cap so a long command, file
    path, or reconstructed diff is not silently corrupted (#4928). A hard cap is
    still applied for storage safety, aligned with the result snippet cap.
    """
    out = {}
    if not isinstance(args, dict):
        return out
    for k, v in list(args.items())[:limit]:
        s = str(v)
        cap = _TOOL_ARG_CONTENT_CAP if str(k).lower() in _TOOL_ARG_CONTENT_KEYS else 120
        out[k] = s[:cap] + ('...' if len(s) > cap else '')
    return out

def _nearest_assistant_msg_idx(messages, msg_idx: int) -> int:
    """Find the closest preceding assistant message index for a tool result."""
    for idx in range(msg_idx - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get('role') == 'assistant':
            return idx
    return -1

def _extract_tool_calls_from_messages(messages, live_tool_calls=None):
    """Build persisted tool-call summaries from final messages plus live progress fallback."""
    tool_calls = []
    pending_names = {}
    pending_args = {}
    pending_asst_idx = {}
    tool_msg_sequence = []

    for msg_idx, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'assistant':
            content = m.get('content', '')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'tool_use':
                        tid = part.get('id', '')
                        if tid:
                            pending_names[tid] = part.get('name', '')
                            pending_args[tid] = part.get('input', {})
                            pending_asst_idx[tid] = msg_idx
            for tc in m.get('tool_calls', []):
                if not isinstance(tc, dict):
                    continue
                tid = tc.get('id', '') or tc.get('call_id', '')
                fn = tc.get('function', {})
                name = fn.get('name', '')
                try:
                    args = json.loads(fn.get('arguments', '{}') or '{}')
                except Exception:
                    args = {}
                if tid and name:
                    pending_names[tid] = name
                    pending_args[tid] = args
                    pending_asst_idx[tid] = msg_idx
        elif role == 'tool':
            tid = m.get('tool_call_id') or m.get('tool_use_id', '')
            raw = m.get('content', '')
            seq = {'msg_idx': msg_idx, 'raw': raw, 'resolved': False}
            if tid:
                name = pending_names.get(tid, '')
                if name and name != 'tool':
                    tool_calls.append({
                        'name': name,
                        'snippet': _tool_result_snippet(raw),
                        'tid': tid,
                        'assistant_msg_idx': pending_asst_idx.get(tid, -1),
                        'args': _truncate_tool_args(pending_args.get(tid, {})),
                    })
                    seq['resolved'] = True
            tool_msg_sequence.append(seq)

    live = [tc for tc in (live_tool_calls or []) if isinstance(tc, dict) and tc.get('name') and tc.get('name') != 'clarify']
    if live:
        for seq_idx, seq in enumerate(tool_msg_sequence):
            if seq.get('resolved'):
                continue
            if seq_idx >= len(live):
                break
            live_tc = live[seq_idx]
            tool_calls.append({
                'name': live_tc.get('name', 'tool'),
                'snippet': _tool_result_snippet(seq.get('raw', '')),
                'tid': live_tc.get('tid', '') or '',
                'assistant_msg_idx': _nearest_assistant_msg_idx(messages, seq.get('msg_idx', -1)),
                'args': _truncate_tool_args(live_tc.get('args', {}), limit=4),
            })

    return tool_calls

def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for a persisted partial assistant marker."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )

def _partial_marker_already_present(messages, candidate: dict, *, before_idx: int | None = None) -> bool:
    """Check for an equivalent partial marker in the current user turn only."""
    if not isinstance(messages, list) or not isinstance(candidate, dict):
        return False
    end = before_idx if isinstance(before_idx, int) else len(messages)
    end = max(0, min(end, len(messages)))
    start = 0
    for idx in range(end - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get('role') == 'user':
            start = idx + 1
            break
    candidate_sig = _partial_message_signature(candidate)
    for msg in messages[start:end]:
        if isinstance(msg, dict) and msg.get('_partial') and _partial_message_signature(msg) == candidate_sig:
            return True
    return False
