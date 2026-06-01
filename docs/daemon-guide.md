---
owner: platform
verification_status: verified
---

# Daemon Guide

## Purpose

The daemon is a local sidecar worker for queued Tugboat jobs. It is not a public service and does not grant extra authority. It uses the same service layer, policy gates, VCS adapter, audit ledger, and read-only kill switch as the CLI.

## Status

Inspect daemon state:

```bash
tugboat daemon status --repo .
```

The command prints `.sidecar/daemon.sqlite` queue state, whether `.sidecar/read-only.kill` is enabled, queued and leased job counts, and any expired lease summary. Status is read-only: it does not recover, requeue, fail, acquire, or transition jobs.

## Read-Only Kill Switch

Enable read-only mode before incidents, restores, retention review, or policy investigations:

```bash
tugboat daemon read-only --repo . --enable
tugboat daemon read-only --repo . --status
```

Disable it only after review:

```bash
tugboat daemon read-only --repo . --disable
```

The kill switch blocks direct write paths such as apply, auto-apply, rollback execution, retention deletion, migration apply, restore execution, and harness cleanup.

## Run One Job

Process a single queued job:

```bash
tugboat daemon run-once --repo . --worker-id tugboat-daemon --lease-seconds 300
```

`run-once` recovers stale leases, acquires one eligible job, records audited state transitions, and stops.

## Cycle And Watch Traces

Run a bounded cycle that discovers trace files and processes jobs:

```bash
tugboat daemon cycle --repo . \
  --trace-dir traces \
  --max-jobs 1 \
  --concurrency 1 \
  --cycles 1
```

For repeated local cycles:

```bash
tugboat daemon cycle --repo . --trace-dir traces --cycles 5 --interval-seconds 2
```

Use rate limits for noisy trace directories:

```bash
tugboat daemon cycle --repo . \
  --trace-dir traces \
  --rate-limit-window-seconds 300 \
  --rate-limit-max-jobs 3
```

## Local Socket

Serve a Unix socket inside `.sidecar`:

```bash
tugboat daemon serve --repo . --socket .sidecar/daemon.sock --max-requests 10
```

Socket paths must resolve inside the repo sidecar. Public listeners are rejected; TCP bind addresses must be local-only when address validation is used.

## Worktree Profile

Record worktree-local app boot metadata and local observability references:

```bash
tugboat daemon profile --repo . \
  --app-boot-json '{"command":"python -m app","healthcheck":"http://127.0.0.1:8000/health"}' \
  --observability-ref http://127.0.0.1:8000/health
```

Observability refs must be local-only.

## Recovery

If a worker crashes, `daemon status` reports `stuck_job_count`, `oldest_stuck_job_id`, and `oldest_stuck_lease_expires_at` for expired active leases. The next `run-once` or `cycle` recovers expired leases. Resume jobs must carry checkpoint metadata whose manifest hash still matches the current manifest. Mismatched checkpoints fail closed.

Keep `.sidecar/daemon.sqlite` and `.sidecar/db.sqlite` together during backup and restore so queue state and audit state remain comparable.
