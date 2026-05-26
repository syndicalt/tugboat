---
owner: platform
verification_status: verified
---

# Tugboat Release Checklist

## Purpose

Use this checklist before publishing a Tugboat release or promoting a build for team use. The default release posture is proposal-only: `proposal_only` mode, `auto_apply: disabled`, and no required live provider credentials.

## Preconditions

- The release branch is clean except for intended release changes.
- The release notes identify user-visible changes, policy changes, storage changes, and rollback notes.
- Any `.sidecar` schema or artifact-format change has an explicit migration or compatibility note.
- No release step requires secret values in logs, CI summaries, or retained artifacts.

## Checklist

Run these commands from the repository root:

```bash
tugboat doctor
tugboat index --repo . --check
tugboat harness check --repo .
python -m pytest -q
tugboat ops release-manifest --repo . --wheel dist/<wheel>.whl --commit <sha> --ci-url <url> --approver <name>
```

Before tagging:

- Confirm `tugboat doctor` reports `proposal_only` and `auto_apply: disabled`.
- Confirm CI retained the pytest log, harness output, and release artifact manifest.
- Confirm `.sidecar/ops/release-artifact-manifest.json` records the wheel hash, retained evidence, commit, CI URL, and approver.
- Confirm the security review for the release has no open critical or high findings.
- Confirm generated artifacts under `.sidecar/runs` contain no raw secrets.
- Record the release version, git commit, CI run URL, and approver.

## Rollback

Rollback means returning users to the prior package and keeping the current `.sidecar` directory readable:

```bash
git tag --delete <bad-tag>
python -m pip install tugboat==<previous-version>
tugboat status --repo .
```

If the release changed `.sidecar` data, restore from the backup taken before upgrade and run the recovery verification in `docs/ops/sidecar-backup-restore.md`.

## Evidence to Retain

Retain these release records for at least one year:

- Release checklist result.
- CI run URL and logs.
- `tugboat doctor` output.
- `tugboat harness check --repo .` output.
- `python -m pytest -q` output.
- Security review approval.
- Artifact retention/redaction confirmation.
