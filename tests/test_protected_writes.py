from __future__ import annotations

import pytest

from tugboat.security.protected_writes import (
    DiffTargetGuard,
    ProtectedWriteError,
    diff_touched_paths,
)


def test_diff_touched_paths_uses_deleted_file_path_for_dev_null_target():
    assert diff_touched_paths("--- a/CODEX.md\n+++ /dev/null\n") == {"CODEX.md"}


def test_diff_touched_paths_normalizes_absolute_and_windows_paths():
    assert diff_touched_paths("--- /CODEX.md\n+++ b/docs\\AGENTS.md\n") == {
        "docs/AGENTS.md"
    }


def test_diff_target_guard_rejects_diff_without_declared_target():
    with pytest.raises(ProtectedWriteError, match="diff does not declare any target files"):
        DiffTargetGuard(("CODEX.md",)).validate("@@\n+No file header.\n")

