from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from tugboat.security.secrets import scan_text
from tugboat.traces.schema import CanonicalEpisode, TraceEvent


@dataclass(frozen=True)
class TraceThreatFinding:
    code: str
    severity: str
    evidence_id: str
    message: str
    source_trust: str

    def to_json(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "evidence_id": self.evidence_id,
            "message": self.message,
            "source_trust": self.source_trust,
        }


_PROMPT_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore codex.md",
    "ignore agents.md",
    "disable approval",
    "disable approvals",
    "bypass policy",
    "system override",
)

_CONFLICTING_REQUEST_MARKERS = (
    "skip tests",
    "do not run tests",
    "disable approval",
    "disable approvals",
    "bypass policy",
)

_SUCCESS_MARKERS = (
    "all tests passed",
    "tests passed",
    "success",
    "completed successfully",
)


def detect_trace_threats(episode: CanonicalEpisode) -> tuple[TraceThreatFinding, ...]:
    findings: list[TraceThreatFinding] = []
    events = tuple(_episode_events(episode))

    for event in events:
        text = _event_text(event)
        if not text:
            continue
        if scan_text(f"trace:{event.evidence_id}", text):
            findings.append(
                TraceThreatFinding(
                    code="secret_trace_content",
                    severity="critical",
                    evidence_id=event.evidence_id,
                    message="Trace content contains material matching a secret pattern.",
                    source_trust=event.source_trust,
                )
            )
        normalized = text.lower()
        if any(marker in normalized for marker in _PROMPT_INJECTION_MARKERS):
            code = (
                "poisoned_command_output"
                if event.event_type == "tool_result"
                else "prompt_injection_attempt"
            )
            findings.append(
                TraceThreatFinding(
                    code=code,
                    severity="high",
                    evidence_id=event.evidence_id,
                    message="Trace content attempts to override instruction or policy authority.",
                    source_trust=event.source_trust,
                )
            )
        if event.event_type == "user_request" and any(
            marker in normalized for marker in _CONFLICTING_REQUEST_MARKERS
        ):
            findings.append(
                TraceThreatFinding(
                    code="conflicting_instruction_request",
                    severity="medium",
                    evidence_id=event.evidence_id,
                    message="User request conflicts with protected harness obligations.",
                    source_trust=event.source_trust,
                )
            )

    if _has_failed_tool_result(episode) and episode.final_answer:
        final_answer = episode.final_answer.lower()
        if any(marker in final_answer for marker in _SUCCESS_MARKERS):
            final_event = _last_event(events, "final_answer")
            findings.append(
                TraceThreatFinding(
                    code="forged_success_claim",
                    severity="high",
                    evidence_id=final_event.evidence_id if final_event else "final_answer",
                    message="Final answer claims success despite failed tool evidence.",
                    source_trust=final_event.source_trust if final_event else "agent",
                )
            )

    return tuple(findings)


def _episode_events(episode: CanonicalEpisode) -> Iterable[TraceEvent]:
    yield from episode.request_events
    yield from episode.tool_calls
    yield from episode.command_outputs
    yield from episode.diffs
    yield from episode.test_results
    yield from episode.policy_events
    yield from episode.user_corrections
    yield from episode.subagent_reports
    yield from episode.final_answer_events


def _event_text(event: TraceEvent) -> str:
    payload = event.payload
    values = [
        payload.get("content"),
        payload.get("text"),
        payload.get("message"),
        payload.get("summary"),
        payload.get("output"),
    ]
    return "\n".join(str(value) for value in values if value is not None)


def _has_failed_tool_result(episode: CanonicalEpisode) -> bool:
    return any(
        event.payload.get("exit_code") not in {None, 0}
        or event.payload.get("passed") is False
        or str(event.payload.get("status", "")).lower() in {"failed", "fail", "failure"}
        for event in (*episode.command_outputs, *episode.test_results)
    )


def _last_event(events: tuple[TraceEvent, ...], event_type: str) -> TraceEvent | None:
    for event in reversed(events):
        if event.event_type == event_type:
            return event
    return None
