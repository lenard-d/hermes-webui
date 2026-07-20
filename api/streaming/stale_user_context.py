"""Detection and repair of stale user-context contamination."""

from __future__ import annotations

import copy

from .thinking_content import _message_content_part_text
from api.workspace_context import (
    _LEGACY_WORKSPACE_PREFIX_ANY_RE,
    _WORKSPACE_PREFIX_ANY_RE,
    _strip_workspace_prefix,
)


def _strip_workspace_prefixes_for_compare(text: str) -> str:
    """Remove WebUI workspace sentinels anywhere before text comparison."""
    value = _strip_workspace_prefix(text, include_legacy=True)
    for pattern in (_WORKSPACE_PREFIX_ANY_RE, _LEGACY_WORKSPACE_PREFIX_ANY_RE):
        value = pattern.sub('', value)
    return value.strip()


def _normalize_user_text(text):
    """Collapse whitespace and strip workspace sentinels for tail comparisons."""
    if not isinstance(text, str):
        return ""
    return " ".join(_strip_workspace_prefixes_for_compare(text).split())


def _raw_message_text(value) -> str:
    """Extract text from a message content payload without stripping markup.

    Used for the stale-user-merge detector so the literal boundary between
    the prior tail and the current turn survives into the comparison. The
    thinking-markup strip in ``_message_text`` collapses newlines, which
    would defeat the boundary check.
    """
    if isinstance(value, list):
        return ' '.join(
            _message_content_part_text(p)
            for p in value
            if isinstance(p, dict)
        )
    return str(value or '')


def _stale_user_tail_candidate(msg):
    """Return normalized text if msg is a user row that could be a stale tail."""
    if not isinstance(msg, dict) or msg.get('role') != 'user':
        return None
    raw = _raw_message_text(msg.get('content', ''))
    if not raw.strip():
        return None
    return _normalize_user_text(raw)


def _last_user_row(messages):
    """Return the last user-role row in `messages`, or None."""
    for msg in reversed(list(messages or [])):
        if isinstance(msg, dict) and msg.get('role') == 'user':
            return msg
    return None


def _stale_prefix_matches_prior_user_context(
    stale_prefix,
    stale_segments,
    previous_context,
):
    """Return True when a stale prefix is explainable by prior user context.

    First-generation repair usually produces segments matching consecutive
    prior user rows. Once a session is already contaminated, later repair can
    replay a stable stale prefix from an older polluted row even after newer
    clean user turns have moved the context tail forward. Handle both shapes
    while still requiring all evidence to come from prior user-role rows.
    """
    prior_rows = [
        _stale_user_tail_candidate(msg)
        for msg in previous_context or []
    ]
    prior_rows = [row for row in prior_rows if row]
    if not prior_rows:
        return False

    if stale_segments:
        segment_count = len(stale_segments)
        for start in range(0, len(prior_rows) - segment_count + 1):
            if prior_rows[start:start + segment_count] == stale_segments:
                return True

        # Already-polluted sessions may replay paragraphs from older polluted
        # rows after newer clean turns have advanced the context tail. In that
        # shape the stale paragraphs are still all prior user content, but they
        # are substrings within older merged rows rather than standalone rows.
        #
        # NOTE: this substring match is intentionally loose (a stale segment can
        # coincidentally appear inside an unrelated prior row). Correctness does
        # NOT depend on it being precise — the caller (_detect_stale_user_merge)
        # only reaches here once the row's suffix already normalizes to the
        # ENTIRE submitted turn, so anything this branch flags has a prefix that
        # is extra-to-the-submission. Cleaning therefore only ever rewrites the
        # row to the user's actual current turn; it can never drop legitimate
        # current-turn content even on a coincidental substring hit.
        row_index = 0
        row_offset = 0
        matched_all_segments = True
        for segment in stale_segments:
            matched_segment = False
            while row_index < len(prior_rows):
                row = prior_rows[row_index]
                pos = row.find(segment, row_offset)
                if pos >= 0:
                    row_offset = pos + len(segment)
                    matched_segment = True
                    break
                row_index += 1
                row_offset = 0
            if not matched_segment:
                matched_all_segments = False
                break
        if matched_all_segments:
            return True

    prefix_norm = _normalize_user_text(stale_prefix)
    if not prefix_norm:
        return False
    for row in prior_rows:
        if row == prefix_norm or row.startswith(f'{prefix_norm} '):
            return True
    return False


def _detect_stale_user_merge(
    message,
    msg_text,
    previous_user_tail,
    previous_context=None,
):
    """Return True if `message` is the current user turn with a stale prefix merged in.

    The agent's defensive repair path can concatenate prior user context with
    the submitted current turn as ``<stale>\\n\\n<current>``. The stale portion
    can be either the immediate prior user tail or a replayed prefix from an
    older already-polluted user row. The literal ``\\n\\n`` boundary must survive
    into the comparison; a single-newline or space-only join is not the repair
    shape and must not match. Workspace sentinels may be present on either or
    both halves and are stripped before comparison.
    """
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    current_norm = _normalize_user_text(msg_text)
    if not current_norm:
        return False

    merged = _raw_message_text(message.get('content', '')).replace("\r\n", "\n")
    if "\n\n" not in merged:
        return False

    # The user's current turn can itself contain paragraph breaks. Find a repair
    # boundary whose suffix normalizes to the *entire* submitted turn, then treat
    # only the prefix as stale context. A plain split-and-last-segment check would
    # miss ``<stale>\n\n<current paragraph A>\n\n<current paragraph B>``.
    stale_segments = []
    stale_prefix = ''
    search_end = len(merged)
    while search_end > 0:
        boundary_idx = merged.rfind("\n\n", 0, search_end)
        if boundary_idx < 0:
            break
        suffix = merged[boundary_idx + 2:]
        if _normalize_user_text(suffix) == current_norm:
            prefix = merged[:boundary_idx]
            candidate_segments = [
                _normalize_user_text(segment)
                for segment in prefix.split("\n\n")
            ]
            if candidate_segments and all(candidate_segments):
                stale_segments = candidate_segments
                stale_prefix = prefix
                break
            # The suffix matched the submitted turn, but this boundary leaves
            # blank prefix segments; try the next candidate boundary to the left.
        search_end = boundary_idx
    if not stale_segments:
        return False

    if previous_context is not None and _stale_prefix_matches_prior_user_context(
        stale_prefix,
        stale_segments,
        previous_context,
    ):
        return True

    return bool(
        previous_context is None
        and len(stale_segments) == 1
        and _normalize_user_text(previous_user_tail) == stale_segments[0]
    )


def _strip_stale_user_merge_from_messages(
    messages,
    msg_text,
    previous_user_tail,
    previous_context=None,
):
    """Return messages with stale-prefixed current user turns replaced by clean ones.

    Both context-merge (model-facing) and display-merge (visible transcript)
    callers funnel through this so a single detection rule governs persistence.
    The current user row is replaced with a clean copy using `msg_text` so the
    displayed bubble matches what the human submitted, never the polluted pair.
    """
    if not messages or not msg_text:
        return messages
    out = []
    for msg in messages:
        if _detect_stale_user_merge(
            msg,
            msg_text,
            previous_user_tail,
            previous_context=previous_context,
        ):
            cleaned = (
                copy.deepcopy(msg)
                if isinstance(msg, dict)
                else {'role': 'user', 'content': msg_text}
            )
            cleaned['content'] = msg_text
            out.append(cleaned)
        else:
            out.append(msg)
    return out
