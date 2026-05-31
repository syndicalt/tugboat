from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

import yaml

from tugboat.config import load_policy
from tugboat.corpus.indexer import instruction_paths


MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_PATTERN = re.compile(r"^[ \t]*(```|~~~)")
ANCHOR_WORD_PATTERN = re.compile(r"[^a-z0-9 -]")
ANCHOR_SPACE_PATTERN = re.compile(r"[ -]+")


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
    token_metrics: dict[str, object]


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

    for path in _configured_instruction_paths(repo):
        relative_path = path.relative_to(repo).as_posix()

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

        for ref in _repo_local_file_refs(text):
            findings.extend(_file_ref_findings(repo, path, ref))

        local_markdown_refs = _repo_local_markdown_refs(text)
        if not local_markdown_refs:
            if not is_monolithic:
                findings.append(
                    f"{relative_path} has no repo-local markdown references; keep instruction files "
                    "as short maps to deeper docs."
                )
            continue

        for ref in local_markdown_refs:
            findings.extend(_markdown_ref_findings(repo, path, ref))
            target = (path.parent / ref.path).resolve()
            if _is_relative_to(target, repo) and target.is_file():
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

    for path in _configured_instruction_paths(repo):
        relative_path = path.relative_to(repo).as_posix()
        markdown_refs = sorted(
            _repo_local_markdown_refs(path.read_text(encoding="utf-8")),
            key=lambda ref: ref.path.as_posix(),
        )
        refs = sorted(
            {ref.path.as_posix() for ref in markdown_refs}
        )
        if refs:
            knowledge_map[relative_path] = refs
        for file_ref in _repo_local_file_refs(path.read_text(encoding="utf-8")):
            for finding in _file_ref_findings(repo, path, file_ref):
                stale_docs.append(finding)
                missing_file_match = re.match(
                    r"(.+) references missing repo-local file (.+)\.", finding
                )
                if missing_file_match:
                    doc_path, missing_ref = missing_file_match.groups()
                    doc_gardening_tasks.append(f"Add or fix {missing_ref} referenced by {doc_path}.")
        for markdown_ref in markdown_refs:
            ref_path = markdown_ref.path.as_posix()
            referenced_docs.add(ref_path)
            target = (path.parent / markdown_ref.path).resolve()
            if not _is_relative_to(target, repo) or not target.is_file():
                missing_docs.add(ref_path)
                doc_gardening_tasks.append(f"Add or fix {ref_path} referenced by {relative_path}.")
                continue
            for finding in _markdown_ref_findings(repo, path, markdown_ref):
                stale_docs.append(finding)
                missing_anchor_match = re.match(
                    r"(.+) references missing repo-local markdown anchor (.+)\.", finding
                )
                if missing_anchor_match:
                    doc_path, missing_ref = missing_anchor_match.groups()
                    doc_gardening_tasks.append(f"Add or fix {missing_ref} referenced by {doc_path}.")
            for finding in _metadata_findings(repo, target):
                stale_docs.append(finding)
                if "ownership metadata" in finding:
                    doc_gardening_tasks.append(f"Add ownership metadata to {ref_path}.")
                if "verification-status metadata" in finding:
                    doc_gardening_tasks.append(f"Add verification-status metadata to {ref_path}.")
                missing_link_match = re.match(
                    r"(.+) references missing repo-local markdown file (.+)\.", finding
                )
                if missing_link_match:
                    doc_path, missing_ref = missing_link_match.groups()
                    doc_gardening_tasks.append(f"Add or fix {missing_ref} referenced by {doc_path}.")
                missing_file_match = re.match(
                    r"(.+) references missing repo-local file (.+)\.", finding
                )
                if missing_file_match:
                    doc_path, missing_ref = missing_file_match.groups()
                    doc_gardening_tasks.append(f"Add or fix {missing_ref} referenced by {doc_path}.")
                freshness_match = re.match(r"(.+) is older than source file (.+)\.", finding)
                if freshness_match:
                    doc_path, source_ref = freshness_match.groups()
                    doc_gardening_tasks.append(f"Refresh {doc_path} from {source_ref}.")

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
        token_metrics=_token_metrics(repo, knowledge_map),
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


@dataclass(frozen=True)
class MarkdownRef:
    path: Path
    anchor: str


def _repo_local_markdown_refs(text: str) -> list[MarkdownRef]:
    refs: list[MarkdownRef] = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        raw_target = match.group(1).strip()
        target = _link_destination(raw_target)
        if target is None:
            continue

        path = Path(target.path)
        if path.suffix.lower() == ".md":
            refs.append(MarkdownRef(path=path, anchor=target.anchor))

    return refs


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def _token_metrics(repo: Path, knowledge_map: dict[str, list[str]]) -> dict[str, object]:
    instruction_files: list[dict[str, object]] = []
    instruction_total = 0
    active_files: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    must_rules: set[str] = set()
    never_rules: set[str] = set()

    for path in _configured_instruction_paths(repo):
        relative_path = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8")
        estimated_tokens = _estimated_tokens(text)
        instruction_total += estimated_tokens
        instruction_files.append(
            {
                "path": relative_path,
                "estimated_tokens": estimated_tokens,
                "line_count": len(text.splitlines()),
            }
        )
        active_files[relative_path] = estimated_tokens
        _collect_rules(text, rule_counts, must_rules, never_rules)

    for refs in knowledge_map.values():
        for ref in refs:
            target = (repo / ref).resolve()
            if not _is_relative_to(target, repo) or not target.is_file():
                continue
            active_files.setdefault(ref, _estimated_tokens(target.read_text(encoding="utf-8")))

    duplicate_rule_tokens = 0
    for normalized_rule, count in rule_counts.items():
        if count > 1:
            duplicate_rule_tokens += (count - 1) * _estimated_tokens(normalized_rule)

    active_context_files = [
        {"path": path, "estimated_tokens": active_files[path]}
        for path in sorted(active_files)
    ]
    return {
        "instruction_corpus_estimated_tokens": instruction_total,
        "active_context_estimated_tokens": sum(active_files.values()),
        "duplicate_rule_estimated_tokens": duplicate_rule_tokens,
        "instruction_files": sorted(instruction_files, key=lambda item: str(item["path"])),
        "active_context_files": active_context_files,
    }


def _estimated_tokens(text: str) -> int:
    return len(TOKEN_PATTERN.findall(text))


@dataclass(frozen=True)
class LinkDestination:
    path: str
    anchor: str


def _configured_instruction_paths(repo: Path) -> list[Path]:
    policy = load_policy(repo)
    seen: set[Path] = set()
    paths: list[Path] = []
    for path, _entry in sorted(
        instruction_paths(repo, policy.instruction_files),
        key=lambda item: item[0].relative_to(repo).as_posix(),
    ):
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _link_destination(raw_target: str) -> LinkDestination | None:
    if not raw_target or raw_target.startswith("#"):
        return None

    first_token = raw_target.split()[0]
    path_and_query, _, fragment = first_token.partition("#")
    target = path_and_query.split("?", 1)[0]
    if not target or "://" in target or ":" in target or target.startswith("/"):
        return None

    return LinkDestination(path=target, anchor=fragment)


def _metadata_findings(repo: Path, path: Path) -> list[str]:
    relative_path = path.relative_to(repo).as_posix()
    text = path.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)
    findings: list[str] = []
    if "owner" not in frontmatter:
        findings.append(f"{relative_path} is missing ownership metadata.")
    if "verification_status" not in frontmatter and "verification-status" not in frontmatter:
        findings.append(f"{relative_path} is missing verification-status metadata.")
    for source_ref in _source_file_refs(frontmatter):
        source_path = (repo / source_ref).resolve()
        if not _is_relative_to(source_path, repo.resolve()) or not source_path.is_file():
            findings.append(f"{relative_path} references missing source file {source_ref}.")
            continue
        if path.stat().st_mtime < source_path.stat().st_mtime:
            findings.append(f"{relative_path} is older than source file {source_ref}.")
    for ref in _repo_local_markdown_refs(text):
        findings.extend(_markdown_ref_findings(repo, path, ref))
    for ref in _repo_local_file_refs(text):
        findings.extend(_file_ref_findings(repo, path, ref))
    return findings


def _markdown_ref_findings(repo: Path, source_path: Path, ref: MarkdownRef) -> list[str]:
    relative_path = source_path.relative_to(repo).as_posix()
    target = (source_path.parent / ref.path).resolve()
    if not _is_relative_to(target, repo.resolve()):
        return [
            f"{relative_path} references markdown file outside the repo: {ref.path.as_posix()}."
        ]
    if not target.is_file():
        return [
            f"{relative_path} references missing repo-local markdown file {ref.path.as_posix()}."
        ]
    if ref.anchor and ref.anchor not in _markdown_anchors(target.read_text(encoding="utf-8")):
        return [
            f"{relative_path} references missing repo-local markdown anchor "
            f"{ref.path.as_posix()}#{ref.anchor}."
        ]
    return []


def _repo_local_file_refs(text: str) -> list[Path]:
    refs: list[Path] = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        raw_target = match.group(1).strip()
        target = _link_destination(raw_target)
        if target is None:
            continue

        path = Path(target.path)
        if path.suffix.lower() != ".md":
            refs.append(path)

    return refs


def _file_ref_findings(repo: Path, source_path: Path, ref: Path) -> list[str]:
    relative_path = source_path.relative_to(repo).as_posix()
    target = (source_path.parent / ref).resolve()
    if not _is_relative_to(target, repo.resolve()):
        return [f"{relative_path} references file outside the repo: {ref.as_posix()}."]
    if not target.is_file():
        return [f"{relative_path} references missing repo-local file {ref.as_posix()}."]
    return []


def _frontmatter(text: str) -> dict[str, object]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return {}
    raw_frontmatter = "\n".join(lines[1:closing_index])
    payload = yaml.safe_load(raw_frontmatter) if raw_frontmatter.strip() else {}
    if isinstance(payload, dict):
        return {str(key).strip(): value for key, value in payload.items() if str(key).strip()}
    return {}


def _source_file_refs(frontmatter: dict[str, object]) -> list[str]:
    raw = frontmatter.get("source_files") or frontmatter.get("source-files") or ""
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


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


def _markdown_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    used: dict[str, int] = {}
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        fence_match = FENCE_PATTERN.match(stripped)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        match = HEADING_PATTERN.match(line)
        if not match:
            continue
        anchor = _anchor_for(match.group(2).strip())
        count = used.get(anchor, 0)
        used[anchor] = count + 1
        anchors.add(anchor if count == 0 else f"{anchor}-{count}")
    return anchors


def _anchor_for(heading: str) -> str:
    anchor = heading.lower().strip()
    anchor = ANCHOR_WORD_PATTERN.sub("", anchor)
    return ANCHOR_SPACE_PATTERN.sub("-", anchor).strip("-")


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
        freshness_match = re.match(r"(.+) is older than source file (.+)\.", finding)
        if freshness_match:
            doc_path, source_ref = freshness_match.groups()
            mapping[f"Refresh {doc_path} from {source_ref}."] = [finding]
        missing_link_match = re.match(
            r"(.+) references missing repo-local markdown file (.+)\.", finding
        )
        if missing_link_match:
            doc_path, missing_ref = missing_link_match.groups()
            mapping[f"Add or fix {missing_ref} referenced by {doc_path}."] = [finding]
        missing_file_match = re.match(
            r"(.+) references missing repo-local file (.+)\.", finding
        )
        if missing_file_match:
            doc_path, missing_ref = missing_file_match.groups()
            mapping[f"Add or fix {missing_ref} referenced by {doc_path}."] = [finding]
        missing_anchor_match = re.match(
            r"(.+) references missing repo-local markdown anchor (.+)\.", finding
        )
        if missing_anchor_match:
            doc_path, missing_ref = missing_anchor_match.groups()
            mapping[f"Add or fix {missing_ref} referenced by {doc_path}."] = [finding]
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
