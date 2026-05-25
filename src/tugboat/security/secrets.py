from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("ghp_token", re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line_number: int
    kind: str


class SecretScanError(ValueError):
    def __init__(self, findings: tuple[SecretFinding, ...]):
        self.findings = findings
        summary = ", ".join(f"{finding.path}:{finding.line_number}:{finding.kind}" for finding in findings)
        super().__init__(f"secret scan failed: {summary}")


def scan_text(path: str, text: str) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(SecretFinding(path=path, line_number=line_number, kind=kind))
    return tuple(findings)


def scan_path(path: Path) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    paths = sorted(candidate for candidate in path.rglob("*") if candidate.is_file()) if path.is_dir() else [path]
    for candidate in paths:
        text = _read_text_or_none(candidate)
        if text is None:
            continue
        findings.extend(scan_text(candidate.as_posix(), text))
    result = tuple(findings)
    if result:
        raise SecretScanError(result)
    return result


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
