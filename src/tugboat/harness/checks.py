from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


INSTRUCTION_FILES = ("AGENTS.md", "CODEX.md", "CLAUDE.md", "SKILL.md")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class HarnessCheckResult:
    passed: bool
    findings: list[str]


@dataclass(frozen=True)
class HarnessReport:
    knowledge_map: dict[str, list[str]]
    missing_docs: list[str]
    stale_docs: list[str]
    orphaned_runbooks: list[str]
    recurring_failures_without_docs: list[str]
    doc_gardening_tasks: list[str]


@dataclass(frozen=True)
class CleanupCandidate:
    candidate_id: str
    task: str
    source_findings: list[str]
    risk_class: str = "review_required"
    auto_apply: bool = False
    required_eval_suites: tuple[str, ...] = ("structural",)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "risk_class": self.risk_class,
            "auto_apply": self.auto_apply,
            "task": self.task,
            "source_findings": list(self.source_findings),
            "required_eval_suites": list(self.required_eval_suites),
        }


def check_harness_legibility(
    repo: Path,
    max_instruction_lines: int = 100,
    max_must_count: int = 8,
) -> HarnessCheckResult:
    repo = repo.resolve()
    findings: list[str] = []
    rule_counts: dict[str, int] = {}
    must_rules: set[str] = set()
    never_rules: set[str] = set()

    for relative_path in INSTRUCTION_FILES:
        path = repo / relative_path
        if not path.exists():
            continue

        text = path.read_text(encoding="utf-8")
        line_count = len(text.splitlines())
        is_monolithic = line_count > max_instruction_lines
        if is_monolithic:
            findings.append(
                f"{relative_path} has {line_count} instruction lines; keep it at or below "
                f"{max_instruction_lines} and move detail into repo-local markdown references."
            )
        must_count = _must_count(text)
        if must_count > max_must_count:
            findings.append(
                f"{relative_path} has {must_count} MUST-level rules; keep MUST density at or below "
                f"{max_must_count}."
            )
        _collect_rules(text, rule_counts, must_rules, never_rules)

        local_markdown_refs = _repo_local_markdown_refs(text)
        if not local_markdown_refs:
            if not is_monolithic:
                findings.append(
                    f"{relative_path} has no repo-local markdown references; keep instruction files "
                    "as short maps to deeper docs."
                )
            continue

        for ref in local_markdown_refs:
            target = (path.parent / ref).resolve()
            if not _is_relative_to(target, repo):
                findings.append(
                    f"{relative_path} references markdown file outside the repo: {ref.as_posix()}."
                )
                continue

            if not target.is_file():
                findings.append(
                    f"{relative_path} references missing repo-local markdown file {ref.as_posix()}."
                )
                continue
            findings.extend(_metadata_findings(repo, target))

    for normalized_rule, count in sorted(rule_counts.items()):
        if count > 1:
            readable_rule = re.sub(r"^(MUST|NEVER)\s+", "", normalized_rule, flags=re.IGNORECASE)
            findings.append(f"Duplicate instruction rule appears {count} times: {readable_rule}.")
    for rule in sorted(must_rules & never_rules):
        findings.append(f"Conflicting instruction rules: MUST {rule}. vs NEVER {rule}.")

    return HarnessCheckResult(passed=not findings, findings=findings)


def generate_harness_report(repo: Path) -> HarnessReport:
    repo = repo.resolve()
    knowledge_map: dict[str, list[str]] = {}
    missing_docs: set[str] = set()
    stale_docs: list[str] = []
    referenced_docs: set[str] = set()
    doc_gardening_tasks: list[str] = []

    for relative_path in INSTRUCTION_FILES:
        path = repo / relative_path
        if not path.exists():
            continue
        refs = sorted({ref.as_posix() for ref in _repo_local_markdown_refs(path.read_text(encoding="utf-8"))})
        if refs:
            knowledge_map[relative_path] = refs
        for ref in refs:
            referenced_docs.add(ref)
            target = (path.parent / ref).resolve()
            if not _is_relative_to(target, repo) or not target.is_file():
                missing_docs.add(ref)
                doc_gardening_tasks.append(f"Add or fix {ref} referenced by {relative_path}.")
                continue
            for finding in _metadata_findings(repo, target):
                stale_docs.append(finding)
                if "ownership metadata" in finding:
                    doc_gardening_tasks.append(f"Add ownership metadata to {ref}.")
                if "verification-status metadata" in finding:
                    doc_gardening_tasks.append(f"Add verification-status metadata to {ref}.")

    orphaned_runbooks = [
        path.relative_to(repo).as_posix()
        for path in sorted((repo / "docs").rglob("*.md")) if (repo / "docs").is_dir()
        if path.relative_to(repo).as_posix() not in referenced_docs
    ]
    for orphan in orphaned_runbooks:
        doc_gardening_tasks.append(
            f"Either reference {orphan} from an instruction map or remove/archive it."
        )
    recurring_failures_without_docs = _recurring_failures_without_docs(repo)
    for failure in recurring_failures_without_docs:
        doc_gardening_tasks.append(f"Document recurring failure {failure}")

    return HarnessReport(
        knowledge_map=knowledge_map,
        missing_docs=sorted(missing_docs),
        stale_docs=_dedupe(stale_docs),
        orphaned_runbooks=orphaned_runbooks,
        recurring_failures_without_docs=recurring_failures_without_docs,
        doc_gardening_tasks=_dedupe(doc_gardening_tasks),
    )


def generate_cleanup_candidates(repo: Path) -> list[CleanupCandidate]:
    report = generate_harness_report(repo)
    finding_by_task = _finding_by_cleanup_task(report)
    candidates: list[CleanupCandidate] = []
    for index, task in enumerate(report.doc_gardening_tasks, start=1):
        candidates.append(
            CleanupCandidate(
                candidate_id=f"harness-cleanup-{index}",
                task=task,
                source_findings=finding_by_task.get(task, [task]),
            )
        )
    return candidates


def _repo_local_markdown_refs(text: str) -> list[Path]:
    refs: list[Path] = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        raw_target = match.group(1).strip()
        target = _link_destination(raw_target)
        if target is None:
            continue

        path = Path(target)
        if path.suffix.lower() == ".md":
            refs.append(path)

    return refs


def _link_destination(raw_target: str) -> str | None:
    if not raw_target or raw_target.startswith("#"):
        return None

    first_token = raw_target.split()[0]
    target = first_token.split("#", 1)[0].split("?", 1)[0]
    if not target or "://" in target or ":" in target or target.startswith("/"):
        return None

    return target


def _metadata_findings(repo: Path, path: Path) -> list[str]:
    relative_path = path.relative_to(repo).as_posix()
    frontmatter = _frontmatter(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    if "owner" not in frontmatter:
        findings.append(f"{relative_path} is missing ownership metadata.")
    if "verification_status" not in frontmatter and "verification-status" not in frontmatter:
        findings.append(f"{relative_path} is missing verification-status metadata.")
    return findings


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return metadata
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    return {}


def _must_count(text: str) -> int:
    return len(re.findall(r"\bMUST\b", text, flags=re.IGNORECASE))


def _collect_rules(
    text: str,
    rule_counts: dict[str, int],
    must_rules: set[str],
    never_rules: set[str],
) -> None:
    for line in text.splitlines():
        normalized = line.strip().lstrip("-*0123456789. ").strip()
        match = re.match(r"^(MUST|NEVER)\s+(.+)$", normalized, flags=re.IGNORECASE)
        if not match:
            continue
        modal = match.group(1).upper()
        rule = match.group(2).rstrip(".").strip().lower()
        if not rule:
            continue
        counted_rule = f"{modal} {rule}"
        rule_counts[counted_rule] = rule_counts.get(counted_rule, 0) + 1
        if modal == "MUST":
            must_rules.add(rule)
        else:
            never_rules.add(rule)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _recurring_failures_without_docs(repo: Path) -> list[str]:
    path = repo / ".sidecar" / "recurring-failures.json"
    if not path.exists():
        return []
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("recurring-failures.json must contain a JSON object")
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("unsupported recurring-failures.json schema_version")
    failures = payload.get("failures", [])
    if not isinstance(failures, list):
        raise ValueError("recurring-failures.json failures must be a list")
    missing: list[str] = []
    for item in failures:
        if not isinstance(item, dict):
            raise ValueError("recurring failure entries must be JSON objects")
        failure_id = str(item.get("failure_id", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if not failure_id or not summary:
            raise ValueError("recurring failure entries require failure_id and summary")
        doc_ref = str(item.get("doc_ref", "")).strip()
        if doc_ref and _doc_ref_exists(repo, doc_ref):
            continue
        missing.append(f"{failure_id}: {summary}")
    return missing


def _doc_ref_exists(repo: Path, doc_ref: str) -> bool:
    path = (repo / doc_ref).resolve()
    return _is_relative_to(path, repo) and path.is_file() and path.suffix.lower() == ".md"


def _finding_by_cleanup_task(report: HarnessReport) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for missing in report.missing_docs:
        prefix = f"Add or fix {missing} referenced by "
        for task in report.doc_gardening_tasks:
            if task.startswith(prefix):
                mapping[task] = [f"{missing} is missing or stale."]
    for finding in report.stale_docs:
        path = finding.split(" is missing ", 1)[0]
        if "ownership metadata" in finding:
            mapping[f"Add ownership metadata to {path}."] = [finding]
        if "verification-status metadata" in finding:
            mapping[f"Add verification-status metadata to {path}."] = [finding]
    for orphan in report.orphaned_runbooks:
        task = f"Either reference {orphan} from an instruction map or remove/archive it."
        mapping[task] = [f"{orphan} is not referenced by any instruction map."]
    for failure in report.recurring_failures_without_docs:
        mapping[f"Document recurring failure {failure}"] = [failure]
    return mapping


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
