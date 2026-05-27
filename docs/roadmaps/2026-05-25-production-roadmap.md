---
owner: platform
verification_status: verified
---

# Tugboat Production Roadmap

## Purpose

Tugboat should become a production-quality agent instruction observability and optimization system. It should run beside an agent as a local sidecar, expose optional MCP tools for live agent integration, use `llmff` as the reproducible pipeline runner, and optimize instruction/configuration files through a SkillOpt-style evidence loop.

The target is not a prompt editor. The target is a governed optimizer for agent harness artifacts: `AGENTS.md`, `CODEX.md`, `CLAUDE.md`, `SKILL.md`, runbooks, eval definitions, local harness docs, and related policy files.

## Source Principles

### Microsoft SkillOpt

SkillOpt treats a compact natural-language skill document as external trainable state for a frozen agent. The optimization loop is:

1. Roll out the target agent with the current skill/config.
2. Capture scored trajectories: messages, tool calls, verifier feedback, metadata, and final scores.
3. Reflect separately on successes and failures.
4. Propose bounded add/delete/replace edits under a textual learning-rate budget.
5. Accept only when held-out validation improves.
6. Retain rejected edits, slow-update memory, and optimizer-side meta guidance so future proposals avoid repeated bad directions.

Tugboat adapts that from single skill documents to a governed instruction corpus with precedence, provenance, rollback, and file-level authority boundaries.

### OpenAI Harness Engineering

OpenAI's harness guidance raises the implementation bar:

- Humans steer; agents execute.
- Repository knowledge is the system of record.
- Instruction files should be short maps to deeper repo-local docs, not giant manuals.
- Agent legibility is a product requirement.
- Architecture and taste should be enforced mechanically through tests, lints, and invariants.
- Feedback loops and cleanup agents prevent entropy from compounding.
- Isolated worktrees and local observability make agents able to validate their own work.

Tugboat should optimize the harness, not merely rewrite prose.

### llmff Contract

`llmff` remains the bounded pipeline substrate:

- Tugboat owns observation, policy, precedence, storage, approvals, patch authority, VCS mechanics, rollback, scheduling, and audit state.
- `llmff` owns manifest-defined execution, typed inputs and outputs, provider/backend abstraction, validation, traces, events, checkpoints, and exit codes.
- Tugboat must run `llmff inspect --format json` before pipeline execution.
- Payload outputs must stay separate from lifecycle events.
- Traces, events, checkpoints, and inspect reports must be stored under each run.
- Process exit code is the success/failure authority.
- Long jobs use checkpoints and resume only when manifest hashes match.

## Release Status

Status as of the production release candidate: Phases 0 through 10 are implemented for the local, proposal-first product surface. The remaining work is no longer baseline production readiness; it is post-release expansion, hardening, and operator experience.

The committed product provides:

- `tugboat doctor`
- `tugboat index --repo PATH`
- `tugboat audit --repo PATH --trace TRACE`
- `tugboat propose --repo PATH --audit RUN`
- `tugboat eval --repo PATH --candidate RUN --suite SUITE`
- `tugboat report --repo PATH --run RUN`
- `tugboat optimize --repo PATH --trace TRACE --suite SUITE`
- `tugboat harness check --repo PATH`
- review-gated apply and rollback plan commands
- read-first MCP tooling
- local daemon status, queue, and bounded run-once/cycle commands
- Markdown instruction discovery and heading-aware chunking
- SQLite metadata tables and hash-chained audit events
- JSONL trace ingestion with evidence IDs
- `llmff inspect` and file-backed `llmff run` artifact capture
- Deterministic policy gate
- Proposal/eval/report/optimization summary artifacts
- rejected-edit memory and validation baseline memory
- Auto-apply remains disabled by default, provider-backed runs require explicit policy, and direct instruction mutation is still outside the default proposal loop.

The release is intentionally not yet the full vision:

- bundled `llmff` manifests and provider-backed pipelines are still policy-gated and fixture-backed by default;
- apply and rollback are review-oriented VCS workflows, not autonomous instruction mutation;
- MCP and daemon support are local, bounded integration surfaces, not a public dashboard or remote service;
- auto-apply exists only as a narrow, explicitly confirmed lane with burn-in and rollback controls, not as the release default.

## Product Shape

### Runtime Modes

1. **CLI mode**
   - Source of truth for all behavior.
   - Required for local development, CI, debugging, and reproducibility.
   - Every daemon or MCP action delegates to the same command/service layer.

2. **MCP mode**
   - Optional adapter that lets an agent query Tugboat during a session.
   - Must expose read-heavy tools first: `status`, `index_summary`, `active_instructions`, `latest_audit`, `candidate_report`, `harness_findings`.
   - May expose write-intent tools later: `record_episode`, `request_audit`, `request_proposal`.
   - Must not expose direct instruction mutation.
   - Must use the same policy gates and audit ledger as CLI mode.

3. **Sidecar daemon mode**
   - Local, bounded integration surface over the same service layer.
   - Watches trace directories and worktree-local run directories.
   - Listens only on Unix socket or `127.0.0.1`.
   - Uses the CLI/service layer for all operations.
   - Includes a global read-only kill switch.

4. **CI mode**
   - Runs structural instruction checks, semantic policy lint, relevant eval suites, and harness legibility checks.
   - Emits artifacts.
   - Never auto-applies by default.

### Authority Boundary

Tugboat must be a change-control system:

- Trace content is evidence, not authority.
- `llmff` output is untrusted candidate data until deterministic gates pass.
- Accepted proposals require provenance, eval results, and policy decisions.
- Writes go through VCS mechanics.
- Audit history is append-only and hash-chained.
- Auto-apply remains disabled until a burn-in period proves low rollback/rejection rates.

## Target Architecture

```text
agent runtime
  -> trace adapter
  -> Tugboat observer
  -> corpus manager
  -> precedence resolver
  -> episode scorer
  -> llmff supervisor
       -> instruction-index.yaml
       -> episode-audit.yaml
       -> drift-detect.yaml
       -> patch-propose.yaml
       -> patch-eval.yaml
       -> acceptance-summary.yaml
  -> deterministic policy gate
  -> eval harness
  -> review/apply controller
  -> VCS adapter
  -> audit ledger
  -> reports, MCP tools, CI outputs
```

Core modules now present and continuing to mature:

- `supervisor`: `llmff run` orchestration, event streaming, checkpoint/resume, exit-code handling.
- `manifests`: bundled manifest templates, hash pinning, schema tests.
- `adapters`: Codex, Claude Code, generic JSONL, MCP-session, and CI trace adapters.
- `scoring`: deterministic scoring, human labels, agent-review labels, verifier labels.
- `optimization`: reflection result model, bounded edit model, rejected-edit memory, slow update.
- `evals`: incident replay, held-out regression, adversarial, cross-agent, governance, provider smoke.
- `patches`: unified diff parser, Markdown patch apply preview, overlap checks, semantic diff.
- `vcs`: branch, commit, PR, revert, dirty-state and stale-base checks.
- `mcp`: read-first server tools and write-intent request tools.
- `daemon`: queue, local socket, watcher, kill switch.
- `harness`: repo knowledge-map checks, doc freshness, structural lints, recurring cleanup proposals.

## Roadmap

The phase list below records the production-release implementation baseline. Future work should be tracked in a follow-on roadmap rather than reopening these baseline phases.

### Phase 0: Production Baseline Hardening

Goal: make the MVP trustworthy enough to build on.

Deliverables:

- Replace canned audit/propose/eval behavior with explicit fixture-backed service interfaces.
- Add schema-versioned artifact models for `audit.json`, `candidate.json`, `eval-report.json`, `decision.json`, and `report.md`.
- Add JSON schema validation for every run artifact.
- Add secret scanning for traces, snapshots, prompts, diffs, checkpoints, and reports.
- Add retention policy config for raw traces and checkpoints.
- Expand deterministic policy gate:
  - file allowlist and glob allowlist;
  - protected heading allowlist;
  - max changed lines per risk class;
  - Markdown parse validity;
  - fenced block validity;
  - frontmatter preservation;
  - no removal of approval, sandbox, test, review, secrets, memory, network, deploy, and permission constraints;
  - no higher-priority contradiction;
  - no single-untrusted-source policy adoption;
  - no eval-definition edit while evaluating a pending candidate.
- Add current-state dashboard via CLI report: `tugboat status --repo PATH`.

Exit criteria:

- `pytest -q` passes.
- Every artifact has a schema test.
- Malicious trace fixture suite passes.
- Policy gate rejects all restricted/prohibited examples from `SPEC.md`.
- No live provider or network needed for tests.

### Phase 1: Real llmff Pipeline Execution

Goal: turn Tugboat from artifact generator into real `llmff`-backed optimizer supervisor.

Deliverables:

- Implement `LlmffRunSupervisor`.
- Add `llmff run` support with:
  - `--trace`;
  - `--events`;
  - `--checkpoint`;
  - `--timeout-ms`;
  - `--retry-attempts`;
  - explicit input and output paths.
- Preserve `llmff` exit codes in Tugboat run status.
- Capture `run_failed.failure_kind` and sanitized failure messages.
- Store inspect, trace, events, checkpoint, and declared outputs under `.sidecar/runs/<run-id>/`.
- Implement manifest registry:
  - `.sidecar/manifests/instruction-index.yaml`;
  - `.sidecar/manifests/episode-audit.yaml`;
  - `.sidecar/manifests/drift-detect.yaml`;
  - `.sidecar/manifests/patch-propose.yaml`;
  - `.sidecar/manifests/patch-eval.yaml`;
  - `.sidecar/manifests/acceptance-summary.yaml`.
- Add manifest hash pinning and policy allowlist.
- Add mock-backend fixtures for all manifests.

Exit criteria:

- `tugboat audit` consumes real `llmff` audit output.
- `tugboat propose` consumes real `llmff` candidate output.
- `tugboat eval` consumes real `llmff` eval output.
- Tests prove inspect-before-run, exit-code preservation, checkpoint mismatch rejection, and file-backed output ownership.

### Phase 2: Trace Adapters and Episode Capture

Goal: collect high-quality rollout evidence from real agent work.

Deliverables:

- Define canonical episode schema:
  - request;
  - active instruction snapshot;
  - tool calls;
  - command outputs;
  - diffs;
  - test results;
  - user corrections;
  - subagent reports;
  - final answer;
  - outcome labels;
  - verifier scores.
- Implement adapters:
  - Codex local session export;
  - Claude Code transcript import;
  - generic JSONL;
  - MCP live event capture;
  - CI failure import.
- Add redaction pipeline before cloud model calls.
- Add source trust classification.
- Add outcome scoring plugins:
  - tests pass/fail;
  - human accepted/rejected;
  - agent review severity;
  - policy violation;
  - user correction recurrence.

Exit criteria:

- Tugboat can ingest at least three real trace formats into one canonical schema.
- Trace fixtures cover prompt injection, forged success, poisoned command output, secrets, and conflicting instructions.
- Audit reports cite evidence IDs and instruction refs precisely.

### Phase 3: SkillOpt-Style Optimization Loop

Goal: implement the rollout -> reflect -> bounded edit -> held-out gate loop.

Deliverables:

- Add batch run model:
  - train episodes;
  - held-out validation episodes;
  - unseen test suites.
- Implement success/failure minibatch construction.
- Implement reflection artifacts:
  - recurring failure patterns;
  - preserved success patterns;
  - affected instruction chunks;
  - proposed root cause.
- Implement bounded edit operators:
  - `add`;
  - `replace`;
  - `delete`;
  - `split`;
  - `merge`;
  - `demote`;
  - `promote`;
  - `annotate`.
- Implement textual learning-rate budgets:
  - max files touched;
  - max sections touched;
  - max changed lines;
  - max normative changes;
  - operator-specific risk limits.
- Implement candidate ranking and merge of compatible edits.
- Implement held-out validation gate:
  - candidate accepted only if validation score strictly improves;
  - regression score must not degrade beyond tolerance;
  - governance suite must pass.
- Add rejected-edit memory:
  - semantic fingerprint;
  - rejection reason;
  - affected source refs;
  - future proposal suppression signal.
- Add slow-update memory:
  - recurring successful edits;
  - recurring rejected directions;
  - optimizer-side meta guidance.

Exit criteria:

- A fixture benchmark demonstrates at least one accepted improvement and one rejected harmful edit.
- Held-out and triggering episodes are separate.
- Rejected edits influence later proposals.
- All accepted candidates include bounded operator metadata.

### Phase 4: Evaluation Harness

Goal: make "better instruction" mean measured behavior improvement, not plausible prose.

Deliverables:

- Structural eval suites:
  - parser golden tests;
  - anchor stability;
  - frontmatter preservation;
  - fenced code preservation;
  - link/path validation;
  - semantic diff classification.
- Behavioral eval suites:
  - incident replay;
  - successful episode no-regression;
  - common instruction obligation tasks;
  - cross-agent Codex/Claude compatibility;
  - final-answer evidence checks;
  - tool-permission and approval-boundary checks.
- Adversarial eval suites:
  - malicious issue text;
  - poisoned command output;
  - fake emergency deploy;
  - forged success claims;
  - hidden prompt injection in docs;
  - eval leakage.
- Longitudinal metrics:
  - acceptance rate;
  - rejection rate;
  - rollback rate;
  - recurring incident rate;
  - mean changed lines;
  - corpus growth;
  - duplicate rule count;
  - governance regression count;
  - user correction recurrence.

Exit criteria:

- `tugboat eval --suite all` can run offline using fixtures.
- Live provider smoke tests exist but are opt-in.
- Reports show trigger score, held-out score, governance result, and recommendation.

### Phase 5: VCS-Gated Apply and Rollback

Goal: support real instruction changes without untracked mutation.

Deliverables:

- Add `tugboat apply --candidate ID` modes:
  - proposal artifact only;
  - create branch;
  - local commit;
  - create PR when configured.
- Add `tugboat rollback --decision ID`.
- Implement VCS adapter:
  - clean-worktree checks;
  - target-file dirty checks;
  - base hash checks;
  - branch naming;
  - commit message generation;
  - revert commit generation;
  - PR metadata bundle.
- Store:
  - pre/post hashes;
  - applied commit;
  - rollback command;
  - review actor;
  - decision rationale.
- Enforce Class C and Class D rules:
  - Class C always explicit human review;
  - Class D always reject.

Exit criteria:

- Apply and rollback tests cover clean, dirty, stale-base, conflicting patch, and revert paths.
- No code path writes instruction files without VCS adapter.
- Audit ledger proves every applied change and rollback.

### Phase 6: MCP Integration

Goal: let agents use Tugboat as a live harness tool without giving it unsafe authority.

Recommended stance: MCP is an adapter, not the source of truth. The CLI/service layer remains authoritative.

Read-only MCP tools:

- `tugboat_status(repo)`
- `tugboat_instruction_graph(repo)`
- `tugboat_harness_findings(repo)`
- `tugboat_latest_runs(repo, limit)`
- `tugboat_run_report(repo, run_id)`
- `tugboat_candidate(repo, candidate_id)`

Write-intent MCP tools:

- `tugboat_record_episode(repo, trace_jsonl)`
- `tugboat_request_audit(repo, trace_id)`
- `tugboat_request_proposal(repo, audit_id)`
- `tugboat_request_eval(repo, candidate_id, suite)`

Explicitly deferred MCP tools:

- direct apply;
- rollback;
- policy change;
- provider credential management;
- daemon mode control.

Security requirements:

- local-only transport;
- repo allowlist;
- per-tool policy checks;
- no secret-bearing payloads in tool responses;
- audit every MCP call;
- return artifact refs instead of raw prompts or model payloads.

Exit criteria:

- MCP server passes contract tests.
- Codex or Claude can query Tugboat status and request audits.
- No MCP tool can directly mutate instruction files.

### Phase 7: Harness Engineering System

Goal: encode OpenAI harness best practices as enforceable repository health machinery.

Deliverables:

- Expand `tugboat harness check`:
  - short instruction-map limits;
  - required repo-local references;
  - stale link detection;
  - ownership metadata;
  - verification-status metadata;
  - doc freshness against source files;
  - duplicate/conflicting rule detection;
  - orphaned runbooks;
  - "too many MUSTs" signal;
  - giant instruction-file warnings.
- Add `tugboat harness report`:
  - knowledge-map graph;
  - missing docs;
  - stale docs;
  - recurring failures without docs;
  - proposed doc-gardening tasks.
- Add recurring cleanup proposal loop:
  - scans docs and rules;
  - opens cleanup candidates;
  - runs structural evals;
  - never auto-applies governance changes.
- Add worktree-local run profiles:
  - `.sidecar/runs` isolation;
  - optional per-worktree app boot metadata;
  - optional local observability references.

Exit criteria:

- Harness checks are usable in CI.
- Cleanup proposals are small, reviewable, and tied to evidence.
- Agent instructions remain map-like instead of monolithic.

### Phase 8: Daemon and Queue

Goal: make Tugboat continuously useful without making it externally exposed or self-authorizing.

Deliverables:

- Local queue backed by SQLite.
- Watch configured trace directories.
- Unix socket or `127.0.0.1` only.
- Read-only kill switch.
- Job state machine:
  - queued;
  - inspecting;
  - running;
  - evaluating;
  - waiting_review;
  - rejected;
  - applied;
  - rolled_back;
  - failed.
- Job leases and crash recovery.
- Checkpoint resume for long `llmff` jobs.
- Rate limits and concurrency limits.

Exit criteria:

- Daemon can be killed into read-only mode.
- Restart resumes queued jobs safely.
- No public listener exists.
- CLI and MCP read daemon state without bypassing the service layer.

### Phase 9: Narrow Auto-Apply

Goal: support a very small automatic lane only after evidence proves safety.

Prerequisites:

- At least 14 days proposal-only burn-in by default.
- Historical rejection rate at or below the policy threshold; default maximum is 10%.
- Historical rollback rate at or below the policy threshold; default maximum is 2%.
- Maximum changed-line budget at or below the policy threshold; default maximum is 30 changed lines.
- Repo allowlist.
- Class A only.
- Held-out eval pass.
- Governance regression pass.
- VCS-backed commit or branch.
- One-command rollback.

Allowed examples:

- typo fixes;
- broken internal links;
- formatting normalization;
- duplicate sentence removal;
- verified stale command reference.

Forbidden:

- memory behavior;
- approvals;
- sandboxing;
- network;
- deployment;
- secrets;
- provider routing;
- sidecar authority;
- anything Class B/C/D.

Exit criteria:

- Auto-apply defaults off.
- Enabling requires policy plus CLI confirmation.
- Every auto-apply is auditable and reversible.

### Phase 10: Production Operations

Goal: make Tugboat operable for real teams.

Deliverables:

- Installation packages.
- Config migration system.
- Release checklist.
- CI templates.
- Golden fixture suite.
- Security review guide.
- Backup/restore for `.sidecar`.
- Artifact retention and redaction controls.
- Observability:
  - run duration;
  - failure kind;
  - provider/backend failure rate;
  - accepted/rejected/rolled-back edits;
  - eval suite trends;
  - corpus size growth.
- Documentation:
  - quickstart;
  - architecture;
  - threat model;
  - policy examples;
  - MCP guide;
  - CI guide;
  - operating runbook.

Exit criteria:

- Fresh repo can adopt Tugboat in under 15 minutes.
- Existing repo can run proposal-only mode without credentials.
- Production repo can opt into provider-backed `llmff` pipelines with explicit policy.

## Core Data Model Extensions

Add or mature these tables:

- `trace_events`
- `instruction_snapshots`
- `instruction_graphs`
- `llmff_jobs`
- `llmff_events`
- `llmff_outputs`
- `reflections`
- `edit_operations`
- `candidate_edits`
- `eval_cases`
- `eval_runs`
- `validation_splits`
- `review_actions`
- `mcp_calls`
- `daemon_jobs`
- `harness_findings`
- `doc_gardening_runs`
- `optimizer_memory`

Every row that participates in a decision must be reachable from an append-only audit event.

## Risk Classes

Keep the current classes and make them executable:

- **Class A:** safe, tiny, auto-apply candidate only after burn-in.
- **Class B:** review-required improvement.
- **Class C:** restricted policy change requiring explicit human review.
- **Class D:** prohibited; reject.

Risk classification must consider:

- file;
- section;
- edit operator;
- changed modal language;
- source trust;
- affected policy domain;
- eval impact;
- precedence level;
- prior rejected-edit fingerprints.

## Acceptance Criteria for Full Vision

Tugboat fulfills the vision when all of these are true:

1. A real agent episode can be captured without manual transcript shaping.
2. Active instruction files are indexed with precedence and source refs.
3. `llmff` pipelines diagnose the episode and produce typed audit outputs.
4. `llmff` pipelines propose bounded edit operations from evidence.
5. Deterministic policy gates reject unsafe candidates before review.
6. Incident replay and held-out validation run before acceptance.
7. A candidate is accepted only when validation improves and governance does not regress.
8. Rejected edits influence later proposals.
9. VCS-backed apply and rollback are audited.
10. MCP can expose Tugboat to an agent without granting mutation authority.
11. Harness checks keep repository knowledge agent-legible.
12. CI can enforce structural and governance invariants.
13. Daemon mode is local-only and kill-switchable.
14. Auto-apply remains narrow, evidence-gated, and reversible.
15. Operators can inspect every decision from trace evidence to final patch.

## Near-Term Implementation Order

The original near-term order has been delivered for the production candidate:

1. **llmff supervisor and manifest contracts**
   - Delivered as policy-gated `llmff` inspect/run handling, file-backed events/traces/checkpoints, manifest registry, and typed output validation.

2. **episode audit pipeline**
   - Delivered through canonical trace inputs, evidence refs, instruction refs, and audit artifacts.

3. **patch proposal pipeline**
   - Delivered through bounded candidate artifacts, style and policy constraints, rejected-edit memory, and deterministic patch validation.

4. **evaluation harness**
   - Delivered through incident replay, held-out validation, governance regression checks, and adversarial fixtures.

5. **VCS-gated apply/rollback**
   - Delivered through proposal, branch, commit, PR, and rollback workflows while preserving proposal-only default.

MCP, daemon operations, auto-apply, and Phase 10 production docs/ops were also delivered for the release candidate.

## Resolved Decisions

- Trace adapters ship as a multi-format surface: Codex, Claude, generic JSONL, CI, and MCP traces.
- Tugboat owns the default fixture-backed `llmff` integration and manifest contracts for the local release surface; provider-backed expansion remains policy-gated.
- Accepted/rejected optimizer memory stays local SQLite for the release product.
- MCP is packaged as part of the `tugboat` CLI through `tugboat mcp stdio`.
- Daemon queue state uses `.sidecar/daemon.sqlite`; durable audit and decision state use `.sidecar/db.sqlite`.
- Auto-apply thresholds are policy-owned and checked against ledger-derived metrics. Runtime CLI parameters confirm intent; they do not override burn-in, rejection-rate, rollback-rate, or changed-line thresholds.

## Remaining Open Decisions

- What minimum validation lift is required for acceptance: strict score improvement, confidence interval, or policy-specific threshold.
- How to represent conflicting instruction precedence across global, repo, user, and skill scopes.
- Whether post-release optimizer summaries should export to Zaxy as an optional operator memory bridge.
- Whether provider-backed `llmff` manifests should remain bundled in Tugboat or move into a separate manifest package after real-world usage.

## Non-Negotiable Invariants

- No direct mutation of instruction files outside VCS adapter.
- No auto-apply by default.
- No public network listener.
- No raw secrets in reports, events, or MCP responses.
- No hidden provider calls; inspect/preflight must declare external requirements.
- No model output is accepted without deterministic validation.
- No triggering episode may serve as the only validation case.
- No policy weakening from a single untrusted source.
- No audit history rewrite.
- No `llmff` ownership of approval, patch authority, memory, or governance.

## References

- `SPEC.md`
- `docs/superpowers/plans/2026-05-25-agent-instruction-sidecar-mvp.md`
- Microsoft SkillOpt project: https://microsoft.github.io/SkillOpt/
- SkillOpt paper page: https://huggingface.co/papers/2605.23904
- OpenAI harness engineering: https://openai.com/index/harness-engineering/
- `llmff` agent workflow contract: https://raw.githubusercontent.com/syndicalt/llmff/main/docs/agent-workflows.md
