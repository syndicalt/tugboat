# Changelog

## Unreleased

- Continue v1 hardening against `docs/roadmaps/v1.0.0-roadmap.md`.
- Keep release candidates proposal-only by default unless a release note explicitly says otherwise.

## 1.0.0

Draft release notes live in `docs/releases/1.0.0-draft.md`.

Planned v1 highlights:

- Stable local sidecar workflow for governed maintenance of `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, policies, and eval definitions.
- Evidence-backed audit, proposal, eval, report, review, apply, and rollback workflows.
- Proposal-only by default with human review as the primary control.
- Narrow Class A auto-apply remains opt-in, policy-gated, eval-gated, VCS-backed, and rollback-backed.
- The read-only kill switch covers write paths including apply, auto-apply, retention deletion, migration apply, restore execution, and harness cleanup.
- Migration guide and compatibility policy for v1 adoption.
- The migration guide and compatibility policy are release-blocking v1 docs.
- Release notes keep known limitations documented before publication.

## 0.1.0

Initial proposal-only MVP release. See `docs/releases/0.1.0.md`.
