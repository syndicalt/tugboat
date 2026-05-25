from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


DOC_CONTRACTS = {
    "docs/ops/release-checklist.md": {
        "sections": [
            "## Purpose",
            "## Preconditions",
            "## Checklist",
            "## Rollback",
            "## Evidence to Retain",
        ],
        "required_text": [
            "tugboat doctor",
            "tugboat harness check --repo .",
            "python -m pytest -q",
            "proposal_only",
            "auto_apply: disabled",
        ],
    },
    "docs/ci/ci-guide.md": {
        "sections": [
            "## Purpose",
            "## Required Checks",
            "## GitHub Actions Template",
            "## Artifacts",
        ],
        "required_text": [
            "tugboat doctor",
            "tugboat index --repo . --check",
            "tugboat harness check --repo .",
            "python -m pytest -q",
            "actions/upload-artifact",
        ],
    },
    "docs/ops/security-review.md": {
        "sections": [
            "## Purpose",
            "## Review Scope",
            "## Required Checks",
            "## Approval Record",
        ],
        "required_text": [
            "rg -n",
            "tugboat audit --repo . --trace",
            "provider credential",
            "auto-apply",
            "redaction",
        ],
    },
    "docs/ops/sidecar-backup-restore.md": {
        "sections": [
            "## Purpose",
            "## Backup",
            "## Integrity Check",
            "## Restore",
            "## Recovery Verification",
        ],
        "required_text": [
            ".sidecar",
            "tar -czf",
            "sqlite3 .sidecar/db.sqlite",
            "PRAGMA integrity_check",
            "tugboat status --repo .",
        ],
    },
    "docs/ops/artifact-retention-redaction.md": {
        "sections": [
            "## Purpose",
            "## Retention Classes",
            "## Redaction Controls",
            "## Deletion Procedure",
            "## Audit Evidence",
        ],
        "required_text": [
            ".sidecar/runs",
            "find .sidecar/runs",
            "rg -n",
            "OPENAI_API_KEY",
            "redacted",
        ],
    },
    "docs/ops/operating-runbook.md": {
        "sections": [
            "## Purpose",
            "## Daily Checks",
            "## Incident Response",
            "## Rollback",
            "## Escalation",
        ],
        "required_text": [
            "tugboat status --repo .",
            "tugboat harness report --repo .",
            "tugboat report --repo . --run",
            "failure_kind",
            "rollback",
        ],
    },
    "docs/ops/quick-adoption-proposal-only.md": {
        "sections": [
            "## Purpose",
            "## Assumptions",
            "## Fifteen-Minute Adoption",
            "## No-Credentials Proposal Loop",
            "## Stop Criteria",
        ],
        "required_text": [
            "without credentials",
            "proposal_only",
            "tugboat doctor",
            "tugboat index --repo .",
            "tugboat audit --repo . --trace traces/example.jsonl --mock-llmff-inspect",
            "tugboat propose --repo . --audit latest",
        ],
    },
}


@pytest.mark.parametrize("relative_path, contract", DOC_CONTRACTS.items())
def test_phase_10_operations_docs_exist_with_required_sections_and_commands(
    relative_path: str, contract: dict[str, list[str]]
) -> None:
    doc_path = REPO_ROOT / relative_path

    assert doc_path.exists(), f"{relative_path} is required by Phase 10 operations docs"

    content = doc_path.read_text(encoding="utf-8")
    assert content.startswith("# "), f"{relative_path} must start with a markdown title"

    for section in contract["sections"]:
        assert section in content, f"{relative_path} is missing section {section!r}"

    for text in contract["required_text"]:
        assert text in content, f"{relative_path} is missing required text {text!r}"
