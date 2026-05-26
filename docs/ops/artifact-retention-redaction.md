# Artifact Retention and Redaction Controls

## Purpose

Tugboat artifacts make proposals reproducible, but traces and command output can contain private data. Retention must keep decision evidence while reducing long-lived exposure in `.sidecar/runs`.

## Retention Classes

Default retention targets:

| Class | Examples | Retention |
| --- | --- | --- |
| Raw trace inputs | `trace-input.jsonl`, transcript exports | 14 days |
| Lifecycle artifacts | inspect reports, events, checkpoints | 30 days |
| Review artifacts | `audit.json`, `candidate.diff`, `eval-report.json`, `optimization-summary.json`, `report.md` | 180 days |
| Decisions | accepted, rejected, rollback, release evidence | 1 year |

Extend retention only for an active incident, audit, or legal hold.

## Redaction Controls

Before sharing or uploading artifacts, scan for common secret patterns:

```bash
rg -n "OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|token|secret|password|Bearer " .sidecar/runs
```

If the scan finds sensitive content:

- Replace the value with `[redacted]` in the shared copy.
- Keep the original only in the approved restricted evidence store.
- Re-run the scan on the redacted copy.
- Do not paste raw trace content into pull requests, tickets, or chat.

## Deletion Procedure

Preview before deleting:

```bash
find .sidecar/runs -type f -name "trace-input.jsonl" -mtime +14 -print
find .sidecar/runs -type f \( -name "events.jsonl" -o -name "checkpoint*" \) -mtime +30 -print
```

Delete only after the preview is reviewed:

```bash
find .sidecar/runs -type f -name "trace-input.jsonl" -mtime +14 -delete
find .sidecar/runs -type f \( -name "events.jsonl" -o -name "checkpoint*" \) -mtime +30 -delete
```

Never delete `audit.json`, `candidate.diff`, `eval-report.json`, `optimization-summary.json`, `decision.json`, or `report.md` while a proposal, release, rollback, or incident review is open.

## Audit Evidence

For each cleanup, record:

- Date and operator.
- Retention rule applied.
- Dry-run output.
- Deletion command output.
- Redaction scan result, including whether `OPENAI_API_KEY` or any other credential pattern was found.
