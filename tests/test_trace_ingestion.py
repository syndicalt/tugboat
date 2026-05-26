from __future__ import annotations

import json
from pathlib import Path

from tugboat.audit.pipeline import _write_canonical_episode
from tugboat.db import Store
from tugboat.paths import sidecar_dir
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
        {"type": "policy_violation", "policy": "secrets", "status": "failed"},
        {"type": "subagent_report", "agent": "reviewer", "summary": "missing test"},
        {"type": "outcome_label", "label": "accepted"},
        {"type": "verifier_score", "name": "quality", "score": 0.9},
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
        "policy",
        "agent",
        "verifier",
        "verifier",
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
        {"type": "policy_violation", "policy": "secrets", "status": "failed"},
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
    assert episode.policy_events[0].payload["policy"] == "secrets"
    assert episode.user_corrections[0].payload["content"] == "You skipped regression tests"
    assert episode.subagent_reports[0].payload["agent"] == "reviewer"
    assert episode.final_answer == "Fixed"
    assert episode.outcome_label_events[0].payload["label"] == "rejected"
    assert episode.outcome_labels == ("rejected",)
    assert episode.verifier_score_events[0].payload["name"] == "governance"
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


def test_canonical_episode_redacted_events_include_instruction_snapshot(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    _write_jsonl(
        trace_path,
        [
            {
                "type": "instruction_snapshot",
                "source": "CODEX.md",
                "text": "Use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx",
            },
            {"type": "user_request", "content": "Fix bug"},
        ],
    )

    episode = ingest_jsonl_trace_as_episode(trace_path)

    assert [event.event_type for event in episode.redacted_events()] == [
        "instruction_snapshot",
        "user_request",
    ]
    assert episode.redacted_events()[0].payload == {
        "type": "instruction_snapshot",
        "source": "CODEX.md",
        "text": "Use OPENAI_API_KEY=[REDACTED:openai_api_key]",
    }


def test_canonical_episode_artifact_redacts_top_level_summary_fields(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    _write_jsonl(
        trace_path,
        [
            {"type": "user_request", "content": "Use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"},
            {"type": "final_answer", "content": "Saved sk-abcdefghijklmnopqrstuvwx"},
        ],
    )
    bundle = ingest_jsonl_trace(trace_path)
    artifact_path = tmp_path / "canonical-episode.json"

    _write_canonical_episode(bundle, artifact_path)

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["request"] == "Use OPENAI_API_KEY=[REDACTED:openai_api_key]"
    assert payload["final_answer"] == "Saved [REDACTED:openai_api_key]"


def test_canonical_episode_artifact_exposes_roadmap_event_groups(tmp_path: Path):
    trace_path = tmp_path / "episode.jsonl"
    _write_jsonl(
        trace_path,
        [
            {"type": "user_request", "content": "Fix bug"},
            {"type": "tool_call", "tool": "pytest", "args": ["-q"]},
            {
                "type": "tool_result",
                "tool": "pytest",
                "exit_code": 1,
                "output": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx",
            },
            {"type": "diff", "path": "CODEX.md", "patch": "@@ +Use tests"},
            {"type": "test_result", "suite": "unit", "passed": False},
            {"type": "user_correction", "content": "You skipped regression tests"},
            {"type": "subagent_report", "agent": "reviewer", "summary": "missing test"},
        ],
    )
    artifact_path = tmp_path / "canonical-episode.json"

    _write_canonical_episode(ingest_jsonl_trace(trace_path), artifact_path)

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["tool_calls"][0]["payload"]["tool"] == "pytest"
    assert payload["command_outputs"][0]["payload"]["output"] == (
        "OPENAI_API_KEY=[REDACTED:openai_api_key]"
    )
    assert payload["diffs"][0]["payload"]["path"] == "CODEX.md"
    assert payload["test_results"][0]["payload"]["passed"] is False
    assert payload["user_corrections"][0]["payload"]["content"] == "You skipped regression tests"
    assert payload["subagent_reports"][0]["payload"]["agent"] == "reviewer"


def test_store_records_canonical_episode_and_trace_events_with_audit_reachability(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = repo / "episode.jsonl"
    rows = [
        {"type": "user_request", "content": "Fix bug"},
        {"type": "tool_call", "tool": "pytest", "args": ["-q"]},
        {"type": "tool_result", "tool": "pytest", "exit_code": 0},
    ]
    _write_jsonl(trace_path, rows)
    bundle = ingest_jsonl_trace(trace_path)

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        episode_id = store.record_trace_episode(repo=repo, bundle=bundle)
        episode_count = store.count("episodes")
        trace_event_rows = store.connection.execute(
            """
            SELECT t.evidence_id, t.event_type, a.event_type, a.payload_json
            FROM trace_events t
            JOIN audit_events a ON a.sequence = t.audit_event_sequence
            ORDER BY t.line_number
            """
        ).fetchall()

    assert episode_id == 1
    assert episode_count == 1
    assert [row[0] for row in trace_event_rows] == [event.evidence_id for event in bundle.events]
    assert [row[1] for row in trace_event_rows] == [
        "user_request",
        "tool_call",
        "tool_result",
    ]
    assert {row[2] for row in trace_event_rows} == {"trace_event.recorded"}
    assert json.loads(trace_event_rows[0][3])["episode_id"] == episode_id
