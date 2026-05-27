from tugboat.patches import (
    apply_unified_diff,
    bounded_edit_metadata_mismatch_fields,
    classify_markdown_diff_operations,
)


def test_apply_unified_diff_uses_declared_hunk_start_for_ranged_hunks():
    base = "alpha\nbeta\ngamma\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,1 +1,1 @@\n"
        " beta\n"
        "+delta\n"
    )

    assert apply_unified_diff(base, diff) is None


def test_apply_unified_diff_rejects_hunk_count_mismatch():
    base = "alpha\nbeta\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,1 +1,1 @@\n"
        " alpha\n"
        "-beta\n"
        "+gamma\n"
    )

    assert apply_unified_diff(base, diff) is None


def test_apply_unified_diff_applies_valid_ranged_hunk():
    base = "alpha\nbeta\ngamma\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -2,1 +2,1 @@\n"
        "-beta\n"
        "+delta\n"
    )

    assert apply_unified_diff(base, diff) == "alpha\ndelta\ngamma\n"


def test_apply_unified_diff_rejects_bare_hunk_headers():
    base = "alpha\nbeta\n"
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+delta\n"

    assert apply_unified_diff(base, diff) is None


def test_apply_unified_diff_rejects_model_text_around_patch():
    base = "alpha\nbeta\n"
    diff = (
        "Here is the patch:\n"
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-alpha\n"
        "+delta\n"
    )

    assert apply_unified_diff(base, diff) is None


def test_apply_unified_diff_rejects_multiple_file_headers():
    base = "alpha\nbeta\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-alpha\n"
        "+delta\n"
        "--- a/AGENTS.md\n"
        "+++ b/AGENTS.md\n"
    )

    assert apply_unified_diff(base, diff) is None


def test_apply_unified_diff_rejects_unexpected_file_path():
    base = "alpha\nbeta\n"
    diff = (
        "--- a/AGENTS.md\n"
        "+++ b/AGENTS.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-alpha\n"
        "+delta\n"
    )

    assert apply_unified_diff(base, diff, expected_path="CODEX.md") is None


def test_classify_markdown_diff_operations_derives_add_section_and_budget():
    base = "# Testing\n\nUse regression tests.\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -2,0 +3,1 @@\n"
        "+Agents must add a regression case before closing fixes.\n"
    )

    operations = classify_markdown_diff_operations(base, diff, expected_path="CODEX.md")

    assert [operation.as_metadata() for operation in operations] == [
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 1,
        }
    ]


def test_classify_markdown_diff_operations_derives_heading_promotion():
    base = "# Rules\n\n## Testing\n\nUse tests.\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -3,1 +3,1 @@\n"
        "-## Testing\n"
        "+# Testing\n"
    )

    operations = classify_markdown_diff_operations(base, diff, expected_path="CODEX.md")

    assert [operation.as_metadata() for operation in operations] == [
        {
            "operator": "promote",
            "file": "CODEX.md",
            "section": "Testing",
            "changed_lines": 1,
            "normative_changes": 0,
        }
    ]


def test_classify_markdown_diff_operations_derives_remaining_structural_operators():
    cases = [
        (
            "# Testing\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,1 +3,0 @@\n-Use tests.\n",
            "delete",
            "Testing",
        ),
        (
            "# Testing\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,1 +3,1 @@\n-Use tests.\n+Use regression tests.\n",
            "replace",
            "Testing",
        ),
        (
            "# Testing\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,0 +4,1 @@\n+## Regression\n",
            "split",
            "Regression",
        ),
        (
            "# Testing\n\n## Regression\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,1 +3,0 @@\n-## Regression\n",
            "merge",
            "Regression",
        ),
        (
            "# Testing\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,0 +4,1 @@\n+<!-- keep fixture coverage explicit -->\n",
            "annotate",
            "Testing",
        ),
        (
            "# Testing\n\n# Regression\n\nUse tests.\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -3,1 +3,1 @@\n-# Regression\n+## Regression\n",
            "demote",
            "Regression",
        ),
    ]

    for base, diff, operator, section in cases:
        operations = classify_markdown_diff_operations(base, diff, expected_path="CODEX.md")

        assert [operation.operator for operation in operations] == [operator]
        assert [operation.section for operation in operations] == [section]


def test_bounded_edit_metadata_mismatch_fields_reports_deterministic_fields():
    base = "# Testing\n\nUse tests.\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -2,0 +3,1 @@\n"
        "+Agents must add a regression case before closing fixes.\n"
    )

    mismatches = bounded_edit_metadata_mismatch_fields(
        base,
        diff,
        (
            {
                "operator": "delete",
                "file": "CODEX.md",
                "section": "Approval",
                "changed_lines": 2,
                "normative_changes": 0,
            },
        ),
        expected_path="CODEX.md",
    )

    assert mismatches == ("operator", "section", "changed_lines", "normative_changes")


def test_bounded_edit_metadata_mismatch_fields_reports_count_and_invalid_diff():
    base = "# Testing\n\nUse tests.\n"
    diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -2,0 +3,1 @@\n"
        "+Add regression test guidance.\n"
    )

    assert bounded_edit_metadata_mismatch_fields(
        base,
        diff,
        (
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            },
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            },
        ),
        expected_path="CODEX.md",
    ) == ("count",)
    assert bounded_edit_metadata_mismatch_fields(
        base,
        "not a diff\n",
        (),
        expected_path="CODEX.md",
    ) == ("diff",)


def test_classify_markdown_diff_operations_handles_document_fallback_and_invalid_preview():
    document_operations = classify_markdown_diff_operations(
        "plain text\n",
        "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,1 +1,2 @@\n plain text\n+more text\n",
        expected_path="CODEX.md",
    )
    assert [operation.section for operation in document_operations] == ["Document"]

    assert (
        classify_markdown_diff_operations(
            "actual text\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,1 +1,1 @@\n-wrong text\n+new text\n",
            expected_path="CODEX.md",
        )
        == ()
    )
    assert (
        classify_markdown_diff_operations(
            "plain text\n",
            "--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,2 +1,1 @@\n-plain text\n+new text\n",
            expected_path="CODEX.md",
        )
        == ()
    )
