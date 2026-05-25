import hashlib
from pathlib import Path

from tugboat.corpus.markdown import parse_markdown


def test_parse_markdown_ignores_fenced_headings_and_deduplicates_anchors():
    path = Path("tests/fixtures/instructions/parser/complex.md")

    document = parse_markdown(path, kind="agent_policy", precedence=70, protected=True)

    assert document.path == "tests/fixtures/instructions/parser/complex.md"
    assert document.kind == "agent_policy"
    assert document.precedence == 70
    assert document.protected is True
    assert [chunk.anchor for chunk in document.chunks] == [
        "intro",
        "setup",
        "setup-1",
        "details",
    ]
    assert [chunk.heading_path for chunk in document.chunks] == [
        ("Intro",),
        ("Intro", "Setup"),
        ("Intro", "Setup"),
        ("Intro", "Setup", "Details"),
    ]


def test_parse_markdown_records_utf8_byte_ranges_and_text_hashes(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    text = "# Café\n\nFollow unicode guidance.\n"
    path.write_text(text, encoding="utf-8")

    document = parse_markdown(path, kind="skill", precedence=60, protected=False)

    chunk = document.chunks[0]
    assert chunk.byte_start == 0
    assert chunk.byte_end == len(text.encode("utf-8"))
    assert chunk.text == text
    expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert chunk.text_hash == expected_hash
    assert document.hash == expected_hash


def test_parse_markdown_preserves_preamble_before_first_heading(tmp_path: Path):
    path = tmp_path / "CODEX.md"
    text = "Global preamble must be indexed.\n\n# Rules\n\nFollow tests.\n"
    path.write_text(text, encoding="utf-8")

    document = parse_markdown(path, kind="agent_policy", precedence=70, protected=True)

    preamble = document.chunks[0]
    assert preamble.heading_path == ()
    assert preamble.anchor == ""
    assert preamble.text == "Global preamble must be indexed.\n\n"
    assert preamble.byte_start == 0
    assert preamble.byte_end == len(preamble.text.encode("utf-8"))
    assert document.chunks[1].heading_path == ("Rules",)
