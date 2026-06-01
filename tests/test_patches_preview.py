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


def test_apply_unified_diff_rejects_zero_start_with_nonzero_span():
    base = "alpha\n"
    old_zero_diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -0,1 +1,1 @@\n"
        "-alpha\n"
        "+beta\n"
    )
    new_zero_diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,1 +0,1 @@\n"
        "-alpha\n"
        "+beta\n"
    )

    for diff in (old_zero_diff, new_zero_diff):
        assert apply_unified_diff(base, diff, expected_path="CODEX.md") is None
        assert (
            classify_markdown_diff_operations(base, diff, expected_path="CODEX.md")
            == ()
        )
        assert bounded_edit_metadata_mismatch_fields(
            base,
            diff,
            (
                {
                    "operator": "replace",
                    "file": "CODEX.md",
                    "section": "Document",
                    "changed_lines": 1,
                    "normative_changes": 0,
                },
            ),
            expected_path="CODEX.md",
        ) == ("diff",)


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


def test_apply_unified_diff_generated_single_line_replacements_preserve_expected_text():
    base_lines = [
        "# Rules\n",
        "\n",
        "Use tests.\n",
        "Record rollback.\n",
        "Keep review notes.\n",
    ]
    base = "".join(base_lines)

    for line_number, original in enumerate(base_lines, start=1):
        replacement = f"Generated replacement {line_number}.\n"
        diff = (
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            f"@@ -{line_number},1 +{line_number},1 @@\n"
            f"-{original}"
            f"+{replacement}"
        )

        expected_lines = [*base_lines]
        expected_lines[line_number - 1] = replacement

        assert apply_unified_diff(base, diff, expected_path="CODEX.md") == "".join(
            expected_lines
        )


def test_bounded_edit_metadata_generated_replacements_match_classifier_output():
    base = "# Rules\n\nUse tests.\nRecord rollback.\nKeep review notes.\n"

    for line_number, original in (
        (3, "Use tests.\n"),
        (4, "Record rollback.\n"),
        (5, "Keep review notes.\n"),
    ):
        replacement = f"Generated replacement {line_number}.\n"
        diff = (
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            f"@@ -{line_number},1 +{line_number},1 @@\n"
            f"-{original}"
            f"+{replacement}"
        )
        operations = classify_markdown_diff_operations(base, diff, expected_path="CODEX.md")

        assert bounded_edit_metadata_mismatch_fields(
            base,
            diff,
            tuple(operation.as_metadata() for operation in operations),
            expected_path="CODEX.md",
        ) == ()


def test_generated_bounded_operator_diffs_round_trip_through_metadata():
    allowed_operators = {
        "add",
        "delete",
        "replace",
        "annotate",
        "split",
        "merge",
        "promote",
        "demote",
    }

    for case in _generated_patch_cases():
        operations = classify_markdown_diff_operations(
            case["base"],
            case["diff"],
            expected_path="CODEX.md",
        )

        assert apply_unified_diff(
            case["base"],
            case["diff"],
            expected_path="CODEX.md",
        ) == case["expected_text"]
        assert [operation.operator for operation in operations] == [case["operator"]]
        assert bounded_edit_metadata_mismatch_fields(
            case["base"],
            case["diff"],
            tuple(operation.as_metadata() for operation in operations),
            expected_path="CODEX.md",
        ) == ()
        metadata = operations[0].as_metadata()
        assert metadata["operator"] in allowed_operators
        assert metadata["file"] == "CODEX.md"
        assert metadata["changed_lines"] == max(case["removed_count"], case["added_count"])
        assert metadata["normative_changes"] <= metadata["changed_lines"]


def test_generated_bounded_operator_diffs_reject_mutated_hunk_counts():
    for case in _generated_patch_cases():
        invalid_diff = case["diff"].replace(case["header"], case["invalid_header"], 1)

        assert apply_unified_diff(
            case["base"],
            invalid_diff,
            expected_path="CODEX.md",
        ) is None
        assert (
            classify_markdown_diff_operations(
                case["base"],
                invalid_diff,
                expected_path="CODEX.md",
            )
            == ()
        )
        assert bounded_edit_metadata_mismatch_fields(
            case["base"],
            invalid_diff,
            (),
            expected_path="CODEX.md",
        ) == ("diff",)


def _generated_patch_cases() -> tuple[dict[str, object], ...]:
    return (
        {
            "operator": "add",
            "base": "# Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -2,0 +3,1 @@\n"
                "+Add regression guidance.\n"
            ),
            "expected_text": "# Testing\nAdd regression guidance.\n\nUse tests.\n",
            "header": "@@ -2,0 +3,1 @@",
            "invalid_header": "@@ -2,0 +3,2 @@",
            "removed_count": 0,
            "added_count": 1,
        },
        {
            "operator": "delete",
            "base": "# Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,1 +3,0 @@\n"
                "-Use tests.\n"
            ),
            "expected_text": "# Testing\n\n",
            "header": "@@ -3,1 +3,0 @@",
            "invalid_header": "@@ -3,2 +3,0 @@",
            "removed_count": 1,
            "added_count": 0,
        },
        {
            "operator": "replace",
            "base": "# Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,1 +3,1 @@\n"
                "-Use tests.\n"
                "+Use regression tests.\n"
            ),
            "expected_text": "# Testing\n\nUse regression tests.\n",
            "header": "@@ -3,1 +3,1 @@",
            "invalid_header": "@@ -3,2 +3,1 @@",
            "removed_count": 1,
            "added_count": 1,
        },
        {
            "operator": "annotate",
            "base": "# Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,0 +4,1 @@\n"
                "+<!-- keep fixture coverage explicit -->\n"
            ),
            "expected_text": "# Testing\n\n<!-- keep fixture coverage explicit -->\nUse tests.\n",
            "header": "@@ -3,0 +4,1 @@",
            "invalid_header": "@@ -3,0 +4,2 @@",
            "removed_count": 0,
            "added_count": 1,
        },
        {
            "operator": "split",
            "base": "# Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,0 +4,1 @@\n"
                "+## Regression\n"
            ),
            "expected_text": "# Testing\n\n## Regression\nUse tests.\n",
            "header": "@@ -3,0 +4,1 @@",
            "invalid_header": "@@ -3,0 +4,2 @@",
            "removed_count": 0,
            "added_count": 1,
        },
        {
            "operator": "merge",
            "base": "# Testing\n\n## Regression\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,1 +3,0 @@\n"
                "-## Regression\n"
            ),
            "expected_text": "# Testing\n\n\nUse tests.\n",
            "header": "@@ -3,1 +3,0 @@",
            "invalid_header": "@@ -3,2 +3,0 @@",
            "removed_count": 1,
            "added_count": 0,
        },
        {
            "operator": "promote",
            "base": "# Rules\n\n## Testing\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,1 +3,1 @@\n"
                "-## Testing\n"
                "+# Testing\n"
            ),
            "expected_text": "# Rules\n\n# Testing\n\nUse tests.\n",
            "header": "@@ -3,1 +3,1 @@",
            "invalid_header": "@@ -3,2 +3,1 @@",
            "removed_count": 1,
            "added_count": 1,
        },
        {
            "operator": "demote",
            "base": "# Testing\n\n# Regression\n\nUse tests.\n",
            "diff": (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -3,1 +3,1 @@\n"
                "-# Regression\n"
                "+## Regression\n"
            ),
            "expected_text": "# Testing\n\n## Regression\n\nUse tests.\n",
            "header": "@@ -3,1 +3,1 @@",
            "invalid_header": "@@ -3,2 +3,1 @@",
            "removed_count": 1,
            "added_count": 1,
        },
    )


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


def test_apply_unified_diff_rejects_unsafe_file_paths():
    base = "alpha\n"
    traversal_diff = (
        "--- a/../CODEX.md\n"
        "+++ b/../CODEX.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-alpha\n"
        "+beta\n"
    )

    assert apply_unified_diff(base, traversal_diff) is None
    assert classify_markdown_diff_operations(base, traversal_diff) == ()


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
