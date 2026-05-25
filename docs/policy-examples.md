# Tugboat Policy Examples

## Proposal Only

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
  allowed_manifest_hashes:
    - replace-with-reviewed-manifest-hash
```

Provider-backed runs require explicit policy and reviewed manifest hashes.

## MCP Allowlist

```yaml
mcp:
  allowed_repositories:
    - /absolute/path/to/repo
  tool_policy:
    tugboat_status: allow
    tugboat_instruction_graph: allow
    tugboat_request_audit: allow
    tugboat_apply: deny
```

Use `allowed_repositories` and `tool_policy` to keep MCP access scoped to reviewed repos and approved tools.

## Auto-Apply Disabled

```yaml
auto_apply:
  enabled: false
  max_changed_lines: 20
```

Auto-apply remains disabled unless a separate burn-in, confirmation, VCS, and rollback policy is satisfied.
