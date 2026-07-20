"""Live token and context metering for one local run.

The tracker owns the mutable prompt estimate and per-model context lookup cache.
It deliberately exposes one snapshot interface to the local event translator and
the terminal result projector.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any


class LocalUsageTracker:
    """Build best-effort live usage snapshots for one admitted stream."""

    def __init__(
        self,
        api: ModuleType,
        *,
        session_id: str,
        session_getter: Callable[[], Any],
        agent_getter: Callable[[], Any],
    ) -> None:
        self._api = api
        self._session_id = session_id
        self._session_getter = session_getter
        self._agent_getter = agent_getter
        self._prompt_estimate_tokens = 0
        self._prompt_exact_tokens = 0
        self._tool_delta_tokens = 0
        self._seen_tool_ids: set[str] = set()
        self._real_context_length: int | None = None
        self._session_cache = [None]

    def _session(self):
        return self._api._live_usage_session_snapshot(
            self._session_id,
            self._session_getter(),
            self._session_cache,
        )

    def _seed_prompt_estimate(self) -> int:
        if self._prompt_estimate_tokens > 0:
            return self._prompt_estimate_tokens
        base = 0
        agent = self._agent_getter()
        if agent is not None:
            try:
                compressor = getattr(agent, "context_compressor", None)
                if compressor:
                    base = getattr(compressor, "last_prompt_tokens", 0) or 0
            except Exception:
                base = 0
        if not base:
            try:
                base = getattr(self._session(), "last_prompt_tokens", 0) or 0
            except Exception:
                base = 0
        self._prompt_estimate_tokens = int(base or 0)
        self._prompt_exact_tokens = self._prompt_estimate_tokens
        return self._prompt_estimate_tokens

    def add_tool_messages(self, messages) -> int:
        """Advance the next-prompt estimate using live tool activity."""
        if not messages:
            return self._prompt_estimate_tokens
        self._seed_prompt_estimate()
        usage = self._api.live_usage_prompt_estimate_after_tool_delta(
            base_prompt_tokens=self._prompt_exact_tokens,
            exact_prompt_tokens=self._prompt_exact_tokens,
            messages=messages,
            turn_tool_prompt_tokens=self._tool_delta_tokens,
        )
        self._prompt_estimate_tokens = usage["last_prompt_tokens"]
        self._tool_delta_tokens = usage["turn_tool_prompt_tokens"]
        return self._prompt_estimate_tokens

    def record_tool_start(self, tool_call_id, name, args) -> bool:
        if not tool_call_id or tool_call_id in self._seen_tool_ids:
            return False
        self._seen_tool_ids.add(tool_call_id)
        self.add_tool_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": str(name or ""),
                                "arguments": self._api.json.dumps(
                                    args if isinstance(args, dict) else {},
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            },
                        }
                    ],
                }
            ]
        )
        return True

    def record_tool_complete(self, tool_call_id, name, function_result) -> bool:
        if not tool_call_id:
            return False
        self.add_tool_messages(
            [
                {
                    "role": "tool",
                    "name": str(name or ""),
                    "tool_call_id": tool_call_id,
                    "content": self._api._tool_result_snippet(function_result),
                }
            ]
        )
        return True

    def _resolved_context_length(self, compressor, session_obj) -> int:
        """Resolve one trustworthy per-model context cap for this stream."""
        cached_length = getattr(compressor, "context_length", 0) or 0
        if self._real_context_length is not None:
            return self._real_context_length or cached_length

        resolved = 0
        agent = self._agent_getter()
        try:
            model = str(getattr(agent, "model", "") or "").strip()
            provider = str(getattr(agent, "provider", "") or "").strip()
            base_url = str(getattr(agent, "base_url", "") or "").strip()
            api_key = getattr(agent, "api_key", "") or ""
            if model:
                lookup_inputs = self._api._context_length_lookup_inputs_for_model
                accept_refresh = self._api._should_accept_session_context_length_refresh
                from agent.model_metadata import get_model_context_length

                try:
                    from api.config import get_config_for_profile_home
                    from api.profiles import get_hermes_home_for_profile

                    profile_home = get_hermes_home_for_profile(
                        getattr(session_obj, "profile", None)
                    )
                    config = get_config_for_profile_home(profile_home)
                except Exception:
                    from api.config import get_config

                    config = get_config()
                inputs = lookup_inputs(
                    model,
                    provider,
                    base_url=base_url,
                    api_key=api_key,
                    cfg=config if isinstance(config, dict) else {},
                )
                real_length = (
                    get_model_context_length(
                        model,
                        inputs.base_url,
                        api_key=inputs.api_key,
                        config_context_length=inputs.config_context_length,
                        provider=inputs.provider or provider or "",
                        custom_providers=inputs.custom_providers,
                    )
                    or 0
                )
                if (
                    real_length
                    and real_length != cached_length
                    and accept_refresh(cached_length, real_length)
                ):
                    resolved = real_length
        except TypeError:
            try:
                accept_refresh = self._api._should_accept_session_context_length_refresh
                from agent.model_metadata import get_model_context_length

                model = str(getattr(agent, "model", "") or "").strip()
                base_url = str(getattr(agent, "base_url", "") or "").strip()
                real_length = get_model_context_length(model, base_url) or 0
                if (
                    real_length
                    and real_length != cached_length
                    and accept_refresh(cached_length, real_length)
                ):
                    resolved = real_length
            except Exception:
                pass
        except Exception:
            pass
        self._real_context_length = resolved
        return resolved or cached_length

    def snapshot(self) -> dict[str, Any]:
        """Return the live usage projection without mutating durable state."""
        usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_hit_percent": None,
            "context_length": 0,
            "threshold_tokens": 0,
            "last_prompt_tokens": 0,
            "post_compression_context_tokens_estimate": None,
        }
        session_obj = self._session()
        agent = self._agent_getter()
        if agent is not None:
            for source, target in (
                ("session_prompt_tokens", "input_tokens"),
                ("session_completion_tokens", "output_tokens"),
                ("session_estimated_cost_usd", "estimated_cost"),
                ("session_cache_read_tokens", "cache_read_tokens"),
                ("session_cache_write_tokens", "cache_write_tokens"),
            ):
                try:
                    usage[target] = getattr(agent, source, 0) or 0
                except Exception:
                    pass
            try:
                compressor = getattr(agent, "context_compressor", None)
                if compressor:
                    cached_length = getattr(compressor, "context_length", 0) or 0
                    context_length = self._resolved_context_length(
                        compressor, session_obj
                    )
                    threshold = getattr(compressor, "threshold_tokens", 0) or 0
                    if (
                        context_length != cached_length
                        and cached_length > 0
                        and threshold > 0
                    ):
                        threshold = int(threshold * context_length / cached_length)
                    usage["context_length"] = context_length
                    usage["threshold_tokens"] = threshold
                    usage["last_prompt_tokens"] = (
                        getattr(compressor, "last_prompt_tokens", 0) or 0
                    )
            except Exception:
                pass

        if session_obj is not None:
            for field in (
                "input_tokens",
                "output_tokens",
                "estimated_cost",
                "cache_read_tokens",
                "cache_write_tokens",
                "context_length",
                "threshold_tokens",
                "last_prompt_tokens",
            ):
                if not usage.get(field):
                    try:
                        usage[field] = getattr(session_obj, field, 0) or 0
                    except Exception:
                        pass
            estimate = getattr(
                session_obj, "post_compression_context_tokens_estimate", None
            )
            if isinstance(estimate, int) and estimate > 0:
                usage["post_compression_context_tokens_estimate"] = estimate

        real_prompt_tokens = int(usage.get("last_prompt_tokens") or 0)
        usage["cache_hit_percent"] = self._api.prompt_cache_hit_percent(
            usage.get("cache_read_tokens") or 0,
            usage.get("input_tokens") or 0,
        )
        if real_prompt_tokens and real_prompt_tokens != self._prompt_exact_tokens:
            self._prompt_exact_tokens = real_prompt_tokens
            self._prompt_estimate_tokens = real_prompt_tokens
            self._tool_delta_tokens = 0
        elif self._prompt_estimate_tokens > real_prompt_tokens:
            usage["last_prompt_tokens"] = self._prompt_estimate_tokens
        return usage
