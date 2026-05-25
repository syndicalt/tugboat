import json
from pathlib import Path

import pytest

from tugboat.eval.service import write_eval_report
from tugboat.policy.gate import CandidatePatch, PolicyDecision, SourceRef
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report
from tugboat.security.secrets import SecretScanError


def _candidate() -> CandidatePatch:
    return CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )


def test_write_candidate_writes_deterministic_repo_local_artifacts(tmp_path: Path):
    artifacts = write_candidate(tmp_path, "run-1", _candidate())

    assert artifacts.diff_path == tmp_path / ".sidecar" / "runs" / "run-1" / "candidate.diff"
    assert artifacts.json_path == tmp_path / ".sidecar" / "runs" / "run-1" / "candidate.json"
    assert artifacts.diff_path.read_text(encoding="utf-8") == _candidate().diff
    assert json.loads(artifacts.json_path.read_text(encoding="utf-8")) == {
        "audit_id": 2,
        "base_file": "CODEX.md",
        "base_hash": "abc123",
        "diff_hash": CandidatePatch.hash_text(_candidate().diff),
        "rationale": "Clarify ambiguous guidance.",
        "risk_class": "instruction_clarification",
        "schema_version": 1,
        "sources": [{"source_id": "trace-1", "trusted": True}],
    }


def test_write_eval_report_writes_json_report(tmp_path: Path):
    report_path = write_eval_report(
        tmp_path,
        "run-1",
        candidate_id=5,
        suite_id="unit",
        passed=True,
        metrics={"failures": 0, "duration_seconds": 1.25},
    )

    assert report_path == tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "candidate_id": 5,
        "metrics": {"duration_seconds": 1.25, "failures": 0},
        "passed": True,
        "schema_version": 1,
        "suite_id": "unit",
    }


def test_write_report_writes_markdown_summary(tmp_path: Path):
    report_path = write_report(
        tmp_path,
        "run-1",
        candidate=_candidate(),
        decision=PolicyDecision(False, ("modal_weakening", "new_external_endpoint")),
        eval_report_path=tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json",
    )

    assert report_path == tmp_path / ".sidecar" / "runs" / "run-1" / "report.md"
    assert report_path.read_text(encoding="utf-8") == "\n".join(
        [
            "# Tugboat Report",
            "",
            "- schema_version: 1",
            "- candidate: CODEX.md",
            "- risk_class: instruction_clarification",
            "- policy_allowed: false",
            "- policy_reasons: modal_weakening,new_external_endpoint",
            "- eval_report: .sidecar/runs/run-1/eval-report.json",
            "",
            "## Rationale",
            "",
            "Clarify ambiguous guidance.",
            "",
        ]
    )


def test_write_candidate_rejects_secret_in_diff(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )

    with pytest.raises(SecretScanError, match="openai_api_key"):
        write_candidate(tmp_path, "run-1", candidate)


def test_write_report_rejects_secret_in_rationale(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=2,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Leaked token ghp_abcdefghijklmnopqrstuvwx",
        sources=(SourceRef("trace-1", trusted=True),),
    )

    with pytest.raises(SecretScanError, match="ghp_token"):
        write_report(
            tmp_path,
            "run-1",
            candidate=candidate,
            decision=PolicyDecision(True, ()),
            eval_report_path=tmp_path / ".sidecar" / "runs" / "run-1" / "eval-report.json",
        )
