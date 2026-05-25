from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tugboat.security.redaction import redact_payload


@dataclass(frozen=True)
class TraceEvent:
    evidence_id: str
    event_type: str
    source_trust: str
    line_number: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class TraceBundle:
    trace_path: Path
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class CanonicalEpisode:
    trace_path: Path
    request: str | None
    instruction_snapshot: tuple[dict[str, Any], ...]
    tool_calls: tuple[TraceEvent, ...]
    command_outputs: tuple[TraceEvent, ...]
    diffs: tuple[TraceEvent, ...]
    test_results: tuple[TraceEvent, ...]
    user_corrections: tuple[TraceEvent, ...]
    subagent_reports: tuple[TraceEvent, ...]
    final_answer: str | None
    outcome_labels: tuple[str, ...]
    verifier_scores: dict[str, float]

    def redacted_events(self) -> tuple[TraceEvent, ...]:
        events = (
            *self.tool_calls,
            *self.command_outputs,
            *self.diffs,
            *self.test_results,
            *self.user_corrections,
            *self.subagent_reports,
        )
        return tuple(
            TraceEvent(
                evidence_id=event.evidence_id,
                event_type=event.event_type,
                source_trust=event.source_trust,
                line_number=event.line_number,
                payload=redact_payload(event.payload),
            )
            for event in events
        )
