from __future__ import annotations

from tugboat.security.redaction import redact_payload, redact_text


def test_redact_text_replaces_secret_values_but_preserves_context():
    text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx used in trace"

    redacted = redact_text(text)

    assert redacted == "OPENAI_API_KEY=[REDACTED:openai_api_key] used in trace"


def test_redact_payload_recurses_through_nested_values():
    payload = {
        "type": "tool_result",
        "output": "token ghp_abcdefghijklmnopqrstuvwx",
        "nested": {"lines": ["safe", "sk-abcdefghijklmnopqrstuvwx"]},
    }

    redacted = redact_payload(payload)

    assert redacted == {
        "type": "tool_result",
        "output": "token [REDACTED:ghp_token]",
        "nested": {"lines": ["safe", "[REDACTED:openai_api_key]"]},
    }
