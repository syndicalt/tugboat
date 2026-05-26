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
            'python -m pip install -e ".[dev]"',
            "tugboat doctor",
            "tugboat ci --repo .",
            "tugboat index --repo . --check",
            "tugboat harness check --repo .",
            "python -m pytest -q",
            "actions/upload-artifact",
            ".sidecar/ci/ci-report.json",
            ".sidecar/ci/**",
            "if: always()",
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
            "tugboat ops backup --repo . --archive",
            ".sidecar/ops/backup-plan.json",
            "does not execute the plan",
            "staging=\"$(mktemp -d",
            "tugboat ops restore --repo . --archive \"$backup\" --staging \"$staging\" --pre-restore \"$pre_restore\"",
            ".sidecar/ops/restore-plan.json",
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
            "optimization-summary.json",
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
            "tugboat audit --repo . --trace traces/example.jsonl",
            "tugboat propose --repo . --audit latest",
        ],
    },
    "docs/quickstart.md": {
        "sections": [
            "## Install",
            "## Initialize",
            "## Proposal Loop",
            "## Next Checks",
        ],
        "required_text": [
            "tugboat doctor",
            "tugboat index --repo .",
            "tugboat optimize --repo . --trace traces/example.jsonl --suite all",
            "tugboat audit --repo . --trace",
            "tugboat propose --repo . --audit latest",
            "optimization-summary.json",
            "under 15 minutes",
        ],
    },
    "docs/architecture.md": {
        "sections": [
            "## Boundary",
            "## Components",
            "## Data Flow",
            "## Authority Model",
        ],
        "required_text": [
            "llmff",
            "CLI",
            "MCP",
            "daemon",
            "VCS adapter",
        ],
    },
    "docs/threat-model.md": {
        "sections": [
            "## Assets",
            "## Trust Boundaries",
            "## Threats",
            "## Controls",
        ],
        "required_text": [
            "untrusted traces",
            "secret",
            "local-only",
            "audit ledger",
            "VCS",
        ],
    },
    "docs/policy-examples.md": {
        "sections": [
            "## Proposal Only",
            "## Provider Backed",
            "## MCP Allowlist",
            "## Auto-Apply Disabled",
        ],
        "required_text": [
            ".sidecar/policy.yaml",
            "instruction_files",
            "allowed_repositories",
            "tool_policy",
            "auto_apply",
        ],
    },
    "docs/mcp-guide.md": {
        "sections": [
            "## Transport",
            "## Read Tools",
            "## Write-Intent Tools",
            "## Security Policy",
        ],
        "required_text": [
            "tugboat mcp stdio",
            "tugboat_status",
            "tugboat_request_audit",
            "repo allowlist",
            "direct apply",
        ],
    },
    "docs/ci/github-actions-template.yml": {
        "sections": [],
        "required_text": [
            'python -m pip install -e ".[dev]"',
            "tugboat doctor",
            "tugboat ci --repo .",
            "tugboat index --repo . --check",
            "tugboat harness check --repo .",
            "python -m pytest --cov -q",
            "actions/upload-artifact",
            ".sidecar/ci/**",
            "if: always()",
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
    if relative_path.endswith(".md"):
        assert content.startswith("# "), f"{relative_path} must start with a markdown title"

    for section in contract["sections"]:
        assert section in content, f"{relative_path} is missing section {section!r}"

    for text in contract["required_text"]:
        assert text in content, f"{relative_path} is missing required text {text!r}"
