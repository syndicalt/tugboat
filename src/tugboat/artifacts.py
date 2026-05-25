from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

JSON_ARTIFACT_SCHEMAS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "audit.json": {
        "schema_version": int,
        "audit_id": int,
        "edit_warranted": bool,
        "evidence_refs": list,
        "failure_class": str,
        "severity": str,
        "confidence": (int, float),
    },
    "candidate.json": {
        "schema_version": int,
        "audit_id": int,
        "base_file": str,
        "base_hash": str,
        "diff_hash": str,
        "risk_class": str,
        "rationale": str,
        "sources": list,
    },
    "eval-report.json": {
        "schema_version": int,
        "candidate_id": int,
        "metrics": dict,
        "passed": bool,
        "suite_id": str,
    },
    "decision.json": {
        "schema_version": int,
        "candidate_id": int,
        "decision": str,
        "policy_allowed": bool,
        "policy_reasons": list,
    },
}


class ArtifactValidationError(ValueError):
    pass


def validate_json_artifact(name: str, payload: dict[str, Any]) -> None:
    schema = JSON_ARTIFACT_SCHEMAS.get(name)
    if schema is None:
        raise ArtifactValidationError(f"unknown artifact schema: {name}")
    for field, expected_type in schema.items():
        if field not in payload:
            raise ArtifactValidationError(f"{name} missing required field: {field}")
        if not isinstance(payload[field], expected_type):
            raise ArtifactValidationError(f"{name} field has wrong type: {field}")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ArtifactValidationError(f"{name} has unsupported schema_version")


def validate_report_markdown(text: str) -> None:
    required = ("# Tugboat Report", "## Rationale")
    for marker in required:
        if marker not in text:
            raise ArtifactValidationError(f"report.md missing required section: {marker}")


def write_json_artifact(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text_artifact(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
