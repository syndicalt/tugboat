import json
from pathlib import Path

import pytest

from tugboat.artifacts import (
    ArtifactValidationError,
    JSON_ARTIFACT_JSON_SCHEMAS,
    validate_json_artifact,
    validate_report_markdown,
    write_json_artifact,
    write_text_artifact,
)


def test_write_json_artifact_creates_parent_and_sorts_keys(tmp_path: Path):
    path = write_json_artifact(tmp_path / "run" / "audit.json", {"z": 1, "a": 2})

    assert path == tmp_path / "run" / "audit.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "z": 1}
    assert path.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "z": 1\n}\n'


def test_write_text_artifact_creates_parent(tmp_path: Path):
    path = write_text_artifact(tmp_path / "run" / "candidate.diff", "diff")

    assert path == tmp_path / "run" / "candidate.diff"
    assert path.read_text(encoding="utf-8") == "diff"


def test_validate_audit_artifact_requires_schema_version():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_json_artifact(
            "audit.json",
            {
                "audit_id": 1,
                "edit_warranted": True,
                "evidence_refs": [],
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.75,
            },
        )


def test_validate_candidate_artifact_rejects_wrong_schema_version():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_json_artifact(
            "candidate.json",
            {
                "schema_version": 999,
                "audit_id": 1,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "diff_hash": "def",
                "risk_class": "instruction_clarification",
                "rationale": "because",
                "sources": [],
            },
        )


def test_validate_eval_report_artifact_accepts_current_schema():
    validate_json_artifact(
        "eval-report.json",
        {
            "schema_version": 1,
            "candidate_id": 1,
            "governance_passed": True,
            "held_out_score": 1.0,
            "metrics": {"governance_regressions": 0},
            "passed": True,
            "recommendation": "accept",
            "suite_id": "governance-regression",
            "trigger_score": 1.0,
        },
    )


def test_validate_policy_gate_artifact_accepts_current_schema():
    validate_json_artifact(
        "policy-gate.json",
        {
            "schema_version": 1,
            "allowed": False,
            "reasons": ["held_out_regression"],
        },
    )


def test_validate_llmff_inspect_artifact_accepts_current_schema():
    validate_json_artifact(
        "llmff-inspect.json",
        {
            "schema_version": 1,
            "manifest_path": ".sidecar/manifests/episode-audit.yaml",
            "manifest_hash": "abc123",
            "network_required": False,
            "inspect": {"manifest": "episode-audit", "providers": []},
        },
    )


def test_validate_eval_suite_artifact_accepts_current_schema():
    validate_json_artifact(
        "eval-suite.json",
        {
            "schema_version": 1,
            "suite_id": "governance-regression",
        },
    )


def test_validate_optimizer_memory_artifact_accepts_current_schema():
    validate_json_artifact(
        "optimizer-memory.json",
        {
            "schema_version": 1,
            "rejected_edits": [
                {
                    "semantic_fingerprint": "abc123",
                    "rejection_reason": "held_out_not_improved",
                    "source_refs": ["audit:1"],
                }
            ],
            "slow_update_notes": ["Prefer smaller edits."],
        },
    )


def test_validate_optimization_summary_artifact_requires_schema_version():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_json_artifact(
            "optimization-summary.json",
            {
                "audit_run": "run-1",
                "candidate_id": 1,
                "decision": "needs_review",
                "held_out_score": 0.9,
                "recommendation": "accept",
                "suite_id": "held-out",
                "trigger_score": 0.7,
            },
        )


def test_validate_optimization_summary_artifact_accepts_current_schema():
    validate_json_artifact(
        "optimization-summary.json",
        {
            "schema_version": 1,
            "audit_run": "run-1",
            "candidate_id": 1,
            "decision": "needs_review",
            "held_out_score": 0.9,
            "recommendation": "accept",
            "suite_id": "held-out",
            "trigger_score": 0.7,
        },
    )


def test_json_artifact_schemas_are_real_json_schema_objects():
    audit_schema = JSON_ARTIFACT_JSON_SCHEMAS["audit.json"]

    assert audit_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert audit_schema["type"] == "object"
    assert "schema_version" in audit_schema["required"]
    assert audit_schema["properties"]["schema_version"]["const"] == 1


def test_validate_json_artifact_rejects_additional_properties():
    with pytest.raises(ArtifactValidationError, match="additional property"):
        validate_json_artifact(
            "eval-report.json",
            {
                "schema_version": 1,
                "candidate_id": 1,
                "governance_passed": True,
                "held_out_score": 1.0,
                "metrics": {},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 1.0,
                "raw_model_payload": "must not appear",
            },
        )


def test_validate_audit_artifact_rejects_non_string_evidence_refs():
    with pytest.raises(ArtifactValidationError, match="evidence_refs"):
        validate_json_artifact(
            "audit.json",
            {
                "schema_version": 1,
                "audit_id": 1,
                "edit_warranted": True,
                "evidence_refs": [7],
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.75,
            },
        )


def test_validate_candidate_artifact_rejects_malformed_sources():
    with pytest.raises(ArtifactValidationError, match="sources"):
        validate_json_artifact(
            "candidate.json",
            {
                "schema_version": 1,
                "audit_id": 1,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "diff_hash": "def",
                "risk_class": "instruction_clarification",
                "rationale": "because",
                "sources": [{"source_id": 123, "trusted": "yes"}],
            },
        )


def test_validate_candidate_artifact_rejects_malformed_bounded_edit_metadata():
    base_payload = {
        "schema_version": 1,
        "audit_id": 1,
        "base_file": "CODEX.md",
        "base_hash": "abc",
        "diff_hash": "def",
        "risk_class": "instruction_clarification",
        "rationale": "because",
        "sources": [{"source_id": "ev_1", "trusted": True}],
    }

    bad_items = [
        {
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 0,
        },
        {
            "operator": "rewrite_everything",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 0,
        },
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": "1",
            "normative_changes": 0,
        },
    ]

    for item in bad_items:
        with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
            validate_json_artifact(
                "candidate.json",
                {**base_payload, "bounded_edit_metadata": [item]},
            )


def test_validate_candidate_artifact_accepts_bounded_edit_metadata():
    validate_json_artifact(
        "candidate.json",
        {
            "schema_version": 1,
            "audit_id": 1,
            "base_file": "CODEX.md",
            "base_hash": "abc",
            "diff_hash": "def",
            "risk_class": "instruction_clarification",
            "rationale": "because",
            "sources": [{"source_id": "ev_1", "trusted": True}],
            "bounded_edit_metadata": [
                {
                    "operator": "add",
                    "file": "CODEX.md",
                    "section": "Testing",
                    "changed_lines": 1,
                    "normative_changes": 0,
                }
            ],
        },
    )


def test_validate_decision_artifact_requires_policy_reasons():
    with pytest.raises(ArtifactValidationError, match="policy_reasons"):
        validate_json_artifact(
            "decision.json",
            {
                "schema_version": 1,
                "candidate_id": 1,
                "decision": "needs_review",
                "policy_allowed": True,
            },
        )


def test_validate_apply_plan_artifact_accepts_vcs_backed_apply_payload():
    validate_json_artifact(
        "apply-plan.json",
        {
            "schema_version": 1,
            "mode": "commit",
            "candidate_id": 7,
            "decision_id": "run-1",
            "run_id": "run-1",
            "target_files": ["CODEX.md"],
            "branch_name": "tugboat/run-1/candidate-7/codex-md",
            "commit_message": "tugboat: apply candidate 7",
            "pre_hashes": {"CODEX.md": "before"},
            "post_hashes": {"CODEX.md": "after"},
            "applied_commit": "abc123",
            "rollback_command": [["git", "revert", "--no-edit", "abc123"]],
            "pr_metadata": {},
            "review_actor": "tugboat",
            "auto_apply": False,
            "explicit_human_review": False,
            "review_required_reasons": [],
            "decision_rationale": "policy gate and eval report passed",
        },
    )


def test_validate_auto_apply_approval_requires_readiness_metrics():
    with pytest.raises(ArtifactValidationError, match="readiness_metrics"):
        validate_json_artifact(
            "auto-apply-approval.json",
            {
                "actor": "operator@example.com",
                "candidate_id": "7",
                "change_class": "A",
                "policy_version": 9,
                "repository": "/repo",
                "rollback_command": ["tugboat", "rollback", "--execute"],
                "vcs": {"branch_name": "branch", "commit_sha": "abc", "mode": "commit"},
            },
        )


def test_validate_rollback_plan_rejects_missing_metadata():
    with pytest.raises(ArtifactValidationError, match="metadata"):
        validate_json_artifact(
            "rollback-plan.json",
            {
                "schema_version": 1,
                "decision_id": "run-1",
                "candidate_id": 7,
                "executed": True,
                "revert_commit": "def456",
            },
        )


def test_validate_report_markdown_requires_sections():
    with pytest.raises(ArtifactValidationError, match="Rationale"):
        validate_report_markdown("# Tugboat Report\n\n- schema_version: 1\n- candidate: CODEX.md\n")


def test_validate_report_markdown_requires_schema_version_marker():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_report_markdown(
            "# Tugboat Report\n\n"
            "- candidate: CODEX.md\n"
            "\n"
            "## Rationale\n\n"
            "Because.\n"
        )
