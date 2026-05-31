---
owner: platform
verification_status: verified
---

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
llmff:
  binary: python -m tugboat.llmff.fixture_backend
  require_inspect: true
  allow_network: false
```

This fixture backend is local, credential-free, and intended for proposal-only adoption. Replace `llmff.binary` with a reviewed production runner only when enabling provider-backed pipelines.

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

## Pull Request Apply Mode

```yaml
vcs:
  pull_request:
    enabled: true
    provider: github_cli
    remote: origin
    base_branch: main
    draft: true
```

PR apply mode is fail-closed. `tugboat apply --mode pr` requires explicit policy, pushes the generated Tugboat branch to the configured remote, creates the pull request through the GitHub CLI, and records the PR result in the apply artifact and audit ledger.

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
  max_changed_lines: 50
  max_instruction_token_delta: 50
  minimum_burn_in_days: 14
  maximum_rejection_rate: 0.10
  maximum_rollback_rate: 0.02
  paused_repositories: []
  paused_lanes: []
  paused_categories: []
  pause_for_incident: false
  lanes:
    docs_hygiene:
      enabled: true
      allowed_categories:
        - broken_internal_link
        - duplicate_sentence_removal
        - formatting_normalization
        - stale_command_reference
        - typo_fix
      allowed_risk_classes:
        - A
      max_changed_lines: 50
      max_instruction_token_delta: 50
      minimum_burn_in_days: 3
      maximum_rejection_rate: 0.20
      maximum_rollback_rate: 0.05
    skill_improvement:
      enabled: true
      allowed_categories:
        - skill_improvement
      allowed_risk_classes:
        - A
      max_changed_lines: 30
      max_instruction_token_delta: 30
      minimum_burn_in_days: 7
      maximum_rejection_rate: 0.15
      maximum_rollback_rate: 0.03
```

Auto-apply remains disabled unless the global switch is enabled and a lane-specific burn-in, confirmation, VCS, eval, governance, token-growth, and rollback policy is satisfied. Token growth is measured from `eval-report.json` `metrics.instruction_token_delta`; missing or malformed token metrics block auto-apply.
Pause controls are also policy-owned: `paused_repositories`, `paused_lanes`, `paused_categories`, and `pause_for_incident` block candidates with distinct audit reasons and cannot be overridden by CLI threshold flags.
