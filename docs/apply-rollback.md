---
owner: platform
verification_status: verified
---

# Review, Apply, And Rollback

## Review Packet

Before applying anything, inspect the run artifacts:

```bash
tugboat report --repo . --run latest
tugboat inspect-decision --repo . --decision latest
tugboat inspect-decision --repo . --decision latest --compare <decision-id>
```

`report` includes metadata-only `risk_explanation`, `rollback_readiness`, and `highest_impact_summary` lines so reviewers can quickly see policy risk, rollback state, the most important bounded edit, score delta, token delta, and governance status before opening the diff.

`inspect-decision` prints the decision trace path plus a concise metadata summary: run id, decision, candidate id, candidate file, candidate state, risk explanation, eval pass/fail summary, strict rollback readiness, broader rollback readiness, highest-impact metadata, and the next artifact to inspect. With `--compare`, it adds a bounded comparison against another decision id or run ref. It does not print raw trace payload snippets, rationale text, optimizer memory, or rollback bodies.

Review these files under `.sidecar/runs/<run-id>/`:

- `candidate.diff`
- `candidate.json`
- `candidate.raw.json`
- `proposal-rationale.raw.json`
- `eval-report.json`
- `policy-gate.json`
- `optimization-summary.json`
- `decision-trace.json`

Do not apply if the candidate changes approvals, sandboxing, network, deployment, secrets, provider routing, memory behavior, or policy authority without explicit human review.

## Proposal Mode

Use proposal mode to produce an apply plan without changing instruction files:

```bash
tugboat apply --repo . --candidate latest --mode proposal --review-actor <name>
```

This writes `apply-plan.json` and records provenance without creating a branch or commit.

## Branch And Commit Modes

Use branch mode when a human will inspect the working tree:

```bash
tugboat apply --repo . --candidate latest --mode branch --human-review --review-actor <name>
```

Use commit mode when the reviewed patch should become a local VCS commit:

```bash
tugboat apply --repo . --candidate latest --mode commit --human-review --review-actor <name>
```

Both modes require:

- clean worktree checks;
- target-file dirty checks;
- base-hash checks;
- deterministic policy gate pass;
- eval evidence with held-out improvement;
- governance pass;
- candidate provenance;
- secret-scanned artifacts.

## Pull Request Mode

Enable PR mode in policy before use:

```yaml
vcs:
  pull_request:
    enabled: true
    provider: github_cli
    remote: origin
    base_branch: main
    draft: true
```

Then run:

```bash
tugboat apply --repo . --candidate latest --mode pr --human-review --review-actor <name>
```

PR mode is fail-closed. The local flow is: create generated branch, apply diff, commit, push remote branch, create PR, publish `apply-plan.json` and `provenance-bundle.json`, then record ledger events. If branch, diff, or commit fails before remote work, Tugboat cleans up local generated state. If push, PR creation, or artifact publication fails after a local commit, Tugboat writes `.sidecar/runs/<run-id>/apply-incident.json`, records `apply.failed`, returns the local repo to the original branch where possible, and does not delete remote branches or close PRs automatically. Operators should preserve the incident artifact, inspect any pushed branch or created PR, and clean up remote state only after evidence is retained.
Generated PR bodies contain structured review metadata, validation status, rollback readiness, and artifact references. They do not include raw trace snippets or candidate rationale text.

## Rollback Plan

Generate a rollback plan without reverting:

```bash
tugboat rollback --repo . --decision latest
```

Execute the rollback after review:

```bash
tugboat rollback --repo . --decision latest --execute
```

Rollback execution creates a revert commit, writes `rollback-plan.json`, records `rollback.applied`, stores post-rollback hashes, and updates the decision trace. If Git revert fails during execution, Tugboat writes `.sidecar/runs/<run-id>/rollback-incident.json` with `rollback_applied: false` and records `rollback.failed` without writing a success rollback plan. If the revert commit succeeds but `rollback-plan.json` cannot be published, Tugboat writes `rollback-incident.json` with `rollback_applied: true`, `rollback_plan_written: false`, the `revert_commit`, attempted `rollback_plan`, post-rollback hashes, and restored-pre-hash status; it records `rollback.applied` followed by `rollback.failed` so metrics see the real rollback while incident handling stays fail-closed. Repos with `auto_apply.pause_for_incident: true` treat either failed rollback incident as active evidence and pause auto-apply until a later successful rollback event supersedes it.

## Safety Stops

Stop and investigate when Tugboat prints:

- `apply blocked: read-only kill switch is enabled`
- `apply blocked: dirty target`
- `apply blocked: base hash`
- `apply blocked: policy gate rejected candidate`
- `apply blocked: Class C candidates require explicit human review`
- `rollback blocked: read-only kill switch is enabled`
- `rollback blocked: apply plan`
- `rollback blocked: git revert`

Preserve `.sidecar/runs/<run-id>/` until the review closes. If `rollback-incident.json` exists with `rollback_applied: false`, resolve or abort the Git revert state before retrying. If it has `rollback_applied: true` and `rollback_plan_written: false`, preserve the incident artifact and reconstruct or repair the missing rollback plan evidence before closing the incident.
