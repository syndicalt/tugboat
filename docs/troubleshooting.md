---
owner: platform
verification_status: verified
---

# Troubleshooting

## First Checks

Start with these commands:

```bash
tugboat doctor
tugboat status --repo .
tugboat harness check --repo .
tugboat harness report --repo .
```

If a run exists, inspect:

```bash
tugboat report --repo . --run latest
tugboat inspect-decision --repo . --decision latest
```

## Init And Policy

`init blocked: .sidecar/policy.yaml already exists`

Review the existing policy instead of overwriting it. Confirm `mode: proposal_only`, `auto_apply.enabled: false`, and `llmff.allow_network: false` unless the repo intentionally opts into provider-backed runs.

`manifest hash is not allowed by policy`

Run `tugboat ci --repo .` to materialize manifests, then review `.sidecar/manifests`. Add hashes only after manifest review.

## llmff Failures

`audit blocked: trace file not found`

Create or export the trace file first, then rerun with the same `--trace` path. For a quick local smoke test, use `tests/fixtures/traces/codex-local-session-export.jsonl`.

`audit blocked: invalid trace`

Validate that the trace is JSONL or JSON matching the selected `--trace-format`. Use `--trace-format auto` for generic JSONL, Codex JSONL, Claude transcript JSON/JSONL, MCP session JSONL, or CI failure JSON traces.

`audit blocked: invalid trace: unsupported JSON trace format`

The file is a JSON object that does not match a supported object-based trace shape, such as Claude transcript JSON or CI failure JSON. Export the session as JSONL, use a supported trace fixture shape, or pass an explicit non-auto `--trace-format` only when the file truly matches that adapter.

`instruction index blocked: llmff inspect failed: binary not found`

Check `.sidecar/policy.yaml` and verify `llmff.binary`. The default after `tugboat init --repo .` is `python -m tugboat.llmff.fixture_backend`.

`llmff inspect failed`

Inspect `.sidecar/runs/<run-id>/<manifest>/llmff-inspect.json`. Provider-backed runs must declare network requirements, providers, and external calls, and repo policy must allow them.

`llmff patch-eval failed with exit code`

Inspect `.sidecar/runs/<run-id>/patch-eval/llmff-events.jsonl`, `.sidecar/runs/<run-id>/eval-report.raw.json`, and `.sidecar/runs/<run-id>/policy-decision.raw.json`.

## Eval And Acceptance

`eval rejected: llmff eval_report cannot accept without validation split provenance`

The eval report must include separate triggering and held-out validation cases. Do not use the triggering episode as the only validation case.

`eval rejected: held_out_score must strictly improve`

Review the validation baseline in optimizer memory. A candidate can be plausible and still fail the acceptance gate.

`governance_passed` is false

Treat the candidate as rejected. Governance regressions override accept recommendations.

## Apply And Rollback

`apply blocked: dirty target`

Commit, stash, or discard local target-file edits before applying.

`apply blocked: base hash`

Regenerate the candidate from the current file state. Do not hand-edit candidate hashes.

`apply blocked: policy gate rejected candidate`

Inspect `policy-gate.json` and `decision-trace.json`. Common causes are protected headings, Class C review requirements, Class D prohibited topics, or rejected-edit memory suppression.

`rollback blocked: git revert`

Resolve or abort the VCS revert state manually, preserve `.sidecar/runs/<run-id>/rollback-plan.json` if present, then rerun after the repository is clean.

## MCP

MCP tools return repo allowlist errors when `.sidecar/policy.yaml` lacks the absolute repo path under `mcp.allowed_repositories`.

Bound read-only stdio:

```bash
tugboat mcp stdio --repo . --read-only
```

This profile rejects repo overrides and does not expose write-intent tools.

## Daemon

`daemon serve blocked: socket_path must resolve inside repo sidecar`

Use a socket path such as `.sidecar/daemon.sock`.

Queued jobs are not moving:

```bash
tugboat daemon status --repo .
tugboat daemon read-only --repo . --status
tugboat daemon run-once --repo .
```

If the kill switch is enabled, queued jobs will not execute. If status reports `stuck_job_count` greater than zero, `run-once` or `cycle` recovers expired leases before acquiring work.

## Operations

`migration blocked: sidecar schema version <n> is newer than supported version <m>`

Stop and upgrade Tugboat before running more commands against that sidecar. A newer `.sidecar/version.json` means the sidecar may contain schema or artifact semantics this binary does not understand; older binaries must not migrate, apply, or reinterpret it.

## Secrets And Redaction

`retention blocked: scan budget exceeded`

Increase `retention.max_scan_files` in `.sidecar/policy.yaml` only after reviewing `.sidecar/runs` growth, or archive/delete old sidecar runs manually. Tugboat blocks before deletion or redaction export when the scan budget is exceeded.

If a secret appears in a trace, prompt, event, checkpoint, diff, or report:

```bash
tugboat daemon read-only --repo . --enable
tugboat retention --repo . --redact-output /tmp/tugboat-redacted
```

Do not upload raw `.sidecar/runs/**` artifacts until redaction is reviewed.

## Release Evidence

`release manifest blocked: commit does not match current HEAD`

Commit tracked docs/tests first, then regenerate the manifest with:

```bash
tugboat ops release-manifest --repo . --wheel dist/<wheel>.whl --commit "$(git rev-parse HEAD)" --ci-url <url> --approver <name> --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/ci-report.json --evidence .sidecar/ci/security-review.md --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
```

`release manifest blocked: pytest coverage evidence did not pass`

Regenerate retained evidence with the full coverage report; the release gate requires a parseable total coverage percentage at or above 90%:

```bash
python -m pytest --cov=src --cov-report=term-missing -q 2>&1 | tee .sidecar/ci/pytest-coverage.log
```

`release manifest blocked: CI evidence is required`

Run `tugboat ci --repo .` and retain `.sidecar/ci/ci-report.json` with the other release evidence.

`release manifest blocked: security review evidence is required`

Retain an approved security-review artifact at `.sidecar/ci/security-review.md` or another path whose filename contains `security-review`. The file must state that there are no open critical or high findings.

`release manifest blocked: install smoke evidence did not pass`

Regenerate installed-wheel smoke from a clean virtual environment and retain the installed CLI command output:

```bash
python -m venv .sidecar/ci/install-smoke-venv
.sidecar/ci/install-smoke-venv/bin/python -m pip install dist/<wheel>.whl
{
  echo "installed tugboat wheel: dist/<wheel>.whl"
  echo "installed tugboat doctor"
  .sidecar/ci/install-smoke-venv/bin/tugboat doctor
  echo "installed tugboat index --repo . --check"
  .sidecar/ci/install-smoke-venv/bin/tugboat index --repo . --check
  echo "installed tugboat harness check --repo ."
  .sidecar/ci/install-smoke-venv/bin/tugboat harness check --repo .
} 2>&1 | tee .sidecar/ci/install-smoke.txt
```
