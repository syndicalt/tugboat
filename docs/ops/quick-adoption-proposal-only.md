# Quick Adoption for Proposal-Only Mode

## Purpose

Use this guide to adopt Tugboat in an existing repository in under 15 minutes without credentials. The goal is a local proposal loop that creates review artifacts but does not mutate instruction files.

## Assumptions

- Python 3.11 or newer is available.
- Tugboat is installed from the source tree or package.
- The repository has at least one instruction or harness document, such as `AGENTS.md`, `CODEX.md`, `CLAUDE.md`, `SKILL.md`, or `docs/runbook.md`.
- The default policy stays `proposal_only`.
- No live provider credentials are configured or required.

## Fifteen-Minute Adoption

From the target repository root:

```bash
tugboat doctor
tugboat index --repo .
tugboat harness check --repo .
```

If `tugboat doctor` does not report `proposal_only` and disabled auto-apply, stop and review policy before continuing.

## No-Credentials Proposal Loop

Create a minimal trace fixture:

```bash
mkdir -p traces
printf '{"event":"user_correction","message":"The runbook missed the rollback command."}\n' > traces/example.jsonl
```

Run the proposal loop without credentials:

```bash
tugboat audit --repo . --trace traces/example.jsonl --mock-llmff-inspect
tugboat propose --repo . --audit latest
tugboat report --repo . --run latest
```

Review the generated `.sidecar/runs/<run-id>/candidate.diff` before applying anything manually. Proposal-only adoption is successful when the team can inspect the report and reject or copy changes by hand.

## Stop Criteria

Stop adoption and open a review if:

- `tugboat doctor` does not report `proposal_only`.
- Tugboat asks for provider credentials.
- A candidate edits secrets, approvals, sandboxing, network, deployment, memory, provider routing, or policy authority.
- `.sidecar/runs` contains unredacted credentials or private trace data.
- `tugboat harness check --repo .` reports missing local knowledge-map references.
