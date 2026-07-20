"""Provider-facing message sanitation, positioning, dedupe, and stable IDs."""

from __future__ import annotations

import copy
import re
from types import ModuleType


OOB_USER_MESSAGE_BLOCK_RE = re.compile(
    r'\[OUT-OF-BAND\s+USER\s+MESSAGE(?:\s*(?:—|-)\s*.*?)?\]\s*?.*?\[/OUT-OF-BAND\s+USER\s+MESSAGE\]',
    re.DOTALL | re.IGNORECASE,
)


def strip_native_image_parts_from_content(api: ModuleType, content):
    """Return provider-safe content with native image parts removed.

    Text-only provider endpoints (for example DeepSeek/OpenAI-compatible text
    models) reject historical OpenAI-style ``image_url`` parts before the agent
    can recover.  When WebUI is configured for text-mode image handling, preserve
    textual content from mixed content arrays and drop only the native image
    blocks from replayed history.
    """
    if not isinstance(content, list):
        return content
    clean_parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'image_url' or 'image_url' in part:
            continue
        clean_parts.append(copy.deepcopy(part))
    if not clean_parts:
        return ''
    if len(clean_parts) == 1 and clean_parts[0].get('type') == 'text':
        return str(clean_parts[0].get('text') or '')
    return clean_parts


def strip_oob_blocks(api: ModuleType, content):
    """Remove consumed [OUT-OF-BAND USER MESSAGE ...] blocks from content.

    These markers are internal control data that should never reach the model.
    They can appear as plain strings or inside list-based content parts.
    """
    if isinstance(content, str):
        return api._OOB_USER_MESSAGE_BLOCK_RE.sub('', content)
    if isinstance(content, list):
        return [api._strip_oob_blocks(part) for part in content]
    if isinstance(content, dict):
        return {
            key: api._strip_oob_blocks(value)
            if isinstance(value, (str, list, dict))
            else copy.deepcopy(value)
            for key, value in content.items()
        }
    return content


def content_has_reasoning_only_parts(api: ModuleType, content) -> bool:
    if not isinstance(content, list) or not content:
        return False
    saw_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get('type')
        if part_type in {'thinking', 'reasoning'}:
            text = part.get('thinking') or part.get('reasoning') or part.get('text') or ''
            if str(text).strip():
                saw_reasoning = True
            continue
        if part_type == 'text' and str(part.get('text') or part.get('content') or '').strip():
            return False
        if part_type not in {'text', 'thinking', 'reasoning'}:
            return False
    return saw_reasoning


def is_reasoning_only_assistant_message(api: ModuleType, msg) -> bool:
    """Return True for display-only assistant Thinking entries.

    These entries keep partial Thinking cards visible after reload/cancel, but
    they are not API-safe history: providers only see a blank assistant turn.
    Visible assistant replies that also carry reasoning metadata are kept.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    if msg.get('tool_calls'):
        return False
    content = msg.get('content', '')
    if api._message_text(content).strip():
        return False
    if str(msg.get('reasoning') or msg.get('reasoning_content') or '').strip():
        return True
    return api._content_has_reasoning_only_parts(content)


def is_local_reasoning_replay_base_url(api: ModuleType, base_url: str | None) -> bool:
    """Return True when a custom provider base URL confidently points at localhost."""
    if not base_url:
        return False
    try:
        from urllib.parse import urlsplit

        raw = str(base_url or '').strip()
        if not raw:
            return False
        parsed = urlsplit(raw)
        if not parsed.hostname and '://' not in raw:
            parsed = urlsplit(f"http://{raw}")
        host = (parsed.hostname or '').strip().lower()
    except Exception:
        return False
    return host in {'localhost', '127.0.0.1', '::1', 'localhost.localdomain'}


def should_strip_reasoning_content(
    api: ModuleType,
    cfg: dict | None,
    *,
    mode: str | None = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    effective_base_url: str | None = None,
) -> bool:
    """Decide whether historical assistant reasoning_content should be stripped from model-facing history.

    This is a provider/protocol decision, not a model-capability heuristic.
    Local/generic backends (LM Studio, llama.cpp, Ollama, and custom localhost
    OpenAI-compatible endpoints) do not require historical reasoning replay and
    receive stale content when it's preserved. Unknown providers preserve by
    default so replay does not break providers that require reasoning/tool-call
    continuity.

    Args:
        cfg: Config dict from get_config(), expected to contain webui.reasoning_content_replay.
        mode: Explicit override mode ("strip", "preserve", "auto"). If provided, bypasses config lookup.
        effective_model: Runtime-resolved model for the current session/request.
        effective_provider: Runtime-resolved provider for the current session/request.
        effective_base_url: Runtime-resolved base URL for custom providers.

    Returns:
        True if reasoning_content should be stripped from sanitized output.
        False if it should be preserved in sanitized output.
    """
    # Explicit mode override takes priority
    if mode is not None:
        return mode == "strip"

    # Config lookup. Missing/invalid config preserves shipped behavior: do not
    # strip reasoning_content unless config explicitly requests it or auto can
    # identify a local/generic effective backend.
    if cfg is None:
        return False

    webui_cfg = cfg.get("webui", {}) or {}
    if not isinstance(webui_cfg, dict):
        return False

    replay_mode = webui_cfg.get("reasoning_content_replay")
    if not isinstance(replay_mode, str):
        return False

    normalized = replay_mode.strip().lower()

    if normalized == "preserve":
        return False
    if normalized == "strip":
        return True
    if normalized == "auto":
        # Auto mode: prefer runtime-resolved provider/model because profile
        # defaults may differ from a per-session/request override.
        model_cfg = cfg.get("model", {}) or {}
        if not isinstance(model_cfg, dict):
            model_cfg = {}

        provider_id = str(effective_provider or model_cfg.get("provider", "") or "").strip().lower()
        model_id = str(
            effective_model or model_cfg.get("default") or model_cfg.get("name") or ""
        ).strip().lower()
        base_url = effective_base_url or model_cfg.get("base_url")

        if not provider_id:
            return False

        # Known providers that require historical reasoning_content replay:
        # - DeepSeek thinking mode distinguishes normal history from tool-call reasoning chains
        # - Anthropic Claude 4+/3.7+ uses structured reasoning in tool-use contexts
        # - OpenAI GPT-5+/o-series requires reasoning replay for tool-use continuity
        if provider_id == "deepseek":
            return False  # preserve for DeepSeek

        if provider_id == "anthropic" and model_id.startswith("claude"):
            return False  # preserve for Claude models

        if provider_id == "openai":
            # Preserve for GPT-5+ and o-series (not GPT-4o, etc.)
            # Use exact match + dash-prefixed suffix to avoid broad substring matches.
            _is_reasoning_model = (
                model_id == "gpt-5"
                or model_id.startswith("gpt-5-")
                or model_id in {"o1", "o3", "o4"}
                or model_id.startswith(("o1-", "o3-", "o4-"))
            )
            if _is_reasoning_model:
                return False  # preserve for reasoning-capable OpenAI models

        if provider_id in {"lmstudio", "ollama", "llamacpp", "llama.cpp"}:
            return True

        if provider_id == "custom" or provider_id.startswith("custom:"):
            return api._is_local_reasoning_replay_base_url(base_url)

        # Unknown/cloud providers preserve by default.
        return False

    # Unknown mode -- preserve default behavior.
    return False


def sanitize_messages_for_api(
    api: ModuleType,
    messages,
    *,
    cfg: dict = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    effective_base_url: str | None = None,
):
    """Return a deep copy of messages with only API-safe fields.

    The webui stores extra metadata on messages (attachments, timestamp, _ts)
    for display purposes. Some providers (e.g. Z.AI/GLM) reject unknown fields
    instead of ignoring them, causing HTTP 400 errors on subsequent messages.

    Also strips orphaned tool-role messages whose tool_call_id cannot be linked
    to a preceding assistant message with tool_calls. Strictly-conformant providers
    (Mercury-2/Inception, newer OpenAI models) reject histories containing dangling
    tool results with a 400 error: "Message has tool role, but there was no previous
    assistant message with a tool call."

    If ``agent.image_input_mode`` resolves to ``text``, native historical
    ``image_url`` content parts are stripped too.  Current-turn uploads already
    respect text mode in ``_build_native_multimodal_message``; this closes the
    remaining replay gap where an older native image in the saved transcript kept
    causing 400s on every later text-only turn (#2297).
    """
    strip_native_images = cfg is not None and api._resolve_image_input_mode(cfg) == "text"
    # First pass: collect all tool_call_ids declared by assistant messages.
    # Handles both OpenAI ('id') and Anthropic ('call_id') field names.
    valid_tool_call_ids: set = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            for tc in msg.get('tool_calls') or []:
                if isinstance(tc, dict):
                    tid = tc.get('id') or tc.get('call_id') or ''
                    if tid:
                        valid_tool_call_ids.add(tid)

    # Second pass: build the sanitized list, dropping orphaned tool messages.
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Skip display-only Thinking entries. They are visible transcript
        # metadata, not provider-facing assistant turns.
        if api._is_reasoning_only_assistant_message(msg):
            continue
        # Skip persisted error markers — never send them to the LLM as prior context.
        if msg.get('_error'):
            continue
        # Skip _partial markers with no visible content. Partial messages that
        # carry actual text (e.g. "Python is a high-level…") are kept so the
        # model can continue from the cut-off point (#893). But empty partials
        # (reasoning-only or tool-only cancellations where thinking markup was
        # stripped) have nothing for the model to continue from and cause
        # API 400 errors on strict providers (empty assistant content).
        if msg.get('_partial') and not str(msg.get('content') or '').strip():
            continue
        # Note: _recovered user messages are NOT skipped here — they may need
        # to be retained to preserve role alternation when a kept assistant
        # follows.  The _recovered skip happens in a final pass after orphaned
        # tool_calls are stripped, so the anchor check is exact (#4283).
        # Temporarily mark _recovered users so the final pass can find them.
        is_recovered = msg.get('_recovered') and msg.get('role') == 'user'
        role = msg.get('role')
        if role == 'tool':
            tid = msg.get('tool_call_id') or ''
            if not tid or tid not in valid_tool_call_ids:
                # Orphaned tool result — skip to avoid 400 from strict providers.
                continue
        sanitized = {k: v for k, v in msg.items() if k in api._API_SAFE_MSG_KEYS}
        # Drop empty tool_calls — strict providers (DeepSeek, newer OpenAI)
        # reject tool_calls: [] with HTTP 400 even when no orphaned calls exist.
        if 'tool_calls' in sanitized and not sanitized['tool_calls']:
            del sanitized['tool_calls']
        # Provider-aware reasoning_content stripping from model-facing history.
        # Historical assistant reasoning_content is stripped only when the user
        # explicitly requests strip mode or auto mode identifies a local/generic
        # effective backend.
        if msg.get('role') == 'assistant' and 'reasoning_content' in sanitized:
            if api._should_strip_reasoning_content(
                cfg,
                effective_model=effective_model,
                effective_provider=effective_provider,
                effective_base_url=effective_base_url,
            ):
                del sanitized['reasoning_content']
        if is_recovered:
            sanitized['_recovered'] = True  # temporary marker — stripped before return
        if 'content' in sanitized:
            sanitized['content'] = api._strip_oob_blocks(sanitized['content'])
        if strip_native_images and 'content' in sanitized:
            sanitized['content'] = api._strip_native_image_parts_from_content(sanitized.get('content'))
        if sanitized.get('role'):
            clean.append(sanitized)

    # Third pass: strip orphaned tool_calls from assistant messages — calls whose id
    # has no matching tool-role response in the clean list.  Strict providers (DeepSeek,
    # newer OpenAI) reject with 400 when an assistant message references a tool call that
    # was never answered (e.g. session aborted before results flushed).
    answered_ids: set = set()
    for msg in clean:
        if msg.get('role') == 'tool':
            tid = msg.get('tool_call_id') or ''
            if tid:
                answered_ids.add(tid)

    filtered_clean = []
    for msg in clean:
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            kept = [
                tc for tc in msg['tool_calls']
                if isinstance(tc, dict) and
                (tc.get('id') or tc.get('call_id') or '') in answered_ids
            ]
            if not kept:
                # All calls orphaned: drop tool_calls key; if no content, drop message.
                msg = {k: v for k, v in msg.items() if k != 'tool_calls'}
                if not str(msg.get('content') or '').strip():
                    continue
            else:
                msg = dict(msg, tool_calls=kept)
        filtered_clean.append(msg)

    # Fourth pass: drop _recovered user messages unless removing one would fuse
    # two same-role neighbours.  Operating on filtered_clean (post orphaned-tool/
    # tool_calls stripping) means the neighbour check is exact (#4283).  The
    # decision uses the ACTUAL kept sequence: the previously-kept message's role
    # (`final[-1]`) and the next surviving message's role.  A _recovered user is
    # kept ONLY when it separates two assistants (prev kept == assistant AND next
    # surviving == assistant) — i.e. it is an answered turn whose removal would
    # leave `assistant, assistant` adjacency.  In every other case dropping it is
    # safe and correct: it would either leave a clean `user, assistant` pair, or
    # (if next is a user) it is a stale unanswered prompt that must not replay.
    # Deciding only on "an assistant follows" (ignoring the prev kept role) is the
    # bug that re-introduced `user, _recovered user, assistant` → adjacent users
    # → strict-provider 400 once the anchoring assistant's predecessor was a user.
    final = []
    for i, msg in enumerate(filtered_clean):
        if msg.get('_recovered') and msg.get('role') == 'user':
            prev_role = final[-1].get('role') if final else None
            next_role = None
            for j in range(i + 1, len(filtered_clean)):
                next_role = filtered_clean[j].get('role')
                break
            # Keep only if this recovered user actually separates two assistants.
            if not (prev_role == 'assistant' and next_role == 'assistant'):
                continue  # drop — fusing the neighbours is clean, or it's a stale prompt
            # Keep but strip the temporary marker
            msg = {k: v for k, v in msg.items() if k != '_recovered'}
        final.append(msg)
    return final


def api_safe_message_positions(api: ModuleType, messages):
    """Return [(original_index, sanitized_message)] for API-safe messages."""
    valid_tool_call_ids: set = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            for tc in msg.get('tool_calls') or []:
                if isinstance(tc, dict):
                    tid = tc.get('id') or tc.get('call_id') or ''
                    if tid:
                        valid_tool_call_ids.add(tid)

    out = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if api._is_reasoning_only_assistant_message(msg):
            continue
        if msg.get('_error'):
            continue
        if msg.get('_partial') and not str(msg.get('content') or '').strip():
            continue
        # Note: _recovered user messages are NOT skipped here — deferred to
        # a final pass after orphaned tool_calls stripping (#4283).
        is_recovered = msg.get('_recovered') and msg.get('role') == 'user'
        role = msg.get('role')
        if role == 'tool':
            tid = msg.get('tool_call_id') or ''
            if not tid or tid not in valid_tool_call_ids:
                continue
        sanitized = {k: v for k, v in msg.items() if k in api._API_SAFE_MSG_KEYS}
        if 'tool_calls' in sanitized and not sanitized['tool_calls']:
            del sanitized['tool_calls']
        if is_recovered:
            sanitized['_recovered'] = True  # temporary marker — stripped before return
        if 'content' in sanitized:
            sanitized['content'] = api._strip_oob_blocks(sanitized['content'])
        if sanitized.get('role'):
            out.append((idx, sanitized))

    # Third pass: strip orphaned tool_calls from assistant messages (mirrors
    # _sanitize_messages_for_api pass 3).
    answered_ids: set = set()
    for _idx, msg in out:
        if msg.get('role') == 'tool':
            tid = msg.get('tool_call_id') or ''
            if tid:
                answered_ids.add(tid)

    filtered_out = []
    for idx, msg in out:
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            kept = [
                tc for tc in msg['tool_calls']
                if isinstance(tc, dict) and
                (tc.get('id') or tc.get('call_id') or '') in answered_ids
            ]
            if not kept:
                msg = {k: v for k, v in msg.items() if k != 'tool_calls'}
                if not str(msg.get('content') or '').strip():
                    continue
            else:
                msg = dict(msg, tool_calls=kept)
        filtered_out.append((idx, msg))

    # Fourth pass: drop _recovered user messages unless removing one would fuse
    # two same-role neighbours — mirrors _sanitize_messages_for_api pass 4 (#4283).
    # Decide on the ACTUAL kept sequence: prev kept role (final_out[-1]) + next
    # surviving role. Keep ONLY when it separates two assistants; otherwise drop.
    final_out = []
    for i, (idx, msg) in enumerate(filtered_out):
        if msg.get('_recovered') and msg.get('role') == 'user':
            prev_role = final_out[-1][1].get('role') if final_out else None
            next_role = None
            for j in range(i + 1, len(filtered_out)):
                next_role = filtered_out[j][1].get('role')
                break
            if not (prev_role == 'assistant' and next_role == 'assistant'):
                continue
            msg = {k: v for k, v in msg.items() if k != '_recovered'}
        final_out.append((idx, msg))
    return final_out


def deduplicate_context_messages(api: ModuleType, messages):
    """Remove duplicate messages from context by identity, keeping first occurrence.

    Prevents the agent from seeing the same message twice in conversation_history
    when result_messages contain duplicates that weren't caught by display-merge.
    Compression/reference markers are internal recovery material: keep at most
    one canonical assistant reference so a mis-role ``user`` marker cannot become
    the next active user instruction.
    """
    if not messages:
        return messages
    seen = set()
    deduped = []
    for msg in messages:
        if api._is_context_compression_marker(msg):
            marker_key = (
                '__context_compression_marker__',
                " ".join(api._message_text(msg.get('content', '')).split())[:500],
            )
            if marker_key in seen:
                continue
            seen.add(marker_key)
            if isinstance(msg, dict) and msg.get('role') != 'assistant':
                msg = copy.deepcopy(msg)
                msg['role'] = 'assistant'
            deduped.append(msg)
            continue
        if api._is_compressed_context_tool_result_summary_message(msg) and not msg.get('tool_call_id'):
            deduped.append(msg)
            continue
        key = api._message_identity(msg)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        deduped.append(msg)
    return deduped


def assign_stable_message_ids(api: ModuleType, result_messages, *existing_arrays):
    """Mint a stable, session-unique integer ``id`` on model-result rows lacking one.

    Both ``messages`` (display transcript) and ``context_messages`` (model-facing
    history) are derived from the *same* per-turn ``result['messages']`` dicts.
    Stamping the id on those shared dicts here makes each logical row carry an
    identical id in both arrays, so the fork/truncate aligner
    (``session_ops.truncate_context_for_display_keep``) can match rows by its
    preferred ``id`` key instead of the fragile content-signature fallback that
    goes blind on large sessions full of structurally-identical rows.

    Ids are monotonic within a session (max existing id + 1, seeded from the
    result rows plus any ``existing_arrays`` passed for collision-avoidance).
    Across sessions ids may repeat, which is safe: alignment only ever compares
    rows within one session, and a fork copies both arrays together so the child
    stays internally consistent. Rows whose historical id was carried forward by
    ``_restore_reasoning_metadata`` keep it; only genuinely new rows are minted.

    Returns the number of rows newly stamped. Mutates ``result_messages`` in place.
    """
    if not result_messages:
        return 0
    seed = 0
    for arr in (result_messages, *existing_arrays):
        for m in arr or []:
            if isinstance(m, dict):
                mid = m.get('id')
                # bool is an int subclass; exclude it so a stray True/False id
                # can never seed the counter.
                if isinstance(mid, int) and not isinstance(mid, bool) and mid > seed:
                    seed = mid
    stamped = 0
    for m in result_messages:
        if isinstance(m, dict) and m.get('id') is None:
            seed += 1
            m['id'] = seed
            stamped += 1
    return stamped
