from pathlib import Path

from api.runs import provider_errors


ROOT = Path(__file__).resolve().parents[1]
TERMINAL_EVENTS_JS = (
    ROOT / "static" / "modules" / "messages" / "terminal-events.js"
).read_text(encoding="utf-8")
MESSAGE_RENDERER_JS = (
    ROOT / "static" / "modules" / "ui" / "renderer.js"
).read_text(encoding="utf-8")
ERROR_DETAILS_CSS = (
    ROOT / "static" / "style_parts" / "004-chat-workspace-responsive.css"
).read_text(encoding="utf-8")


CODEX_PLAN_LIMIT_ERROR = (
    "HTTP 429: {\"error\": {\"type\": \"usage_limit_exceeded\", "
    "\"message\": \"Plan limit reached. You've reached the limit of messages per 5 hours.\"}}"
)


def test_codex_oauth_usage_exhaustion_is_classified_as_quota():
    for err in [
        'Plan limit reached',
        'usage_limit_exceeded',
        'usage limit exceeded',
        "You've reached the limit of messages per 5 hours",
        "You've used up your usage",
        CODEX_PLAN_LIMIT_ERROR,
    ]:
        classified = provider_errors._classify_provider_error(err, Exception(err))
        assert classified['type'] == 'quota_exhausted', err
        assert classified['label'] == 'Out of credits'
        assert 'credits' in classified['hint'].lower() or 'usage' in classified['hint'].lower()


def test_silent_provider_failure_gets_specific_catch_all_error():
    classified = provider_errors._classify_provider_error('', None, silent_failure=True)

    assert classified['type'] == 'no_response'
    assert classified['label'] == 'No response from provider'
    assert 'returned no content and no error' in classified['hint']


def test_provider_error_payload_includes_bounded_redacted_details(monkeypatch):
    secret = 'sk-proj-' + ('a' * 80)
    raw_error = CODEX_PLAN_LIMIT_ERROR + ' token=' + secret

    monkeypatch.setattr(provider_errors, '_redact_text', lambda text: text.replace(secret, '[REDACTED]'))
    payload = provider_errors._provider_error_payload(raw_error, 'quota_exhausted', 'Switch providers')

    assert payload['message']
    assert secret not in payload['message']
    assert payload['details']
    assert secret not in payload['details']
    assert '[REDACTED]' in payload['details']
    assert len(payload['details']) <= 1200


def test_frontend_renders_apperror_details_in_collapsible_block():
    apperror_idx = TERMINAL_EVENTS_JS.find("source.addEventListener('apperror'")
    cancel_idx = TERMINAL_EVENTS_JS.find("source.addEventListener('cancel'", apperror_idx)
    assert apperror_idx != -1 and cancel_idx != -1
    apperror_block = TERMINAL_EVENTS_JS[apperror_idx:cancel_idx]

    assert 'd.details' in apperror_block
    assert 'provider_details:details' in apperror_block
    assert 'm.provider_details' in MESSAGE_RENDERER_JS
    assert '<details class="provider-error-details"' in MESSAGE_RENDERER_JS
    assert 'Provider details' in MESSAGE_RENDERER_JS
    assert '.provider-error-details' in ERROR_DETAILS_CSS
