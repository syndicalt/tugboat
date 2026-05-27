# Tugboat MVP: proposal-only harness/config observability for coding agents

I'm releasing the first MVP of Tugboat, a local-first sidecar for maintaining the instruction and config layer around coding agents.

Repo: https://github.com/syndicalt/tugboat

The inspiration is the same:

- @OpenAI's harness engineering article by Ryan Lopopolo:
  https://openai.com/index/harness-engineering/

- Microsoft Research's SkillOpt paper, shared by @Yif_Yang:
  https://x.com/Yif_Yang/status/2058975302341296511
  https://huggingface.co/papers/2605.23904

The full vision is that agent traces should not just explain what went wrong. They should help maintain the repo-local operating knowledge that prevents the same failure next time.

This MVP is the first production-shaped slice of that loop.

Tugboat watches the harness and instruction layer around a repo: `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, policy docs, eval definitions, and related config. It indexes those files, checks harness hygiene, runs governed audit/proposal/eval flows through `llmff`, and emits review artifacts with provenance.

The MVP loop is:

```text
repo instructions + trace evidence -> audit -> bounded proposal -> eval -> human review
```

The important part: this release is proposal-only.

Tugboat does not auto-apply instruction changes by default. It produces review packets: diffs, rationales, evidence refs, eval results, decision metadata, rollback commands, and release artifacts. The goal is to make agent-operating knowledge observable and reviewable before anyone lets it mutate repo instructions.

What works in the MVP:

- indexes repo-local instruction and harness files
- checks harness docs and linked references
- normalizes trace evidence into durable sidecar artifacts
- runs `llmff`-backed audit/propose/eval pipelines
- gates candidate edits with policy, provenance, eval, and safety checks
- records rejected optimization fingerprints for later suppression
- keeps auto-apply disabled unless explicitly approved outside the MVP posture
- retains release evidence and a signed-off artifact manifest

The current release candidate passed the MVP gates:

- `820` tests passing
- `90.16%` coverage
- lint passing
- wheel build passing
- `twine check` passing
- clean virtualenv install smoke passing
- installed CLI smoke for `doctor`, `index --check`, and `harness check`
- release manifest generated for commit `c462115265a3bb0c0965b62d99757a48c7097f14`

This is not the whole vision yet.

It is the wedge: production harness/config observability plus governed optimization proposals. The next step is broader real-world usage: feed it more traces, tighten the review UX, and measure whether the proposed instruction changes reduce repeated agent failures without weakening authority boundaries.

OpenAI's harness engineering work points toward better agent environments. SkillOpt points toward skills and instructions as optimizable external state. Tugboat MVP turns that into a local operator workflow:

```text
observe -> propose -> validate -> review
```

No silent mutation.

No generic prompt fiddling.

A release candidate for keeping agent instructions honest.

## Short X Post

I'm releasing the Tugboat MVP: a local-first, proposal-only sidecar for coding-agent harness/config observability.

Repo: https://github.com/syndicalt/tugboat

Inspired by @OpenAI harness engineering and Microsoft Research SkillOpt.

It watches repo instructions like `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, and eval docs, then turns trace evidence into governed review proposals.

```text
observe -> propose -> validate -> review
```

MVP gates passed: 820 tests, 90.16% coverage, wheel build, clean install smoke, release manifest.
