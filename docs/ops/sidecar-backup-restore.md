# Sidecar Backup and Restore

## Purpose

The `.sidecar` directory contains Tugboat state: run artifacts, manifest copies, policy, and SQLite metadata. Back it up before upgrades, retention cleanup, restore tests, and production incident work.

## Backup

Stop writers first: pause CI jobs, daemon jobs, MCP write-intent requests, and manual `tugboat audit` or `tugboat propose` runs.

Create a timestamped archive from the repository root:

```bash
backup="sidecar-backup-$(date +%Y%m%d%H%M%S).tgz"
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
mkdir -p /tmp/tugboat-restore-check
tar -xzf "$backup" -C /tmp/tugboat-restore-check
sqlite3 /tmp/tugboat-restore-check/.sidecar/db.sqlite "PRAGMA integrity_check;"
```

When the staging check passes, replace the current sidecar:

```bash
mv .sidecar ".sidecar.pre-restore-$(date +%Y%m%d%H%M%S)"
mv /tmp/tugboat-restore-check/.sidecar .sidecar
```

## Recovery Verification

After restore, run:

```bash
tugboat status --repo .
tugboat harness check --repo .
```

Confirm the latest run, pending candidates, and indexed document count match the incident record or backup notes. Keep the pre-restore directory until the team accepts the recovery.
