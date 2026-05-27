# Tugboat Policy Examples

## Proposal Only

Create a proposal-only starter policy with:

```bash
tugboat init --repo .
```

The generated policy is intentionally conservative and does not overwrite existing policy files.

`.sidecar/policy.yaml`:

```yaml
version: 1
mode: proposal_only
instruction_files:
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
auto_apply:
  enabled: false
```

## Provider Backed

```yaml
llmff:
  binary: llmff
  require_inspect: true
  allow_network: false
  allowed_providers:
    - openai
  allowed_manifest_hashes:
    - replace-with-reviewed-manifest-hash
```

Provider-backed runs require explicit provider policy and reviewed manifest hashes. Omit `allowed_providers` for credential-free local and fixture-backed runs.

## MCP Allowlist

```yaml
mcp:
  allowed_repositories:
    - /absolute/path/to/repo
  tool_policy:
    tugboat_status: allow
    tugboat_instruction_graph: allow
    tugboat_record_episode: allow
    tugboat_request_audit: allow
    tugboat_request_proposal: allow
    tugboat_request_eval: allow
```

Use `allowed_repositories` and `tool_policy` to keep MCP access scoped to reviewed repos and approved tools. Omit or set `deny` for any write-intent tool that should not create `.sidecar/mcp` artifacts or enqueue daemon work.

## Auto-Apply Disabled

```yaml
auto_apply:
  enabled: false
  max_changed_lines: 20
```

Auto-apply remains disabled unless a separate burn-in, confirmation, VCS, and rollback policy is satisfied.
