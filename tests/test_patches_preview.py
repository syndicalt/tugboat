from tugboat.patches import apply_unified_diff


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
