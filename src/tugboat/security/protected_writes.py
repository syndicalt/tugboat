from __future__ import annotations

from dataclasses import dataclass


class ProtectedWriteError(ValueError):
    pass


@dataclass(frozen=True)
class DiffTargetGuard:
    allowed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = tuple(_repo_relative_posix(path) for path in self.allowed_paths)
        if not normalized:
            raise ProtectedWriteError("diff target allowlist is empty")
        object.__setattr__(self, "allowed_paths", normalized)

    def validate(self, diff_text: str) -> None:
        touched_paths = diff_touched_paths(diff_text)
        if not touched_paths:
            raise ProtectedWriteError("diff does not declare any target files")
        unexpected = tuple(sorted(touched_paths - set(self.allowed_paths)))
        if unexpected:
            raise ProtectedWriteError(
                "diff targets are not allowed: " + ", ".join(unexpected)
            )


def diff_touched_paths(diff_text: str) -> set[str]:
    touched: set[str] = set()
    pending_old_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            pending_old_path = _normalize_diff_path(line[4:].strip())
            continue
        if line.startswith("+++ "):
            new_path = _normalize_diff_path(line[4:].strip())
            if new_path is not None:
                touched.add(new_path)
            elif pending_old_path is not None:
                touched.add(pending_old_path)
            pending_old_path = None
    return touched


def _normalize_diff_path(value: str) -> str | None:
    path = value.split("\t", 1)[0].split(" ", 1)[0]
    if path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return _repo_relative_posix(path)


def _repo_relative_posix(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


__all__ = ["DiffTargetGuard", "ProtectedWriteError", "diff_touched_paths"]
