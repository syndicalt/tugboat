---
owner: platform
verification_status: verified
---

# Security Review 2026-05-26

## Scope

Release: Tugboat 0.1.0 proposal-only MVP.

Build/code artifact commit reviewed: `271347f`.

Release evidence is maintained in the release documentation commits after that build artifact.

Wheel reviewed: `dist/tugboat-0.1.0-py3-none-any.whl`.

## Commands

```bash
PYTHONPATH=src python -m tugboat doctor
PYTHONPATH=src python -m tugboat index --repo . --check
PYTHONPATH=src python -m tugboat harness check --repo .
PYTHONPATH=src pytest --cov=src --cov-report=term-missing -q
python -m build --wheel
PYTHONPATH=src python -m tugboat ops release-manifest --repo . --wheel dist/tugboat-0.1.0-py3-none-any.whl --commit 271347f --ci-url local://release-smoke/2026-05-26 --approver cheapseatsecon --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt
```

## Findings

No open critical or high findings for proposal-only release.

Public-provider execution remains opt-in and requires explicit repository policy. Auto-apply remains disabled by default.

## Decision

Approved for proposal-only release.

Provider-backed pipelines and broad auto-apply are not approved by this review.
