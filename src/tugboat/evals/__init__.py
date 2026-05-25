from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class StructuralFinding:
    code: str
    message: str
    severity: Severity
    target: str | None = None


@dataclass(frozen=True)
class StructuralEvalReport:
    passed: bool
    findings: tuple[StructuralFinding, ...]
    anchors_before: tuple[str, ...]
    anchors_after: tuple[str, ...]
    semantic_diff: str


@dataclass(frozen=True)
class EvalCaseRecord:
    case_id: str
    case_hash: str
    split_name: str


@dataclass(frozen=True)
class OfflineEvalReport:
    suite_id: str
    passed: bool
    metrics: dict[str, int]
    trigger_score: float
    held_out_score: float
    governance_passed: bool
    recommendation: str
    live_provider_required: bool = False
    eval_cases: tuple[EvalCaseRecord, ...] = ()
    validation_splits: dict[str, tuple[str, ...]] | None = None


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ANCHOR_WORD_RE = re.compile(r"[^a-z0-9 -]")
_ANCHOR_SPACE_RE = re.compile(r"[ -]+")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_NORMATIVE_WORDS = frozenset({"must", "required", "shall", "always", "never"})
_PERMISSIVE_WORDS = frozenset({"may", "optional", "skip", "skipped"})
_PATH_SUFFIXES = (
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".py",
    ".sh",
)


def evaluate_markdown_pair(
    before: str,
    after: str,
    *,
    root: Path | None = None,
) -> StructuralEvalReport:
    findings: list[StructuralFinding] = []
    anchors_before = _anchors(before)
    anchors_after = _anchors(after)

    if anchors_before != anchors_after:
        findings.append(
            StructuralFinding(
                code="anchor.changed",
                message="Markdown heading anchors changed.",
                severity=Severity.ERROR,
            )
        )

    findings.extend(_frontmatter_findings(before, after))
    findings.extend(_fence_findings(before, after))
    if root is not None:
        findings.extend(_local_path_findings(after, root))

    semantic_diff = _classify_semantic_diff(before, after)
    if semantic_diff == "normative_change":
        findings.append(
            StructuralFinding(
                code="semantic.normative_change",
                message="Candidate appears to change normative instruction strength.",
                severity=Severity.WARNING,
            )
        )

    return StructuralEvalReport(
        passed=not any(finding.severity is Severity.ERROR for finding in findings),
        findings=tuple(findings),
        anchors_before=anchors_before,
        anchors_after=anchors_after,
        semantic_diff=semantic_diff,
    )


def evaluate_markdown_candidate(markdown: str, *, root: Path | None = None) -> StructuralEvalReport:
    return evaluate_markdown_pair(markdown, markdown, root=root)


def run_offline_eval_suite(root: Path, *, suite_id: str) -> OfflineEvalReport:
    if suite_id != "all":
        raise ValueError("only offline suite 'all' is supported")

    policy_text = _read_optional(root / "CODEX.md") or _read_optional(root / "AGENTS.md") or ""
    structural = evaluate_markdown_candidate(policy_text, root=root)
    governance_regressions = int(_has_governance_regression(policy_text))
    fixture_metrics, fixture_cases = _run_fixture_cases(root)
    structural_case = EvalCaseRecord(
        case_id="structural:current-policy",
        case_hash=_text_hash(policy_text),
        split_name="trigger",
    )
    eval_cases = (structural_case, *fixture_cases)
    validation_splits = _validation_splits(eval_cases)
    behavioral_cases = max(
        1,
        fixture_metrics["incident_replay_cases"]
        + fixture_metrics["held_out_cases"]
        + fixture_metrics["cross_agent_cases"],
    )
    adversarial_cases = max(1, fixture_metrics["adversarial_cases"])
    structural_cases = 1
    passed = (
        structural.passed
        and governance_regressions == 0
        and fixture_metrics["fixture_case_failures"] == 0
    )
    metrics = {
        **fixture_metrics,
        "structural_cases": structural_cases,
        "behavioral_cases": behavioral_cases,
        "adversarial_cases": adversarial_cases,
        "governance_regressions": governance_regressions,
        "structural_findings": len(structural.findings),
    }
    trigger_score = 1.0 if structural.passed and governance_regressions == 0 else 0.0
    held_out_score = _category_score(
        passed_cases=fixture_metrics["held_out_passed"],
        total_cases=fixture_metrics["held_out_cases"],
        fallback=trigger_score,
    )
    return OfflineEvalReport(
        suite_id=suite_id,
        passed=passed,
        metrics=metrics,
        trigger_score=trigger_score if fixture_metrics["fixture_case_failures"] == 0 else 0.0,
        held_out_score=held_out_score,
        governance_passed=governance_regressions == 0,
        recommendation="accept" if passed else "reject",
        eval_cases=eval_cases,
        validation_splits=validation_splits,
    )


def _run_fixture_cases(root: Path) -> tuple[dict[str, int], tuple[EvalCaseRecord, ...]]:
    metrics = {
        "incident_replay_cases": 0,
        "held_out_cases": 0,
        "adversarial_cases": 0,
        "cross_agent_cases": 0,
        "held_out_passed": 0,
        "fixture_case_failures": 0,
    }
    cases: list[EvalCaseRecord] = []
    fixture_root = root / ".sidecar" / "evals"
    if not fixture_root.exists():
        return metrics, ()

    category_metric = {
        "incident_replay": "incident_replay_cases",
        "held_out": "held_out_cases",
        "adversarial": "adversarial_cases",
        "cross_agent": "cross_agent_cases",
    }
    for path in sorted(fixture_root.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"eval fixture must be a JSON object: {path}")
        if int(payload.get("schema_version", 0)) != 1:
            raise ValueError(f"unsupported eval fixture schema_version: {path}")
        category = str(payload["category"])
        metric = category_metric.get(category)
        if metric is None:
            raise ValueError(f"unknown eval fixture category: {category}")
        markdown = str(payload["markdown"])
        expected_passed = bool(payload["expected_passed"])
        actual_passed = evaluate_markdown_candidate(markdown, root=root).passed and not _has_governance_regression(markdown)
        cases.append(
            EvalCaseRecord(
                case_id=f"{category}:{path.stem}",
                case_hash=_json_hash(payload),
                split_name=_split_for_category(category),
            )
        )
        metrics[metric] += 1
        if category == "held_out" and actual_passed == expected_passed:
            metrics["held_out_passed"] += 1
        if actual_passed != expected_passed:
            metrics["fixture_case_failures"] += 1
    return metrics, tuple(cases)


def _validation_splits(cases: tuple[EvalCaseRecord, ...]) -> dict[str, tuple[str, ...]]:
    split_names = ("trigger", "held_out", "governance")
    return {
        split_name: tuple(sorted(case.case_id for case in cases if case.split_name == split_name))
        for split_name in split_names
    }


def _split_for_category(category: str) -> str:
    if category == "held_out":
        return "held_out"
    if category in {"adversarial", "cross_agent"}:
        return "governance"
    return "trigger"


def _json_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _category_score(*, passed_cases: int, total_cases: int, fallback: float) -> float:
    if total_cases == 0:
        return fallback
    return passed_cases / total_cases


def _anchors(markdown: str) -> tuple[str, ...]:
    anchors: list[str] = []
    used: dict[str, int] = {}
    in_fence = False
    fence_marker = ""

    for line in markdown.splitlines():
        stripped = line.lstrip()
        fence_match = _FENCE_RE.match(stripped)
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

        match = _HEADING_RE.match(line)
        if match:
            anchors.append(_dedupe_anchor(_anchor_for(match.group(2).strip()), used))

    return tuple(anchors)


def _read_optional(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _has_governance_regression(markdown: str) -> bool:
    words = set(_words(markdown))
    return "skip" in words and "tests" in words


def _frontmatter_findings(before: str, after: str) -> tuple[StructuralFinding, ...]:
    before_frontmatter = _frontmatter(before)
    after_frontmatter = _frontmatter(after)
    if before_frontmatter is not None and after_frontmatter is None:
        return (
            StructuralFinding(
                code="frontmatter.removed",
                message="YAML frontmatter was removed.",
                severity=Severity.ERROR,
            ),
        )
    if before_frontmatter is not None and before_frontmatter != after_frontmatter:
        return (
            StructuralFinding(
                code="frontmatter.changed",
                message="YAML frontmatter changed.",
                severity=Severity.ERROR,
            ),
        )
    return ()


def _frontmatter(markdown: str) -> str | None:
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[: index + 1])
    return None


def _fence_findings(before: str, after: str) -> tuple[StructuralFinding, ...]:
    findings: list[StructuralFinding] = []
    before_fences, before_unclosed = _fenced_blocks(before)
    after_fences, after_unclosed = _fenced_blocks(after)

    if before_unclosed or after_unclosed:
        findings.append(
            StructuralFinding(
                code="fence.unclosed",
                message="Markdown contains an unclosed fenced code block.",
                severity=Severity.ERROR,
            )
        )

    if before_fences != after_fences:
        findings.append(
            StructuralFinding(
                code="fence.changed",
                message="Fenced code blocks changed.",
                severity=Severity.ERROR,
            )
        )

    return tuple(findings)


def _fenced_blocks(markdown: str) -> tuple[tuple[str, ...], bool]:
    blocks: list[str] = []
    active: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
                active = [line]
                continue
            if marker == fence_marker:
                active.append(line)
                blocks.append("".join(active))
                active = []
                in_fence = False
                fence_marker = ""
                continue

        if in_fence:
            active.append(line)

    return tuple(blocks), in_fence


def _local_path_findings(markdown: str, root: Path) -> tuple[StructuralFinding, ...]:
    findings: list[StructuralFinding] = []
    seen: set[str] = set()

    for target in [*_markdown_link_targets(markdown), *_inline_path_targets(markdown)]:
        normalized = _normalize_local_target(target)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        if not (root / normalized).exists():
            findings.append(
                StructuralFinding(
                    code="link.local_missing",
                    message=f"Local link or path does not exist: {normalized}",
                    severity=Severity.ERROR,
                    target=normalized,
                )
            )

    return tuple(findings)


def _markdown_link_targets(markdown: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in _LINK_RE.finditer(markdown))


def _inline_path_targets(markdown: str) -> tuple[str, ...]:
    targets: list[str] = []
    for match in _CODE_SPAN_RE.finditer(markdown):
        value = match.group(1).strip()
        if "/" in value or value.endswith(_PATH_SUFFIXES):
            targets.append(value)
    return tuple(targets)


def _normalize_local_target(target: str) -> str | None:
    target = target.split()[0].strip("<>")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return None

    path = parsed.path
    if not path:
        return None
    return path.lstrip("/")


def _classify_semantic_diff(before: str, after: str) -> str:
    before_words = _words(before)
    after_words = _words(after)
    if before_words == after_words:
        return "unchanged"

    if _weakens_normative_language(before_words, after_words):
        return "normative_change"

    before_set = set(before_words)
    after_set = set(after_words)
    if before_set.issubset(after_set):
        return "additive_clarification"

    return "content_change"


def _weakens_normative_language(before_words: tuple[str, ...], after_words: tuple[str, ...]) -> bool:
    before_normative = bool(set(before_words) & _NORMATIVE_WORDS)
    after_permissive = bool(set(after_words) & _PERMISSIVE_WORDS)
    return before_normative and after_permissive


def _words(markdown: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z]+", markdown.lower()))


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
