from tugboat.corpus.precedence import resolve_precedence
from tugboat.models import DocumentRecord


def _document(path: str, precedence: int) -> DocumentRecord:
    return DocumentRecord(
        path=path,
        kind="agent_policy",
        precedence=precedence,
        protected=False,
        hash="hash",
        mtime=0.0,
        parser_version="test",
        chunks=(),
    )


def test_resolve_precedence_orders_documents_by_precedence_descending_then_path():
    documents = (
        _document("CODEX.md", 70),
        _document("AGENTS.md", 80),
        _document("z/SKILL.md", 60),
        _document("a/SKILL.md", 60),
    )

    graph = resolve_precedence(documents)

    assert [document.path for document in graph.documents] == [
        "AGENTS.md",
        "CODEX.md",
        "a/SKILL.md",
        "z/SKILL.md",
    ]
