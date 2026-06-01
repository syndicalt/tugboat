from __future__ import annotations

import tomllib
from pathlib import Path

from tugboat import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]
V1_RELEASE_VERSION = "1.0.0"
V1_RELEASE_CRITICAL_DOCS = (
    "docs/migration-v1.md",
    "docs/compatibility-policy.md",
    "docs/llmff-compatibility.md",
    "docs/roadmaps/v1.0.0-roadmap.md",
    "docs/releases/1.0.0-draft.md",
    "docs/ops/release-checklist.md",
    "docs/ops/security-review-production-candidate.md",
)


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


def test_v1_release_critical_docs_are_verified_when_package_is_v1() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == V1_RELEASE_VERSION

    for relative_path in V1_RELEASE_CRITICAL_DOCS:
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert (
            "verification_status: verified" in content
        ), f"{relative_path} must be verified before v1 release"

    release_notes = (REPO_ROOT / "docs/releases/1.0.0-draft.md").read_text(
        encoding="utf-8"
    )
    assert "python -m pip install tugboat==1.0.0" in release_notes
    assert "Known limitations:" in release_notes
    assert "verification evidence" in release_notes
