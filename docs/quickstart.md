---
owner: platform
verification_status: verified
---

# Tugboat Quickstart

## Install

Install the current release into the repository environment, then verify the CLI:

```bash
python -m pip install tugboat
```

For a local wheel release, install the retained wheel artifact instead:

```bash
python -m pip install dist/tugboat-1.0.0-py3-none-any.whl
```

Verify the installed command:

```bash
tugboat doctor
```

The default posture should be proposal-only with auto-apply disabled.

## Initialize

Bootstrap proposal-only local policy, then index the current instruction corpus:

```bash
tugboat init
tugboat index --repo .
```

The generated policy uses Tugboat's shipped local fixture backend for `llmff`, with `allow_network: false` and no `allowed_providers`, so the proposal loop works without provider credentials.

For CI or dry-run adoption, use:

```bash
tugboat index --repo . --check
```

## Solo Local

Use the local path when you are evaluating Tugboat in one repository without provider credentials:

```bash
tugboat doctor
tugboat init
tugboat index --repo .
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

Inspect `.sidecar/runs/<run-id>/report.md` and `candidate.diff` before deciding whether to apply anything. The default policy remains proposal-only.

## Team Proposal-Only

Use the team path when candidates need review through normal branch or pull-request workflow:

```bash
tugboat optimize --repo . --trace traces/example.jsonl --suite all
tugboat report --repo . --run latest
tugboat inspect-decision --repo . --decision latest
tugboat apply --repo . --candidate latest --mode pr --human-review --review-actor <name>
```

`apply --mode pr` is VCS-gated and review-oriented. Keep humans in the approval path, inspect `optimization-summary.json`, and reject candidates that weaken safety, broaden authority, or lack evidence.

## CI Adoption

Use CI to prove the repo remains ready for proposal-only Tugboat workflows:

```bash
tugboat doctor
tugboat ci --repo .
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

CI must not run apply, auto-apply, rollback --execute, or mutation modes. Retain `.sidecar/ci/ci-report.json`, harness output, and any generated review artifacts for human inspection.

## Proposal Loop

Run the governed local optimization loop against a saved trace bundle:

```bash
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

Tugboat auto-detects generic JSONL, Codex JSONL, Claude transcript JSON/JSONL, MCP session JSONL, and CI failure JSON traces. Use `--trace-format` only when you need to override detection.

For a training minibatch, pass multiple trace files and record any held-out or unseen validation metadata explicitly:

```bash
tugboat optimize --repo . --trace traces/failure.jsonl --train-trace traces/success.jsonl --suite held-out --held-out-episode held-out:no-regression --unseen-suite governance
```

This runs audit, proposal, held-out evaluation, and the final acceptance gate. It writes `optimization-summary.json` next to the candidate and eval artifacts.

For debugging or CI decomposition, the same loop can be run step by step:

```bash
tugboat audit --repo . --trace traces/example.jsonl
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
tugboat report --repo . --run latest
```

This is designed to work under 15 minutes for an existing repo and does not require provider credentials in proposal-only mode after `tugboat init`; the default local fixture backend emits the same file-backed manifest outputs used by the proposal loop. `--mock-llmff-inspect` is audit-only smoke-test mode and cannot feed `propose`.

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

Review `.sidecar/runs/<run-id>/candidate.diff`, `eval-report.json`, `optimization-summary.json`, and `report.md`. When the candidate rewrites `SKILL.md`, inspect `eval-report.json` `skill_report` before applying; failing trigger, executability, ambiguity, overfit, section, token-footprint, or safety checks should stay in review.

For the complete command surface, see [docs/cli-reference.md](cli-reference.md). For reviewed mutation workflows, see [docs/apply-rollback.md](apply-rollback.md). For instruction-file structure, see [docs/instruction-best-practices.md](instruction-best-practices.md). For auto-apply, daemon, and troubleshooting operations, see [docs/auto-apply.md](auto-apply.md), [docs/daemon-guide.md](daemon-guide.md), and [docs/troubleshooting.md](troubleshooting.md).

## Read-Only MCP

After `tugboat init --repo .`, attach MCP clients with the read-only profile:

```bash
tugboat mcp stdio --repo . --read-only
```

This profile exposes read tools such as status, instruction graph, latest runs, harness findings, candidate reports, and decision-trace artifact refs. It does not advertise write-intent tools and rejects repo overrides.
