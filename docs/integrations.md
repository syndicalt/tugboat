---
owner: platform
verification_status: draft
---

# Integration Guide

## Purpose

This guide shows how to connect Tugboat to common local agent workflows without changing Tugboat's authority model. Tugboat stays local-first and proposal-only by default. It ingests traces, produces review artifacts, and requires explicit VCS-backed apply or rollback commands for mutation.

## Common Setup

Run the same baseline setup for every integration:

```bash
tugboat doctor --repo .
tugboat init --repo .
tugboat index --repo . --check
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

If `tugboat doctor --repo .` reports missing policy, initialize before running the proposal loop. If it reports provider-backed networking, confirm the repo policy and manifest hashes before using provider-backed pipelines.

## Codex

Use exported Codex session JSONL as the preferred trace source. The shared safe fixture is `tests/fixtures/traces/codex-local-session-export.jsonl`.

```bash
tugboat optimize --repo . --trace tests/fixtures/traces/codex-local-session-export.jsonl --trace-format codex --suite all
```

Codex traces can include base instruction snapshots and tool evidence. Tugboat treats those as evidence, not authority.

## Claude Code

Use Claude transcript JSON or JSONL exports when available. The shared safe fixture is `tests/fixtures/traces/claude-transcript.json`.

```bash
tugboat audit --repo . --trace tests/fixtures/traces/claude-transcript.json --trace-format claude
```

Review generated candidate diffs before applying them. Claude transcript content is untrusted until Tugboat's deterministic gates and evals pass.

## Cursor

For Cursor workflows, export or adapt agent session logs into generic JSONL, Codex-style JSONL, or MCP-session JSONL. Keep repository instructions in files Tugboat indexes, such as `CODEX.md`, `AGENTS.md`, and `SKILL.md`.

Use `tugboat index --repo . --check` in CI to catch broken instruction references before accepting proposed harness changes.

## Aider

For Aider workflows, capture command output, final responses, and changed-file context into generic JSONL or CI failure JSON. Run Tugboat in proposal-only mode first:

```bash
tugboat audit --repo . --trace traces/aider-session.jsonl
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
```

Do not point auto-apply at broad code-edit traces. Keep auto-apply limited to reviewed Class A harness/config maintenance lanes.

## Continue.dev

For Continue.dev, capture local session traces or convert exported chat/tool records into generic JSONL. Store examples under a repo-local `traces/` directory that is safe to share or already redacted.

Run `tugboat retention --repo . --redact-output` before sharing retained run artifacts outside the local machine.

## MCP Clients

Use the read-only profile first:

```bash
tugboat mcp stdio --repo . --read-only
```

MCP clients may read status, instruction graph, harness findings, latest runs, candidate reports, and decision trace references. Do not pass provider credentials through MCP. Do not expose MCP tools as a direct instruction mutation path.

## CI Systems

CI should validate the harness and produce artifacts, not silently mutate instructions:

```bash
tugboat doctor --repo .
tugboat ci --repo .
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

Use CI failure traces to feed the proposal loop after review. The shared safe fixture is `tests/fixtures/traces/ci-failure.json`.

## Trace Fixtures

Safe shared fixtures:

- `tests/fixtures/traces/codex-local-session-export.jsonl`
- `tests/fixtures/traces/claude-transcript.json`
- `tests/fixtures/traces/mcp-session.jsonl`
- `tests/fixtures/traces/ci-failure.json`

Each fixture should be credential-free, redacted, and ingestible by the real trace adapters. Add new fixtures only with tests that prove format detection and ingestion.

## Safety Defaults

The default posture is proposal-only. auto-apply remains disabled unless a repo explicitly enables a narrow Class A lane and passes policy, confirmation, ledger, eval, VCS, and rollback gates.

Provider-backed execution remains opt-in. Keep `llmff.allow_network: false` unless the repo intentionally enables provider-backed manifests and pins or reviews manifest hashes.
