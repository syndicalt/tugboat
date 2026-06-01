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

GitHub Actions upload artifacts are short-lived CI scratch evidence. For a stable release, attach `.sidecar/ops/release-artifact-manifest.json`, coverage, build, twine, install-smoke, security-review, and CI logs to the GitHub Release or another durable release archive for at least one year.

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
      - run: |
          set -o pipefail
          python -m build --wheel --outdir dist 2>&1 | tee .sidecar/ci/build-wheel.txt
          WHEEL="$(ls dist/tugboat-*.whl | sort | tail -n 1)"
          echo "built ${WHEEL}" | tee -a .sidecar/ci/build-wheel.txt
      - run: |
          set -o pipefail
          WHEEL="$(ls dist/tugboat-*.whl | sort | tail -n 1)"
          python -m twine check "${WHEEL}" 2>&1 | tee .sidecar/ci/twine-check.txt
      - run: |
          cat > .sidecar/ci/security-review.md <<'EOF'
          # Security Review

          No open critical or high findings for proposal-only operation.

          Approved as a release candidate for proposal-only use.
          EOF
      - run: |
          set -o pipefail
          WHEEL="$(ls dist/tugboat-*.whl | sort | tail -n 1)"
          python -m venv .sidecar/ci/install-smoke-venv
          {
            .sidecar/ci/install-smoke-venv/bin/python -m pip install "${WHEEL}"
            echo "installed tugboat wheel: ${WHEEL}"
            echo "installed tugboat --version"
            .sidecar/ci/install-smoke-venv/bin/tugboat --version
            echo "installed tugboat doctor"
            .sidecar/ci/install-smoke-venv/bin/tugboat doctor --repo .
            echo "installed tugboat index --repo . --check"
            .sidecar/ci/install-smoke-venv/bin/tugboat index --repo . --check
            echo "installed tugboat harness check --repo ."
            .sidecar/ci/install-smoke-venv/bin/tugboat harness check --repo .
            python - <<'PY'
          from pathlib import Path
          repo = Path(".sidecar/ci/proposal-smoke-repo")
          repo.mkdir(parents=True, exist_ok=True)
          (repo / "AGENTS.md").write_text("# Agent Instructions\n\nKeep changes reviewed.\n", encoding="utf-8")
          PY
            echo "installed tugboat init --repo .sidecar/ci/proposal-smoke-repo"
            .sidecar/ci/install-smoke-venv/bin/tugboat init --repo .sidecar/ci/proposal-smoke-repo
            echo "installed tugboat index --repo .sidecar/ci/proposal-smoke-repo"
            .sidecar/ci/install-smoke-venv/bin/tugboat index --repo .sidecar/ci/proposal-smoke-repo
            echo "installed tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all"
            .sidecar/ci/install-smoke-venv/bin/tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all
            run_dir="$(find .sidecar/ci/proposal-smoke-repo/.sidecar/runs -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
            for artifact in audit.json candidate.json eval-report.json optimization-summary.json report.md; do
              test -f "${run_dir}/${artifact}"
            done
            echo "proposal smoke artifact: audit.json"
            echo "proposal smoke artifact: candidate.json"
            echo "proposal smoke artifact: eval-report.json"
            echo "proposal smoke artifact: optimization-summary.json"
            echo "proposal smoke artifact: report.md"
          } 2>&1 | tee .sidecar/ci/install-smoke.txt
      - run: |
          WHEEL="$(ls dist/tugboat-*.whl | sort | tail -n 1)"
          tugboat ops release-manifest --repo . --wheel "${WHEEL}" --commit <sha> --ci-url <url> --approver <name> --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/ci-report.json --evidence .sidecar/ci/security-review.md --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
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
