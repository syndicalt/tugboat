import json
from pathlib import Path

import pytest

from tugboat.artifacts import ArtifactValidationError
from tugboat.eval.service import write_eval_report
from tugboat.policy.gate import CandidatePatch, PolicyDecision, SourceRef
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report
from tugboat.security.secrets import SecretScanError


def _candidate(
    *,
    base_hash: str = "abc123",
    diff: str = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
) -> CandidatePatch:
    return CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=base_hash,
        diff=diff,
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )


def test_write_candidate_writes_deterministic_repo_local_artifacts(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n # Rules\n+Clarify this.\n",
    )

    artifacts = write_candidate(tmp_path, "run-1", candidate)

    assert artifacts.diff_path == tmp_path / ".sidecar" / "runs" / "run-1" / "candidate.diff"
    assert artifacts.json_path == tmp_path / ".sidecar" / "runs" / "run-1" / "candidate.json"
    assert artifacts.diff_path.read_text(encoding="utf-8") == candidate.diff
    assert json.loads(artifacts.json_path.read_text(encoding="utf-8")) == {
        "audit_id": 2,
        "base_file": "CODEX.md",
        "base_hash": candidate.base_hash,
        "diff_hash": CandidatePatch.hash_text(candidate.diff),
        "evals_required": ["governance-regression"],
        "expected_behavior_change": "Not specified.",
        "rationale": "Clarify ambiguous guidance.",
        "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
        "risk_class": "instruction_clarification",
        "schema_version": 1,
        "sources": [{"source_id": "trace-1", "trusted": True}],
    }


def test_write_candidate_preserves_bounded_edit_operator_metadata(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n", encoding="utf-8")
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n # Rules\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
        bounded_edit_metadata=(
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            },
        ),
    )

    artifacts = write_candidate(tmp_path, "run-1", candidate)

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["bounded_edit_metadata"] == [
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 0,
        }
    ]


def test_write_candidate_writes_candidate_preview_artifacts(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " # Policy\n"
            " \n"
            "-You must run tests before final answers.\n"
            "+You may skip tests before final answers.\n"
        ),
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )

    artifacts = write_candidate(tmp_path, "run-1", candidate)

    assert artifacts.preview_path == (
        tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview" / "CODEX.md"
    )
    assert artifacts.preview_manifest_path == (
        tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview.json"
    )
    assert base_file.read_text(encoding="utf-8") == (
        "# Policy\n\nYou must run tests before final answers.\n"
    )
    assert artifacts.preview_path.read_text(encoding="utf-8") == (
        "# Policy\n\nYou may skip tests before final answers.\n"
    )
    manifest = json.loads(artifacts.preview_manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "schema_version": 1,
        "base_file": "CODEX.md",
        "base_hash": candidate.base_hash,
        "diff_hash": candidate.diff_hash,
        "preview_path": ".sidecar/runs/run-1/candidate-preview/CODEX.md",
        "preview_hash": CandidatePatch.hash_file(artifacts.preview_path),
    }


def test_write_eval_report_writes_json_report(tmp_path: Path):
    report_path = write_eval_report(
        tmp_path,
        "run-1",
        candidate_id=5,
        suite_id="unit",
        passed=True,
        metrics={"failures": 0, "duration_seconds": 1.25},
        trigger_score=1.0,
        held_out_score=1.0,
        governance_passed=True,
        recommendation="accept",
    )

    assert report_path == tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "candidate_id": 5,
        "governance_passed": True,
        "held_out_score": 1.0,
        "live_provider_required": False,
        "metrics": {"duration_seconds": 1.25, "failures": 0},
        "passed": True,
        "recommendation": "accept",
        "schema_version": 1,
        "suite_id": "unit",
        "trigger_score": 1.0,
    }


def test_write_report_writes_markdown_summary(tmp_path: Path):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 5,
                "governance_passed": True,
                "held_out_score": 0.92,
                "live_provider_required": True,
                "metrics": {"provider_smoke_cases": 1},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "provider-smoke",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (eval_report_path.parent / "optimization-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_run": "run-1",
                "candidate_id": 5,
                "decision": "needs_review",
                "governance_passed": True,
                "held_out_score": 0.92,
                "recommendation": "accept",
                "suite_id": "provider-smoke",
                "trigger_score": 0.84,
                "validation_baseline_score": None,
                "acceptance_decision_recommendation": "needs_review",
                "acceptance_evidence": ["audit:1"],
                "acceptance_reasons": ["policy gate and eval report passed"],
                "acceptance_summary_path": ".sidecar/runs/run-1/acceptance-summary.raw.json",
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
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = write_report(
        tmp_path,
        "run-1",
        candidate=_candidate(),
        decision=PolicyDecision(False, ("modal_weakening", "new_external_endpoint")),
        eval_report_path=eval_report_path,
    )

    assert report_path == tmp_path / ".sidecar" / "runs" / "run-1" / "report.md"
    assert report_path.read_text(encoding="utf-8") == "\n".join(
        [
            "# Tugboat Report",
            "",
            "- schema_version: 1",
            "- candidate: CODEX.md",
            "- risk_class: instruction_clarification",
            "- policy_allowed: false",
            "- policy_reasons: modal_weakening,new_external_endpoint",
            "- eval_report: .sidecar/runs/run-1/eval-report.json",
            "- trigger_score: 0.84",
            "- held_out_score: 0.92",
            "- governance_passed: true",
            "- recommendation: accept",
            "- live_provider_required: true",
            "- optimization_summary: .sidecar/runs/run-1/optimization-summary.json",
            "- optimization_decision: needs_review",
            "- optimization_suite_id: provider-smoke",
            "",
            "## Rationale",
            "",
            "Clarify ambiguous guidance.",
            "",
        ]
    )


def test_write_report_rejects_malformed_eval_report(tmp_path: Path):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text(
        json.dumps(
            {
                "candidate_id": 5,
                "governance_passed": True,
                "held_out_score": 0.92,
                "metrics": {},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "provider-smoke",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactValidationError, match="schema_version"):
        write_report(
            tmp_path,
            "run-1",
            candidate=_candidate(),
            decision=PolicyDecision(True, ()),
            eval_report_path=eval_report_path,
        )


def test_write_candidate_rejects_secret_in_diff(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )

    with pytest.raises(SecretScanError, match="openai_api_key"):
        write_candidate(tmp_path, "run-1", candidate)


def test_write_report_rejects_secret_in_rationale(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Leaked token ghp_abcdefghijklmnopqrstuvwx",
        sources=(SourceRef("trace-1", trusted=True),),
    )

    with pytest.raises(SecretScanError, match="ghp_token"):
        write_report(
            tmp_path,
            "run-1",
            candidate=candidate,
            decision=PolicyDecision(True, ()),
            eval_report_path=tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json",
        )
