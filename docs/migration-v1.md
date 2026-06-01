---
owner: platform
verification_status: draft
---

# Tugboat V1 Migration Guide

## Purpose

This guide documents the supported migration path from Tugboat 0.x releases, starting with 0.1.0, to Tugboat 1.0.0. The v1 posture stays conservative: proposal-only by default, auto-apply remains opt-in, and local operator review remains the primary control.

## Supported Upgrade Path

The supported path is:

1. Upgrade the Tugboat package.
2. Back up the repository `.sidecar` directory.
3. Run a dry migration check.
4. Review the migration plan.
5. Apply the migration only when the plan is understood.
6. Re-run doctor, index check, harness check, and the relevant proposal loop.

Tugboat records sidecar schema state in `.sidecar/version.json`. Older 0.x sidecars may also carry the legacy `.sidecar/VERSION` marker. If either sidecar schema marker is newer than supported by the installed binary, `tugboat ops migrate --repo .` blocks instead of attempting a downgrade or lossy read.

Unmarked 0.x sidecars are treated as legacy schema v1. A `policy.yaml version` field is policy metadata, not the sidecar schema marker, and migration preserves it while writing `.sidecar/version.json`.

## Before Upgrading

From the repository root, collect a backup and baseline status:

```bash
tugboat status --repo .
tugboat ops backup --repo . --archive /tmp/tugboat-sidecar-before-v1.tgz
tugboat ops backup --repo . --archive /tmp/tugboat-sidecar-before-v1.tgz --execute
```

Keep the backup procedure aligned with `docs/ops/sidecar-backup-restore.md`. Do not delete old release evidence until the upgraded CLI can read status, run harness checks, and inspect existing decision artifacts.

## Migration Procedure

Start with a dry run:

```bash
tugboat ops migrate --repo .
```

The dry run prints pending migration steps and does not mutate `.sidecar`.

Apply only after review:

```bash
tugboat ops migrate --repo . --apply
```

The apply path writes `.sidecar/migrations/pre-migration-state.json` before mutation and `.sidecar/migrations/migration-report.json` after migration. Both files use validated artifact schemas. The snapshot records the previous sidecar version marker and policy files so migration review has concrete restore evidence. It is secret-scanned and written owner-only, but it may still contain local policy details; do not share it unredacted. The read-only kill switch blocks migration apply, so disable read-only mode only after incident review is complete.

After applying, run:

```bash
tugboat doctor --repo .
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

## Compatibility Policy

The v1 CLI and artifact compatibility policy is maintained in `docs/compatibility-policy.md`.

For v1, stable commands should remain compatible unless a documented deprecation path exists. Artifact schema changes require either backward-compatible readers or an explicit migration. Older binaries may block newer sidecars, but they should not silently rewrite or downgrade them.

## Known Limitations

- No public network daemon.
- No hosted web UI.
- No multi-user team server mode.
- No provider credential management through MCP.
- No broad auto-apply beyond narrow, policy-gated Class A lanes.
- Provider-backed `llmff` paths require explicit policy opt-in and reviewed provider configuration.

## Rollback

If an upgraded package or migration fails validation, stop daemon and auto-apply activity first:

```bash
tugboat daemon read-only --repo . --enable
```

Then reinstall the prior package and restore the pre-upgrade `.sidecar` backup using `docs/ops/sidecar-backup-restore.md`.

Do not manually edit `.sidecar/version.json` to force compatibility. Use the recorded backup, `.sidecar/migrations/pre-migration-state.json`, or a reviewed migration instead.
