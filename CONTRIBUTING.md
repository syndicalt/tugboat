# Contributing To Tugboat

## Development Setup

Use an editable local install for development:

```bash
python -m pip install -e ".[dev]"
tugboat doctor --repo .
```

Tugboat is a local-first sidecar for governed instruction maintenance. Keep changes aligned with `docs/roadmaps/v1.0.0-roadmap.md`, the documented CLI contracts, and the proposal-only by default posture.

## Production Bar

NO HACKS. Changes should be production-ready, test-backed, and scoped to the behavior being changed.

Preserve these defaults:

- proposal-only by default;
- `llmff` as the bounded pipeline runner;
- human review as the primary control;
- VCS-backed apply and rollback;
- narrow Class A auto-apply only when policy explicitly opts in;
- local-only daemon and read-first MCP behavior.

Avoid broad authority expansion, remote daemon behavior, provider credential management, or silent mutation paths.

## Testing

Run the full gate before opening a pull request:

```bash
tugboat harness check --repo .
python -m pytest --cov=src --cov-report=term-missing -q
```

For focused work, add targeted tests first and run the smallest relevant test module before the full gate.

## Pull Requests

Pull requests should include:

- the user-visible outcome;
- affected commands, files, or artifacts;
- tests run;
- docs updated;
- safety and rollback considerations;
- any compatibility impact.

Keep unrelated refactors out of feature and bugfix PRs.

## Security And Secrets

Do not commit provider credentials; do not commit provider credentials in trace payloads, `.sidecar` runtime databases, or private artifacts. If a test needs credential-like text, use synthetic fixtures that are clearly fake and covered by redaction expectations.

If you find a vulnerability, follow `SECURITY.md` instead of opening a public issue.
