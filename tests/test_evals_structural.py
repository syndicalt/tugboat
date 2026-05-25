from pathlib import Path

from tugboat.evals import Severity, evaluate_markdown_pair


def test_stable_headings_and_anchors_pass_without_findings():
    before = """# Overview

Follow the repo instructions.

## Setup Guide

Run tests before reporting success.
"""
    after = """# Overview

Follow the repo instructions carefully.

## Setup Guide

Run the relevant tests before reporting success.
"""

    report = evaluate_markdown_pair(before, after)

    assert report.passed is True
    assert report.anchors_before == ("overview", "setup-guide")
    assert report.anchors_after == ("overview", "setup-guide")
    assert report.findings == ()


def test_frontmatter_removed_is_reported():
    before = """---
owner: platform
status: active
---
# Policy

Keep approval boundaries.
"""
    after = """# Policy

Keep approval boundaries.
"""

    report = evaluate_markdown_pair(before, after)

    assert report.passed is False
    assert any(
        finding.code == "frontmatter.removed" and finding.severity is Severity.ERROR
        for finding in report.findings
    )


def test_fenced_code_change_is_reported():
    before = """# Runbook

```bash
python -m pytest -q
```
"""
    after = """# Runbook

```bash
python -m pytest
```
"""

    report = evaluate_markdown_pair(before, after)

    assert report.passed is False
    assert any(finding.code == "fence.changed" for finding in report.findings)


def test_damaged_fenced_code_is_reported():
    before = """# Runbook

```bash
python -m pytest -q
```
"""
    after = """# Runbook

```bash
python -m pytest -q
"""

    report = evaluate_markdown_pair(before, after)

    assert report.passed is False
    assert any(finding.code == "fence.unclosed" for finding in report.findings)


def test_broken_local_link_and_path_are_reported(tmp_path: Path):
    existing = tmp_path / "docs" / "guide.md"
    existing.parent.mkdir()
    existing.write_text("# Guide\n", encoding="utf-8")

    markdown = """# Links

See [guide](docs/guide.md), [missing](docs/missing.md), and `scripts/missing.sh`.
External links like [site](https://example.com) are not checked.
"""

    report = evaluate_markdown_pair(markdown, markdown, root=tmp_path)

    assert report.passed is False
    assert {finding.target for finding in report.findings} == {
        "docs/missing.md",
        "scripts/missing.sh",
    }
    assert all(finding.code == "link.local_missing" for finding in report.findings)


def test_semantic_diff_classifies_additive_clarification_and_normative_change():
    additive = evaluate_markdown_pair(
        "# Reviews\n\nRun tests before final response.\n",
        "# Reviews\n\nRun tests before final response. For example, use the focused suite first.\n",
    )
    normative = evaluate_markdown_pair(
        "# Reviews\n\nYou must run tests before final response.\n",
        "# Reviews\n\nYou may skip tests before final response.\n",
    )

    assert additive.semantic_diff == "additive_clarification"
    assert normative.semantic_diff == "normative_change"
    assert any(finding.code == "semantic.normative_change" for finding in normative.findings)
