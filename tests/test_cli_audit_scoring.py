from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main


def test_mock_audit_uses_scoring_outcomes_for_failed_tests(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nRun tests before final.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"test_result","suite":"unit","passed":false,"status":"failed"}\n',
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace), "--mock-llmff-inspect"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "agent_ignored_instruction"
    assert audit["severity"] == "high"
    assert audit["confidence"] == 0.85
    assert audit["scoring"] == [
        {
            "plugin": "tests",
            "label": "failed-tests",
            "metrics": {"failed_tests": 1},
            "evidence": audit["evidence_refs"],
        }
    ]

