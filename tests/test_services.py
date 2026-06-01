import json
import os
from pathlib import Path

import pytest

from tugboat.artifacts import ArtifactValidationError
from tugboat.eval.service import write_eval_report
from tugboat.policy.gate import CandidatePatch, PolicyDecision, SourceRef
from tugboat.propose.service import write_candidate
from tugboat.report.service import (
    highest_impact_summary_fields,
    rollback_readiness_summary,
    write_report,
)
from tugboat.security.secrets import SecretScanError


BOUNDED_EDIT_METADATA = (
    {
        "operator": "add",
        "file": "CODEX.md",
        "section": "Testing",
        "changed_lines": 1,
        "normative_changes": 0,
    },
)


def _candidate(
    *,
    base_hash: str = "abc123",
    diff: str = "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Clarify this.\n",
    expected_behavior_change: str = "Not specified.",
    sources: tuple[SourceRef, ...] = (SourceRef("trace-1", trusted=True),),
) -> CandidatePatch:
    return CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=base_hash,
        diff=diff,
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        expected_behavior_change=expected_behavior_change,
        sources=sources,
        bounded_edit_metadata=BOUNDED_EDIT_METADATA,
    )


def test_highest_impact_summary_fields_rank_bounded_edits_and_degrade_unknowns():
    fields = highest_impact_summary_fields(
        {
            "governance_passed": True,
            "held_out_score": "not-a-score",
            "metrics": "not-an-object",
            "trigger_score": 0.84,
        },
        {
            "accepted_bounded_edit_metadata": [
                {
                    "changed_lines": 8,
                    "file": "CODEX.md",
                    "normative_changes": 0,
                    "operator": "add",
                    "section": "Testing",
                },
                {
                    "changed_lines": 2,
                    "file": "AGENTS.md",
                    "normative_changes": 1,
                    "operator": "replace",
                },
            ],
        },
    )

    assert fields == {
        "changed_lines": "2",
        "governance_passed": "true",
        "held_out_delta": "unknown",
        "instruction_token_delta": "unknown",
        "normative_changes": "1",
        "operator": "replace",
        "target": "AGENTS.md",
    }


def test_highest_impact_summary_fields_treats_non_numeric_edit_counts_as_zero():
    fields = highest_impact_summary_fields(
        {
            "governance_passed": False,
            "held_out_score": 1.0,
            "metrics": {"instruction_token_delta": -2},
            "trigger_score": 0.75,
        },
        {
            "accepted_bounded_edit_metadata": [
                {
                    "changed_lines": True,
                    "file": "CODEX.md",
                    "operator": "add",
                    "normative_changes": "unknown",
                    "section": "Testing",
                },
                {
                    "changed_lines": 1.9,
                    "file": "CODEX.md",
                    "operator": "replace",
                    "normative_changes": 0.7,
                    "section": "Review",
                },
            ],
        },
    )

    assert fields == {
        "changed_lines": "1",
        "governance_passed": "false",
        "held_out_delta": "0.25",
        "instruction_token_delta": "-2",
        "normative_changes": "0",
        "operator": "replace",
        "target": "CODEX.md#Review",
    }


@pytest.mark.parametrize(
    "optimization_payload",
    (
        {},
        {"accepted_bounded_edit_metadata": "not-a-list"},
        {"accepted_bounded_edit_metadata": []},
        {"accepted_bounded_edit_metadata": ["not-an-object"]},
    ),
)
def test_highest_impact_summary_fields_returns_none_without_bounded_edit_metadata(
    optimization_payload: dict[str, object],
):
    assert highest_impact_summary_fields({"metrics": {}}, optimization_payload) is None


def test_write_candidate_writes_deterministic_repo_local_artifacts(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,1 +1,2 @@\n # Rules\n+Clarify this.\n",
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
        "bounded_edit_metadata": [
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            }
        ],
    }


def test_write_candidate_records_scoped_preview_manifest(tmp_path: Path):
    base_file = tmp_path / "services" / "web" / "CODEX.md"
    base_file.parent.mkdir(parents=True)
    base_file.write_text("# Rules\n", encoding="utf-8")
    diff = (
        "--- a/services/web/CODEX.md\n"
        "+++ b/services/web/CODEX.md\n"
        "@@ -1,1 +1,2 @@\n"
        " # Rules\n"
        "+Clarify scoped browser tests.\n"
    )
    candidate = CandidatePatch(
        audit_id=2,
        base_file="services/web/CODEX.md",
        base_hash=CandidatePatch.hash_file(base_file),
        diff=diff,
        risk_class="instruction_clarification",
        rationale="Clarify scoped browser guidance.",
        scope_root="services/web",
        sources=(SourceRef("trace-1", trusted=True),),
        bounded_edit_metadata=(
            {
                "operator": "add",
                "file": "services/web/CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
                "scope_root": "services/web",
            },
        ),
    )

    artifacts = write_candidate(tmp_path, "run-1", candidate)

    candidate_payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    preview_payload = json.loads(artifacts.preview_manifest_path.read_text(encoding="utf-8"))
    assert candidate_payload["scope_root"] == "services/web"
    assert candidate_payload["bounded_edit_metadata"][0]["scope_root"] == "services/web"
    assert preview_payload["scope_root"] == "services/web"
    assert preview_payload["preview_path"] == (
        ".sidecar/runs/run-1/candidate-preview/services/web/CODEX.md"
    )


def test_write_candidate_preserves_bounded_edit_operator_metadata(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n", encoding="utf-8")
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,1 +1,2 @@\n # Rules\n+Clarify this.\n",
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
            "@@ -1,3 +1,3 @@\n"
            " # Policy\n"
            " \n"
            "-You must run tests before final answers.\n"
            "+You may skip tests before final answers.\n"
        ),
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
        bounded_edit_metadata=BOUNDED_EDIT_METADATA,
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


def test_write_candidate_marks_generated_artifacts_private_under_permissive_umask(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,1 +1,2 @@\n # Rules\n+Clarify this.\n",
    )

    previous_umask = os.umask(0o022)
    try:
        artifacts = write_candidate(tmp_path, "run-1", candidate)
    finally:
        os.umask(previous_umask)

    generated_files = (
        artifacts.diff_path,
        artifacts.json_path,
        artifacts.preview_path,
        artifacts.preview_manifest_path,
    )
    assert [path.stat().st_mode & 0o777 for path in generated_files] == [0o600] * 4
    assert artifacts.diff_path.parent.stat().st_mode & 0o777 == 0o700
    assert artifacts.preview_path.parent.stat().st_mode & 0o777 == 0o700


def test_write_candidate_cleans_published_artifacts_when_preview_validation_fails(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Missing context line\n"
            "+Clarify this.\n"
        ),
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(ValueError, match="candidate diff cannot be applied"):
        write_candidate(tmp_path, "run-1", candidate)

    assert not (run_dir / "candidate.diff").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate-preview.json").exists()
    assert not (run_dir / "candidate-preview").exists()


def test_write_candidate_rejects_secret_in_candidate_metadata(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " # Rules\n"
            " \n"
            " Use tests.\n"
            "+Clarify regression expectations.\n"
        ),
        risk_class="instruction_clarification",
        rationale="Candidate rationale leaked ghp_abcdefghijklmnopqrstuvwx",
        sources=(SourceRef("trace-1", trusted=True),),
        bounded_edit_metadata=BOUNDED_EDIT_METADATA,
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(SecretScanError, match="ghp_token"):
        write_candidate(tmp_path, "run-1", candidate)

    assert not (run_dir / "candidate.diff").exists()
    assert not (run_dir / "candidate.json").exists()
    assert not (run_dir / "candidate-preview.json").exists()
    assert not (run_dir / "candidate-preview").exists()


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
        longitudinal_metrics={
            "acceptance_rate": 0.5,
            "rejection_rate": 0.25,
            "rollback_rate": 0.25,
            "recurring_incident_rate": 0.2,
            "mean_changed_lines": 4,
            "corpus_growth": 3,
            "duplicate_rule_count": 1,
            "stale_doc_count": 2,
            "governance_regression_count": 0,
            "user_correction_recurrence": 2,
        },
    )

    assert report_path == tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "candidate_id": 5,
        "governance_passed": True,
        "held_out_score": 1.0,
        "live_provider_required": False,
        "longitudinal_metrics": {
            "acceptance_rate": 0.5,
            "corpus_growth": 3,
            "duplicate_rule_count": 1,
            "stale_doc_count": 2,
            "governance_regression_count": 0,
            "mean_changed_lines": 4,
            "recurring_incident_rate": 0.2,
            "rejection_rate": 0.25,
            "rollback_rate": 0.25,
            "user_correction_recurrence": 2,
        },
        "metrics": {"duration_seconds": 1.25, "failures": 0},
        "passed": True,
        "recommendation": "accept",
        "schema_version": 1,
        "suite_id": "unit",
        "trigger_score": 1.0,
    }


def test_write_eval_report_marks_report_private_under_permissive_umask(tmp_path: Path):
    previous_umask = os.umask(0o022)
    try:
        report_path = write_eval_report(
            tmp_path,
            "run-1",
            candidate_id=5,
            suite_id="unit",
            passed=True,
            metrics={"failures": 0},
            trigger_score=1.0,
            held_out_score=1.0,
            governance_passed=True,
            recommendation="accept",
        )
    finally:
        os.umask(previous_umask)

    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report_path.parent.stat().st_mode & 0o777 == 0o700


def test_write_eval_report_rejects_secret_in_metrics(tmp_path: Path):
    with pytest.raises(SecretScanError, match="ghp_token"):
        write_eval_report(
            tmp_path,
            "run-1",
            candidate_id=5,
            suite_id="unit",
            passed=False,
            metrics={"raw_output": "provider leaked ghp_abcdefghijklmnopqrstuvwx"},
            trigger_score=0.4,
            held_out_score=0.3,
            governance_passed=False,
            recommendation="reject",
        )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    ).exists()


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
                "longitudinal_metrics": {
                    "acceptance_rate": 0.5,
                    "corpus_growth": 3,
                    "duplicate_rule_count": 1,
                    "governance_regression_count": 0,
                    "mean_changed_lines": 4,
                    "recurring_incident_rate": 0.2,
                    "rejection_rate": 0.25,
                    "rollback_rate": 0.25,
                    "stale_doc_count": 2,
                    "user_correction_recurrence": 2,
                },
                "metrics": {"instruction_token_delta": 6, "provider_smoke_cases": 1},
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
                "reviewer_checklist": [
                    "Review candidate diff and proposal rationale against trace evidence.",
                    "Confirm risk classification matches the bounded edit.",
                    "Verify source evidence supports the recommendation.",
                    "Confirm expected behavior change is narrow and intentional.",
                    "Confirm rollback command before applying.",
                ],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = eval_report_path.parent
    (run_dir / "trace-input.jsonl").write_text('{"type":"user_request"}\n', encoding="utf-8")
    (run_dir / "instruction-snapshot").mkdir()
    (run_dir / "instruction-graph.json").write_text('{"documents":[]}\n', encoding="utf-8")
    (run_dir / "audit.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (run_dir / "candidate.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (run_dir / "candidate.diff").write_text("--- a/CODEX.md\n+++ b/CODEX.md\n", encoding="utf-8")
    (run_dir / "policy-gate.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (run_dir / "decision.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (run_dir / "provenance-bundle.json").write_text('{"schema_version":1}\n', encoding="utf-8")
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
            "- risk_explanation: class=instruction_clarification policy_allowed=false policy_reasons=modal_weakening,new_external_endpoint review_required=none",
            "- rollback_readiness: state=planned command=tugboat rollback --decision latest artifact=.sidecar/runs/run-1/optimization-summary.json applied_commit=missing",
            "- source_evidence: trace-1 trusted=true",
            "- expected_behavior_change: Not specified.",
            "- trace_input: .sidecar/runs/run-1/trace-input.jsonl",
            "- instruction_snapshot: .sidecar/runs/run-1/instruction-snapshot",
            "- instruction_graph: .sidecar/runs/run-1/instruction-graph.json",
            "- audit_report: .sidecar/runs/run-1/audit.json",
            "- candidate_metadata: .sidecar/runs/run-1/candidate.json",
            "- candidate_diff: .sidecar/runs/run-1/candidate.diff",
            "- policy_gate: .sidecar/runs/run-1/policy-gate.json",
            "- eval_report: .sidecar/runs/run-1/eval-report.json",
            "- decision_artifact: .sidecar/runs/run-1/decision.json",
            "- provenance_bundle: .sidecar/runs/run-1/provenance-bundle.json",
            "- trigger_score: 0.84",
            "- held_out_score: 0.92",
            "- governance_passed: true",
            "- recommendation: accept",
            "- live_provider_required: true",
            "- longitudinal_acceptance_rate: 0.5",
            "- longitudinal_rejection_rate: 0.25",
            "- longitudinal_rollback_rate: 0.25",
            "- longitudinal_recurring_incident_rate: 0.2",
            "- longitudinal_mean_changed_lines: 4",
            "- longitudinal_corpus_growth: 3",
            "- longitudinal_duplicate_rule_count: 1",
            "- longitudinal_stale_doc_count: 2",
            "- longitudinal_governance_regression_count: 0",
            "- longitudinal_user_correction_recurrence: 2",
            "- highest_impact_summary: CODEX.md#Testing add changed_lines=1 held_out_delta=0.08 instruction_token_delta=6 governance_passed=true",
            "- optimization_summary: .sidecar/runs/run-1/optimization-summary.json",
            "- optimization_decision: needs_review",
            "- optimization_suite_id: provider-smoke",
            "- optimization_trigger_score: 0.84",
            "- optimization_held_out_score: 0.92",
            "- optimization_governance_passed: true",
            "- optimization_recommendation: accept",
            "- acceptance_reason: policy gate and eval report passed",
            "- reviewer_checklist: Review candidate diff and proposal rationale against trace evidence.; Confirm risk classification matches the bounded edit.; Verify source evidence supports the recommendation.; Confirm expected behavior change is narrow and intentional.; Confirm rollback command before applying.",
            "- rollback_command: tugboat rollback --decision latest",
            "",
            "## Rationale",
            "",
            "Clarify ambiguous guidance.",
            "",
        ]
    )


def test_write_report_summarizes_source_evidence_and_expected_behavior_without_payloads(
    tmp_path: Path,
):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 5,
                "governance_passed": True,
                "held_out_score": 0.92,
                "metrics": {},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        expected_behavior_change="Require regression test guidance for bug fixes.",
        sources=(
            SourceRef("audit:run-1:ev_user_correction", trusted=True),
            SourceRef("drift-cluster:testing", trusted=False),
        ),
    )

    report_path = write_report(
        tmp_path,
        "run-1",
        candidate=candidate,
        decision=PolicyDecision(True, ()),
        eval_report_path=eval_report_path,
    )

    report = report_path.read_text(encoding="utf-8")
    assert (
        "- source_evidence: audit:run-1:ev_user_correction trusted=true; "
        "drift-cluster:testing trusted=false"
    ) in report
    assert (
        "- expected_behavior_change: Require regression test guidance for bug fixes."
    ) in report
    assert "user_correction" in report
    assert "You skipped" not in report


def test_write_report_degrades_review_readiness_without_optional_artifacts(tmp_path: Path):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 5,
                "governance_passed": True,
                "held_out_score": 0.92,
                "metrics": {},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "unit",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report_path = write_report(
        tmp_path,
        "run-1",
        candidate=_candidate(),
        decision=PolicyDecision(True, ()),
        eval_report_path=eval_report_path,
    )
    report = report_path.read_text(encoding="utf-8")

    assert (
        "- risk_explanation: class=instruction_clarification policy_allowed=true "
        "policy_reasons=none review_required=none"
    ) in report
    assert (
        "- rollback_readiness: state=missing command=none artifact=none "
        "applied_commit=missing"
    ) in report


def test_rollback_readiness_summary_reports_apply_ready_and_invalid_metadata(
    tmp_path: Path,
):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "apply-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "proposal",
                "candidate_id": 5,
                "decision_id": "decision-1",
                "run_id": "run-1",
                "target_files": ["CODEX.md"],
                "branch_name": "tugboat/run-1",
                "commit_message": "message",
                "pre_hashes": {},
                "post_hashes": {},
                "applied_commit": "",
                "rollback_command": [["tugboat", "rollback", "--decision", "latest"]],
                "provenance_bundle": ".sidecar/runs/run-1/provenance-bundle.json",
                "pr_metadata": {},
                "review_actor": "operator",
                "auto_apply": False,
                "explicit_human_review": False,
                "review_required_reasons": [],
                "decision_rationale": "proposal mode",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert rollback_readiness_summary(tmp_path, run_dir) == (
        "state=apply_ready command=tugboat rollback --decision latest "
        "artifact=.sidecar/runs/run-1/apply-plan.json applied_commit=missing"
    )

    (run_dir / "apply-plan.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    assert rollback_readiness_summary(tmp_path, run_dir) == (
        "state=apply_ready command=none artifact=.sidecar/runs/run-1/apply-plan.json "
        "applied_commit=missing"
    )


def test_rollback_readiness_summary_handles_bad_optional_metadata(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "optimization-summary.json").write_text("not-json\n", encoding="utf-8")

    assert rollback_readiness_summary(tmp_path, run_dir) == (
        "state=missing command=none artifact=none applied_commit=missing"
    )

    (run_dir / "apply-plan.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="apply-plan.json must be a JSON object"):
        rollback_readiness_summary(tmp_path, run_dir)


def test_rollback_readiness_summary_reports_external_repo_artifact_path(
    tmp_path: Path,
):
    run_dir = tmp_path / "outside-run"
    run_dir.mkdir()
    (run_dir / "rollback-plan.json").write_text('{"schema_version":1}\n', encoding="utf-8")

    assert rollback_readiness_summary(tmp_path / "repo", run_dir, applied_commit="abc123") == (
        f"state=applied_ready command=none artifact={run_dir / 'rollback-plan.json'} "
        "applied_commit=present"
    )


def test_write_report_marks_report_private_under_permissive_umask(tmp_path: Path):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 5,
                "governance_passed": True,
                "held_out_score": 0.92,
                "metrics": {},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "unit",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    previous_umask = os.umask(0o022)
    try:
        report_path = write_report(
            tmp_path,
            "run-1",
            candidate=_candidate(),
            decision=PolicyDecision(True, ()),
            eval_report_path=eval_report_path,
        )
    finally:
        os.umask(previous_umask)

    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report_path.parent.stat().st_mode & 0o777 == 0o700


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


def test_write_report_rejects_non_object_eval_report(tmp_path: Path):
    eval_report_path = tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    eval_report_path.parent.mkdir(parents=True)
    eval_report_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="eval report must be a JSON object"):
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


def test_write_report_rejects_secret_in_expected_behavior_change(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        expected_behavior_change="Use token ghp_abcdefghijklmnopqrstuvwx.",
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
