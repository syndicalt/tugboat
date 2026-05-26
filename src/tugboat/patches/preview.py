from __future__ import annotations


def apply_unified_diff(base_text: str, diff: str) -> str | None:
    base_lines = base_text.splitlines(keepends=True)
    output: list[str] = []
    position = 0
    in_hunk = False

    for line in diff.splitlines(keepends=True):
        if not in_hunk and line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\"):
            continue

        marker = line[:1]
        body = line[1:]
        if marker in {" ", "-"}:
            aligned = _align_base_line(base_lines, position, body)
            if aligned is None:
                return None
            output.extend(base_lines[position:aligned])
            position = aligned
            if marker == " ":
                output.append(base_lines[position])
            position += 1
            continue
        if marker == "+":
            output.append(body)
            continue
        return None

    output.extend(base_lines[position:])
    return "".join(output)


def _align_base_line(base_lines: list[str], position: int, expected: str) -> int | None:
    for index in range(position, len(base_lines)):
        if base_lines[index] == expected:
            return index
    return None
