---
owner: platform
verification_status: verified
---

# Tugboat Threat Model

## Assets

- Instruction files such as `CODEX.md`, `AGENTS.md`, `CLAUDE.md`, and `SKILL.md`.
- `.sidecar` run artifacts, SQLite state, and the append-only audit ledger.
- Provider credentials, local traces, policy files, and VCS history.

## Trust Boundaries

Untrusted traces, model outputs, MCP payloads, and daemon job payloads cross into Tugboat. Treat untrusted traces as hostile input until path validation, secret scanning, and hash checks complete for the local repo, VCS, policy file, and audit ledger.

## Threats

- Untrusted traces may request weaker sandboxing, approval, secret, network, or deployment policy.
- A candidate diff may try to alter Tugboat authority or protected instructions.
- MCP clients may request raw prompts or attempt direct apply.
- A daemon listener may accidentally become non-local.

## Controls

Controls include local-only transport, repo allowlist checks, secret scanning, redaction, deterministic policy gates, VCS-backed apply, rollback plans, append-only audit events, and rejection of direct instruction mutation outside the VCS adapter.
