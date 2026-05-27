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
```

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

PR mode is fail-closed. If branch, commit, push, or provider execution fails, Tugboat cleans up generated state where possible and records the failure.

## Rollback Plan

Generate a rollback plan without reverting:

```bash
tugboat rollback --repo . --decision latest
```

Execute the rollback after review:

```bash
tugboat rollback --repo . --decision latest --execute
```

Rollback execution creates a revert commit, writes `rollback-plan.json`, records `rollback.applied`, stores post-rollback hashes, and updates the decision trace.

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

Preserve `.sidecar/runs/<run-id>/` until the review closes.
