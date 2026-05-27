from __future__ import annotations

from pathlib import Path

from tugboat.corpus.markdown import parse_markdown
from tugboat.corpus.precedence import resolve_precedence
from tugboat.models import DocumentRecord, IndexResult, InstructionFilePolicy, Policy


_GLOB_CHARS = set("*?[")


def index_repo(repo: Path, policy: Policy) -> IndexResult:
    documents = []
    seen: set[Path] = set()

    for path, entry in sorted(instruction_paths(repo, policy.instruction_files)):
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

    return IndexResult(documents=resolve_precedence(documents).documents)


def instruction_chunk_refs(index: IndexResult) -> list[str]:
    return [
        _instruction_chunk_ref(document, chunk.anchor, chunk.byte_start, chunk.byte_end)
        for document in index.documents
        for chunk in document.chunks
    ]


def _instruction_chunk_ref(
    document: DocumentRecord, anchor: str, byte_start: int, byte_end: int
) -> str:
    if anchor:
        return f"{document.path}#{anchor}"
    return f"{document.path}#bytes-{byte_start}-{byte_end}"


def instruction_paths(
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
