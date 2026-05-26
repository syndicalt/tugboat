import hashlib
import json
from pathlib import Path

import pytest

from tugboat.artifacts import ArtifactValidationError
from tugboat.cli import _write_optimization_summary, main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


def _write_fake_llmff(
    path: Path,
    *,
    eval_passed: bool = False,
    fail_manifest: str | None = None,
    sources: object | None = None,
    bounded_edit_metadata: object | None = None,
    candidate_overrides: dict[str, object] | None = None,
    audit_report: object | None = None,
    evidence_ids: object | None = None,
    instruction_index: object | None = None,
    drift_clusters: object | None = None,
    eval_report: object | None = None,
    policy_decision: object | None = None,
    acceptance_summary: object | None = None,
    reflections: object | None = None,
    secret_artifact: str | None = None,
    invalid_json_output: str | None = None,
    optimizer_notes: object | None = None,
    proposal_rationale: object | None = None,
) -> Path:
    if sources is None:
        sources = [{"source_id": "ev_fake", "trusted": True}]
    if bounded_edit_metadata is None:
        bounded_edit_metadata = [
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            }
        ]
    if eval_report is None:
        eval_report = (
            {
                "passed": True,
                "trigger_score": 0.7,
                "held_out_score": 0.9,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0, "held_out_cases": 3},
                "validation_splits": {
                    "trigger": ["trigger:regression"],
                    "held_out": ["held-out:no-regression"],
                    "governance": ["governance:policy"],
                },
            }
            if eval_passed
            else {
                "passed": False,
                "metrics": {"governance_regressions": 1, "held_out_cases": 3},
            }
        )
    if policy_decision is None:
        policy_decision = (
            {"allowed": True, "reasons": []}
            if eval_passed
            else {"allowed": False, "reasons": ["held_out_regression"]}
        )
    if candidate_overrides is None:
        candidate_overrides = {}
    if audit_report is None:
        audit_report = {
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_fake"],
        }
    if evidence_ids is None:
        evidence_ids = {"evidence_ids": ["ev_fake"]}
    if instruction_index is None:
        instruction_index = {"documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]}
    if drift_clusters is None:
        drift_clusters = {"clusters": [{"cluster_id": "drift-1", "evidence_refs": ["ev_fake"]}]}
    if optimizer_notes is None:
        optimizer_notes = {
            "notes": [
                {
                    "summary": "Regression guidance should be proposed from drift clusters.",
                    "evidence_refs": ["ev_fake"],
                }
            ]
        }
    if acceptance_summary is None:
        acceptance_summary = {
            "decision_recommendation": "needs_review",
            "reasons": ["policy gate and eval report passed"],
            "evidence": ["audit:1"],
            "reviewer_checklist": ["Review candidate diff", "Confirm rollback command"],
            "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
        }
    if proposal_rationale is None:
        proposal_rationale = {
            "rationale": "Patch proposal is grounded in drift clusters and optimizer notes.",
            "evidence_refs": ["ev_fake"],
            "style_constraints": ["Preserve existing instruction tone."],
        }
    if reflections is None:
        reflections = [
            {
                "source_ref": "audit:latest",
                "summary": "Tests were skipped because regression guidance was missing.",
            }
        ]
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

EVAL_PASSED = __EVAL_PASSED__
FAIL_MANIFEST = __FAIL_MANIFEST__
SOURCES = __SOURCES__
BOUNDED_EDIT_METADATA = __BOUNDED_EDIT_METADATA__
CANDIDATE_OVERRIDES = __CANDIDATE_OVERRIDES__
AUDIT_REPORT = __AUDIT_REPORT__
EVIDENCE_IDS = __EVIDENCE_IDS__
INSTRUCTION_INDEX = __INSTRUCTION_INDEX__
DRIFT_CLUSTERS = __DRIFT_CLUSTERS__
OPTIMIZER_NOTES = __OPTIMIZER_NOTES__
PROPOSAL_RATIONALE = __PROPOSAL_RATIONALE__
EVAL_REPORT = __EVAL_REPORT__
POLICY_DECISION = __POLICY_DECISION__
ACCEPTANCE_SUMMARY = __ACCEPTANCE_SUMMARY__
REFLECTIONS = __REFLECTIONS__
SECRET_ARTIFACT = __SECRET_ARTIFACT__
SECRET_VALUE = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"
INVALID_JSON_OUTPUT = __INVALID_JSON_OUTPUT__

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    manifest = Path(args[3]).stem
    inspect_payload = {
        "manifest": manifest,
        "network_required": False,
        "providers": [],
        "external_calls": [],
    }
    if SECRET_ARTIFACT == "inspect_episode_audit" and manifest == "episode-audit":
        inspect_payload["raw_model_payload"] = SECRET_VALUE
    print(json.dumps(inspect_payload))
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
        output_dir = next(iter(outputs.values())).parent
        output_dir.joinpath("llmff-inputs.json").write_text(json.dumps({
            name: str(path) for name, path in inputs.items()
        }, sort_keys=True) + "\\n", encoding="utf-8")
        with output_dir.joinpath("llmff-inputs-by-manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "inputs": {name: str(path) for name, path in inputs.items()},
                "manifest": manifest,
            }, sort_keys=True) + "\\n")
        runtime_args = {
            "manifest": manifest,
            "retry_attempts": args[args.index("--retry-attempts") + 1],
            "retry_backoff_ms": args[args.index("--retry-backoff-ms") + 1],
            "timeout_ms": args[args.index("--timeout-ms") + 1],
        }
        with output_dir.joinpath("llmff-run-args.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(runtime_args, sort_keys=True) + "\\n")
    trace.write_text('{"event":"step","name":"episode-audit"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if SECRET_ARTIFACT == "events" and manifest == "episode-audit":
        events.write_text(json.dumps({"event": "model_output", "text": SECRET_VALUE}) + "\\n", encoding="utf-8")
    if SECRET_ARTIFACT == "checkpoint" and manifest == "episode-audit":
        checkpoint.write_text(json.dumps({"token": SECRET_VALUE}) + "\\n", encoding="utf-8")
    if manifest == FAIL_MANIFEST:
        events.write_text(json.dumps({
            "event": "run_failed",
            "run_failed": {
                "failure_kind": "fixture_failure",
                "failure_message": "fixture failed"
            }
        }) + "\\n", encoding="utf-8")
        raise SystemExit(7)
    if manifest == "instruction-index":
        if INVALID_JSON_OUTPUT == "instruction_index":
            outputs["instruction_index"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["instruction_index"].write_text(json.dumps(INSTRUCTION_INDEX) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        if SECRET_ARTIFACT == "audit_report":
            outputs["audit_report"].write_text(json.dumps({"raw_model_payload": SECRET_VALUE}) + "\\n", encoding="utf-8")
        elif INVALID_JSON_OUTPUT == "audit_report":
            outputs["audit_report"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["audit_report"].write_text(json.dumps(AUDIT_REPORT) + "\\n", encoding="utf-8")
        if "evidence_ids" in outputs:
            if INVALID_JSON_OUTPUT == "evidence_ids":
                outputs["evidence_ids"].write_text("{not json\\n", encoding="utf-8")
            else:
                outputs["evidence_ids"].write_text(json.dumps(EVIDENCE_IDS) + "\\n", encoding="utf-8")
    elif manifest == "drift-detect":
        if INVALID_JSON_OUTPUT == "drift_clusters":
            outputs["drift_clusters"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["drift_clusters"].write_text(json.dumps(DRIFT_CLUSTERS) + "\\n", encoding="utf-8")
        if "optimizer_notes" in outputs:
            if INVALID_JSON_OUTPUT == "optimizer_notes":
                outputs["optimizer_notes"].write_text("{not json\\n", encoding="utf-8")
            else:
                outputs["optimizer_notes"].write_text(json.dumps(OPTIMIZER_NOTES) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        import hashlib
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        if "proposal_rationale" in outputs:
            if INVALID_JSON_OUTPUT == "proposal_rationale":
                outputs["proposal_rationale"].write_text("{not json\\n", encoding="utf-8")
            else:
                outputs["proposal_rationale"].write_text(json.dumps(PROPOSAL_RATIONALE) + "\\n", encoding="utf-8")
        if SECRET_ARTIFACT == "candidate_patch":
            outputs["candidate_patch"].write_text(json.dumps({"raw_model_payload": SECRET_VALUE}) + "\\n", encoding="utf-8")
            raise SystemExit(0)
        if INVALID_JSON_OUTPUT == "candidate_patch":
            outputs["candidate_patch"].write_text("{not json\\n", encoding="utf-8")
            raise SystemExit(0)
        candidate_patch = {
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@\\n+Add llmff proposed regression guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "llmff proposed this from audited evidence",
            "expected_behavior_change": "Agents add regression guidance before closing similar fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
            "sources": SOURCES,
            "reflections": REFLECTIONS,
            "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
        }
        candidate_patch.update(CANDIDATE_OVERRIDES)
        outputs["candidate_patch"].write_text(json.dumps(candidate_patch) + "\\n", encoding="utf-8")
    elif manifest == "patch-eval":
        if SECRET_ARTIFACT == "eval_report":
            outputs["eval_report"].write_text(json.dumps({"raw_model_payload": SECRET_VALUE}) + "\\n", encoding="utf-8")
        elif INVALID_JSON_OUTPUT == "eval_report":
            outputs["eval_report"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["eval_report"].write_text(json.dumps(EVAL_REPORT) + "\\n", encoding="utf-8")
        if INVALID_JSON_OUTPUT == "policy_decision":
            outputs["policy_decision"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["policy_decision"].write_text(json.dumps(POLICY_DECISION) + "\\n", encoding="utf-8")
    elif manifest == "acceptance-summary":
        if INVALID_JSON_OUTPUT == "acceptance_summary":
            outputs["acceptance_summary"].write_text("{not json\\n", encoding="utf-8")
        else:
            outputs["acceptance_summary"].write_text(json.dumps(ACCEPTANCE_SUMMARY) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""".replace("__EVAL_PASSED__", repr(eval_passed)).replace(
            "__FAIL_MANIFEST__", repr(fail_manifest)
        ).replace(
            "__SOURCES__", repr(sources)
        ).replace(
            "__BOUNDED_EDIT_METADATA__", repr(bounded_edit_metadata)
        ).replace(
            "__CANDIDATE_OVERRIDES__", repr(candidate_overrides)
        ).replace("__AUDIT_REPORT__", repr(audit_report)).replace(
            "__EVIDENCE_IDS__", repr(evidence_ids)
        ).replace(
            "__INSTRUCTION_INDEX__", repr(instruction_index)
        ).replace(
            "__DRIFT_CLUSTERS__", repr(drift_clusters)
        ).replace(
            "__OPTIMIZER_NOTES__", repr(optimizer_notes)
        ).replace(
            "__PROPOSAL_RATIONALE__", repr(proposal_rationale)
        ).replace(
            "__EVAL_REPORT__", repr(eval_report)
        ).replace(
            "__POLICY_DECISION__", repr(policy_decision)
        ).replace(
            "__ACCEPTANCE_SUMMARY__", repr(acceptance_summary)
        ).replace(
            "__REFLECTIONS__", repr(reflections)
        ).replace(
            "__SECRET_ARTIFACT__", repr(secret_artifact)
        ).replace(
            "__INVALID_JSON_OUTPUT__", repr(invalid_json_output)
        ),
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
    assert (run_dir / "episode-audit" / "llmff-trace.jsonl").exists()
    assert (run_dir / "episode-audit" / "llmff-events.jsonl").exists()
    assert (run_dir / "episode-audit" / "checkpoint.json").exists()
    assert (run_dir / "audit.raw.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        job = store.connection.execute(
            """
            SELECT id, manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ? AND manifest_name = 'episode-audit.yaml'
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


def test_audit_preserves_llmff_reported_instruction_refs(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Rules\n\nUse tests.\n\n## Review\n\nCheck the failure first.\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        audit_report={
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_fake"],
            "instruction_refs": ["CODEX.md#rules"],
        },
    )
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
    assert audit["instruction_refs"] == ["CODEX.md#rules"]
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        stored_refs = json.loads(
            store.connection.execute(
                "SELECT instruction_refs_json FROM audits WHERE run_id = ?",
                (run_dir.name,),
            ).fetchone()[0]
        )
    assert stored_refs == ["CODEX.md#rules"]


def test_audit_rejects_malformed_llmff_raw_audit_output_without_normalizing(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        audit_report={
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": "high",
            "evidence_refs": ["ev_fake"],
        },
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "audit rejected: audit.raw.json field has wrong type: confidence" in output
    assert (run_dir / "audit.raw.json").exists()
    assert not (run_dir / "audit.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audits = store.connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
        run_status = store.connection.execute(
            "SELECT status FROM runs WHERE id = ? AND stage = 'audit'",
            (run_dir.name,),
        ).fetchone()[0]
    assert audits == 0
    assert run_status == "failed"


def test_audit_rejects_invalid_json_raw_audit_output_without_normalizing(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="audit_report",
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "audit rejected: audit.raw.json contains invalid JSON" in output
    assert (run_dir / "audit.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_rejects_invalid_json_evidence_ids_output_without_normalizing(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="evidence_ids",
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "audit rejected: evidence-ids.raw.json contains invalid JSON" in output
    assert (run_dir / "evidence-ids.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_rejects_wrong_type_evidence_ids_output_without_normalizing(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        evidence_ids={"evidence_ids": "ev_fake"},
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "audit rejected: evidence-ids.raw.json field has wrong type: evidence_ids" in output
    assert (run_dir / "evidence-ids.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_rejects_evidence_refs_not_declared_by_evidence_ids(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        audit_report={
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_hallucinated"],
        },
        evidence_ids={"evidence_ids": ["ev_real"]},
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert (
        "audit rejected: audit evidence refs not declared by evidence_ids: "
        "ev_hallucinated"
    ) in output
    assert (run_dir / "audit.raw.json").exists()
    assert (run_dir / "evidence-ids.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_rejects_malformed_llmff_instruction_index_raw_output(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        instruction_index={"documents": [{"path": 7, "obligations": ["Use tests."]}]},
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert (
        "instruction index rejected: instruction-index.raw.json field has wrong type: documents[0].path"
        in output
    )
    assert (run_dir / "instruction-index.raw.json").exists()
    assert not (run_dir / "audit.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_rejects_invalid_json_instruction_index_raw_output(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="instruction_index",
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "instruction index rejected: instruction-index.raw.json contains invalid JSON" in output
    assert (run_dir / "instruction-index.raw.json").exists()
    assert not (run_dir / "audit.raw.json").exists()
    assert not (run_dir / "audit.json").exists()


def test_audit_runs_instruction_index_before_episode_audit(tmp_path: Path):
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
    llmff_inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
    llmff_inputs_by_manifest = [
        json.loads(line)
        for line in (run_dir / "llmff-inputs-by-manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
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
                SELECT o.output_name
                FROM llmff_outputs o
                JOIN llmff_jobs j ON j.id = o.job_id
                WHERE j.run_id = ?
                ORDER BY o.id
                """,
                (run_dir.name,),
            )
        ]

    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
    ]
    assert output_names == ["instruction_index", "audit_report", "evidence_ids"]
    assert llmff_inputs_by_manifest[0]["inputs"] == {
        "instruction_corpus": str(run_dir / "instruction-snapshot"),
        "policy": str(repo / ".sidecar" / "policy.yaml"),
    }
    assert Path(llmff_inputs["instruction_index"]) == run_dir / "instruction-index.raw.json"
    assert (run_dir / "instruction-index.raw.json").exists()
    assert (run_dir / "evidence-ids.raw.json").exists()


def test_audit_passes_canonical_episode_artifact_to_llmff(tmp_path: Path):
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
    assert Path(llmff_inputs["episode_trace"]) == run_dir / "canonical-episode.json"
    assert (run_dir / "trace-input.jsonl").read_text(encoding="utf-8") == trace.read_text(
        encoding="utf-8"
    )
    canonical = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    assert canonical["schema_version"] == 1
    assert canonical["request"] == "Fix bug without secrets"
    assert canonical["events"][0]["event_type"] == "user_request"
    assert canonical["events"][0]["source_trust"] == "user"
    assert canonical["events"][0]["evidence_id"].startswith("ev_")


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


def test_audit_rejects_llmff_events_with_secret_without_normalizing_audit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", secret_artifact="events")
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
    assert audit["secret_findings"][0]["path"].endswith("llmff-events.jsonl")
    assert (run_dir / "audit.raw.json").exists()
    assert audit["failure_class"] != "instruction_conflict"


def test_audit_rejects_llmff_checkpoint_with_secret_without_normalizing_audit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", secret_artifact="checkpoint")
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
    assert audit["secret_findings"][0]["path"].endswith("checkpoint.json")
    assert (run_dir / "audit.raw.json").exists()
    assert audit["failure_class"] != "instruction_conflict"


def test_audit_rejects_llmff_raw_output_with_secret_without_normalizing_audit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", secret_artifact="audit_report")
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
    assert audit["secret_findings"][0]["path"].endswith("audit.raw.json")
    assert audit["failure_class"] != "instruction_conflict"


def test_audit_rejects_llmff_inspect_artifact_with_secret_without_running_audit(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        secret_artifact="inspect_episode_audit",
    )
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
    assert audit["secret_findings"][0]["path"].endswith("llmff-inspect.json")
    assert not (run_dir / "audit.raw.json").exists()


def test_propose_requires_real_llmff_audit_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")

    assert main(["audit", "--repo", str(repo), "--trace", str(trace), "--mock-llmff-inspect"]) == 0
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "propose requires llmff audit output" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_llmff_candidate_output_with_secret(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", secret_artifact="candidate_patch")
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "secret scan failed" in output
    assert "openai_api_key" in output
    assert not (run_dir / "candidate.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_invalid_json_candidate_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="candidate_patch",
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate.raw.json contains invalid JSON" in output
    assert (run_dir / "candidate.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


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
    assert candidate["expected_behavior_change"] == (
        "Agents add regression guidance before closing similar fixes."
    )
    assert candidate["evals_required"] == ["governance-regression"]
    assert candidate["rollback_plan"] == ["tugboat", "rollback", "--decision", "latest"]
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


def test_propose_rejects_candidate_source_not_in_audit_evidence(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        sources=[{"source_id": "ev_hallucinated", "trusted": True}],
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate source refs not declared by audit evidence" in output
    assert (run_dir / "candidate.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        assert store.connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_propose_preserves_llmff_pending_eval_definition_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        candidate_overrides={
            "pending_audit_eval_definition_paths": ["tests/fixtures/evals/*.json"],
        },
    )
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
    assert candidate["pending_audit_eval_definition_paths"] == [
        "tests/fixtures/evals/*.json"
    ]


def test_propose_runs_drift_detect_before_patch_propose(tmp_path: Path):
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
    llmff_inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
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
                SELECT o.output_name
                FROM llmff_outputs o
                JOIN llmff_jobs j ON j.id = o.job_id
                WHERE j.run_id = ?
                ORDER BY o.id
                """,
                (run_dir.name,),
            )
        ]

    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
    ]
    assert output_names == [
        "instruction_index",
        "audit_report",
        "evidence_ids",
        "drift_clusters",
        "optimizer_notes",
        "candidate_patch",
        "proposal_rationale",
    ]
    assert Path(llmff_inputs["drift_clusters"]) == run_dir / "drift.raw.json"
    assert Path(llmff_inputs["optimizer_notes"]) == run_dir / "optimizer-notes.raw.json"
    assert (run_dir / "drift.raw.json").exists()
    assert (run_dir / "optimizer-notes.raw.json").exists()
    assert (run_dir / "proposal-rationale.raw.json").exists()


def test_propose_passes_raw_instruction_index_to_drift_and_patch_manifests(tmp_path: Path):
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
    records = [
        json.loads(line)
        for line in (run_dir / "llmff-inputs-by-manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    inputs_by_manifest = {record["manifest"]: record["inputs"] for record in records}
    raw_index = run_dir / "instruction-index.raw.json"

    assert Path(inputs_by_manifest["drift-detect"]["instruction_index"]) == (
        run_dir / "instruction-snapshot"
    )
    assert Path(inputs_by_manifest["drift-detect"]["instruction_index_artifact"]) == raw_index
    assert Path(inputs_by_manifest["patch-propose"]["instruction_index"]) == (
        run_dir / "instruction-snapshot"
    )
    assert Path(inputs_by_manifest["patch-propose"]["instruction_index_artifact"]) == raw_index
    assert Path(inputs_by_manifest["patch-propose"]["optimizer_notes"]) == (
        run_dir / "optimizer-notes.raw.json"
    )


def test_propose_rejects_missing_raw_instruction_index_before_llmff(tmp_path: Path, capsys):
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
    capsys.readouterr()
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    (run_dir / "instruction-index.raw.json").unlink()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    assert "propose requires llmff instruction index output: missing instruction-index.raw.json" in output
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name
            FROM llmff_jobs
            WHERE run_id = ?
              AND manifest_name IN ('drift-detect.yaml', 'patch-propose.yaml')
            """,
            (run_dir.name,),
        ).fetchall()
    assert jobs == []


def test_propose_preserves_existing_manifest_input_contract_while_raw_index_exists(
    tmp_path: Path,
):
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
    manifest_dir = repo / ".sidecar" / "manifests"
    (manifest_dir / "drift-detect.yaml").write_text(
        """
name: drift-detect
purpose: old local manifest
inputs:
  - audit_reports
  - instruction_index
outputs:
  - drift_clusters
  - optimizer_notes
""".lstrip(),
        encoding="utf-8",
    )
    (manifest_dir / "patch-propose.yaml").write_text(
        """
name: patch-propose
purpose: old local manifest
inputs:
  - instruction_index
  - drift_clusters
  - optimizer_notes
  - policy
outputs:
  - candidate_patch
  - proposal_rationale
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    records = [
        json.loads(line)
        for line in (run_dir / "llmff-inputs-by-manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    inputs_by_manifest = {record["manifest"]: record["inputs"] for record in records}
    assert (run_dir / "instruction-index.raw.json").exists()
    assert Path(inputs_by_manifest["drift-detect"]["instruction_index"]) == (
        run_dir / "instruction-snapshot"
    )
    assert "instruction_index_artifact" not in inputs_by_manifest["drift-detect"]
    assert Path(inputs_by_manifest["patch-propose"]["instruction_index"]) == (
        run_dir / "instruction-snapshot"
    )
    assert "instruction_index_artifact" not in inputs_by_manifest["patch-propose"]
    assert "optimizer_memory" not in inputs_by_manifest["patch-propose"]


def test_propose_rejects_materialized_manifest_missing_required_input(
    tmp_path: Path,
    capsys,
):
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
    capsys.readouterr()
    manifest_dir = repo / ".sidecar" / "manifests"
    (manifest_dir / "drift-detect.yaml").write_text(
        """
name: drift-detect
purpose: broken local manifest
inputs:
  - instruction_index
outputs:
  - drift_clusters
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    assert "drift-detect.yaml missing required llmff inputs: audit_reports" in output


def test_propose_rejects_materialized_drift_manifest_missing_required_output(
    tmp_path: Path,
    capsys,
):
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
    capsys.readouterr()
    manifest_dir = repo / ".sidecar" / "manifests"
    (manifest_dir / "drift-detect.yaml").write_text(
        """
name: drift-detect
purpose: broken local manifest
inputs:
  - audit_reports
  - instruction_index
outputs:
  - drift_clusters
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "drift-detect.yaml missing required llmff outputs: optimizer_notes" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_materialized_patch_propose_manifest_missing_required_output(
    tmp_path: Path,
    capsys,
):
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
    capsys.readouterr()
    manifest_dir = repo / ".sidecar" / "manifests"
    (manifest_dir / "patch-propose.yaml").write_text(
        """
name: patch-propose
purpose: broken local manifest
inputs:
  - instruction_index
  - drift_clusters
  - optimizer_notes
  - policy
outputs:
  - candidate_patch
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "patch-propose.yaml missing required llmff outputs: proposal_rationale" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_materialized_manifest_without_input_list(
    tmp_path: Path,
    capsys,
):
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
    capsys.readouterr()
    manifest_dir = repo / ".sidecar" / "manifests"
    (manifest_dir / "drift-detect.yaml").write_text(
        """
name: drift-detect
purpose: malformed local manifest
input:
  - audit_reports
  - instruction_index
outputs:
  - drift_clusters
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    assert "drift-detect.yaml must declare llmff inputs as a list" in output


def test_propose_rejects_malformed_llmff_drift_raw_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        drift_clusters={"clusters": [{"cluster_id": "drift-1", "evidence_refs": [7]}]},
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "drift.raw.json field has wrong type: clusters[0].evidence_refs[0]" in output
    assert (run_dir / "drift.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_invalid_json_drift_raw_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="drift_clusters",
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "drift.raw.json contains invalid JSON" in output
    assert (run_dir / "drift.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_malformed_llmff_optimizer_notes_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        optimizer_notes={"notes": [{"summary": "Use drift evidence.", "evidence_refs": [7]}]},
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "optimizer-notes.raw.json field has wrong type: notes[0].evidence_refs[0]" in output
    assert (run_dir / "optimizer-notes.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_invalid_json_optimizer_notes_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="optimizer_notes",
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "optimizer-notes.raw.json contains invalid JSON" in output
    assert (run_dir / "optimizer-notes.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_malformed_llmff_proposal_rationale_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        proposal_rationale={
            "rationale": "Use drift evidence.",
            "evidence_refs": [7],
            "style_constraints": ["Preserve tone."],
        },
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        output_names = [
            row[0]
            for row in store.connection.execute(
                """
                SELECT o.output_name
                FROM llmff_outputs o
                JOIN llmff_jobs j ON j.id = o.job_id
                WHERE j.run_id = ?
                ORDER BY o.id
                """,
                (run_dir.name,),
            )
        ]
    assert "proposal-rationale.raw.json field has wrong type: evidence_refs[0]" in output
    assert "proposal_rationale" in output_names
    assert (run_dir / "proposal-rationale.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_propose_rejects_invalid_json_proposal_rationale_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="proposal_rationale",
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "proposal-rationale.raw.json contains invalid JSON" in output
    assert (run_dir / "proposal-rationale.raw.json").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()


def test_pipeline_preserves_per_manifest_lifecycle_artifacts(tmp_path: Path):
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
    for stage in (
        "instruction-index",
        "episode-audit",
        "drift-detect",
        "patch-propose",
        "patch-eval",
    ):
        assert (run_dir / stage / "llmff-inspect.json").exists()
        assert (run_dir / stage / "llmff-trace.jsonl").exists()
        assert (run_dir / stage / "llmff-events.jsonl").exists()
        assert (run_dir / stage / "checkpoint.json").exists()


def test_propose_rejects_malformed_llmff_bounded_edit_metadata(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        bounded_edit_metadata=[
            {
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            }
        ],
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate.raw.json missing required field: bounded_edit_metadata[0].operator" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_count = store.connection.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]
    assert candidate_count == 0


def test_propose_rejects_malformed_llmff_candidate_sources(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        sources=[{"source_id": "ev_fake", "trusted": "false"}],
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate.raw.json field has wrong type: sources[0].trusted" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_count = store.connection.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]
    assert candidate_count == 0


def test_propose_rejects_malformed_llmff_candidate_scalar_fields(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        candidate_overrides={"base_file": ["CODEX.md"]},
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate.raw.json field has wrong type: base_file" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_count = store.connection.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]
    assert candidate_count == 0


def test_propose_rejects_malformed_llmff_reflection_artifact(
    tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        reflections=[{"source_ref": "audit:latest", "raw_model_payload": "nope"}],
    )
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "candidate.raw.json missing required field: reflections[0].summary" in output
    assert not (run_dir / "reflection-001.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        reflection_count = store.connection.execute(
            "SELECT COUNT(*) FROM reflections"
        ).fetchone()[0]
    assert reflection_count == 0


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
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="validation_baseline",
            key="validation_baseline:held-out",
            payload={
                "candidate_id": 7,
                "held_out_score": 0.82,
                "suite_id": "held-out",
            },
        )

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    llmff_inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
    assert Path(llmff_inputs["optimizer_memory"]) == run_dir / "optimizer-memory.json"
    optimizer_memory = json.loads((run_dir / "optimizer-memory.json").read_text(encoding="utf-8"))
    assert optimizer_memory == {
        "schema_version": 1,
        "rejected_edits": [
            {
                "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                "rejection_reason": "held_out_not_improved",
                "semantic_fingerprint": "fingerprint-1",
                "source_refs": ["audit:1"],
            }
        ],
        "slow_update_notes": [],
        "slow_update_records": [],
        "validation_baselines": [
            {
                "candidate_id": 7,
                "held_out_score": 0.82,
                "suite_id": "held-out",
            }
        ],
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
        "schema_version": 1,
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


def test_propose_rejects_candidate_over_learning_rate_budget(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        bounded_edit_metadata=[
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 21,
                "normative_changes": 0,
            }
        ],
    )
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
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        stored_state = store.connection.execute(
            "SELECT state FROM candidates WHERE id = ?",
            (candidate["candidate_id"],),
        ).fetchone()[0]

    assert policy_gate == {
        "schema_version": 1,
        "allowed": False,
        "reasons": ["max_changed_lines_exceeded"],
    }
    assert decision["decision"] == "rejected"
    assert stored_state == "rejected"


def test_propose_records_llmff_failure_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", fail_manifest="patch-propose")
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
    capsys.readouterr()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "llmff patch-propose failed with exit code 7" in output
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate.diff").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        run_row = store.connection.execute(
            "SELECT stage, status FROM runs WHERE id = ?",
            (run_dir.name,),
        ).fetchone()
        job_row = store.connection.execute(
            """
            SELECT j.status, e.event_type, e.payload_json
            FROM llmff_jobs j
            JOIN llmff_events e ON e.job_id = j.id
            WHERE j.run_id = ? AND j.manifest_name = 'patch-propose.yaml'
            """,
            (run_dir.name,),
        ).fetchone()
        candidate_count = store.connection.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]

    assert run_row == ("propose", "failed")
    assert job_row[0:2] == ("failed", "run_failed")
    assert json.loads(job_row[2])["run_failed"]["failure_kind"] == "fixture_failure"
    assert candidate_count == 0


def test_eval_rejects_llmff_eval_output_with_secret(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", secret_artifact="eval_report")
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
    capsys.readouterr()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "eval rejected: secret scan failed" in output
    assert "openai_api_key" in output
    assert not (run_dir / "eval-report.raw.json").exists()
    assert not (run_dir / "eval-report.json").exists()


def test_eval_rejects_invalid_json_eval_report_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="eval_report",
    )
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
    capsys.readouterr()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "eval rejected: eval-report.raw.json contains invalid JSON" in output
    assert (run_dir / "eval-report.raw.json").exists()
    assert not (run_dir / "eval-report.json").exists()


def test_eval_rejects_invalid_json_policy_decision_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        invalid_json_output="policy_decision",
    )
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
    capsys.readouterr()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "eval rejected: policy-decision.raw.json contains invalid JSON" in output
    assert (run_dir / "policy-decision.raw.json").exists()
    assert not (run_dir / "eval-report.json").exists()


def test_eval_consumes_real_llmff_file_backed_eval_output(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        sources=[{"source_id": "ev_fake", "trusted": True}],
    )
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
    assert eval_report["governance_passed"] is True
    assert eval_report["recommendation"] == "reject"
    assert policy_decision == {
        "schema_version": 1,
        "allowed": True,
        "reasons": [],
    }
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
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
    ]
    assert output_names == [
        "instruction_index",
        "audit_report",
        "evidence_ids",
        "drift_clusters",
        "optimizer_notes",
        "candidate_patch",
        "proposal_rationale",
        "eval_report",
        "policy_decision",
    ]
    assert rejected_memory is not None
    assert rejected_memory[0] == "rejected_edit"
    assert len(rejected_memory[1]) == 64
    assert json.loads(rejected_memory[2]) == {
        "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
        "rejection_reason": "reject",
        "semantic_fingerprint": rejected_memory[1],
        "source_refs": ["ev_fake"],
    }
    assert rejected_memory[3] is not None


def test_eval_recomputes_policy_gate_instead_of_trusting_llmff_policy_decision(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    codex.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_passed=True,
        policy_decision={"allowed": True, "reasons": []},
    )
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
    codex.write_text("# Rules\n\nUse tests.\n\nRepo changed after proposal.\n", encoding="utf-8")
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    raw_policy_decision = json.loads((run_dir / "policy-decision.raw.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))

    assert raw_policy_decision == {"allowed": True, "reasons": []}
    assert policy_gate == {
        "schema_version": 1,
        "allowed": False,
        "reasons": ["base_hash_mismatch"],
    }
    assert eval_report["passed"] is False
    assert eval_report["governance_passed"] is False
    assert eval_report["recommendation"] == "reject"
    assert summary["decision"] == "rejected"
    assert not (run_dir / "acceptance-summary.raw.json").exists()


def test_eval_does_not_copy_llmff_policy_denial_when_deterministic_gate_allows(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": False,
            "trigger_score": 0.7,
            "held_out_score": 0.6,
            "governance_passed": False,
            "recommendation": "reject",
            "metrics": {"governance_regressions": 1, "held_out_cases": 3},
        },
        policy_decision={"allowed": False, "reasons": ["llmff_claimed_policy_denial"]},
    )
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
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    raw_policy_decision = json.loads((run_dir / "policy-decision.raw.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))

    assert raw_policy_decision == {
        "allowed": False,
        "reasons": ["llmff_claimed_policy_denial"],
    }
    assert policy_gate == {"schema_version": 1, "allowed": True, "reasons": []}
    assert eval_report["passed"] is False
    assert eval_report["recommendation"] == "reject"
    assert not (run_dir / "acceptance-summary.raw.json").exists()


def test_eval_rejects_malformed_llmff_eval_report_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": "false",
            "trigger_score": 0.7,
            "held_out_score": 0.9,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0},
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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
    capsys.readouterr()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "eval rejected: eval-report.raw.json field has wrong type: passed" in output
    assert (run_dir / "eval-report.raw.json").exists()
    assert not (run_dir / "eval-report.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        eval_count = store.connection.execute("SELECT COUNT(*) FROM evals").fetchone()[0]
    assert eval_count == 0


def test_eval_records_llmff_failure_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff", fail_manifest="patch-eval")
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
    capsys.readouterr()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "llmff patch-eval failed with exit code 7" in output
    assert not (run_dir / "eval-report.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        run_row = store.connection.execute(
            "SELECT stage, status FROM runs WHERE id = ?",
            (run_dir.name,),
        ).fetchone()
        job_row = store.connection.execute(
            """
            SELECT j.status, e.event_type, e.payload_json
            FROM llmff_jobs j
            JOIN llmff_events e ON e.job_id = j.id
            WHERE j.run_id = ? AND j.manifest_name = 'patch-eval.yaml'
            """,
            (run_dir.name,),
        ).fetchone()
        eval_count = store.connection.execute("SELECT COUNT(*) FROM evals").fetchone()[0]

    assert run_row == ("eval", "failed")
    assert job_row[0:2] == ("failed", "run_failed")
    assert json.loads(job_row[2])["run_failed"]["failure_kind"] == "fixture_failure"
    assert eval_count == 0


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
        slow_update_notes = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'slow_update'
            ORDER BY id
            """
        ).fetchall()
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
        ("acceptance-summary.yaml", "completed"),
    ]
    assert eval_report["trigger_score"] == 0.7
    assert eval_report["held_out_score"] == 0.9
    assert summary == {
        "schema_version": 1,
        "audit_run": run_dir.name,
        "candidate_id": decision["candidate_id"],
        "decision": "needs_review",
        "governance_passed": True,
        "held_out_score": 0.9,
        "recommendation": "accept",
        "suite_id": "held-out",
        "trigger_score": 0.7,
        "validation_baseline_score": None,
        "acceptance_decision_recommendation": "needs_review",
        "acceptance_evidence": ["audit:1"],
        "acceptance_reasons": ["policy gate and eval report passed"],
        "acceptance_summary_path": f".sidecar/runs/{run_dir.name}/acceptance-summary.raw.json",
        "accepted_bounded_edit_metadata": [
            {
                "changed_lines": 1,
                "file": "CODEX.md",
                "normative_changes": 0,
                "operator": "add",
                "section": "Testing",
            }
        ],
        "reviewer_checklist": ["Review candidate diff", "Confirm rollback command"],
        "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
    }
    assert decision["decision"] == "needs_review"
    assert candidate_state == "needs_review"
    assert decision_rows[-1] == (
        "optimization_acceptance_gate",
        "needs_review",
        "held_out_improved",
    )
    assert [json.loads(row[0]) for row in slow_update_notes] == [
        {
            "category": "successful",
            "legacy_note": "successful: held_out_improved for candidate "
            f"{decision['candidate_id']} in suite held-out",
            "note": "held_out_improved for candidate "
            f"{decision['candidate_id']} in suite held-out",
        }
    ]


def test_optimization_summary_rejects_accepted_eval_without_bounded_edit_metadata(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "candidate_id": 7,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "risk_class": "instruction_clarification",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "decision.json").write_text(
        json.dumps({"candidate_id": 7, "decision": "needs_review"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "governance_passed": True,
                "held_out_score": 0.9,
                "metrics": {"governance_regressions": 0},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "held-out",
                "trigger_score": 0.7,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}) + "\n",
        encoding="utf-8",
    )

    assert _write_optimization_summary(repo, run_dir, suite_id="held-out") == 1

    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert summary["decision"] == "rejected"
    assert "accepted_bounded_edit_metadata" not in summary
    assert decision["policy_reasons"] == ["accepted candidate missing bounded edit metadata"]


def test_optimization_summary_requires_acceptance_summary_for_needs_review(tmp_path: Path):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "candidate_id": 7,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "risk_class": "instruction_clarification",
                "bounded_edit_metadata": [
                    {
                        "changed_lines": 1,
                        "file": "CODEX.md",
                        "normative_changes": 0,
                        "operator": "add",
                        "section": "Testing",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "decision.json").write_text(
        json.dumps({"candidate_id": 7, "decision": "needs_review"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "governance_passed": True,
                "held_out_score": 0.9,
                "metrics": {"governance_regressions": 0},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "held-out",
                "trigger_score": 0.7,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactValidationError, match="acceptance-summary.raw.json"):
        _write_optimization_summary(repo, run_dir, suite_id="held-out")

    assert not (run_dir / "optimization-summary.json").exists()


def test_optimize_passes_policy_llmff_runtime_knobs_to_all_manifests(tmp_path: Path):
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
  timeout_ms: 12345
  retry_attempts: 2
  retry_backoff_ms: 250
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "held-out"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    runtime_args = [
        json.loads(line)
        for line in (run_dir / "llmff-run-args.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert runtime_args == [
        {
            "manifest": manifest,
            "timeout_ms": "12345",
            "retry_attempts": "2",
            "retry_backoff_ms": "250",
        }
        for manifest in [
            "instruction-index",
            "episode-audit",
            "drift-detect",
            "patch-propose",
            "patch-eval",
            "acceptance-summary",
        ]
    ]


def test_optimize_runs_acceptance_summary_manifest_after_eval_gate(tmp_path: Path):
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
        output = store.connection.execute(
            """
            SELECT o.output_name, o.artifact_path
            FROM llmff_outputs o
            JOIN llmff_jobs j ON j.id = o.job_id
            WHERE j.run_id = ? AND j.manifest_name = 'acceptance-summary.yaml'
            """,
            (run_dir.name,),
        ).fetchone()

    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
        ("acceptance-summary.yaml", "completed"),
    ]
    summary = json.loads((run_dir / "acceptance-summary.raw.json").read_text(encoding="utf-8"))
    inputs = json.loads((run_dir / "llmff-inputs.json").read_text(encoding="utf-8"))
    assert summary["decision_recommendation"] == "needs_review"
    assert set(inputs) == {
        "audit_report",
        "candidate_patch",
        "eval_reports",
        "policy_gate",
        "proposal_rationale",
        "risk_class",
    }
    assert Path(inputs["audit_report"]) == run_dir / "audit.raw.json"
    assert Path(inputs["proposal_rationale"]) == run_dir / "proposal-rationale.raw.json"
    assert (run_dir / "acceptance-summary" / "llmff-inspect.json").exists()
    assert (run_dir / "acceptance-summary" / "llmff-trace.jsonl").exists()
    assert (run_dir / "acceptance-summary" / "llmff-events.jsonl").exists()
    assert (run_dir / "acceptance-summary" / "checkpoint.json").exists()
    assert output == ("acceptance_summary", str(run_dir / "acceptance-summary.raw.json"))


def test_eval_runs_acceptance_summary_and_writes_optimization_summary_for_file_backed_candidate(
    tmp_path: Path,
):
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

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    acceptance_summary = json.loads((run_dir / "acceptance-summary.raw.json").read_text(encoding="utf-8"))
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
        baseline_payload = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'validation_baseline'
              AND key = 'validation_baseline:all'
            """
        ).fetchone()[0]
        gate_decision = store.connection.execute(
            """
            SELECT policy, decision, reason
            FROM decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        validation_splits = store.connection.execute(
            """
            SELECT split_name, case_ids_json
            FROM validation_splits
            WHERE suite_id = 'all'
            ORDER BY split_name
            """
        ).fetchall()

    assert jobs[-2:] == [("patch-eval.yaml", "completed"), ("acceptance-summary.yaml", "completed")]
    assert acceptance_summary["decision_recommendation"] == "needs_review"
    assert summary["decision"] == "needs_review"
    assert summary["suite_id"] == "all"
    assert summary["validation_baseline_score"] is None
    assert summary["acceptance_summary_path"] == (
        f".sidecar/runs/{run_dir.name}/acceptance-summary.raw.json"
    )
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert eval_report["validation_splits"] == {
        "governance": ["governance:policy"],
        "held_out": ["held-out:no-regression"],
        "trigger": ["trigger:regression"],
    }
    assert summary["accepted_bounded_edit_metadata"] == [
        {
            "changed_lines": 1,
            "file": "CODEX.md",
            "normative_changes": 0,
            "operator": "add",
            "section": "Testing",
        }
    ]
    assert json.loads(baseline_payload) == {
        "candidate_id": summary["candidate_id"],
        "held_out_score": 0.9,
        "suite_id": "all",
    }
    assert gate_decision == (
        "optimization_acceptance_gate",
        "needs_review",
        "held_out_improved",
    )
    assert {row[0]: json.loads(row[1]) for row in validation_splits} == {
        "governance": ["governance:policy"],
        "held_out": ["held-out:no-regression"],
        "trigger": ["trigger:regression"],
    }


def test_eval_rejects_accept_recommendation_without_held_out_cases(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.7,
            "held_out_score": 0.9,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 0},
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert "llmff eval_report cannot accept without held-out validation cases" in output
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert report["metrics"]["held_out_cases"] == 0
    assert not (run_dir / "acceptance-summary.raw.json").exists()


def test_eval_rejects_accept_recommendation_without_validation_splits(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.7,
            "held_out_score": 0.9,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert "llmff eval_report cannot accept without validation split provenance" in output
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert not (run_dir / "acceptance-summary.raw.json").exists()


def test_eval_rejects_overlapping_trigger_and_held_out_validation_splits(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.7,
            "held_out_score": 0.9,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
            "validation_splits": {
                "trigger": ["case:shared"],
                "held_out": ["case:shared"],
            },
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert "triggering validation cases overlap held-out validation cases" in output
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert not (run_dir / "acceptance-summary.raw.json").exists()


def test_eval_rejects_file_backed_candidate_before_acceptance_summary_when_baseline_not_improved(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.2,
            "held_out_score": 0.3,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
            "validation_splits": {
                "trigger": ["trigger:regression"],
                "held_out": ["held-out:no-regression"],
            },
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo.resolve()),
            memory_type="validation_baseline",
            key="validation_baseline:all",
            payload={"suite_id": "all", "held_out_score": 0.4},
        )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
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
        baseline_payload = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'validation_baseline'
              AND key = 'validation_baseline:all'
            """
        ).fetchone()[0]
        gate_decision = store.connection.execute(
            """
            SELECT policy, decision, reason
            FROM decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert jobs[-1] == ("patch-eval.yaml", "completed")
    assert ("acceptance-summary.yaml", "completed") not in jobs
    assert not (run_dir / "acceptance-summary.raw.json").exists()
    assert summary["decision"] == "rejected"
    assert summary["suite_id"] == "all"
    assert summary["validation_baseline_score"] == 0.4
    assert decision["policy_reasons"] == ["held-out eval score did not improve over baseline"]
    assert json.loads(baseline_payload)["held_out_score"] == 0.4
    assert gate_decision == (
        "optimization_acceptance_gate",
        "rejected",
        "held-out eval score did not improve over baseline",
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
        slow_update_notes = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'slow_update'
            ORDER BY id
            """
        ).fetchall()

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
    assert [json.loads(row[0]) for row in slow_update_notes] == [
        {
            "category": "rejected",
            "legacy_note": "rejected: eval report recommendation was reject for candidate "
            f"{decision['candidate_id']} in suite held-out",
            "note": "eval report recommendation was reject for candidate "
            f"{decision['candidate_id']} in suite held-out",
        }
    ]


def test_optimize_feeds_episode_minibatch_guidance_to_patch_propose(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"type":"user_request","text":"Fix bug"}',
                '{"type":"user_correction","content":"You skipped regression tests"}',
                '{"type":"outcome_label","label":"rejected"}',
                "",
            ]
        ),
        encoding="utf-8",
    )
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
    inputs_by_manifest = [
        json.loads(line)
        for line in (run_dir / "llmff-inputs-by-manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    patch_propose_inputs = next(
        item["inputs"] for item in inputs_by_manifest if item["manifest"] == "patch-propose"
    )
    optimizer_memory = json.loads(Path(patch_propose_inputs["optimizer_memory"]).read_text(encoding="utf-8"))
    batch = json.loads((run_dir / "optimization-batch.json").read_text(encoding="utf-8"))

    assert batch == {
        "schema_version": 1,
        "held_out_suite": "held-out",
        "success_episodes": [],
        "failure_episodes": ["1"],
        "success_patterns": [],
        "failure_patterns": ["You skipped regression tests"],
    }
    assert optimizer_memory["slow_update_records"] == [
        {
            "category": "optimizer_guidance",
            "note": (
                "SkillOpt minibatch before held-out suite held-out: "
                "avoid repeating failure patterns: You skipped regression tests"
            ),
        }
    ]


def test_optimize_rejects_candidate_when_held_out_does_not_beat_validation_baseline(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.2,
            "held_out_score": 0.3,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
            "validation_splits": {
                "trigger": ["trigger:regression"],
                "held_out": ["held-out:no-regression"],
            },
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="validation_baseline",
            key="validation_baseline:held-out",
            payload={"suite_id": "held-out", "held_out_score": 0.4},
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
        baseline_payload = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'validation_baseline'
              AND key = 'validation_baseline:held-out'
            """
        ).fetchone()[0]
        rejected_edit_rows = store.connection.execute(
            """
            SELECT key, payload_json
            FROM optimizer_memory
            WHERE memory_type = 'rejected_edit'
            """
        ).fetchall()

    assert summary["decision"] == "rejected"
    assert summary["validation_baseline_score"] == 0.4
    assert decision["decision"] == "rejected"
    assert decision["policy_reasons"] == ["held-out eval score did not improve over baseline"]
    assert candidate_state == "rejected"
    assert gate_decision == (
        "optimization_acceptance_gate",
        "rejected",
        "held-out eval score did not improve over baseline",
    )
    assert json.loads(baseline_payload)["held_out_score"] == 0.4
    fingerprint = hashlib.sha256(b"add\nCODEX.md\nTesting").hexdigest()
    assert [(row[0], json.loads(row[1])) for row in rejected_edit_rows] == [
        (
            fingerprint,
            {
                "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                "rejection_reason": "held-out eval score did not improve over baseline",
                "semantic_fingerprint": fingerprint,
                "source_refs": [f"candidate:{summary['candidate_id']}", "suite:held-out"],
            },
        )
    ]


def test_optimize_records_baseline_rejection_before_acceptance_summary_runs(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        fail_manifest="acceptance-summary",
        eval_report={
            "passed": True,
            "trigger_score": 0.2,
            "held_out_score": 0.3,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
            "validation_splits": {
                "trigger": ["trigger:regression"],
                "held_out": ["held-out:no-regression"],
            },
        },
        policy_decision={"allowed": True, "reasons": []},
    )
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
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="validation_baseline",
            key="validation_baseline:held-out",
            payload={"suite_id": "held-out", "held_out_score": 0.4},
        )

    assert main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "held-out"]) == 1

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert not (run_dir / "acceptance-summary.raw.json").exists()
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert summary["decision"] == "rejected"
    assert decision["policy_reasons"] == ["held-out eval score did not improve over baseline"]


def test_optimize_records_validation_baseline_after_accepted_held_out_improvement(
    tmp_path: Path,
):
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
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        baseline_payload = store.connection.execute(
            """
            SELECT payload_json
            FROM optimizer_memory
            WHERE memory_type = 'validation_baseline'
              AND key = 'validation_baseline:held-out'
            """
        ).fetchone()[0]

    assert summary["decision"] == "needs_review"
    assert summary["validation_baseline_score"] is None
    assert json.loads(baseline_payload) == {
        "candidate_id": summary["candidate_id"],
        "held_out_score": 0.9,
        "suite_id": "held-out",
    }


def test_optimize_rejects_candidate_when_governance_gate_fails_despite_accept_recommendation(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_report={
            "passed": True,
            "trigger_score": 0.7,
            "held_out_score": 0.9,
            "governance_passed": False,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 1, "held_out_cases": 3},
            "validation_splits": {
                "trigger": ["trigger:regression"],
                "held_out": ["held-out:no-regression"],
            },
        },
        policy_decision={"allowed": False, "reasons": ["governance_regression"]},
    )
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
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_dir.name,),
        ).fetchall()

    assert summary["decision"] == "rejected"
    assert summary["recommendation"] == "accept"
    assert decision["decision"] == "rejected"
    assert decision["policy_reasons"] == ["eval governance did not pass"]
    assert candidate_state == "rejected"
    assert not (run_dir / "acceptance-summary.raw.json").exists()
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
    ]


def test_optimize_rejects_malformed_acceptance_summary_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_passed=True,
        acceptance_summary={
            "decision_recommendation": "needs_review",
            "reasons": ["policy gate and eval report passed"],
            "evidence": ["audit:1"],
            "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
        },
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "reviewer_checklist" in output
    assert (run_dir / "acceptance-summary.raw.json").exists()
    assert not (run_dir / "optimization-summary.json").exists()


def test_optimize_rejects_invalid_json_acceptance_summary_output(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_passed=True,
        invalid_json_output="acceptance_summary",
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "acceptance-summary.raw.json contains invalid JSON" in output
    assert (run_dir / "acceptance-summary.raw.json").exists()
    assert not (run_dir / "optimization-summary.json").exists()


def test_optimize_rejects_semantically_empty_acceptance_summary_output(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(
        tmp_path / "fake-llmff",
        eval_passed=True,
        acceptance_summary={
            "decision_recommendation": "ship_it_anyway",
            "reasons": [],
            "evidence": [],
            "reviewer_checklist": [],
            "rollback_command": [],
        },
    )
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

    output = capsys.readouterr().out
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert "acceptance-summary.raw.json field has unsupported value: decision_recommendation" in output
    assert (run_dir / "acceptance-summary.raw.json").exists()
    assert not (run_dir / "optimization-summary.json").exists()
