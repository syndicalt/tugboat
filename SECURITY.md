# Security Policy

## Supported Versions

Security fixes target the current main branch and the latest published release line. Pre-1.0 releases may receive fixes through the next patch or minor release rather than long-term backports.

## Reporting A Vulnerability

Do not open a public issue for a vulnerability.

Report privately through GitHub Security Advisories when available, or contact the maintainer directly with a minimal, sanitized description. Include affected versions, the command or workflow involved, and safe reproduction steps.

## Sensitive Data

Do not include provider credentials. Reports must not include provider credentials, private traces, raw `.sidecar` databases, proprietary instruction files, or unredacted artifacts. In short, do not include provider credentials in any vulnerability report.

If evidence is necessary, share sanitized artifact refs, hashes, command output, or reduced fixtures. Remove API keys, OAuth tokens, session cookies, repository secrets, and customer data before sending.

## Security Model

Tugboat is local-first and proposal-only by default. It treats traces, model output, and provider-backed results as evidence, not authority.

Core controls include the read-only kill switch, VCS-gated mutation, rollback provenance, policy gates, secret scanning, local-only daemon sockets, read-first MCP behavior, and explicit opt-in for provider-backed `llmff` paths.

See `docs/threat-model.md` and `docs/ops/security-review.md` for the working threat model and release security review checklist.

## Response Expectations

The maintainer will acknowledge actionable private reports, assess severity, and coordinate a fix or mitigation. Public disclosure should wait until a fix, workaround, or explicit non-issue determination is available.
