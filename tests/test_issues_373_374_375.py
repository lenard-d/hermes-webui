"""
Tests for issues #373, #374, and #375.

#373: Chat silently swallows errors — no feedback when agent fails to respond
#374: Remove stale OpenAI models from default list (gpt-4o, o3)
#375: Model dropdown should fetch live models from provider
"""
import pathlib
from collections import defaultdict
from contextlib import nullcontext
from types import SimpleNamespace

from api.config.static_catalog import FALLBACK_MODELS, PROVIDER_MODELS
from api.runs.local_failures import LocalFailureContext, LocalFailureOwner
from api.runs.provider_errors import _classify_provider_error
from tests.frontend_asset_contract import family_source

REPO = pathlib.Path(__file__).parent.parent
CONFIG_PY    = (REPO / "api" / "config" / "static_catalog.py").read_text(encoding="utf-8")
LIVE_MODELS_PY = (REPO / "api" / "routes_parts" / "live_models.py").read_text(encoding="utf-8")
UI_JS        = family_source("ui")


# ── Issue #373: Silent error detection ──────────────────────────────────────

class TestSilentErrorDetection:
    """A local turn without a final answer must settle as a visible error."""

    @staticmethod
    def _failure_owner(*, pending_message="Please respond"):
        events = []
        session = SimpleNamespace(
            session_id="silent-turn",
            messages=[],
            context_messages=[],
            tool_calls=[],
            active_stream_id="silent-stream",
            pending_user_message=pending_message,
            pending_attachments=[],
            pending_started_at=123,
            pending_user_source="webui",
            save=lambda: None,
        )
        context = LocalFailureContext(
            session=session,
            session_id=session.session_id,
            stream_id="silent-stream",
            message_text=pending_message,
            pending_source="webui",
            route_model="test-model",
            route_provider="test-provider",
            ephemeral=True,
            cancel_event=SimpleNamespace(is_set=lambda: False),
            checkpoint=SimpleNamespace(close=lambda: None),
            session_lock=None,
            publish=lambda event, payload: events.append((event, payload)),
            classify=_classify_provider_error,
            session_payload=lambda *_args, **_kwargs: {},
            credential_self_heal=lambda *_args, **_kwargs: None,
            prepared_agent=SimpleNamespace(
                provider="test-provider", base_url=None, api_key=None
            ),
            conversation=None,
            event_translator=SimpleNamespace(token_sent=False),
            previous_messages=[],
            previous_context_messages=[],
            logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        )
        return LocalFailureOwner(context), session, events

    @staticmethod
    def _inspect(owner, result, *, agent_error=None):
        return owner.inspect_terminal_result(
            result,
            agent=SimpleNamespace(_last_error=agent_error),
            tool_limit_reached=False,
            captured_terminal_error=[None],
            compression_origin_id="silent-turn",
            compression_continuation_id=None,
        )

    def test_no_final_answer_is_handled_as_terminal_failure(self):
        """A result that merely echoes the prompt cannot complete the turn."""
        owner, _session, _events = self._failure_owner()

        outcome = self._inspect(
            owner, {"messages": [{"role": "user", "content": "Please respond"}]}
        )

        assert outcome.handled is True

    def test_silent_terminal_failure_emits_no_response_event(self):
        """A provider result with no final content reports the specific terminal state."""
        owner, _session, events = self._failure_owner()

        self._inspect(owner, {"messages": []})

        assert [event for event, _payload in events] == ["apperror"]
        payload = events[0][1]
        assert payload["type"] == "no_response"
        assert payload["hint"]
        assert payload["session_id"] == "silent-turn"

    def test_silent_terminal_failure_never_emits_normal_completion(self):
        """The failure owner exposes only an error event, never a done event."""
        owner, _session, events = self._failure_owner()

        self._inspect(owner, {"messages": []})

        assert [event for event, _payload in events] == ["apperror"]

    def test_auth_error_takes_precedence_over_no_final_answer(self):
        """Structured provider auth failures retain their actionable classification."""
        owner, _session, events = self._failure_owner()

        outcome = self._inspect(
            owner,
            {
                "messages": [],
                "error": {
                    "type": "authentication_error",
                    "status_code": 401,
                    "message": "Invalid API key",
                },
            },
        )

        assert outcome.handled is True
        assert events[-1][1]["type"] == "auth_mismatch"

    def test_silent_failure_persists_visible_error_turn(self):
        """The durable transcript contains an error turn instead of swallowing the prompt."""
        owner, session, _events = self._failure_owner()

        self._inspect(owner, {"messages": []})

        assert session.messages[-1]["role"] == "assistant"
        assert session.messages[-1]["_error"] is True

    def test_silent_failure_materializes_the_pending_prompt_before_error(self):
        """Reloading a failed turn retains the user's prompt before its error marker."""
        owner, session, _events = self._failure_owner(pending_message="Keep this prompt")

        self._inspect(owner, {"messages": []})

        assert [message["role"] for message in session.messages] == ["user", "assistant"]
        assert session.messages[0]["content"] == "Keep this prompt"
        assert session.messages[0]["_recovered"] is True

    def test_silent_failure_error_turn_has_a_distinct_no_response_label(self):
        """The persisted terminal message gives users a clear no-response explanation."""
        owner, session, _events = self._failure_owner()

        self._inspect(owner, {"messages": []})

        assert session.messages[-1]["content"].startswith(
            "**No response from provider:**"
        )


# ── Issue #374: Stale model list cleanup ─────────────────────────────────────

class TestStaleModelListCleanup:
    """gpt-4o and o3 must be removed from the primary OpenAI model lists."""

    def test_gpt4o_removed_from_fallback_models(self):
        """_FALLBACK_MODELS must not contain gpt-4o (issue #374)."""
        fallback_block_start = CONFIG_PY.find("FALLBACK_MODELS = [")
        fallback_block_end = CONFIG_PY.find("]", fallback_block_start)
        fallback_block = CONFIG_PY[fallback_block_start:fallback_block_end]
        assert "gpt-4o" not in fallback_block, (
            "_FALLBACK_MODELS still contains gpt-4o — remove it per issue #374"
        )

    def test_o3_removed_from_fallback_models(self):
        """_FALLBACK_MODELS must not contain o3 (issue #374)."""
        fallback_block_start = CONFIG_PY.find("FALLBACK_MODELS = [")
        fallback_block_end = CONFIG_PY.find("]", fallback_block_start)
        fallback_block = CONFIG_PY[fallback_block_start:fallback_block_end]
        assert '"o3"' not in fallback_block and "'o3'" not in fallback_block, (
            "_FALLBACK_MODELS still contains o3 — remove it per issue #374"
        )

    def test_gpt4o_removed_from_provider_models_openai(self):
        """_PROVIDER_MODELS['openai'] must not contain gpt-4o (issue #374)."""
        openai_start = CONFIG_PY.find('"openai": [')
        openai_end = CONFIG_PY.find("],", openai_start)
        openai_block = CONFIG_PY[openai_start:openai_end]
        assert "gpt-4o" not in openai_block, (
            "_PROVIDER_MODELS['openai'] still contains gpt-4o — remove per issue #374"
        )

    def test_o3_removed_from_provider_models_openai(self):
        """_PROVIDER_MODELS['openai'] must not contain o3 (issue #374)."""
        openai_start = CONFIG_PY.find('"openai": [')
        openai_end = CONFIG_PY.find("],", openai_start)
        openai_block = CONFIG_PY[openai_start:openai_end]
        assert '"o3"' not in openai_block and "'o3'" not in openai_block, (
            "_PROVIDER_MODELS['openai'] still contains o3 — remove per issue #374"
        )

    def test_fallback_still_has_gpt54_mini(self):
        """_FALLBACK_MODELS must still contain gpt-5.4-mini (not over-trimmed)."""
        assert "gpt-5.4-mini" in CONFIG_PY, (
            "_FALLBACK_MODELS must keep gpt-5.4-mini as primary OpenAI model (#374)"
        )

    def test_fallback_has_gpt54(self):
        """_FALLBACK_MODELS must contain gpt-5.4-mini as the primary OpenAI option."""
        from api.config import _FALLBACK_MODELS
        ids = [m["id"] for m in _FALLBACK_MODELS]
        assert any("gpt-5.4-mini" in mid for mid in ids), (
            "_FALLBACK_MODELS must include gpt-5.4-mini as the primary OpenAI option"
        )

    def test_copilot_list_unchanged(self):
        """Copilot provider model list should still include gpt-4o (it's a valid Copilot model)."""
        copilot_start = CONFIG_PY.find('"copilot": [')
        copilot_end = CONFIG_PY.find("],", copilot_start)
        if copilot_start == -1:
            return  # No copilot list — that's fine
        copilot_block = CONFIG_PY[copilot_start:copilot_end]
        assert "gpt-4o" in copilot_block, (
            "Copilot provider model list should keep gpt-4o (it's available via Copilot) (#374)"
        )


# ── Issue #375: Live model fetching ─────────────────────────────────────────

class TestLiveModelFetching:
    """Backend and frontend must support live model fetching from provider APIs."""

    def test_live_models_endpoint_dispatches_to_live_model_owner(self, monkeypatch):
        """The configuration route delegates /api/models/live to its domain owner."""
        import api.profiles as profiles
        from api.http.routes import configuration_queries

        marker = object()
        seen = []
        monkeypatch.setattr(
            profiles,
            "profile_env_for_active_request",
            lambda *_args, **_kwargs: nullcontext(),
        )
        def unused(*_args, **_kwargs):
            return None

        context = defaultdict(lambda: unused)
        context.update(
            {
                "_handle_live_models": lambda handler, parsed: seen.append(
                    (handler, parsed.path)
                )
                or marker,
            }
        )
        handler = object()

        assert configuration_queries.handle_get(
            handler,
            SimpleNamespace(path="/api/models/live", query="provider=nous"),
            context,
        ) is marker
        assert seen == [(handler, "/api/models/live")]

    def test_live_models_handler_function_exists(self):
        """The live-model owner must define _handle_live_models() (#375)."""
        assert "def _handle_live_models(" in LIVE_MODELS_PY, (
            "the live-model owner must define _handle_live_models() (#375)"
        )

    def test_live_models_all_providers_handled_via_agent(self):
        """_handle_live_models must delegate to provider_model_ids() which handles all
        providers gracefully — live fetch where possible, static fallback otherwise.
        The old 'not_supported' return for Anthropic/Google is superseded: those
        providers now return live or static model lists via the agent delegate."""
        assert "provider_model_ids" in LIVE_MODELS_PY, (
            "_handle_live_models must delegate to hermes_cli.models.provider_model_ids() "
            "so all providers are handled uniformly (#375 upgrade)"
        )

    def test_frontend_has_fetch_live_models_function(self):
        """ui.js must define _fetchLiveModels() for background live model loading (#375)."""
        assert "function _fetchLiveModels(" in UI_JS or "async function _fetchLiveModels(" in UI_JS, (
            "ui.js must define _fetchLiveModels() function (#375)"
        )

    def test_frontend_live_models_cache_exists(self):
        """ui.js must cache live model responses to avoid redundant API calls (#375)."""
        assert "_liveModelCache" in UI_JS, (
            "ui.js must use _liveModelCache to avoid re-fetching on every dropdown open (#375)"
        )

    def test_frontend_calls_live_models_after_static_load(self):
        """populateModelDropdown must call _fetchLiveModels after rendering the static list (#375)."""
        assert "_fetchLiveModels" in UI_JS, (
            "populateModelDropdown must call _fetchLiveModels for background update (#375)"
        )

    def test_frontend_live_fetch_only_adds_new_models(self):
        """_fetchLiveModels must not duplicate models already in the static list (#375)."""
        assert "existingIds" in UI_JS, (
            "_fetchLiveModels must track existing model IDs to avoid duplicates (#375)"
        )

    def test_frontend_live_fetch_covers_all_providers(self):
        """_fetchLiveModels no longer skips any provider — all providers return
        live or fallback models via provider_model_ids() on the backend (#375 upgrade)."""
        # The old skip list (anthropic, google, gemini) must be gone from the guard
        skip_guard_pos = UI_JS.find("includes(provider)")
        if skip_guard_pos != -1:
            guard_line = UI_JS[max(0,skip_guard_pos-100):skip_guard_pos+50]
            assert "anthropic" not in guard_line, (
                "_fetchLiveModels must not skip anthropic — backend now handles it (#375 upgrade)"
            )

    def test_live_models_endpoint_wired_through_http_composition_root(self, monkeypatch):
        """The HTTP composition root reaches the configuration route group."""
        from api.http import router
        from api.http.context import UNHANDLED
        from api.http.routes import configuration_queries, observability_queries, public

        seen = []
        monkeypatch.setattr(public, "handle_get", lambda *_args: UNHANDLED)
        monkeypatch.setattr(
            observability_queries, "handle_get", lambda *_args: UNHANDLED
        )
        monkeypatch.setattr(
            configuration_queries,
            "handle_get",
            lambda _handler, parsed, _ctx: seen.append(parsed.path) or True,
        )
        context = {
            "_handle_extension_sidecar_proxy": lambda *_args, **_kwargs: False,
            "_guard_request_session_visibility": lambda *_args, **_kwargs: True,
        }

        assert router.handle_get(
            object(),
            SimpleNamespace(path="/api/models/live", query=""),
            context,
        ) is True
        assert seen == ["/api/models/live"]


# ── #669: Gemini model IDs must be valid for Google AI Studio endpoint ────────

class TestGeminiModelIds:
    """Gemini 3.x model IDs must be valid for the native Google AI Studio provider.

    The original code had gemini-3.1-flash-lite-preview missing from the
    dropdown. The fallback list also erroneously used gemini-3.1-pro-preview
    in some provider sections while omitting gemini-3.1-flash-lite-preview.
    All provider sections must now include the full current Gemini 3.x lineup.
    """

    VALID_GEMINI_3 = [
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
    ]

    def test_gemini_provider_models_has_3x(self):
        """_PROVIDER_MODELS['gemini'] must contain valid Gemini 3.x model IDs (#669)."""
        gemini_ids = {model["id"] for model in PROVIDER_MODELS["gemini"]}
        for mid in self.VALID_GEMINI_3:
            assert mid in gemini_ids, (
                f"_PROVIDER_MODELS['gemini'] must contain {mid!r} — "
                f"this is a valid Google AI Studio model ID (#669)"
            )

    def test_gemini_provider_models_has_flash_lite(self):
        """_PROVIDER_MODELS['gemini'] must contain gemini-3.1-flash-lite-preview (#669).

        This was the model the reporter selected from the wizard — it must appear
        in the native gemini provider model list so users can select it.
        """
        gemini_ids = {model["id"] for model in PROVIDER_MODELS["gemini"]}
        assert "gemini-3.1-flash-lite-preview" in gemini_ids, (
            "_PROVIDER_MODELS['gemini'] missing gemini-3.1-flash-lite-preview — "
            "this was the exact model the #669 reporter tried and got API_KEY_INVALID"
        )

    def test_fallback_models_has_gemini_3x(self):
        """_FALLBACK_MODELS must contain valid Gemini 3.x OpenRouter model IDs (#669)."""
        fallback_ids = {model["id"] for model in FALLBACK_MODELS}
        for mid in ("google/gemini-3.1-pro-preview", "google/gemini-3-flash-preview"):
            assert mid in fallback_ids, (
                f"_FALLBACK_MODELS must contain {mid!r} for OpenRouter Google models (#669)"
            )

    def test_gemini_provider_also_has_stable_25(self):
        """_PROVIDER_MODELS['gemini'] must retain stable Gemini 2.5 models (#669)."""
        gemini_ids = {model["id"] for model in PROVIDER_MODELS["gemini"]}
        assert "gemini-2.5-pro" in gemini_ids, (
            "_PROVIDER_MODELS['gemini'] must keep gemini-2.5-pro as a stable fallback"
        )

    def test_no_invalid_gemini_3_pro_model(self):
        """gemini-3-pro-preview must not appear — it was shut down March 9 2026 (#669)."""
        all_model_ids = {
            model["id"]
            for models in PROVIDER_MODELS.values()
            for model in models
        } | {model["id"] for model in FALLBACK_MODELS}
        assert "gemini-3-pro-preview" not in all_model_ids, (
            "gemini-3-pro-preview remains in the catalog — "
            "it was shut down March 9 2026, use gemini-3.1-pro-preview (#669)"
        )
