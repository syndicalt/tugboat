from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


INSTRUCTION_FILES = ("AGENTS.md", "CODEX.md", "CLAUDE.md", "SKILL.md")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class HarnessCheckResult:
    passed: bool
    findings: list[str]


def check_harness_legibility(repo: Path, max_instruction_lines: int = 100) -> HarnessCheckResult:
    repo = repo.resolve()
    findings: list[str] = []

    for relative_path in INSTRUCTION_FILES:
        path = repo / relative_path
        if not path.exists():
            continue

        text = path.read_text(encoding="utf-8")
        line_count = len(text.splitlines())
        is_monolithic = line_count > max_instruction_lines
        if is_monolithic:
            findings.append(
                f"{relative_path} has {line_count} instruction lines; keep it at or below "
                f"{max_instruction_lines} and move detail into repo-local markdown references."
            )

        local_markdown_refs = _repo_local_markdown_refs(text)
        if not local_markdown_refs:
            if not is_monolithic:
                findings.append(
                    f"{relative_path} has no repo-local markdown references; keep instruction files "
                    "as short maps to deeper docs."
                )
            continue

        for ref in local_markdown_refs:
            target = (path.parent / ref).resolve()
            if not _is_relative_to(target, repo):
                findings.append(
                    f"{relative_path} references markdown file outside the repo: {ref.as_posix()}."
                )
                continue

            if not target.is_file():
                findings.append(
                    f"{relative_path} references missing repo-local markdown file {ref.as_posix()}."
                )

    return HarnessCheckResult(passed=not findings, findings=findings)


def _repo_local_markdown_refs(text: str) -> list[Path]:
    refs: list[Path] = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        raw_target = match.group(1).strip()
        target = _link_destination(raw_target)
        if target is None:
            continue

        path = Path(target)
        if path.suffix.lower() == ".md":
            refs.append(path)

    return refs


def _link_destination(raw_target: str) -> str | None:
    if not raw_target or raw_target.startswith("#"):
        return None

    first_token = raw_target.split()[0]
    target = first_token.split("#", 1)[0].split("?", 1)[0]
    if not target or "://" in target or ":" in target or target.startswith("/"):
        return None

    return target


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
