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

Review `rollback-plan.json` and `decision-trace.json` after execution.

## Monitoring

Use the operations summary to inspect lane-level auto-apply activity:

```bash
tugboat ops observability --repo .
```

The report is written to `.sidecar/ops/observability/summary.json`. Its `auto_apply_lanes` section counts eligible, rejected, staged, applied, rolled-back, and paused candidates by lane. Counts are derived from append-only audit events such as `auto_apply.decided`, `auto_apply.applied`, and `rollback.applied`; successful precheck and final decisions for the same candidate are deduplicated.

## Emergency Stop

Enable the read-only kill switch to block auto-apply and other direct write paths:

```bash
tugboat daemon read-only --repo . --enable
```

Do not disable it until candidate state, rollback state, and retained artifacts have been reviewed.
