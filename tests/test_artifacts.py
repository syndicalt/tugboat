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
                "expected_behavior_change": "Agents preserve regression-test guidance.",
                "evals_required": ["governance-regression"],
                "risk_class": "instruction_clarification",
                "rationale": "because",
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
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


def test_validate_instruction_graph_artifact_accepts_current_schema():
    validate_json_artifact(
        "instruction-graph.json",
        {
            "schema_version": 1,
            "documents": [
                {
                    "path": "CODEX.md",
                    "kind": "codex",
                    "precedence": 90,
                    "protected": True,
                    "hash": "abc123",
                    "parser_version": "markdown-v1",
                    "chunks": [
                        {
                            "heading_path": ["Rules"],
                            "anchor": "rules",
                            "byte_start": 0,
                            "byte_end": 12,
                            "text_hash": "def456",
                        }
                    ],
                }
            ],
        },
    )


def test_validate_audit_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "audit.raw.json",
        {
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_fake"],
            "instruction_refs": ["CODEX.md#rules"],
        },
    )


def test_validate_instruction_index_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "instruction-index.raw.json",
        {"documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]},
    )


def test_validate_canonical_episode_artifact_accepts_current_schema():
    validate_json_artifact(
        "canonical-episode.json",
        {
            "schema_version": 1,
            "trace_path": "trace.jsonl",
            "request": "Fix bug",
            "final_answer": None,
            "instruction_snapshot": [{"source": "CODEX.md", "text": "Use tests."}],
            "events": [
                {
                    "evidence_id": "ev_123",
                    "event_type": "user_request",
                    "source_trust": "user",
                    "line_number": 1,
                    "payload": {"type": "user_request", "content": "Fix bug"},
                }
            ],
            "outcome_labels": [],
            "verifier_scores": {},
        },
    )


def test_validate_reflection_artifact_accepts_current_schema():
    validate_json_artifact(
        "reflection.json",
        {
            "source_ref": "audit:latest",
            "summary": "Tests were skipped because regression guidance was missing.",
        },
    )


def test_validate_drift_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "drift.raw.json",
        {"clusters": [{"cluster_id": "drift-1", "evidence_refs": ["ev_fake"]}]},
    )


def test_validate_candidate_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "candidate.raw.json",
        {
            "base_file": "CODEX.md",
            "base_hash": "abc123",
            "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
            "risk_class": "instruction_clarification",
            "rationale": "Preserve regression guidance.",
            "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["revert generated diff"],
            "sources": [{"source_id": "ev_fake", "trusted": True}],
            "reflections": [{"source_ref": "audit:latest", "summary": "Tests were skipped."}],
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


def test_validate_candidate_raw_artifact_requires_proposal_metadata():
    with pytest.raises(ArtifactValidationError, match="base_hash"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": "CODEX.md",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
            },
        )


def test_validate_candidate_raw_artifact_rejects_wrong_field_types():
    with pytest.raises(ArtifactValidationError, match="base_file"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": 123,
                "base_hash": "abc123",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
            },
        )


def test_validate_candidate_raw_artifact_rejects_malformed_bounded_edit_metadata():
    payload = {
        "base_file": "CODEX.md",
        "base_hash": "abc123",
        "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
        "risk_class": "instruction_clarification",
        "rationale": "Preserve regression guidance.",
        "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
        "evals_required": ["governance-regression"],
        "rollback_plan": ["revert generated diff"],
        "sources": [{"source_id": "ev_fake", "trusted": True}],
        "bounded_edit_metadata": [
            {
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            }
        ],
    }

    with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
        validate_json_artifact("candidate.raw.json", payload)


def test_validate_eval_raw_artifacts_accept_current_schema():
    validate_json_artifact(
        "eval-report.raw.json",
        {
            "passed": False,
            "metrics": {"governance_regressions": 1, "held_out_cases": 3},
        },
    )
    validate_json_artifact(
        "policy-decision.raw.json",
        {"allowed": False, "reasons": ["held_out_regression"]},
    )


def test_validate_candidate_raw_artifact_rejects_unknown_top_level_fields():
    with pytest.raises(ArtifactValidationError, match="additional property"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": "CODEX.md",
                "base_hash": "abc123",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
                "raw_model_payload": "unbounded side channel",
            },
        )


def test_validate_eval_report_raw_artifact_rejects_unknown_top_level_fields():
    with pytest.raises(ArtifactValidationError, match="additional property"):
        validate_json_artifact(
            "eval-report.raw.json",
            {
                "passed": True,
                "metrics": {"governance_regressions": 0},
                "raw_model_payload": "unbounded side channel",
            },
        )


def test_validate_policy_decision_raw_artifact_rejects_unknown_top_level_fields():
    with pytest.raises(ArtifactValidationError, match="additional property"):
        validate_json_artifact(
            "policy-decision.raw.json",
            {
                "allowed": True,
                "reasons": [],
                "raw_model_payload": "unbounded side channel",
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
            "validation_baselines": [
                {
                    "candidate_id": 7,
                    "held_out_score": 0.82,
                    "suite_id": "held-out",
                }
            ],
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
                "governance_passed": True,
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
            "governance_passed": True,
            "held_out_score": 0.9,
            "recommendation": "accept",
            "suite_id": "held-out",
            "trigger_score": 0.7,
            "validation_baseline_score": None,
            "accepted_bounded_edit_metadata": [
                {
                    "changed_lines": 1,
                    "file": "CODEX.md",
                    "normative_changes": 0,
                    "operator": "add",
                    "section": "Testing",
                }
            ],
        },
    )


def test_validate_optimization_summary_artifact_rejects_empty_accepted_metadata():
    with pytest.raises(ArtifactValidationError, match="accepted_bounded_edit_metadata"):
        validate_json_artifact(
            "optimization-summary.json",
            {
                "schema_version": 1,
                "audit_run": "run-1",
                "candidate_id": 1,
                "decision": "needs_review",
                "governance_passed": True,
                "held_out_score": 0.9,
                "recommendation": "accept",
                "suite_id": "held-out",
                "trigger_score": 0.7,
                "accepted_bounded_edit_metadata": [],
            },
        )


def test_validate_observability_summary_artifact_accepts_current_schema():
    validate_json_artifact(
        "observability-summary.json",
        {
            "schema_version": 1,
            "summary": {
                "run_duration": {
                    "count": 1,
                    "total_seconds": 12,
                    "average_seconds": 12,
                    "max_seconds": 12,
                },
                "failure_kind_counts": {"provider_error": 1},
                "edits": {"accepted": 1, "rejected": 1, "rolled_back": 0},
                "edit_rates": {
                    "acceptance_rate": 0.5,
                    "rejection_rate": 0.5,
                    "rollback_rate": 0,
                    "reviewed_count": 2,
                },
                "mean_changed_lines": 4,
                "eval_suite_trends": {"all": {"latest_score": 0.9}},
                "corpus_growth": {"earliest_count": 1, "latest_count": 2, "delta": 1},
                "provider_backend_failure_rate": {"failed": 1, "rate": 1, "total": 1},
                "duplicate_rule_count": 1,
                "user_correction_recurrence": {"correction_count": 1},
                "recurring_incident_rate": {
                    "incident_count": 3,
                    "recurring_incident_count": 2,
                    "rate": 0.666667,
                    "unique_incident_class_count": 2,
                },
            },
        },
    )


def test_validate_harness_cleanup_candidates_artifact_accepts_current_schema():
    validate_json_artifact(
        "harness-cleanup-candidates.json",
        {
            "schema_version": 1,
            "candidates": [
                {
                    "candidate_id": "harness-cleanup-1",
                    "risk_class": "review_required",
                    "auto_apply": False,
                    "task": "Add ownership metadata to docs/runbook.md.",
                    "source_findings": ["docs/runbook.md is missing ownership metadata."],
                    "required_eval_suites": ["structural"],
                }
            ],
        },
    )


def test_validate_harness_cleanup_candidates_rejects_auto_apply_candidate():
    with pytest.raises(ArtifactValidationError, match="auto_apply"):
        validate_json_artifact(
            "harness-cleanup-candidates.json",
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "candidate_id": "harness-cleanup-1",
                        "risk_class": "review_required",
                        "auto_apply": True,
                        "task": "Add ownership metadata to docs/runbook.md.",
                        "source_findings": ["docs/runbook.md is missing ownership metadata."],
                        "required_eval_suites": ["structural"],
                    }
                ],
            },
        )


def test_validate_harness_report_artifact_accepts_current_schema():
    validate_json_artifact(
        "harness-report.json",
        {
            "schema_version": 1,
            "knowledge_map": {"AGENTS.md": ["docs/runbook.md"]},
            "missing_docs": ["docs/missing.md"],
            "stale_docs": ["docs/runbook.md is missing ownership metadata."],
            "orphaned_runbooks": ["docs/orphan.md"],
            "recurring_failures_without_docs": [
                "approval-boundary: Approval corrections repeated."
            ],
            "doc_gardening_tasks": ["Add ownership metadata to docs/runbook.md."],
        },
    )


def test_validate_harness_report_rejects_non_string_findings():
    with pytest.raises(ArtifactValidationError, match="stale_docs"):
        validate_json_artifact(
            "harness-report.json",
            {
                "schema_version": 1,
                "knowledge_map": {"AGENTS.md": ["docs/runbook.md"]},
                "missing_docs": [],
                "stale_docs": [123],
                "orphaned_runbooks": [],
                "recurring_failures_without_docs": [],
                "doc_gardening_tasks": [],
            },
        )


def test_validate_harness_report_rejects_invalid_knowledge_map_entries():
    with pytest.raises(ArtifactValidationError, match="knowledge_map.AGENTS.md"):
        validate_json_artifact(
            "harness-report.json",
            {
                "schema_version": 1,
                "knowledge_map": {"AGENTS.md": "docs/runbook.md"},
                "missing_docs": [],
                "stale_docs": [],
                "orphaned_runbooks": [],
                "recurring_failures_without_docs": [],
                "doc_gardening_tasks": [],
            },
        )


def test_validate_sidecar_migration_report_artifact_accepts_current_schema():
    validate_json_artifact(
        "sidecar-migration-report.json",
        {
            "schema_version": 1,
            "artifact_kind": "sidecar_migration_report",
            "current_version": 1,
            "target_version": 3,
            "applied_migrations": [
                {
                    "migration_id": "sidecar-v1-to-v2",
                    "from_version": 1,
                    "to_version": 2,
                    "description": "introduce explicit sidecar schema marker",
                    "actions": [
                        "read legacy policy and artifact layout",
                        "write schema marker after migration execution",
                    ],
                }
            ],
            "version_marker": ".sidecar/version.json",
        },
    )


def test_validate_sidecar_migration_report_rejects_missing_step_actions():
    with pytest.raises(ArtifactValidationError, match="applied_migrations\\[0\\].actions"):
        validate_json_artifact(
            "sidecar-migration-report.json",
            {
                "schema_version": 1,
                "artifact_kind": "sidecar_migration_report",
                "current_version": 1,
                "target_version": 3,
                "applied_migrations": [
                    {
                        "migration_id": "sidecar-v1-to-v2",
                        "from_version": 1,
                        "to_version": 2,
                        "description": "introduce explicit sidecar schema marker",
                    }
                ],
                "version_marker": ".sidecar/version.json",
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
                "expected_behavior_change": "Agents preserve regression-test guidance.",
                "evals_required": ["governance-regression"],
                "risk_class": "instruction_clarification",
                "rationale": "because",
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "sources": [{"source_id": 123, "trusted": "yes"}],
            },
        )


def test_validate_candidate_artifact_requires_proposal_metadata():
    with pytest.raises(ArtifactValidationError, match="expected_behavior_change"):
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
            },
        )


def test_validate_candidate_artifact_rejects_malformed_bounded_edit_metadata():
    base_payload = {
        "schema_version": 1,
        "audit_id": 1,
        "base_file": "CODEX.md",
        "base_hash": "abc",
        "diff_hash": "def",
        "expected_behavior_change": "Agents preserve regression-test guidance.",
        "evals_required": ["governance-regression"],
        "risk_class": "instruction_clarification",
        "rationale": "because",
        "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
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
            "expected_behavior_change": "Agents preserve regression-test guidance.",
            "evals_required": ["governance-regression"],
            "risk_class": "instruction_clarification",
            "rationale": "because",
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
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


def test_validate_ci_report_artifact_requires_check_results():
    validate_json_artifact(
        "ci-report.json",
        {
            "schema_version": 1,
            "mode": "ci_check",
            "auto_apply": False,
            "checks": {
                "index": {"passed": True, "indexed_documents": 1},
                "harness": {"passed": True, "findings": []},
                "semantic_policy_lint": {"passed": True, "findings": []},
                "eval": {
                    "passed": True,
                    "candidate": "run-1",
                    "suite_id": "all",
                    "report_path": ".sidecar/runs/run-1/eval-report.json",
                    "trigger_score": 1.0,
                    "held_out_score": 1.0,
                    "governance_passed": True,
                    "recommendation": "accept",
                },
            },
        },
    )
    with pytest.raises(ArtifactValidationError, match="checks"):
        validate_json_artifact(
            "ci-report.json",
            {
                "schema_version": 1,
                "mode": "ci_check",
                "auto_apply": False,
            },
        )


def test_validate_acceptance_summary_raw_artifact_requires_review_bundle():
    validate_json_artifact(
        "acceptance-summary.raw.json",
        {
            "decision_recommendation": "needs_review",
            "reasons": ["policy gate and eval report passed"],
            "evidence": ["audit:1"],
            "reviewer_checklist": ["Review candidate diff"],
            "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
        },
    )
    with pytest.raises(ArtifactValidationError, match="reviewer_checklist"):
        validate_json_artifact(
            "acceptance-summary.raw.json",
            {
                "decision_recommendation": "needs_review",
                "reasons": ["policy gate and eval report passed"],
                "evidence": ["audit:1"],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
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


@pytest.mark.parametrize(
    "text,match",
    [
        (
            "# Tugboat Report\n\n"
            "- schema_version: 1\n"
            "- schema_version: 1\n"
            "- candidate: CODEX.md\n"
            "- risk_class: instruction_clarification\n"
            "- policy_allowed: true\n"
            "- policy_reasons: \n"
            "- eval_report: .sidecar/runs/run-1/eval-report.json\n"
            "\n"
            "## Rationale\n\n"
            "Because.\n",
            "duplicate metadata field",
        ),
        (
            "# Tugboat Report\n\n"
            "- schema_version: 1\n"
            "- candidate: CODEX.md\n"
            "- risk_class: instruction_clarification\n"
            "- policy_allowed: true\n"
            "- policy_reasons: \n"
            "\n"
            "## Rationale\n\n"
            "Because.\n",
            "eval_report",
        ),
        (
            "# Tugboat Report\n\n"
            "- schema_version 1\n"
            "- candidate: CODEX.md\n"
            "- risk_class: instruction_clarification\n"
            "- policy_allowed: true\n"
            "- policy_reasons: \n"
            "- eval_report: .sidecar/runs/run-1/eval-report.json\n"
            "\n"
            "## Rationale\n\n"
            "Because.\n",
            "metadata entry",
        ),
    ],
)
def test_validate_report_markdown_rejects_malformed_metadata(text: str, match: str):
    with pytest.raises(ArtifactValidationError, match=match):
        validate_report_markdown(text)
