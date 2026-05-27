---
owner: platform
verification_status: verified
---

# Artifact Retention and Redaction Controls

## Purpose

Tugboat artifacts make proposals reproducible, but traces and command output can contain private data. Retention must keep decision evidence while reducing long-lived exposure in `.sidecar/runs`.

## Retention Classes

Default retention targets:

| Class | Examples | Retention |
| --- | --- | --- |
| Raw trace inputs | `trace-input.jsonl`, transcript exports | 14 days |
| Runtime lifecycle streams | events, checkpoints | 7 days |
| Review artifacts | `audit.json`, `candidate.diff`, `eval-report.json`, `optimization-summary.json`, `report.md` | 180 days |
| Decisions | accepted, rejected, rollback, release evidence | 1 year |

Extend retention only for an active incident, audit, or legal hold.

## Redaction Controls

Before sharing or uploading artifacts, scan for common secret patterns:

```bash
rg -n "OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|token|secret|password|Bearer " .sidecar/runs
```

Tugboat also scans retained run artifacts during retention previews and can write a redacted export without mutating the originals:

```bash
tugboat retention --repo . --redact-output /tmp/tugboat-redacted-export
```

The export preserves `.sidecar/runs/...` relative paths under the output directory, replaces common credential values with `[REDACTED:<kind>]`, and writes owner-only files. Redaction export is blocked while `.sidecar/read-only.kill` exists and the output directory must be outside `.sidecar`.

If the scan finds sensitive content:

- Replace the value with `[redacted]` in the shared copy.
- Keep the original only in the approved restricted evidence store.
- Re-run the scan on the redacted copy.
- Do not paste raw trace content into pull requests, tickets, or chat.

## Deletion Procedure

Preview with Tugboat before deleting:

```bash
tugboat retention --repo .
```

The command writes `.sidecar/ops/retention/retention-report.json`. A dry run records `status: complete`, the candidate paths, and an empty deleted list.

Delete only after the dry-run report is reviewed:

```bash
tugboat retention --repo . --apply
```

Apply mode writes a preflight report with `status: planned` before deleting anything, then atomically replaces it with `status: complete` after deletion succeeds. If the final report write fails, treat the remaining `status: planned` report as an incomplete cleanup record that needs operator review.

Use raw `find .sidecar/runs` cleanup only for manual incident recovery, and attach the command output to the review record.

Never delete `audit.json`, `candidate.diff`, `eval-report.json`, `optimization-summary.json`, `decision.json`, or `report.md` while a proposal, release, rollback, or incident review is open.

## Audit Evidence

For each cleanup, record:

- Date and operator.
- Retention rule applied.
- Dry-run output and `.sidecar/ops/retention/retention-report.json`.
- Apply output, when deletion was approved.
- Redaction scan result, including whether `OPENAI_API_KEY` or any other credential pattern was found.
