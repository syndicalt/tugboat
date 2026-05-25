from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ScoreOutcome:
    plugin: str
    label: str
    metrics: dict[str, int]
    evidence: tuple[str, ...] = ()


def score_episode(episode: Any) -> tuple[ScoreOutcome, ...]:
    events = list(_episode_events(episode))
    outcomes: list[ScoreOutcome] = []

    for event in events:
        event_type = _event_type(event)
        evidence = (_evidence_id(event),)

        if event_type == "test_result" and _test_failed(event):
            outcomes.append(
                ScoreOutcome(
                    plugin="tests",
                    label="failed-tests",
                    metrics={"failed_tests": 1},
                    evidence=evidence,
                )
            )
        elif event_type in {"human_decision", "human_label", "human_review", "outcome_label"}:
            human_label = str(_field(event, "label") or _field(event, "status")).lower()
            if human_label in {"accepted", "accept", "approved", "approve"}:
                outcomes.append(
                    ScoreOutcome(
                        plugin="human",
                        label="human-accepted",
                        metrics={"accepted": 1},
                        evidence=evidence,
                    )
                )
            elif human_label in {"rejected", "reject", "denied", "deny"}:
                outcomes.append(
                    ScoreOutcome(
                        plugin="human",
                        label="human-rejected",
                        metrics={"rejected": 1},
                        evidence=evidence,
                    )
                )
        elif event_type == "verifier_score":
            score = _verifier_score(event)
            if score < 0.5:
                outcomes.append(
                    ScoreOutcome(
                        plugin="verifier",
                        label="verifier-failed",
                        metrics={"score_percent": int(score * 100)},
                        evidence=evidence,
                    )
                )
        elif event_type in {"agent_review", "review_finding", "agent_review_finding"}:
            severity = _review_severity(event)
            severity_score = _severity_score(severity)
            if severity_score >= 3:
                outcomes.append(
                    ScoreOutcome(
                        plugin="agent-review",
                        label="agent-review-severe",
                        metrics={"severity_score": severity_score},
                        evidence=evidence,
                    )
                )
        elif event_type in {"policy_violation", "policy_denial", "policy_failure"}:
            outcomes.append(
                ScoreOutcome(
                    plugin="policy",
                    label="policy-violation",
                    metrics={"violations": 1},
                    evidence=evidence,
                )
            )

    outcomes.extend(_score_user_correction_recurrence(events))
    return tuple(outcomes)


def _episode_events(episode: Any) -> Iterable[dict[str, Any]]:
    if all(hasattr(episode, name) for name in _CANONICAL_EVENT_GROUPS):
        raw_events = [
            event
            for group_name in _CANONICAL_EVENT_GROUPS
            for event in getattr(episode, group_name)
        ]
    elif isinstance(episode, dict) and "events" in episode:
        raw_events = episode["events"]
    elif not isinstance(episode, dict) and not _is_event_sequence(episode):
        value = _to_mapping(episode)
        raw_events = value["events"] if "events" in value else value
    else:
        raw_events = episode

    if isinstance(raw_events, dict):
        raw_events = raw_events.values()

    for event in raw_events or ():
        yield _to_mapping(event)


def _is_event_sequence(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict))


_CANONICAL_EVENT_GROUPS = (
    "tool_calls",
    "command_outputs",
    "diffs",
    "test_results",
    "policy_events",
    "user_corrections",
    "subagent_reports",
    "final_answer_events",
    "outcome_label_events",
    "verifier_score_events",
)


def _to_mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _event_type(event: dict[str, Any]) -> str:
    return str(_field(event, "type") or _field(event, "event_type") or _field(event, "kind")).lower()


def _field(event: dict[str, Any], name: str) -> Any:
    if name in event:
        return event[name]
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload.get(name)
    return None


def _evidence_id(event: dict[str, Any]) -> str:
    value = (
        _field(event, "id")
        or _field(event, "evidence_id")
        or _field(event, "source_id")
        or _field(event, "source_ref")
    )
    return str(value) if value is not None else "unknown"


def _test_failed(event: dict[str, Any]) -> bool:
    status = str(_field(event, "status") or _field(event, "outcome") or "").lower()
    return status in {"failed", "fail", "failure"} or _field(event, "passed") is False


def _review_severity(event: dict[str, Any]) -> str:
    finding = _to_mapping(_field(event, "finding"))
    return str(
        finding.get("severity")
        or _field(event, "severity")
        or _field(event, "level")
        or ""
    ).lower()


def _severity_score(severity: str) -> int:
    return {
        "info": 0,
        "low": 1,
        "medium": 2,
        "moderate": 2,
        "high": 3,
        "severe": 3,
        "critical": 4,
        "blocker": 4,
    }.get(severity, 0)


def _verifier_score(event: dict[str, Any]) -> float:
    value = _field(event, "score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _score_user_correction_recurrence(events: list[dict[str, Any]]) -> tuple[ScoreOutcome, ...]:
    correction_groups: list[tuple[set[str], list[str]]] = []
    for event in events:
        if _event_type(event) != "user_correction":
            continue
        tokens = _correction_tokens(str(_field(event, "text") or _field(event, "message") or ""))
        if not tokens:
            continue
        evidence_id = _evidence_id(event)
        for group_tokens, group_evidence in correction_groups:
            if _similar(tokens, group_tokens):
                group_tokens.update(tokens)
                group_evidence.append(evidence_id)
                break
        else:
            correction_groups.append((set(tokens), [evidence_id]))

    return tuple(
        ScoreOutcome(
            plugin="user-correction",
            label="recurring-user-correction",
            metrics={"recurrence_count": len(evidence)},
            evidence=tuple(evidence),
        )
        for _, evidence in correction_groups
        if len(evidence) > 1
    )


def _correction_tokens(text: str) -> set[str]:
    words = ("".join(char.lower() if char.isalnum() else " " for char in text)).split()
    stopwords = {"a", "an", "the", "to", "and", "or", "please", "response"}
    return {word for word in words if word not in stopwords}


def _similar(left: set[str], right: set[str]) -> bool:
    overlap = left & right
    return len(overlap) >= 3 and len(overlap) / min(len(left), len(right)) >= 0.6
