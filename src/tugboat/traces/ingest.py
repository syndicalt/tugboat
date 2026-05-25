from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tugboat.traces.schema import TraceBundle, TraceEvent


TRUST_BY_EVENT_TYPE = {
    "user_request": "user",
    "user_correction": "user",
    "tool_call": "tool",
    "tool_result": "tool",
    "diff": "artifact",
    "test_result": "artifact",
    "final_answer": "agent",
}


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _evidence_id(line_number: int, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        f"{line_number}\n{_canonical_payload(payload)}".encode("utf-8")
    ).hexdigest()
    return f"ev_{digest[:16]}"


def ingest_jsonl_trace(path: Path) -> TraceBundle:
    events: list[TraceEvent] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"trace line {line_number} must be a JSON object")
            event_type = str(payload.get("type", "unknown"))
            events.append(
                TraceEvent(
                    evidence_id=_evidence_id(line_number, payload),
                    event_type=event_type,
                    source_trust=TRUST_BY_EVENT_TYPE.get(event_type, "untrusted"),
                    line_number=line_number,
                    payload=payload,
                )
            )
    return TraceBundle(trace_path=path, events=tuple(events))
