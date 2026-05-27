---
owner: platform
verification_status: verified
---

# Tugboat MCP Guide

## Transport

For read-only agent attachment, bind the stdio server to the current repo:

```bash
tugboat mcp stdio --repo . --read-only
```

In this profile Tugboat injects the bound repo for read tool calls, rejects repo overrides, and does not advertise write-intent tools. MCP is a local-only adapter surface. It does not replace the CLI and must obey the repo allowlist and per-tool policy.

Unbound stdio remains available for clients that pass a `repo` argument with each tool call:

```bash
tugboat mcp stdio
```

## Read Tools

Read tools include `tugboat_status`, `tugboat_instruction_graph`, `tugboat_harness_findings`, `tugboat_latest_runs`, `tugboat_run_report`, `tugboat_candidate`, and `tugboat_decision_trace`.

Responses return summaries and artifact references, not raw prompt or model payloads. `tugboat_decision_trace` writes a local `decision-trace.json` artifact and returns its repo-relative path and hash.

## Write-Intent Tools

Write-intent tools include `tugboat_record_episode`, `tugboat_request_audit`, `tugboat_request_proposal`, and `tugboat_request_eval`.

These tools require an explicit `allow` entry in `mcp.tool_policy` before they can run. `tugboat_record_episode` stores episode artifacts under `.sidecar/mcp/episodes`; the request tools create queued request artifacts under `.sidecar/mcp/requests`. None of these tools directly mutate instruction files.

## Security Policy

Configure a repo allowlist and per-tool policy in `.sidecar/policy.yaml`. A repo allowlist is mandatory for all MCP tools, including bound read-only stdio. Read tools may run with no per-tool entry unless denied; write-intent tools require explicit `allow`. The direct apply, rollback, policy change, provider credential management, and daemon control actions are not exposed through MCP tools.
