"""Session-title analysis, generation, validation, and publication."""

from __future__ import annotations

import re
import threading
from typing import Optional

import logging

from api.sessions.cache import get_session
from api.sessions.operations import mark_session_title_generated, session_has_manual_title
from api.sessions.projects import title_from
from api.sessions.repository import edit_session

from .thinking_content import (
    _looks_invalid_generated_title,
    _message_text,
    _sanitize_generated_title,
    _strip_thinking_markup,
)
from api.workspace_context import _strip_workspace_prefix


logger = logging.getLogger(__name__)


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


def _is_minimax_route(provider: str = '', model: str = '', base_url: str = '') -> bool:
    text = ' '.join([
        str(provider or '').lower(),
        str(model or '').lower(),
        str(base_url or '').lower(),
    ])
    return 'minimax' in text or 'minimaxi.com' in text


def _route_rejects_reasoning_extra(provider: str = '', model: str = '', base_url: str = '') -> bool:
    """Routes known to reject an ``extra_body`` ``reasoning`` parameter with HTTP 400.

    Title generation injects ``extra_body={"reasoning": {"enabled": False}}`` to
    suppress thinking on reasoning-capable models (#2083). But OpenAI Chat
    Completions (and Azure OpenAI) reject unknown top-level params with a 400, so
    that inject silently fails the title call and falls back to a low-quality
    heuristic title (#4161). Skip the inject for those routes.

    OpenRouter Anthropic mandatory-reasoning models (Claude Sonnet 4.6 / Opus 4.8)
    are reasoning-capable but reject a reasoning *disable* — title gen only needs
    reasoning off, so skip the inject for them too rather than risk the same 400.
    """
    provider_lower = str(provider or '').strip().lower()
    model_lower = str(model or '').strip().lower()
    # Hostname-based match (not substring) so a proxy URL that merely *contains*
    # one of these strings in a path segment isn't mis-classified.
    host = ''
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(str(base_url or '').strip()).hostname or '').lower()
    except Exception:
        host = ''
    if host == 'openai.com' or host.endswith('.openai.azure.com'):
        return True
    # Azure AI Foundry chat-completions hosts (also reject the reasoning param).
    if host.endswith('.services.ai.azure.com') or host.endswith('.cognitiveservices.azure.com'):
        return True
    if provider_lower in ('openai', 'openai-api', 'openai-codex'):
        return True
    if (
        provider_lower in ('azure', 'azure-foundry', 'azure-ai-foundry', 'azure-ai')
        or provider_lower.startswith('azure/')
        or provider_lower.startswith('azure-')
    ):
        return True
    if (host == 'openrouter.ai' or host.endswith('.openrouter.ai')) and model_lower.startswith('anthropic/'):
        # Anthropic on OpenRouter: mandatory-reasoning families reject a disable.
        return True
    return False


def _get_aux_title_config() -> dict:
    """Return title_generation auxiliary config, or an empty dict on errors."""
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config
        tg = _get_auxiliary_task_config('title_generation')
        return tg if isinstance(tg, dict) else {}
    except Exception:
        return {}


def _aux_title_configured() -> bool:
    """Return True when any auxiliary title_generation config field is meaningfully set."""
    tg = _get_aux_title_config()
    provider = tg.get('provider', '') or ''
    model = tg.get('model', '') or ''
    base_url = tg.get('base_url', '') or ''
    return bool(model or base_url or (provider and provider.lower() != 'auto'))


def _aux_title_timeout(default: float = 15.0) -> float:
    """Return the configured timeout (seconds) for auxiliary title generation.

    Only accepts positive numeric values.  Falls back to *default* when the
    value is ``None``, non-numeric, zero, or negative, and emits a debug log
    so mis-configurations are visible in server output.
    """
    try:
        tg = _get_aux_title_config()
        raw = tg.get('timeout')
        if raw is None:
            return default
        try:
            value = float(raw)
        except (ValueError, TypeError):
            logger.debug("aux title timeout: non-numeric value %r, falling back to %s", raw, default)
            return default
        if value > 0:
            return value
        logger.debug("aux title timeout: non-positive value %s, falling back to %s", value, default)
        return default
    except Exception:
        return default


def _title_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    # Title generation is a small auxiliary task, but reasoning models may
    # spend a surprising amount of the completion budget before emitting final
    # content.  Keep the budget high enough for MiniMax/Kimi-style reasoning
    # responses without making title generation depend on provider-specific
    # one-off branches.
    return 512


def _title_retry_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    return max(1024, _title_completion_budget(provider, model, base_url) * 2)


def _title_retry_status(status: str) -> bool:
    # Whether to grant a second budget attempt within the same prompt+model
    # combination.  ``llm_length`` indicates the model would have produced
    # content with more headroom, so doubling the budget can help.
    #
    # ``llm_empty_reasoning`` historically also triggered a retry, but for
    # reasoning models (Qwen3-thinking, DeepSeek-R1, Kimi-K2, etc.) that
    # status means the model burned its entire budget on hidden reasoning
    # tokens and emitted nothing visible.  Doubling the budget in that case
    # just doubles the GPU/credit cost without changing the outcome — the
    # next attempt produces the same shape.  We skip the retry for empty-
    # reasoning statuses and let the title path fall through to the local
    # fallback summary.  See issue #2083 for the LM Studio + Qwen3 repro.
    return status in {
        'llm_length',
        'llm_length_aux',
    }


def _title_should_skip_remaining_attempts(status: str) -> bool:
    """Statuses where re-issuing the next prompt against the same model
    produces the same failing shape (model burned its budget on hidden
    reasoning, hit a hard provider gate, etc.).

    Short-circuit the prompt-iteration loop so we don't issue a second
    full-budget LLM call (and twice the GPU/credit burn) only to land in
    the same fallback path. See issue #2083.

    Add a status here only when retrying the next prompt is provably
    wasted work (single-call signal already establishes that the next
    call will return the same shape). Length-truncation WITHOUT
    reasoning is NOT in the set — that's legitimately recoverable by
    a larger budget on a different prompt and stays in
    :func:`_title_retry_status`.
    """
    return status in {
        'llm_empty_reasoning',
        'llm_empty_reasoning_aux',
    }


def _safe_obj_value(obj, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    # Missing MagicMock attrs stringify as mock reprs and look truthy.  Treat
    # them as absent so tests model real provider objects accurately.
    if value.__class__.__module__.startswith('unittest.mock'):
        return None
    return value


def _safe_text_value(value) -> str:
    if value is None:
        return ''
    if value.__class__.__module__.startswith('unittest.mock'):
        return ''
    return str(value or '').strip()


def _extract_title_response(resp, *, aux: bool = False) -> tuple[str, str]:
    """Return (content, empty_status) from an OpenAI-compatible response."""
    suffix = '_aux' if aux else ''
    try:
        choices = _safe_obj_value(resp, 'choices') or []
        choice = choices[0] if choices else None
        message = _safe_obj_value(choice, 'message')
        content = _safe_text_value(_safe_obj_value(message, 'content'))
        if content:
            return content, ''
        finish_reason = _safe_text_value(_safe_obj_value(choice, 'finish_reason')).lower()
        reasoning = (
            _safe_text_value(_safe_obj_value(message, 'reasoning'))
            or _safe_text_value(_safe_obj_value(message, 'reasoning_content'))
            or _safe_text_value(_safe_obj_value(message, 'thinking'))
        )
        # When the model emitted reasoning tokens but no visible content, it
        # burned its budget on hidden thinking — retrying with a larger budget
        # almost never recovers a useful title (see issue #2083: Qwen3-thinking
        # via LM Studio loops indefinitely on auto-title generation).  Report
        # this case distinctly so callers can short-circuit instead of double-
        # billing the GPU/credit on a near-certain repeat.
        if reasoning:
            return '', f'llm_empty_reasoning{suffix}'
        if finish_reason == 'length':
            return '', f'llm_length{suffix}'
        return '', f'llm_empty{suffix}'
    except Exception:
        return '', f'llm_empty{suffix}'


def generate_title_raw_via_aux(
    user_text: str,
    assistant_text: str,
    provider: str = '',
    model: str = '',
    base_url: str = '',
) -> tuple[Optional[str], str]:
    """Return (raw_text, status) via auxiliary LLM route."""
    if not user_text or not assistant_text:
        return None, 'missing_exchange'
    qa, prompts = _title_prompts(user_text, assistant_text)
    configured = _get_aux_title_config()
    caller_supplied_route = bool(provider or model or base_url)
    provider = provider or configured.get('provider', '') or ''
    if str(provider).strip().lower() == 'auto':
        provider = ''
    model = model or configured.get('model', '') or ''
    base_url = base_url or configured.get('base_url', '') or ''
    try:
        from api.profiles import (
            split_webui_provider_model_value as _split_webui_provider_model_value,
        )

        normalized_model, normalized_provider = _split_webui_provider_model_value(
            model or None,
            provider or None,
        )
        model = normalized_model or ''
        provider = normalized_provider or ''
    except ValueError:
        pass
    api_key = ''
    if not caller_supplied_route:
        api_key = str(configured.get('api_key', '') or '').strip()
    base_max_tokens = _title_completion_budget(provider, model, base_url)
    reasoning_extra = {}
    if not _route_rejects_reasoning_extra(provider, model, base_url):
        reasoning_extra["reasoning"] = {"enabled": False}
    if _is_minimax_route(provider, model, base_url):
        reasoning_extra["reasoning_split"] = True
    try:
        _timeout = _aux_title_timeout()
        from agent.auxiliary_client import call_llm
        last_status = 'llm_error_aux'
        for idx, prompt in enumerate(prompts):
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": qa},
            ]
            budgets = [base_max_tokens]
            try:
                for budget_idx, max_tokens in enumerate(budgets):
                    resp = call_llm(
                        task='title_generation',
                        provider=provider or None,
                        model=model or None,
                        base_url=base_url or None,
                        api_key=api_key or None,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.2,
                        timeout=_timeout,
                        extra_body=reasoning_extra or None,
                    )
                    raw, empty_status = _extract_title_response(resp, aux=True)
                    if raw:
                        return raw, ('llm_aux' if idx == 0 and budget_idx == 0 else 'llm_aux_retry')
                    last_status = empty_status or 'llm_empty_aux'
                    if budget_idx == 0 and _title_retry_status(last_status):
                        budgets.append(_title_retry_completion_budget(provider, model, base_url))
            except Exception as e:
                last_status = 'llm_error_aux'
                logger.debug("Aux title generation attempt %s failed: %s", idx + 1, e)
            # If the model just burned its budget on hidden reasoning, retrying
            # the next prompt against the same model produces the same shape.
            # Short-circuit to the local fallback path (#2083).
            if _title_should_skip_remaining_attempts(last_status):
                logger.debug(
                    "Aux title generation short-circuiting after %s (reasoning-only response).",
                    last_status,
                )
                break
        return None, last_status
    except Exception as e:
        logger.debug("Aux title generation failed: %s", e)
        return None, 'llm_error_aux'


def generate_title_raw_via_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str]:
    """Return (raw_text, status) via active-agent route."""
    if not user_text or not assistant_text:
        return None, 'missing_exchange'
    if agent is None:
        return None, 'missing_agent'

    qa, prompts = _title_prompts(user_text, assistant_text)
    base_max_tokens = _title_completion_budget(
        getattr(agent, 'provider', ''),
        getattr(agent, 'model', ''),
        getattr(agent, 'base_url', ''),
    )
    disabled_reasoning = {"enabled": False}
    prev_reasoning = getattr(agent, 'reasoning_config', None)
    try:
        agent.reasoning_config = disabled_reasoning
        for idx, prompt in enumerate(prompts):
            api_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": qa},
            ]
            budgets = [base_max_tokens]
            try:
                last_status = 'llm_empty'
                for budget_idx, max_tokens in enumerate(budgets):
                    raw = ""
                    empty_status = ''
                    if getattr(agent, 'api_mode', '') == 'codex_responses':
                        codex_kwargs = agent._build_api_kwargs(api_messages)
                        codex_kwargs.pop('tools', None)
                        if 'max_output_tokens' in codex_kwargs:
                            codex_kwargs['max_output_tokens'] = max_tokens
                        resp = agent._run_codex_stream(codex_kwargs)
                        assistant_message, _ = agent._normalize_codex_response(resp)
                        raw = (assistant_message.content or '') if assistant_message else ''
                        if not raw:
                            empty_status = 'llm_empty'
                    elif getattr(agent, 'api_mode', '') == 'anthropic_messages':
                        from agent.anthropic_adapter import build_anthropic_kwargs, normalize_anthropic_response
                        ant_kwargs = build_anthropic_kwargs(
                            model=agent.model,
                            messages=api_messages,
                            tools=None,
                            max_tokens=max_tokens,
                            reasoning_config=disabled_reasoning,
                            is_oauth=getattr(agent, '_is_anthropic_oauth', False),
                            preserve_dots=agent._anthropic_preserve_dots(),
                            base_url=getattr(agent, '_anthropic_base_url', None),
                        )
                        resp = agent._anthropic_messages_create(ant_kwargs)
                        assistant_message, _ = normalize_anthropic_response(
                            resp, strip_tool_prefix=getattr(agent, '_is_anthropic_oauth', False)
                        )
                        raw = (assistant_message.content or '') if assistant_message else ''
                        if not raw:
                            empty_status = 'llm_empty'
                    else:
                        api_kwargs = agent._build_api_kwargs(api_messages)
                        api_kwargs.pop('tools', None)
                        api_kwargs['temperature'] = 0.1
                        api_kwargs['timeout'] = 15.0
                        # Reasoning suppression for title gen is already handled
                        # route-correctly by `_build_api_kwargs()` from the
                        # `agent.reasoning_config = {"enabled": False}` set above —
                        # each provider profile applies (or deliberately omits) the
                        # disable in the form its endpoint accepts (OpenAI/Nous omit
                        # the field; LM Studio uses top-level reasoning_effort;
                        # OpenRouter Anthropic mandatory-reasoning is omitted). Do NOT
                        # re-inject a generic `reasoning:{enabled:False}` here — that
                        # re-adds a 400-rejected param on top of the profile output
                        # (#4161). MiniMax still needs reasoning_split, which the
                        # profile path does not add.
                        _tg_extra = dict(api_kwargs.get('extra_body') or {})
                        if _is_minimax_route(getattr(agent, 'provider', ''), getattr(agent, 'model', ''), getattr(agent, 'base_url', '')):
                            _tg_extra['reasoning_split'] = True
                        if _tg_extra:
                            api_kwargs['extra_body'] = _tg_extra
                        if 'max_completion_tokens' in api_kwargs:
                            api_kwargs['max_completion_tokens'] = max_tokens
                        else:
                            api_kwargs['max_tokens'] = max_tokens
                        resp = agent._ensure_primary_openai_client(reason='title_generation').chat.completions.create(
                            **api_kwargs,
                        )
                        raw, empty_status = _extract_title_response(resp)
                    raw = str(raw or '').strip()
                    if raw:
                        return raw, ('llm' if idx == 0 and budget_idx == 0 else 'llm_retry')
                    last_status = empty_status or 'llm_empty'
                    if budget_idx == 0 and _title_retry_status(last_status):
                        budgets.append(_title_retry_completion_budget(
                            getattr(agent, 'provider', ''),
                            getattr(agent, 'model', ''),
                            getattr(agent, 'base_url', ''),
                        ))
            except Exception as e:
                last_status = 'llm_error'
                logger.debug(
                    "Agent title generation attempt %s failed: provider=%s model=%s error=%s",
                    idx + 1,
                    getattr(agent, 'provider', None),
                    getattr(agent, 'model', None),
                    e,
                )
            # If the model just burned its budget on hidden reasoning, retrying
            # the next prompt against the same model produces the same shape.
            # Short-circuit to the local fallback path (#2083).
            if _title_should_skip_remaining_attempts(last_status):
                logger.debug(
                    "Agent title generation short-circuiting after %s (reasoning-only response).",
                    last_status,
                )
                break
        return None, last_status
    except Exception as e:
        logger.debug("Agent title generation failed: %s", e)
        return None, 'llm_error'
    finally:
        agent.reasoning_config = prev_reasoning


def _generate_llm_session_title_for_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str, str]:
    """Generate a title via active-agent route, then sanitize/validate result."""
    raw, status = generate_title_raw_via_agent(agent, user_text, assistant_text)
    if not raw:
        return None, status, ''
    title = _sanitize_generated_title(raw)
    if title:
        if _title_language_mismatch(user_text, title):
            return None, 'llm_language_mismatch', str(raw)[:120]
        return title, status, ''
    return None, 'llm_invalid', str(raw)[:120]


def _generate_llm_session_title_via_aux(user_text: str, assistant_text: str, agent=None, *, use_agent_model: bool = False) -> tuple[Optional[str], str, str]:
    """Generate a title via dedicated auxiliary LLM route, then sanitize/validate result.

    When use_agent_model is False (default), the auxiliary client resolves
    provider/model/base_url from config.yaml auxiliary.title_generation, which
    prevents the session's chat model (e.g. a Chinese model) from overriding
    the dedicated title model.  When True, the agent's attrs are passed through
    (legacy fallback behaviour).
    """
    if use_agent_model and agent:
        provider = getattr(agent, 'provider', '')
        model = getattr(agent, 'model', '')
        base_url = getattr(agent, 'base_url', '')
    else:
        provider = ''
        model = ''
        base_url = ''
    raw, status = generate_title_raw_via_aux(
        user_text,
        assistant_text,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    if not raw:
        return None, status, ''
    title = _sanitize_generated_title(raw)
    if title:
        if _title_language_mismatch(user_text, title):
            return None, 'llm_language_mismatch_aux', str(raw)[:120]
        return title, status, ''
    return None, 'llm_invalid_aux', str(raw)[:120]


def _put_title_status(put_event, session_id: str, status: str, reason: str = '', title: str = '', raw_preview: str = '') -> None:
    payload = {'session_id': session_id, 'status': status}
    if reason:
        payload['reason'] = reason
    if title:
        payload['title'] = title
    if raw_preview:
        payload['raw_preview'] = raw_preview
    put_event('title_status', payload)
    logger.info(
        "title_status session=%s status=%s reason=%s title=%r raw_preview=%r",
        session_id,
        status,
        reason or '-',
        title or '',
        (raw_preview or '')[:120],
    )


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


def _run_background_title_update(session_id: str, user_text: str, assistant_text: str, placeholder_title: str, put_event, agent=None):
    """Generate and publish a better title after `done`, then end the stream."""
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            _put_title_status(put_event, session_id, 'skipped', 'missing_session')
            return
        # Allow self-heal when a previously generated title leaked thinking text.
        _invalid_existing = _looks_invalid_generated_title(s.title)
        if getattr(s, 'llm_title_generated', False) and not _invalid_existing:
            _put_title_status(put_event, session_id, 'skipped', 'already_generated', str(s.title or ''))
            return
        current = str(s.title or '').strip()
        if session_has_manual_title(s):
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', current)
            return
        still_auto = (
            current == placeholder_title
            or current in ('Untitled', 'New Chat', '')
            or _is_provisional_title(current, s.messages)
            or _invalid_existing
        )
        if not still_auto:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', current)
            return
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
            aux_title_configured = _aux_title_configured()
            if agent and not aux_title_configured:
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
                if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
            else:
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
                if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
            source = llm_status
            if not next_title:
                fallback_title = _fallback_title_from_exchange(user_text, assistant_text)
                if fallback_title and not _is_generic_fallback_title(fallback_title):
                    logger.debug("Using local fallback for session title generation")
                    next_title = fallback_title
                    source = 'fallback'
                elif fallback_title:
                    logger.debug("Skipping generic local fallback for session title generation: %r", fallback_title)
        fallback_reason = (
            f'local_summary:{llm_status}'
            if source == 'fallback' and llm_status
            else 'local_summary'
        )
        wrote_title = False
        effective_title = current
        if next_title:
            skip_for_newer_title = False
            try:
                with edit_session(
                    session_id,
                    touch_updated_at=False,
                    save_when=lambda _session: wrote_title,
                ) as current_session:
                    effective_title = str(current_session.title or '').strip()
                    manual_title = session_has_manual_title(current_session)
                    invalid_existing_now = _looks_invalid_generated_title(current_session.title)
                    still_auto = (
                        effective_title == placeholder_title
                        or effective_title in ('Untitled', 'New Chat', '')
                        or _is_provisional_title(effective_title, current_session.messages)
                        or invalid_existing_now
                    )
                    if manual_title or not still_auto:
                        skip_for_newer_title = True
                    elif next_title != effective_title:
                        current_session.title = next_title
                        mark_session_title_generated(current_session)
                        effective_title = current_session.title
                        wrote_title = True
            except KeyError:
                _put_title_status(put_event, session_id, 'skipped', 'missing_session')
                return
            if skip_for_newer_title:
                _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective_title)
                return

        if wrote_title:
            if source == 'fallback':
                _put_title_status(put_event, session_id, source, fallback_reason, effective_title, raw_preview)
            else:
                _put_title_status(put_event, session_id, source, llm_status, effective_title, raw_preview)
            put_event('title', {'session_id': session_id, 'title': effective_title})
        else:
            _put_title_status(put_event, session_id, 'skipped', source or 'unchanged', effective_title, raw_preview)
    finally:
        put_event('stream_end', {'session_id': session_id})


def _run_background_title_refresh(session_id: str, user_text: str, assistant_text: str, current_title: str, put_event, agent=None):
    """Refresh an existing LLM-generated title using the latest exchange text.

    Unlike _run_background_title_update, this does NOT guard on
    llm_title_generated — it assumes the title was already LLM-generated
    and the session has progressed enough to warrant a refresh.
    It does NOT emit stream_end (the caller already did).
    """
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            return
        # Safety: skip if user manually renamed since the check
        effective = str(s.title or '').strip()
        if session_has_manual_title(s):
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective)
            return
        if effective != current_title:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective)
            return
        if not effective or effective in ('Untitled', 'New Chat'):
            return
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
            aux_title_configured = _aux_title_configured()
            if agent and not aux_title_configured:
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
                if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
            else:
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
                if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
        if not next_title:
            _put_title_status(put_event, session_id, 'refresh_skipped', llm_status or 'empty', effective, raw_preview)
            return
        # Skip if the new title is essentially the same (after normalization)
        normalized_current = re.sub(r'\s+', ' ', effective).strip().lower()
        normalized_new = re.sub(r'\s+', ' ', next_title).strip().lower()
        if normalized_current == normalized_new:
            _put_title_status(put_event, session_id, 'refresh_skipped', 'same_title', effective, raw_preview)
            return
        wrote_title = False
        skip_for_newer_title = False
        try:
            with edit_session(
                session_id,
                touch_updated_at=False,
                save_when=lambda _session: wrote_title,
            ) as current_session:
                effective_title = str(current_session.title or '').strip()
                # Re-check under the session owner lock: a user rename or a
                # newer automatic publication must win over this slow worker.
                if session_has_manual_title(current_session) or effective_title != current_title:
                    skip_for_newer_title = True
                else:
                    current_session.title = next_title
                    mark_session_title_generated(current_session)
                    effective_title = current_session.title
                    wrote_title = True
        except KeyError:
            return
        if skip_for_newer_title:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective_title)
            return
        _put_title_status(put_event, session_id, 'refreshed', llm_status, effective_title, raw_preview)
        put_event('title', {'session_id': session_id, 'title': effective_title})
        logger.info("Adaptive title refresh: session=%s new_title=%r", session_id, effective_title)
    except Exception:
        logger.debug("Background title refresh failed for session %s", session_id, exc_info=True)


def generate_session_title_for_session(session, *, prefer_latest: bool = False, agent=None) -> tuple[Optional[str], str, str]:
    """Generate a session title on demand from persisted conversation messages.

    This helper powers explicit UI title-regeneration controls. It intentionally
    does not inspect or mutate ``llm_title_generated``; callers decide whether
    replacing the current title is allowed, then persist the returned title.
    """
    messages = getattr(session, 'messages', None) or []
    if prefer_latest:
        user_text, assistant_text = _latest_exchange_snippets(messages)
    else:
        user_text, assistant_text = _first_exchange_snippets(messages)
    if not user_text:
        return None, 'empty_user_message', ''
    from api import profiles as profiles_api

    with profiles_api.profile_env_for_background_worker(session, "manual title regeneration", logger_override=logger):
        next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent)
    if next_title:
        return next_title, llm_status, raw_preview
    fallback_title = _fallback_title_from_exchange(user_text, assistant_text)
    if fallback_title and not _is_generic_fallback_title(fallback_title):
        reason = f'local_summary:{llm_status}' if llm_status else 'local_summary'
        return fallback_title, reason, raw_preview
    return None, llm_status or 'empty_title', raw_preview


def _maybe_schedule_title_refresh(session, put_event, agent):
    """Check if the session is due for an adaptive title refresh and schedule it."""
    refresh_interval = _get_title_refresh_interval()
    if refresh_interval <= 0:
        return
    current_title = str(session.title or '').strip()
    if not current_title or current_title in ('Untitled', 'New Chat'):
        return
    if session_has_manual_title(session):
        return
    if not getattr(session, 'llm_title_generated', False):
        return
    exchange_count = _count_exchanges(session.messages)
    if exchange_count <= 0 or exchange_count % refresh_interval != 0:
        return
    last_u, last_a = _latest_exchange_snippets(session.messages)
    if not last_u and not last_a:
        return
    threading.Thread(
        target=_run_background_title_refresh,
        args=(session.session_id, last_u, last_a, current_title, put_event, agent),
        daemon=True,
    ).start()
