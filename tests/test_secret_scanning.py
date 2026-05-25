from __future__ import annotations

from pathlib import Path

import pytest

from tugboat.security.secrets import SecretFinding, SecretScanError, scan_path, scan_text


def test_scan_text_reports_openai_style_api_key_without_value():
    findings = scan_text("trace.jsonl", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx")

    assert findings == (
        SecretFinding(
            path="trace.jsonl",
            line_number=1,
            kind="openai_api_key",
        ),
    )


def test_scan_path_rejects_nested_files_with_secrets(tmp_path: Path):
    snapshot = tmp_path / "instruction-snapshot"
    snapshot.mkdir()
    (snapshot / "CODEX.md").write_text("token = ghp_abcdefghijklmnopqrstuvwx\n", encoding="utf-8")

    with pytest.raises(SecretScanError, match="ghp_token"):
        scan_path(snapshot)


def test_scan_path_ignores_binary_files(tmp_path: Path):
    binary = tmp_path / "checkpoint.bin"
    binary.write_bytes(b"\xff\x00\xff")

    assert scan_path(binary) == ()
