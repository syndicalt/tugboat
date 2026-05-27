from __future__ import annotations

from dataclasses import dataclass
import re


HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
OLD_FILE_HEADER = re.compile(r"^--- (?P<path>\S+)\n?$")
NEW_FILE_HEADER = re.compile(r"^\+\+\+ (?P<path>\S+)\n?$")
MARKDOWN_HEADING = re.compile(r"^(?P<marker>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
NORMATIVE_MODAL = re.compile(
    r"\b(must|must not|shall|shall not|required|requires|should|should not|may not)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MarkdownDiffOperation:
    operator: str
    file: str
    section: str
    changed_lines: int
    normative_changes: int

    def as_metadata(self) -> dict[str, object]:
        return {
            "operator": self.operator,
            "file": self.file,
            "section": self.section,
            "changed_lines": self.changed_lines,
            "normative_changes": self.normative_changes,
        }


def apply_unified_diff(
    base_text: str,
    diff: str,
    *,
    expected_path: str | None = None,
) -> str | None:
    base_lines = base_text.splitlines(keepends=True)
    output: list[str] = []
    position = 0
    hunk: _RangedHunk | None = None
    old_header_path: str | None = None
    new_header_path: str | None = None
    seen_hunk = False

    for line in diff.splitlines(keepends=True):
        if hunk is None and line.startswith("---"):
            if hunk is not None or seen_hunk or old_header_path is not None:
                return None
            old_header_path = _parse_file_header(line, OLD_FILE_HEADER, expected_prefix="a/")
            if old_header_path is None:
                return None
            continue
        if hunk is None and line.startswith("+++"):
            if hunk is not None or seen_hunk or old_header_path is None or new_header_path is not None:
                return None
            new_header_path = _parse_file_header(line, NEW_FILE_HEADER, expected_prefix="b/")
            if new_header_path is None:
                return None
            if new_header_path != old_header_path:
                return None
            if expected_path is not None and new_header_path != expected_path:
                return None
            continue
        if line.startswith("@@"):
            if old_header_path is None or new_header_path is None:
                return None
            if hunk is not None and not _ranged_hunk_counts_match(hunk):
                return None
            parsed = _parse_ranged_hunk_header(line)
            if parsed is None:
                return None
            seen_hunk = True
            start_position = max(parsed.old_start - 1, 0)
            if start_position < position or start_position > len(base_lines):
                return None
            output.extend(base_lines[position:start_position])
            position = start_position
            hunk = parsed
            continue
        if hunk is None or line.startswith("\\"):
            return None

        marker = line[:1]
        body = line[1:]
        if hunk is not None:
            if marker in {" ", "-"}:
                if position >= len(base_lines) or base_lines[position] != body:
                    return None
                hunk.old_seen += 1
                if marker == " ":
                    output.append(base_lines[position])
                    hunk.new_seen += 1
                position += 1
                continue
            if marker == "+":
                output.append(body)
                hunk.new_seen += 1
                continue
            return None

    if hunk is not None and not _ranged_hunk_counts_match(hunk):
        return None
    if not seen_hunk:
        return None
    output.extend(base_lines[position:])
    return "".join(output)


def classify_markdown_diff_operations(
    base_text: str,
    diff: str,
    *,
    expected_path: str | None = None,
) -> tuple[MarkdownDiffOperation, ...]:
    patch = _parse_single_file_diff(diff, expected_path=expected_path)
    if patch is None:
        return ()
    new_text = apply_unified_diff(base_text, diff, expected_path=expected_path)
    if new_text is None:
        return ()
    base_lines = base_text.splitlines()
    new_lines = new_text.splitlines()
    operations: list[MarkdownDiffOperation] = []
    for hunk in patch.hunks:
        removed = [_strip_line_end(line) for line in hunk.removed]
        added = [_strip_line_end(line) for line in hunk.added]
        if not removed and not added:
            continue
        operator = _classify_hunk_operator(removed, added)
        section = _hunk_section(
            operator=operator,
            removed=removed,
            added=added,
            base_lines=base_lines,
            new_lines=new_lines,
            old_start=hunk.old_start,
            new_start=hunk.new_start,
        )
        operations.append(
            MarkdownDiffOperation(
                operator=operator,
                file=patch.path,
                section=section,
                changed_lines=max(len(removed), len(added)),
                normative_changes=_normative_change_count(removed, added),
            )
        )
    return tuple(operations)


def bounded_edit_metadata_mismatch_fields(
    base_text: str,
    diff: str,
    metadata: tuple[dict[str, object], ...],
    *,
    expected_path: str | None = None,
) -> tuple[str, ...]:
    operations = classify_markdown_diff_operations(
        base_text,
        diff,
        expected_path=expected_path,
    )
    if not operations:
        return ("diff",)
    actual_metadata = [operation.as_metadata() for operation in operations]
    mismatches: list[str] = []
    if len(metadata) != len(actual_metadata):
        mismatches.append("count")
    for claimed, actual in zip(metadata, actual_metadata, strict=False):
        for field in ("operator", "file", "section", "changed_lines", "normative_changes"):
            if claimed.get(field) != actual[field]:
                mismatches.append(field)
    return tuple(dict.fromkeys(mismatches))


@dataclass(frozen=True)
class _ParsedDiff:
    path: str
    hunks: tuple["_ParsedHunk", ...]


@dataclass(frozen=True)
class _ParsedHunk:
    old_start: int
    new_start: int
    removed: tuple[str, ...]
    added: tuple[str, ...]


def _parse_single_file_diff(
    diff: str,
    *,
    expected_path: str | None,
) -> _ParsedDiff | None:
    old_header_path: str | None = None
    new_header_path: str | None = None
    hunks: list[_ParsedHunk] = []
    current_header: _RangedHunk | None = None
    removed: list[str] = []
    added: list[str] = []

    def finish_hunk() -> bool:
        nonlocal current_header, removed, added
        if current_header is None:
            return True
        if not _ranged_hunk_counts_match(current_header):
            return False
        hunks.append(
            _ParsedHunk(
                old_start=current_header.old_start,
                new_start=current_header.new_start,
                removed=tuple(removed),
                added=tuple(added),
            )
        )
        current_header = None
        removed = []
        added = []
        return True

    for line in diff.splitlines(keepends=True):
        if current_header is None and line.startswith("---"):
            if old_header_path is not None or hunks:
                return None
            old_header_path = _parse_file_header(line, OLD_FILE_HEADER, expected_prefix="a/")
            if old_header_path is None:
                return None
            continue
        if current_header is None and line.startswith("+++"):
            if old_header_path is None or new_header_path is not None or hunks:
                return None
            new_header_path = _parse_file_header(line, NEW_FILE_HEADER, expected_prefix="b/")
            if new_header_path is None or new_header_path != old_header_path:
                return None
            if expected_path is not None and new_header_path != expected_path:
                return None
            continue
        if line.startswith("@@"):
            if old_header_path is None or new_header_path is None:
                return None
            if not finish_hunk():
                return None
            parsed = _parse_ranged_hunk_header(line)
            if parsed is None:
                return None
            current_header = parsed
            continue
        if current_header is None or line.startswith("\\"):
            return None
        marker = line[:1]
        body = line[1:]
        if marker == " ":
            current_header.old_seen += 1
            current_header.new_seen += 1
            continue
        if marker == "-":
            current_header.old_seen += 1
            removed.append(body)
            continue
        if marker == "+":
            current_header.new_seen += 1
            added.append(body)
            continue
        return None

    if not finish_hunk() or new_header_path is None or not hunks:
        return None
    return _ParsedDiff(path=new_header_path, hunks=tuple(hunks))


def _parse_file_header(
    line: str,
    pattern: re.Pattern[str],
    *,
    expected_prefix: str,
) -> str | None:
    match = pattern.match(line)
    if match is None:
        return None
    path = match.group("path")
    if not path.startswith(expected_prefix):
        return None
    return path.removeprefix(expected_prefix)


class _RangedHunk:
    def __init__(self, old_start: int, old_count: int, new_start: int, new_count: int):
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.old_seen = 0
        self.new_seen = 0


def _parse_ranged_hunk_header(line: str) -> _RangedHunk | None:
    match = HUNK_HEADER.match(line)
    if match is None:
        return None
    old_start = int(match.group("old_start"))
    old_count = int(match.group("old_count") or "1")
    new_start = int(match.group("new_start"))
    new_count = int(match.group("new_count") or "1")
    return _RangedHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
    )


def _ranged_hunk_counts_match(hunk: _RangedHunk) -> bool:
    return hunk.old_seen == hunk.old_count and hunk.new_seen == hunk.new_count


def _classify_hunk_operator(removed: list[str], added: list[str]) -> str:
    removed_headings = [_markdown_heading(line) for line in removed]
    added_headings = [_markdown_heading(line) for line in added]
    removed_headings = [heading for heading in removed_headings if heading is not None]
    added_headings = [heading for heading in added_headings if heading is not None]
    if len(removed_headings) == 1 and len(added_headings) == 1:
        removed_heading = removed_headings[0]
        added_heading = added_headings[0]
        if removed_heading[1] == added_heading[1]:
            if added_heading[0] < removed_heading[0]:
                return "promote"
            if added_heading[0] > removed_heading[0]:
                return "demote"
    if added and not removed:
        if all(_is_annotation_line(line) for line in added):
            return "annotate"
        if added_headings:
            return "split"
        return "add"
    if removed and not added:
        if removed_headings:
            return "merge"
        return "delete"
    return "replace"


def _hunk_section(
    *,
    operator: str,
    removed: list[str],
    added: list[str],
    base_lines: list[str],
    new_lines: list[str],
    old_start: int,
    new_start: int,
) -> str:
    for line in [*added, *removed]:
        heading = _markdown_heading(line)
        if heading is not None:
            return heading[1]
    if operator in {"delete", "merge"}:
        return _section_at_line(base_lines, old_start)
    return _section_at_line(new_lines, new_start)


def _section_at_line(lines: list[str], line_number: int) -> str:
    index = max(line_number - 1, 0)
    for cursor in range(min(index, len(lines) - 1), -1, -1):
        heading = _markdown_heading(lines[cursor])
        if heading is not None:
            return heading[1]
    for cursor in range(index, len(lines)):
        heading = _markdown_heading(lines[cursor])
        if heading is not None:
            return heading[1]
    return "Document"


def _normative_change_count(removed: list[str], added: list[str]) -> int:
    removed_normative = sum(1 for line in removed if _is_normative_line(line))
    added_normative = sum(1 for line in added if _is_normative_line(line))
    return max(removed_normative, added_normative)


def _markdown_heading(line: str) -> tuple[int, str] | None:
    match = MARKDOWN_HEADING.match(line.strip())
    if match is None:
        return None
    return len(match.group("marker")), match.group("title").strip()


def _is_annotation_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("<!--") or stripped.lower().startswith(("note:", "> note:"))


def _is_normative_line(line: str) -> bool:
    return _markdown_heading(line) is None and NORMATIVE_MODAL.search(line) is not None


def _strip_line_end(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")
