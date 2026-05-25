# SPEC: Agent Instruction Sidecar

## 1. Summary

Agent Instruction Sidecar is a local-first sidecar that observes an agent's work, evaluates behavior against instruction/configuration Markdown files, and improves those files through an evidence-gated optimization loop.

The system targets files such as:

- `SKILL.md`
- `CODEX.md`
- `CLAUDE.md`
- `AGENTS.md`
- repo-specific policy docs
- eval definitions and operating runbooks

The sidecar uses `llmff` as the bounded inference and evaluation pipeline substrate. The sidecar owns observation, policy, instruction precedence, storage, patch authority, approval, rollback, and audit state. `llmff` owns reproducible pipeline execution, typed inputs/outputs, validation, traces, events, checkpoints, and backend abstraction.

The design is inspired by Microsoft's SkillOpt work in "Skill Evolution Enables Self-Improving Agents", where natural-language skills are treated as external, optimizable state and changed through bounded edit operators plus validation. This project adapts that idea to production agent configuration files with stronger governance, provenance, and rollback.

## 2. Problem

Modern coding agents rely on natural-language configuration files as operational policy. These files define:

- how the agent should plan, test, edit, verify, and report;
- which tools and permissions are allowed;
- how local, repo, and skill-specific instructions interact;
- what project-specific conventions must be preserved.

These files drift over time. Common failures include:

- stale paths, commands, and runbooks;
- duplicated or contradictory instructions;
- global instructions overriding local ones incorrectly;
- missing guidance after repeated user corrections;
- unclear precedence across `SKILL.md`, `CODEX.md`, `CLAUDE.md`, and `AGENTS.md`;
- docs that describe desired behavior but do not reliably produce it;
- unchecked accumulation of one-off rules after isolated failures.

The current improvement loop is mostly manual. A user notices bad behavior, corrects the agent, and maybe someone updates a Markdown file later. That loop loses evidence, lacks regression tests, and rarely checks whether the edit actually improves future agent behavior.

## 3. Goals

- Continuously evaluate whether agent behavior matches the active instruction corpus.
- Detect instruction gaps, conflicts, stale guidance, and behavioral drift.
- Generate minimal, evidence-backed Markdown patch proposals.
- Run regression and held-out evals before accepting changes.
- Support a narrow, policy-controlled auto-apply lane for low-risk edits.
- Preserve full provenance for every proposal, rejection, acceptance, and rollback.
- Keep the main agent's runtime stable by applying instruction changes between episodes, not during active work.
- Use `llmff` manifests for reproducible optimization pipelines rather than building a bespoke inference runner.

## 4. Non-Goals

- This is not a replacement for Codex, Claude Code, or other agent runtimes.
- This is not an agent framework, planner, chat UI, or tool router.
- This is not a persistent world model or generalized memory database.
- This does not give an LLM unrestricted authority to rewrite its own policy.
- This does not make `llmff` responsible for approval, memory, patch authority, or governance.
- This does not guarantee model behavior is reproducible across provider changes.
- MVP does not include a public web dashboard, remote plugin registry, distributed vector store, or background auto-apply.

## 5. Core Principle

Instruction files are treated as production artifacts.

Every change must be:

- attributable to observed evidence or an explicit maintenance task;
- bounded in scope;
- validated against regression cases;
- reversible;
- auditable;
- governed by file-level and semantic risk policy.

The sidecar may optimize Markdown, but it must do so like a change-control system, not like unconstrained self-modification.

## 6. System Boundary

### 6.1 Main Agent Runtime

The main agent does the actual work: coding, testing, tool use, review, research, and reporting. It reads stable instruction files at task start or session start.

The sidecar does not interrupt the main agent mid-task to rewrite instructions. It may monitor live work, but config mutation is staged after an episode boundary.

### 6.2 Sidecar Supervisor

The sidecar supervisor owns:

- trace ingestion;
- instruction discovery and indexing;
- instruction precedence;
- work episode scoring;
- risk classification;
- `llmff` job orchestration;
- deterministic policy checks;
- eval suite selection;
- patch application;
- audit ledger writes;
- rollback workflow;
- daemon/CLI/CI modes.

### 6.3 `llmff`

`llmff` owns bounded pipeline execution:

- manifest-defined graph execution;
- typed inputs and outputs;
- backend/provider adapters;
- retrieval and reranking stages;
- JSON validation and repair;
- traces, events, checkpoints, and lifecycle artifacts;
- inspect/preflight output;
- subprocess exit status.

`llmff` must not own:

- long-term instruction policy;
- approval rules;
- edit authority;
- memory;
- sidecar scheduling;
- task-level retries;
- repo governance;
- VCS operations;
- final acceptance decisions.

## 7. Architecture

### 7.1 Components

#### Observer

Captures work episodes from main-agent activity.

Inputs may include:

- user request;
- selected instruction files;
- tool calls;
- command outputs;
- diffs;
- test results;
- user corrections;
- subagent reports;
- final answer;
- explicit success/failure labels.

The observer is read-only. It cannot mutate instruction files.

#### Instruction Corpus Manager

Discovers and indexes instruction documents.

Responsibilities:

- find configured instruction files;
- parse Markdown into heading-aware chunks;
- preserve byte ranges and anchors;
- compute content hashes;
- assign document kind and owner;
- record precedence and scope;
- snapshot files before each optimization run.

Example document kinds:

- global policy;
- repo policy;
- agent-specific policy;
- skill;
- project runbook;
- eval definition;
- memory-derived preference;
- local machine note.

#### Precedence Resolver

Builds the active instruction graph for a given work episode.

It resolves:

- global vs repo-specific instructions;
- system-level vs user-level vs skill-level constraints;
- agent-specific docs such as `CODEX.md` and `CLAUDE.md`;
- skill-specific scope;
- conflicting modal language;
- stale or shadowed instructions.

Output is a structured instruction context with explicit source references.

#### Episode Scorer

Classifies observed behavior against the instruction graph.

Failure classes:

- `agent_ignored_instruction`
- `instruction_missing`
- `instruction_ambiguous`
- `instruction_conflict`
- `instruction_stale`
- `instruction_too_broad`
- `instruction_too_specific`
- `eval_gap`
- `unsafe_instruction_pressure`
- `user_preference_not_encoded`
- `no_instruction_change_needed`

Each score includes:

- evidence references;
- affected instruction chunks;
- confidence;
- severity;
- suggested optimization target.

#### Optimization Job Supervisor

Runs `llmff` jobs for diagnosis, patch generation, and eval.

Responsibilities:

- pin manifest versions;
- run `llmff inspect --format json` before execution;
- write trace/events/checkpoint/output artifacts;
- enforce model/provider policy;
- handle exit codes;
- store job metadata;
- prevent unapproved network/plugin access.

#### Patch Proposer

Uses `llmff` pipelines to produce bounded Markdown patch candidates.

Patch candidates must include:

- base file hash;
- unified diff;
- rationale;
- evidence links;
- expected behavior change;
- risk class;
- evals required;
- rollback plan.

The proposer does not decide final acceptance.

#### Deterministic Policy Gate

Applies non-LLM checks to candidate patches.

Checks include:

- file allowlist;
- section allowlist;
- max changed lines;
- base hash match;
- Markdown parse validity;
- no malformed fenced blocks;
- no secrets;
- no new external endpoints without review;
- no weaker modal language in protected sections;
- no removal of approval, sandbox, test, review, secrets, memory, network, or deploy constraints;
- no contradiction with higher-priority instructions;
- no patch generated solely from a single untrusted source.

#### Eval Harness

Runs behavioral and structural evals before acceptance.

Eval categories:

- incident replay;
- held-out regression episodes;
- synthetic adversarial cases;
- cross-agent compatibility cases;
- Markdown lint and semantic diff checks;
- governance regression tests;
- provider smoke tests when explicitly enabled.

The eval harness must separate triggering episodes from held-out validation cases.

#### Patch Applicator

Applies accepted patches through auditable VCS mechanics.

Modes:

- proposal only;
- create branch;
- create PR;
- local commit;
- narrow auto-apply commit.

All applies require:

- pre/post file hashes;
- stored diff;
- eval report;
- policy decision;
- rollback command;
- audit ledger entry.

#### Audit Ledger

Append-only record of sidecar decisions.

Tracks:

- source episode IDs;
- instruction snapshots;
- llmff manifest hashes;
- inspect reports;
- model/backend identifiers;
- prompt template hashes;
- candidate diffs;
- eval suite versions;
- policy gate results;
- reviewer decisions;
- applied commits;
- rollbacks;
- rejected-edit memory.

The sidecar cannot rewrite its own audit ledger.

## 8. Data Flow

1. Main agent performs a work episode.
2. Observer captures trace bundle and outcome evidence.
3. Corpus manager snapshots active instruction files.
4. Precedence resolver builds the active instruction graph.
5. Episode scorer classifies behavior against instructions.
6. Optimization supervisor runs `llmff` diagnosis pipeline.
7. Patch proposer generates candidate Markdown diffs.
8. Deterministic policy gate rejects unsafe or invalid candidates.
9. Eval harness runs incident replay and held-out regression tests.
10. Acceptance policy classifies the candidate as reject, needs review, or auto-apply eligible.
11. Patch applicator writes a branch, PR, local commit, or proposal artifact.
12. Audit ledger records the complete decision bundle.
13. Future optimization runs retrieve accepted and rejected edit history.

## 9. `llmff` Pipeline Manifests

The system should use small, composable manifests rather than one giant optimization prompt.

### 9.1 `instruction-index.yaml`

Purpose: convert Markdown chunks and metadata into structured instruction records.

Inputs:

- document chunk text;
- path;
- heading path;
- document kind;
- precedence metadata.

Outputs:

- obligations;
- prohibitions;
- recommendations;
- examples;
- scope;
- referenced tools/files;
- risk tags.

### 9.2 `episode-audit.yaml`

Purpose: classify a work episode against active instructions.

Inputs:

- trace summary;
- tool events;
- diffs;
- test results;
- final answer;
- active instruction graph.

Outputs:

- failure class;
- evidence refs;
- affected instruction refs;
- severity;
- confidence;
- whether an instruction edit is warranted.

### 9.3 `drift-detect.yaml`

Purpose: detect recurring behavior drift across episodes.

Inputs:

- recent audit reports;
- accepted/rejected edit history;
- instruction graph snapshots.

Outputs:

- drift cluster;
- recurrence count;
- candidate root cause;
- priority.

### 9.4 `patch-propose.yaml`

Purpose: generate bounded Markdown edit candidates.

Inputs:

- audit report;
- affected chunks;
- file policy;
- rejected-edit memory;
- style constraints.

Outputs:

- unified diff;
- rationale;
- expected behavior change;
- risk class;
- eval requirements.

### 9.5 `patch-eval.yaml`

Purpose: judge behavioral impact of a candidate patch.

Inputs:

- candidate patch;
- old instruction graph;
- new instruction graph;
- eval cases.

Outputs:

- pass/fail;
- behavioral improvement score;
- governance regression score;
- failures;
- recommendation.

### 9.6 `acceptance-summary.yaml`

Purpose: produce a final structured acceptance bundle for review.

Inputs:

- candidate patch;
- policy gate results;
- eval reports;
- risk class.

Outputs:

- decision recommendation;
- reasons;
- evidence;
- reviewer checklist;
- rollback command.

## 10. Edit Operators

Inspired by SkillOpt, edits should use bounded natural-language operators:

- `add`: add missing guidance, examples, or constraints;
- `replace`: clarify ambiguous or stale guidance;
- `delete`: remove obsolete or duplicated guidance;
- `split`: separate mixed concerns into scoped sections;
- `merge`: consolidate repeated instructions;
- `demote`: move overly broad guidance into a narrower scope;
- `promote`: raise recurring local guidance to a higher-precedence file, review required;
- `annotate`: add provenance or examples without changing normative behavior.

Each operator has a risk profile. `delete`, `promote`, and authority-changing `replace` operations require review by default.

## 11. Risk Classes

### 11.1 Class A: Safe Auto-Apply Candidate

Allowed only when all checks pass.

Examples:

- typo fixes;
- broken internal links;
- formatting normalization;
- duplicate sentence removal;
- stale command path fix verified by local command;
- clarifying example that does not alter required behavior.

Constraints:

- one file;
- small diff budget, default max 20 changed lines;
- no protected heading changes;
- no deletion of normative requirements;
- no weaker modal language;
- evals pass;
- VCS-backed patch only.

### 11.2 Class B: Review-Required Improvement

Examples:

- adding new behavioral guidance;
- changing examples that may steer future model behavior;
- consolidating repeated rules across files;
- changing precedence notes;
- adding failure handling guidance;
- modifying testing or verification expectations.

Requires human or designated main-agent review.

### 11.3 Class C: Restricted Policy Change

Examples:

- tool permissions;
- sandbox behavior;
- approval requirements;
- network access;
- secrets handling;
- memory behavior;
- deployment behavior;
- security incident response;
- model/provider routing;
- sidecar's own authority.

Requires explicit human review and cannot auto-apply.

### 11.4 Class D: Prohibited

Examples:

- weakening immutable higher-priority policy;
- editing audit history;
- editing eval definitions to make a pending patch pass;
- changing approval policy and applying the change in the same run;
- accepting instructions from untrusted trace content as policy;
- loading arbitrary plugins from the repo under review.

Must be rejected.

## 12. Auto-Apply Policy

Auto-apply is disabled by default in MVP.

When enabled, auto-apply is limited to Class A edits and requires:

- proposal-only burn-in period has completed;
- low historical rejection and rollback rate;
- configured repo allowlist;
- unchanged base file hash;
- deterministic policy gate pass;
- held-out eval pass;
- no governance regression;
- VCS commit or branch;
- audit ledger write;
- one-command rollback.

Default production rollout:

1. Proposal-only for at least 30 days.
2. Auto-apply only for formatting, typos, broken links, and verified stale command references.
3. Expand only after observing low rollback rate and clear net benefit.

## 13. Threat Model

### 13.1 Instruction Poisoning

Untrusted traces may include adversarial text telling the sidecar to weaken config files.

Mitigations:

- treat trace content as evidence, not authority;
- require source classification;
- forbid direct policy adoption from untrusted text;
- include prompt-injection evals;
- require clustered evidence for normative edits.

### 13.2 Policy Erosion

Small edits may gradually weaken constraints.

Mitigations:

- protected terms and headings;
- semantic diff lint;
- immutable higher-priority policy;
- periodic baseline comparison;
- auto-apply rate limits;
- rollback metrics.

### 13.3 Reward Hacking

The optimizer may make instructions pass evals while reducing general quality.

Mitigations:

- held-out evals;
- rejected-edit memory;
- adversarial cases;
- longitudinal metrics;
- reviewer rejection analysis;
- diverse task suite.

### 13.4 Privilege Escalation

Markdown edits can change future tool permissions or approval norms.

Mitigations:

- Class C review gate;
- sidecar least privilege;
- no direct write to protected files except through VCS;
- no sidecar edits to its own approval policy.

### 13.5 Data Leakage

Traces and checkpoints may contain secrets.

Mitigations:

- local-first artifact storage;
- secret scanning;
- artifact retention policy;
- redaction before cloud model calls;
- provider allowlist;
- encrypted storage option for production.

### 13.6 Supply Chain Risk

Pipeline runners, plugins, and manifests can become attack paths.

Mitigations:

- pin `llmff` version;
- pin manifest hashes;
- disallow arbitrary repo plugin loading by default;
- run offline where possible;
- record inspect reports;
- expose plugin/network requirements before execution.

## 14. Storage Model

Use SQLite for indexed metadata and file-backed artifacts for raw outputs.

Artifacts live under:

```text
.sidecar/
  db.sqlite
  policy.yaml
  manifests/
  evals/
  runs/
    <run-id>/
      trace-input.jsonl
      instruction-snapshot/
      llmff-inspect.json
      llmff-events.jsonl
      llmff-trace.jsonl
      checkpoint/
      audit.json
      candidate.diff
      policy-gate.json
      eval-report.json
      decision.json
```

Core tables:

- `documents`: path, repo, kind, hash, mtime, parser version;
- `chunks`: document id, heading path, byte range, text hash, anchor;
- `episodes`: trace id, repo, started_at, outcome, summary hash;
- `runs`: run id, source episode id, manifest hash, status, timestamps;
- `audits`: run id, failure class, evidence refs, instruction refs, severity, confidence;
- `candidates`: audit id, base hash, diff hash, risk class, rationale, state;
- `evals`: candidate id, suite id, metrics, pass/fail, regression details;
- `decisions`: candidate id, actor, policy, decision, applied commit, rollback ref;
- `rejected_edits`: candidate id, reason, semantic fingerprint;
- `rollbacks`: decision id, reason, revert commit, post-rollback eval result.

## 15. Interfaces

### 15.1 CLI

```bash
sidecar doctor
sidecar index --repo PATH
sidecar index --repo PATH --check
sidecar audit --repo PATH --trace TRACE_PATH
sidecar propose --audit AUDIT_ID
sidecar eval --candidate CANDIDATE_ID --suite SUITE
sidecar apply --candidate CANDIDATE_ID
sidecar rollback --decision DECISION_ID
sidecar policy check --candidate CANDIDATE_ID
sidecar report --run RUN_ID
```

CLI mode is the MVP and source of truth.

### 15.2 CI Mode

CI should:

- run index checks on changed instruction files;
- run semantic policy lint;
- run relevant eval suites;
- fail with a patch/eval artifact when needed;
- never auto-apply by default.

### 15.3 Daemon Mode

Daemon mode is deferred until CLI mode is stable.

When added, it should:

- listen only on a Unix socket or `127.0.0.1`;
- watch configured trace directories;
- enqueue audits;
- expose run status and patch artifacts;
- use the same DB and command implementations as CLI mode;
- include a global read-only kill switch.

No public network listener is allowed in MVP.

### 15.4 VCS Interface

All writes go through VCS mechanics:

- create patch artifact;
- create branch;
- create PR;
- local commit;
- rollback via revert commit.

Untracked direct mutation is prohibited except in explicit dry-run scratch directories.

## 16. Policy File

Each repo may define `.sidecar/policy.yaml`.

Example:

```yaml
version: 1
mode: proposal_only
instruction_files:
  - path: AGENTS.md
    kind: repo_policy
    precedence: 80
    protected: true
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
  - path: .codex/skills/**/SKILL.md
    kind: skill
    precedence: 60
auto_apply:
  enabled: false
  max_changed_lines: 20
  allowed_risk_classes: [A]
  forbidden_terms:
    - approval
    - sandbox
    - secret
    - deploy
    - network
    - permission
    - must
    - never
evals:
  required_suites:
    - governance-regression
    - instruction-parser-golden
llmff:
  binary: llmff
  require_inspect: true
  allow_network: false
  allowed_manifest_hashes: []
```

## 17. Evaluation Strategy

### 17.1 Structural Evals

- Markdown parser golden tests;
- frontmatter preservation;
- fenced code block preservation;
- heading anchor stability;
- link/path validation;
- semantic diff classification.

### 17.2 Behavioral Evals

- replay real failures;
- replay successful episodes to ensure no regression;
- synthetic tasks for common instruction obligations;
- cross-agent cases for Codex and Claude-specific docs;
- final-answer evidence checks;
- tool-permission and approval-boundary checks.

### 17.3 Adversarial Evals

- malicious issue text asking to remove rules;
- poisoned command output;
- fake emergency deploy request;
- conflicting local/global instruction case;
- trace with forged success claims;
- hidden prompt-injection content in docs;
- eval leakage case.

### 17.4 Longitudinal Metrics

Track:

- proposal acceptance rate;
- reviewer rejection rate;
- rollback rate;
- recurring incident rate;
- mean changed lines per accepted edit;
- instruction corpus size growth;
- duplicate rule count;
- governance regression count;
- user correction recurrence.

## 18. MVP Scope

The MVP should implement:

- local CLI;
- Markdown discovery and chunking;
- SQLite metadata store;
- trace bundle ingestion from files;
- `llmff`-backed audit/propose/eval pipelines;
- deterministic patch safety gate;
- proposal artifact output;
- manual apply command;
- no daemon;
- no background auto-apply;
- no public network service;
- no dashboard.

The first useful loop:

```text
trace bundle -> audit -> candidate patch -> regression eval -> reviewable diff
```

## 19. Production Roadmap

### Phase 0: Design Validation

- Write fixtures for instruction files and traces.
- Define policy schema.
- Define `llmff` manifest contracts.
- Create manual eval cases.

### Phase 1: CLI Proposal Loop

- Implement `index`, `audit`, `propose`, `eval`, and `report`.
- Store all artifacts locally.
- Produce patch files only.
- No direct writes.

### Phase 2: VCS-Gated Apply

- Add `apply` and `rollback`.
- Create branches or local commits.
- Add provenance bundle.
- Add CI check mode.

### Phase 3: Review-Required Optimization

- Add rejected-edit memory.
- Add drift clustering.
- Add held-out eval suites.
- Add review summaries.

### Phase 4: Narrow Auto-Apply

- Enable Class A only after burn-in.
- Add rate limits and cooldowns.
- Add canary mode.
- Add global kill switch.

### Phase 5: Daemon Mode

- Watch trace directories.
- Queue audit jobs.
- Serve local-only status API.
- Preserve CLI as source of truth.

## 20. Testing Strategy

MVP tests:

- parser tests for Markdown headings, anchors, frontmatter, and fenced code;
- instruction precedence tests;
- trace ingestion fixture tests;
- `llmff inspect` artifact handling tests;
- patch parser and application tests;
- stale base hash rejection;
- overlapping edit rejection;
- protected section rejection;
- semantic modal weakening detection;
- deterministic mock backend tests;
- end-to-end fixture: failure trace to audit to patch to eval to review artifact.

Production tests:

- schema compatibility tests for `llmff` outputs;
- property tests for patch application;
- malicious trace fixtures;
- rollback tests;
- CI mode tests;
- live provider smoke tests, opt-in only;
- multi-repo policy isolation tests.

## 21. Operational Requirements

- Sidecar runs least-privilege by default.
- Proposal-only mode is default.
- All model calls must be attributable to a manifest hash and run ID.
- All external calls must be declared by inspect/preflight output.
- Raw trace artifacts must be protected from casual disclosure.
- Cloud model use requires redaction or explicit policy.
- Sidecar cannot edit its own audit records.
- Sidecar cannot change its own approval policy and apply that change in the same run.
- A global read-only kill switch must exist before daemon mode.

## 22. Open Decisions

- Exact trace format adapters for Codex, Claude Code, and other agent runtimes.
- Whether Zaxy stores long-term accepted/rejected edit memory or only summaries.
- Whether eval suites live in `.sidecar/evals` or a shared central repo.
- Whether Class A auto-applies should create local commits or PR branches by default.
- How to represent instruction precedence across mixed global, repo, and skill scopes.
- Whether to support remote sidecar execution after local-first MVP.
- How much raw prompt content can be retained safely.

## 23. Naming

Working names:

- Agent Instruction Sidecar
- Instruction Optimizer Sidecar
- Agent Config Optimizer
- InstructionOps
- SkillOps

The most precise product category is:

```text
agent instruction observability and optimization
```

## 24. Source Anchors

- `llmff`: https://github.com/syndicalt/llmff
- `llmff` agent workflow contract: https://github.com/syndicalt/llmff/blob/main/docs/agent-workflows.md
- SkillOpt paper page: https://huggingface.co/papers/2605.23904

## 25. Reviewer Synthesis

Three independent architecture reviews converged on the same shape:

- use `llmff` as the bounded pipeline runner, not the sidecar brain;
- keep policy, storage, approval, and patch authority in the sidecar;
- require deterministic gates around LLM-generated edits;
- treat auto-apply as a narrow, delayed production feature;
- preserve auditability, rollback, and rejected-edit memory;
- start with a CLI proposal loop before daemon or auto-apply behavior.

This architecture is production-feasible if it remains conservative about authority and aggressive about evidence.
