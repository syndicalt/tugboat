---
owner: platform
verification_status: verified
---

# Auto-Apply Guide

## Default Posture

Auto-apply defaults off. Proposal-only mode is the release posture:

```bash
tugboat doctor
tugboat status --repo .
```

Do not enable auto-apply for policy, authority, provider routing, secrets, deployment, network, sandboxing, memory behavior, or approval changes.

## Eligibility

Auto-apply is only for narrow Class A changes in an enabled policy lane after a burn-in period. A candidate must have:

- explicit repo policy enabling auto-apply;
- matching policy version confirmation;
- at least the configured burn-in days;
- low rejection rate;
- low rollback rate;
- held-out eval pass;
- governance pass;
- evaluated instruction token delta within policy limits;
- VCS-backed commit path;
- one-command rollback;
- allowed change category;
- no protected policy-domain escalation.

Default lane thresholds are intentionally usable but still bounded:

- `docs_hygiene`: 3 burn-in days, 20% maximum rejection rate, 5% maximum rollback rate, 50 changed lines, and 50 added instruction tokens.
- `skill_improvement`: 7 burn-in days, 15% maximum rejection rate, 3% maximum rollback rate, 30 changed lines, and 30 added instruction tokens.

Repos can tighten these values in `.sidecar/policy.yaml`; runtime auto-apply commands cannot override them.
The global `auto_apply.max_instruction_token_delta` is an absolute cap, and each lane can set an equal or stricter `max_instruction_token_delta`. Auto-apply fails closed with `instruction_token_delta_missing` if the eval artifact does not include `metrics.instruction_token_delta`, and with `max_instruction_token_delta_exceeded` if the evaluated candidate grows the instruction corpus beyond policy.

Allowed examples include typo fixes, broken internal links, formatting normalization, duplicate sentence removal, and verified stale command references.

## Pause Controls

Pause controls are policy-owned. Do not use runtime flags to weaken or bypass them:

```yaml
auto_apply:
  paused_repositories:
    - /absolute/path/to/repo
  paused_lanes:
    - docs_hygiene
  paused_categories:
    - typo_fix
  pause_for_incident: true
```

Paused repositories, lanes, categories, and incident pause state block otherwise eligible candidates with explicit reasons in preflight reports and auto-apply decision events. The operations observability report treats explicitly paused lanes like disabled lanes when reporting staged-but-unapplied candidates.

`pause_for_incident: true` is an evidence-driven watch, not a permanent pause by itself. It blocks auto-apply when Tugboat finds active incident evidence such as a `rollback.failed` audit event for an unresolved failed rollback. Preflight, shadow, and final auto-apply reports include `incident_active` and `active_incidents` under `checks.auto_apply`; missing or invalid referenced incident artifacts remain visible and fail closed until a later `rollback.applied` event for the same candidate supersedes the failed rollback.

## Dry Check

Run without confirmation first:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name>
```

The expected safe result for an unconfirmed or ineligible candidate is `auto-apply blocked:` with reasons in the candidate run artifacts.

For a no-mutation eligibility report, use preflight:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name> --preflight
```

Preflight writes `.sidecar/runs/<run-id>/auto-apply-preflight.json` and prints its path. It exits `0` when the report is produced, whether the candidate is eligible or ineligible. It does not apply patches, create branches, commit, write `apply-plan.json`, write `auto-apply-approval.json`, or append auto-apply decision events. The report includes policy, stored gate, eval, VCS, lane, readiness, and reasons so operators can see exactly why a candidate would or would not apply.

Passing `--confirm-auto-apply --auto-apply-policy-version <version>` to preflight lets the report model confirmed execution and include a pending approval bundle for an otherwise eligible candidate. It still does not mutate the repository. The read-only kill switch blocks preflight because preflight writes an artifact.

## Shadow Mode

Use shadow mode for audited no-mutation canary telemetry:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name> \
  --shadow \
  --confirm-auto-apply \
  --auto-apply-policy-version 1
```

Shadow mode evaluates the same auto-apply gates, writes `.sidecar/runs/<run-id>/auto-apply-shadow.json`, and appends a small `auto_apply.shadowed` audit event with candidate id, run id, actor, lane, eligibility, reasons, and report path. It does not apply patches, create branches, commit, write `apply-plan.json`, write `auto-apply-approval.json`, or append `auto_apply.decided`. Operations observability counts shadowed candidates separately from eligible, staged, applied, and rolled-back candidates. The read-only kill switch blocks shadow mode because it writes an artifact and audit event.

## Confirmed Execution

Only after review:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name> \
  --confirm-auto-apply \
  --auto-apply-policy-version 1
```

The command writes an audited commit-mode apply plan and records rollback metadata. Burn-in, rejection rate, and rollback rate are computed from the ledger and checked against policy.

## Rollback

Every successful auto-apply must record a one-command rollback:

```bash
tugboat rollback --repo . --decision latest --execute
```

Review `rollback-plan.json` and `decision-trace.json` after execution. If rollback execution fails before a revert commit is recorded, Tugboat writes `rollback-incident.json` and records `rollback.failed` so the failed recovery is auditable.

## Monitoring

Use the operations summary to inspect lane-level auto-apply activity:

```bash
tugboat ops observability --repo .
```

The report is written to `.sidecar/ops/observability/summary.json`. Its `auto_apply_lanes` section counts shadowed, eligible, rejected, staged, applied, rolled-back, and paused candidates by lane. Counts are derived from append-only audit events such as `auto_apply.shadowed`, `auto_apply.decided`, `auto_apply.applied`, and `rollback.applied`; successful precheck and final decisions for the same candidate are deduplicated. Candidates blocked by repository, lane, category, or active incident pause controls are counted as paused rather than rejected so operational pauses do not inflate policy rejection rates.

## Emergency Stop

Enable the read-only kill switch to block auto-apply and other direct write paths:

```bash
tugboat daemon read-only --repo . --enable
```

Do not disable it until candidate state, rollback state, and retained artifacts have been reviewed.
