---
owner: platform
verification_status: verified
---

# Sidecar Backup and Restore

## Purpose

The `.sidecar` directory contains Tugboat state: run artifacts, manifest copies, policy, and SQLite metadata. Back it up before upgrades, retention cleanup, restore tests, and production incident work.

## Backup

Stop writers first: pause CI jobs, daemon jobs, MCP write-intent requests, and manual `tugboat audit` or `tugboat propose` runs.

Create and verify a backup before `tugboat ops migrate --repo . --apply` or before upgrading Tugboat across a release that changes `.sidecar` schema or artifact semantics. Older Tugboat binaries intentionally block newer sidecars; if `tugboat ops migrate --repo .` reports a sidecar schema version newer than supported, upgrade Tugboat and restore only from a backup created by a compatible or older Tugboat version.

Write the Tugboat backup plan before running shell commands:

```bash
backup="sidecar-backup-$(date +%Y%m%d%H%M%S).tgz"
tugboat ops backup --repo . --archive "$backup"
```

This writes `.sidecar/ops/backup-plan.json` with the archive, checksum, integrity-check, and status commands. Tugboat does not execute the plan; the operator remains responsible for running the approved commands in the target environment.

When local execution is approved, Tugboat can create the archive, write the checksum, verify it, and check SQLite integrity:

```bash
tugboat ops backup --repo . --archive "$backup" --execute
```

Create a timestamped archive from the repository root:

```bash
tar -czf "$backup" .sidecar
sha256sum "$backup" > "$backup.sha256"
```

Store the archive and checksum in the team's approved backup location. Do not commit the backup archive to the repository.

## Integrity Check

Verify the archive and SQLite database before declaring the backup usable:

```bash
sha256sum -c "$backup.sha256"
sqlite3 .sidecar/db.sqlite "PRAGMA integrity_check;"
tugboat status --repo .
```

The SQLite command must return `ok`. If it does not, keep the archive but mark it unusable for restore until investigated.

## Restore

Restore into a clean staging path first:

```bash
staging="$(mktemp -d /tmp/tugboat-restore-check.XXXXXX)"
pre_restore=".sidecar.pre-restore-$(date +%Y%m%d%H%M%S)"
tugboat ops restore --repo . --archive "$backup" --staging "$staging" --pre-restore "$pre_restore"
tar -xzf "$backup" -C "$staging"
sqlite3 "$staging/.sidecar/db.sqlite" "PRAGMA integrity_check;"
```

This writes `.sidecar/ops/restore-plan.json` with the staging, integrity-check, sidecar move, status, and harness-check commands. Review the plan before replacing the current `.sidecar`.

When local restore execution is approved, Tugboat verifies the archive checksum when present, extracts into the clean staging path, checks the staged SQLite database, moves the current sidecar to the pre-restore path, restores the staged sidecar, and removes staging:

```bash
tugboat ops restore --repo . --archive "$backup" --staging "$staging" --pre-restore "$pre_restore" --execute
```

Restore execution is blocked while `.sidecar/read-only.kill` exists.

When the staging check passes, replace the current sidecar:

```bash
mv .sidecar "$pre_restore"
mv "$staging/.sidecar" .sidecar
```

## Recovery Verification

After restore, run:

```bash
tugboat status --repo .
tugboat harness check --repo .
```

Confirm the latest run, pending candidates, and indexed document count match the incident record or backup notes. Keep the pre-restore directory until the team accepts the recovery.
