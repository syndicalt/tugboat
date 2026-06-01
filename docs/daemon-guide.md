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

## User Services

Run the daemon as a non-root user service only after the repo has passed `tugboat doctor --repo .`, `tugboat harness check --repo .`, and an operator has reviewed `.sidecar/policy.yaml`. User services should run bounded `daemon cycle` commands, not public listeners, and should rely on `.sidecar/read-only.kill` for emergency write-path shutdown.

Linux `systemd --user` example:

```ini
[Unit]
Description=Tugboat local sidecar cycle
After=default.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/repo
ExecStart=/usr/bin/env tugboat daemon cycle --repo /absolute/path/to/repo --trace-dir traces --cycles 1 --interval-seconds 60 --max-jobs 1 --concurrency 1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Install and inspect it as the repository owner:

```bash
mkdir -p ~/.config/systemd/user
systemctl --user daemon-reload
systemctl --user enable --now tugboat.service
systemctl --user status tugboat.service
journalctl --user -u tugboat.service
```

macOS LaunchAgent example:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.tugboat</string>
  <key>WorkingDirectory</key>
  <string>/absolute/path/to/repo</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>tugboat</string>
    <string>daemon</string>
    <string>cycle</string>
    <string>--repo</string>
    <string>/absolute/path/to/repo</string>
    <string>--trace-dir</string>
    <string>traces</string>
    <string>--cycles</string>
    <string>1</string>
    <string>--interval-seconds</string>
    <string>60</string>
    <string>--max-jobs</string>
    <string>1</string>
    <string>--concurrency</string>
    <string>1</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StartInterval</key>
  <integer>60</integer>
</dict>
</plist>
```

Load and inspect it as the logged-in user:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.tugboat.plist
launchctl print "gui/$(id -u)/com.example.tugboat"
launchctl bootout "gui/$(id -u)/com.example.tugboat"
```

For either service manager, keep stdout/stderr logs local, keep the working directory repo-specific, and do not run `daemon serve` as a public listener. To stop mutation during an incident, create the read-only kill switch with `tugboat daemon read-only --repo . --enable` and confirm `.sidecar/read-only.kill` exists before restarting the service. Use `tugboat daemon status --repo .` and one manual `tugboat daemon run-once --repo .` after review to recover stale leases.

## Recovery

If a worker crashes, `daemon status` reports `stuck_job_count`, `oldest_stuck_job_id`, and `oldest_stuck_lease_expires_at` for expired active leases. The next `run-once` or `cycle` recovers expired leases. Resume jobs must carry checkpoint metadata whose manifest hash still matches the current manifest. Mismatched checkpoints fail closed.

Keep `.sidecar/daemon.sqlite` and `.sidecar/db.sqlite` together during backup and restore so queue state and audit state remain comparable.
