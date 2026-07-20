"""
Tests for issue #1014 — model-not-found error classification.

Covers:
  1. streaming.py: 404/model-not-found errors detected and classified as 'model_not_found'
  2. streaming.py: HTML tags stripped from provider error messages before classification
  3. static/messages.js: apperror handler has model_not_found branch
  4. static/i18n.js: model_not_found_label key present in all locales
  5. streaming.py: model_not_found checked after auth but before generic error
"""
import pathlib
import re

from api.streaming import _classify_provider_error

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
        src = _read("api/streaming.py")
        # Find the HTML sanitization block in the exception handler
        # It should appear before _exc_lower = err_str.lower()
        sanitize_idx = src.find("re.sub(r'<[^>]+>'")
        exc_lower_idx = src.find("_exc_lower = err_str.lower()")
        assert sanitize_idx != -1, (
            "HTML tag stripping (re.sub) not found in streaming.py exception handler"
        )
        assert exc_lower_idx != -1, "_exc_lower not found"
        assert sanitize_idx < exc_lower_idx, (
            "HTML sanitization must happen before error classification"
        )

    def test_whitespace_normalization(self):
        """Stripped HTML must have whitespace collapsed."""
        src = _read("api/streaming.py")
        sanitize_idx = src.find("re.sub(r'<[^>]+>'")
        block = src[sanitize_idx:sanitize_idx + 300]
        assert r"\s+" in block, (
            "Whitespace normalization (\\s+) not found after HTML strip"
        )


# ── 3. static/messages.js: apperror handler ──────────────────────────────────

class TestApperrorModelNotFound:
    """messages.js apperror handler must handle model_not_found type."""

    def test_model_not_found_type_handled(self):
        """apperror handler must check for type='model_not_found'."""
        src = _read("static/messages.js")
        assert "model_not_found" in src, (
            "model_not_found type not handled in messages.js apperror handler"
        )

    def test_model_not_found_label(self):
        """'Model not found' label must appear in the error handling."""
        src = _read("static/messages.js")
        assert "Model not found" in src, (
            "'Model not found' label not found in messages.js"
        )

    def test_is_model_not_found_variable(self):
        """isModelNotFound variable must be defined."""
        src = _read("static/messages.js")
        assert "isModelNotFound" in src, (
            "isModelNotFound variable not found in messages.js apperror handler"
        )


# ── 4. static/i18n.js: all locales ───────────────────────────────────────────

class TestI18nModelNotFound:
    """All locales must have model_not_found_label."""

    REQUIRED_KEY = "model_not_found_label"

    def _locale_names(self, src: str) -> list:
        pattern = re.compile(
            r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
            re.MULTILINE,
        )
        names = []
        for match in pattern.finditer(src):
            names.append(match.group("quoted") or match.group("plain"))
        return names

    def _count_key(self, src: str, key: str) -> int:
        return len(re.findall(r'\b' + re.escape(key) + r'\b', src))

    def test_all_locales_have_model_not_found_label(self):
        """model_not_found_label must appear in all locales."""
        src = _read("static/i18n.js")
        locale_count = len(self._locale_names(src))
        count = self._count_key(src, self.REQUIRED_KEY)
        assert count >= locale_count, (
            f"model_not_found_label found {count} times, expected >= {locale_count} "
            f"(one per locale)"
        )

    def test_english_label_is_plain_string(self):
        """English model_not_found_label must be a plain string, not a function."""
        src = _read("static/i18n.js")
        en_start = src.find("\n  en: {")
        es_start = src.find("\n  es: {")
        en_block = src[en_start:es_start]
        assert self.REQUIRED_KEY in en_block, "Key not in en block"
        idx = en_block.find(self.REQUIRED_KEY)
        line = en_block[idx:idx + 200]
        assert "=>" not in line, (
            "model_not_found_label should be a plain string, not an arrow function"
        )
