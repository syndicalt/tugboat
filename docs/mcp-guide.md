# Tugboat MCP Guide

## Transport

Run MCP over stdio:

```bash
tugboat mcp stdio
```

MCP is local-only adapter surface. It does not replace the CLI and must obey repo allowlist and per-tool policy.

## Read Tools

Read tools include `tugboat_status`, `tugboat_instruction_graph`, `tugboat_harness_findings`, `tugboat_latest_runs`, `tugboat_run_report`, and `tugboat_candidate`.

Responses return summaries and artifact references, not raw prompt or model payloads.

## Write-Intent Tools

Write-intent tools include `tugboat_record_episode`, `tugboat_request_audit`, `tugboat_request_proposal`, and `tugboat_request_eval`.

These tools require an explicit `allow` entry in `mcp.tool_policy` before they can run. `tugboat_record_episode` stores episode artifacts under `.sidecar/mcp/episodes`; the request tools create queued request artifacts under `.sidecar/mcp/requests`. None of these tools directly mutate instruction files.

## Security Policy

Configure a repo allowlist and per-tool policy in `.sidecar/policy.yaml`. A repo allowlist is mandatory for all MCP tools. Read tools may run with no per-tool entry unless denied; write-intent tools require explicit `allow`. The direct apply, rollback, policy change, provider credential management, and daemon control actions are not exposed through MCP tools.
