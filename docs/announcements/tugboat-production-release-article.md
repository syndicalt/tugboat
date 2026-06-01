---
owner: platform
verification_status: verified
---

# Tugboat: production harness/config observability for coding agents

Repo: https://github.com/syndicalt/tugboat

Tugboat is a local-first sidecar for the operational layer around coding agents: the instructions, skills, policies, runbooks, eval definitions, and harness configs that shape what agents are allowed to do and how their work is reviewed.

The short version:

```text
trace evidence -> audit -> bounded proposal -> eval -> review -> governed apply
```

The default posture is still proposal-only. Tugboat does not silently rewrite repo instructions. It watches the harness/config layer, produces evidence-backed review artifacts, validates candidate edits, and records enough provenance for an operator to understand what changed, why it was proposed, and how to roll it back.

## Why This Exists

Modern coding agents do not operate only through model weights. They operate through a harness: repo-local instructions, policy files, tool rules, eval definitions, CI checks, memory guidance, and review workflows.

When that layer is stale or ambiguous, agent behavior drifts. A user corrects the agent in one session, the task may get fixed, and the learning often disappears into the transcript. The durable operating knowledge remains unchanged, so the next run can repeat the same failure.

Tugboat treats that operational layer as production state.

It was shaped by two related ideas:

- OpenAI's harness engineering framing: better agents require better environments, feedback loops, evals, observability, and mechanical checks around them.
- SkillOpt-style external-state optimization: natural-language skills and instructions can be improved from execution evidence without pretending every improvement belongs in the model.

Tugboat brings those ideas into a repo-local workflow for software teams. The goal is not generic prompt fiddling. The goal is governed maintenance of the files agents actually rely on.

## What Tugboat Monitors

Tugboat indexes and reasons over repo-local operating files, including:

- `CODEX.md`
- `AGENTS.md`
- `CLAUDE.md`
- `SKILL.md`
- `.codex/skills/**/SKILL.md`
- runbooks
- eval definitions
- policy docs
- local harness and CI docs

Those files are treated as an instruction graph with precedence, protected regions, content hashes, and review boundaries. Candidate edits must stay bounded to allowed files and sections.

## The Core Workflow

Initialize a repo:

```bash
tugboat init --repo .
tugboat index --repo .
tugboat status --repo .
```

Run the proposal loop:

```bash
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

Or run the pipeline in explicit stages:

```bash
tugboat audit --repo . --trace traces/example.jsonl --trace-format auto
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
tugboat report --repo . --run latest
tugboat inspect-decision --repo . --decision latest
```

Review the generated packet under `.sidecar/runs/<run-id>/`:

- `candidate.diff`
- `candidate.json`
- `candidate.raw.json`
- `proposal-rationale.raw.json`
- `eval-report.json`
- `policy-gate.json`
- `optimization-summary.json`
- `decision-trace.json`
- `report.md`

That packet is the center of the product. It is where trace evidence, proposal rationale, eval results, policy decisions, rejected-edit memory, and review metadata meet.

## llmff As The Pipeline Boundary

Tugboat keeps `llmff` as the bounded orchestration layer for inspect, run, trace, event, checkpoint, and typed-output handling.

That matters because optimization work needs reproducible boundaries. Provider-backed runs must be visible in policy. Inspect artifacts must declare external calls. Eval artifacts must validate against schema and include held-out provenance before a candidate can be accepted.

The sidecar owns governance and storage. `llmff` owns the execution pipeline boundary.

## Proposal-First By Default

The release posture is intentionally conservative where authority matters:

- `mode: proposal_only`
- `auto_apply.enabled: false`
- provider-backed `llmff` runs require explicit policy
- MCP write-intent tools require explicit allow entries
- the read-only kill switch blocks direct write paths
- protected policy, approvals, sandboxing, network, provider routing, secrets, deployment, and memory-behavior changes require human review or are rejected

This is the difference between "the agent changed my rules" and "the system produced a reviewable candidate with evidence."

## Apply And Rollback

When a candidate is ready, Tugboat supports reviewed mutation paths:

```bash
tugboat apply --repo . --candidate latest --mode proposal
tugboat apply --repo . --candidate latest --mode branch --human-review --review-actor <name>
tugboat apply --repo . --candidate latest --mode commit --human-review --review-actor <name>
tugboat apply --repo . --candidate latest --mode pr --human-review --review-actor <name>
```

Rollback is part of the contract:

```bash
tugboat rollback --repo . --decision latest
tugboat rollback --repo . --decision latest --execute
```

Apply and rollback flows are VCS-backed. They check base hashes, dirty targets, policy gate state, eval evidence, candidate provenance, and rollback metadata before recording durable decisions.

## Auto-Apply Without Recklessness

Tugboat includes auto-apply, but it is deliberately narrow.

Auto-apply is not a blanket permission for an agent to mutate instructions. It is a policy-governed commit lane for low-risk Class A changes after the system has earned evidence.

Runtime confirmation is explicit:

```bash
tugboat auto-apply --repo . --candidate latest --actor <name>
tugboat auto-apply --repo . --candidate latest --actor <name> \
  --confirm-auto-apply \
  --auto-apply-policy-version 1
```

Thresholds are not CLI overrides. They live in policy and are checked against ledger-derived metrics:

```yaml
auto_apply:
  enabled: false
  max_changed_lines: 50
  lanes:
    docs_hygiene:
      max_changed_lines: 50
      minimum_burn_in_days: 3
      maximum_rejection_rate: 0.20
      maximum_rollback_rate: 0.05
    skill_improvement:
      max_changed_lines: 30
      minimum_burn_in_days: 7
      maximum_rejection_rate: 0.15
      maximum_rollback_rate: 0.03
```

Even when enabled, auto-apply requires:

- explicit repo policy
- matching policy-version confirmation
- allowed repository
- allowed risk class
- allowed change category
- held-out eval pass
- governance pass
- VCS-backed commit
- one-command rollback
- acceptable ledger-derived burn-in, rejection rate, and rollback rate

Class B is not enabled by default. Policy, authority, provider, network, sandbox, deployment, secrets, approval, and memory-behavior changes remain outside the default auto-apply lanes.

## MCP And Daemon Operations

Tugboat exposes MCP and daemon workflows without changing the authority model.

Read-only MCP:

```bash
tugboat mcp stdio --repo . --read-only
```

Daemon status and bounded local processing:

```bash
tugboat daemon status --repo .
tugboat daemon run-once --repo .
tugboat daemon cycle --repo . --trace-dir traces --max-jobs 1
```

The daemon is local-only, queue-backed, audited, and kill-switchable:

```bash
tugboat daemon read-only --repo . --enable
```

It does not grant extra authority. It uses the same service layer, policy gates, sidecar storage, VCS checks, and read-only kill switch as the CLI.

## Release Evidence

The production release surface is documented and tested.

Important docs:

- `docs/quickstart.md`
- `docs/cli-reference.md`
- `docs/apply-rollback.md`
- `docs/auto-apply.md`
- `docs/daemon-guide.md`
- `docs/troubleshooting.md`
- `docs/ops/release-checklist.md`

Release verification includes:

```bash
tugboat doctor
tugboat index --repo . --check
tugboat harness check --repo .
tugboat ci --repo .
python -m pytest --cov=src --cov-report=term-missing -q
python -m build --wheel
python -m twine check dist/<wheel>.whl
```

Release manifests are generated from retained evidence:

```bash
tugboat ops release-manifest --repo . \
  --wheel dist/<wheel>.whl \
  --commit "$(git rev-parse HEAD)" \
  --ci-url <url> \
  --approver <name> \
  --security-review-decision approved_proposal_only \
  --security-review-critical-high-findings 0 \
  --evidence .sidecar/ci/doctor.txt \
  --evidence .sidecar/ci/index-check.txt \
  --evidence .sidecar/ci/harness.txt \
  --evidence .sidecar/ci/ci-report.json \
  --evidence .sidecar/ci/pytest-coverage.log \
  --evidence .sidecar/ci/build-wheel.txt \
  --evidence .sidecar/ci/twine-check.txt \
  --evidence .sidecar/ci/install-smoke.txt
```

The release gate requires full-suite coverage at or above 90%, harness health, docs contracts, retained CI evidence, and exact-HEAD release manifest generation.

## What This Enables

Tugboat gives teams a way to treat agent-operating knowledge as governed production state:

- trace evidence becomes reviewable maintenance input
- recurring failures can become bounded instruction proposals
- proposal quality is checked against held-out evidence
- unsafe or repeated bad directions are remembered and suppressed
- config and instruction drift becomes observable
- review, apply, and rollback become explicit workflows

The product is intentionally a sidecar. It does not replace Codex, Claude Code, `llmff`, CI, or human review. It gives those systems a durable maintenance loop around the files that shape agent behavior.

## The Bet

Agent traces should not just explain what went wrong.

They should help maintain the operating layer that prevents the same failure next time.

That operating layer is no longer just documentation. For agentic software development, it is part of the production harness.

Tugboat is built for that layer.

```text
observe -> propose -> validate -> review -> govern
```

No silent mutation. No generic prompt fiddling. A production-shaped loop for keeping agent instructions, skills, harness config, and policy honest.

## Short X Post

Tugboat is a local-first sidecar for production harness/config observability around coding agents.

Repo: https://github.com/syndicalt/tugboat

It turns agent trace evidence into governed proposals for repo-local operating files like `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, eval docs, and policy config.

```text
trace -> audit -> proposal -> eval -> review -> governed apply
```

Default posture: proposal-only.

Auto-apply exists, but narrowly: policy opt-in, Class A lanes only, ledger-derived burn-in/reliability, held-out eval, governance pass, VCS commit, one-command rollback.

The goal is not prompt fiddling. It is production maintenance for the instruction/config layer agents rely on.
