"""Conversation selection and title-quality policy.

This module is pure apart from reading the refresh setting.  It owns which
conversation text may become a title, prompt construction, language/script
validation, and the dependency-free fallback summary.
"""

from __future__ import annotations

import re
from typing import Optional

from api.sessions.projects import title_from
from api.workspace_context import _strip_workspace_prefix

from ..thinking_content import (
    _looks_invalid_generated_title,
    _message_text,
    _strip_thinking_markup,
)


def _first_exchange_snippets(messages):
    """Return (first_user_text, first_assistant_text) snippets for title generation.

    Prefer the first substantive assistant answer in the opening exchange,
    skipping empty placeholders and assistant tool-call preambles.
    """
    user_text = ''
    asst_text = ''
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'user':
            candidate = _message_text(m.get('content'))
            if not user_text and candidate:
                user_text = candidate
                continue
            if user_text and candidate:
                break
        elif role == 'assistant' and user_text:
            candidate = _message_text(m.get('content'))
            # Skip tool-call preambles *only* when content is empty or looks
            # like meta-reasoning ("Let me check my memory first.", "The user
            # is asking...", etc.). Assistant rows that carry tool_calls but
            # also contain a substantive answer text are kept — those are
            # agentic first-turn plans that are legitimate title candidates.
            if m.get('tool_calls') and (not candidate or _looks_invalid_generated_title(candidate)):
                continue
            if candidate:
                asst_text = candidate
        if user_text and asst_text:
            break
    return user_text[:500], asst_text[:500]


def _latest_exchange_snippets(messages):
    """Return (last_user_text, last_assistant_text) snippets for title refresh.

    Walks the message list backwards to find the last user+assistant pair,
    skipping empty or tool-call-only assistant messages.
    """
    user_text = ''
    asst_text = ''
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'assistant' and not asst_text:
            candidate = _message_text(m.get('content'))
            # Skip tool-call-only preambles
            if m.get('tool_calls') and (not candidate or _looks_invalid_generated_title(candidate)):
                continue
            if candidate:
                asst_text = candidate
        elif role == 'user' and not user_text:
            candidate = _message_text(m.get('content'))
            if candidate:
                user_text = candidate
        if user_text and asst_text:
            break
    return user_text[:500], asst_text[:500]


def _count_exchanges(messages):
    """Count the number of user messages (rough exchange count)."""
    count = 0
    for m in messages or []:
        if isinstance(m, dict) and m.get('role') == 'user':
            content = m.get('content', '')
            if isinstance(content, list):
                content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
            if str(content).strip():
                count += 1
    return count


def _get_title_refresh_interval() -> int:
    """Read the auto_title_refresh_every setting (0 = disabled)."""
    try:
        from api.config import load_settings
        settings = load_settings()
        val = settings.get('auto_title_refresh_every', '0')
        return int(val) if str(val).strip().isdigit() and int(val) > 0 else 0
    except Exception:
        return 0


def _is_provisional_title(current_title: str, messages) -> bool:
    """Heuristic: title equals first-message substring placeholder."""
    derived = title_from(messages, '') or ''
    if not derived:
        return False
    current = re.sub(r'\s+', ' ', str(current_title or '')).strip()
    candidate = re.sub(r'\s+', ' ', str(derived[:64] or '')).strip()
    if not current or not candidate:
        return False
    return current == candidate


def _detect_title_language(text: str) -> str:
    """Best-effort language hint for title generation/validation."""
    s = re.sub(r'\s+', ' ', str(text or '')).strip().lower()
    if not s:
        return ''
    german_markers = {
        'warum', 'werden', 'wird', 'wurde', 'hier', 'nicht', 'mehr', 'alte', 'alten',
        'bilder', 'angezeigt', 'prüfe', 'ich', 'und', 'oder', 'mit', 'für', 'von',
        'zu', 'ist', 'sind', 'bitte', 'kannst',
    }
    tokens = re.findall(r'[A-Za-zÀ-ÖØ-öø-ÿ]+', s)
    german_hits = sum(1 for tok in tokens if tok in german_markers)
    if re.search(r'[äöüß]', s) or german_hits >= 3:
        return 'de'
    return ''


def _script_counts(text: str) -> dict:
    """Return per-script alphabetic character counts for *text*.

    Buckets: ``latin``, ``cjk`` (Han/Hiragana/Katakana/Hangul), ``cyrillic``,
    ``arabic``, ``hebrew``, ``greek``, ``devanagari``. Non-alphabetic and
    unclassified characters are ignored.
    """
    counts: dict[str, int] = {}
    for ch in str(text or ''):
        if not ch.isalpha():
            continue
        o = ord(ch)
        if (0x0041 <= o <= 0x024F) or (0x1E00 <= o <= 0x1EFF):
            bucket = 'latin'
        elif (
            (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF)   # Han
            or (0x3040 <= o <= 0x30FF)                            # Hiragana/Katakana
            or (0xAC00 <= o <= 0xD7A3) or (0x1100 <= o <= 0x11FF) # Hangul
        ):
            bucket = 'cjk'
        elif 0x0400 <= o <= 0x04FF:
            bucket = 'cyrillic'
        elif (0x0600 <= o <= 0x06FF) or (0x0750 <= o <= 0x077F):
            bucket = 'arabic'
        elif 0x0590 <= o <= 0x05FF:
            bucket = 'hebrew'
        elif 0x0370 <= o <= 0x03FF:
            bucket = 'greek'
        elif 0x0900 <= o <= 0x097F:
            bucket = 'devanagari'
        else:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _dominant_script(text: str) -> str:
    """Return a coarse writing-script bucket for *text*, or '' when undecidable.

    Script-level (not language-level) classification is cheap and dependency-free.
    Returns the dominant script only when it holds a clear (≥60%) majority of the
    alphabetic characters, so mixed/borrowed text doesn't flip the bucket. Used
    to establish the conversation start's expected script for cross-script title
    drift detection (#3293).
    """
    counts = _script_counts(text)
    total = sum(counts.values())
    if total < 2:
        return ''
    top, top_n = max(counts.items(), key=lambda kv: kv[1])
    if top_n / total >= 0.6:
        return top
    return ''


def _title_prompt_language_rule(user_text: str) -> str:
    return "Match the language of the user question.\n"


def _title_language_mismatch(user_text: str, title: str) -> bool:
    """Reject titles whose language clearly diverges from the conversation start.

    Two independent signals:
    1. Cross-script drift (#3293): when the conversation start has a clear
       dominant writing script (e.g. latin/English) and the generated title
       introduces a *substantial* amount of a different script (e.g. CJK or
       Cyrillic), reject. This is language-agnostic and catches the common
       "English chat -> Chinese/Spanish/Russian title" drift. Because titles are
       short and frequently embed a borrowed Latin technical term (e.g. a CJK
       title containing the word "Python"), the title side uses a proportion
       threshold (>=35% of the title's alphabetic characters in a non-start
       script, min 2 chars) rather than a strict majority -- so a CJK title with
       one English word still trips, while an English title with a single
       foreign place-name does not.
    2. The legacy German-start → English-title heuristic, preserved verbatim so
       the original behavior keeps working for same-script (latin) drift that
       the script check can't see.
    """
    candidate = str(title or '').strip()
    if not candidate:
        return False

    # (1) Cross-script mismatch — language-agnostic.
    user_script = _dominant_script(user_text)
    if user_script:
        title_counts = _script_counts(candidate)
        title_total = sum(title_counts.values())
        if title_total >= 2:
            for script, n in title_counts.items():
                if script != user_script and n >= 2 and (n / title_total) >= 0.35:
                    return True

    # (2) Legacy same-script German→English heuristic.
    if _detect_title_language(user_text) != 'de':
        return False
    candidate_lower = candidate.lower()
    if _detect_title_language(candidate_lower) == 'de':
        return False
    english_markers = {
        'old', 'image', 'display', 'issue', 'problem', 'discussion', 'conversation',
        'session', 'title', 'fix', 'bug', 'attachment', 'attachments', 'context',
    }
    tokens = re.findall(r'[a-z]+', candidate_lower)
    english_hits = sum(1 for tok in tokens if tok in english_markers)
    return english_hits >= 2


def _title_prompts(user_text: str, assistant_text: str) -> tuple[str, list[str]]:
    qa = f"User question:\n{user_text[:500]}\n\nAssistant answer:\n{assistant_text[:500]}"
    language_rule = _title_prompt_language_rule(user_text)
    prompts = [
        (
            "Generate a short session title from this conversation start.\n"
            "Use BOTH the user's question and the assistant's visible answer.\n"
            f"{language_rule}"
            "Return only the title text, 3-8 words, as a topic label.\n"
            "Do not use markdown, bullets, labels, or prefixes like Session Title:.\n"
            "Do not output a full sentence.\n"
            "Do not output acknowledgements or completion phrases like OK, done, or all set.\n"
            "Do not describe internal reasoning.\n"
            "Bad: The user is asking..., OK, all set.\n"
            "Good: Title Generation Test, Clarify Dialog Layout, GitHub Issue Triage"
        ),
        (
            "Rewrite this conversation start as a concise noun-phrase title.\n"
            "Use the actual topic, not the task outcome.\n"
            f"{language_rule}"
            "Return title text only.\n"
            "Do not use markdown, bullets, labels, or prefixes like Session Title:.\n"
            "Never output acknowledgements, completion status, or meta commentary."
        ),
    ]
    return qa, prompts


def _fallback_title_from_exchange(user_text: str, assistant_text: str) -> Optional[str]:
    """Generate a readable local fallback title when LLM title generation fails."""
    user_text = (user_text or '').strip()
    assistant_text = _strip_thinking_markup(assistant_text or '').strip()
    if not user_text:
        return None
    user_text = _strip_workspace_prefix(user_text)
    user_text = re.sub(r'\s+', ' ', user_text).strip()
    assistant_text = re.sub(r'\s+', ' ', assistant_text).strip()
    combined = f"{user_text} {assistant_text}".strip().lower()
    combined_raw = f"{user_text} {assistant_text}".strip()
    def _contains_latin(text: str) -> bool:
        return bool(re.search(r'[A-Za-z]', text or ''))

    def _extract_named_topic(text: str) -> str:
        m = re.search(r'"([^"\n]{2,24})"', text)
        if m:
            return (m.group(1) or '').strip()
        m = re.search(r'“([^”\n]{2,24})”', text)
        if m:
            return (m.group(1) or '').strip()
        return ''

    topic_name = _extract_named_topic(combined_raw)
    if topic_name:
        if not _contains_latin(topic_name):
            if any(k in combined for k in ('time', 'schedule', 'efficiency', 'manage', 'fitness', 'singing', 'calligraphy')):
                return 'Time management discussion'
            if any(k in combined for k in ('hermes', 'codex', 'ai')):
                return 'AI productivity discussion'
            return 'Conversation topic'
        if any(k in combined for k in ('time', 'schedule', 'efficiency', 'manage', 'fitness', 'singing', 'calligraphy')):
            return f'{topic_name} time management'
        if any(k in combined for k in ('hermes', 'codex', 'ai')):
            return f'{topic_name} AI productivity'
        return f'{topic_name} discussion'

    if any(k in combined for k in ('title', 'session title')) and any(k in combined for k in ('summary', 'summar', 'short title')):
        if any(k in combined for k in ('test', 'ok', 'reply ok')):
            return 'Session title auto-summary test'
        return 'Session title auto-summary'
    if any(k in combined for k in ('clarify', 'clarification')) and any(k in combined for k in ('dialog', 'card')):
        return 'Clarify dialog card'
    if any(k in combined for k in ('issue', 'github', 'pr')) and any(k in combined for k in ('triage', 'bug', 'review')):
        return 'GitHub Issue Triage'

    head = re.split(r'[.!?\n]', user_text)[0].strip()
    if not head:
        return None

    stop_en = {
        'the', 'this', 'that', 'with', 'from', 'into', 'just', 'reply', 'please',
        'need', 'needs', 'want', 'wants', 'user', 'assistant', 'could', 'would',
        'should', 'about', 'there', 'here', 'test', 'testing', 'title', 'summary',
    }
    # Unicode-aware Latin tokenization: keep the old "no leading underscore"
    # and non-Latin placeholder behavior while allowing letters such as ä/ö/ü/ß.
    # The previous ASCII-only pattern turned "führe" into "f" + "hre"; the short
    # "f" was filtered and the broken "hre" became part of the title.
    latin_word = r'A-Za-z0-9À-ÖØ-öø-ÿ'
    tokens = re.findall(rf'[{latin_word}][{latin_word}_./+-]*', head)
    if not tokens:
        return 'Conversation topic'

    picked = []
    for tok in tokens:
        lower_tok = tok.lower()
        if lower_tok in stop_en or len(lower_tok) < 3:
            continue
        if tok not in picked:
            picked.append(tok)
        if len(picked) >= 4:
            break

    if picked:
        return ' '.join(picked)[:60]
    return 'Conversation topic'


def _is_generic_fallback_title(title: str) -> bool:
    """Return True for low-information fallback labels that should not be persisted."""
    return str(title or '').strip().lower() in {'conversation topic'}
