from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


DOC_CONTRACTS = {
    "docs/ops/release-checklist.md": {
        "sections": [
            "## Purpose",
            "## Preconditions",
            "## Checklist",
            "## Publish",
            "## Rollback",
            "## Evidence to Retain",
        ],
        "required_text": [
            "tugboat doctor",
            "tugboat harness check --repo .",
            "tugboat ops release-manifest --repo .",
            "python -m build --wheel",
            "python -m twine check dist/<wheel>.whl",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "--evidence .sidecar/ci/pytest-coverage.log",
            "--evidence .sidecar/ci/twine-check.txt",
            "git tag -a v<version> <sha>",
            "git push origin v<version>",
            "python -m twine upload dist/<wheel>.whl",
            "proposal_only",
            "auto_apply: disabled",
            ".sidecar/ops/release-artifact-manifest.json",
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
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "actions/upload-artifact",
            ".sidecar/ci/ci-report.json",
            ".sidecar/ci/**",
            "if: always()",
            "retention-days: 14",
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
            "allowed_providers",
            "No environment-only approval path",
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
            "tugboat retention --repo . --redact-output",
            "optimization-summary.json",
            "| Runtime lifecycle streams | events, checkpoints | 7 days |",
            "tugboat retention --repo .",
            ".sidecar/ops/retention/retention-report.json",
            "status: planned",
            "status: complete",
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
            "allow_network: false",
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
            "allowed_providers",
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
            "allowed_providers",
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
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "actions/upload-artifact",
            ".sidecar/ci/ci-report.json",
            ".sidecar/ci/**",
            "if: always()",
            "retention-days: 14",
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
        assert _markdown_body(content).startswith("# "), f"{relative_path} must start with a markdown title"

    for section in contract["sections"]:
        assert section in content, f"{relative_path} is missing section {section!r}"

    for text in contract["required_text"]:
        assert text in content, f"{relative_path} is missing required text {text!r}"


def test_dated_security_review_matches_release_evidence_commit() -> None:
    release_notes = (REPO_ROOT / "docs/releases/0.1.0.md").read_text(encoding="utf-8")
    security_review = (REPO_ROOT / "docs/ops/security-review-2026-05-26.md").read_text(
        encoding="utf-8"
    )

    expected_commit = _single_match(
        r"Build/code artifact commit: `([0-9a-f]{7,40})`\.",
        release_notes,
    )
    expected_ci_url = _single_match(
        r"--ci-url (local://release-smoke/2026-05-26-[0-9a-f]{7,40})",
        release_notes,
    )
    expected_coverage = _single_match(
        r"passed with ([0-9]+ tests and [0-9]+\.[0-9]+% coverage)",
        release_notes,
    )

    assert f"Build/code artifact commit: `{expected_commit}`." in release_notes
    assert f"--commit {expected_commit}" in release_notes
    assert f"Build/code artifact commit reviewed: `{expected_commit}`." in security_review
    assert f"--commit {expected_commit}" in security_review
    assert expected_ci_url in release_notes
    assert expected_ci_url in security_review
    assert expected_coverage in release_notes
    assert expected_coverage in security_review
    combined = f"{release_notes}\n{security_review}"
    assert "1b979e3" not in combined
    assert "728 tests" not in combined
    assert "90.00% coverage" not in combined
    assert "e58d673" not in combined
    assert "725 tests" not in combined
    assert "90.11% coverage" not in combined
    assert "local://release-smoke/2026-05-26 --approver" not in combined


def test_github_actions_ci_workflow_enforces_proposal_only_release_gates() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    required_fragments = [
        "name: tugboat-ci",
        "pull_request:",
        "branches: [main]",
        "permissions:",
        "contents: read",
        'python-version: "3.13"',
        'python -m pip install -e ".[dev]"',
        "tugboat doctor",
        "tugboat ci --repo .",
        "tugboat index --repo . --check",
        "tugboat harness check --repo .",
        "python -m pytest --cov=src --cov-report=term-missing -q",
        "actions/upload-artifact@v4",
        "if: always()",
        "retention-days: 14",
        ".sidecar/ci/ci-report.json",
    ]
    for fragment in required_fragments:
        assert fragment in workflow
    assert "auto-apply" not in workflow
    assert "OPENAI_API_KEY" not in workflow


def _single_match(pattern: str, content: str) -> str:
    matches = re.findall(pattern, content)
    assert len(matches) == 1
    return matches[0]


def _markdown_body(content: str) -> str:
    if not content.startswith("---\n"):
        return content
    _, separator, body = content[4:].partition("\n---\n")
    return body.lstrip() if separator else content
