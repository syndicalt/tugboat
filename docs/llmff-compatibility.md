---
owner: platform
verification_status: draft
---

# llmff Compatibility Matrix

## Purpose

This matrix documents the `llmff` runner and manifest contract surface Tugboat supports for v1. Tugboat uses `llmff` as the bounded pipeline runner; Tugboat policy, artifact validation, eval gates, VCS checks, and human review remain the authority layer.

## Supported Runners

| Runner | v1 support | Network | Notes |
| --- | --- | --- | --- |
| `tugboat-fixture-llmff` fixture backend | Supported | No | Default local and CI path for credential-free proposal loops. |
| Reviewed local `llmff` binary compatible with Tugboat manifests | Supported with policy review | Policy dependent | Must satisfy manifest inspect/run contracts and artifact schemas. |
| Provider-backed `llmff` runner | Experimental opt-in | Yes | Requires `llmff.allow_network: true`, approved providers, and reviewed or pinned manifests. |

The fixture backend remains the baseline compatibility target for local development, CI, docs examples, and proposal-only adoption.

## Bundled Manifest Contracts

Tugboat ships and validates these manifest templates:

| Manifest | Purpose | Required output artifacts |
| --- | --- | --- |
| `instruction-index.yaml` | Build the instruction index | `instruction-index.raw.json` |
| `episode-audit.yaml` | Score an episode against instructions | `audit.raw.json`, `evidence-ids.raw.json` |
| `drift-detect.yaml` | Detect recurring instruction drift | `drift.raw.json`, `optimizer-notes.raw.json` |
| `patch-propose.yaml` | Propose bounded edits | `candidate.raw.json`, `proposal-rationale.raw.json` |
| `patch-eval.yaml` | Evaluate candidate patches | `eval-report.raw.json`, `policy-decision.raw.json` |
| `acceptance-summary.yaml` | Summarize candidate disposition | `acceptance-summary.raw.json` |

`tugboat ci --repo .` materializes the manifest registry and performs manifest contract validation offline. Contract validation checks required manifest presence, name/file consistency, declared inputs, declared outputs, and known JSON artifact schema names.

## Provider-Backed Execution

Provider-backed execution is not enabled by environment variables alone.

Repositories must opt in through policy:

```yaml
llmff:
  allow_network: true
  allowed_providers:
    - openai
  allowed_manifest_hashes:
    - <reviewed-manifest-sha256>
```

Use `allowed_manifest_hashes` to pin reviewed manifests when using a non-fixture runner. Provider-backed release evidence must include network-required pipeline evidence and declared provider calls before a release can be approved as provider-backed.

## Version Policy

For v1, Tugboat supports the manifest contract shape bundled in the installed package. Compatible external `llmff` runners must:

- support inspect and run phases used by Tugboat;
- read and write file-backed inputs and outputs declared by the manifest;
- preserve declared artifact paths;
- report network requirements and provider calls during inspect;
- return nonzero exit codes for failed manifests without partially authorizing Tugboat changes.

Breaking manifest contract changes require release notes, compatibility docs updates, and either backward-compatible readers or migration guidance.

## Verification

Before relying on a runner or manifest set, run:

```bash
tugboat doctor --repo .
tugboat ci --repo .
tugboat optimize --repo . --trace traces/example.jsonl --suite all
```

For provider-backed paths, also retain release evidence through:

```bash
tugboat ops release-manifest --repo . --wheel dist/<wheel>.whl --commit <sha> --ci-url <url> --approver <name> --security-review-decision approved_provider_backed --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/ci-report.json --evidence .sidecar/ci/security-review.md --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt --evidence <provider-backed-evidence.json>
```
