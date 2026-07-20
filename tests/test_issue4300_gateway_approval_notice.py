"""Regression coverage for #4300 legacy gateway approval unsupported notice."""

from tests.frontend_asset_contract import family_asset_paths, family_source

from types import SimpleNamespace
from unittest.mock import patch

MESSAGES_JS = family_source("messages")
I18N_JS = family_source("i18n")


def test_gateway_chat_emits_unsupported_notice_once_per_session():
    """The legacy transport publishes one actionable warning per session."""
    from api.runs.gateway import _publish_gateway_approval_capability_warning

    session = SimpleNamespace()
    events = []
    with patch(
        "api.runs.gateway.gateway_approval_unavailable_reason",
        return_value="unsupported",
    ):
        for _ in range(2):
            _publish_gateway_approval_capability_warning(
                session,
                base_url="http://gateway.test",
                api_key="",
                publish=lambda event, data: events.append((event, data)),
            )

    assert events == [
        (
            "warning",
            {
                "type": "approval_gateway_unsupported",
                "message": (
                    "Approvals require a newer gateway. Upgrade the connected "
                    "Hermes gateway to enable this."
                ),
            },
        )
    ]
    assert session._approval_notice_emitted is True


def test_messages_js_handles_approval_gateway_unsupported_event():
    """Verify client-side warning handler recognizes the event type."""
    assert "d.type==='approval_gateway_unsupported'" in MESSAGES_JS


def test_messages_js_references_i18n_key_for_approval_gateway_unsupported():
    """Verify the i18n key is referenced in messages.js."""
    # The key should be used in the message handling logic
    assert "approval_gateway_unsupported" in MESSAGES_JS


def test_i18n_js_has_approval_gateway_unsupported_key():
    """Verify the i18n key exists in at least the English locale (first occurrence)."""
    assert "approval_gateway_unsupported: 'Approvals require a newer gateway" in I18N_JS
    english_path = next(
        path for path in family_asset_paths("i18n") if path.name == "locale-en.js"
    )
    english = english_path.read_text(encoding="utf-8")
    assert "approval_gateway_unsupported:" in english, (
        "approval_gateway_unsupported key not found in English i18n locale"
    )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
