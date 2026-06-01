---
owner: platform
verification_status: verified
---

# Instruction Best Practices

## Purpose

Use this guide before asking Tugboat to maintain `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, or eval definitions. Tugboat is proposal-only by default, so these practices make generated candidates easier to review, validate, and roll back.

Tugboat should treat trace and model output as evidence, never authority. The repo policy `instruction_files` list remains the authority boundary for which instruction surfaces Tugboat can observe and propose against. In monorepos, use `scope_root` on `instruction_files` entries to make package or service ownership explicit and to prevent cross-scope instruction mutations.

## Instruction Maps

The baseline rule is to keep instruction files as maps to repo-local docs. `CODEX.md` and `AGENTS.md` should name the stable operating rules, point to owned runbooks, and avoid copying large policy blocks from other files.

Do:

- keep authority, ownership, and scope visible near the top of the file;
- link to repo-local runbooks instead of embedding long procedures;
- use stable headings that Tugboat can cite in evidence and reports;
- avoid monolithic policy growth by moving detailed procedures into owned docs;
- run `tugboat harness check --repo .` after reorganizing instruction maps.

Do not:

- hide approval, sandbox, network, or rollback rules in unrelated sections;
- mix temporary incident notes into permanent policy;
- add every local exception to the top-level agent file.

## Skills

The baseline rule is to keep skills triggerable and testable. `SKILL.md` files should explain when the skill applies, what inputs it expects, what checks prove completion, and which files or commands are authoritative.

Do:

- keep trigger rules concrete enough for an agent to decide whether the skill applies;
- preserve trigger conditions when accepting a rewrite;
- list verification commands and expected artifacts;
- keep safety constraints explicit when a skill can change instructions, policy, evals, or release files;
- prefer links to examples or fixtures over long embedded transcripts.

Do not:

- broaden authority with vague phrases such as "handle anything related";
- remove prerequisites that prevent unsafe execution;
- bury required checks in prose that is not tied to a command or artifact.

## Runbooks

The baseline rule is to keep runbooks rollback-ready. A runbook should say who owns the procedure, when to use it, what evidence to collect, what command sequence is safe, and how to return to the previous state.

Do:

- include preconditions, stop criteria, rollback steps, and retained evidence;
- keep production operations local-first unless a reviewed policy explicitly enables networked provider paths;
- keep destructive actions behind explicit execute or apply flags.

Do not:

- treat review-only commands and mutating commands as interchangeable;
- omit the artifact paths an operator should inspect;
- rely on unrecorded chat context to explain why a procedure is safe.

## Eval Definitions

The baseline rule is to keep eval definitions separate from trigger traces. Evals should validate candidate behavior without being edited by the same pending candidate they are judging.

Do:

- store eval definitions in stable, reviewable files;
- name the behavior being protected and the expected pass/fail signal;
- keep held-out or unseen validation separate from the trace that triggered a proposal;
- review any candidate that edits eval definitions as policy-sensitive.

Do not:

- let a pending candidate weaken the eval that decides whether it passes;
- place secrets or provider credentials in eval fixtures;
- use eval definitions as a substitute for operator review.

## Review Checklist

Before accepting a Tugboat proposal:

- confirm the candidate is grounded in cited trace or harness evidence;
- confirm `eval-report.json`, `optimization-summary.json`, and `report.md` agree on the decision;
- inspect token footprint and safety checks for `SKILL.md` rewrites;
- verify rollback instructions exist before any apply mode;
- keep auto-apply disabled unless the repo has explicitly opted into narrow Class A lanes.
