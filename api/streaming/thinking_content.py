"""Thinking extraction and visible assistant-content normalization."""

from __future__ import annotations

import re


_INLINE_THINKING_TAG_PAIRS = (
    ("<think>", "</think>"),
    ("<|channel>thought\n", "<channel|>"),
    ("<|turn|>thinking\n", "<turn|>"),
)


def _inline_thinking_fence_marker_at(text, index):
    # A fenced code block opener may be indented up to 3 spaces in Markdown
    # (4+ spaces is an indented code block, handled separately). The marker is
    # only a fence when it sits at the start of a line (after optional 1-3
    # spaces of indentation).
    if index > 0 and text[index - 1] != '\n':
        # Allow up to 3 leading spaces: walk back over spaces to a line start.
        back = index - 1
        spaces = 0
        while back >= 0 and text[back] == ' ' and spaces < 3:
            back -= 1
            spaces += 1
        if not (back < 0 or text[back] == '\n'):
            return ''
    if text.startswith('```', index):
        return '```'
    if text.startswith('~~~', index):
        return '~~~'
    return ''


def _next_inline_thinking_opener(text, start):
    """Index of the earliest complete thinking opener at/after `start`, or -1.
    Cheap str.find per opener — lets the scanner bulk-skip plain trailing content
    instead of walking it char-by-char (#3633 Codex per-token perf catch)."""
    best = -1
    for open_tag, _close in _INLINE_THINKING_TAG_PAIRS:
        i = text.find(open_tag, start)
        if i != -1 and (best == -1 or i < best):
            best = i
    return best


def _text_tail_is_partial_opener(text):
    """True when the END of `text` is a non-empty proper prefix of some thinking
    opener (e.g. ``<thi`` for ``<think>``). Used to decide whether a streaming
    tail might be a forming block worth code-aware handling."""
    for open_tag, _close in _INLINE_THINKING_TAG_PAIRS:
        m = min(len(open_tag) - 1, len(text))
        for n in range(m, 0, -1):
            if open_tag.startswith(text[-n:]):
                return True
    return False


def _line_is_indented_code(text, line_start):
    """True when the line beginning at `line_start` is a markdown indented code
    block line (>=4 leading spaces or a leading tab, and not blank). `line_start`
    must be the index of the first character of the line. O(1)-ish: only inspects
    the line's leading characters, not the whole document (the per-character
    variant was O(n^2) on long no-newline content — #3633 Codex perf catch)."""
    if line_start >= len(text):
        return False
    if text[line_start] == '\t':
        # A leading tab is indented code only if the line isn't otherwise blank.
        nl = text.find('\n', line_start)
        seg = text[line_start:(nl if nl != -1 else len(text))]
        return bool(seg.strip())
    if text.startswith('    ', line_start):
        nl = text.find('\n', line_start)
        seg = text[line_start:(nl if nl != -1 else len(text))]
        return bool(seg.strip())
    return False


def _merge_inline_thinking_reasoning(existing_reasoning, extracted_parts):
    out = str(existing_reasoning or '').strip()
    for part in extracted_parts or ():
        item = str(part or '').strip()
        if not item:
            continue
        if not out:
            out = item
            continue
        if out == item or any(existing.strip() == item for existing in out.split('\n\n')):
            continue
        out = out + '\n\n' + item
    return out


def _extract_inline_thinking_from_content(raw_content, existing_reasoning='', *, streaming=False):
    """Split inline thinking blocks out of assistant content.

    Code-aware: thinking tags inside a triple-fence (``` / ~~~), an inline
    single-backtick code span, or an indented (>=4-space / tab) code block are
    LEFT VISIBLE — they are literal text a user typed/pasted, not a real thinking
    trace. (#3633 deep-review / Codex catch: the earlier full-scan version only
    protected triple fences, so a literal `<think>` in an inline code span got
    silently extracted.)

    ``streaming`` gates partial/unclosed-block handling: during live streaming an
    unmatched open tag means "still thinking" and its tail is shown as reasoning;
    on the persist/reload path (streaming=False) an unclosed tag is LEFT VISIBLE
    so prose after a literal ``<think>`` is never silently truncated on save.
    """
    text = '' if raw_content is None else str(raw_content)
    if not text:
        return text, str(existing_reasoning or '').strip()
    # Fast path (#3633 Codex perf catch — _parseStreamState / syncInflight call
    # this on the FULL accumulator on every streamed token, so the common no-tag
    # case must not do the O(length) char walk per call). If the text contains no
    # complete thinking opener AND — when streaming — its tail is not a prefix of
    # any opener (a partial opener mid-stream), there is nothing to extract:
    # return the text unchanged. Two cheap substring scans instead of a full walk.
    if not any(open_tag in text for open_tag, _close in _INLINE_THINKING_TAG_PAIRS):
        tail_is_partial_opener = False
        if streaming:
            for open_tag, _close in _INLINE_THINKING_TAG_PAIRS:
                # Does the END of text look like the START of an opener?
                max_prefix = min(len(open_tag) - 1, len(text))
                for n in range(max_prefix, 0, -1):
                    if open_tag.startswith(text[-n:]):
                        tail_is_partial_opener = True
                        break
                if tail_is_partial_opener:
                    break
        if not tail_is_partial_opener:
            return text, str(existing_reasoning or '').strip()
    visible = []
    extracted = []
    cursor = 0
    index = 0
    fence = ''
    in_backtick = False
    length = len(text)
    # Incremental, O(1)-per-iteration line state (the previous per-character line
    # scan made the whole pass O(n^2) on long no-newline content — #3633 Codex
    # perf catch). `line_is_indented_code` is recomputed only at a line start.
    line_is_indented_code = _line_is_indented_code(text, 0)
    # Whether any non-whitespace char appeared in text[:index] — the cheap
    # equivalent of the old `text[:index].strip() != ''` leading check.
    seen_nonspace = False
    # Whether a LEADING thinking block/prefix was removed — only then do we
    # lstrip the final content (so a reply that legitimately starts with
    # indented code / whitespace and has NO leading thinking wrapper keeps its
    # leading whitespace — #3633 Codex catch).
    leading_removed = False
    # Index of the next opener at/after `index` (recomputed only when we pass it).
    # When no opener remains ahead, the rest of the text is plain and can be
    # appended in one slice — this keeps a stream that DID contain a leading
    # thinking block from re-walking the whole growing answer tail every token
    # (#3633 Codex perf catch: the per-token full walk was O(n^2) over a stream).
    next_opener = _next_inline_thinking_opener(text, 0)
    while index < length:
        if next_opener == -1 or index > next_opener:
            next_opener = _next_inline_thinking_opener(text, index)
        if next_opener == -1:
            # No further COMPLETE opener ahead. The remaining tail is plain
            # visible content and can be appended in one slice — EXCEPT during
            # streaming when the tail is a prefix of an opener (e.g. "...<thi"):
            # that may be a forming block and must be suppressed, but ONLY if it
            # is outside code context (a partial opener inside inline-backtick /
            # fenced / indented code stays visible — master parity). Determining
            # code state needs the char walk, so in that case fall through to the
            # normal loop (bounded — a partial tail is a transient single token)
            # rather than bulk-skipping. Otherwise stop (avoids re-walking the
            # growing answer tail every token — #3633 perf catch).
            if streaming and _text_tail_is_partial_opener(text):
                pass  # fall through to the code-aware char walk for the tail
            else:
                break
        ch = text[index]
        if index > 0 and text[index - 1] == '\n':
            line_is_indented_code = _line_is_indented_code(text, index)
        marker = _inline_thinking_fence_marker_at(text, index)
        if marker:
            fence = '' if fence == marker else (fence or marker)
        # Inline single-backtick code span toggles on each lone backtick that is
        # not part of a triple fence. Only tracked outside a triple fence.
        if not fence and not marker and ch == '`':
            in_backtick = not in_backtick
        in_code = bool(fence) or in_backtick or line_is_indented_code
        if not in_code:
            pair = None
            for open_tag, close_tag in _INLINE_THINKING_TAG_PAIRS:
                if text.startswith(open_tag, index):
                    pair = (open_tag, close_tag)
                    break
            if pair:
                open_tag, close_tag = pair
                close_index = text.find(close_tag, index + len(open_tag))
                if close_index == -1:
                    # Unclosed open tag. A LEADING unclosed block (nothing
                    # visible before it) is a genuine thinking trace that got
                    # cut off / persisted mid-thought → reasoning (master #3455
                    # leading-only intent, and the live-stream "still thinking"
                    # case). An unclosed tag AFTER visible content on the persist
                    # path is almost always a literal typed tag — leave it (and
                    # the prose after it) visible so nothing is silently
                    # truncated (#3633 Codex catch). During live streaming any
                    # unmatched open tag is treated as in-progress thinking.
                    leading = not seen_nonspace
                    if not streaming and not leading:
                        break
                    if leading:
                        leading_removed = True
                    visible.append(text[cursor:index])
                    partial = text[index + len(open_tag):]
                    if partial:
                        extracted.append(partial)
                    cursor = length
                    index = length
                    break
                visible.append(text[cursor:index])
                extracted.append(text[index + len(open_tag):close_index])
                if not seen_nonspace:
                    leading_removed = True
                seen_nonspace = True  # the extracted tag span is non-whitespace
                index = close_index + len(close_tag)
                cursor = index
                continue
            if streaming:
                matched_partial = False
                for open_tag, _close_tag in _INLINE_THINKING_TAG_PAIRS:
                    rest = text[index:]
                    if len(rest) < len(open_tag) and open_tag.startswith(rest):
                        if not seen_nonspace:
                            leading_removed = True
                        visible.append(text[cursor:index])
                        cursor = length
                        index = length
                        matched_partial = True
                        break
                if matched_partial or index >= length:
                    break
        if not ch.isspace():
            seen_nonspace = True
        index += 1
    if cursor < length:
        visible.append(text[cursor:])
    content = ''.join(visible)
    if leading_removed:
        content = content.lstrip()
    reasoning = _merge_inline_thinking_reasoning(existing_reasoning, extracted)
    return content, reasoning


def _split_thinking_from_content(raw_content, existing_reasoning=''):
    """Split inline thinking blocks out of assistant content for persistence.

    Persistence path: streaming=False, so an unclosed tag stays visible content
    (a partial block only means "still thinking" during a live stream).
    """
    return _extract_inline_thinking_from_content(
        raw_content,
        existing_reasoning=existing_reasoning,
        streaming=False,
    )


def _strip_thinking_markup(text: str) -> str:
    """Remove common reasoning/thinking wrappers from model text."""
    if not text:
        return ''
    s = str(text)
    # Treat provider thinking wrappers as metadata only when they lead the
    # response. Literal discussion of these tags later in normal prose should
    # stay visible (#2152).
    s = re.sub(r'^\s*<think>.*?</think>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'^\s*<\|channel\|?>thought\n?.*?<channel\|>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'^\s*<\|turn\|>thinking\n.*?<turn\|>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)  # Gemma 4
    s = re.sub(r'^\s*(the|ther)\s+user\s+is\s+asking[^\n]*(?:\n|$)', ' ', s, flags=re.IGNORECASE)
    # Strip plain-text thinking preambles from models that don't use <think> tags (e.g. Qwen3).
    # These appear as the very first sentence of the assistant response and are not useful as titles.
    s = re.sub(
        r"^\s*(?:here(?:'s| is) (?:a |my )?(?:thinking|thought) (?:process|trace|through)\b[^\n]*\n?"
        r"|let me (?:think|work|reason|analyze|walk) (?:through|about|this|step)\b[^\n]*\n?"
        r"|i(?:'ll| will) (?:think|work|reason|analyze|break this down)\b[^\n]*\n?"
        r"|(?:okay|alright|sure|of course),?\s+let me\b[^\n]*\n?)",
        ' ', s, flags=re.IGNORECASE
    )
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _strip_xml_tool_calls(text: str) -> str:
    """Strip XML-style function_calls blocks that DeepSeek and similar models
    emit in their raw response text.  These blocks are processed separately as
    tool calls; leaving them in the assistant content causes them to render
    visibly in the chat bubble.

    Handles both complete blocks (<function_calls>…</function_calls>) and
    partial/orphaned opening tags that may appear at the tail of a stream.
    Also handles variants like <｜DSML｜function_calls> from DeepSeek on Bedrock.
    """
    if not text:
        return text
    s = str(text)
    # Check if contains any function_calls/DSML marker (case-insensitive)
    _lo = s.lower()
    if 'function_calls' not in _lo and 'dsml' not in _lo:
        return text

    _dsml_prefix = r'(?:\s*｜\s*DSML\s*[｜|]\s*)?'
    open_tag = rf'<{_dsml_prefix}function_calls'
    close_tag = rf'</{_dsml_prefix}function_calls>'
    # Strip complete blocks for both <function_calls> and <｜DSML｜function_calls>.
    s = re.sub(
        rf'{open_tag}>.*?{close_tag}',
        '',
        s,
        flags=re.IGNORECASE | re.DOTALL
    )
    # Strip orphaned/truncated opening tags, including missing ">" at stream tail.
    s = re.sub(
        rf'{open_tag}(?:>|$).*$',
        '',
        s,
        flags=re.IGNORECASE | re.DOTALL
    )
    # Remove malformed DSML fragments like "<｜DSML |" that can leak in tokens.
    s = re.sub(r'<\s*｜\s*DSML\s*[｜|]\s*', '', s, flags=re.IGNORECASE)
    return s.strip()


def _sanitize_generated_title(text: str) -> str:
    """Sanitize LLM-generated title text before persisting to session."""
    s = _strip_thinking_markup(text or '')
    s = re.sub(
        r'^\s*(?:[*_`~]+\s*)?(?:session\s+title|title)\s*:\s*(?:[*_`~]+\s*)?',
        '',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r'^\s*title\s*:\s*', '', s, flags=re.IGNORECASE)
    s = s.strip(" \t\r\n\"'`*_~")
    s = re.sub(r'\s+', ' ', s).strip()
    # Guard against chain-of-thought leakage and meta-reasoning patterns.
    if _looks_invalid_generated_title(s):
        return ''
    return s[:80]


def _looks_invalid_generated_title(text: str) -> bool:
    s = str(text or '')
    if not s.strip():
        return True
    return bool(
        re.search(r'<think>|<\|channel\|>thought|<\|turn\|>thinking', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(the|ther)\s+user\s+', s, flags=re.IGNORECASE)
        or re.search(r'^\s*user\s+\w+\s+', s, flags=re.IGNORECASE)
        or re.search(r'\b(they|user)\s+want(s)?\s+me\s+to\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(i|we)\s+(should|need to|will|can)\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*let me\b', s, flags=re.IGNORECASE)
        or re.search(r"^\s*here(?:'s| is) (?:a |my )?(?:thinking|thought)", s, flags=re.IGNORECASE)
        or re.search(r'^\s*(ok|okay|done|all set|complete|completed|finished)\b[\s.!?]*$', s, flags=re.IGNORECASE)
    )


def _structured_visible_text(value, *, depth: int = 0) -> str:
    """Extract provider text without stringifying metadata-only objects."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or depth >= 4:
        return ''
    for key in ('value', 'text', 'content', 'input_text', 'output_text'):
        text = _structured_visible_text(value.get(key), depth=depth + 1)
        if text:
            return text
    return ''


def _message_content_part_text(part) -> str:
    """Extract visible text from a structured content part."""
    if not isinstance(part, dict):
        return ''
    for key in ('text', 'content', 'input_text', 'output_text'):
        text = _structured_visible_text(part.get(key))
        if text:
            return text
    return ''


def _message_text(value) -> str:
    """Extract plain text from mixed message content payloads."""
    if isinstance(value, list):
        parts = []
        for p in value:
            if not isinstance(p, dict):
                continue
            ptype = str(p.get('type') or '').lower()
            if ptype in ('', 'text', 'input_text', 'output_text'):
                parts.append(_message_content_part_text(p))
        return _strip_thinking_markup('\n'.join(parts).strip())
    return _strip_thinking_markup(str(value or '').strip())


def _assistant_content_part_is_tool_use(part) -> bool:
    """Return True when a content[] part represents a tool invocation boundary."""
    if not isinstance(part, dict):
        return False
    part_type = str(part.get('type') or '').lower()
    if part_type in {'tool_use', 'tool_call'}:
        return True
    if part_type:
        return False
    if _message_content_part_text(part).strip():
        return False
    return any(key in part for key in ('tool_use_id', 'tool_call_id', 'call_id')) and any(
        key in part for key in ('name', 'tool_name', 'input', 'args')
    )


def _assistant_message_has_final_visible_text(message) -> bool:
    """Return True when an assistant row carries a settled visible answer."""
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return False
    content = message.get('content', '')
    if isinstance(content, list):
        last_tool_idx = -1
        for idx, part in enumerate(content):
            if _assistant_content_part_is_tool_use(part):
                last_tool_idx = idx
        if last_tool_idx >= 0:
            tail_parts = content[last_tool_idx + 1:]
            return bool(_message_text(tail_parts).strip())
        if message.get('tool_calls'):
            return False
        return bool(_message_text(content).strip())
    if message.get('tool_calls'):
        return False
    return bool(_message_text(content).strip())
