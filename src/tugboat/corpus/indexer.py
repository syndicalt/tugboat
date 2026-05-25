from __future__ import annotations

from pathlib import Path

from tugboat.corpus.markdown import parse_markdown
from tugboat.models import IndexResult, InstructionFilePolicy, Policy


_GLOB_CHARS = set("*?[")


def index_repo(repo: Path, policy: Policy) -> IndexResult:
    documents = []
    seen: set[Path] = set()

    for path, entry in sorted(_instruction_paths(repo, policy.instruction_files)):
        if path in seen:
            continue
        seen.add(path)
        documents.append(
            parse_markdown(
                path,
                kind=entry.kind,
                precedence=entry.precedence,
                protected=entry.protected,
                document_path=path.relative_to(repo).as_posix(),
            )
        )

    return IndexResult(documents=tuple(documents))


def _instruction_paths(
    repo: Path, entries: tuple[InstructionFilePolicy, ...]
) -> list[tuple[Path, InstructionFilePolicy]]:
    paths: list[tuple[Path, InstructionFilePolicy]] = []
    for entry in entries:
        if any(char in entry.path for char in _GLOB_CHARS):
            matches = (path for path in repo.glob(entry.path) if path.is_file())
        else:
            path = repo / entry.path
            matches = (candidate for candidate in (path,) if candidate.is_file())
        paths.extend((path, entry) for path in matches)
    return paths
