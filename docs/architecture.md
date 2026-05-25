# Tugboat Architecture

## Boundary

Tugboat is a local-first sidecar for agent instruction and harness artifacts. The CLI remains the source of truth. MCP and daemon entry points are adapters over the same service layer and must not bypass policy, storage, or VCS adapter controls.

`llmff` is the bounded pipeline runner. Tugboat owns policy, instruction precedence, audit state, approval, rollback, and patch authority.

## Components

- CLI commands own explicit user workflows.
- Corpus indexing parses instruction files and builds instruction graphs.
- Policy gates classify candidate edits before review or apply.
- The VCS adapter is the only path that may write instruction files.
- MCP exposes read tools and write-intent request tools.
- The daemon queues local jobs and reports state without public exposure.

## Data Flow

Trace bundles enter through files, MCP write-intent artifacts, or daemon jobs. Tugboat snapshots instruction files, records local metadata in SQLite, runs `llmff` manifests, writes artifacts under `.sidecar/runs`, evaluates candidates, and produces review bundles.

## Authority Model

Proposal generation has no write authority over protected files. Apply and rollback require the VCS adapter, base hashes, target-file cleanliness, audit events, and human-review metadata for review-required classes.
