from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _init_release_repo(repo: Path) -> str:
    _git(repo, "init")
    _git(repo, "config", "user.email", "tugboat@example.test")
    _git(repo, "config", "user.name", "Tugboat Tests")
    (repo / "README.md").write_text("# Release repo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _write_release_evidence(repo: Path) -> dict[str, Path]:
    evidence_dir = repo / ".sidecar" / "ci"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "doctor": evidence_dir / "doctor.txt",
        "index": evidence_dir / "index-check.txt",
        "harness": evidence_dir / "harness.txt",
        "coverage": evidence_dir / "pytest-coverage.log",
        "build": evidence_dir / "build-wheel.txt",
        "twine": evidence_dir / "twine-check.txt",
        "install": evidence_dir / "install-smoke.txt",
    }
    evidence["doctor"].write_text("tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n", encoding="utf-8")
    evidence["index"].write_text("index: ok\n", encoding="utf-8")
    evidence["harness"].write_text("harness: ok\n", encoding="utf-8")
    evidence["coverage"].write_text("633 passed\n", encoding="utf-8")
    evidence["build"].write_text("built dist/tugboat-0.1.0-py3-none-any.whl\n", encoding="utf-8")
    evidence["twine"].write_text("PASSED dist/tugboat-0.1.0-py3-none-any.whl\n", encoding="utf-8")
    evidence["install"].write_text(
        "tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n",
        encoding="utf-8",
    )
    return evidence


def _release_manifest_args(
    *,
    repo: Path,
    wheel: Path,
    commit: str,
    evidence_paths: list[Path],
    ci_url: str = "https://ci.example/runs/1",
    security_review_decision: str = "approved_proposal_only",
    security_review_critical_high_findings: str = "0",
) -> list[str]:
    args = [
        "ops",
        "release-manifest",
        "--repo",
        str(repo),
        "--wheel",
        str(wheel),
        "--commit",
        commit,
        "--ci-url",
        ci_url,
        "--approver",
        "release-owner",
        "--security-review-decision",
        security_review_decision,
        "--security-review-critical-high-findings",
        security_review_critical_high_findings,
    ]
    for evidence_path in evidence_paths:
        args.extend(["--evidence", str(evidence_path)])
    return args


def test_ops_release_manifest_records_release_artifacts_and_audits_hash(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    evidence = _write_release_evidence(repo)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    current_head = _init_release_repo(repo)

    previous_umask = os.umask(0o022)
    try:
        assert (
            main(
                _release_manifest_args(
                    repo=repo,
                    wheel=wheel,
                    commit=current_head,
                    evidence_paths=list(evidence.values()),
                )
            )
            == 0
        )
    finally:
        os.umask(previous_umask)

    output_path = sidecar_dir(repo) / "ops" / "release-artifact-manifest.json"
    assert f"release manifest: {output_path}" in capsys.readouterr().out
    assert output_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "artifact_kind": "release_artifact_manifest",
        "package": {"name": "tugboat", "version": "0.1.0"},
        "commit": current_head,
        "ci_url": "https://ci.example/runs/1",
        "approver": "release-owner",
        "security_review": {
            "decision": "approved_proposal_only",
            "critical_high_findings": 0,
        },
        "wheel": {
            "path": str(wheel.resolve()),
            "sha256": hashlib.sha256(b"wheel-bytes").hexdigest(),
            "size_bytes": len(b"wheel-bytes"),
        },
        "smoke_commands": [
            "tugboat doctor",
            "tugboat index --repo . --check",
            "tugboat harness check --repo .",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "python -m build --wheel",
            "python -m twine check dist/<wheel>.whl",
            "clean venv install from built wheel",
            "installed tugboat doctor",
        ],
        "retained_evidence": [
            {
                "path": str(evidence["doctor"].resolve()),
                "sha256": hashlib.sha256(
                    b"tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n"
                ).hexdigest(),
                "size_bytes": len(b"tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n"),
            },
            {
                "path": str(evidence["index"].resolve()),
                "sha256": hashlib.sha256(b"index: ok\n").hexdigest(),
                "size_bytes": len(b"index: ok\n"),
            },
            {
                "path": str(evidence["harness"].resolve()),
                "sha256": hashlib.sha256(b"harness: ok\n").hexdigest(),
                "size_bytes": len(b"harness: ok\n"),
            },
            {
                "path": str(evidence["coverage"].resolve()),
                "sha256": hashlib.sha256(b"633 passed\n").hexdigest(),
                "size_bytes": len(b"633 passed\n"),
            },
            {
                "path": str(evidence["build"].resolve()),
                "sha256": hashlib.sha256(
                    b"built dist/tugboat-0.1.0-py3-none-any.whl\n"
                ).hexdigest(),
                "size_bytes": len(b"built dist/tugboat-0.1.0-py3-none-any.whl\n"),
            },
            {
                "path": str(evidence["twine"].resolve()),
                "sha256": hashlib.sha256(
                    b"PASSED dist/tugboat-0.1.0-py3-none-any.whl\n"
                ).hexdigest(),
                "size_bytes": len(b"PASSED dist/tugboat-0.1.0-py3-none-any.whl\n"),
            },
            {
                "path": str(evidence["install"].resolve()),
                "sha256": hashlib.sha256(
                    b"tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n"
                ).hexdigest(),
                "size_bytes": len(b"tugboat: ok\nmode: proposal_only\nauto_apply: disabled\n"),
            },
        ],
    }

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        event = store.connection.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "release.manifest_written"
    audit_payload = json.loads(event[1])
    assert audit_payload["artifact"] == ".sidecar/ops/release-artifact-manifest.json"
    assert audit_payload["artifact_sha256"] == hashlib.sha256(
        output_path.read_bytes()
    ).hexdigest()
    assert audit_payload["commit"] == current_head
    assert audit_payload["security_review"] == {
        "decision": "approved_proposal_only",
        "critical_high_findings": 0,
    }


def test_ops_release_manifest_rejects_commit_that_is_not_current_head(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    current_head = _init_release_repo(repo)
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    evidence = _write_release_evidence(repo)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            _release_manifest_args(
                repo=repo,
                wheel=wheel,
                commit="0" * 40,
                evidence_paths=list(evidence.values()),
            )
        )
        == 1
    )

    assert (
        f"release manifest blocked: commit does not match current HEAD: {current_head}"
        in capsys.readouterr().out
    )
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_requires_retained_evidence_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(wheel),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "approved_proposal_only",
                "--security-review-critical-high-findings",
                "0",
            ]
        )
        == 1
    )

    assert "release manifest blocked: retained evidence is required" in capsys.readouterr().out
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_requires_coverage_evidence_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    harness_output = repo / ".sidecar" / "ci" / "harness.txt"
    harness_output.parent.mkdir(parents=True)
    harness_output.write_text("harness: ok\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(wheel),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "approved_proposal_only",
                "--security-review-critical-high-findings",
                "0",
                "--evidence",
                str(harness_output),
            ]
        )
        == 1
    )

    assert "release manifest blocked: pytest coverage evidence is required" in capsys.readouterr().out
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_requires_full_checklist_evidence_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    current_head = _init_release_repo(repo)
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    evidence = _write_release_evidence(repo)
    evidence["twine"].unlink()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            _release_manifest_args(
                repo=repo,
                wheel=wheel,
                commit=current_head,
                evidence_paths=[path for path in evidence.values() if path.exists()],
            )
        )
        == 1
    )

    assert "release manifest blocked: twine check evidence is required" in capsys.readouterr().out
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_blocks_open_critical_or_high_security_findings(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(wheel),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "approved_proposal_only",
                "--security-review-critical-high-findings",
                "1",
            ]
        )
        == 1
    )

    assert (
        "release manifest blocked: security review has open critical/high findings"
        in capsys.readouterr().out
    )
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_blocks_unapproved_security_review_decision(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(wheel),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "rejected",
                "--security-review-critical-high-findings",
                "0",
            ]
        )
        == 1
    )

    assert (
        "release manifest blocked: security review decision is not approved"
        in capsys.readouterr().out
    )
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_blocks_missing_wheel_without_writing(tmp_path: Path, capsys) -> None:
    repo = tmp_path
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(repo / "dist" / "missing.whl"),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "approved_proposal_only",
                "--security-review-critical-high-findings",
                "0",
            ]
        )
        == 1
    )

    assert "release manifest blocked: wheel does not exist" in capsys.readouterr().out
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_blocks_secret_bearing_evidence_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    evidence = repo / ".sidecar" / "ci" / "pytest.log"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("provider token sk-thissecretkeyvalue1234567890\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ops",
                "release-manifest",
                "--repo",
                str(repo),
                "--wheel",
                str(wheel),
                "--commit",
                "abc1234",
                "--ci-url",
                "https://ci.example/runs/1",
                "--approver",
                "release-owner",
                "--security-review-decision",
                "approved_proposal_only",
                "--security-review-critical-high-findings",
                "0",
                "--evidence",
                str(evidence),
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "release manifest blocked: retained evidence contains secret" in output
    assert "sk-thissecret" not in output
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()


def test_ops_release_manifest_blocks_secret_bearing_payload_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    evidence = _write_release_evidence(repo)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"tugboat\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )

    assert (
        main(
            _release_manifest_args(
                repo=repo,
                wheel=wheel,
                commit="abc1234",
                ci_url="https://ci.example/runs/1?token=sk-thissecretkeyvalue1234567890",
                evidence_paths=list(evidence.values()),
            )
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "release manifest blocked: secret scan failed" in output
    assert "sk-thissecret" not in output
    assert not (sidecar_dir(repo) / "ops" / "release-artifact-manifest.json").exists()
