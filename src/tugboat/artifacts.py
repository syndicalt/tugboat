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
            "evidence_refs": {"type": "array"},
            "failure_class": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {"type": "number"},
            "secret_findings": {"type": "array"},
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
            "sources": {"type": "array"},
            "pending_audit_eval_definition_paths": {"type": "array"},
            "bounded_edit_metadata": {"type": "array"},
        },
    },
    "eval-report.json": {
        "$schema": JSON_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "candidate_id", "metrics", "passed", "suite_id"],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "candidate_id": {"type": "integer"},
            "metrics": {"type": "object"},
            "passed": {"type": "boolean"},
            "suite_id": {"type": "string"},
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
            "policy_reasons": {"type": "array"},
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
        expected_type = field_schema.get("type")
        if expected_type is not None and not _matches_json_schema_type(value, str(expected_type)):
            raise ArtifactValidationError(f"{name} field has wrong type: {field}")
        if "const" in field_schema and value != field_schema["const"]:
            raise ArtifactValidationError(f"{name} has unsupported schema_version")


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


def write_json_artifact(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text_artifact(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
