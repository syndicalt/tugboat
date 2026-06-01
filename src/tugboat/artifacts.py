from __future__ import annotations

import copy
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from tugboat.paths import mark_private_file
from tugboat.security.secrets import SecretScanError, scan_text


SCHEMA_VERSION = 1

JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

EVAL_TOKEN_METRIC_PROPERTIES: dict[str, dict[str, str]] = {
    "instruction_tokens_before": {"type": "number"},
    "instruction_tokens_after": {"type": "number"},
    "instruction_token_delta": {"type": "number"},
    "duplicate_rule_tokens_before": {"type": "number"},
    "duplicate_rule_tokens_after": {"type": "number"},
    "duplicate_rule_token_delta": {"type": "number"},
    "instruction_token_growth_reason": {"type": "string"},
    "instruction_token_growth_acceptable": {"type": "number"},
}

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


def _audited_object_schema(
    required: list[str],
    properties: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [*required, "audit_event_sequence", "event_hash"],
        "properties": {
            **properties,
            "audit_event_sequence": {"type": "integer"},
            "event_hash": {"type": "string"},
        },
    }


def _audited_array_schema(
    required: list[str],
    properties: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _audited_object_schema(required, properties),
    }


def _artifact_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "sha256"],
        "properties": {
            "path": {"type": "string"},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }


SKILL_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "skill_path",
        "passed",
        "findings",
        "metrics",
        "required_sections",
        "forbidden_sections",
        "safety_weakening",
        "overfit_risk",
    ],
    "properties": {
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
        "skill_path": {"type": "string", "minLength": 1},
        "passed": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "severity", "message"],
                "properties": {
                    "code": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "message": {"type": "string", "minLength": 1},
                    "target": {"type": "string"},
                },
            },
        },
        "metrics": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "trigger_preservation_score",
                "executability_score",
                "ambiguity_score",
                "overfit_risk_score",
                "token_footprint_score",
                "safety_preservation_score",
                "required_sections_passed",
                "forbidden_sections_found",
                "non_goals_passed",
                "examples_or_fixtures_passed",
                "skill_tokens_before",
                "skill_tokens_after",
                "skill_token_delta",
                "skill_token_growth_limit",
            ],
            "properties": {
                "trigger_preservation_score": {"type": "number"},
                "executability_score": {"type": "number"},
                "ambiguity_score": {"type": "number"},
                "overfit_risk_score": {"type": "number"},
                "token_footprint_score": {"type": "number"},
                "safety_preservation_score": {"type": "number"},
                "required_sections_passed": {"type": "integer"},
                "forbidden_sections_found": {"type": "integer"},
                "non_goals_passed": {"type": "integer"},
                "examples_or_fixtures_passed": {"type": "integer"},
                "skill_tokens_before": {"type": "integer"},
                "skill_tokens_after": {"type": "integer"},
                "skill_token_delta": {"type": "integer"},
                "skill_token_growth_limit": {"type": "integer"},
            },
        },
        "required_sections": {"type": "array", "items": {"type": "string"}},
        "forbidden_sections": {"type": "array", "items": {"type": "string"}},
        "safety_weakening": {"type": "boolean"},
        "overfit_risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}


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
            "instruction_refs",
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
    "batch-audit-reports.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "primary_audit", "reports"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "primary_audit": {"type": "string"},
            "reports": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "run_id",
                        "episode_id",
                        "split",
                        "path",
                        "evidence_refs",
                        "source_refs",
                    ],
                    "properties": {
                        "run_id": {"type": "string"},
                        "episode_id": {"type": "string"},
                        "split": {"type": "string", "enum": ["train", "trigger"]},
                        "path": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    "evidence-ids.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence_ids"],
        "properties": {
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
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
                    "required": ["path", "obligations", "chunks"],
                    "properties": {
                        "path": {"type": "string"},
                        "obligations": {"type": "array", "items": {"type": "string"}},
                        "chunks": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["ref", "heading_path", "anchor"],
                                "properties": {
                                    "ref": {"type": "string"},
                                    "heading_path": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "anchor": {"type": "string"},
                                },
                            },
                        },
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
            "tool_calls",
            "command_outputs",
            "diffs",
            "test_results",
            "policy_events",
            "user_corrections",
            "subagent_reports",
            "final_answer",
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
            "tool_calls": {"type": "array", "items": {"type": "object"}},
            "command_outputs": {"type": "array", "items": {"type": "object"}},
            "diffs": {"type": "array", "items": {"type": "object"}},
            "test_results": {"type": "array", "items": {"type": "object"}},
            "policy_events": {"type": "array", "items": {"type": "object"}},
            "user_corrections": {"type": "array", "items": {"type": "object"}},
            "subagent_reports": {"type": "array", "items": {"type": "object"}},
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
            "bounded_edit_metadata",
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
            "scope_root": {"type": "string"},
            "sources": {
                "type": "array",
                "minItems": 1,
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
                "minItems": 1,
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
                        "scope_root": {"type": "string"},
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
            "bounded_edit_metadata",
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
            "scope_root": {"type": "string"},
            "sources": {
                "type": "array",
                "minItems": 1,
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
                    "required": [
                        "source_ref",
                        "summary",
                        "recurring_failure_patterns",
                        "preserved_success_patterns",
                        "affected_instruction_chunks",
                        "proposed_root_cause",
                    ],
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
                "minItems": 1,
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
                        "scope_root": {"type": "string"},
                    },
                },
            },
            "pending_audit_eval_definition_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
    "candidate-set.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "candidate_id",
                        "base_file",
                        "base_hash",
                        "diff",
                        "risk_class",
                        "rationale",
                        "expected_behavior_change",
                        "evals_required",
                        "rollback_plan",
                        "sources",
                        "bounded_edit_metadata",
                    ],
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "base_file": {"type": "string"},
                        "base_hash": {"type": "string"},
                        "diff": {"type": "string"},
                        "risk_class": {"type": "string"},
                        "rationale": {"type": "string"},
                        "expected_behavior_change": {"type": "string"},
                        "evals_required": {"type": "array", "items": {"type": "string"}},
                        "rollback_plan": {"type": "array", "items": {"type": "string"}},
                        "scope_root": {"type": "string"},
                        "sources": {
                            "type": "array",
                            "minItems": 1,
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
                                "required": [
                                    "source_ref",
                                    "summary",
                                    "recurring_failure_patterns",
                                    "preserved_success_patterns",
                                    "affected_instruction_chunks",
                                    "proposed_root_cause",
                                ],
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
                            "minItems": 1,
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
                                    "scope_root": {"type": "string"},
                                },
                            },
                        },
                        "pending_audit_eval_definition_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "candidate-ranking.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "selected_candidate_ids", "merged", "rejected_candidates"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "selected_candidate_ids": {"type": "array", "items": {"type": "string"}},
            "merged": {"type": "boolean"},
            "rejected_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_id", "reasons"],
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                        "suppression_context": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "future_proposal_suppression_signal",
                                            "semantic_fingerprint",
                                            "rejection_reason",
                                            "source_refs",
                                        ],
                                        "properties": {
                                            "future_proposal_suppression_signal": {
                                                "type": "string",
                                                "const": (
                                                    "suppress_matching_bounded_edit_fingerprint"
                                                ),
                                            },
                                            "semantic_fingerprint": {"type": "string"},
                                            "rejection_reason": {"type": "string"},
                                            "source_refs": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "operator": {"type": "string"},
                                            "file": {"type": "string"},
                                            "section": {"type": "string"},
                                            "category": {"type": "string"},
                                            "failure_pattern": {"type": "string"},
                                            "review_actor": {"type": "string"},
                                            "review_template": {"type": "string"},
                                        },
                                    },
                                    {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "cluster_id",
                                            "evidence_refs",
                                            "rejection_reason",
                                            "source_refs",
                                        ],
                                        "properties": {
                                            "cluster_id": {"type": "string"},
                                            "evidence_refs": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "rejection_reason": {"type": "string"},
                                            "source_refs": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "category": {"type": "string"},
                                            "failure_pattern": {"type": "string"},
                                            "review_actor": {"type": "string"},
                                            "review_template": {"type": "string"},
                                        },
                                    },
                                ],
                            },
                        },
                    },
                },
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
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cluster_id", "evidence_refs"],
                    "properties": {
                        "cluster_id": {"type": "string", "minLength": 1},
                        "evidence_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
    "optimizer-notes.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["notes"],
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["summary", "evidence_refs"],
                    "properties": {
                        "summary": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    "proposal-rationale.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "evidence_refs", "style_constraints"],
        "properties": {
            "rationale": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "style_constraints": {"type": "array", "items": {"type": "string"}},
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
            "metrics": {
                "type": "object",
                "properties": EVAL_TOKEN_METRIC_PROPERTIES,
            },
            "skill_report": SKILL_REPORT_SCHEMA,
            "longitudinal_metrics": {"type": "object"},
            "passed": {"type": "boolean"},
            "recommendation": {"type": "string"},
            "suite_id": {"type": "string"},
            "trigger_score": {"type": "number"},
            "validation_splits": {
                "type": "object",
                "properties": {
                    "trigger": {"type": "array", "items": {"type": "string"}},
                    "held_out": {"type": "array", "items": {"type": "string"}},
                    "governance": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
            "eval_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["case_id", "case_hash", "split_name"],
                    "properties": {
                        "case_id": {"type": "string", "minLength": 1},
                        "case_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "split_name": {"type": "string", "minLength": 1},
                    },
                },
            },
            "live_provider_required": {"type": "boolean"},
        },
    },
    "eval-report.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["passed", "metrics"],
        "properties": {
            "passed": {"type": "boolean"},
            "trigger_score": {"type": "number"},
            "held_out_score": {"type": "number"},
            "governance_passed": {"type": "boolean"},
            "recommendation": {"type": "string"},
            "metrics": {
                "type": "object",
                "properties": EVAL_TOKEN_METRIC_PROPERTIES,
            },
            "skill_report": SKILL_REPORT_SCHEMA,
            "validation_splits": {
                "type": "object",
                "properties": {
                    "trigger": {"type": "array", "items": {"type": "string"}},
                    "held_out": {"type": "array", "items": {"type": "string"}},
                    "governance": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
            "eval_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["case_id", "case_hash", "split_name"],
                    "properties": {
                        "case_id": {"type": "string", "minLength": 1},
                        "case_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "split_name": {"type": "string", "minLength": 1},
                    },
                },
            },
            "live_provider_required": {"type": "boolean"},
        },
    },
    "policy-decision.raw.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["allowed", "reasons"],
        "properties": {
            "allowed": {"type": "boolean"},
            "reasons": {"type": "array", "items": {"type": "string"}},
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
            "scope_root": {"type": "string"},
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
            "external_calls",
            "inspect",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "manifest_path": {"type": "string"},
            "manifest_hash": {"type": "string"},
            "network_required": {"type": "boolean"},
            "external_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "target"],
                    "properties": {
                        "kind": {"type": "string", "minLength": 1},
                        "target": {"type": "string", "minLength": 1},
                    },
                },
            },
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
                                    "source_ref",
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
                                    "source_ref": {"type": "string"},
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
        "required": [
            "source_ref",
            "summary",
            "recurring_failure_patterns",
            "preserved_success_patterns",
            "affected_instruction_chunks",
            "proposed_root_cause",
        ],
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
        "required": [
            "schema_version",
            "rejected_edits",
            "rejected_clusters",
            "slow_update_notes",
            "slow_update_records",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "rejected_edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "future_proposal_suppression_signal",
                        "semantic_fingerprint",
                        "rejection_reason",
                        "source_refs",
                    ],
                    "properties": {
                        "future_proposal_suppression_signal": {
                            "type": "string",
                            "const": "suppress_matching_bounded_edit_fingerprint",
                        },
                        "semantic_fingerprint": {"type": "string"},
                        "rejection_reason": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "operator": {"type": "string"},
                        "file": {"type": "string"},
                        "section": {"type": "string"},
                        "category": {"type": "string"},
                        "failure_pattern": {"type": "string"},
                        "review_actor": {"type": "string"},
                        "review_template": {"type": "string"},
                    },
                },
            },
            "rejected_clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "cluster_id",
                        "rejection_reason",
                        "source_refs",
                        "evidence_refs",
                    ],
                    "properties": {
                        "cluster_id": {"type": "string", "minLength": 1},
                        "rejection_reason": {"type": "string", "minLength": 1},
                        "source_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "evidence_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "category": {"type": "string", "minLength": 1},
                        "failure_pattern": {"type": "string", "minLength": 1},
                        "review_actor": {"type": "string", "minLength": 1},
                        "review_template": {"type": "string", "minLength": 1},
                    },
                },
            },
            "slow_update_notes": {"type": "array", "items": {"type": "string"}},
            "slow_update_records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "note"],
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["successful", "rejected", "optimizer_guidance"],
                        },
                        "note": {"type": "string"},
                    },
                },
            },
            "validation_baselines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_id", "held_out_score", "suite_id"],
                    "properties": {
                        "candidate_id": {"type": ["integer", "null"]},
                        "held_out_score": {"type": "number"},
                        "suite_id": {"type": "string"},
                    },
                },
            },
        },
    },
    "optimization-batch.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "train_episodes",
            "held_out_episodes",
            "unseen_suites",
            "held_out_suite",
            "success_episodes",
            "failure_episodes",
            "success_patterns",
            "failure_patterns",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "train_episodes": {"type": "array", "items": {"type": "string"}},
            "held_out_episodes": {"type": "array", "items": {"type": "string"}},
            "unseen_suites": {"type": "array", "items": {"type": "string"}},
            "held_out_suite": {"type": "string"},
            "success_episodes": {"type": "array", "items": {"type": "string"}},
            "failure_episodes": {"type": "array", "items": {"type": "string"}},
            "success_patterns": {"type": "array", "items": {"type": "string"}},
            "failure_patterns": {"type": "array", "items": {"type": "string"}},
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
            "reflection_artifact_path": {"type": "string"},
            "suite_id": {"type": "string"},
            "trigger_score": {"type": "number"},
            "validation_baseline_score": {"type": ["number", "null"]},
            "acceptance_decision_recommendation": {"type": "string"},
            "acceptance_evidence": {"type": "array", "items": {"type": "string"}},
            "acceptance_reasons": {"type": "array", "items": {"type": "string"}},
            "acceptance_summary_path": {"type": "string"},
            "accepted_bounded_edit_metadata": {
                "type": "array",
                "minItems": 1,
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
                        "operator": {"type": "string"},
                        "file": {"type": "string"},
                        "section": {"type": "string"},
                        "changed_lines": {"type": "integer"},
                        "normative_changes": {"type": "integer"},
                    },
                },
            },
            "reviewer_checklist": {"type": "array", "items": {"type": "string"}},
            "rollback_command": {"type": "array", "items": {"type": "string"}},
            "unseen_suite_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "suite_id",
                        "passed",
                        "governance_passed",
                        "recommendation",
                        "held_out_score",
                        "trigger_score",
                    ],
                    "properties": {
                        "suite_id": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "governance_passed": {"type": "boolean"},
                        "recommendation": {"type": "string"},
                        "held_out_score": {"type": "number"},
                        "trigger_score": {"type": "number"},
                    },
                },
            },
        },
    },
    "unseen-eval-reports.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "reports"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "reports": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "suite_id",
                        "passed",
                        "governance_passed",
                        "recommendation",
                        "held_out_score",
                        "trigger_score",
                    ],
                    "properties": {
                        "suite_id": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "governance_passed": {"type": "boolean"},
                        "recommendation": {"type": "string"},
                        "held_out_score": {"type": "number"},
                        "trigger_score": {"type": "number"},
                    },
                },
            },
        },
    },
    "eval-report-collection.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "primary_suite", "reports"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "primary_suite": {"type": "string"},
            "reports": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "suite_id",
                        "role",
                        "path",
                        "passed",
                        "governance_passed",
                        "recommendation",
                        "held_out_score",
                        "trigger_score",
                    ],
                    "properties": {
                        "suite_id": {"type": "string"},
                        "role": {"type": "string"},
                        "path": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "governance_passed": {"type": "boolean"},
                        "recommendation": {"type": "string"},
                        "held_out_score": {"type": "number"},
                        "trigger_score": {"type": "number"},
                    },
                },
            },
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
                    "governance_regression_count",
                    "corpus_growth",
                    "provider_backend_failure_rate",
                    "duplicate_rule_count",
                    "stale_doc_count",
                    "user_correction_recurrence",
                    "recurring_incident_rate",
                    "auto_apply_lanes",
                ],
                "properties": {
                    "run_duration": {"type": "object"},
                    "failure_kind_counts": {"type": "object"},
                    "edits": {"type": "object"},
                    "edit_rates": {"type": "object"},
                    "mean_changed_lines": {"type": "number"},
                    "eval_suite_trends": {"type": "object"},
                    "governance_regression_count": {"type": "integer"},
                    "corpus_growth": {"type": "object"},
                    "provider_backend_failure_rate": {"type": "object"},
                    "duplicate_rule_count": {"type": "integer"},
                    "stale_doc_count": {"type": "integer"},
                    "user_correction_recurrence": {"type": "object"},
                    "recurring_incident_rate": {"type": "object"},
                    "auto_apply_lanes": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "shadowed",
                                "eligible",
                                "rejected",
                                "staged",
                                "applied",
                                "rolled_back",
                                "paused",
                            ],
                            "properties": {
                                "shadowed": {"type": "integer"},
                                "eligible": {"type": "integer"},
                                "rejected": {"type": "integer"},
                                "staged": {"type": "integer"},
                                "applied": {"type": "integer"},
                                "rolled_back": {"type": "integer"},
                                "paused": {"type": "integer"},
                            },
                        },
                    },
                    "daemon_queue": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "jobs_by_state",
                            "oldest_queued_job_id",
                            "kill_switch_enabled",
                            "leased_job_count",
                            "stuck_job_count",
                            "oldest_stuck_job_id",
                            "oldest_stuck_lease_expires_at",
                            "recovery_hint",
                        ],
                        "properties": {
                            "jobs_by_state": {"type": "object"},
                            "oldest_queued_job_id": {"type": ["integer", "null"]},
                            "kill_switch_enabled": {"type": "boolean"},
                            "leased_job_count": {"type": "integer"},
                            "stuck_job_count": {"type": "integer"},
                            "oldest_stuck_job_id": {"type": ["integer", "null"]},
                            "oldest_stuck_lease_expires_at": {"type": ["string", "null"]},
                            "recovery_hint": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
    },
    "release-artifact-manifest.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "artifact_kind",
            "package",
            "commit",
            "ci_url",
            "approver",
            "security_review",
            "wheel",
            "smoke_commands",
            "retained_evidence",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "artifact_kind": {"type": "string", "const": "release_artifact_manifest"},
            "package": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version"],
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                },
            },
            "commit": {"type": "string"},
            "ci_url": {"type": "string"},
            "approver": {"type": "string"},
            "security_review": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "critical_high_findings"],
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["approved_proposal_only", "approved_provider_backed"],
                    },
                    "critical_high_findings": {"type": "integer"},
                },
            },
            "wheel": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "sha256", "size_bytes"],
                "properties": {
                    "path": {"type": "string"},
                    "sha256": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                },
            },
            "smoke_commands": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "retained_evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "sha256", "size_bytes"],
                    "properties": {
                        "path": {"type": "string"},
                        "sha256": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                    },
                },
            },
            "provider_backed_evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path",
                        "providers",
                        "external_calls",
                        "network_required",
                        "manifest_hash",
                    ],
                    "properties": {
                        "path": {"type": "string"},
                        "providers": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "external_calls": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["kind", "target"],
                                "properties": {
                                    "kind": {"type": "string", "const": "model_provider"},
                                    "target": {"type": "string"},
                                },
                            },
                        },
                        "network_required": {"type": "boolean", "const": True},
                        "manifest_hash": {"type": "string"},
                    },
                },
            },
        },
    },
    "harness-cleanup-candidates.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "structural_eval", "candidates"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "structural_eval": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "suite_id",
                    "runner",
                    "passed",
                    "candidate_count",
                    "evaluated_candidates",
                    "candidate_hashes",
                    "findings",
                ],
                "properties": {
                    "suite_id": {"type": "string", "const": "structural"},
                    "runner": {"type": "string", "const": "harness-cleanup-structural"},
                    "passed": {"type": "boolean"},
                    "candidate_count": {"type": "integer"},
                    "evaluated_candidates": {"type": "array", "items": {"type": "string"}},
                    "candidate_hashes": {"type": "object"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "candidate_id",
                        "risk_class",
                        "auto_apply",
                        "task",
                        "source_findings",
                        "required_eval_suites",
                    ],
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "risk_class": {"type": "string", "enum": ["review_required"]},
                        "auto_apply": {"type": "boolean", "enum": [False]},
                        "task": {"type": "string"},
                        "source_findings": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "required_eval_suites": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "harness-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "knowledge_map",
            "missing_docs",
            "stale_docs",
            "orphaned_runbooks",
            "recurring_failures_without_docs",
            "doc_gardening_tasks",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "knowledge_map": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "missing_docs": {"type": "array", "items": {"type": "string"}},
            "stale_docs": {"type": "array", "items": {"type": "string"}},
            "orphaned_runbooks": {"type": "array", "items": {"type": "string"}},
            "recurring_failures_without_docs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "doc_gardening_tasks": {"type": "array", "items": {"type": "string"}},
            "token_metrics": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "instruction_corpus_estimated_tokens",
                    "active_context_estimated_tokens",
                    "duplicate_rule_estimated_tokens",
                    "instruction_files",
                    "active_context_files",
                ],
                "properties": {
                    "instruction_corpus_estimated_tokens": {"type": "integer"},
                    "active_context_estimated_tokens": {"type": "integer"},
                    "duplicate_rule_estimated_tokens": {"type": "integer"},
                    "retrieval_pack_estimated_tokens": {"type": "integer"},
                    "retrieval_pack_file_count": {"type": "integer"},
                    "token_budget": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "instruction_file_estimated_tokens",
                            "active_context_estimated_tokens",
                            "retrieval_pack_estimated_tokens",
                        ],
                        "properties": {
                            "instruction_file_estimated_tokens": {"type": "integer"},
                            "active_context_estimated_tokens": {"type": "integer"},
                            "retrieval_pack_estimated_tokens": {"type": "integer"},
                            "duplicate_rule_estimated_tokens": {"type": "integer"},
                        },
                    },
                    "token_budget_violations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "instruction_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path", "estimated_tokens", "line_count"],
                            "properties": {
                                "path": {"type": "string"},
                                "estimated_tokens": {"type": "integer"},
                                "line_count": {"type": "integer"},
                            },
                        },
                    },
                    "active_context_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path", "estimated_tokens"],
                            "properties": {
                                "path": {"type": "string"},
                                "estimated_tokens": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
    },
    "harness-cleanup-proposal.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "kind",
            "candidate_id",
            "state",
            "auto_apply",
            "risk_class",
            "task",
            "source_findings",
            "required_eval_suites",
            "structural_eval",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "kind": {"type": "string", "const": "cleanup_proposal"},
            "candidate_id": {"type": "string"},
            "state": {"type": "string", "const": "waiting_review"},
            "auto_apply": {"type": "boolean", "enum": [False]},
            "risk_class": {"type": "string", "enum": ["review_required"]},
            "task": {"type": "string"},
            "source_findings": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "required_eval_suites": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "structural_eval": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bundle", "candidate_hash", "suite_id"],
                "properties": {
                    "bundle": {"type": "string"},
                    "candidate_hash": {"type": "string"},
                    "suite_id": {"type": "string", "const": "structural"},
                },
            },
        },
    },
    "worktree-profile.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "app_boot",
            "observability_refs",
            "runs_dir",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "app_boot": {"type": "object"},
            "observability_refs": {"type": "array", "items": {"type": "string"}},
            "runs_dir": {"type": "string", "const": ".sidecar/runs"},
        },
    },
    "status-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "mode",
            "auto_apply",
            "indexed_documents",
            "latest_run",
            "latest_llmff_job",
            "latest_llmff_exit_code",
            "latest_llmff_failure_kind",
            "pending_candidates",
            "retention_candidates",
            "retention_redaction_candidates",
            "manifest_policy",
            "daemon_queue",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string"},
            "auto_apply": {"type": "string", "enum": ["enabled", "disabled"]},
            "indexed_documents": {"type": "integer"},
            "latest_run": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["run_id", "stage", "status"],
                "properties": {
                    "run_id": {"type": "string"},
                    "stage": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
            "latest_llmff_job": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["manifest_name", "status"],
                "properties": {
                    "manifest_name": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
            "latest_llmff_exit_code": {"type": ["integer", "null"]},
            "latest_llmff_failure_kind": {"type": ["string", "null"]},
            "pending_candidates": {"type": "integer"},
            "retention_candidates": {"type": "integer"},
            "retention_redaction_candidates": {"type": "integer"},
            "manifest_policy": {"type": "string"},
            "daemon_queue": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "queue_path",
                    "kill_switch_enabled",
                    "jobs_by_state",
                    "oldest_queued_job_id",
                    "leased_job_count",
                    "stuck_job_count",
                    "oldest_stuck_job_id",
                    "oldest_stuck_lease_expires_at",
                    "recovery_hint",
                ],
                "properties": {
                    "queue_path": {"type": "string"},
                    "kill_switch_enabled": {"type": "boolean"},
                    "jobs_by_state": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                    },
                    "oldest_queued_job_id": {"type": ["integer", "null"]},
                    "leased_job_count": {"type": "integer"},
                    "stuck_job_count": {"type": "integer"},
                    "oldest_stuck_job_id": {"type": ["integer", "null"]},
                    "oldest_stuck_lease_expires_at": {"type": ["string", "null"]},
                    "recovery_hint": {"type": ["string", "null"]},
                },
            },
        },
    },
    "sidecar-migration-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "artifact_kind",
            "current_version",
            "target_version",
            "applied_migrations",
            "version_marker",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "artifact_kind": {"type": "string", "const": "sidecar_migration_report"},
            "current_version": {"type": "integer"},
            "target_version": {"type": "integer"},
            "applied_migrations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "migration_id",
                        "from_version",
                        "to_version",
                        "description",
                        "actions",
                    ],
                    "properties": {
                        "migration_id": {"type": "string"},
                        "from_version": {"type": "integer"},
                        "to_version": {"type": "integer"},
                        "description": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "version_marker": {"type": "string"},
            "pre_migration_snapshot": {"type": "string"},
        },
    },
    "sidecar-migration-snapshot.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "artifact_kind",
            "captured_version",
            "captured_files",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "artifact_kind": {"type": "string", "const": "sidecar_migration_snapshot"},
            "captured_version": {"type": "integer"},
            "captured_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "existed", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "existed": {"type": "boolean"},
                        "content": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },
    "ops-command-bundle.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "bundle"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "bundle": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "commands"],
                "properties": {
                    "name": {"type": "string"},
                    "commands": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "argv"],
                            "properties": {
                                "label": {"type": "string"},
                                "argv": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "stdout_path": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
    "retention-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "mode",
            "status",
            "candidates",
            "deleted",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string", "enum": ["dry-run", "apply"]},
            "status": {"type": "string", "enum": ["planned", "complete"]},
            "candidates": {"type": "array", "items": {"type": "string"}},
            "deleted": {"type": "array", "items": {"type": "string"}},
            "redaction_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "line_number", "kind"],
                    "properties": {
                        "path": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "kind": {"type": "string"},
                    },
                },
            },
        },
    },
    "mcp-request.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["request_id", "kind", "state", "write_intent", "repo_policy", "execution"],
        "properties": {
            "request_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["audit", "proposal", "eval", "optimization"]},
            "state": {"type": "string", "const": "queued"},
            "write_intent": {"type": "boolean", "const": True},
            "repo_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "version", "hash"],
                "properties": {
                    "path": {"type": "string"},
                    "version": {"type": "integer"},
                    "hash": {"type": ["string", "null"]},
                },
            },
            "execution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "payload"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["trace_audit", "proposal", "eval", "optimization"],
                    },
                    "payload": {
                        "type": "object",
                        "additionalProperties": {
                            "type": [
                                "array",
                                "string",
                                "integer",
                                "number",
                                "boolean",
                                "null",
                            ],
                        },
                    },
                },
            },
            "trace_id": {"type": "string"},
            "trace_format": {"type": "string"},
            "audit_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "held_out_episode_ids": {"type": "array", "items": {"type": "string"}},
            "suite": {"type": "string"},
            "train_trace_ids": {"type": "array", "items": {"type": "string"}},
            "unseen_suites": {"type": "array", "items": {"type": "string"}},
        },
    },
    "daemon-discovered-traces.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "traces"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "traces": {"type": "array", "items": {"type": "string"}},
        },
    },
    "ci-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "mode", "auto_apply", "checks"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string", "enum": ["ci_check"]},
            "auto_apply": {"type": "boolean"},
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "index",
                    "harness",
                    "harness_report",
                    "manifest_contracts",
                    "semantic_policy_lint",
                ],
                "properties": {
                    "index": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["passed", "indexed_documents"],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "indexed_documents": {"type": "integer"},
                        },
                    },
                    "harness": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "passed",
                            "findings",
                            "report_path",
                            "report_sha256",
                            "doc_gardening_task_count",
                        ],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "findings": {"type": "array", "items": {"type": "string"}},
                            "report_path": {"type": "string"},
                            "report_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "doc_gardening_task_count": {"type": "integer", "minimum": 0},
                        },
                    },
                    "harness_report": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "passed",
                            "missing_docs",
                            "stale_docs",
                            "orphaned_runbooks",
                            "recurring_failures_without_docs",
                            "doc_gardening_tasks",
                            "token_budget_violations",
                        ],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "missing_docs": {"type": "array", "items": {"type": "string"}},
                            "stale_docs": {"type": "array", "items": {"type": "string"}},
                            "orphaned_runbooks": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "recurring_failures_without_docs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "doc_gardening_tasks": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "token_budget_violations": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                    "manifest_contracts": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["passed", "findings"],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "findings": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "semantic_policy_lint": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["passed", "findings"],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "findings": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "eval": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "passed",
                            "candidate",
                            "suite_id",
                            "report_path",
                            "trigger_score",
                            "held_out_score",
                            "governance_passed",
                            "recommendation",
                        ],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "candidate": {"type": "string"},
                            "suite_id": {"type": "string"},
                            "report_path": {"type": "string"},
                            "trigger_score": {"type": "number"},
                            "held_out_score": {"type": "number"},
                            "governance_passed": {"type": "boolean"},
                            "recommendation": {"type": "string"},
                        },
                    },
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
            "decision_recommendation": {
                "type": "string",
                "enum": ["needs_review", "reject"],
            },
            "reasons": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "reviewer_checklist": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "rollback_command": {"type": "array", "minItems": 1, "items": {"type": "string"}},
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
    "decision-trace.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "decision_ref",
            "run_id",
            "run",
            "episode",
            "decision",
            "candidate",
            "audit",
            "trace_events",
            "unresolved_evidence_refs",
            "instruction_snapshots",
            "instruction_graphs",
            "reflections",
            "edit_operations",
            "candidate_edits",
            "evals",
            "eval_runs",
            "eval_cases",
            "validation_splits",
            "review_actions",
            "rollbacks",
            "optimizer_memory",
            "llmff_jobs",
            "artifacts",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "decision_ref": {"type": "string"},
            "run_id": {"type": "string"},
            "run": {
                "oneOf": [
                    {"type": "null"},
                    _audited_object_schema(
                        [
                            "run_id",
                            "episode_id",
                            "stage",
                            "manifest_hash",
                            "status",
                            "run_dir",
                            "created_at",
                            "updated_at",
                        ],
                        {
                            "run_id": {"type": "string"},
                            "episode_id": {"type": ["integer", "null"]},
                            "stage": {"type": "string"},
                            "manifest_hash": {"type": "string"},
                            "status": {"type": "string"},
                            "run_dir": {"type": "string"},
                            "created_at": {"type": "string"},
                            "updated_at": {"type": "string"},
                        },
                    ),
                ],
            },
            "episode": {
                "oneOf": [
                    {"type": "null"},
                    _audited_object_schema(
                        [
                            "episode_id",
                            "repo_path",
                            "trace_path",
                            "started_at",
                            "outcome",
                            "summary_hash",
                        ],
                        {
                            "episode_id": {"type": "integer"},
                            "repo_path": {"type": "string"},
                            "trace_path": {"type": "string"},
                            "started_at": {"type": "string"},
                            "outcome": {"type": "string"},
                            "summary_hash": {"type": "string"},
                        },
                    ),
                ],
            },
            "decision": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "decision_id",
                    "candidate_id",
                    "actor",
                    "policy",
                    "decision",
                    "reason",
                    "created_at",
                    "applied_commit",
                    "rollback_ref",
                    "audit_event_sequence",
                    "event_hash",
                ],
                "properties": {
                    "decision_id": {"type": "integer"},
                    "candidate_id": {"type": "integer"},
                    "actor": {"type": "string"},
                    "policy": {"type": "string"},
                    "decision": {"type": "string"},
                    "reason": {"type": "string"},
                    "created_at": {"type": "string"},
                    "applied_commit": {"type": "string"},
                    "rollback_ref": {"type": "string"},
                    "audit_event_sequence": {"type": "integer"},
                    "event_hash": {"type": "string"},
                },
            },
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_id",
                    "audit_id",
                    "base_file",
                    "base_hash",
                    "diff_hash",
                    "diff_path",
                    "risk_class",
                    "rationale",
                    "state",
                    "audit_event_sequence",
                    "event_hash",
                ],
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "audit_id": {"type": "integer"},
                    "base_file": {"type": "string"},
                    "base_hash": {"type": "string"},
                    "diff_hash": {"type": "string"},
                    "diff_path": {"type": "string"},
                    "risk_class": {"type": "string"},
                    "rationale": {"type": "string"},
                    "state": {"type": "string"},
                    "audit_event_sequence": {"type": "integer"},
                    "event_hash": {"type": "string"},
                },
            },
            "audit": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "audit_id",
                    "run_id",
                    "failure_class",
                    "severity",
                    "confidence",
                    "evidence_refs",
                    "instruction_refs",
                    "audit_event_sequence",
                    "event_hash",
                ],
                "properties": {
                    "audit_id": {"type": "integer"},
                    "run_id": {"type": "string"},
                    "failure_class": {"type": "string"},
                    "severity": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "instruction_refs": {"type": "array", "items": {"type": "string"}},
                    "audit_event_sequence": {"type": "integer"},
                    "event_hash": {"type": "string"},
                },
            },
            "trace_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "evidence_id",
                        "event_type",
                        "source_trust",
                        "line_number",
                        "payload_snippet",
                        "payload_truncated",
                        "audit_event_sequence",
                        "event_hash",
                    ],
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "event_type": {"type": "string"},
                        "source_trust": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "payload_snippet": {"type": "string", "maxLength": 512},
                        "payload_truncated": {"type": "boolean"},
                        "audit_event_sequence": {"type": "integer"},
                        "event_hash": {"type": "string"},
                    },
                },
            },
            "unresolved_evidence_refs": {"type": "array", "items": {"type": "string"}},
            "instruction_snapshots": _audited_array_schema(
                [
                    "snapshot_id",
                    "run_id",
                    "path",
                    "content_hash",
                    "artifact_path",
                ],
                {
                    "snapshot_id": {"type": "integer"},
                    "run_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content_hash": {"type": "string"},
                    "artifact_path": {"type": "string"},
                },
            ),
            "instruction_graphs": _audited_array_schema(
                ["graph_id", "run_id", "graph_hash", "artifact_path"],
                {
                    "graph_id": {"type": "integer"},
                    "run_id": {"type": "string"},
                    "graph_hash": {"type": "string"},
                    "artifact_path": {"type": "string"},
                },
            ),
            "reflections": _audited_array_schema(
                [
                    "reflection_id",
                    "run_id",
                    "source_ref",
                    "reflection_hash",
                    "artifact_path",
                ],
                {
                    "reflection_id": {"type": "integer"},
                    "run_id": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "reflection_hash": {"type": "string"},
                    "artifact_path": {"type": "string"},
                },
            ),
            "edit_operations": _audited_array_schema(
                [
                    "edit_operation_id",
                    "candidate_id",
                    "operator",
                    "target_path",
                    "payload",
                ],
                {
                    "edit_operation_id": {"type": "integer"},
                    "candidate_id": {"type": "integer"},
                    "operator": {"type": "string"},
                    "target_path": {"type": "string"},
                    "payload": {"type": "object"},
                },
            ),
            "candidate_edits": _audited_array_schema(
                [
                    "candidate_edit_id",
                    "candidate_id",
                    "edit_operation_id",
                    "target_path",
                    "risk_class",
                ],
                {
                    "candidate_edit_id": {"type": "integer"},
                    "candidate_id": {"type": "integer"},
                    "edit_operation_id": {"type": "integer"},
                    "target_path": {"type": "string"},
                    "risk_class": {"type": "string"},
                },
            ),
            "evals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "eval_id",
                        "suite_id",
                        "report_path",
                        "passed",
                        "metrics",
                        "audit_event_sequence",
                        "event_hash",
                    ],
                    "properties": {
                        "eval_id": {"type": "integer"},
                        "suite_id": {"type": "string"},
                        "report_path": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "metrics": {"type": "object"},
                        "audit_event_sequence": {"type": "integer"},
                        "event_hash": {"type": "string"},
                    },
                },
            },
            "eval_runs": _audited_array_schema(
                ["eval_run_id", "candidate_id", "suite_id", "status", "report_path"],
                {
                    "eval_run_id": {"type": "integer"},
                    "candidate_id": {"type": "integer"},
                    "suite_id": {"type": "string"},
                    "status": {"type": "string"},
                    "report_path": {"type": "string"},
                },
            ),
            "eval_cases": _audited_array_schema(
                ["eval_case_id", "suite_id", "case_id", "case_hash"],
                {
                    "eval_case_id": {"type": "integer"},
                    "suite_id": {"type": "string"},
                    "case_id": {"type": "string"},
                    "case_hash": {"type": "string"},
                },
            ),
            "validation_splits": _audited_array_schema(
                ["validation_split_id", "suite_id", "split_name", "case_ids"],
                {
                    "validation_split_id": {"type": "integer"},
                    "suite_id": {"type": "string"},
                    "split_name": {"type": "string"},
                    "case_ids": {"type": "array", "items": {"type": "string"}},
                },
            ),
            "review_actions": _audited_array_schema(
                ["review_action_id", "candidate_id", "actor", "action", "reason"],
                {
                    "review_action_id": {"type": "integer"},
                    "candidate_id": {"type": "integer"},
                    "actor": {"type": "string"},
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                },
            ),
            "rollbacks": _audited_array_schema(
                [
                    "rollback_id",
                    "decision_id",
                    "candidate_id",
                    "reason",
                    "revert_commit",
                    "post_rollback_eval_result",
                    "rollback_plan",
                    "executed",
                ],
                {
                    "rollback_id": {"type": "integer"},
                    "decision_id": {"type": "string"},
                    "candidate_id": {"type": "integer"},
                    "reason": {"type": "string"},
                    "revert_commit": {"type": "string"},
                    "post_rollback_eval_result": {"type": "object"},
                    "rollback_plan": {"type": "string"},
                    "executed": {"type": "boolean"},
                },
            ),
            "optimizer_memory": _audited_array_schema(
                ["optimizer_memory_id", "memory_type", "key", "payload"],
                {
                    "optimizer_memory_id": {"type": "integer"},
                    "memory_type": {"type": "string"},
                    "key": {"type": "string"},
                    "payload": {"type": "object"},
                },
            ),
            "llmff_jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "job_id",
                        "manifest_name",
                        "manifest_hash",
                        "status",
                        "exit_code",
                        "audit_event_sequence",
                        "event_hash",
                        "events",
                        "outputs",
                    ],
                    "properties": {
                        "job_id": {"type": "integer"},
                        "manifest_name": {"type": "string"},
                        "manifest_hash": {"type": "string"},
                        "status": {"type": "string"},
                        "exit_code": {"type": ["integer", "null"]},
                        "audit_event_sequence": {"type": "integer"},
                        "event_hash": {"type": "string"},
                        "events": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "event_id",
                                    "event_type",
                                    "audit_event_sequence",
                                    "event_hash",
                                ],
                                "properties": {
                                    "event_id": {"type": "integer"},
                                    "event_type": {"type": "string"},
                                    "audit_event_sequence": {"type": "integer"},
                                    "event_hash": {"type": "string"},
                                },
                            },
                        },
                        "outputs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "output_id",
                                    "output_name",
                                    "artifact_path",
                                    "content_hash",
                                    "audit_event_sequence",
                                    "event_hash",
                                ],
                                "properties": {
                                    "output_id": {"type": "integer"},
                                    "output_name": {"type": "string"},
                                    "artifact_path": {"type": "string"},
                                    "content_hash": {"type": "string"},
                                    "audit_event_sequence": {"type": "integer"},
                                    "event_hash": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "artifacts": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
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
            "provenance_bundle",
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
            "provenance_bundle": {"type": "string"},
            "pr_metadata": {"type": "object"},
            "pr_result": {"type": "object"},
            "review_actor": {"type": "string"},
            "auto_apply": {"type": "boolean"},
            "explicit_human_review": {"type": "boolean"},
            "review_required_reasons": {"type": "array", "items": {"type": "string"}},
            "decision_rationale": {"type": "string"},
        },
    },
    "provenance-bundle.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "run_id",
            "candidate_id",
            "mode",
            "target_files",
            "applied_commit",
            "rollback_command",
            "pre_hashes",
            "post_hashes",
            "source_artifacts",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "run_id": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "mode": {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "applied_commit": {"type": "string"},
            "rollback_command": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "pre_hashes": {"type": "object"},
            "post_hashes": {"type": "object"},
            "recorded_provenance": {
                "type": "object",
                "required": [
                    "audit_id",
                    "eval_id",
                    "policy_decision_id",
                    "audit_event_sequences",
                ],
                "properties": {
                    "audit_id": {"type": "integer"},
                    "eval_id": {"type": "integer"},
                    "policy_decision_id": {"type": "integer"},
                    "audit_event_sequences": {
                        "type": "object",
                        "required": ["audit", "candidate", "eval", "policy_decision"],
                        "properties": {
                            "audit": {"type": "integer"},
                            "candidate": {"type": "integer"},
                            "eval": {"type": "integer"},
                            "policy_decision": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
            "source_artifacts": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "apply_plan",
                    "candidate_diff",
                    "candidate_metadata",
                    "eval_report",
                    "policy_gate",
                ],
                "properties": {
                    "apply_plan": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "candidate_diff": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "candidate_metadata": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "eval_report": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "policy_gate": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "auto-apply-preflight.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "run_id",
            "candidate_id",
            "mode",
            "target_files",
            "branch_name",
            "eligible",
            "would_apply",
            "lane",
            "reasons",
            "approval_bundle",
            "checks",
            "readiness_metrics",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "run_id": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "mode": {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "branch_name": {"type": "string"},
            "eligible": {"type": "boolean"},
            "would_apply": {"type": "boolean"},
            "lane": {"type": ["string", "null"]},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "approval_bundle": {"type": ["object", "null"]},
            "source_artifacts": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_diff",
                    "candidate_metadata",
                    "candidate_preview_manifest",
                    "candidate_preview_file",
                    "eval_report",
                    "policy",
                    "policy_gate",
                ],
                "properties": {
                    "candidate_diff": _artifact_ref_schema(),
                    "candidate_metadata": _artifact_ref_schema(),
                    "candidate_preview_manifest": _artifact_ref_schema(),
                    "candidate_preview_file": _artifact_ref_schema(),
                    "eval_report": _artifact_ref_schema(),
                    "policy": _artifact_ref_schema(),
                    "policy_gate": _artifact_ref_schema(),
                },
            },
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "policy_gate",
                    "stored_policy_gate",
                    "eval_report",
                    "vcs",
                    "candidate_preview",
                    "auto_apply",
                ],
                "properties": {
                    "policy_gate": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["allowed", "reasons"],
                        "properties": {
                            "allowed": {"type": "boolean"},
                            "reasons": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "stored_policy_gate": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["allowed", "reasons"],
                        "properties": {
                            "allowed": {"type": "boolean"},
                            "reasons": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "eval_report": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "candidate_id_matches",
                            "passed",
                            "recommendation",
                            "suite_id",
                        ],
                        "properties": {
                            "candidate_id_matches": {"type": "boolean"},
                            "passed": {"type": "boolean"},
                            "recommendation": {"type": "string"},
                            "suite_id": {"type": "string"},
                            "acceptance_reason": {"type": "string"},
                        },
                    },
                    "vcs": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "preflight_passed",
                            "worktree_clean",
                            "dirty_paths",
                            "target_files_clean",
                            "base_hashes_match",
                            "reasons",
                        ],
                        "properties": {
                            "preflight_passed": {"type": "boolean"},
                            "worktree_clean": {"type": "boolean"},
                            "dirty_paths": {"type": "array", "items": {"type": "string"}},
                            "target_files_clean": {"type": "boolean"},
                            "base_hashes_match": {"type": "boolean"},
                            "reasons": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "candidate_preview": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["passed", "reason"],
                        "properties": {
                            "passed": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
                    "auto_apply": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["incident_active", "active_incidents"],
                        "properties": {
                            "incident_active": {"type": "boolean"},
                            "active_incidents": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "artifact_status",
                                        "artifact_valid",
                                        "candidate_id",
                                        "event_type",
                                        "failure_kind",
                                        "incident",
                                    ],
                                    "properties": {
                                        "artifact_status": {"type": "string"},
                                        "artifact_valid": {"type": "boolean"},
                                        "candidate_id": {"type": "integer"},
                                        "event_type": {"type": "string"},
                                        "failure_kind": {"type": "string"},
                                        "incident": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "readiness_metrics": {"type": "object"},
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
            "lane",
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
            "lane": {"type": "string"},
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
    "apply-incident.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "mode",
            "phase",
            "candidate_id",
            "decision_id",
            "run_id",
            "failure_kind",
            "failure_message",
            "target_files",
            "branch_name",
            "applied_commit",
            "rollback_command",
            "pre_hashes",
            "post_hashes",
            "remote",
            "remote_branch_state",
            "pr_state",
            "pr_created",
            "pr_metadata",
            "pr_result",
            "apply_plan_written",
            "provenance_bundle_written",
            "remote_cleanup_attempted",
            "manual_cleanup",
            "source_artifacts",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string"},
            "phase": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "decision_id": {"type": "string"},
            "run_id": {"type": "string"},
            "failure_kind": {"type": "string"},
            "failure_message": {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "branch_name": {"type": "string"},
            "applied_commit": {"type": "string"},
            "rollback_command": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "pre_hashes": {"type": "object"},
            "post_hashes": {"type": "object"},
            "remote": {"type": "string"},
            "remote_branch_state": {
                "type": "string",
                "enum": ["not_pushed", "pushed", "unknown"],
            },
            "pr_state": {
                "type": "string",
                "enum": ["not_created", "created", "uncertain"],
            },
            "pr_created": {"type": "boolean"},
            "pr_metadata": {"type": "object"},
            "pr_result": {"type": "object"},
            "apply_plan_written": {"type": "boolean"},
            "provenance_bundle_written": {"type": "boolean"},
            "remote_cleanup_attempted": {"type": "boolean", "const": False},
            "manual_cleanup": {"type": "array", "items": {"type": "string"}},
            "source_artifacts": {"type": "object"},
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
            "pre_hashes",
            "post_rollback_hashes",
            "restored_pre_hashes",
            "source_artifacts",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "decision_id": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "metadata": {"type": "object"},
            "executed": {"type": "boolean"},
            "revert_commit": {"type": "string"},
            "pre_hashes": {"type": "object"},
            "post_rollback_hashes": {"type": "object"},
            "restored_pre_hashes": {"type": "boolean"},
            "source_artifacts": {
                "type": "object",
                "additionalProperties": False,
                "required": ["apply_plan"],
                "properties": {
                    "apply_plan": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "provenance_bundle": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "rollback-incident.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "decision_id",
            "candidate_id",
            "failure_kind",
            "failure_message",
            "commit_sha",
            "target_files",
            "rollback_plan_written",
            "rollback_applied",
            "source_artifacts",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "decision_id": {"type": "string"},
            "candidate_id": {"type": "integer"},
            "failure_kind": {"type": "string"},
            "failure_message": {"type": "string"},
            "commit_sha": {"type": "string"},
            "revert_commit": {"type": "string"},
            "rollback_plan": {"type": "string"},
            "post_rollback_hashes": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "restored_pre_hashes": {"type": "boolean"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "rollback_plan_written": {"type": "boolean"},
            "rollback_applied": {"type": "boolean"},
            "source_artifacts": {
                "type": "object",
                "additionalProperties": False,
                "required": ["apply_plan"],
                "properties": {
                    "apply_plan": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                    "provenance_bundle": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


JSON_ARTIFACT_JSON_SCHEMAS["auto-apply-shadow.json"] = copy.deepcopy(
    JSON_ARTIFACT_JSON_SCHEMAS["auto-apply-preflight.json"]
)
JSON_ARTIFACT_JSON_SCHEMAS["auto-apply-shadow.json"]["required"].insert(7, "shadow_mode")
JSON_ARTIFACT_JSON_SCHEMAS["auto-apply-shadow.json"]["properties"]["shadow_mode"] = {
    "type": "boolean",
    "const": True,
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
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{name} must be a JSON object")
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
    if name == "drift.raw.json":
        _validate_drift_raw_artifact(payload)
    if name == "candidate-ranking.json":
        _validate_candidate_ranking_artifact(payload)
    if name == "acceptance-summary.raw.json":
        _validate_acceptance_summary_raw_artifact(payload)
    if name == "optimization-summary.json" and payload.get("decision") == "needs_review":
        for field in (
            "acceptance_decision_recommendation",
            "acceptance_evidence",
            "acceptance_reasons",
            "acceptance_summary_path",
            "accepted_bounded_edit_metadata",
            "reviewer_checklist",
            "rollback_command",
            ):
            if field not in payload:
                raise ArtifactValidationError(f"{name} missing required field: {field}")
    if name in {
        "apply-incident.json",
        "provenance-bundle.json",
        "rollback-plan.json",
        "rollback-incident.json",
    }:
        source_artifacts = payload.get("source_artifacts", {})
        if isinstance(source_artifacts, dict):
            for artifact_name, artifact_ref in source_artifacts.items():
                if not isinstance(artifact_ref, dict):
                    continue
                sha256 = artifact_ref.get("sha256")
                if not (
                    isinstance(sha256, str)
                    and len(sha256) == 64
                    and all(character in "0123456789abcdef" for character in sha256)
                ):
                    raise ArtifactValidationError(
                        f"{name} source_artifacts.{artifact_name}.sha256 must be a SHA-256 digest"
                    )


def _validate_drift_raw_artifact(payload: dict[str, Any]) -> None:
    seen_cluster_ids: set[str] = set()
    for cluster in payload.get("clusters", []):
        if not isinstance(cluster, dict):
            continue
        cluster_id = cluster.get("cluster_id")
        if not isinstance(cluster_id, str):
            continue
        if cluster_id in seen_cluster_ids:
            raise ArtifactValidationError(f"drift.raw.json duplicate cluster_id: {cluster_id}")
        seen_cluster_ids.add(cluster_id)


_REJECTED_EDIT_SUPPRESSION_CONTEXT_REQUIRED = frozenset(
    (
        "future_proposal_suppression_signal",
        "semantic_fingerprint",
        "rejection_reason",
        "source_refs",
    )
)
_REJECTED_EDIT_SUPPRESSION_CONTEXT_ALLOWED = _REJECTED_EDIT_SUPPRESSION_CONTEXT_REQUIRED | frozenset(
    (
        "operator",
        "file",
        "section",
        "category",
        "failure_pattern",
        "review_actor",
        "review_template",
    )
)
_REJECTED_CLUSTER_SUPPRESSION_CONTEXT_REQUIRED = frozenset(
    ("cluster_id", "evidence_refs", "rejection_reason", "source_refs")
)
_REJECTED_CLUSTER_SUPPRESSION_CONTEXT_ALLOWED = (
    _REJECTED_CLUSTER_SUPPRESSION_CONTEXT_REQUIRED
    | frozenset(
        (
            "category",
            "failure_pattern",
            "review_actor",
            "review_template",
        )
    )
)


def _validate_candidate_ranking_artifact(payload: dict[str, Any]) -> None:
    for rejected_index, rejected in enumerate(payload.get("rejected_candidates", [])):
        if not isinstance(rejected, dict):
            continue
        contexts = rejected.get("suppression_context", [])
        if contexts is None:
            continue
        if not isinstance(contexts, list):
            raise ArtifactValidationError(
                "candidate-ranking.json field has wrong type: "
                f"rejected_candidates[{rejected_index}].suppression_context"
            )
        for context_index, context in enumerate(contexts):
            field_path = (
                f"rejected_candidates[{rejected_index}]"
                f".suppression_context[{context_index}]"
            )
            if not isinstance(context, dict):
                raise ArtifactValidationError(
                    f"candidate-ranking.json field has wrong type: {field_path}"
                )
            if "semantic_fingerprint" in context or "future_proposal_suppression_signal" in context:
                _validate_suppression_context_shape(
                    context,
                    field_path=field_path,
                    required=_REJECTED_EDIT_SUPPRESSION_CONTEXT_REQUIRED,
                    allowed=_REJECTED_EDIT_SUPPRESSION_CONTEXT_ALLOWED,
                )
            else:
                _validate_suppression_context_shape(
                    context,
                    field_path=field_path,
                    required=_REJECTED_CLUSTER_SUPPRESSION_CONTEXT_REQUIRED,
                    allowed=_REJECTED_CLUSTER_SUPPRESSION_CONTEXT_ALLOWED,
                )


def _validate_suppression_context_shape(
    context: dict[str, object],
    *,
    field_path: str,
    required: frozenset[str],
    allowed: frozenset[str],
) -> None:
    for field in sorted(required):
        if field not in context:
            raise ArtifactValidationError(
                f"candidate-ranking.json missing required field: {field_path}.{field}"
            )
    extra = sorted(set(context) - allowed)
    if extra:
        raise ArtifactValidationError(
            f"candidate-ranking.json has additional property: {field_path}.{extra[0]}"
        )


_ACCEPTANCE_REVIEW_CHECKLIST_REQUIREMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("candidate diff", ("candidate diff", "bounded edit")),
    ("rationale", ("rationale",)),
    ("risk", ("risk",)),
    ("source evidence", ("source evidence", "evidence")),
    ("expected behavior change", ("expected behavior change", "behavior change")),
    ("rollback", ("rollback",)),
)


def _validate_acceptance_summary_raw_artifact(payload: dict[str, Any]) -> None:
    if payload.get("decision_recommendation") != "needs_review":
        return
    checklist = payload.get("reviewer_checklist")
    if not isinstance(checklist, list):
        return
    for index, item in enumerate(checklist):
        if isinstance(item, str) and not item.strip():
            raise ArtifactValidationError(
                "acceptance-summary.raw.json reviewer_checklist "
                f"has blank item at index {index}"
            )
    normalized_items = [
        " ".join(item.casefold().split())
        for item in checklist
        if isinstance(item, str) and item.strip()
    ]
    for requirement, accepted_phrases in _ACCEPTANCE_REVIEW_CHECKLIST_REQUIREMENTS:
        if not any(
            any(phrase in item for phrase in accepted_phrases)
            for item in normalized_items
        ):
            raise ArtifactValidationError(
                "acceptance-summary.raw.json reviewer_checklist "
                f"missing {requirement} review item"
            )


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
        if field_path == "schema_version":
            raise ArtifactValidationError(f"{artifact_name} has unsupported schema_version")
        raise ArtifactValidationError(f"{artifact_name} field has unsupported value: {field_path}")
    if "enum" in schema:
        allowed_values = schema["enum"]
        if not isinstance(allowed_values, list):
            raise ArtifactValidationError(f"{artifact_name} schema enum must be an array")
        if value not in allowed_values:
            raise ArtifactValidationError(f"{artifact_name} field has unsupported value: {field_path}")
    if "minLength" in schema and isinstance(value, str) and len(value) < int(schema["minLength"]):
        raise ArtifactValidationError(f"{artifact_name} field is too short: {field_path}")
    if "pattern" in schema and isinstance(value, str):
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise ArtifactValidationError(f"{artifact_name} schema pattern must be a string")
        if re.fullmatch(pattern, value) is None:
            raise ArtifactValidationError(f"{artifact_name} field does not match pattern: {field_path}")

    if expected_type == "array":
        if "minItems" in schema and isinstance(value, list) and len(value) < int(schema["minItems"]):
            raise ArtifactValidationError(f"{artifact_name} field has too few items: {field_path}")
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
            else:
                additional_schema = schema.get("additionalProperties")
                if isinstance(additional_schema, dict):
                    _validate_schema_value(
                        artifact_name,
                        f"{field_path}.{child_field}",
                        additional_schema,
                        child_value,
                    )


def write_json_artifact(path: Path, payload: dict[str, Any]) -> Path:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    return _publish_text_artifact(path, text)


def write_text_artifact(path: Path, text: str) -> Path:
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    return _publish_text_artifact(path, text)


def _publish_text_artifact(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        mark_private_file(path)
        _fsync_directory(path.parent)
        return path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
