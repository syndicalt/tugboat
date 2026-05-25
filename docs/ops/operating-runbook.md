# Tugboat Operating Runbook

## Purpose

This runbook covers routine operation, failure triage, and escalation for teams running Tugboat in proposal-only mode. It assumes instruction edits are reviewed by humans before application.

## Daily Checks

Run from the repository root:

```bash
tugboat status --repo .
tugboat harness report --repo .
```

Check:

- Mode is `proposal_only`.
- Auto-apply is disabled unless explicitly approved for this repository.
- Pending candidates have owners.
- The latest run status is expected.
- Harness findings are either resolved or tracked.

## Incident Response

When a run fails or produces an unsafe candidate:

```bash
tugboat status --repo .
tugboat report --repo . --run <run-id>
```

Classify the incident:

- `failure_kind` from `llmff` or Tugboat run metadata.
- Affected repository, branch, and commit.
- Whether secrets, credentials, private traces, or policy files were exposed.
- Whether the candidate changed protected topics such as approvals, sandboxing, network, deployment, memory, provider routing, or secrets.

Pause further proposal runs if the same failure repeats or if artifacts may contain unredacted sensitive data.

## Rollback

For proposal-only use, rollback usually means rejecting the candidate and preserving evidence:

```bash
tugboat report --repo . --run <run-id>
git diff
```

If a branch or commit mode was used, follow the release checklist rollback instructions and keep `.sidecar` evidence intact until review closes.

## Escalation

Escalate to the repository owner and security reviewer when:

- A secret appears in `.sidecar/runs`, CI artifacts, tickets, or chat.
- A candidate changes policy or authority boundaries.
- A restored `.sidecar` database fails `PRAGMA integrity_check`.
- The same `failure_kind` appears in three consecutive production runs.
- CI cannot run `tugboat doctor`, `tugboat harness check --repo .`, or `python -m pytest -q`.
