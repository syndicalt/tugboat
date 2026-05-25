from __future__ import annotations

from collections.abc import Iterable

from tugboat.models import DocumentRecord, InstructionGraph


def resolve_precedence(documents: Iterable[DocumentRecord]) -> InstructionGraph:
    ordered = sorted(documents, key=lambda document: (-document.precedence, document.path))
    return InstructionGraph(documents=tuple(ordered))
