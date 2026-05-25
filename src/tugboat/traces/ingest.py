from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tugboat.traces.schema import CanonicalEpisode, TraceBundle, TraceEvent


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


def ingest_jsonl_trace_as_episode(path: Path) -> CanonicalEpisode:
    bundle = ingest_jsonl_trace(path)
    return canonical_episode_from_bundle(bundle)


def canonical_episode_from_bundle(bundle: TraceBundle) -> CanonicalEpisode:
    request = _first_text(bundle.events, "user_request")
    final_answer = _last_text(bundle.events, "final_answer")
    verifier_scores = {
        str(event.payload.get("name", "default")): float(event.payload["score"])
        for event in bundle.events
        if event.event_type == "verifier_score" and "score" in event.payload
    }
    return CanonicalEpisode(
        trace_path=bundle.trace_path,
        request=request,
        instruction_snapshot=tuple(
            event.payload for event in bundle.events if event.event_type == "instruction_snapshot"
        ),
        tool_calls=_events_of_type(bundle.events, "tool_call"),
        command_outputs=_events_of_type(bundle.events, "tool_result"),
        diffs=_events_of_type(bundle.events, "diff"),
        test_results=_events_of_type(bundle.events, "test_result"),
        policy_events=tuple(
            event
            for event in bundle.events
            if event.event_type in {"policy_violation", "policy_denial", "policy_failure"}
        ),
        user_corrections=_events_of_type(bundle.events, "user_correction"),
        subagent_reports=_events_of_type(bundle.events, "subagent_report"),
        final_answer=final_answer,
        outcome_labels=tuple(
            str(event.payload["label"])
            for event in bundle.events
            if event.event_type == "outcome_label" and "label" in event.payload
        ),
        verifier_scores=verifier_scores,
    )


def _events_of_type(events: tuple[TraceEvent, ...], event_type: str) -> tuple[TraceEvent, ...]:
    return tuple(event for event in events if event.event_type == event_type)


def _first_text(events: tuple[TraceEvent, ...], event_type: str) -> str | None:
    for event in events:
        if event.event_type == event_type:
            return _payload_text(event.payload)
    return None


def _last_text(events: tuple[TraceEvent, ...], event_type: str) -> str | None:
    found = None
    for event in events:
        if event.event_type == event_type:
            found = _payload_text(event.payload)
    return found


def _payload_text(payload: dict[str, Any]) -> str | None:
    for key in ("content", "text", "summary"):
        if key in payload:
            return str(payload[key])
    return None
