# Tugboat

Tugboat is a local-first sidecar for agent instruction observability and governed optimization. It watches the operational layer around coding agents, including `CODEX.md`, `AGENTS.md`, `SKILL.md`, runbooks, eval definitions, and local policy files.

The default posture is proposal-only. Tugboat creates evidence-backed review artifacts and keeps `llmff` as the bounded pipeline runner for inspect, run, trace, event, checkpoint, and typed output handling.

## Quickstart

Start with [docs/quickstart.md](docs/quickstart.md), then use
[docs/cli-reference.md](docs/cli-reference.md) for the full command surface.

## Safe First Run

Tugboat is proposal-only by default, and there are no provider credentials required after init when using the shipped local fixture backend:

```bash
python -m pip install tugboat
tugboat doctor
tugboat init
tugboat index --repo .
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

Review `.sidecar/runs/<run-id>/candidate.diff`, `eval-report.json`, `optimization-summary.json`, and `report.md` before applying any generated change.

## Documentation

For the production release framing, see
[docs/announcements/tugboat-production-release-article.md](docs/announcements/tugboat-production-release-article.md).

For setup and integration details, see [docs/troubleshooting.md](docs/troubleshooting.md),
[docs/integrations.md](docs/integrations.md), and
[docs/instruction-best-practices.md](docs/instruction-best-practices.md).

For release and production checks, see [docs/ops/release-checklist.md](docs/ops/release-checklist.md).

For the path to the stable release, see
[docs/roadmaps/v1.0.0-roadmap.md](docs/roadmaps/v1.0.0-roadmap.md).
