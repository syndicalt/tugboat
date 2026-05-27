# Tugboat

Tugboat is a local-first sidecar for agent instruction observability and governed optimization. It watches the operational layer around coding agents, including `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, eval definitions, and local policy files.

The default posture is proposal-only. Tugboat creates evidence-backed review artifacts and keeps `llmff` as the bounded pipeline runner for inspect, run, trace, event, checkpoint, and typed output handling.

## Quickstart

Start with [docs/quickstart.md](docs/quickstart.md), then use
[docs/cli-reference.md](docs/cli-reference.md) for the full command surface.

For release and production checks, see [docs/ops/release-checklist.md](docs/ops/release-checklist.md).
