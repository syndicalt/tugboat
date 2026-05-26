from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


def test_ops_release_manifest_records_release_artifacts_and_audits_hash(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    wheel = repo / "dist" / "tugboat-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel-bytes")
    pytest_log = repo / ".sidecar" / "ci" / "pytest.log"
    harness_output = repo / ".sidecar" / "ci" / "harness.txt"
    pytest_log.parent.mkdir(parents=True)
    pytest_log.write_text("633 passed\n", encoding="utf-8")
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
                str(pytest_log),
                "--evidence",
                str(harness_output),
            ]
        )
        == 0
    )

    output_path = sidecar_dir(repo) / "ops" / "release-artifact-manifest.json"
    assert f"release manifest: {output_path}" in capsys.readouterr().out
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "artifact_kind": "release_artifact_manifest",
        "package": {"name": "tugboat", "version": "0.1.0"},
        "commit": "abc1234",
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
            "python -m pytest -q",
        ],
        "retained_evidence": [
            {
                "path": str(pytest_log.resolve()),
                "sha256": hashlib.sha256(b"633 passed\n").hexdigest(),
                "size_bytes": len(b"633 passed\n"),
            },
            {
                "path": str(harness_output.resolve()),
                "sha256": hashlib.sha256(b"harness: ok\n").hexdigest(),
                "size_bytes": len(b"harness: ok\n"),
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
    assert audit_payload["commit"] == "abc1234"
    assert audit_payload["security_review"] == {
        "decision": "approved_proposal_only",
        "critical_high_findings": 0,
    }


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
