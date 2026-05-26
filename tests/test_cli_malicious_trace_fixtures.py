from __future__ import annotations

import json
from pathlib import Path

import pytest

from tugboat.cli import main


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "malicious_traces"


@pytest.mark.parametrize(
    ("fixture_name", "expected_codes", "expected_trust"),
    [
        (
            "conflicting_instruction.jsonl",
            ["prompt_injection_attempt", "conflicting_instruction_request"],
            ["user", "user"],
        ),
        (
            "poisoned_output_forged_success.jsonl",
            ["poisoned_command_output", "forged_success_claim"],
            ["tool", "agent"],
        ),
    ],
)
def test_audit_records_malicious_trace_fixture_findings(
    tmp_path: Path,
    fixture_name: str,
    expected_codes: list[str],
    expected_trust: list[str],
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nRun tests before final.\n", encoding="utf-8")

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(FIXTURE_DIR / fixture_name),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    findings = audit["trace_risk_findings"]
    finding_evidence = [finding["evidence_id"] for finding in findings]

    assert [finding["code"] for finding in findings] == expected_codes
    assert [finding["source_trust"] for finding in findings] == expected_trust
    assert all(evidence_id.startswith("ev_") for evidence_id in finding_evidence)
    assert set(finding_evidence).issubset(set(audit["evidence_refs"]))
    assert all(
        finding["source_trust"] in {"agent", "tool", "user"}
        for finding in findings
    )


def test_audit_blocks_secret_trace_fixture_before_scoring(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nNever expose secrets.\n", encoding="utf-8")

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(FIXTURE_DIR / "secret_trace.jsonl"),
                "--mock-llmff-inspect",
            ]
        )
        == 1
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))

    assert audit["failure_class"] == "secret_detected"
    assert audit["severity"] == "critical"
    assert audit["secret_findings"][0]["kind"] == "openai_api_key"
    assert "trace_risk_findings" not in audit
