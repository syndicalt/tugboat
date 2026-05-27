from __future__ import annotations

import re


HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
OLD_FILE_HEADER = re.compile(r"^--- (?P<path>\S+)\n?$")
NEW_FILE_HEADER = re.compile(r"^\+\+\+ (?P<path>\S+)\n?$")


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
    def __init__(self, old_start: int, old_count: int, new_count: int):
        self.old_start = old_start
        self.old_count = old_count
        self.new_count = new_count
        self.old_seen = 0
        self.new_seen = 0


def _parse_ranged_hunk_header(line: str) -> _RangedHunk | None:
    match = HUNK_HEADER.match(line)
    if match is None:
        return None
    old_start = int(match.group("old_start"))
    old_count = int(match.group("old_count") or "1")
    new_count = int(match.group("new_count") or "1")
    return _RangedHunk(old_start=old_start, old_count=old_count, new_count=new_count)


def _ranged_hunk_counts_match(hunk: _RangedHunk) -> bool:
    return hunk.old_seen == hunk.old_count and hunk.new_seen == hunk.new_count
