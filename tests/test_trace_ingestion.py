from __future__ import annotations

import json
from pathlib import Path

from tugboat.traces.ingest import ingest_jsonl_trace


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
