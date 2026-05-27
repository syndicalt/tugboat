# Tugboat: turning agent traces into durable instruction updates

I’ve been building Tugboat, a developer utility for maintaining the instruction and config layer around coding agents.

The idea clicked after reading two recent pieces of work:

- @OpenAI’s harness engineering article by Ryan Lopopolo:
  https://openai.com/index/harness-engineering/

- Microsoft Research’s SkillOpt paper, shared by @Yif_Yang:
  https://x.com/Yif_Yang/status/2058975302341296511
  https://huggingface.co/papers/2605.23904

The OpenAI article argues that as agents take on more of the software lifecycle, engineering work shifts toward designing the environment around them: repo-local knowledge, feedback loops, mechanical checks, observability, and legible harnesses.

SkillOpt makes a complementary point: natural-language skills can be treated as trainable external state for agents.

That framing matters. It suggests we do not always need to change the model to improve agent behavior. Sometimes the best adaptation layer is the instruction, skill, harness, or config file sitting next to the code.

Tugboat is my attempt to apply that idea to everyday developer-agent workflows.

The problem is familiar: a coding agent fails, the user corrects it, and the session moves on. Maybe the task gets fixed. But the learning often stays trapped in the transcript.

The durable instruction file does not change. The skill does not improve. The harness does not get clearer. The next agent can repeat the same mistake.

Tugboat tries to close that loop.

It ingests an agent session trace, normalizes what happened, and asks whether the failure points to a missing, stale, ambiguous, or conflicting instruction. If it does, Tugboat proposes a bounded patch to the relevant repo-local config or instruction file.

The loop is:

```text
session trace -> audit -> proposed instruction/config edit -> eval -> human review
```

A trace might show that an agent skipped tests, ignored a repo convention, touched unrelated files, trusted hostile output, or needed the same user correction repeatedly.

Tugboat should turn that into an evidence-backed proposal:

- clarify this rule
- split this overloaded instruction
- add this regression check
- update this skill
- improve this harness contract

The proposal is not automatically trusted. It carries provenance from the trace, runs through policy and eval gates, and surfaces as a review packet: diff, rationale, evidence refs, eval result, and rollback metadata.

This is not meant to replace Codex, Claude Code, llmff, or any other agent runtime.

Tugboat is a sidecar. It watches the operational layer around agents: `CODEX.md`, `AGENTS.md`, skills, policies, harness docs, and related config.

The core bet is simple:

Agent traces should not just explain what went wrong. They should help maintain the instructions that prevent the same failure next time.

OpenAI’s harness engineering points toward better agent environments. Microsoft Research’s SkillOpt points toward skills as optimizable external state. Tugboat sits between those ideas for day-to-day software work: use real execution traces to improve the repo-local instructions and config that agents rely on.

Not more prompt fiddling.

Not telemetry for its own sake.

A feedback loop for agent operating knowledge.

## Short X post for the article link

I’ve been building Tugboat, inspired by @OpenAI’s harness engineering work and Microsoft Research’s SkillOpt paper from @Yif_Yang.

Agent traces should not just explain failures.

They should help maintain the repo-local instructions, skills, and config that prevent those failures next time.

```text
trace -> audit -> patch -> eval -> human review
```

Article: [link]

## Header image

Generated header image:

`/home/cheapseatsecon/.codex/generated_images/019e6269-da38-7e52-81b0-4bb0f3cfa85b/ig_02224b20f4e9f5f9016a151b1b2cc881978f4facae8a9f5d05.png`
