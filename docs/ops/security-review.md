# Tugboat Security Review Guide

## Purpose

Use this guide before enabling Tugboat in a production repository, before changing policy defaults, and before allowing provider-backed `llmff` runs. The review confirms Tugboat stays proposal-first and does not turn trace evidence into unreviewed authority.

## Review Scope

Review these surfaces:

- `.sidecar` storage, especially runs, traces, inspect reports, events, checkpoints, and SQLite state.
- Provider credential handling and any environment variables passed to `llmff`.
- CI artifact uploads and retention windows.
- Policy files that control allowlists, protected headings, provider routing, and auto-apply.
- MCP or daemon configuration, if present, including listener address and write-intent tooling.

## Required Checks

Run from the repository root:

```bash
rg -n "OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|token|secret|password" . --glob '!.git/**' --glob '!.sidecar/runs/**'
tugboat doctor
tugboat harness check --repo .
tugboat audit --repo . --trace traces/example.jsonl
```

Review outputs for:

- No provider credential value in committed files, logs, traces, or retained artifacts.
- Provider-backed approval requires reviewed manifest hashes and explicit `llmff.allowed_providers`.
- No environment-only approval path is accepted for provider-backed runs or provider smoke checks.
- `auto-apply` remains disabled unless a separate production approval explicitly enables it.
- The redaction pipeline runs before any cloud provider call or artifact upload.
- Candidate diffs do not modify secrets, sandboxing, network, approval, deployment, memory, or provider-routing policy without explicit human review.
- `.sidecar` audit history is append-only and not edited during cleanup.

## Approval Record

Record:

- Reviewer name and date.
- Commit or release version reviewed.
- Commands run and result.
- Open findings with owner and due date.
- Explicit decision: approved for proposal-only, approved for provider-backed pipelines, or rejected pending remediation.
