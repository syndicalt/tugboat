from pathlib import Path
import json
import os
import sqlite3
import time
from contextlib import closing

from tugboat.cli import main
from tugboat.harness.checks import (
    check_harness_legibility,
    generate_cleanup_candidates,
    generate_harness_report,
)


def test_harness_legibility_passes_short_instruction_maps_with_local_refs(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is True
    assert result.findings == []


def test_harness_legibility_flags_monolithic_instruction_files(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text("\n".join(f"line {n}" for n in range(5)), encoding="utf-8")

    result = check_harness_legibility(repo, max_instruction_lines=3)

    assert result.passed is False
    assert result.findings == [
        "AGENTS.md has 5 instruction lines; keep it at or below 3 and move detail into repo-local markdown references."
    ]


def test_harness_legibility_flags_broken_repo_local_markdown_links(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing runbook](docs/missing.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "CODEX.md references missing repo-local markdown file docs/missing.md."
    ]


def test_harness_legibility_flags_instruction_files_without_local_markdown_refs(tmp_path: Path):
    repo = tmp_path
    (repo / "SKILL.md").write_text(
        "# Skill\n\nUse pytest for verification.\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "SKILL.md has no repo-local markdown references; keep instruction files as short maps to deeper docs."
    ]


def test_harness_legibility_flags_missing_ownership_and_verification_metadata(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "docs/runbook.md is missing ownership metadata.",
        "docs/runbook.md is missing verification-status metadata.",
    ]


def test_harness_legibility_flags_broken_repo_local_links_inside_referenced_docs(
    tmp_path: Path,
):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: verified\n"
        "---\n"
        "# Runbook\n\n"
        "See [deep dive](deep/missing.md).\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)
    report = generate_harness_report(repo)

    expected = "docs/runbook.md references missing repo-local markdown file deep/missing.md."
    assert result.passed is False
    assert result.findings == [expected]
    assert report.stale_docs == [expected]
    assert report.doc_gardening_tasks == [
        "Add or fix deep/missing.md referenced by docs/runbook.md."
    ]
    assert [candidate.to_json_dict() for candidate in generate_cleanup_candidates(repo)] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Add or fix deep/missing.md referenced by docs/runbook.md.",
            "source_findings": [expected],
            "required_eval_suites": ["structural"],
        }
    ]


def test_harness_legibility_flags_docs_older_than_declared_source_files(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    source = repo / "src"
    docs.mkdir()
    source.mkdir()
    source_file = source / "service.py"
    doc = docs / "service.md"
    source_file.write_text("def run():\n    return 'old'\n", encoding="utf-8")
    doc.write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: verified\n"
        "source_files: src/service.py\n"
        "---\n"
        "# Service\n",
        encoding="utf-8",
    )
    old_time = time.time() - 20
    new_time = time.time()
    os.utime(doc, (old_time, old_time))
    os.utime(source_file, (new_time, new_time))
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [service](docs/service.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)
    report = generate_harness_report(repo)

    expected = "docs/service.md is older than source file src/service.py."
    assert result.passed is False
    assert result.findings == [expected]
    assert report.stale_docs == [expected]
    assert report.doc_gardening_tasks == ["Refresh docs/service.md from src/service.py."]
    assert [candidate.to_json_dict() for candidate in generate_cleanup_candidates(repo)] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Refresh docs/service.md from src/service.py.",
            "source_findings": [expected],
            "required_eval_suites": ["structural"],
        }
    ]


def test_harness_legibility_flags_duplicate_conflicting_rules_and_too_many_musts(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n\n"
        "- MUST run tests.\n"
        "- MUST run tests.\n"
        "- MUST deploy manually.\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [runbook](docs/runbook.md).\n\n"
        "- NEVER deploy manually.\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo, max_must_count=2)

    assert result.passed is False
    assert result.findings == [
        "AGENTS.md has 3 MUST-level rules; keep MUST density at or below 2.",
        "Duplicate instruction rule appears 2 times: run tests.",
        "Conflicting instruction rules: MUST deploy manually. vs NEVER deploy manually.",
    ]


def test_generate_harness_report_builds_knowledge_map_and_cleanup_tasks(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (docs / "orphan.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Orphan\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\nSee [missing](docs/missing.md).\n",
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    assert report.knowledge_map == {
        "AGENTS.md": ["docs/missing.md", "docs/runbook.md"],
    }
    assert report.missing_docs == ["docs/missing.md"]
    assert report.stale_docs == [
        "docs/runbook.md is missing ownership metadata.",
        "docs/runbook.md is missing verification-status metadata.",
    ]
    assert report.orphaned_runbooks == ["docs/orphan.md"]
    assert report.doc_gardening_tasks == [
        "Add or fix docs/missing.md referenced by AGENTS.md.",
        "Add ownership metadata to docs/runbook.md.",
        "Add verification-status metadata to docs/runbook.md.",
        "Either reference docs/orphan.md from an instruction map or remove/archive it.",
    ]


def test_generate_harness_report_flags_recurring_failures_without_docs(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "recurring-failures.json").write_text(
        """
{
  "schema_version": 1,
  "failures": [
    {"failure_id": "final-answer-evidence", "summary": "Final answers omit evidence."},
    {"failure_id": "covered", "summary": "Covered behavior.", "doc_ref": "docs/runbook.md"},
    {"failure_id": "stale-doc", "summary": "Linked doc is missing.", "doc_ref": "docs/missing.md"}
  ]
}
""".lstrip(),
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    assert report.recurring_failures_without_docs == [
        "final-answer-evidence: Final answers omit evidence.",
        "stale-doc: Linked doc is missing.",
    ]
    assert report.doc_gardening_tasks == [
        "Document recurring failure final-answer-evidence: Final answers omit evidence.",
        "Document recurring failure stale-doc: Linked doc is missing.",
    ]


def test_harness_report_persists_recurring_failures_without_docs(tmp_path: Path, capsys):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "recurring-failures.json").write_text(
        """
{
  "schema_version": 1,
  "failures": [
    {"failure_id": "approval-boundary", "summary": "Approval boundary corrections repeated."}
  ]
}
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["harness", "report", "--repo", str(repo)]) == 0

    output = capsys.readouterr().out
    payload_path = repo / ".sidecar" / "harness-report.json"
    assert "## Recurring Failures Without Docs" in output
    assert "- approval-boundary: Approval boundary corrections repeated." in output
    assert json.loads(payload_path.read_text(encoding="utf-8"))[
        "recurring_failures_without_docs"
    ] == ["approval-boundary: Approval boundary corrections repeated."]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            """
            SELECT severity, audit_event_sequence
            FROM harness_findings
            WHERE finding = 'approval-boundary: Approval boundary corrections repeated.'
            """
        ).fetchone()

    assert row[0] == "recurring_failure_without_doc"
    assert row[1] is not None


def test_harness_report_persists_findings_and_doc_gardening_run(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\nSee [missing](docs/missing.md).\n",
        encoding="utf-8",
    )

    assert main(["harness", "report", "--repo", str(repo)]) == 0

    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        findings = connection.execute(
            """
            SELECT finding, severity, audit_event_sequence
            FROM harness_findings
            ORDER BY finding
            """
        ).fetchall()
        gardening_run = connection.execute(
            """
            SELECT repo_path, status, report_path, audit_event_sequence
            FROM doc_gardening_runs
            """
        ).fetchone()

    assert findings == [
        ("Add or fix docs/missing.md referenced by AGENTS.md.", "task", findings[0][2]),
        ("Add ownership metadata to docs/runbook.md.", "task", findings[1][2]),
        ("Add verification-status metadata to docs/runbook.md.", "task", findings[2][2]),
        ("docs/missing.md", "missing_doc", findings[3][2]),
        ("docs/runbook.md is missing ownership metadata.", "stale_doc", findings[4][2]),
        (
            "docs/runbook.md is missing verification-status metadata.",
            "stale_doc",
            findings[5][2],
        ),
    ]
    assert all(row[2] is not None for row in findings)
    assert gardening_run == (
        str(repo),
        "completed",
        str(repo / ".sidecar" / "harness-report.json"),
        gardening_run[3],
    )
    assert gardening_run[3] is not None
    assert (repo / ".sidecar" / "harness-report.json").exists()


def test_generate_cleanup_candidates_are_review_only_and_tied_to_findings(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    candidates = generate_cleanup_candidates(repo)

    assert [candidate.to_json_dict() for candidate in candidates] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Add ownership metadata to docs/runbook.md.",
            "source_findings": ["docs/runbook.md is missing ownership metadata."],
            "required_eval_suites": ["structural"],
        },
        {
            "candidate_id": "harness-cleanup-2",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Add verification-status metadata to docs/runbook.md.",
            "source_findings": ["docs/runbook.md is missing verification-status metadata."],
            "required_eval_suites": ["structural"],
        },
    ]


def test_cleanup_candidates_include_recurring_failure_doc_tasks(tmp_path: Path):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "recurring-failures.json").write_text(
        """
{
  "schema_version": 1,
  "failures": [
    {"failure_id": "approval-boundary", "summary": "Approval boundary corrections repeated."}
  ]
}
""".lstrip(),
        encoding="utf-8",
    )

    candidates = generate_cleanup_candidates(repo)

    assert [candidate.to_json_dict() for candidate in candidates] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": (
                "Document recurring failure approval-boundary: "
                "Approval boundary corrections repeated."
            ),
            "source_findings": [
                "approval-boundary: Approval boundary corrections repeated.",
            ],
            "required_eval_suites": ["structural"],
        }
    ]


def test_harness_cleanup_cli_writes_review_only_candidate_bundle(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    assert main(["harness", "cleanup", "--repo", str(repo)]) == 0

    output = capsys.readouterr().out
    bundle_path = repo / ".sidecar" / "harness-cleanup-candidates.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert f"cleanup candidates: {bundle_path}" in output
    assert payload == {
        "schema_version": 1,
        "candidates": [
            {
                "candidate_id": "harness-cleanup-1",
                "risk_class": "review_required",
                "auto_apply": False,
                "task": "Add ownership metadata to docs/runbook.md.",
                "source_findings": ["docs/runbook.md is missing ownership metadata."],
                "required_eval_suites": ["structural"],
            },
            {
                "candidate_id": "harness-cleanup-2",
                "risk_class": "review_required",
                "auto_apply": False,
                "task": "Add verification-status metadata to docs/runbook.md.",
                "source_findings": [
                    "docs/runbook.md is missing verification-status metadata.",
                ],
                "required_eval_suites": ["structural"],
            },
        ],
    }
    assert not (repo / "CODEX.md").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM harness_findings
            WHERE severity = 'cleanup_candidate'
            """
        ).fetchone()[0] == 2
