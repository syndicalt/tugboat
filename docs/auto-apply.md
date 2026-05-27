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

Auto-apply is only for narrow Class A changes after a burn-in period. A candidate must have:

- explicit repo policy enabling auto-apply;
- matching policy version confirmation;
- at least the configured burn-in days;
- low rejection rate;
- low rollback rate;
- held-out eval pass;
- governance pass;
- VCS-backed commit path;
- one-command rollback;
- allowed change category;
- no protected policy-domain escalation.

Default policy thresholds are intentionally usable but still bounded: 14 burn-in days, 10% maximum rejection rate, 2% maximum rollback rate, and 30 changed lines. Repos can tighten these values in `.sidecar/policy.yaml`; runtime auto-apply commands cannot override them.

Allowed examples include typo fixes, broken internal links, formatting normalization, duplicate sentence removal, and verified stale command references.

## Dry Check

Run without confirmation first:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name>
```

The expected safe result for an unconfirmed or ineligible candidate is `auto-apply blocked:` with reasons in the candidate run artifacts.

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

## Emergency Stop

Enable the read-only kill switch to block auto-apply and other direct write paths:

```bash
tugboat daemon read-only --repo . --enable
```

Do not disable it until candidate state, rollback state, and retained artifacts have been reviewed.
