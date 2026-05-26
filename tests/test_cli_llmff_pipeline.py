import hashlib
import json
from pathlib import Path

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


def _write_fake_llmff(path: Path, *, eval_passed: bool = False) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

EVAL_PASSED = __EVAL_PASSED__

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
    inputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--input":
            inputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    if outputs:
        next(iter(outputs.values())).parent.joinpath("llmff-inputs.json").write_text(json.dumps({
            name: str(path) for name, path in inputs.items()
        }, sort_keys=True) + "\\n", encoding="utf-8")
    trace.write_text('{"event":"step","name":"episode-audit"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_fake"],
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        import hashlib
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        outputs["candidate_patch"].write_text(json.dumps({
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@\\n+Add llmff proposed regression guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "llmff proposed this from audited evidence",
            "sources": [{"source_id": "ev_fake", "trusted": True}],
            "reflections": [{
                "source_ref": "audit:latest",
                "summary": "Tests were skipped because regression guidance was missing."
            }],
            "bounded_edit_metadata": [{
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0
            }],
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-eval":
        if EVAL_PASSED:
            outputs["eval_report"].write_text(json.dumps({
                "passed": True,
                "trigger_score": 0.7,
                "held_out_score": 0.9,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0, "held_out_cases": 3},
            }) + "\\n", encoding="utf-8")
            outputs["policy_decision"].write_text(json.dumps({
                "allowed": True,
                "reasons": [],
            }) + "\\n", encoding="utf-8")
        else:
            outputs["eval_report"].write_text(json.dumps({
                "passed": False,
                "metrics": {"governance_regressions": 1, "held_out_cases": 3},
            }) + "\\n", encoding="utf-8")
            outputs["policy_decision"].write_text(json.dumps({
                "allowed": False,
                "reasons": ["held_out_regression"],
            }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""".replace("__EVAL_PASSED__", repr(eval_passed)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_audit_consumes_real_llmff_file_backed_audit_output(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "instruction_conflict"
    assert audit["severity"] == "high"
    assert audit["confidence"] == 0.91
    assert audit["evidence_refs"] == ["ev_fake"]
    assert (run_dir / "llmff-trace.jsonl").exists()
    assert (run_dir / "llmff-events.jsonl").exists()
    assert (run_dir / "checkpoint.json").exists()
    assert (run_dir / "audit.raw.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        job = store.connection.execute(
            """
            SELECT id, manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ?
            """,
            (run_dir.name,),
        ).fetchone()
        event_count = store.connection.execute(
            "SELECT COUNT(*) FROM llmff_events WHERE job_id = ?",
            (job[0],),
        ).fetchone()[0]
        output = store.connection.execute(
            """
            SELECT output_name, artifact_path, content_hash, audit_event_sequence
            FROM llmff_outputs
            WHERE job_id = ?
            """,
            (job[0],),
        ).fetchone()

    assert job[1:] == ("episode-audit.yaml", "completed")
    assert event_count == 1
    assert output[0] == "audit_report"
    assert output[1] == str(run_dir / "audit.raw.json")
    assert len(output[2]) == 64
    assert output[3] is not None


def test_audit_passes_redacted_trace_artifact_to_llmff(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug without secrets"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    llmff_inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
    assert Path(llmff_inputs["episode_trace"]) == run_dir / "trace-redacted.jsonl"
    assert (run_dir / "trace-input.jsonl").read_text(encoding="utf-8") == trace.read_text(
        encoding="utf-8"
    )
    assert (run_dir / "trace-redacted.jsonl").read_text(encoding="utf-8") == trace.read_text(
        encoding="utf-8"
    )


def test_audit_rejects_trace_with_secret_before_llmff_execution(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"tool_result","output":"OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"}\n',
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "secret_detected"
    assert audit["edit_warranted"] is False
    assert not (run_dir / "audit.raw.json").exists()


def test_propose_consumes_real_llmff_file_backed_candidate_output(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    diff = (run_dir / "candidate.diff").read_text(encoding="utf-8")
    assert candidate["rationale"] == "llmff proposed this from audited evidence"
    assert candidate["bounded_edit_metadata"] == [
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 0,
        }
    ]
    assert "llmff proposed regression guidance" in diff
    assert (run_dir / "candidate.raw.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        reflection = store.connection.execute(
            """
            SELECT source_ref, reflection_hash, artifact_path, audit_event_sequence
            FROM reflections
            WHERE run_id = ?
            """,
            (run_dir.name,),
        ).fetchone()
        edit = store.connection.execute(
            """
            SELECT id, operator, target_path, payload_json, audit_event_sequence
            FROM edit_operations
            WHERE candidate_id = ?
            """,
            (candidate["candidate_id"],),
        ).fetchone()
        candidate_edit = store.connection.execute(
            """
            SELECT candidate_id, edit_operation_id, target_path, risk_class, audit_event_sequence
            FROM candidate_edits
            WHERE candidate_id = ?
            """,
            (candidate["candidate_id"],),
        ).fetchone()

    assert reflection[0] == "audit:latest"
    assert len(reflection[1]) == 64
    assert Path(reflection[2]).exists()
    assert reflection[3] is not None
    assert edit[1:3] == ("add", "CODEX.md")
    assert json.loads(edit[3]) == {
        "changed_lines": 1,
        "file": "CODEX.md",
        "normative_changes": 0,
        "operator": "add",
        "section": "Testing",
    }
    assert edit[4] is not None
    assert candidate_edit == (
        candidate["candidate_id"],
        edit[0],
        "CODEX.md",
        "instruction_clarification",
        candidate_edit[4],
    )
    assert candidate_edit[4] is not None


def test_propose_passes_persisted_optimizer_memory_to_llmff(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key="fingerprint-1",
            payload={
                "semantic_fingerprint": "fingerprint-1",
                "rejection_reason": "held_out_not_improved",
                "source_refs": ["audit:1"],
            },
        )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    llmff_inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
    assert Path(llmff_inputs["optimizer_memory"]) == run_dir / "optimizer-memory.json"
    optimizer_memory = json.loads((run_dir / "optimizer-memory.json").read_text(encoding="utf-8"))
    assert optimizer_memory == {
        "rejected_edits": [
            {
                "rejection_reason": "held_out_not_improved",
                "semantic_fingerprint": "fingerprint-1",
                "source_refs": ["audit:1"],
            }
        ],
        "slow_update_notes": [],
    }


def test_propose_suppresses_candidate_matching_rejected_edit_memory(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    fingerprint = hashlib.sha256(b"add\nCODEX.md\nTesting").hexdigest()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "semantic_fingerprint": fingerprint,
                "rejection_reason": "held_out_not_improved",
                "source_refs": ["audit:1"],
            },
        )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        stored = store.connection.execute(
            """
            SELECT c.state, d.decision, d.reason
            FROM candidates c
            JOIN decisions d ON d.candidate_id = c.id
            WHERE c.id = ?
            """,
            (candidate["candidate_id"],),
        ).fetchone()

    assert candidate["bounded_edit_metadata"][0]["section"] == "Testing"
    assert policy_gate == {
        "allowed": False,
        "reasons": ["suppressed_by_rejected_edit_memory"],
    }
    assert decision["decision"] == "rejected"
    assert decision["policy_allowed"] is False
    assert decision["policy_reasons"] == ["suppressed_by_rejected_edit_memory"]
    assert stored == (
        "rejected",
        "rejected",
        "suppressed_by_rejected_edit_memory",
    )


def test_eval_consumes_real_llmff_file_backed_eval_output(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "governance-regression"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    policy_decision = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    assert eval_report["passed"] is False
    assert eval_report["metrics"] == {"governance_regressions": 1, "held_out_cases": 3}
    assert eval_report["governance_passed"] is False
    assert eval_report["recommendation"] == "reject"
    assert policy_decision == {"allowed": False, "reasons": ["held_out_regression"]}
    assert (run_dir / "eval-report.raw.json").exists()
    assert (run_dir / "policy-decision.raw.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_dir.name,),
        ).fetchall()
        output_names = [
            row[0]
            for row in store.connection.execute(
                """
                SELECT output_name
                FROM llmff_outputs
                ORDER BY id
                """
            )
        ]
        rejected_memory = store.connection.execute(
            """
            SELECT memory_type, key, payload_json, audit_event_sequence
            FROM optimizer_memory
            WHERE memory_type = 'rejected_edit'
            """
        ).fetchone()

    assert jobs == [
        ("episode-audit.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
    ]
    assert output_names == [
        "audit_report",
        "candidate_patch",
        "eval_report",
        "policy_decision",
    ]
    assert rejected_memory is not None
    assert rejected_memory[0] == "rejected_edit"
    assert len(rejected_memory[1]) == 64
    assert json.loads(rejected_memory[2]) == {
        "rejection_reason": "reject",
        "semantic_fingerprint": rejected_memory[1],
        "source_refs": ["audit:1"],
    }
    assert rejected_memory[3] is not None


def test_optimize_runs_llmff_propose_and_eval_as_governed_workflow(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", eval_passed=True)
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

    assert main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "held-out"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_dir.name,),
        ).fetchall()
        candidate_state = store.connection.execute("SELECT state FROM candidates").fetchone()[0]
        decision_rows = store.connection.execute(
            """
            SELECT policy, decision, reason
            FROM decisions
            ORDER BY id
            """
        ).fetchall()

    assert jobs == [
        ("episode-audit.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
    ]
    assert eval_report["trigger_score"] == 0.7
    assert eval_report["held_out_score"] == 0.9
    assert summary == {
        "audit_run": run_dir.name,
        "candidate_id": decision["candidate_id"],
        "decision": "needs_review",
        "held_out_score": 0.9,
        "recommendation": "accept",
        "suite_id": "held-out",
        "trigger_score": 0.7,
    }
    assert decision["decision"] == "needs_review"
    assert candidate_state == "needs_review"
    assert decision_rows[-1] == (
        "optimization_acceptance_gate",
        "needs_review",
        "held_out_improved",
    )


def test_optimize_rejects_candidate_when_held_out_gate_fails(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
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

    assert main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "held-out"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_state = store.connection.execute("SELECT state FROM candidates").fetchone()[0]
        gate_decision = store.connection.execute(
            """
            SELECT policy, decision, reason
            FROM decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert summary["decision"] == "rejected"
    assert summary["recommendation"] == "reject"
    assert decision["decision"] == "rejected"
    assert decision["policy_reasons"] == ["eval report recommendation was reject"]
    assert candidate_state == "rejected"
    assert gate_decision == (
        "optimization_acceptance_gate",
        "rejected",
        "eval report recommendation was reject",
    )
