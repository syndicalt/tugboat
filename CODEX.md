# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Project Map

- Start with [docs/quickstart.md](docs/quickstart.md) for the CLI workflow and release smoke commands.
- Use [docs/cli-reference.md](docs/cli-reference.md) for the full command surface.
- Use [docs/architecture.md](docs/architecture.md) for Tugboat's sidecar, policy, storage, and `llmff` boundaries.
- Before promoting a build, follow [docs/ops/release-checklist.md](docs/ops/release-checklist.md).
- Use [docs/ci/ci-guide.md](docs/ci/ci-guide.md) and [docs/ci/github-actions-template.yml](docs/ci/github-actions-template.yml) for CI adoption.
- Use [docs/mcp-guide.md](docs/mcp-guide.md) for MCP setup and authority boundaries.
- Use [docs/apply-rollback.md](docs/apply-rollback.md) for review, apply, PR, and rollback workflows.
- Use [docs/auto-apply.md](docs/auto-apply.md) before touching the narrow auto-apply lane.
- Use [docs/daemon-guide.md](docs/daemon-guide.md) for queue, cycle, socket, profile, and read-only kill-switch operations.
- Use [docs/troubleshooting.md](docs/troubleshooting.md) for common blocked commands and incident triage.
- Use [docs/policy-examples.md](docs/policy-examples.md) for proposal-only, provider-backed, MCP, and auto-apply policy examples.
- Use [docs/threat-model.md](docs/threat-model.md) before changing trust boundaries, redaction, secrets handling, MCP, daemon, apply, or rollback behavior.
- Use [docs/ops/operating-runbook.md](docs/ops/operating-runbook.md), [docs/ops/security-review.md](docs/ops/security-review.md), [docs/ops/sidecar-backup-restore.md](docs/ops/sidecar-backup-restore.md), and [docs/ops/artifact-retention-redaction.md](docs/ops/artifact-retention-redaction.md) for production operations.
- Use [docs/ops/quick-adoption-proposal-only.md](docs/ops/quick-adoption-proposal-only.md) for credential-free existing-repo rollout.
- Release evidence lives in [docs/releases/0.1.0.md](docs/releases/0.1.0.md), [docs/ops/security-review-2026-05-26.md](docs/ops/security-review-2026-05-26.md), [docs/releases/production-candidate.md](docs/releases/production-candidate.md), and [docs/ops/security-review-production-candidate.md](docs/ops/security-review-production-candidate.md).
- Roadmap and historical planning live in [docs/roadmaps/2026-05-25-production-roadmap.md](docs/roadmaps/2026-05-25-production-roadmap.md) and [docs/superpowers/plans/2026-05-25-agent-instruction-sidecar-mvp.md](docs/superpowers/plans/2026-05-25-agent-instruction-sidecar-mvp.md).
- Announcement copy lives in [docs/announcements/tugboat-mvp-announcement.md](docs/announcements/tugboat-mvp-announcement.md) and [docs/announcements/tugboat-x-announcement.md](docs/announcements/tugboat-x-announcement.md).

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Use Zaxy for Memory

**Use memory early for non-trivial work. Keep it factual.**

- Check Zaxy/memory for relevant prior context before substantive work.
- Record durable assumptions, tradeoffs, decisions, and next steps when they will help future runs.
- Do not store secrets, credentials, private user data, or speculative guesses as facts.
- Keep memory entries concise and tied to verified project or user context.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
