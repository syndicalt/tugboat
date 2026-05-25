# Tugboat Quickstart

## Install

Install the package in the repository environment, then verify the CLI:

```bash
tugboat doctor
```

The default posture should be proposal-only with auto-apply disabled.

## Initialize

Index the current instruction corpus:

```bash
tugboat index --repo .
```

For CI or dry-run adoption, use:

```bash
tugboat index --repo . --check
```

## Proposal Loop

Run the first local proposal loop against a saved trace bundle:

```bash
tugboat audit --repo . --trace traces/example.jsonl --mock-llmff-inspect
tugboat propose --repo . --audit latest
tugboat eval --repo . --candidate latest --suite all
tugboat report --repo . --run latest
```

This is designed to work under 15 minutes for an existing repo and does not require provider credentials in proposal-only mode.

## Next Checks

Before applying any generated diff, run:

```bash
tugboat harness check --repo .
python -m pytest -q
```

Review `.sidecar/runs/<run-id>/candidate.diff`, `eval-report.json`, and `report.md`.
