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
            "metrics": {"governance_regressions": 0},
            "passed": True,
            "suite_id": "governance-regression",
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
                "metrics": {},
                "passed": True,
                "suite_id": "all",
                "raw_model_payload": "must not appear",
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
