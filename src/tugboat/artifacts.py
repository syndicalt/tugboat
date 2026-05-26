from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

BOUNDED_EDIT_OPERATORS = (
    "add",
    "annotate",
    "delete",
    "demote",
    "merge",
    "promote",
    "replace",
    "split",
)

JSON_ARTIFACT_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "audit.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "audit_id",
            "edit_warranted",
            "evidence_refs",
            "failure_class",
            "severity",
            "confidence",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "audit_id": {"type": "integer"},
            "edit_warranted": {"type": "boolean"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "failure_class": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {"type": "number"},
            "instruction_refs": {"type": "array", "items": {"type": "string"}},
            "secret_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "line_number", "kind"],
                    "properties": {
                        "path": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "kind": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "scoring": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["plugin", "label", "metrics", "evidence"],
                    "properties": {
                        "plugin": {"type": "string"},
                        "label": {"type": "string"},
                        "metrics": {"type": "object"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "trace_risk_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["code", "severity", "evidence_id", "message", "source_trust"],
                    "properties": {
                        "code": {"type": "string"},
                        "severity": {"type": "string"},
                        "evidence_id": {"type": "string"},
                        "message": {"type": "string"},
                        "source_trust": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "llmff_exit_code": {"type": "integer"},
            "llmff_failure_kind": {"type": "string"},
            "llmff_failure_message": {"type": "string"},
        },
    },
    "audit.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "edit_warranted",
            "evidence_refs",
            "failure_class",
            "severity",
            "confidence",
        ],
        "properties": {
            "edit_warranted": {"type": "boolean"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "failure_class": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {"type": "number"},
            "instruction_refs": {"type": "array", "items": {"type": "string"}},
        },
    },
    "instruction-index.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["documents"],
        "properties": {
            "documents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "obligations"],
                    "properties": {
                        "path": {"type": "string"},
                        "obligations": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    "canonical-episode.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "trace_path",
            "request",
            "instruction_snapshot",
            "events",
            "outcome_labels",
            "verifier_scores",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "trace_path": {"type": "string"},
            "request": {"type": ["string", "null"]},
            "final_answer": {"type": ["string", "null"]},
            "instruction_snapshot": {"type": "array", "items": {"type": "object"}},
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "evidence_id",
                        "event_type",
                        "source_trust",
                        "line_number",
                        "payload",
                    ],
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "event_type": {"type": "string"},
                        "source_trust": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "payload": {"type": "object"},
                    },
                },
            },
            "outcome_labels": {"type": "array", "items": {"type": "string"}},
            "verifier_scores": {"type": "object"},
        },
    },
    "candidate.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "audit_id",
            "base_file",
            "base_hash",
            "diff_hash",
            "expected_behavior_change",
            "evals_required",
            "risk_class",
            "rationale",
            "rollback_plan",
            "sources",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "audit_id": {"type": "integer"},
            "candidate_id": {"type": "integer"},
            "base_file": {"type": "string"},
            "base_hash": {"type": "string"},
            "diff_hash": {"type": "string"},
            "expected_behavior_change": {"type": "string"},
            "evals_required": {"type": "array", "items": {"type": "string"}},
            "risk_class": {"type": "string"},
            "rationale": {"type": "string"},
            "rollback_plan": {"type": "array", "items": {"type": "string"}},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source_id", "trusted"],
                    "properties": {
                        "source_id": {"type": "string"},
                        "trusted": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
            "pending_audit_eval_definition_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "bounded_edit_metadata": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "operator",
                        "file",
                        "section",
                        "changed_lines",
                        "normative_changes",
                    ],
                    "properties": {
                        "operator": {"type": "string", "enum": list(BOUNDED_EDIT_OPERATORS)},
                        "file": {"type": "string"},
                        "section": {"type": "string"},
                        "changed_lines": {"type": "integer"},
                        "normative_changes": {"type": "integer"},
                    },
                },
            },
        },
    },
    "candidate.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "base_file",
            "base_hash",
            "diff",
            "risk_class",
            "rationale",
            "expected_behavior_change",
            "evals_required",
            "rollback_plan",
            "sources",
        ],
        "properties": {
            "base_file": {"type": "string"},
            "base_hash": {"type": "string"},
            "diff": {"type": "string"},
            "risk_class": {"type": "string"},
            "rationale": {"type": "string"},
            "expected_behavior_change": {"type": "string"},
            "evals_required": {"type": "array", "items": {"type": "string"}},
            "rollback_plan": {"type": "array", "items": {"type": "string"}},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source_id", "trusted"],
                    "properties": {
                        "source_id": {"type": "string"},
                        "trusted": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
            "reflections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source_ref", "summary"],
                    "properties": {
                        "source_ref": {"type": "string"},
                        "summary": {"type": "string"},
                        "recurring_failure_patterns": {"type": "array", "items": {"type": "string"}},
                        "preserved_success_patterns": {"type": "array", "items": {"type": "string"}},
                        "affected_instruction_chunks": {"type": "array", "items": {"type": "string"}},
                        "proposed_root_cause": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "bounded_edit_metadata": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "operator",
                        "file",
                        "section",
                        "changed_lines",
                        "normative_changes",
                    ],
                    "properties": {
                        "operator": {"type": "string", "enum": list(BOUNDED_EDIT_OPERATORS)},
                        "file": {"type": "string"},
                        "section": {"type": "string"},
                        "changed_lines": {"type": "integer"},
                        "normative_changes": {"type": "integer"},
                    },
                },
            },
            "operator_metadata": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "operator",
                        "file",
                        "section",
                        "changed_lines",
                        "normative_changes",
                    ],
                    "properties": {
                        "operator": {"type": "string", "enum": list(BOUNDED_EDIT_OPERATORS)},
                        "file": {"type": "string"},
                        "section": {"type": "string"},
                        "changed_lines": {"type": "integer"},
                        "normative_changes": {"type": "integer"},
                    },
                },
            },
            "pending_audit_eval_definition_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
    "drift.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["clusters"],
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cluster_id", "evidence_refs"],
                    "properties": {
                        "cluster_id": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    "eval-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "candidate_id",
            "governance_passed",
            "held_out_score",
            "metrics",
            "passed",
            "recommendation",
            "suite_id",
            "trigger_score",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "candidate_id": {"type": "integer"},
            "governance_passed": {"type": "boolean"},
            "held_out_score": {"type": "number"},
            "metrics": {"type": "object"},
            "passed": {"type": "boolean"},
            "recommendation": {"type": "string"},
            "suite_id": {"type": "string"},
            "trigger_score": {"type": "number"},
            "live_provider_required": {"type": "boolean"},
        },
    },
    "eval-report.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "passed": {},
            "trigger_score": {},
            "held_out_score": {},
            "governance_passed": {},
            "recommendation": {},
            "metrics": {},
            "live_provider_required": {},
        },
    },
    "policy-decision.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "allowed": {},
            "reasons": {},
        },
    },
    "candidate-preview.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "base_file",
            "base_hash",
            "diff_hash",
            "preview_path",
            "preview_hash",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "base_file": {"type": "string"},
            "base_hash": {"type": "string"},
            "diff_hash": {"type": "string"},
            "preview_path": {"type": "string"},
            "preview_hash": {"type": "string"},
        },
    },
    "policy-gate.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "allowed", "reasons"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "allowed": {"type": "boolean"},
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
    },
    "llmff-inspect.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "manifest_path",
            "manifest_hash",
            "network_required",
            "inspect",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "manifest_path": {"type": "string"},
            "manifest_hash": {"type": "string"},
            "network_required": {"type": "boolean"},
            "inspect": {"type": "object"},
        },
    },
    "instruction-graph.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "documents"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "documents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path",
                        "kind",
                        "precedence",
                        "protected",
                        "hash",
                        "parser_version",
                        "chunks",
                    ],
                    "properties": {
                        "path": {"type": "string"},
                        "kind": {"type": "string"},
                        "precedence": {"type": "integer"},
                        "protected": {"type": "boolean"},
                        "hash": {"type": "string"},
                        "parser_version": {"type": "string"},
                        "chunks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "heading_path",
                                    "anchor",
                                    "byte_start",
                                    "byte_end",
                                    "text_hash",
                                ],
                                "properties": {
                                    "heading_path": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "anchor": {"type": "string"},
                                    "byte_start": {"type": "integer"},
                                    "byte_end": {"type": "integer"},
                                    "text_hash": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "reflection.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["source_ref", "summary"],
        "properties": {
            "source_ref": {"type": "string"},
            "summary": {"type": "string"},
            "recurring_failure_patterns": {"type": "array", "items": {"type": "string"}},
            "preserved_success_patterns": {"type": "array", "items": {"type": "string"}},
            "affected_instruction_chunks": {"type": "array", "items": {"type": "string"}},
            "proposed_root_cause": {"type": "string"},
        },
    },
    "eval-suite.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "suite_id"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "suite_id": {"type": "string"},
        },
    },
    "optimizer-memory.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "rejected_edits", "slow_update_notes"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "rejected_edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "semantic_fingerprint",
                        "rejection_reason",
                        "source_refs",
                    ],
                    "properties": {
                        "semantic_fingerprint": {"type": "string"},
                        "rejection_reason": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "slow_update_notes": {"type": "array", "items": {"type": "string"}},
        },
    },
    "optimization-summary.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "audit_run",
            "candidate_id",
            "decision",
            "governance_passed",
            "held_out_score",
            "recommendation",
            "suite_id",
            "trigger_score",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "audit_run": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "decision": {"type": "string"},
            "governance_passed": {"type": "boolean"},
            "held_out_score": {"type": "number"},
            "recommendation": {"type": "string"},
            "suite_id": {"type": "string"},
            "trigger_score": {"type": "number"},
            "validation_baseline_score": {"type": ["number", "null"]},
        },
    },
    "observability-summary.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "summary"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "run_duration",
                    "failure_kind_counts",
                    "edits",
                    "edit_rates",
                    "mean_changed_lines",
                    "eval_suite_trends",
                    "corpus_growth",
                    "provider_backend_failure_rate",
                    "duplicate_rule_count",
                    "user_correction_recurrence",
                    "recurring_incident_rate",
                ],
                "properties": {
                    "run_duration": {"type": "object"},
                    "failure_kind_counts": {"type": "object"},
                    "edits": {"type": "object"},
                    "edit_rates": {"type": "object"},
                    "mean_changed_lines": {"type": "number"},
                    "eval_suite_trends": {"type": "object"},
                    "corpus_growth": {"type": "object"},
                    "provider_backend_failure_rate": {"type": "object"},
                    "duplicate_rule_count": {"type": "integer"},
                    "user_correction_recurrence": {"type": "object"},
                    "recurring_incident_rate": {"type": "object"},
                },
            },
        },
    },
    "acceptance-summary.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "decision_recommendation",
            "reasons",
            "evidence",
            "reviewer_checklist",
            "rollback_command",
        ],
        "properties": {
            "decision_recommendation": {"type": "string"},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "reviewer_checklist": {"type": "array", "items": {"type": "string"}},
            "rollback_command": {"type": "array", "items": {"type": "string"}},
        },
    },
    "decision.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "candidate_id",
            "decision",
            "policy_allowed",
            "policy_reasons",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "candidate_id": {"type": "integer"},
            "decision": {"type": "string"},
            "policy_allowed": {"type": "boolean"},
            "policy_reasons": {"type": "array", "items": {"type": "string"}},
        },
    },
    "apply-plan.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "mode",
            "candidate_id",
            "decision_id",
            "run_id",
            "target_files",
            "branch_name",
            "commit_message",
            "pre_hashes",
            "post_hashes",
            "applied_commit",
            "rollback_command",
            "pr_metadata",
            "review_actor",
            "auto_apply",
            "explicit_human_review",
            "review_required_reasons",
            "decision_rationale",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "decision_id": {"type": "string"},
            "run_id": {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "branch_name": {"type": "string"},
            "commit_message": {"type": "string"},
            "pre_hashes": {"type": "object"},
            "post_hashes": {"type": "object"},
            "applied_commit": {"type": "string"},
            "rollback_command": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "pr_metadata": {"type": "object"},
            "review_actor": {"type": "string"},
            "auto_apply": {"type": "boolean"},
            "explicit_human_review": {"type": "boolean"},
            "review_required_reasons": {"type": "array", "items": {"type": "string"}},
            "decision_rationale": {"type": "string"},
        },
    },
    "auto-apply-approval.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "actor",
            "candidate_id",
            "change_class",
            "policy_version",
            "repository",
            "rollback_command",
            "vcs",
            "readiness_metrics",
        ],
        "properties": {
            "actor": {"type": "string"},
            "candidate_id": {"type": "string"},
            "change_class": {"type": "string"},
            "policy_version": {"type": "integer"},
            "repository": {"type": "string"},
            "rollback_command": {"type": "array", "items": {"type": "string"}},
            "vcs": {
                "type": "object",
                "additionalProperties": False,
                "required": ["branch_name", "commit_sha", "mode"],
                "properties": {
                    "branch_name": {"type": "string"},
                    "commit_sha": {"type": "string"},
                    "mode": {"type": "string"},
                },
            },
            "readiness_metrics": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "applied_count",
                    "burn_in_days",
                    "rejected_count",
                    "rejection_rate",
                    "reviewed_count",
                    "rollback_count",
                    "rollback_rate",
                    "source_audit_range",
                ],
                "properties": {
                    "applied_count": {"type": "integer"},
                    "burn_in_days": {"type": "integer"},
                    "rejected_count": {"type": "integer"},
                    "rejection_rate": {"type": "number"},
                    "reviewed_count": {"type": "integer"},
                    "rollback_count": {"type": "integer"},
                    "rollback_rate": {"type": "number"},
                    "source_audit_range": {"type": "object"},
                },
            },
        },
    },
    "rollback-plan.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "decision_id",
            "candidate_id",
            "metadata",
            "executed",
            "revert_commit",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "decision_id": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "metadata": {"type": "object"},
            "executed": {"type": "boolean"},
            "revert_commit": {"type": "string"},
        },
    },
}


class ArtifactValidationError(ValueError):
    pass


def load_json_object_artifact(path: Path, artifact_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"{artifact_name} contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{artifact_name} must be a JSON object")
    return payload


def validate_json_artifact(name: str, payload: dict[str, Any]) -> None:
    schema = JSON_ARTIFACT_JSON_SCHEMAS.get(name)
    if schema is None:
        raise ArtifactValidationError(f"unknown artifact schema: {name}")
    if schema.get("type") != "object":
        raise ArtifactValidationError(f"{name} schema must be an object schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ArtifactValidationError(f"{name} schema properties must be an object")
    for field in schema.get("required", []):
        if field not in payload:
            raise ArtifactValidationError(f"{name} missing required field: {field}")
    if schema.get("additionalProperties") is False:
        allowed = set(properties)
        extra = sorted(set(payload) - allowed)
        if extra:
            raise ArtifactValidationError(f"{name} has additional property: {extra[0]}")
    for field, value in payload.items():
        field_schema = properties.get(field)
        if field_schema is None:
            continue
        _validate_schema_value(name, field, field_schema, value)


def validate_report_markdown(text: str) -> None:
    lines = text.splitlines()
    if not lines or lines[0] != "# Tugboat Report":
        raise ArtifactValidationError("report.md missing required section: # Tugboat Report")
    try:
        rationale_index = lines.index("## Rationale")
    except ValueError as exc:
        raise ArtifactValidationError("report.md missing required section: ## Rationale") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:rationale_index]:
        if not line:
            continue
        if not line.startswith("- ") or ": " not in line:
            raise ArtifactValidationError("report.md has malformed metadata entry")
        field, value = line[2:].split(": ", 1)
        if field in metadata:
            raise ArtifactValidationError(f"report.md duplicate metadata field: {field}")
        metadata[field] = value

    required_metadata = (
        "schema_version",
        "candidate",
        "risk_class",
        "policy_allowed",
        "policy_reasons",
        "eval_report",
    )
    for field in required_metadata:
        if field not in metadata:
            raise ArtifactValidationError(f"report.md missing metadata field: {field}")
    if metadata["schema_version"] != str(SCHEMA_VERSION):
        raise ArtifactValidationError("report.md has unsupported schema_version")
    if metadata["policy_allowed"] not in {"true", "false"}:
        raise ArtifactValidationError("report.md metadata field has unsupported value: policy_allowed")
    for field in ("candidate", "risk_class", "eval_report"):
        if not metadata[field]:
            raise ArtifactValidationError(f"report.md metadata field must not be empty: {field}")


def _matches_json_schema_type(value: object, expected_type: object) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_json_schema_type(value, item) for item in expected_type)
    expected_type = str(expected_type)
    if expected_type == "null":
        return value is None
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "string":
        return isinstance(value, str)
    raise ArtifactValidationError(f"unsupported JSON Schema type: {expected_type}")


def _validate_schema_value(
    artifact_name: str,
    field_path: str,
    schema: dict[str, Any],
    value: object,
) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_json_schema_type(value, expected_type):
        raise ArtifactValidationError(f"{artifact_name} field has wrong type: {field_path}")
    if "const" in schema and value != schema["const"]:
        raise ArtifactValidationError(f"{artifact_name} has unsupported schema_version")
    if "enum" in schema:
        allowed_values = schema["enum"]
        if not isinstance(allowed_values, list):
            raise ArtifactValidationError(f"{artifact_name} schema enum must be an array")
        if value not in allowed_values:
            raise ArtifactValidationError(f"{artifact_name} field has unsupported value: {field_path}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema is None:
            return
        if not isinstance(item_schema, dict):
            raise ArtifactValidationError(f"{artifact_name} schema items must be an object")
        for index, item in enumerate(value if isinstance(value, list) else []):
            _validate_schema_value(artifact_name, f"{field_path}[{index}]", item_schema, item)

    if expected_type == "object":
        if not isinstance(value, dict):
            return
        properties = schema.get("properties", {})
        if properties is not None and not isinstance(properties, dict):
            raise ArtifactValidationError(f"{artifact_name} schema properties must be an object")
        for required_field in schema.get("required", []):
            if required_field not in value:
                raise ArtifactValidationError(
                    f"{artifact_name} missing required field: {field_path}.{required_field}"
                )
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ArtifactValidationError(
                    f"{artifact_name} has additional property: {field_path}.{extra[0]}"
                )
        for child_field, child_value in value.items():
            child_schema = properties.get(child_field)
            if isinstance(child_schema, dict):
                _validate_schema_value(
                    artifact_name,
                    f"{field_path}.{child_field}",
                    child_schema,
                    child_value,
                )


def write_json_artifact(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text_artifact(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
