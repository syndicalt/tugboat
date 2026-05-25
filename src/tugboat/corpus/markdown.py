from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tugboat.models import ChunkRecord, DocumentRecord


PARSER_VERSION = "markdown-heading-v1"
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ANCHOR_WORD_RE = re.compile(r"[^a-z0-9 -]")
_ANCHOR_SPACE_RE = re.compile(r"[ -]+")


def parse_markdown(
    path: Path,
    *,
    kind: str,
    precedence: int,
    protected: bool,
    document_path: str | None = None,
) -> DocumentRecord:
    text = path.read_text(encoding="utf-8")
    headings = _find_headings(text)
    chunks = _build_chunks(text, headings)
    stat = path.stat()

    return DocumentRecord(
        path=document_path or str(path),
        kind=kind,
        precedence=precedence,
        protected=protected,
        hash=_sha256(text),
        mtime=stat.st_mtime,
        parser_version=PARSER_VERSION,
        chunks=chunks,
    )


def _find_headings(text: str) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    fence_marker = ""
    char_offset = 0

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            char_offset += len(line)
            continue

        if not in_fence:
            match = _HEADING_RE.match(line.rstrip("\r\n"))
            if match:
                headings.append((char_offset, len(match.group(1)), match.group(2).strip()))

        char_offset += len(line)

    return headings


def _build_chunks(
    text: str, headings: list[tuple[int, int, str]]
) -> tuple[ChunkRecord, ...]:
    if not headings:
        return (
            ChunkRecord(
                heading_path=(),
                anchor="",
                byte_start=0,
                byte_end=len(text.encode("utf-8")),
                text_hash=_sha256(text),
                text=text,
            ),
        )

    used_anchors: dict[str, int] = {}
    stack: list[tuple[int, str]] = []
    chunks: list[ChunkRecord] = []
    byte_offsets = _byte_offsets(text)

    first_heading_start = headings[0][0]
    if first_heading_start > 0:
        preamble_text = text[:first_heading_start]
        chunks.append(
            ChunkRecord(
                heading_path=(),
                anchor="",
                byte_start=0,
                byte_end=byte_offsets[first_heading_start],
                text_hash=_sha256(preamble_text),
                text=preamble_text,
            )
        )

    for index, (start, level, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        stack = [(existing_level, name) for existing_level, name in stack if existing_level < level]
        stack.append((level, heading))
        chunk_text = text[start:end]
        chunks.append(
            ChunkRecord(
                heading_path=tuple(name for _, name in stack),
                anchor=_dedupe_anchor(_anchor_for(heading), used_anchors),
                byte_start=byte_offsets[start],
                byte_end=byte_offsets[end],
                text_hash=_sha256(chunk_text),
                text=chunk_text,
            )
        )

    return tuple(chunks)


def _byte_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for char in text:
        total += len(char.encode("utf-8"))
        offsets.append(total)
    return offsets


def _anchor_for(heading: str) -> str:
    anchor = heading.lower().strip()
    anchor = _ANCHOR_WORD_RE.sub("", anchor)
    anchor = _ANCHOR_SPACE_RE.sub("-", anchor).strip("-")
    return anchor


def _dedupe_anchor(anchor: str, used_anchors: dict[str, int]) -> str:
    count = used_anchors.get(anchor, 0)
    used_anchors[anchor] = count + 1
    if count == 0:
        return anchor
    return f"{anchor}-{count}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
