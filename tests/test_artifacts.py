import json
import os
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
from tugboat.security.secrets import SecretScanError


BOUNDED_EDIT_METADATA = [
    {
        "operator": "add",
        "file": "CODEX.md",
        "section": "Testing",
        "changed_lines": 1,
        "normative_changes": 0,
    }
]


def test_write_json_artifact_creates_parent_and_sorts_keys(tmp_path: Path):
    path = write_json_artifact(tmp_path / "run" / "audit.json", {"z": 1, "a": 2})

    assert path == tmp_path / "run" / "audit.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "z": 1}
    assert path.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "z": 1\n}\n'


def test_write_json_artifact_is_secret_scanned_and_owner_only(tmp_path: Path):
    previous_umask = os.umask(0o022)
    try:
        path = write_json_artifact(tmp_path / "run" / "status-report.json", {"schema_version": 1})
    finally:
        os.umask(previous_umask)

    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SecretScanError):
        write_json_artifact(tmp_path / "run" / "leaked.json", {"token": "sk-" + "a" * 20})
    assert not (tmp_path / "run" / "leaked.json").exists()


def test_write_text_artifact_creates_parent(tmp_path: Path):
    path = write_text_artifact(tmp_path / "run" / "candidate.diff", "diff")

    assert path == tmp_path / "run" / "candidate.diff"
    assert path.read_text(encoding="utf-8") == "diff"


def test_write_text_artifact_is_secret_scanned_and_owner_only(tmp_path: Path):
    previous_umask = os.umask(0o022)
    try:
        path = write_text_artifact(tmp_path / "run" / "candidate.diff", "diff")
    finally:
        os.umask(previous_umask)

    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SecretScanError):
        write_text_artifact(tmp_path / "run" / "leaked.diff", "sk-" + "a" * 20)
    assert not (tmp_path / "run" / "leaked.diff").exists()


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
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
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
            "longitudinal_metrics": {
                "acceptance_rate": 0.5,
                "corpus_growth": 2,
                "duplicate_rule_count": 1,
                "governance_regression_count": 0,
                "mean_changed_lines": 4,
                "recurring_incident_rate": 0.25,
                "rejection_rate": 0.25,
                "rollback_rate": 0.25,
                "user_correction_recurrence": 1,
            },
            "metrics": {"governance_regressions": 0},
            "passed": True,
            "recommendation": "accept",
            "suite_id": "governance-regression",
            "trigger_score": 1.0,
        },
    )


def test_validate_eval_report_artifact_accepts_skill_report():
    skill_report = {
        "schema_version": 1,
        "skill_path": "SKILL.md",
        "passed": False,
        "findings": [
            {
                "code": "skill.trigger.removed",
                "severity": "error",
                "message": "Skill trigger description was removed.",
                "target": "frontmatter.description",
            }
        ],
        "metrics": {
            "trigger_preservation_score": 0.0,
            "executability_score": 1.0,
            "ambiguity_score": 0.5,
            "overfit_risk_score": 0.75,
            "token_footprint_score": 1.0,
            "safety_preservation_score": 0.0,
            "required_sections_passed": 0,
            "forbidden_sections_found": 1,
            "skill_tokens_before": 120,
            "skill_tokens_after": 142,
            "skill_token_delta": 22,
            "skill_token_growth_limit": 320,
        },
        "required_sections": ["frontmatter.name", "frontmatter.description"],
        "forbidden_sections": ["Secrets", "Credentials", "Approval Bypass"],
        "safety_weakening": True,
        "overfit_risk": "medium",
    }

    validate_json_artifact(
        "eval-report.json",
        {
            "schema_version": 1,
            "candidate_id": 1,
            "governance_passed": False,
            "held_out_score": 1.0,
            "metrics": {"governance_regressions": 1},
            "passed": False,
            "recommendation": "reject",
            "skill_report": skill_report,
            "suite_id": "all",
            "trigger_score": 0.0,
        },
    )
    validate_json_artifact(
        "eval-report.raw.json",
        {
            "passed": False,
            "metrics": {"governance_regressions": 1},
            "skill_report": skill_report,
        },
    )


def test_validate_eval_report_artifact_rejects_malformed_skill_report():
    with pytest.raises(ArtifactValidationError, match="skill_report.findings\\[0\\].severity"):
        validate_json_artifact(
            "eval-report.json",
            {
                "schema_version": 1,
                "candidate_id": 1,
                "governance_passed": False,
                "held_out_score": 1.0,
                "metrics": {"governance_regressions": 1},
                "passed": False,
                "recommendation": "reject",
                "skill_report": {
                    "schema_version": 1,
                    "skill_path": "SKILL.md",
                    "passed": False,
                    "findings": [
                        {
                            "code": "skill.trigger.removed",
                            "severity": "critical",
                            "message": "bad",
                        }
                    ],
                    "metrics": {},
                    "required_sections": [],
                    "forbidden_sections": [],
                    "safety_weakening": True,
                    "overfit_risk": "medium",
                },
                "suite_id": "all",
                "trigger_score": 0.0,
            },
        )


def test_validate_unseen_eval_reports_artifact_accepts_current_schema():
    validate_json_artifact(
        "unseen-eval-reports.json",
        {
            "schema_version": 1,
            "reports": [
                {
                    "suite_id": "governance",
                    "passed": True,
                    "governance_passed": True,
                    "recommendation": "accept",
                    "held_out_score": 0.95,
                    "trigger_score": 0.8,
                }
            ],
        },
    )


def test_validate_eval_report_collection_artifact_accepts_current_schema():
    validate_json_artifact(
        "eval-report-collection.json",
        {
            "schema_version": 1,
            "primary_suite": "held-out",
            "reports": [
                {
                    "suite_id": "held-out",
                    "role": "held_out",
                    "path": ".sidecar/runs/run-1/eval-report.json",
                    "passed": True,
                    "governance_passed": True,
                    "recommendation": "accept",
                    "held_out_score": 0.9,
                    "trigger_score": 0.7,
                },
                {
                    "suite_id": "governance",
                    "role": "unseen",
                    "path": ".sidecar/runs/run-1/unseen-evals/governance/eval-report.json",
                    "passed": True,
                    "governance_passed": True,
                    "recommendation": "accept",
                    "held_out_score": 0.95,
                    "trigger_score": 0.8,
                },
            ],
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
            "external_calls": [],
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
                            "source_ref": "CODEX.md#rules",
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


def test_validate_audit_raw_artifact_requires_instruction_refs():
    with pytest.raises(ArtifactValidationError, match="instruction_refs"):
        validate_json_artifact(
            "audit.raw.json",
            {
                "edit_warranted": True,
                "failure_class": "instruction_conflict",
                "severity": "high",
                "confidence": 0.91,
                "evidence_refs": ["ev_fake"],
            },
        )


def test_validate_batch_audit_reports_artifact_accepts_current_schema():
    validate_json_artifact(
        "batch-audit-reports.json",
        {
            "schema_version": 1,
            "primary_audit": "audit.raw.json",
            "reports": [
                {
                    "run_id": "20260527T000000Z",
                    "episode_id": "1",
                    "split": "train",
                    "path": "../20260527T000000Z/audit.raw.json",
                    "evidence_refs": ["ev_train"],
                    "source_refs": ["audit:20260527T000000Z:ev_train"],
                },
                {
                    "run_id": "20260527T000001Z",
                    "episode_id": "2",
                    "split": "trigger",
                    "path": "audit.raw.json",
                    "evidence_refs": ["ev_trigger"],
                    "source_refs": ["audit:20260527T000001Z:ev_trigger"],
                },
            ],
        },
    )


def test_validate_batch_audit_reports_artifact_rejects_unknown_split():
    with pytest.raises(ArtifactValidationError, match="split"):
        validate_json_artifact(
            "batch-audit-reports.json",
            {
                "schema_version": 1,
                "primary_audit": "audit.raw.json",
                "reports": [
                    {
                        "run_id": "20260527T000000Z",
                        "episode_id": "1",
                        "split": "held_out",
                        "path": "audit.raw.json",
                        "evidence_refs": ["ev_held_out"],
                        "source_refs": ["audit:20260527T000000Z:ev_held_out"],
                    }
                ],
            },
        )


def test_validate_instruction_index_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "instruction-index.raw.json",
        {
            "documents": [
                {
                    "path": "CODEX.md",
                    "obligations": ["Use tests."],
                    "chunks": [
                        {
                            "ref": "CODEX.md#rules",
                            "anchor": "rules",
                            "heading_path": ["Rules"],
                        }
                    ],
                }
            ]
        },
    )


def test_validate_instruction_index_raw_artifact_requires_citeable_chunks():
    with pytest.raises(ArtifactValidationError, match="documents\\[0\\].chunks"):
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
            "tool_calls": [],
            "command_outputs": [],
            "diffs": [],
            "test_results": [],
            "policy_events": [],
            "user_corrections": [],
            "subagent_reports": [],
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


def test_validate_canonical_episode_artifact_requires_final_answer():
    with pytest.raises(ArtifactValidationError, match="final_answer"):
        validate_json_artifact(
            "canonical-episode.json",
            {
                "schema_version": 1,
                "trace_path": "trace.jsonl",
                "request": "Fix bug",
                "instruction_snapshot": [{"source": "CODEX.md", "text": "Use tests."}],
                "tool_calls": [],
                "command_outputs": [],
                "diffs": [],
                "test_results": [],
                "policy_events": [],
                "user_corrections": [],
                "subagent_reports": [],
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


def test_validate_reflection_artifact_requires_skillopt_fields():
    with pytest.raises(ArtifactValidationError, match="recurring_failure_patterns"):
        validate_json_artifact(
            "reflection.json",
            {
                "source_ref": "audit:latest",
                "summary": "Tests were skipped because regression guidance was missing.",
            },
        )


def test_validate_reflection_artifact_accepts_current_schema():
    validate_json_artifact(
        "reflection.json",
        {
            "source_ref": "audit:latest",
            "summary": "Tests were skipped because regression guidance was missing.",
            "recurring_failure_patterns": ["Bug fixes close without regression tests."],
            "preserved_success_patterns": ["Keep existing concise test guidance."],
            "affected_instruction_chunks": ["CODEX.md#rules"],
            "proposed_root_cause": "Regression-test expectations were implicit.",
        },
    )


def test_validate_drift_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "drift.raw.json",
        {"clusters": [{"cluster_id": "drift-1", "evidence_refs": ["ev_fake"]}]},
    )


def test_validate_drift_raw_artifact_rejects_non_reviewable_cluster():
    with pytest.raises(ArtifactValidationError, match="clusters\\[0\\].evidence_refs"):
        validate_json_artifact(
            "drift.raw.json",
            {"clusters": [{"cluster_id": "drift-1", "evidence_refs": []}]},
        )


def test_validate_drift_raw_artifact_rejects_duplicate_cluster_ids():
    with pytest.raises(ArtifactValidationError, match="duplicate cluster_id: drift-1"):
        validate_json_artifact(
            "drift.raw.json",
            {
                "clusters": [
                    {"cluster_id": "drift-1", "evidence_refs": ["ev_a"]},
                    {"cluster_id": "drift-1", "evidence_refs": ["ev_b"]},
                ],
            },
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
            "reflections": [
                {
                    "source_ref": "audit:latest",
                    "summary": "Tests were skipped.",
                    "recurring_failure_patterns": ["Bug fixes close without regression tests."],
                    "preserved_success_patterns": ["Keep existing concise test guidance."],
                    "affected_instruction_chunks": ["CODEX.md#rules"],
                    "proposed_root_cause": "Regression-test expectations were implicit.",
                }
            ],
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


def test_validate_candidate_raw_reflections_require_skillopt_fields():
    with pytest.raises(ArtifactValidationError, match="recurring_failure_patterns"):
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


def test_validate_candidate_raw_artifact_requires_bounded_edit_metadata():
    with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": "CODEX.md",
                "base_hash": "abc123",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
            },
        )


def test_validate_candidate_raw_artifact_rejects_empty_bounded_edit_metadata():
    with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": "CODEX.md",
                "base_hash": "abc123",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
                "bounded_edit_metadata": [],
            },
        )


def test_validate_candidate_set_raw_artifact_accepts_current_schema():
    validate_json_artifact(
        "candidate-set.raw.json",
        {
            "candidates": [
                {
                    "candidate_id": "testing",
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
                            "operator": "add",
                            "file": "CODEX.md",
                            "section": "Testing",
                            "changed_lines": 1,
                            "normative_changes": 0,
                        }
                    ],
                }
            ]
        },
    )


def test_validate_candidate_ranking_artifact_accepts_current_schema():
    validate_json_artifact(
        "candidate-ranking.json",
        {
            "schema_version": 1,
            "selected_candidate_ids": ["testing", "review"],
            "merged": True,
            "rejected_candidates": [
                {
                    "candidate_id": "approval",
                    "reasons": ["suppressed_by_rejected_edit_memory"],
                    "suppression_context": [
                        {
                            "future_proposal_suppression_signal": (
                                "suppress_matching_bounded_edit_fingerprint"
                            ),
                            "semantic_fingerprint": "abc123",
                            "rejection_reason": "held_out_not_improved",
                            "source_refs": ["audit:1"],
                            "operator": "delete",
                            "file": "CODEX.md",
                            "section": "Approval",
                            "category": "review_intelligence",
                            "failure_pattern": "regression_missing",
                            "review_actor": "maintainer",
                            "review_template": "default",
                        }
                    ],
                }
            ],
        },
    )


def test_validate_candidate_ranking_artifact_rejects_raw_suppression_payload():
    with pytest.raises(ArtifactValidationError, match="raw_trace_payload"):
        validate_json_artifact(
            "candidate-ranking.json",
            {
                "schema_version": 1,
                "selected_candidate_ids": ["testing"],
                "merged": False,
                "rejected_candidates": [
                    {
                        "candidate_id": "approval",
                        "reasons": ["suppressed_by_rejected_edit_memory"],
                        "suppression_context": [
                            {
                                "future_proposal_suppression_signal": (
                                    "suppress_matching_bounded_edit_fingerprint"
                                ),
                                "semantic_fingerprint": "abc123",
                                "rejection_reason": "held_out_not_improved",
                                "source_refs": ["audit:1"],
                                "raw_trace_payload": "user asked to remove human review",
                            }
                        ],
                    }
                ],
            },
        )


def test_validate_candidate_raw_artifact_rejects_empty_sources():
    with pytest.raises(ArtifactValidationError, match="sources"):
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
                "sources": [],
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
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
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
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
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
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


def test_validate_eval_raw_artifact_accepts_validation_splits():
    payload = {
        "passed": True,
        "metrics": {"governance_regressions": 0, "held_out_cases": 1},
        "validation_splits": {
            "trigger": ["episode:trigger-1"],
            "held_out": ["episode:held-out-1"],
            "custom": ["case:custom"],
        },
        "eval_cases": [
            {
                "case_id": "episode:trigger-1",
                "case_hash": "a" * 64,
                "split_name": "trigger",
            },
            {
                "case_id": "episode:held-out-1",
                "case_hash": "b" * 64,
                "split_name": "held_out",
            },
        ],
    }

    validate_json_artifact("eval-report.raw.json", payload)


def test_validate_eval_raw_artifacts_reject_missing_required_typed_fields():
    with pytest.raises(ArtifactValidationError, match="required"):
        validate_json_artifact("eval-report.raw.json", {})

    with pytest.raises(ArtifactValidationError, match="required"):
        validate_json_artifact("policy-decision.raw.json", {})


def test_validate_eval_raw_artifacts_reject_wrong_field_types():
    with pytest.raises(ArtifactValidationError, match="passed"):
        validate_json_artifact(
            "eval-report.raw.json",
            {"passed": "false", "metrics": {}},
        )

    with pytest.raises(ArtifactValidationError, match="allowed"):
        validate_json_artifact(
            "policy-decision.raw.json",
            {"allowed": "true", "reasons": []},
        )


def test_validate_eval_raw_artifact_rejects_malformed_validation_split_arrays():
    with pytest.raises(ArtifactValidationError, match="validation_splits.trigger"):
        validate_json_artifact(
            "eval-report.raw.json",
            {
                "passed": True,
                "metrics": {"governance_regressions": 0, "held_out_cases": 1},
                "validation_splits": {"trigger": "episode:trigger-1"},
            },
        )


def test_validate_eval_raw_artifact_rejects_malformed_eval_cases():
    with pytest.raises(ArtifactValidationError, match=r"eval_cases\[0\]\.case_hash"):
        validate_json_artifact(
            "eval-report.raw.json",
            {
                "passed": True,
                "metrics": {"governance_regressions": 0, "held_out_cases": 1},
                "eval_cases": [
                    {
                        "case_id": "episode:trigger-1",
                        "case_hash": "not-a-hash",
                        "split_name": "trigger",
                    }
                ],
            },
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
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
                "raw_model_payload": "unbounded side channel",
            },
        )


def test_validate_candidate_raw_artifact_rejects_operator_metadata_alias():
    with pytest.raises(ArtifactValidationError, match="additional property"):
        validate_json_artifact(
            "candidate.raw.json",
            {
                "base_file": "CODEX.md",
                "base_hash": "abc123",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Use tests.\n",
                "risk_class": "instruction_clarification",
                "rationale": "Preserve regression guidance.",
                "expected_behavior_change": "Agents keep regression guidance during bug fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["revert generated diff"],
                "sources": [{"source_id": "ev_fake", "trusted": True}],
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
                "operator_metadata": BOUNDED_EDIT_METADATA,
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
                    "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                    "semantic_fingerprint": "abc123",
                    "rejection_reason": "held_out_not_improved",
                    "source_refs": ["audit:1"],
                }
            ],
            "rejected_clusters": [
                {
                    "cluster_id": "drift-1",
                    "rejection_reason": "redundant_rule",
                    "source_refs": ["candidate:7", "suite:human_review"],
                    "evidence_refs": ["ev_fake"],
                    "category": "policy_regression",
                    "failure_pattern": "duplicates existing guidance",
                    "review_actor": "reviewer",
                }
            ],
            "slow_update_notes": ["Prefer smaller edits."],
            "slow_update_records": [
                {
                    "category": "optimizer_guidance",
                    "note": "Prefer smaller edits.",
                }
            ],
            "validation_baselines": [
                {
                    "candidate_id": 7,
                    "held_out_score": 0.82,
                    "suite_id": "held-out",
                }
            ],
        },
    )


def test_validate_optimizer_memory_artifact_accepts_structured_rejected_edit_context():
    validate_json_artifact(
        "optimizer-memory.json",
        {
            "schema_version": 1,
            "rejected_edits": [
                {
                    "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                    "semantic_fingerprint": "abc123",
                    "rejection_reason": "redundant_rule",
                    "source_refs": ["candidate:7", "suite:human_review"],
                    "operator": "add",
                    "file": "CODEX.md",
                    "section": "Rules",
                    "category": "policy_regression",
                    "failure_pattern": "duplicates existing guidance",
                    "review_actor": "reviewer",
                    "review_template": "redundant-rule",
                }
            ],
            "rejected_clusters": [],
            "slow_update_notes": [],
            "slow_update_records": [],
        },
    )


def test_validate_optimizer_memory_artifact_rejects_unknown_suppression_signal():
    with pytest.raises(ArtifactValidationError, match="future_proposal_suppression_signal"):
        validate_json_artifact(
            "optimizer-memory.json",
            {
                "schema_version": 1,
                "rejected_edits": [
                    {
                        "future_proposal_suppression_signal": "unknown",
                        "semantic_fingerprint": "abc123",
                        "rejection_reason": "held_out_not_improved",
                        "source_refs": ["audit:1"],
                    }
                ],
                "rejected_clusters": [],
                "slow_update_notes": [],
                "slow_update_records": [],
            },
        )


def test_validate_optimizer_memory_artifact_rejects_empty_rejected_cluster_evidence():
    with pytest.raises(ArtifactValidationError, match="rejected_clusters\\[0\\].evidence_refs"):
        validate_json_artifact(
            "optimizer-memory.json",
            {
                "schema_version": 1,
                "rejected_edits": [],
                "rejected_clusters": [
                    {
                        "cluster_id": "drift-1",
                        "evidence_refs": [],
                        "rejection_reason": "redundant_rule",
                        "source_refs": ["candidate:7", "cluster:drift-1", "suite:human_review"],
                    }
                ],
                "slow_update_notes": [],
                "slow_update_records": [],
            },
        )


def test_validate_optimizer_memory_artifact_rejects_raw_rejected_cluster_payload():
    with pytest.raises(ArtifactValidationError, match="raw_trace_payload"):
        validate_json_artifact(
            "optimizer-memory.json",
            {
                "schema_version": 1,
                "rejected_edits": [],
                "rejected_clusters": [
                    {
                        "cluster_id": "drift-1",
                        "evidence_refs": ["ev_fake"],
                        "rejection_reason": "redundant_rule",
                        "source_refs": ["candidate:7", "cluster:drift-1", "suite:human_review"],
                        "raw_trace_payload": "user correction text",
                    }
                ],
                "slow_update_notes": [],
                "slow_update_records": [],
            },
        )


def test_validate_optimizer_memory_artifact_requires_structured_slow_update_records():
    with pytest.raises(ArtifactValidationError, match="slow_update_records"):
        validate_json_artifact(
            "optimizer-memory.json",
            {
                "schema_version": 1,
                "rejected_edits": [],
                "rejected_clusters": [],
                "slow_update_notes": [],
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
                "governance_regression_count": 2,
                "corpus_growth": {"earliest_count": 1, "latest_count": 2, "delta": 1},
                "provider_backend_failure_rate": {"failed": 1, "rate": 1, "total": 1},
                "duplicate_rule_count": 1,
                "stale_doc_count": 1,
                "user_correction_recurrence": {"correction_count": 1},
                "recurring_incident_rate": {
                    "incident_count": 3,
                    "recurring_incident_count": 2,
                    "rate": 0.666667,
                    "unique_incident_class_count": 2,
                },
                "daemon_queue": {
                    "jobs_by_state": {"queued": 1},
                    "oldest_queued_job_id": 1,
                    "kill_switch_enabled": False,
                    "leased_job_count": 0,
                    "stuck_job_count": 0,
                    "oldest_stuck_job_id": None,
                    "oldest_stuck_lease_expires_at": None,
                    "recovery_hint": None,
                },
                "auto_apply_lanes": {
                    "docs_hygiene": {
                        "shadowed": 0,
                        "eligible": 1,
                        "rejected": 0,
                        "staged": 1,
                        "applied": 1,
                        "rolled_back": 0,
                        "paused": 0,
                    }
                },
            },
        },
    )


def test_validate_harness_cleanup_candidates_artifact_accepts_current_schema():
    validate_json_artifact(
        "harness-cleanup-candidates.json",
        {
            "schema_version": 1,
            "structural_eval": {
                "suite_id": "structural",
                "runner": "harness-cleanup-structural",
                "passed": True,
                "candidate_count": 1,
                "evaluated_candidates": ["harness-cleanup-1"],
                "candidate_hashes": {"harness-cleanup-1": "a" * 64},
                "findings": [],
            },
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
                "structural_eval": {
                    "suite_id": "structural",
                    "runner": "harness-cleanup-structural",
                    "passed": False,
                    "candidate_count": 1,
                    "evaluated_candidates": ["harness-cleanup-1"],
                    "candidate_hashes": {"harness-cleanup-1": "a" * 64},
                    "findings": ["harness-cleanup-1: cleanup candidates must remain review-only"],
                },
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


def test_validate_harness_cleanup_proposal_artifact_accepts_current_schema():
    validate_json_artifact(
        "harness-cleanup-proposal.json",
        {
            "schema_version": 1,
            "kind": "cleanup_proposal",
            "candidate_id": "harness-cleanup-1",
            "state": "waiting_review",
            "auto_apply": False,
            "risk_class": "review_required",
            "task": "Add ownership metadata to docs/runbook.md.",
            "source_findings": ["docs/runbook.md is missing ownership metadata."],
            "required_eval_suites": ["structural"],
            "structural_eval": {
                "bundle": ".sidecar/harness-cleanup-candidates.json",
                "candidate_hash": "a" * 64,
                "suite_id": "structural",
            },
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
            "token_metrics": {
                "instruction_corpus_estimated_tokens": 12,
                "active_context_estimated_tokens": 20,
                "duplicate_rule_estimated_tokens": 4,
                "retrieval_pack_estimated_tokens": 20,
                "retrieval_pack_file_count": 2,
                "instruction_files": [
                    {"path": "AGENTS.md", "estimated_tokens": 12, "line_count": 3}
                ],
                "active_context_files": [
                    {"path": "AGENTS.md", "estimated_tokens": 12},
                    {"path": "docs/runbook.md", "estimated_tokens": 8},
                ],
            },
        },
    )


def test_validate_harness_report_artifact_accepts_legacy_without_token_metrics():
    validate_json_artifact(
        "harness-report.json",
        {
            "schema_version": 1,
            "knowledge_map": {"AGENTS.md": ["docs/runbook.md"]},
            "missing_docs": [],
            "stale_docs": [],
            "orphaned_runbooks": [],
            "recurring_failures_without_docs": [],
            "doc_gardening_tasks": [],
        },
    )


def test_validate_harness_report_artifact_accepts_legacy_token_metrics_without_retrieval_pack():
    validate_json_artifact(
        "harness-report.json",
        {
            "schema_version": 1,
            "knowledge_map": {"AGENTS.md": ["docs/runbook.md"]},
            "missing_docs": [],
            "stale_docs": [],
            "orphaned_runbooks": [],
            "recurring_failures_without_docs": [],
            "doc_gardening_tasks": [],
            "token_metrics": {
                "instruction_corpus_estimated_tokens": 12,
                "active_context_estimated_tokens": 20,
                "duplicate_rule_estimated_tokens": 4,
                "instruction_files": [
                    {"path": "AGENTS.md", "estimated_tokens": 12, "line_count": 3}
                ],
                "active_context_files": [
                    {"path": "AGENTS.md", "estimated_tokens": 12},
                    {"path": "docs/runbook.md", "estimated_tokens": 8},
                ],
            },
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


def test_validate_worktree_profile_artifact_accepts_current_schema():
    validate_json_artifact(
        "worktree-profile.json",
        {
            "schema_version": 1,
            "app_boot": {"command": "python -m app"},
            "observability_refs": ["http://127.0.0.1:8000/health"],
            "runs_dir": ".sidecar/runs",
        },
    )


def test_validate_worktree_profile_artifact_requires_schema_version():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_json_artifact(
            "worktree-profile.json",
            {
                "app_boot": {"command": "python -m app"},
                "observability_refs": [],
                "runs_dir": ".sidecar/runs",
            },
        )


def test_validate_status_report_artifact_accepts_current_schema():
    validate_json_artifact(
        "status-report.json",
        {
            "schema_version": 1,
            "mode": "proposal_only",
            "auto_apply": "disabled",
            "indexed_documents": 2,
            "latest_run": {
                "run_id": "run-1",
                "stage": "audit",
                "status": "completed",
            },
            "latest_llmff_job": {
                "manifest_name": "episode-audit.yaml",
                "status": "completed",
            },
            "latest_llmff_exit_code": 0,
            "latest_llmff_failure_kind": None,
            "pending_candidates": 0,
            "retention_candidates": 0,
            "retention_redaction_candidates": 0,
            "manifest_policy": "unrestricted",
        },
    )


def test_validate_status_report_artifact_requires_schema_version():
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        validate_json_artifact(
            "status-report.json",
            {
                "mode": "proposal_only",
                "auto_apply": "disabled",
                "indexed_documents": 0,
                "latest_run": None,
                "latest_llmff_job": None,
                "latest_llmff_exit_code": None,
                "latest_llmff_failure_kind": None,
                "pending_candidates": 0,
                "retention_candidates": 0,
                "retention_redaction_candidates": 0,
                "manifest_policy": "unrestricted",
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


def test_validate_ops_command_bundle_artifact_accepts_current_schema():
    validate_json_artifact(
        "ops-command-bundle.json",
        {
            "schema_version": 1,
            "bundle": {
                "name": "sidecar-backup",
                "commands": [
                    {
                        "label": "create sidecar archive",
                        "argv": ["tar", "-czf", "sidecar-backup.tgz", ".sidecar"],
                    },
                    {
                        "label": "write archive checksum",
                        "argv": ["sha256sum", "sidecar-backup.tgz"],
                        "stdout_path": "sidecar-backup.tgz.sha256",
                    },
                ],
            },
        },
    )


def test_validate_ops_command_bundle_rejects_command_without_argv():
    with pytest.raises(ArtifactValidationError, match="bundle.commands\\[0\\].argv"):
        validate_json_artifact(
            "ops-command-bundle.json",
            {
                "schema_version": 1,
                "bundle": {
                    "name": "sidecar-backup",
                    "commands": [
                        {
                            "label": "create sidecar archive",
                        }
                    ],
                },
            },
        )


def test_validate_retention_report_artifact_accepts_current_schema():
    validate_json_artifact(
        "retention-report.json",
        {
            "schema_version": 1,
            "mode": "dry-run",
            "status": "complete",
            "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
            "deleted": [],
            "redaction_candidates": [
                {
                    "path": ".sidecar/runs/run-1/trace-input.jsonl",
                    "line_number": 1,
                    "kind": "openai_api_key",
                }
            ],
        },
    )


def test_validate_retention_report_accepts_legacy_report_without_redaction_candidates():
    validate_json_artifact(
        "retention-report.json",
        {
            "schema_version": 1,
            "mode": "dry-run",
            "status": "complete",
            "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
            "deleted": [],
        },
    )


def test_validate_retention_report_rejects_unknown_status():
    with pytest.raises(ArtifactValidationError, match="status"):
        validate_json_artifact(
            "retention-report.json",
            {
                "schema_version": 1,
                "mode": "apply",
                "status": "maybe-complete",
                "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
                "deleted": [],
                "redaction_candidates": [],
            },
        )


def test_validate_retention_report_rejects_apply_mode_without_deleted_list():
    with pytest.raises(ArtifactValidationError, match="deleted"):
        validate_json_artifact(
            "retention-report.json",
            {
                "schema_version": 1,
                "mode": "apply",
                "status": "complete",
                "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
            },
        )


def test_validate_mcp_request_artifact_accepts_common_write_intent_shape():
    validate_json_artifact(
        "mcp-request.json",
        {
            "request_id": "mcp-audit-20260526T000000000000Z",
            "kind": "audit",
            "state": "queued",
            "write_intent": True,
            "repo_policy": {
                "path": ".sidecar/policy.yaml",
                "version": 1,
                "hash": None,
            },
            "execution": {
                "kind": "trace_audit",
                "payload": {
                    "trace_artifact_ref": ".sidecar/mcp/episodes/mcp-trace-20260526T000000000000Z.jsonl",
                    "trace_format": "generic-jsonl",
                },
            },
            "trace_id": "mcp-trace-20260526T000000000000Z",
        },
    )


def test_validate_mcp_request_artifact_rejects_missing_repo_policy():
    with pytest.raises(ArtifactValidationError, match="repo_policy"):
        validate_json_artifact(
            "mcp-request.json",
            {
                "request_id": "mcp-audit-20260526T000000000000Z",
                "kind": "audit",
                "state": "queued",
                "write_intent": True,
                "trace_id": "mcp-trace-20260526T000000000000Z",
            },
        )


def test_validate_daemon_discovered_traces_artifact_accepts_current_schema():
    validate_json_artifact(
        "daemon-discovered-traces.json",
        {
            "schema_version": 1,
            "traces": ["/repo/traces/episode.jsonl"],
        },
    )


def test_validate_daemon_discovered_traces_artifact_rejects_legacy_list():
    with pytest.raises(ArtifactValidationError, match="must be a JSON object"):
        validate_json_artifact(
            "daemon-discovered-traces.json",
            ["/repo/traces/episode.jsonl"],  # type: ignore[arg-type]
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
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
            },
        )


def test_validate_candidate_artifact_rejects_empty_sources():
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
                "sources": [],
                "bounded_edit_metadata": BOUNDED_EDIT_METADATA,
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


def test_validate_candidate_artifact_requires_bounded_edit_metadata():
    with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
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
            },
        )


def test_validate_candidate_artifact_rejects_empty_bounded_edit_metadata():
    with pytest.raises(ArtifactValidationError, match="bounded_edit_metadata"):
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
                "bounded_edit_metadata": [],
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
            "provenance_bundle": ".sidecar/runs/run-1/provenance-bundle.json",
            "pr_metadata": {},
            "review_actor": "tugboat",
            "auto_apply": False,
            "explicit_human_review": False,
            "review_required_reasons": [],
            "decision_rationale": "policy gate and eval report passed",
        },
    )


def test_validate_decision_trace_artifact_accepts_provenance_payload():
    validate_json_artifact(
        "decision-trace.json",
        {
            "schema_version": 1,
            "decision_ref": "latest",
            "run_id": "run-1",
            "run": {
                "run_id": "run-1",
                "episode_id": 1,
                "stage": "eval",
                "manifest_hash": "f" * 64,
                "status": "completed",
                "run_dir": ".sidecar/runs/run-1",
                "created_at": "2026-05-26T00:00:00Z",
                "updated_at": "2026-05-26T00:00:00Z",
                "audit_event_sequence": 37,
                "event_hash": "d" * 64,
            },
            "episode": {
                "episode_id": 1,
                "repo_path": ".",
                "trace_path": "trace.jsonl",
                "started_at": "2026-05-26T00:00:00Z",
                "outcome": "captured",
                "summary_hash": "e" * 64,
                "audit_event_sequence": 38,
                "event_hash": "f" * 64,
            },
            "decision": {
                "decision_id": 3,
                "candidate_id": 7,
                "actor": "tugboat",
                "policy": "optimization_acceptance_gate",
                "decision": "needs_review",
                "reason": "held_out_improved",
                "created_at": "2026-05-26T00:00:00Z",
                "applied_commit": "",
                "rollback_ref": "",
                "audit_event_sequence": 42,
                "event_hash": "0" * 64,
            },
            "candidate": {
                "candidate_id": 7,
                "audit_id": 2,
                "base_file": "CODEX.md",
                "base_hash": "1" * 64,
                "diff_hash": "2" * 64,
                "diff_path": ".sidecar/runs/run-1/candidate.diff",
                "risk_class": "instruction_clarification",
                "rationale": "because",
                "state": "needs_review",
                "audit_event_sequence": 41,
                "event_hash": "3" * 64,
            },
            "audit": {
                "audit_id": 2,
                "run_id": "run-1",
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.82,
                "evidence_refs": ["ev_real"],
                "instruction_refs": ["CODEX.md#rules"],
                "audit_event_sequence": 40,
                "event_hash": "4" * 64,
            },
            "trace_events": [
                {
                    "evidence_id": "ev_real",
                    "event_type": "user_correction",
                    "source_trust": "user",
                    "line_number": 2,
                    "payload_snippet": "{\"content\":\"Fix bug\"}",
                    "payload_truncated": False,
                    "audit_event_sequence": 39,
                    "event_hash": "5" * 64,
                }
            ],
            "unresolved_evidence_refs": [],
            "instruction_snapshots": [],
            "instruction_graphs": [],
            "reflections": [],
            "edit_operations": [],
            "candidate_edits": [],
            "evals": [
                {
                    "eval_id": 4,
                    "suite_id": "all",
                    "report_path": ".sidecar/runs/run-1/eval-report.json",
                    "passed": True,
                    "metrics": {"held_out_cases": 3},
                    "audit_event_sequence": 43,
                    "event_hash": "6" * 64,
                }
            ],
            "eval_runs": [],
            "eval_cases": [],
            "validation_splits": [],
            "review_actions": [],
            "rollbacks": [],
            "optimizer_memory": [],
            "llmff_jobs": [
                {
                    "job_id": 5,
                    "manifest_name": "patch-eval.yaml",
                    "manifest_hash": "7" * 64,
                    "status": "completed",
                    "exit_code": 0,
                    "audit_event_sequence": 44,
                    "event_hash": "8" * 64,
                    "events": [
                        {
                            "event_id": 6,
                            "event_type": "run_completed",
                            "audit_event_sequence": 45,
                            "event_hash": "9" * 64,
                        }
                    ],
                    "outputs": [
                        {
                            "output_id": 7,
                            "output_name": "eval_report",
                            "artifact_path": ".sidecar/runs/run-1/eval-report.raw.json",
                            "content_hash": "a" * 64,
                            "audit_event_sequence": 46,
                            "event_hash": "b" * 64,
                        }
                    ],
                }
            ],
            "artifacts": {
                "audit_report": ".sidecar/runs/run-1/audit.json",
                "candidate_diff": ".sidecar/runs/run-1/candidate.diff",
                "candidate_metadata": ".sidecar/runs/run-1/candidate.json",
                "decision_artifact": ".sidecar/runs/run-1/decision.json",
                "eval_report": ".sidecar/runs/run-1/eval-report.json",
                "trace_input": ".sidecar/runs/run-1/trace-input.jsonl",
            },
        },
    )


def test_validate_provenance_bundle_artifact_accepts_apply_evidence_bundle():
    validate_json_artifact(
        "provenance-bundle.json",
        {
            "schema_version": 1,
            "run_id": "run-1",
            "candidate_id": 7,
            "mode": "commit",
            "target_files": ["CODEX.md"],
            "applied_commit": "abc123",
            "rollback_command": [["git", "revert", "--no-edit", "abc123"]],
            "pre_hashes": {"CODEX.md": "before"},
            "post_hashes": {"CODEX.md": "after"},
            "source_artifacts": {
                "apply_plan": {
                    "path": ".sidecar/runs/run-1/apply-plan.json",
                    "sha256": "0" * 64,
                },
                "candidate_diff": {
                    "path": ".sidecar/runs/run-1/candidate.diff",
                    "sha256": "1" * 64,
                },
                "candidate_metadata": {
                    "path": ".sidecar/runs/run-1/candidate.json",
                    "sha256": "2" * 64,
                },
                "eval_report": {
                    "path": ".sidecar/runs/run-1/eval-report.json",
                    "sha256": "3" * 64,
                },
                "policy_gate": {
                    "path": ".sidecar/runs/run-1/policy-gate.json",
                    "sha256": "4" * 64,
                },
            },
        },
    )


def test_validate_apply_plan_artifact_requires_provenance_bundle_link():
    with pytest.raises(ArtifactValidationError, match="provenance_bundle"):
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


def test_validate_provenance_bundle_artifact_rejects_malformed_sha256():
    with pytest.raises(ArtifactValidationError, match="sha256"):
        validate_json_artifact(
            "provenance-bundle.json",
            {
                "schema_version": 1,
                "run_id": "run-1",
                "candidate_id": 7,
                "mode": "commit",
                "target_files": ["CODEX.md"],
                "applied_commit": "abc123",
                "rollback_command": [["git", "revert", "--no-edit", "abc123"]],
                "pre_hashes": {"CODEX.md": "before"},
                "post_hashes": {"CODEX.md": "after"},
                "source_artifacts": {
                    "apply_plan": {
                        "path": ".sidecar/runs/run-1/apply-plan.json",
                        "sha256": "apply-plan-hash",
                    },
                    "candidate_diff": {
                        "path": ".sidecar/runs/run-1/candidate.diff",
                        "sha256": "0" * 64,
                    },
                    "candidate_metadata": {
                        "path": ".sidecar/runs/run-1/candidate.json",
                        "sha256": "1" * 64,
                    },
                    "eval_report": {
                        "path": ".sidecar/runs/run-1/eval-report.json",
                        "sha256": "2" * 64,
                    },
                    "policy_gate": {
                        "path": ".sidecar/runs/run-1/policy-gate.json",
                        "sha256": "3" * 64,
                    },
                },
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
                "harness": {
                    "passed": True,
                    "findings": [],
                    "report_path": ".sidecar/harness-report.json",
                    "report_sha256": "a" * 64,
                    "doc_gardening_task_count": 0,
                },
                "harness_report": {
                    "passed": True,
                    "missing_docs": [],
                    "stale_docs": [],
                    "orphaned_runbooks": [],
                    "recurring_failures_without_docs": [],
                    "doc_gardening_tasks": [],
                },
                "manifest_contracts": {"passed": True, "findings": []},
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
            "reviewer_checklist": [
                "Review candidate diff and proposal rationale against trace evidence.",
                "Confirm risk classification matches the bounded edit.",
                "Verify source evidence supports the recommendation.",
                "Confirm expected behavior change is narrow and intentional.",
                "Confirm rollback command before applying.",
            ],
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


def test_validate_acceptance_summary_raw_artifact_requires_complete_review_checklist():
    with pytest.raises(ArtifactValidationError, match="expected behavior change"):
        validate_json_artifact(
            "acceptance-summary.raw.json",
            {
                "decision_recommendation": "needs_review",
                "reasons": ["policy gate and eval report passed"],
                "evidence": ["audit:1"],
                "reviewer_checklist": [
                    "Review candidate diff and proposal rationale against trace evidence.",
                    "Confirm risk classification matches the bounded edit.",
                    "Verify source evidence supports the recommendation.",
                    "Confirm rollback command before applying.",
                ],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            },
        )


def test_validate_acceptance_summary_raw_artifact_rejects_blank_review_checklist_items():
    with pytest.raises(ArtifactValidationError, match="blank item"):
        validate_json_artifact(
            "acceptance-summary.raw.json",
            {
                "decision_recommendation": "needs_review",
                "reasons": ["policy gate and eval report passed"],
                "evidence": ["audit:1"],
                "reviewer_checklist": [
                    "Review candidate diff and proposal rationale against trace evidence.",
                    "Confirm risk classification matches the bounded edit.",
                    "Verify source evidence supports the recommendation.",
                    "Confirm expected behavior change is narrow and intentional.",
                    "Confirm rollback command before applying.",
                    "   ",
                ],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            },
        )


def test_validate_acceptance_summary_raw_artifact_rejects_empty_review_fields():
    with pytest.raises(ArtifactValidationError, match="evidence"):
        validate_json_artifact(
            "acceptance-summary.raw.json",
            {
                "decision_recommendation": "needs_review",
                "reasons": ["policy gate and eval report passed"],
                "evidence": [],
                "reviewer_checklist": [
                    "Review candidate diff and proposal rationale against trace evidence.",
                    "Confirm risk classification matches the bounded edit.",
                    "Verify source evidence supports the recommendation.",
                    "Confirm expected behavior change is narrow and intentional.",
                    "Confirm rollback command before applying.",
                ],
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
                "lane": "docs_hygiene",
                "vcs": {"branch_name": "branch", "commit_sha": "abc", "mode": "commit"},
            },
        )


def test_validate_auto_apply_preflight_accepts_current_schema():
    validate_json_artifact(
        "auto-apply-preflight.json",
        {
            "schema_version": 1,
            "run_id": "20260531T120000Z",
            "candidate_id": 7,
            "mode": "commit",
            "target_files": ["CODEX.md"],
            "branch_name": "tugboat/run-7-codex",
            "eligible": False,
            "would_apply": False,
            "lane": None,
            "reasons": ["cli_confirmation_required"],
            "approval_bundle": None,
            "checks": {
                "policy_gate": {"allowed": True, "reasons": []},
                "stored_policy_gate": {"allowed": True, "reasons": []},
                "eval_report": {
                    "candidate_id_matches": True,
                    "passed": True,
                    "recommendation": "accept",
                    "suite_id": "all",
                },
                "vcs": {
                    "preflight_passed": True,
                    "worktree_clean": True,
                    "dirty_paths": [],
                    "target_files_clean": True,
                    "base_hashes_match": True,
                    "reasons": [],
                },
                "auto_apply": {
                    "candidate": {"candidate_id": "7"},
                    "readiness": {"confirmed": False},
                },
            },
            "readiness_metrics": {"reviewed_count": 20},
        },
    )


def test_validate_auto_apply_shadow_accepts_current_schema():
    validate_json_artifact(
        "auto-apply-shadow.json",
        {
            "schema_version": 1,
            "run_id": "20260531T120000Z",
            "candidate_id": 7,
            "mode": "commit",
            "target_files": ["CODEX.md"],
            "branch_name": "tugboat/run-7-codex",
            "shadow_mode": True,
            "eligible": True,
            "would_apply": True,
            "lane": "docs_hygiene",
            "reasons": [],
            "approval_bundle": {"actor": "operator@example.com"},
            "checks": {
                "policy_gate": {"allowed": True, "reasons": []},
                "stored_policy_gate": {"allowed": True, "reasons": []},
                "eval_report": {
                    "candidate_id_matches": True,
                    "passed": True,
                    "recommendation": "accept",
                    "suite_id": "all",
                },
                "vcs": {
                    "preflight_passed": True,
                    "worktree_clean": True,
                    "dirty_paths": [],
                    "target_files_clean": True,
                    "base_hashes_match": True,
                    "reasons": [],
                },
                "auto_apply": {"phase": "shadow"},
            },
            "readiness_metrics": {"reviewed_count": 20},
        },
    )


def test_validate_rollback_incident_accepts_current_schema():
    validate_json_artifact(
        "rollback-incident.json",
        {
            "schema_version": 1,
            "decision_id": "20260525T000000000000Z",
            "candidate_id": 7,
            "failure_kind": "git_revert_failed",
            "failure_message": "git revert failed",
            "commit_sha": "abc123",
            "target_files": ["CODEX.md"],
            "rollback_plan_written": False,
            "rollback_applied": False,
            "source_artifacts": {
                "apply_plan": {
                    "path": ".sidecar/runs/20260525T000000000000Z/apply-plan.json",
                    "sha256": "d" * 64,
                }
            },
        },
    )


def test_validate_rollback_incident_rejects_bad_source_artifact_digest():
    with pytest.raises(ArtifactValidationError, match="SHA-256 digest"):
        validate_json_artifact(
            "rollback-incident.json",
            {
                "schema_version": 1,
                "decision_id": "20260525T000000000000Z",
                "candidate_id": 7,
                "failure_kind": "git_revert_failed",
                "failure_message": "git revert failed",
                "commit_sha": "abc123",
                "target_files": ["CODEX.md"],
                "rollback_plan_written": False,
                "rollback_applied": False,
                "source_artifacts": {
                    "apply_plan": {
                        "path": ".sidecar/runs/20260525T000000000000Z/apply-plan.json",
                        "sha256": "def456",
                    }
                },
            },
        )


def test_validate_auto_apply_preflight_requires_vcs_checks():
    with pytest.raises(ArtifactValidationError, match="checks.vcs"):
        validate_json_artifact(
            "auto-apply-preflight.json",
            {
                "schema_version": 1,
                "run_id": "20260531T120000Z",
                "candidate_id": 7,
                "mode": "commit",
                "target_files": ["CODEX.md"],
                "branch_name": "tugboat/run-7-codex",
                "eligible": False,
                "would_apply": False,
                "lane": None,
                "reasons": ["cli_confirmation_required"],
                "approval_bundle": None,
                "checks": {
                    "policy_gate": {"allowed": True, "reasons": []},
                    "stored_policy_gate": {"allowed": True, "reasons": []},
                    "eval_report": {
                        "candidate_id_matches": True,
                        "passed": True,
                        "recommendation": "accept",
                        "suite_id": "all",
                    },
                    "auto_apply": {"candidate": {"candidate_id": "7"}},
                },
                "readiness_metrics": {"reviewed_count": 20},
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


def test_validate_rollback_plan_accepts_source_artifacts():
    validate_json_artifact(
        "rollback-plan.json",
        {
            "schema_version": 1,
            "decision_id": "run-1",
            "candidate_id": 7,
            "metadata": {
                "commit_sha": "abc123",
                "branch_name": "tugboat/run-1/candidate-7/codex-md",
                "commands": [["git", "revert", "--no-edit", "abc123"]],
            },
            "executed": True,
            "revert_commit": "def456",
            "pre_hashes": {"CODEX.md": "a" * 64},
            "post_rollback_hashes": {"CODEX.md": "a" * 64},
            "restored_pre_hashes": True,
            "source_artifacts": {
                "apply_plan": {
                    "path": ".sidecar/runs/run-1/apply-plan.json",
                    "sha256": "0" * 64,
                },
                "provenance_bundle": {
                    "path": ".sidecar/runs/run-1/provenance-bundle.json",
                    "sha256": "1" * 64,
                },
            },
        },
    )


def test_validate_release_artifact_manifest_accepts_current_schema():
    validate_json_artifact(
        "release-artifact-manifest.json",
        {
            "schema_version": 1,
            "artifact_kind": "release_artifact_manifest",
            "package": {"name": "tugboat", "version": "0.1.0"},
            "commit": "abc1234",
            "ci_url": "https://ci.example/runs/1",
            "approver": "release-owner",
            "security_review": {
                "decision": "approved_proposal_only",
                "critical_high_findings": 0,
            },
            "wheel": {
                "path": "/repo/dist/tugboat-0.1.0-py3-none-any.whl",
                "sha256": "a" * 64,
                "size_bytes": 128,
            },
            "smoke_commands": ["tugboat doctor"],
            "retained_evidence": [
                {"path": "/repo/.sidecar/ci/pytest.log", "sha256": "b" * 64, "size_bytes": 12}
            ],
        },
    )


def test_validate_release_artifact_manifest_accepts_provider_backed_evidence():
    validate_json_artifact(
        "release-artifact-manifest.json",
        {
            "schema_version": 1,
            "artifact_kind": "release_artifact_manifest",
            "package": {"name": "tugboat", "version": "0.1.0"},
            "commit": "abc1234",
            "ci_url": "https://ci.example/runs/1",
            "approver": "release-owner",
            "security_review": {
                "decision": "approved_provider_backed",
                "critical_high_findings": 0,
            },
            "wheel": {
                "path": "/repo/dist/tugboat-0.1.0-py3-none-any.whl",
                "sha256": "a" * 64,
                "size_bytes": 128,
            },
            "smoke_commands": ["tugboat doctor"],
            "retained_evidence": [
                {"path": "/repo/.sidecar/ci/pytest.log", "sha256": "b" * 64, "size_bytes": 12}
            ],
            "provider_backed_evidence": [
                {
                    "path": "/repo/.sidecar/ci/llmff-provider-inspect.json",
                    "providers": ["openai"],
                    "external_calls": [{"kind": "model_provider", "target": "openai"}],
                    "network_required": True,
                }
            ],
        },
    )


def test_validate_release_artifact_manifest_rejects_unapproved_security_decision():
    with pytest.raises(ArtifactValidationError, match="security_review.decision"):
        validate_json_artifact(
            "release-artifact-manifest.json",
            {
                "schema_version": 1,
                "artifact_kind": "release_artifact_manifest",
                "package": {"name": "tugboat", "version": "0.1.0"},
                "commit": "abc1234",
                "ci_url": "https://ci.example/runs/1",
                "approver": "release-owner",
                "security_review": {
                    "decision": "rejected",
                    "critical_high_findings": 0,
                },
                "wheel": {
                    "path": "/repo/dist/tugboat-0.1.0-py3-none-any.whl",
                    "sha256": "a" * 64,
                    "size_bytes": 128,
                },
                "smoke_commands": ["tugboat doctor"],
                "retained_evidence": [],
            },
        )


def test_validate_release_artifact_manifest_requires_retained_evidence():
    with pytest.raises(ArtifactValidationError, match="retained_evidence"):
        validate_json_artifact(
            "release-artifact-manifest.json",
            {
                "schema_version": 1,
                "artifact_kind": "release_artifact_manifest",
                "package": {"name": "tugboat", "version": "0.1.0"},
                "commit": "abc1234",
                "ci_url": "https://ci.example/runs/1",
                "approver": "release-owner",
                "security_review": {
                    "decision": "approved_proposal_only",
                    "critical_high_findings": 0,
                },
                "wheel": {
                    "path": "/repo/dist/tugboat-0.1.0-py3-none-any.whl",
                    "sha256": "a" * 64,
                    "size_bytes": 128,
                },
                "smoke_commands": ["tugboat doctor"],
                "retained_evidence": [],
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
