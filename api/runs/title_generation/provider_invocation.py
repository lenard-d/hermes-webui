"""Provider invocation for session-title generation.

The module owns auxiliary-route resolution, active-agent invocation, retry
budgets, provider-specific request policy, and response normalization.  It does
not schedule work or mutate sessions.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..thinking_content import _sanitize_generated_title
from .policy import _title_language_mismatch, _title_prompts


logger = logging.getLogger(__name__)


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
