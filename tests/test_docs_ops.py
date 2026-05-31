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
            "docs/cli-reference.md",
            "docs/apply-rollback.md",
            "docs/auto-apply.md",
            "docs/daemon-guide.md",
            "docs/troubleshooting.md",
        ],
    },
    "docs/integrations.md": {
        "sections": [
            "## Purpose",
            "## Common Setup",
            "## Codex",
            "## Claude Code",
            "## Cursor",
            "## Aider",
            "## Continue.dev",
            "## MCP Clients",
            "## CI Systems",
            "## Trace Fixtures",
            "## Safety Defaults",
        ],
        "required_text": [
            "tugboat doctor --repo .",
            "tugboat init --repo .",
            "tugboat optimize --repo . --trace traces/example.jsonl --suite all",
            "tests/fixtures/traces/codex-local-session-export.jsonl",
            "tests/fixtures/traces/claude-transcript.json",
            "tests/fixtures/traces/mcp-session.jsonl",
            "tests/fixtures/traces/ci-failure.json",
            "proposal-only",
            "auto-apply remains disabled",
            "Do not pass provider credentials through MCP",
        ],
    },
    "docs/cli-reference.md": {
        "sections": [
            "## Core Workflow",
            "## Proposal Pipeline",
            "## Review And Change Control",
            "## Auto-Apply",
            "## Harness And CI",
            "## MCP And Daemon",
            "## Operations",
        ],
        "required_text": [
            "tugboat doctor",
            "tugboat doctor --repo .",
            "tugboat init --repo .",
            "tugboat optimize --repo . --trace traces/example.jsonl --suite all",
            "inspect-decision",
            "metadata-only operator summary",
            "payload snippets",
            "tugboat apply --repo . --candidate latest --mode pr",
            "tugboat auto-apply --repo . --candidate latest --actor",
            "tugboat daemon cycle --repo . --trace-dir traces",
            "tugboat ops release-manifest --repo .",
            "python -m pytest --cov=src --cov-report=term-missing -q",
        ],
    },
    "docs/apply-rollback.md": {
        "sections": [
            "## Review Packet",
            "## Proposal Mode",
            "## Branch And Commit Modes",
            "## Pull Request Mode",
            "## Rollback Plan",
            "## Safety Stops",
        ],
        "required_text": [
            "tugboat inspect-decision --repo . --decision latest",
            "tugboat apply --repo . --candidate latest --mode proposal",
            "tugboat apply --repo . --candidate latest --mode commit",
            "tugboat apply --repo . --candidate latest --mode pr",
            "tugboat rollback --repo . --decision latest --execute",
            "metadata summary",
            "payload snippets",
            "read-only kill switch",
            "Class C candidates require explicit human review",
        ],
    },
    "docs/daemon-guide.md": {
        "sections": [
            "## Purpose",
            "## Status",
            "## Read-Only Kill Switch",
            "## Run One Job",
            "## Cycle And Watch Traces",
            "## Local Socket",
            "## Worktree Profile",
            "## Recovery",
        ],
        "required_text": [
            "tugboat daemon status --repo .",
            "tugboat daemon read-only --repo . --enable",
            "tugboat daemon run-once --repo .",
            "tugboat daemon cycle --repo .",
            "tugboat daemon serve --repo . --socket .sidecar/daemon.sock",
            "tugboat daemon profile --repo .",
            "local-only",
            "stale leases",
        ],
    },
    "docs/auto-apply.md": {
        "sections": [
            "## Default Posture",
            "## Eligibility",
            "## Dry Check",
            "## Confirmed Execution",
            "## Rollback",
            "## Emergency Stop",
        ],
        "required_text": [
            "Auto-apply defaults off",
            "tugboat auto-apply --repo . --candidate latest --actor",
            "--confirm-auto-apply",
            "--auto-apply-policy-version",
            "docs_hygiene",
            "3 burn-in days",
            "20% maximum rejection rate",
            "skill_improvement",
            "7 burn-in days",
            "3% maximum rollback rate",
            "tugboat rollback --repo . --decision latest --execute",
            "tugboat daemon read-only --repo . --enable",
        ],
    },
    "docs/announcements/tugboat-production-release-article.md": {
        "sections": [
            "## Why This Exists",
            "## What Tugboat Monitors",
            "## The Core Workflow",
            "## llmff As The Pipeline Boundary",
            "## Proposal-First By Default",
            "## Apply And Rollback",
            "## Auto-Apply Without Recklessness",
            "## MCP And Daemon Operations",
            "## Release Evidence",
            "## What This Enables",
            "## The Bet",
            "## Short X Post",
        ],
        "required_text": [
            "https://github.com/syndicalt/tugboat",
            "CODEX.md",
            "AGENTS.md",
            "SKILL.md",
            "tugboat optimize --repo . --trace traces/example.jsonl --suite all",
            "tugboat apply --repo . --candidate latest --mode pr",
            "tugboat auto-apply --repo . --candidate latest --actor",
            "docs_hygiene",
            "minimum_burn_in_days: 3",
            "maximum_rejection_rate: 0.20",
            "skill_improvement",
            "maximum_rollback_rate: 0.03",
            "tugboat daemon read-only --repo . --enable",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "No silent mutation",
        ],
    },
    "docs/roadmaps/2026-05-27-next-roadmap-proposals.md": {
        "sections": [
            "## Purpose",
            "## Selection Criteria",
            "## Proposal 1: Robust Auto-Update",
            "## Proposal 2: Real-World Trace Adapter Hardening",
            "## Proposal 3: Provider-Backed llmff Manifest Expansion",
            "## Proposal 4: Operator Review UX",
            "## Proposal 5: Longitudinal Metrics And Local Dashboard",
            "## Proposal 6: Token Efficiency Evaluation",
            "## Proposal 7: Skill Rewrite Evaluation",
            "## Proposal 8: Team Workflow And PR Integration",
            "## Proposal 9: Harness Health And Cleanup Agents",
            "## Proposal 10: Zaxy Memory Bridge",
            "## Recommended Next Roadmap Shape",
        ],
        "required_text": [
            "auto-update defaults off",
            "Class A only by default",
            "no runtime CLI override for safety thresholds",
            "read-only kill switch",
            "VCS commit or draft PR",
            "one-command rollback is mandatory",
            "provider-backed execution requires `llmff.allow_network: true`",
            "instruction corpus token footprint",
            "auto-update blocks candidates that exceed policy-owned token-growth limits",
            "skill trigger-condition preservation checks",
            "skill rewrite candidates include a skill-specific eval report",
            "Tugboat remains locally functional without Zaxy",
            "from governed proposals to governed maintenance",
        ],
    },
    "docs/roadmaps/v1.0.0-roadmap.md": {
        "sections": [
            "## Purpose",
            "## Current Baseline",
            "## Release Principles",
            "## Milestone 1: 0.2.0 Stabilization And Documentation",
            "## Milestone 2: 0.3.0 Review Intelligence",
            "## Milestone 3: 0.4.0 Automation And Ecosystem",
            "## Milestone 4: 0.5.0 Through 0.8.0 Reliability And Hardening",
            "## Milestone 5: 0.9.0 Release Candidate",
            "## Milestone 6: 1.0.0 Stable Release",
            "## Cross-Cutting Workstreams",
            "## v1 Feature Tracks",
            "## Non-Goals For v1.0.0",
            "## Tracking Model",
            "## Risk Register",
            "## 1.0 Launch Checklist",
        ],
        "required_text": [
            "proposal-only by default",
            "narrow Class A auto-apply",
            "VCS-backed mutation and rollback",
            "`llmff` as the bounded pipeline runner",
            "Rejected-edit memory",
            "Drift clustering",
            "docs/integrations.md",
            "tests/fixtures/traces/codex-local-session-export.jsonl",
            "isolated virtual environment",
            "apply/rollback/auto-apply/daemon/vcs/trace-adapter",
            "Token efficiency",
            "Skill rewrite evaluation",
            "Public network daemon",
            "p0-release-blocker",
            "Security review approved with no open critical or high findings",
        ],
    },
    "docs/troubleshooting.md": {
        "sections": [
            "## First Checks",
            "## Init And Policy",
            "## llmff Failures",
            "## Eval And Acceptance",
            "## Apply And Rollback",
            "## MCP",
            "## Daemon",
            "## Secrets And Redaction",
            "## Release Evidence",
        ],
        "required_text": [
            "tugboat doctor",
            "init blocked: .sidecar/policy.yaml already exists",
            "instruction index blocked: llmff inspect failed: binary not found",
            "llmff eval_report cannot accept without validation split provenance",
            "apply blocked: base hash",
            "tugboat mcp stdio --repo . --read-only",
            "daemon serve blocked: socket_path must resolve inside repo sidecar",
            "tugboat retention --repo . --redact-output",
            "release manifest blocked: commit does not match current HEAD",
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
    "docs/releases/production-candidate.md": {
        "sections": [
            "## Summary",
            "## Scope",
            "## Verification",
            "## Release Manifest",
            "## Decision",
        ],
        "required_text": [
            "exact release commit",
            "production release candidate",
            "proposal-only",
            "auto-apply remains disabled",
            "1099 tests and 90.02% coverage",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "approved_proposal_only",
            ".sidecar/ops/release-artifact-manifest.json",
        ],
    },
    "docs/ops/security-review-production-candidate.md": {
        "sections": [
            "## Scope",
            "## Commands",
            "## Findings",
            "## Decision",
        ],
        "required_text": [
            "Build/code artifact reviewed",
            "exact release commit",
            "proposal_only",
            "auto_apply: disabled",
            "1099 tests and 90.02% coverage",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "No open critical or high findings",
            "Not approved",
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


def test_production_release_candidate_uses_current_head_manifest_shape() -> None:
    release_notes_path = REPO_ROOT / "docs/releases/production-candidate.md"
    security_review_path = REPO_ROOT / "docs/ops/security-review-production-candidate.md"
    release_notes = release_notes_path.read_text(encoding="utf-8")
    security_review = security_review_path.read_text(encoding="utf-8")

    assert '--commit "$(git rev-parse HEAD)"' in release_notes
    assert '--commit "$(git rev-parse HEAD)"' in security_review
    assert "$(git rev-parse --short HEAD)" in release_notes
    assert "$(git rev-parse --short HEAD)" in security_review
    assert "PYTHONPATH=src python -m tugboat ci --repo .` passed with `ci: ok`" in release_notes
    assert "1099 tests and 90.02% coverage" in release_notes
    assert "1099 tests and 90.02% coverage" in security_review
    assert "python -m pytest --cov=src --cov-report=term-missing -q" in release_notes
    assert "python -m pytest --cov=src --cov-report=term-missing -q" in security_review
    assert ".sidecar/ops/release-artifact-manifest.json" in release_notes
    assert "Open Release Work" not in release_notes
    assert "auto_apply: disabled" in security_review
    assert "proposal_only" in security_review
    combined = f"{release_notes}\n{security_review}"
    assert "1531caf0ee99d7c879b20f0b3e9b52d53010099f" not in combined
    assert "e02fc0527c0dbac1fea04251579ba62a85fbe309" not in combined
    assert "--cov=src/tugboat" not in combined


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


def _git(*args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _markdown_body(content: str) -> str:
    if not content.startswith("---\n"):
        return content
    _, separator, body = content[4:].partition("\n---\n")
    return body.lstrip() if separator else content
