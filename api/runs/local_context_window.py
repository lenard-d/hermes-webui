"""Context-window projection for a completed local agent turn.

The provider agent's compressor is not always authoritative after an in-place
model switch: it can still expose the previous model's context window.  This
module resolves that ambiguity once and projects one decision into both the
durable session and the terminal usage event.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.model_context import (
    _context_length_lookup_inputs_for_model,
    _should_accept_session_context_length_refresh,
)


@dataclass(frozen=True)
class ContextWindowProjection:
    """One authoritative context-window view for persistence and SSE."""

    context_length: int
    threshold_tokens: int
    last_prompt_tokens: int

    @classmethod
    def from_agent(
        cls,
        agent,
        *,
        resolved_model: str | None,
        resolved_provider: str | None,
        resolved_base_url: str | None,
        resolved_api_key: str | None,
        config: dict,
    ) -> "ContextWindowProjection":
        compressor = getattr(agent, "context_compressor", None)
        compressor_cap = int(getattr(compressor, "context_length", 0) or 0)
        threshold = int(getattr(compressor, "threshold_tokens", 0) or 0)
        last_prompt = int(getattr(compressor, "last_prompt_tokens", 0) or 0)

        model = str(getattr(agent, "model", resolved_model or "") or "").strip()
        metadata_cap = _resolve_model_context_length(
            model,
            provider=resolved_provider,
            base_url=getattr(agent, "base_url", "") or resolved_base_url or "",
            api_key=getattr(agent, "api_key", "") or resolved_api_key or "",
            config=config,
        )

        effective_cap = compressor_cap
        if not compressor_cap:
            effective_cap = metadata_cap
        elif (
            metadata_cap
            and metadata_cap != compressor_cap
            and _should_accept_session_context_length_refresh(compressor_cap, metadata_cap)
        ):
            effective_cap = metadata_cap
            threshold = _rescale_threshold(
                threshold,
                source_cap=compressor_cap,
                target_cap=metadata_cap,
            )

        return cls(
            context_length=max(0, int(effective_cap or 0)),
            threshold_tokens=max(0, int(threshold or 0)),
            last_prompt_tokens=max(0, int(last_prompt or 0)),
        )

    def persist_on(self, session) -> None:
        """Persist the completed turn's context projection on ``session``."""

        if self.context_length:
            session.context_length = self.context_length
        session.threshold_tokens = self.threshold_tokens
        session.last_prompt_tokens = self.last_prompt_tokens

    def enrich_usage(self, usage: dict, *, session) -> None:
        """Add the same projection to a terminal usage payload."""

        if self.context_length:
            usage["context_length"] = self.context_length
        usage["threshold_tokens"] = self.threshold_tokens
        usage["last_prompt_tokens"] = (
            self.last_prompt_tokens
            or int(getattr(session, "last_prompt_tokens", 0) or 0)
        )


def _resolve_model_context_length(
    model: str,
    *,
    provider: str | None,
    base_url: str,
    api_key: str,
    config: dict,
) -> int:
    """Resolve model metadata while retaining compatibility with older agents."""

    if not model:
        return 0

    try:
        from agent.model_metadata import get_model_context_length

        lookup = _context_length_lookup_inputs_for_model(
            model,
            provider or "",
            base_url=base_url,
            api_key=api_key,
            cfg=config if isinstance(config, dict) else {},
        )
        try:
            value = get_model_context_length(
                model,
                lookup.base_url,
                api_key=lookup.api_key,
                config_context_length=lookup.config_context_length,
                provider=lookup.provider or provider or "",
                custom_providers=lookup.custom_providers,
            )
        except TypeError:
            value = get_model_context_length(model, lookup.base_url)
        return max(0, int(value or 0))
    except Exception:
        # Context metadata is display/accounting enrichment.  An unavailable
        # helper must not turn a completed provider run into a failed turn.
        return 0


def _rescale_threshold(threshold: int, *, source_cap: int, target_cap: int) -> int:
    if threshold <= 0 or source_cap <= 0 or target_cap <= 0:
        return 0
    return int(threshold * target_cap / source_cap)
