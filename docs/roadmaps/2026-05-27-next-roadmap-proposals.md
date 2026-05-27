---
owner: platform
verification_status: verified
---

# Tugboat Next Roadmap Proposals

## Purpose

This document collects candidate tracks for the next Tugboat roadmap after the production release candidate. The baseline product is now proposal-first, local-first, governed, documented, and tested. The next roadmap should focus on differentiation, real-world reliability, and a robust auto-update capability that preserves Tugboat's authority boundaries.

## Selection Criteria

Prioritize roadmap items that:

- deepen Tugboat's wedge as production harness/config observability for coding agents;
- improve evidence quality from real traces;
- make review and rollback easier for operators;
- improve `llmff` reproducibility and provider-backed readiness;
- preserve proposal-first defaults;
- keep auto-update governed, policy-owned, auditable, and reversible.

Avoid roadmap items that turn Tugboat into generic agent analytics, a remote dashboard-first product, or an unconstrained self-modifying prompt system.

## Proposal 1: Robust Auto-Update

Goal: build a reliable auto-update system for low-risk harness/config maintenance without weakening the default proposal-first posture.

This is broader than the current narrow auto-apply lanes. The release baseline has `docs_hygiene` and `skill_improvement` lanes, but auto-update should mean a complete controlled-update lifecycle:

```text
eligible candidate -> preflight -> staged update -> validation -> commit/PR -> monitor -> rollback
```

Required capabilities:

- policy-owned eligibility thresholds:
  - `enabled`;
  - allowed repositories;
  - allowed files and headings;
  - lane-specific limits for `docs_hygiene`, `skill_improvement`, and future reviewed lanes;
  - allowed risk classes;
  - allowed change categories;
  - maximum changed lines;
  - minimum burn-in days;
  - maximum rejection rate;
  - maximum rollback rate;
  - required eval suites.
- ledger-derived readiness:
  - burn-in age;
  - reviewed count;
  - rejection rate;
  - applied count;
  - rollback rate;
  - recent governance failures;
  - candidate category history.
- staged execution:
  - dry-run preflight;
  - isolated branch or worktree;
  - deterministic artifact validation;
  - held-out eval;
  - governance eval;
  - VCS commit or draft PR;
  - post-apply smoke checks.
- monitoring window:
  - configurable burn-in after each auto-update;
  - incident watch;
  - rollback watch;
  - regression evidence capture;
  - decision trace update.
- rollback automation:
  - one-command rollback is mandatory;
  - rollback preflight validates the original apply plan;
  - rollback execution records post-rollback hashes and eval state;
  - failed rollback becomes an incident artifact.
- operator controls:
  - global read-only kill switch;
  - per-repo auto-update pause;
  - per-category pause;
  - policy version confirmation;
  - audit feed of every accepted, rejected, staged, applied, and rolled-back update.

Non-negotiables:

- auto-update defaults off;
- Class A only by default;
- no policy, approval, sandbox, secrets, network, deployment, provider-routing, sidecar-authority, or memory-behavior updates;
- no runtime CLI override for safety thresholds;
- no direct mutation outside the VCS adapter;
- no update without held-out validation and governance pass;
- no update without rollback metadata.

Exit criteria:

- `tugboat auto-apply` remains narrow and policy-confirmed.
- A new auto-update preflight/report path shows why a candidate is or is not eligible.
- Tests prove stale base, dirty target, failed eval, failed governance, bad rollback metadata, and read-only kill switch all block update.
- Tests prove successful auto-update can be rolled back from the recorded decision.
- Docs explain dry-run, execution, monitoring, pause, emergency stop, and rollback.

## Proposal 2: Real-World Trace Adapter Hardening

Goal: improve evidence quality from real coding-agent sessions.

Candidate work:

- expand Codex export parsing coverage;
- harden Claude transcript import;
- improve MCP-session trace capture;
- normalize CI failure traces;
- add trace quality scoring;
- add redaction previews before provider-backed analysis;
- attach active instruction snapshots to every canonical episode;
- detect forged success, poisoned command output, prompt injection, and missing test evidence.

Exit criteria:

- at least five real trace fixtures map into one canonical schema;
- every trace adapter has adversarial fixtures;
- audit reports cite evidence IDs and instruction refs precisely;
- redaction failures block provider-backed runs.

## Proposal 3: Provider-Backed llmff Manifest Expansion

Goal: move beyond fixture-backed default flows while preserving local reproducibility and explicit policy.

Candidate work:

- provider-backed manifests for audit, propose, eval, and acceptance summary;
- manifest package versioning or bundled manifest pinning decision;
- provider smoke suites;
- cost and latency accounting;
- external-call declaration checks;
- manifest hash review workflow;
- reproducible local fallback fixtures.

Exit criteria:

- provider-backed execution requires `llmff.allow_network: true`;
- every provider call is declared by inspect/preflight output;
- every manifest hash is pinned or reviewed;
- fixture-backed tests remain default and credential-free.

## Proposal 4: Operator Review UX

Goal: make review packets easier to use without reducing rigor.

Candidate work:

- richer `tugboat inspect-decision` summaries;
- candidate comparison view;
- risk-class explanation;
- evidence-ref drilldown;
- rollback readiness summary;
- PR body generation;
- review checklist generation;
- rejection reason templates.

Exit criteria:

- an operator can decide accept/reject/rollback from one report plus linked artifacts;
- PR mode emits a complete review body;
- rejected decisions carry structured reasons for future optimizer suppression.

## Proposal 5: Longitudinal Metrics And Local Dashboard

Goal: expose trend data that tells operators whether Tugboat is improving the harness or creating churn.

Candidate work:

- acceptance rate;
- rejection rate;
- rollback rate;
- recurring incident rate;
- eval score trends;
- governance regression trends;
- mean changed lines;
- corpus growth;
- duplicate/conflicting rule count;
- stale-doc count;
- daemon queue health.

Exit criteria:

- `tugboat ops observability --repo .` includes longitudinal trend summaries;
- `tugboat status --repo .` highlights drift and blocked work;
- docs explain which metrics should pause auto-update.

## Proposal 6: Team Workflow And PR Integration

Goal: make Tugboat fit normal engineering review workflows.

Candidate work:

- draft PR creation hardening;
- branch naming and cleanup policies;
- reviewer assignment metadata;
- CODEOWNERS-style policy ownership;
- CI artifact links in generated PRs;
- release-note snippets for accepted harness changes;
- local-only fallback when GitHub CLI is unavailable.

Exit criteria:

- PR mode can generate a reviewable branch and draft PR body from a candidate;
- ownership metadata is visible in review packets;
- PR failure modes are fail-closed and audited.

## Proposal 7: Harness Health And Cleanup Agents

Goal: keep instruction/config files map-like, current, and mechanically legible.

Candidate work:

- duplicate/conflicting rule detection;
- stale file-reference detection against changed source files;
- giant instruction-file warnings;
- "too many MUSTs" signal;
- orphaned runbook detection;
- recurring cleanup candidates;
- cleanup eval suites.

Exit criteria:

- cleanup candidates are proposal-only by default;
- cleanup proposals include source evidence and structural evals;
- auto-update can only touch cleanup candidates that satisfy the robust auto-update lane.

## Proposal 8: Zaxy Memory Bridge

Goal: optionally export high-signal optimizer summaries to Zaxy without making Zaxy part of Tugboat's authority path.

Candidate work:

- export accepted/rejected edit summaries;
- export recurring failure patterns;
- export operator review outcomes;
- import advisory memory as proposal context only;
- record provenance for every imported advisory.

Exit criteria:

- Tugboat remains locally functional without Zaxy;
- Zaxy context is advisory, never authoritative;
- deterministic gates still decide candidate eligibility.

## Recommended Next Roadmap Shape

The next roadmap should make robust auto-update the main spine, with supporting tracks around evidence quality and operator control:

1. Robust Auto-Update
2. Real-World Trace Adapter Hardening
3. Provider-Backed `llmff` Manifest Expansion
4. Operator Review UX
5. Longitudinal Metrics And Local Dashboard
6. Team Workflow And PR Integration
7. Harness Health And Cleanup Agents
8. Optional Zaxy Memory Bridge

The release theme should be:

```text
from governed proposals to governed maintenance
```

The product should still default to proposal-only. The next step is to make narrow, evidence-backed auto-update strong enough that teams can safely enable it for the lowest-risk maintenance lane.
