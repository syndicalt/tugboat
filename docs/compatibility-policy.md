---
owner: platform
verification_status: draft
---

# Tugboat Compatibility Policy

## Purpose

This policy defines what Tugboat treats as stable for v1 and how breaking changes are introduced. It exists so teams can adopt Tugboat as a local sidecar without guessing which commands, artifacts, and safety controls are stable.

## Stable V1 Surface

The stable v1 surface includes:

- CLI commands documented in docs/cli-reference.md;
- documented `.sidecar` artifact schemas;
- policy defaults that keep Tugboat proposal-only by default;
- the read-only kill switch for write-path shutdown;
- VCS-backed apply and rollback behavior;
- fixture-backed `llmff` execution for credential-free local and CI checks;
- release evidence generated through `docs/ops/release-checklist.md`.

Stable does not mean frozen forever. It means changes follow semantic versioning, are documented, and preserve the safety model unless the release notes explicitly say otherwise.

## Experimental Surface

Experimental surfaces may change during 0.x and early v1 hardening:

- provider-backed `llmff` manifests;
- new MCP tools that are not listed as stable in docs;
- daemon scheduling policy beyond documented local queue behavior;
- new cleanup proposal operators;
- new observability metrics.

Experimental surfaces must still preserve local-first operation, proposal-only by default, secret scanning, and the read-only kill switch.

## Deprecation Policy

For v1, a planned breaking CLI or artifact change should emit a deprecation warning for at least one minor release before removal when practical.

Deprecation notices should identify:

- the command, option, field, or artifact being changed;
- the replacement path;
- the earliest removal version;
- whether migration is automatic, manual, or unsupported.

Security fixes may bypass the one minor release window when keeping compatibility would preserve unsafe behavior.

## Artifact Compatibility

Artifact schemas are part of the operator contract. Backward-compatible additions are preferred. Required-field removals, semantic changes, and renamed fields require release notes and either a reader fallback or a sidecar migration.

Sidecar migrations write `sidecar-migration-report.json` evidence. Operators should use `tugboat ops migrate --repo .` before `tugboat ops migrate --repo . --apply`, and should back up `.sidecar` before applying migrations.

If `.sidecar/version.json` is newer than supported, Tugboat blocks instead of attempting a downgrade.

## llmff Compatibility

The fixture backend remains supported for local development, CI, and credential-free proposal loops.

Provider-backed execution is explicitly policy-gated. Repos must opt in with `llmff.allow_network: true`, reviewed provider configuration, and approved manifests or manifest hashes. Tugboat does not treat provider output as authority; it remains evidence that must pass policy, eval, and review gates.

## Known Limitations

- No hosted web UI.
- No public network daemon.
- No general agent runtime.
- No plugin marketplace.
- No multi-user team server mode.
- No MCP path that directly applies changes.
- No broad auto-apply outside narrow Class A policy lanes.
