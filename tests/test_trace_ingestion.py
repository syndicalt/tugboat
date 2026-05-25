from __future__ import annotations

import json
from pathlib import Path

from tugboat.traces.ingest import ingest_jsonl_trace, ingest_jsonl_trace_as_episode


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows),
        encoding="utf-8",
    )


def test_ingest_jsonl_trace_assigns_stable_evidence_ids_and_trust(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    rows = [
        {"type": "user_request", "content": "Add tests"},
        {"type": "tool_call", "tool": "pytest", "args": ["-q"]},
        {"type": "tool_result", "tool": "pytest", "exit_code": 0},
        {"type": "diff", "path": "README.md", "patch": "@@"},
        {"type": "test_result", "suite": "unit", "passed": True},
        {"type": "final_answer", "content": "Done"},
        {"type": "user_correction", "content": "Use memory first"},
        {"type": "mystery", "content": "unclassified"},
    ]
    _write_jsonl(trace_path, rows)

    first = ingest_jsonl_trace(trace_path)
    second = ingest_jsonl_trace(trace_path)

    assert [event.evidence_id for event in first.events] == [
        event.evidence_id for event in second.events
    ]
    assert len({event.evidence_id for event in first.events}) == len(rows)
    assert [event.source_trust for event in first.events] == [
        "user",
        "tool",
        "tool",
        "artifact",
        "artifact",
        "agent",
        "user",
        "untrusted",
    ]
    assert all(event.evidence_id.startswith("ev_") for event in first.events)


def test_ingest_jsonl_trace_preserves_raw_payload_and_line_number(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    row = {"type": "tool_result", "tool": "pytest", "output": "2 passed"}
    _write_jsonl(trace_path, [row])

    bundle = ingest_jsonl_trace(trace_path)

    assert bundle.trace_path == trace_path
    assert bundle.events[0].line_number == 1
    assert bundle.events[0].event_type == "tool_result"
    assert bundle.events[0].payload == row


def test_ingest_jsonl_trace_builds_canonical_episode(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    rows = [
        {"type": "user_request", "content": "Fix bug"},
        {"type": "tool_call", "tool": "pytest", "args": ["-q"]},
        {"type": "tool_result", "tool": "pytest", "exit_code": 1, "output": "failed"},
        {"type": "diff", "path": "CODEX.md", "patch": "@@ +Use tests"},
        {"type": "test_result", "suite": "unit", "passed": False},
        {"type": "user_correction", "content": "You skipped regression tests"},
        {"type": "subagent_report", "agent": "reviewer", "summary": "missing test"},
        {"type": "final_answer", "content": "Fixed"},
        {"type": "outcome_label", "label": "rejected"},
        {"type": "verifier_score", "name": "governance", "score": 0.25},
    ]
    _write_jsonl(trace_path, rows)

    episode = ingest_jsonl_trace_as_episode(trace_path)

    assert episode.request == "Fix bug"
    assert episode.tool_calls[0].payload["tool"] == "pytest"
    assert episode.command_outputs[0].payload["exit_code"] == 1
    assert episode.diffs[0].payload["path"] == "CODEX.md"
    assert episode.test_results[0].payload["passed"] is False
    assert episode.user_corrections[0].payload["content"] == "You skipped regression tests"
    assert episode.subagent_reports[0].payload["agent"] == "reviewer"
    assert episode.final_answer == "Fixed"
    assert episode.outcome_labels == ("rejected",)
    assert episode.verifier_scores == {"governance": 0.25}


def test_canonical_episode_exposes_redacted_events_for_model_payloads(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    _write_jsonl(
        trace_path,
        [
            {"type": "tool_result", "output": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"},
        ],
    )

    episode = ingest_jsonl_trace_as_episode(trace_path)

    assert episode.redacted_events()[0].payload == {
        "type": "tool_result",
        "output": "OPENAI_API_KEY=[REDACTED:openai_api_key]",
    }
