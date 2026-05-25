import json
from pathlib import Path

import pytest

from tugboat.artifacts import (
    ArtifactValidationError,
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
        validate_report_markdown("# Tugboat Report\n\n- candidate: CODEX.md\n")
