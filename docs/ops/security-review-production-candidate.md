---
owner: platform
verification_status: verified
---

# Security Review Production Candidate

## Scope

Production release candidate for Tugboat's proposal-only roadmap implementation.

Build/code artifact commit reviewed: `6e59c42b8d0a3d248c3e33ccaa1e1d0f19dbb248`.

## Commands

```bash
PYTHONPATH=src python -m tugboat ci --repo .
pytest tests/test_docs_ops.py tests/test_harness_legibility.py tests/test_cli_ci.py -q
pytest tests/test_docs_ops.py tests/test_cli_ops_release_manifest.py tests/test_cli_ops_observability.py tests/test_cli_ops_backup.py tests/test_cli_ops_migrations.py tests/test_ops_retention.py -q
pytest --cov=src/tugboat --cov-report=term-missing -q
PYTHONPATH=src python -m tugboat ops release-manifest --repo . --wheel dist/tugboat-0.1.0-py3-none-any.whl --commit 6e59c42b8d0a3d248c3e33ccaa1e1d0f19dbb248 --ci-url local://production-candidate/2026-05-27-6e59c42 --approver cheapseatsecon --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
```

Latest retained coverage evidence: 1097 tests and 90.02% coverage.

## Findings

No open critical or high findings for proposal-only operation.

The reviewed posture is `proposal_only` with `auto_apply: disabled`. Provider-backed `llmff` pipelines remain opt-in and require explicit repository policy. MCP exposes read tools and write-intent request tools, but no MCP tool may directly mutate instruction files.

Direct instruction mutation remains prohibited outside the VCS adapter. Policy gates reject unsafe candidate changes before review, accepted candidates require held-out validation plus governance evidence, and rollback paths are auditable.

Daemon and auto-apply expansion remain frozen unless a test proves a safety invariant. The current release candidate includes auto-apply reversibility proof only for the narrow gated lane, not broad autonomous operation.

## Decision

Approved as a production release candidate for proposal-only use.

Not approved: broad auto-apply, public daemon listeners, provider credential management through MCP, direct apply through MCP, or weakening approval, sandbox, secrets, network, memory, deployment, or permission constraints.
