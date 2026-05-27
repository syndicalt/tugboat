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
tugboat doctor
tugboat ci --repo .
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

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
      - run: tugboat doctor
      - run: tugboat ci --repo .
      - run: tugboat index --repo . --check
      - run: tugboat harness check --repo .
      - run: python -m pytest --cov=src --cov-report=term-missing -q
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: tugboat-ci-artifacts
          retention-days: 14
          path: |
            .sidecar/ci/ci-report.json
            .sidecar/ci/**
            .sidecar/runs/**
            .pytest_cache/**
```

## Artifacts

Retain CI artifacts only when they help review or reproduce a failure:

- Keep sanitized `.sidecar/runs/**` artifacts for failed jobs.
- Keep `.sidecar/ci/ci-report.json` and `.sidecar/ci/**` so reviewers can inspect the Tugboat CI decision bundle.
- Do not upload raw provider credentials, raw private traces, or local `.env` files.
- Prefer short retention windows, such as 14 days for pull requests and 30 days for protected-branch failures.
- Redact before upload when artifacts include traces, prompts, diffs, or command output from private repositories.
