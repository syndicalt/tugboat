import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.cli import main


def _write_fake_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    print(json.dumps({"manifest": Path(args[3]).stem, "network_required": False}))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    trace.write_text('{"event":"step"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.82,
            "evidence_refs": ["ev_e2e"],
        }) + "\\n", encoding="utf-8")
    elif manifest == "drift-detect":
        outputs["drift_clusters"].write_text(json.dumps({
            "clusters": [{"cluster_id": "drift-1", "evidence_refs": ["ev_e2e"]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        outputs["candidate_patch"].write_text(json.dumps({
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@\\n+Add regression-test guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "llmff proposed this from audited evidence",
            "expected_behavior_change": "Agents add regression-test guidance before closing fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
            "sources": [{"source_id": "ev_e2e", "trusted": True}],
            "bounded_edit_metadata": [{
                "operator": "add",
                "file": "CODEX.md",
                "section": "Rules",
                "changed_lines": 1,
                "normative_changes": 0
            }],
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-eval":
        outputs["eval_report"].write_text(json.dumps({
            "passed": True,
            "trigger_score": 0.75,
            "held_out_score": 0.88,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
        }) + "\\n", encoding="utf-8")
        outputs["policy_decision"].write_text(json.dumps({
            "allowed": True,
            "reasons": [],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


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
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0
    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0

    run_dirs = sorted((repo / ".sidecar" / "runs").iterdir())
    assert run_dirs
    run_dir = run_dirs[-1]
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "instruction-snapshot").is_dir()
    manifest_dir = repo / ".sidecar" / "manifests"
    assert sorted(path.name for path in manifest_dir.glob("*.yaml")) == [
        "acceptance-summary.yaml",
        "drift-detect.yaml",
        "episode-audit.yaml",
        "instruction-index.yaml",
        "patch-eval.yaml",
        "patch-propose.yaml",
    ]
    inspect = json.loads(
        (run_dir / "patch-eval" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["manifest_path"].endswith(".sidecar/manifests/patch-eval.yaml")
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "candidate.json").exists()
    assert (run_dir / "policy-gate.json").exists()
    assert (run_dir / "eval-report.json").exists()
    assert (run_dir / "decision.json").exists()
    assert (run_dir / "report.md").exists()
    assert json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
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
        "tugboat.audit.pipeline._scored_audit_payload",
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
