---
owner: platform
verification_status: verified
---

# CLI Reference

## Core Workflow

All commands run from a repository root unless `--repo` points elsewhere.

```bash
tugboat doctor
tugboat doctor --repo .
tugboat init --repo .
tugboat index --repo .
tugboat status --repo .
```

- `doctor` prints installed CLI health, repo policy posture, provider/network posture, manifest policy, and actionable recommendations.
- `init` writes `.sidecar/policy.yaml` and `.sidecar/.gitignore`; it refuses to overwrite an existing policy.
- `index` parses configured instruction files and stores document/chunk metadata. Add `--check` for dry-run validation without writing index rows.
- `status` writes `.sidecar/status-report.json` and prints mode, auto-apply state, latest run, latest `llmff` job, pending candidates, retention candidates, and manifest policy.

## Proposal Pipeline

Use `optimize` for the normal end-to-end proposal loop:

```bash
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

Use the decomposed commands when debugging or when CI needs smaller stages:

```bash
tugboat audit --repo . --trace traces/example.jsonl --trace-format auto
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
tugboat report --repo . --run latest
tugboat inspect-decision --repo . --decision latest
```

Supported `--trace-format` values are `auto`, `generic-jsonl`, `codex`, `claude`, `ci`, and `mcp`.

`optimize` also accepts training and validation metadata:

```bash
tugboat optimize --repo . \
  --trace traces/failure.jsonl \
  --train-trace traces/success.jsonl \
  --suite held-out \
  --held-out-episode held-out:no-regression \
  --unseen-suite governance
```

`audit` supports `--mock-llmff-inspect` for audit-only smoke tests. Do not use mock inspect output as proposal evidence.

## Review And Change Control

Generated candidates are review artifacts until an apply command moves through the VCS adapter:

```bash
tugboat apply --repo . --candidate latest --mode proposal
tugboat apply --repo . --candidate latest --mode branch --human-review --review-actor <name>
tugboat apply --repo . --candidate latest --mode commit --human-review --review-actor <name>
tugboat apply --repo . --candidate latest --mode pr --human-review --review-actor <name>
tugboat rollback --repo . --decision latest
tugboat rollback --repo . --decision latest --execute
```

`proposal` mode writes an apply plan without mutating instruction files. `branch`, `commit`, and `pr` modes require clean/stale-base checks, policy gate proof, eval evidence, and VCS adapter execution.

## Auto-Apply

Auto-apply is a separate, narrow lane:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name>
tugboat auto-apply --repo . --candidate latest --actor <name> \
  --confirm-auto-apply \
  --auto-apply-policy-version 1
```

The command delegates to commit-mode apply with auto-apply gates enabled. It remains blocked unless policy, confirmation, lane match, ledger-derived burn-in and reliability metrics, eval, governance, VCS, and rollback evidence all pass. Runtime arguments confirm intent; policy owns thresholds such as `docs_hygiene.minimum_burn_in_days: 3`, `docs_hygiene.maximum_rejection_rate: 0.20`, and `skill_improvement.maximum_rollback_rate: 0.03`.

## Harness And CI

```bash
tugboat harness check --repo .
tugboat harness report --repo .
tugboat harness cleanup --repo .
tugboat ci --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

`harness cleanup` writes review-only cleanup candidates and is blocked by `.sidecar/read-only.kill`. `ci` writes `.sidecar/ci/ci-report.json` and checks manifest contracts, semantic policy lint, harness health, and optional eval evidence.

## MCP And Daemon

```bash
tugboat mcp stdio --repo . --read-only
tugboat daemon status --repo .
tugboat daemon run-once --repo .
tugboat daemon cycle --repo . --trace-dir traces --max-jobs 1
tugboat daemon serve --repo . --socket .sidecar/daemon.sock --max-requests 1
tugboat daemon read-only --repo . --enable
tugboat daemon read-only --repo . --disable
tugboat daemon profile --repo . --app-boot-json '{"command":"python -m app"}'
```

MCP is an adapter over the CLI/service layer. The daemon is local-only and kill-switchable.

## Operations

```bash
tugboat retention --repo .
tugboat retention --repo . --redact-output /tmp/tugboat-redacted
tugboat retention --repo . --apply
tugboat ops migrate --repo .
tugboat ops migrate --repo . --apply
tugboat ops observability --repo .
tugboat ops backup --repo . --archive /tmp/tugboat-sidecar.tgz
tugboat ops backup --repo . --archive /tmp/tugboat-sidecar.tgz --execute
tugboat ops restore --repo . --archive /tmp/tugboat-sidecar.tgz --staging /tmp/tugboat-restore --pre-restore /tmp/tugboat-pre-restore
tugboat ops release-manifest --repo . --wheel dist/<wheel>.whl --commit "$(git rev-parse HEAD)" --ci-url <url> --approver <name> --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/pytest-coverage.log
```

Operations commands write reviewable artifacts under `.sidecar/ops`. Destructive operations require explicit apply or execute flags and are blocked by the read-only kill switch where applicable.
