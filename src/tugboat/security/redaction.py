from __future__ import annotations

from typing import Any

from tugboat.security.secrets import SECRET_PATTERNS


def redact_text(text: str) -> str:
    redacted = text
    for kind, pattern in SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)
    return redacted


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(item) for item in payload)
    if isinstance(payload, dict):
        return {key: redact_payload(value) for key, value in payload.items()}
    return payload
