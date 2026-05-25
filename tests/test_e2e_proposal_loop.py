import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.cli import main


def test_proposal_loop_writes_review_artifacts_without_mutating_instructions(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )

    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace), "--mock-llmff-inspect"]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "governance-regression"]) == 0
    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0

    run_dirs = sorted((repo / ".sidecar" / "runs").iterdir())
    assert run_dirs
    run_dir = run_dirs[-1]
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "instruction-snapshot").is_dir()
    assert (run_dir / "llmff-inspect.json").exists()
    manifest_dir = repo / ".sidecar" / "manifests"
    assert sorted(path.name for path in manifest_dir.glob("*.yaml")) == [
        "acceptance-summary.yaml",
        "drift-detect.yaml",
        "episode-audit.yaml",
        "instruction-index.yaml",
        "patch-eval.yaml",
        "patch-propose.yaml",
    ]
    inspect = json.loads((run_dir / "llmff-inspect.json").read_text(encoding="utf-8"))
    assert inspect["manifest_path"].endswith(".sidecar/manifests/episode-audit.yaml")
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "candidate.json").exists()
    assert (run_dir / "policy-gate.json").exists()
    assert (run_dir / "eval-report.json").exists()
    assert (run_dir / "decision.json").exists()
    assert (run_dir / "report.md").exists()
    assert json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8")) == {
        "allowed": True,
        "reasons": [],
    }
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert audit["schema_version"] == 1
    assert candidate["schema_version"] == 1
    assert eval_report["schema_version"] == 1
    assert decision["schema_version"] == 1
    assert candidate["audit_id"] == audit["audit_id"]
    assert eval_report["candidate_id"] == candidate["candidate_id"]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == 2
        snapshot = connection.execute(
            """
            SELECT path, artifact_path, content_hash, audit_event_sequence
            FROM instruction_snapshots
            """
        ).fetchone()
        graph = connection.execute(
            """
            SELECT artifact_path, graph_hash, audit_event_sequence
            FROM instruction_graphs
            """
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE stage = 'audit' AND episode_id IS NOT NULL"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM evals").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 5
    assert snapshot == (
        "CODEX.md",
        str(run_dir / "instruction-snapshot" / "CODEX.md"),
        snapshot[2],
        snapshot[3],
    )
    assert len(snapshot[2]) == 64
    assert snapshot[3] is not None
    assert graph == (str(run_dir / "instruction-graph.json"), graph[1], graph[2])
    assert len(graph[1]) == 64
    assert graph[2] is not None
    assert (run_dir / "instruction-graph.json").exists()
    assert codex.read_text(encoding="utf-8") == original


def test_mock_audit_records_chunk_granularity_instruction_refs(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Rules\n\nUse tests.\n\n## Review\n\nCheck the failure first.\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "tugboat.cli._scored_audit_payload",
        lambda bundle: {
            "edit_warranted": True,
            "evidence_refs": [event.evidence_id for event in bundle.events],
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.75,
        },
    )

    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace), "--mock-llmff-inspect"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    expected_refs = ["CODEX.md#rules", "CODEX.md#review"]
    assert audit["instruction_refs"] == expected_refs
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        stored_refs = json.loads(
            connection.execute("SELECT instruction_refs_json FROM audits").fetchone()[0]
        )
    assert stored_refs == expected_refs
