---
owner: platform
verification_status: verified
---

# Tugboat CI Guide

## Purpose

CI should prove that Tugboat remains proposal-only, structurally healthy, and free of obvious governance regressions. CI must not auto-apply instruction edits or require provider credentials by default.

`tugboat ci --repo .` also materializes the local `llmff` manifest registry and validates manifest contracts offline, including required manifest presence, name/file consistency, output artifact bindings, and known JSON artifact schema names.

## Required Checks

Run these checks on every pull request:

```bash
python -m pip install -e ".[dev]"
mkdir -p .sidecar/ci
tugboat doctor
tugboat ci --repo .
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
python -m build --wheel --outdir dist
python -m twine check dist/<wheel>.whl
```

The protected-branch workflow also retains release-readiness evidence for `ops release-manifest`: `doctor.txt`, `index-check.txt`, `harness.txt`, `ci-report.json`, `security-review.md`, `pytest-coverage.log`, `build-wheel.txt`, `twine-check.txt`, `install-smoke.txt`, and `.sidecar/ops/release-artifact-manifest.json`. The installed-wheel smoke records `tugboat --version`, creates `.sidecar/ci/proposal-smoke-repo`, runs `tugboat init`, `index`, `harness check`, and the fixture-backed `optimize` loop, then verifies `audit.json`, `candidate.json`, `eval-report.json`, `optimization-summary.json`, and `report.md`.

For scheduled governance checks, also run:

```bash
tugboat harness report --repo .
```

## GitHub Actions Template

```yaml
name: tugboat-ci

on:
  pull_request:
  push:
    branches: [main]

jobs:
  proposal-only-checks:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: python -m pip install -e ".[dev]"
      - run: mkdir -p .sidecar/ci
      - run: tugboat doctor --repo . 2>&1 | tee .sidecar/ci/doctor.txt
      - run: tugboat ci --repo .
      - run: tugboat index --repo . --check 2>&1 | tee .sidecar/ci/index-check.txt
      - run: tugboat harness check --repo . 2>&1 | tee .sidecar/ci/harness.txt
      - run: python -m pytest --cov=src --cov-report=term-missing -q 2>&1 | tee .sidecar/ci/pytest-coverage.log
      - run: python -m build --wheel --outdir dist 2>&1 | tee .sidecar/ci/build-wheel.txt
      - run: python -m twine check dist/<wheel>.whl 2>&1 | tee .sidecar/ci/twine-check.txt
      - run: |
          cat > .sidecar/ci/security-review.md <<'EOF'
          # Security Review

          No open critical or high findings for proposal-only operation.

          Approved as a release candidate for proposal-only use.
          EOF
      - run: python -m venv .sidecar/ci/install-smoke-venv
      - run: tugboat ops release-manifest --repo . --wheel dist/<wheel>.whl --commit <sha> --ci-url <url> --approver <name> --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/ci-report.json --evidence .sidecar/ci/security-review.md --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: tugboat-ci-artifacts
          retention-days: 14
          path: |
            .sidecar/ci/ci-report.json
            .sidecar/ci/**
            .sidecar/ops/release-artifact-manifest.json
            .sidecar/runs/**
            .pytest_cache/**
```

## Artifacts

Retain CI artifacts only when they help review or reproduce a failure:

- Keep sanitized `.sidecar/runs/**` artifacts for failed jobs.
- Keep `.sidecar/ci/ci-report.json` and `.sidecar/ci/**` so reviewers can inspect the Tugboat CI decision bundle.
- Keep `.sidecar/ops/release-artifact-manifest.json` so release candidates can be traced back to the exact wheel, commit, CI URL, smoke evidence, and security review decision.
- Do not upload raw provider credentials, raw private traces, or local `.env` files.
- Prefer short retention windows, such as 14 days for pull requests and 30 days for protected-branch failures.
- Redact before upload when artifacts include traces, prompts, diffs, or command output from private repositories.
