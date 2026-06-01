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


def test_harness_legibility_flags_missing_repo_local_markdown_anchors(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing anchor](docs/runbook.md#missing-anchor).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "CODEX.md references missing repo-local markdown anchor docs/runbook.md#missing-anchor."
    ]


def test_harness_report_turns_missing_instruction_anchor_into_cleanup_task(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing anchor](docs/runbook.md#missing-anchor).\n",
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    expected = "CODEX.md references missing repo-local markdown anchor docs/runbook.md#missing-anchor."
    assert report.stale_docs == [expected]
    assert report.doc_gardening_tasks == [
        "Add or fix docs/runbook.md#missing-anchor referenced by CODEX.md."
    ]


def test_harness_legibility_ignores_headings_inside_fenced_code_for_anchors(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: verified\n"
        "---\n"
        "# Runbook\n\n"
        "```md\n"
        "# Missing Anchor\n"
        "```\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing anchor](docs/runbook.md#missing-anchor).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "CODEX.md references missing repo-local markdown anchor docs/runbook.md#missing-anchor."
    ]


def test_cleanup_candidate_for_missing_anchor_keeps_source_finding(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing anchor](docs/runbook.md#missing-anchor).\n",
        encoding="utf-8",
    )

    candidates = generate_cleanup_candidates(repo)

    assert [candidate.to_json_dict() for candidate in candidates] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Add or fix docs/runbook.md#missing-anchor referenced by CODEX.md.",
            "source_findings": [
                "CODEX.md references missing repo-local markdown anchor "
                "docs/runbook.md#missing-anchor.",
            ],
            "required_eval_suites": ["structural"],
        }
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


def test_harness_legibility_uses_policy_configured_instruction_files(tmp_path: Path):
    repo = tmp_path
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
instruction_files:
  - path: docs/harness/AGENT-RUNBOOK.md
    kind: repo_policy
    precedence: 80
    protected: true
""".lstrip(),
        encoding="utf-8",
    )
    harness_docs = repo / "docs" / "harness"
    harness_docs.mkdir(parents=True)
    (harness_docs / "AGENT-RUNBOOK.md").write_text(
        "# Harness Runbook\n\nThis configured instruction map has no repo-local docs.\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "docs/harness/AGENT-RUNBOOK.md has no repo-local markdown references; "
        "keep instruction files as short maps to deeper docs."
    ]


def test_harness_report_uses_policy_globbed_instruction_files(tmp_path: Path):
    repo = tmp_path
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
instruction_files:
  - path: .codex/skills/**/SKILL.md
    kind: skill
    precedence: 60
    protected: false
""".lstrip(),
        encoding="utf-8",
    )
    skill_dir = repo / ".codex" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Review Skill\n\nSee [missing procedure](docs/review.md).\n",
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    assert report.knowledge_map == {
        ".codex/skills/review/SKILL.md": ["docs/review.md"],
    }
    assert report.missing_docs == ["docs/review.md"]
    assert report.doc_gardening_tasks == [
        "Add or fix docs/review.md referenced by .codex/skills/review/SKILL.md."
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


def test_harness_legibility_flags_broken_repo_local_file_links_inside_referenced_docs(
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
        "Run [setup](../scripts/setup.sh) before adoption.\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)
    report = generate_harness_report(repo)

    expected = "docs/runbook.md references missing repo-local file ../scripts/setup.sh."
    assert result.passed is False
    assert result.findings == [expected]
    assert report.stale_docs == [expected]
    assert report.doc_gardening_tasks == [
        "Add or fix ../scripts/setup.sh referenced by docs/runbook.md."
    ]
    assert [candidate.to_json_dict() for candidate in generate_cleanup_candidates(repo)] == [
        {
            "candidate_id": "harness-cleanup-1",
            "risk_class": "review_required",
            "auto_apply": False,
            "task": "Add or fix ../scripts/setup.sh referenced by docs/runbook.md.",
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


def test_harness_legibility_flags_docs_older_than_yaml_list_source_files(
    tmp_path: Path,
):
    repo = tmp_path
    docs = repo / "docs"
    source = repo / "src"
    docs.mkdir()
    source.mkdir()
    source_file = source / "service.py"
    doc = docs / "service.md"
    source_file.write_text("def run():\n    return 'new'\n", encoding="utf-8")
    doc.write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: verified\n"
        "source_files:\n"
        "  - src/service.py\n"
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
    assert result.findings == [expected]
    assert report.doc_gardening_tasks == ["Refresh docs/service.md from src/service.py."]


def test_harness_legibility_accepts_hyphenated_yaml_list_source_files(
    tmp_path: Path,
):
    repo = tmp_path
    (repo / "docs").mkdir()
    (repo / "src").mkdir()
    source_file = repo / "src" / "service.py"
    doc = repo / "docs" / "service.md"
    source_file.write_text("changed = True\n", encoding="utf-8")
    doc.write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: verified\n"
        "source-files:\n"
        "  - src/service.py\n"
        "---\n"
        "# Service\n",
        encoding="utf-8",
    )
    os.utime(doc, (time.time() - 20, time.time() - 20))
    os.utime(source_file, None)
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [service](docs/service.md).\n",
        encoding="utf-8",
    )

    assert check_harness_legibility(repo).findings == [
        "docs/service.md is older than source file src/service.py."
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


def test_generate_harness_report_includes_token_efficiency_metrics(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n"
        "# Runbook\n\nUse tests before applying changes.\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\n"
        "MUST use tests before applying changes.\n"
        "MUST use tests before applying changes.\n"
        "See [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    assert report.token_metrics == {
        "active_context_estimated_tokens": 49,
        "active_context_files": [
            {"estimated_tokens": 29, "path": "AGENTS.md"},
            {"estimated_tokens": 20, "path": "docs/runbook.md"},
        ],
        "duplicate_rule_estimated_tokens": 6,
        "instruction_corpus_estimated_tokens": 29,
        "instruction_files": [
            {"estimated_tokens": 29, "line_count": 5, "path": "AGENTS.md"},
        ],
        "retrieval_pack_estimated_tokens": 49,
        "retrieval_pack_file_count": 2,
        "token_budget": {
            "active_context_estimated_tokens": 12000,
            "instruction_file_estimated_tokens": 4000,
            "retrieval_pack_estimated_tokens": 12000,
        },
        "token_budget_violations": [],
    }


def test_generate_harness_report_flags_large_instruction_token_budget(
    tmp_path: Path,
):
    repo = tmp_path
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\n" + "token " * 4001,
        encoding="utf-8",
    )

    report = generate_harness_report(repo)

    assert report.token_metrics["token_budget"] == {
        "active_context_estimated_tokens": 12000,
        "instruction_file_estimated_tokens": 4000,
        "retrieval_pack_estimated_tokens": 12000,
    }
    assert report.token_metrics["token_budget_violations"] == [
        "AGENTS.md estimated at 4004 tokens exceeds instruction file budget 4000."
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

    previous_umask = os.umask(0o022)
    try:
        assert main(["harness", "report", "--repo", str(repo)]) == 0
    finally:
        os.umask(previous_umask)

    output = capsys.readouterr().out
    payload_path = repo / ".sidecar" / "harness-report.json"
    assert payload_path.stat().st_mode & 0o777 == 0o600
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


def test_harness_report_blocks_secret_bearing_payload_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "recurring-failures.json").write_text(
        """
{
  "schema_version": 1,
  "failures": [
    {
      "failure_id": "approval-boundary",
      "summary": "Leaked token sk-thissecretkeyvalue1234567890"
    }
  ]
}
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["harness", "report", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "harness report invalid: secret scan failed" in output
    assert "sk-thissecret" not in output
    assert not (repo / ".sidecar" / "harness-report.json").exists()


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


def test_harness_report_cli_validates_payload_before_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InvalidHarnessReport:
        knowledge_map = {"AGENTS.md": ["docs/runbook.md"]}
        missing_docs: list[str] = []
        stale_docs = [123]
        orphaned_runbooks: list[str] = []
        recurring_failures_without_docs: list[str] = []
        doc_gardening_tasks: list[str] = []
        token_metrics = {
            "instruction_corpus_estimated_tokens": 0,
            "active_context_estimated_tokens": 0,
            "duplicate_rule_estimated_tokens": 0,
            "instruction_files": [],
            "active_context_files": [],
        }

    monkeypatch.setattr(
        "tugboat.cli.generate_harness_report",
        lambda repo: InvalidHarnessReport(),
    )

    assert main(["harness", "report", "--repo", str(tmp_path)]) == 1
    assert not (tmp_path / ".sidecar" / "harness-report.json").exists()


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
    original_agents = "# Agent Map\n\nSee [runbook](docs/runbook.md).\n"
    (repo / "AGENTS.md").write_text(
        original_agents,
        encoding="utf-8",
    )

    assert main(["harness", "cleanup", "--repo", str(repo)]) == 0

    output = capsys.readouterr().out
    bundle_path = repo / ".sidecar" / "harness-cleanup-candidates.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert f"cleanup candidates: {bundle_path}" in output
    assert payload == {
        "schema_version": 1,
        "structural_eval": {
            "suite_id": "structural",
            "runner": "harness-cleanup-structural",
            "passed": True,
            "candidate_count": 2,
            "evaluated_candidates": ["harness-cleanup-1", "harness-cleanup-2"],
            "candidate_hashes": {
                "harness-cleanup-1": payload["structural_eval"]["candidate_hashes"][
                    "harness-cleanup-1"
                ],
                "harness-cleanup-2": payload["structural_eval"]["candidate_hashes"][
                    "harness-cleanup-2"
                ],
            },
            "findings": [],
        },
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
    assert all(
        len(digest) == 64
        for digest in payload["structural_eval"]["candidate_hashes"].values()
    )
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute(
            """
        SELECT COUNT(*) FROM harness_findings
        WHERE severity = 'cleanup_candidate'
        """
        ).fetchone()[0] == 2
    proposal_paths = sorted((repo / ".sidecar" / "harness-cleanup-proposals").glob("*.json"))
    assert [path.stem for path in proposal_paths] == [
        "harness-cleanup-1",
        "harness-cleanup-2",
    ]
    proposal = json.loads(proposal_paths[0].read_text(encoding="utf-8"))
    assert proposal == {
        "schema_version": 1,
        "kind": "cleanup_proposal",
        "candidate_id": "harness-cleanup-1",
        "state": "waiting_review",
        "auto_apply": False,
        "risk_class": "review_required",
        "task": "Add ownership metadata to docs/runbook.md.",
        "source_findings": ["docs/runbook.md is missing ownership metadata."],
        "required_eval_suites": ["structural"],
        "structural_eval": {
            "bundle": ".sidecar/harness-cleanup-candidates.json",
            "candidate_hash": payload["structural_eval"]["candidate_hashes"][
                "harness-cleanup-1"
            ],
            "suite_id": "structural",
        },
    }
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == original_agents


def test_harness_cleanup_cli_is_blocked_by_read_only_kill_switch(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert main(["harness", "cleanup", "--repo", str(repo)]) == 1

    assert "cleanup blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert not (repo / ".sidecar" / "harness-cleanup-candidates.json").exists()
    assert not (repo / ".sidecar" / "harness-cleanup-proposals").exists()
    assert not (repo / ".sidecar" / "db.sqlite").exists()


def test_harness_cleanup_cli_validates_candidate_bundle_before_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InvalidCleanupCandidate:
        def to_json_dict(self) -> dict[str, object]:
            return {
                "candidate_id": "harness-cleanup-1",
                "risk_class": "review_required",
                "auto_apply": True,
                "task": "Unsafe cleanup candidate.",
                "source_findings": ["unsafe"],
                "required_eval_suites": ["structural"],
            }

    monkeypatch.setattr(
        "tugboat.cli.generate_cleanup_candidates",
        lambda repo: [InvalidCleanupCandidate()],
    )

    assert main(["harness", "cleanup", "--repo", str(tmp_path)]) == 1
    assert not (tmp_path / ".sidecar" / "harness-cleanup-candidates.json").exists()


def test_harness_cleanup_cli_blocks_when_structural_eval_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    def failing_structural_eval(repo: Path, candidates: list[object]) -> dict[str, object]:
        return {
            "suite_id": "structural",
            "runner": "harness-cleanup-structural",
            "passed": False,
            "candidate_count": len(candidates),
            "evaluated_candidates": ["harness-cleanup-1", "harness-cleanup-2"],
            "candidate_hashes": {
                "harness-cleanup-1": "a" * 64,
                "harness-cleanup-2": "b" * 64,
            },
            "findings": ["harness-cleanup-1: structural eval failed"],
        }

    monkeypatch.setattr(
        "tugboat.cli.run_cleanup_structural_eval",
        failing_structural_eval,
    )

    assert main(["harness", "cleanup", "--repo", str(repo)]) == 1

    assert "cleanup candidates blocked: structural eval failed" in capsys.readouterr().out
    assert not (repo / ".sidecar" / "harness-cleanup-candidates.json").exists()
