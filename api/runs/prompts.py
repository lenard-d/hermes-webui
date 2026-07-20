"""Ephemeral WebUI system and delivery-context prompts."""

from __future__ import annotations

from typing import Optional

from .webui_prefill import _webui_delivery_context_prompt


_WEBUI_PROGRESS_PROMPT = """
WebUI progress guidance:
- Match the normal Hermes messaging style, but do not let long tool-running WebUI turns appear silent.
- For long multi-step work that uses tools, emit brief user-visible progress updates as normal assistant content, not only as hidden reasoning.
- Before the first tool batch in a long task, say what you are about to inspect.
- After each meaningful batch of tool calls, say what you just confirmed and what you will check next before continuing with more tools.
- Do not run many independent tool batches back-to-back without visible assistant text between them when the task is still ongoing.
- Do not keep progress only in reasoning, thinking, or tool-result channels; those are not a substitute for visible interim updates.
- Each update should say what you are about to check, what you just confirmed, or why the next tool call is needed.
- Keep updates concise, factual, and in the user's language. One or two short sentences are enough.
- Do not reveal hidden reasoning, chain-of-thought, private scratchpads, secrets, raw logs, or long tool output.
- Password, API-key, token, and secret fields are automatically redacted by the system. Treat masked values as intentional redaction, not placeholder text or user input errors, and do not tell the user a stored credential is wrong based on a masked value alone.
- Final visible assistant replies must be clear, user-facing, and in the user's language, not private planning notes.
- Do not include terse planning fragments or scratchpad shorthand in visible assistant text. Avoid fragments like "Need script", "Need check logs", "Need inspect email", or "maybe invite"; either omit them or rewrite them as clear user-facing progress.
- For direct answers or very short tasks, skip progress updates and answer normally.
""".strip()


def _webui_surface_context_prompt(surface_context: Optional[dict]) -> str:
    """Return safe WebUI session metadata for the agent's ephemeral context.

    Messaging gateways inject platform/channel context before each run. Browser
    sessions do not have a chat platform wrapper, so provide an explicit, small
    surface description here instead of relying on the model to infer where it
    is running from the transcript alone.
    """
    if not isinstance(surface_context, dict):
        return ""

    lines = [
        "WebUI session context:",
        "- This browser session is not the same live transcript as Telegram, Discord, Slack, or other messaging surfaces.",
        "- Use durable memory, saved sessions, and available tools for cross-surface recall instead of assuming those transcripts are in this browser chat.",
        "- Do not copy or dump this browser transcript into external notes or durable memory by default.",
        "- Write to external notes or durable memory only for explicit captures, durable user preferences, decisions, blockers/open issues, runbook-worthy workflows, or other clearly reusable signals; otherwise leave notes unchanged.",
        "- When you do write or update a durable note, briefly tell the user what note/section changed so the write is reviewable.",
    ]
    fields = (
        ("source", "Source"),
        ("session_id", "Session ID"),
        ("profile", "Profile"),
        ("workspace", "Workspace"),
    )
    for key, label in fields:
        raw = surface_context.get(key)
        value = str(raw).strip() if raw is not None else ""
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)

def _webui_ephemeral_system_prompt(
    personality_prompt: Optional[str],
    surface_context: Optional[dict] = None,
    config_data: Optional[dict] = None,
) -> str:
    """Build WebUI-only runtime instructions that are not persisted to history."""
    parts = []
    if personality_prompt:
        parts.append(str(personality_prompt).strip())
    surface_prompt = _webui_surface_context_prompt(surface_context)
    if surface_prompt:
        parts.append(surface_prompt)
    parts.append(_WEBUI_PROGRESS_PROMPT)
    delivery_prompt = _webui_delivery_context_prompt(config_data)
    if delivery_prompt:
        parts.append(delivery_prompt)
    return "\n\n".join(part for part in parts if part)
