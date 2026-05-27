---
owner: platform
verification_status: verified
---

# Tugboat Production Release Candidate

## Summary

This release candidate advances Tugboat beyond the MVP into production-readiness evidence for the 2026-05-25 roadmap. The default posture remains proposal-only: provider-backed execution requires explicit policy, auto-apply remains disabled by default, and review/apply flows stay VCS-gated.

## Scope

- Real Codex episode context is captured as canonical policy evidence.
- Rejected edit memory suppresses repeated harmful proposal directions.
- Incident replay is required before eval acceptance.
- MCP proposal and eval requests remain non-mutating write-intent artifacts.
- Auto-apply reversibility is proven only as a narrow safety invariant, not as the default release posture.
- Phase 10 docs and operations artifacts are referenced from the instruction map and carry ownership plus verification metadata.

## Verification

Build/code artifact commit: `6e59c42b8d0a3d248c3e33ccaa1e1d0f19dbb248`.

- `PYTHONPATH=src python -m tugboat ci --repo .` passed with `ci: ok`.
- `pytest tests/test_docs_ops.py tests/test_harness_legibility.py tests/test_cli_ci.py -q` passed with 59 tests.
- `pytest tests/test_docs_ops.py tests/test_cli_ops_release_manifest.py tests/test_cli_ops_observability.py tests/test_cli_ops_backup.py tests/test_cli_ops_migrations.py tests/test_ops_retention.py -q` passed with 64 tests.
- `pytest --cov=src/tugboat --cov-report=term-missing -q` passed with 1097 tests and 90.02% coverage.
- `PYTHONPATH=src python -m tugboat ops release-manifest --repo . --wheel dist/tugboat-0.1.0-py3-none-any.whl --commit 6e59c42b8d0a3d248c3e33ccaa1e1d0f19dbb248 --ci-url local://production-candidate/2026-05-27-6e59c42 --approver cheapseatsecon --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 ...` is the production-candidate release-manifest command shape.

## Open Release Work

- Build a fresh distribution artifact after the production-release version is finalized.
- Run clean virtualenv install smoke against that artifact.
- Write the final release manifest using the finalized wheel path and retained evidence logs.

## Decision

Approved as a production release candidate for proposal-only operation. It is not approval for broad auto-apply, public daemon exposure, provider credential management through MCP, or direct instruction mutation.
