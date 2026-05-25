from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

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
            "risk_class",
            "rationale",
            "sources",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "audit_id": {"type": "integer"},
            "candidate_id": {"type": "integer"},
            "base_file": {"type": "string"},
            "base_hash": {"type": "string"},
            "diff_hash": {"type": "string"},
            "risk_class": {"type": "string"},
            "rationale": {"type": "string"},
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
            "bounded_edit_metadata": {"type": "array", "items": {"type": "object"}},
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
}


class ArtifactValidationError(ValueError):
    pass


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
    required = ("# Tugboat Report", "- schema_version: 1", "## Rationale")
    for marker in required:
        if marker not in text:
            raise ArtifactValidationError(f"report.md missing required section: {marker}")


def _matches_json_schema_type(value: object, expected_type: str) -> bool:
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
    if expected_type is not None and not _matches_json_schema_type(value, str(expected_type)):
        raise ArtifactValidationError(f"{artifact_name} field has wrong type: {field_path}")
    if "const" in schema and value != schema["const"]:
        raise ArtifactValidationError(f"{artifact_name} has unsupported schema_version")

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
