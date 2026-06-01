from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tugboat.corpus.markdown import parse_markdown
from tugboat.corpus.precedence import resolve_precedence
from tugboat.models import DocumentRecord, IndexResult, InstructionFilePolicy, Policy


_GLOB_CHARS = set("*?[")


class InstructionIndexBudgetExceeded(ValueError):
    pass


def index_repo(repo: Path, policy: Policy) -> IndexResult:
    documents = []

    for path, entry in sorted(instruction_paths(repo, policy)):
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
    repo: Path,
    policy_or_entries: Policy | Sequence[InstructionFilePolicy],
    *,
    max_instruction_files: int | None = None,
) -> list[tuple[Path, InstructionFilePolicy]]:
    if isinstance(policy_or_entries, Policy):
        entries = policy_or_entries.instruction_files
        max_instruction_files = policy_or_entries.index_max_instruction_files
    else:
        entries = tuple(policy_or_entries)
    paths: list[tuple[Path, InstructionFilePolicy]] = []
    seen: set[Path] = set()
    for entry in entries:
        scope_base = _scope_base(repo, entry)
        if any(char in entry.path for char in _GLOB_CHARS):
            matches = (path for path in scope_base.glob(entry.path) if path.is_file())
        else:
            path = scope_base / entry.path
            matches = (candidate for candidate in (path,) if candidate.is_file())
        for path in matches:
            if path in seen:
                continue
            seen.add(path)
            discovered_count = len(seen)
            if max_instruction_files is not None and discovered_count > max_instruction_files:
                raise InstructionIndexBudgetExceeded(
                    "instruction file budget exceeded: "
                    f"{discovered_count} discovered, limit {max_instruction_files}"
                )
            paths.append((path, entry))
    return paths


def _scope_base(repo: Path, entry: InstructionFilePolicy) -> Path:
    scope_root = entry.scope_root.strip()
    if not scope_root or scope_root == ".":
        return repo
    return repo / scope_root
