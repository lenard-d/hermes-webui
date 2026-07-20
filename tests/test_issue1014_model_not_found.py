"""
Tests for issue #1014 — model-not-found error classification.

Covers:
  1. streaming.py: 404/model-not-found errors detected and classified as 'model_not_found'
  2. streaming.py: HTML tags stripped from provider error messages before classification
  3. static/messages.js: apperror handler has model_not_found branch
  4. static/i18n.js: model_not_found_label key present in all locales
  5. streaming.py: model_not_found checked after auth but before generic error
"""
from tests.frontend_asset_contract import family_source

import pathlib
import re

from api.runs.provider_errors import _classify_provider_error
from api.runs.local_failures import _sanitize_provider_exception

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ── 1. streaming.py: model-not-found error detection ─────────────────────────

class TestStreamingModelNotFoundDetection:
    """streaming.py must classify 404/model-not-found errors as model_not_found."""

    def test_model_not_found_type_defined_in_streaming(self):
        """'model_not_found' type must be emitted for 404 errors."""
        assert _classify_provider_error("404")['type'] == "model_not_found"

    def test_structured_404_status_is_classified(self):
        """Structured provider status also drives not-found classification."""
        result = _classify_provider_error(
            "request failed",
            {"status_code": 404, "message": "missing model"},
        )
        assert result['type'] == "model_not_found"

    def test_not_found_detects_404(self):
        """'404' must be part of the model-not-found detection logic."""
        assert _classify_provider_error("provider returned 404")['type'] == "model_not_found"

    def test_not_found_detects_not_found_string(self):
        """'not found' must be part of the detection logic."""
        assert _classify_provider_error("model not found")['type'] == "model_not_found"

    def test_not_found_detects_does_not_exist(self):
        """'does not exist' must be part of the detection logic."""
        assert _classify_provider_error("model does not exist")['type'] == "model_not_found"

    def test_not_found_detects_invalid_model(self):
        """'invalid model' must be part of the detection logic."""
        assert _classify_provider_error("invalid model")['type'] == "model_not_found"

    def test_not_found_hint_mentions_settings(self):
        """The model_not_found hint must mention Settings or hermes model."""
        hint = _classify_provider_error("model not found")['hint']
        assert "Settings" in hint or "hermes model" in hint, (
            "model_not_found hint must mention Settings or hermes model command"
        )

    def test_not_found_check_order_after_auth(self):
        """model_not_found must be checked after auth_mismatch (auth first)."""
        result = _classify_provider_error("401 unauthorized: model not found")
        assert result['type'] == "auth_mismatch", (
            "auth_mismatch should be checked before model_not_found — "
            "auth errors must not be mistaken for not-found errors"
        )


# ── 2. streaming.py: HTML sanitization ───────────────────────────────────────

class TestStreamingHtmlSanitization:
    """Provider error messages containing HTML must be stripped."""

    def test_html_strip_before_classification(self):
        """HTML tags must be stripped before error classification."""
        sanitized = _sanitize_provider_exception(
            "<html><body>invalid <strong>model</strong></body></html>"
        )

        assert sanitized == "invalid model"
        assert _classify_provider_error(sanitized)["type"] == "model_not_found"

    def test_whitespace_normalization(self):
        """Stripped HTML must have whitespace collapsed."""
        assert _sanitize_provider_exception(
            "  request\n\t<div>failed</div>   for model  "
        ) == "request failed for model"


# ── 3. static/messages.js: apperror handler ──────────────────────────────────

class TestApperrorModelNotFound:
    """messages.js apperror handler must handle model_not_found type."""

    def test_model_not_found_type_handled(self):
        """apperror handler must check for type='model_not_found'."""
        src = family_source("messages")
        assert "model_not_found" in src, (
            "model_not_found type not handled in messages.js apperror handler"
        )

    def test_model_not_found_label(self):
        """'Model not found' label must appear in the error handling."""
        src = family_source("messages")
        assert "Model not found" in src, (
            "'Model not found' label not found in messages.js"
        )

    def test_is_model_not_found_variable(self):
        """isModelNotFound variable must be defined."""
        src = family_source("messages")
        assert "isModelNotFound" in src, (
            "isModelNotFound variable not found in messages.js apperror handler"
        )


# ── 4. static/i18n.js: all locales ───────────────────────────────────────────

class TestI18nModelNotFound:
    """All locales must have model_not_found_label."""

    REQUIRED_KEY = "model_not_found_label"

    def _locale_names(self, src: str) -> list:
        return re.findall(r"api\.registerLocale\(['\"]([A-Za-z0-9_-]+)['\"],\s*\{", src)

    def _count_key(self, src: str, key: str) -> int:
        return len(re.findall(r'\b' + re.escape(key) + r'\b', src))

    def test_all_locales_have_model_not_found_label(self):
        """model_not_found_label must appear in all locales."""
        src = family_source("i18n")
        locale_count = len(self._locale_names(src))
        count = self._count_key(src, self.REQUIRED_KEY)
        assert count >= locale_count, (
            f"model_not_found_label found {count} times, expected >= {locale_count} "
            f"(one per locale)"
        )

    def test_english_label_is_plain_string(self):
        """English model_not_found_label must be a plain string, not a function."""
        src = family_source("i18n")
        en_start = src.index("api.registerLocale('en', {")
        next_locale = src.index("api.registerLocale('", en_start + 1)
        en_block = src[en_start:next_locale]
        assert self.REQUIRED_KEY in en_block, "Key not in en block"
        idx = en_block.find(self.REQUIRED_KEY)
        line = en_block[idx:idx + 200]
        assert "=>" not in line, (
            "model_not_found_label should be a plain string, not an arrow function"
        )
