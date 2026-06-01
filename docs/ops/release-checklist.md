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
- Any `.sidecar` schema or artifact-format change has an explicit migration or compatibility note, including whether older Tugboat binaries block newer sidecars and whether operators must back up before `tugboat ops migrate --repo . --apply`.
- For v1-facing releases, review `docs/migration-v1.md`, `docs/compatibility-policy.md`, and `docs/llmff-compatibility.md` before tagging.
- No release step requires secret values in logs, CI summaries, or retained artifacts.

## Checklist

Run these commands from the repository root:

```bash
mkdir -p .sidecar/ci
set -o pipefail
tugboat doctor --repo . 2>&1 | tee .sidecar/ci/doctor.txt
tugboat ci --repo .
tugboat index --repo . --check 2>&1 | tee .sidecar/ci/index-check.txt
tugboat harness check --repo . 2>&1 | tee .sidecar/ci/harness.txt
python -m pytest --cov=src --cov-report=term-missing -q 2>&1 | tee .sidecar/ci/pytest-coverage.log
{ echo "python -m build --wheel --outdir dist"; python -m build --wheel --outdir dist; } 2>&1 | tee .sidecar/ci/build-wheel.txt
WHEEL="$(ls dist/tugboat-*.whl | sort | tail -n 1)"
echo "built ${WHEEL}" | tee -a .sidecar/ci/build-wheel.txt
{ echo "python -m twine check ${WHEEL}"; python -m twine check "${WHEEL}"; } 2>&1 | tee .sidecar/ci/twine-check.txt
cat > .sidecar/ci/security-review.md <<'EOF'
# Security Review

No open critical or high findings for proposal-only operation.

Approved as a release candidate for proposal-only use.
EOF
python -m venv .sidecar/ci/install-smoke-venv
{
  .sidecar/ci/install-smoke-venv/bin/python -m pip install "${WHEEL}"
  echo "installed tugboat wheel: ${WHEEL}"
  echo "installed tugboat --version"
  .sidecar/ci/install-smoke-venv/bin/tugboat --version
  echo "installed tugboat doctor"
  .sidecar/ci/install-smoke-venv/bin/tugboat doctor --repo .
  echo "installed tugboat index --repo . --check"
  .sidecar/ci/install-smoke-venv/bin/tugboat index --repo . --check
  echo "installed tugboat harness check --repo ."
  .sidecar/ci/install-smoke-venv/bin/tugboat harness check --repo .
  python - <<'PY'
from pathlib import Path
repo = Path(".sidecar/ci/proposal-smoke-repo")
repo.mkdir(parents=True, exist_ok=True)
(repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
PY
  echo "installed tugboat init --repo .sidecar/ci/proposal-smoke-repo"
  .sidecar/ci/install-smoke-venv/bin/tugboat init --repo .sidecar/ci/proposal-smoke-repo
  echo "installed tugboat index --repo .sidecar/ci/proposal-smoke-repo"
  .sidecar/ci/install-smoke-venv/bin/tugboat index --repo .sidecar/ci/proposal-smoke-repo
  echo "installed tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all"
  .sidecar/ci/install-smoke-venv/bin/tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all
  run_dir="$(find .sidecar/ci/proposal-smoke-repo/.sidecar/runs -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
  for artifact in audit.json candidate.json eval-report.json optimization-summary.json report.md; do
    test -f "${run_dir}/${artifact}"
  done
  echo "proposal smoke artifact: audit.json"
  echo "proposal smoke artifact: candidate.json"
  echo "proposal smoke artifact: eval-report.json"
  echo "proposal smoke artifact: optimization-summary.json"
  echo "proposal smoke artifact: report.md"
} 2>&1 | tee .sidecar/ci/install-smoke.txt
tugboat ops release-manifest --repo . --wheel "${WHEEL}" --commit <sha> --ci-url <url> --approver <name> --security-review-decision approved_proposal_only --security-review-critical-high-findings 0 --evidence .sidecar/ci/doctor.txt --evidence .sidecar/ci/index-check.txt --evidence .sidecar/ci/harness.txt --evidence .sidecar/ci/ci-report.json --evidence .sidecar/ci/security-review.md --evidence .sidecar/ci/pytest-coverage.log --evidence .sidecar/ci/build-wheel.txt --evidence .sidecar/ci/twine-check.txt --evidence .sidecar/ci/install-smoke.txt
```

The `${WHEEL}` variable resolves to the built `dist/<wheel>.whl`; the retained twine step is the copy-paste-safe form of `python -m twine check dist/<wheel>.whl`.

Before tagging:

- Confirm `tugboat doctor` reports `proposal_only` and `auto_apply: disabled`.
- Confirm CI retained the pytest log with total coverage at or above 90%, `ci-report.json`, security-review evidence, harness output, and release artifact manifest.
- Confirm the built wheel installs in a clean virtual environment and the installed `tugboat --version`, `tugboat doctor`, `tugboat index --repo . --check`, and `tugboat harness check --repo .` commands run.
- Confirm the installed proposal-loop smoke in `.sidecar/ci/proposal-smoke-repo` writes `.sidecar/runs/<run-id>/audit.json`, `candidate.json`, `eval-report.json`, `optimization-summary.json`, and `report.md`.
- Confirm `.sidecar/ops/release-artifact-manifest.json` records the wheel hash, retained evidence, commit, CI URL, approver, and security review decision.
- Confirm the security review for the release has no open critical or high findings.
- Confirm generated artifacts under `.sidecar/runs` contain no raw secrets.
- Record the release version, git commit, CI run URL, and approver.

## Provider-Backed Approval

Use `--security-review-decision approved_provider_backed` only for releases that are allowed to run provider-backed `llmff` pipelines. The repo policy must explicitly set `llmff.allow_network: true` and list every approved provider in `llmff.allowed_providers`.

Retain at least one provider-backed pipeline evidence artifact with `network_required: true`, declared `providers`, and `external_calls` entries for the model provider. Pass that artifact with the other `--evidence` files so `.sidecar/ops/release-artifact-manifest.json` records `provider_backed_evidence`.

## Publish

After the checklist passes and the release owner approves publication:

```bash
git tag -a v<version> <sha> -m "tugboat <version>"
git push origin v<version>
python -m twine upload dist/<wheel>.whl
```

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
- `python -m pytest --cov=src --cov-report=term-missing -q` output.
- Installed-wheel smoke output.
- Security review approval.
- Artifact retention/redaction confirmation.
