from __future__ import annotations

import tomllib
from pathlib import Path

from tugboat import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]
V1_RELEASE_VERSION = "1.0.0"


def test_project_metadata_and_runtime_version_are_v1_release_identity() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == V1_RELEASE_VERSION
    assert __version__ == V1_RELEASE_VERSION


def test_changelog_and_release_notes_are_published_for_v1() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_notes = (REPO_ROOT / "docs/releases/1.0.0-draft.md").read_text(encoding="utf-8")

    assert "Draft release notes live in" not in changelog
    assert "Planned v1 highlights" not in changelog
    assert "before publication" not in changelog
    assert "verification_status: verified" in release_notes
    assert "# Tugboat 1.0.0 Release Notes" in release_notes
    assert "planned stable release" not in release_notes
