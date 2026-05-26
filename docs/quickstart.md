---
owner: platform
verification_status: verified
---

# Tugboat Quickstart

## Install

Install the package in the repository environment, then verify the CLI:

```bash
tugboat doctor
```

The default posture should be proposal-only with auto-apply disabled.

## Initialize

Index the current instruction corpus:

```bash
tugboat index --repo .
```

For CI or dry-run adoption, use:

```bash
tugboat index --repo . --check
```

## Proposal Loop

Run the governed local optimization loop against a saved trace bundle:

```bash
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

This runs audit, proposal, held-out evaluation, and the final acceptance gate. It writes `optimization-summary.json` next to the candidate and eval artifacts.

For debugging or CI decomposition, the same loop can be run step by step:

```bash
tugboat audit --repo . --trace traces/example.jsonl
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
tugboat report --repo . --run latest
```

This is designed to work under 15 minutes for an existing repo and does not require provider credentials in proposal-only mode when `llmff` is configured for local or fixture-backed manifests. `--mock-llmff-inspect` is audit-only smoke-test mode and cannot feed `propose`.

Live provider smoke checks are opt-in. Without opt-in, the suite records a skipped live-provider report instead of making a provider call:

```bash
tugboat eval --repo . --candidate latest --suite provider-smoke
```

To enable the smoke preflight for a repo, add explicit policy and provide the local smoke command:

```yaml
llmff:
  allowed_providers:
    - openai

provider_smoke:
  enabled: true
  provider: openai
  command: "python scripts/provider_smoke.py"
```

`llmff.allowed_providers` is required for provider-backed manifests; keep it empty or omitted for credential-free local and fixture-backed runs.

For one-off local checks, `TUGBOAT_PROVIDER_SMOKE_PROVIDER` and `TUGBOAT_PROVIDER_SMOKE_COMMAND` can fill in local values after repo policy enables provider smoke. Environment variables do not authorize provider-backed runs by themselves.

## Next Checks

Before applying any generated diff, run:

```bash
tugboat harness check --repo .
python -m pytest -q
```

Review `.sidecar/runs/<run-id>/candidate.diff`, `eval-report.json`, `optimization-summary.json`, and `report.md`.
