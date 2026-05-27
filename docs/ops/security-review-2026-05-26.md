---
owner: platform
verification_status: verified
---

# Security Review 2026-05-26

## Scope

Release: Tugboat 0.1.0 proposal-only MVP.

Build/code artifact commit reviewed: `c462115265a3bb0c0965b62d99757a48c7097f14`.

Release evidence is retained under `.sidecar/ci` and summarized by `.sidecar/ops/release-artifact-manifest.json`.

Wheel reviewed: `dist/tugboat-0.1.0-py3-none-any.whl`.

## Commands

```bash
PYTHONPATH=src python -m tugboat doctor
PYTHONPATH=src python -m tugboat index --repo . --check
PYTHONPATH=src python -m tugboat harness check --repo .
PYTHONPATH=src pytest --cov=src --cov-report=term-missing -q
python -m build --wheel
python -m twine check dist/tugboat-0.1.0-py3-none-any.whl
python -m venv .sidecar/ci/install-smoke-venv
.sidecar/ci/install-smoke-venv/bin/python -m pip install dist/tugboat-0.1.0-py3-none-any.whl
.sidecar/ci/install-smoke-venv/bin/tugboat doctor
.sidecar/ci/install-smoke-venv/bin/tugboat index --repo . --check
.sidecar/ci/install-smoke-venv/bin/tugboat harness check --repo .
PYTHONPATH=src python -m tugboat ops release-manifest --repo . --wheel dist/tugboat-0.1.0-py3-none-any.whl --commit c462115265a3bb0c0965b62d99757a48c7097f14 --ci-url local://release-smoke/2026-05-26-c462115 --approver cheapseatsecon --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
```

Latest retained coverage evidence: 820 tests and 90.16% coverage.

## Findings

No open critical or high findings for proposal-only release.

Public-provider execution remains opt-in and requires explicit repository policy. Auto-apply remains disabled by default.

Candidate source refs must bind to declared audit evidence, and candidate diffs must target the reviewed `base_file` before VCS apply.

Accepted file-backed `llmff` eval reports must include non-overlapping trigger and held-out validation split provenance.

SQLite audit events are protected from direct update and deletion at the storage layer.

The read-only kill switch blocks direct CLI write paths before apply, auto-apply, retention deletion, migration apply, or harness cleanup writes begin.

`llmff inspect` artifacts must declare explicit external calls, and provider declarations must be represented as provider external-call targets.

Sidecar run directories and raw/runtime trace artifacts use owner-only filesystem permissions. This covers raw traces, redacted traces, instruction snapshots, instruction graphs, audit artifacts, `llmff` lifecycle traces/events, checkpoints, and declared outputs.

## Decision

Approved for proposal-only release.

Provider-backed pipelines and broad auto-apply are not approved by this review.
