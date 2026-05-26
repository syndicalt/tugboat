from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from tugboat.patches import apply_unified_diff

from tugboat.models import Policy


DENIAL_REASON_ORDER = (
    "base_hash_mismatch",
    "base_file_outside_repo",
    "base_file_not_allowed",
    "pending_eval_definition_edit",
    "higher_priority_contradiction",
    "max_changed_lines_exceeded",
    "risk_class_changed_lines_exceeded",
    "markdown_parse_invalid",
    "unbalanced_markdown_fence",
    "frontmatter_removed",
    "protected_heading_changed",
    "governance_constraint_removed",
    "modal_weakening",
    "new_external_endpoint",
    "single_untrusted_source",
    "prohibited_risk_class",
)
PROHIBITED_RISK_CLASSES = frozenset(
    {
        "class_d",
        "d",
        "arbitrary_repo_plugin_loading",
        "approval_policy_self_apply",
        "audit_history_edit",
        "direct_instruction_mutation",
        "credential_exposure",
        "external_network",
        "higher_priority_policy_weakening",
        "pending_eval_definition_bypass",
        "secret_exposure",
        "untrusted_trace_policy_adoption",
        "vcs_apply",
    }
)
RESTRICTED_RISK_CLASSES = frozenset(
    {
        "approval_requirements",
        "class_c",
        "c",
        "deployment_behavior",
        "memory_behavior",
        "model_provider_routing",
        "network_access",
        "restricted_policy_change",
        "sandbox_behavior",
        "secrets_handling",
        "security_incident_response",
        "sidecar_authority",
        "tool_permissions",
    }
)
STRONG_MODALS = re.compile(r"\b(must|never|required|shall)\b", re.IGNORECASE)
WEAK_MODALS = re.compile(r"\b(should|may|can|could|optional|recommend)\b", re.IGNORECASE)
EXTERNAL_ENDPOINT = re.compile(r"https?://[^\s)>\"]+", re.IGNORECASE)
FENCE_START = re.compile(r"^[ \t]*(`{3,}|~{3,})")
MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
GOVERNANCE_TERMS = frozenset(
    {
        "approval",
        "sandbox",
        "test",
        "review",
        "secret",
        "secrets",
        "memory",
        "network",
        "deploy",
        "permission",
    }
)
PROTECTED_HEADING_TERMS = frozenset(
    {
        "approval",
        "constraint",
        "credential",
        "deploy",
        "governance",
        "memory",
        "network",
        "operating constraint",
        "permission",
        "policy",
        "sandbox",
        "secret",
        "security",
    }
)
NON_GOVERNANCE_FORBIDDEN_TERMS = frozenset({"must", "never", "required", "shall"})


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    trusted: bool = False


@dataclass(frozen=True)
class CandidatePatch:
    audit_id: int
    base_file: str
    base_hash: str
    diff: str
    risk_class: str
    rationale: str
    sources: tuple[SourceRef, ...] = ()
    pending_audit_eval_definition_paths: tuple[str, ...] = ()
    bounded_edit_metadata: tuple[dict[str, object], ...] = ()

    @staticmethod
    def hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def diff_hash(self) -> str:
        return self.hash_text(self.diff)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "audit_id": self.audit_id,
            "base_file": self.base_file,
            "base_hash": self.base_hash,
            "diff_hash": self.diff_hash,
            "rationale": self.rationale,
            "risk_class": self.risk_class,
            "sources": [
                {"source_id": source.source_id, "trusted": source.trusted}
                for source in self.sources
            ],
        }
        if self.pending_audit_eval_definition_paths:
            payload["pending_audit_eval_definition_paths"] = list(
                self.pending_audit_eval_definition_paths
            )
        if self.bounded_edit_metadata:
            payload["bounded_edit_metadata"] = list(self.bounded_edit_metadata)
        return payload


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]
    review_required_reasons: tuple[str, ...] = ()
    auto_apply_eligible: bool = False


def evaluate_candidate(repo: Path, policy: Policy, candidate: CandidatePatch) -> PolicyDecision:
    found_reasons: set[str] = set()
    review_required_reasons: set[str] = set()
    repo_root = repo.resolve()
    base_path = (repo / candidate.base_file).resolve()
    if not _is_relative_to(base_path, repo_root):
        found_reasons.add("base_file_outside_repo")
    if not _is_allowed_base_file(candidate.base_file, policy):
        found_reasons.add("base_file_not_allowed")
    if _is_pending_eval_definition_edit(candidate.base_file, candidate):
        found_reasons.add("pending_eval_definition_edit")
    if _has_higher_priority_contradiction(repo, policy, candidate):
        found_reasons.add("higher_priority_contradiction")
    if not base_path.exists() or CandidatePatch.hash_file(base_path) != candidate.base_hash:
        found_reasons.add("base_hash_mismatch")
    changed_line_count = _changed_line_count(candidate.diff)
    if changed_line_count > policy.auto_apply_max_changed_lines:
        found_reasons.add("max_changed_lines_exceeded")
    if _is_markdown_file(candidate.base_file) and _is_relative_to(base_path, repo_root):
        found_reasons.update(_markdown_validation_reasons(base_path, candidate.diff))
        if _is_protected_instruction_file(candidate.base_file, policy):
            found_reasons.update(_protected_heading_reasons(base_path, candidate.diff))
    has_modal_weakening = _has_modal_weakening(candidate.diff)
    if _removes_governance_constraint(candidate.diff, policy) and not has_modal_weakening:
        found_reasons.add("governance_constraint_removed")
    if has_modal_weakening:
        found_reasons.add("modal_weakening")
    if _has_new_external_endpoint(candidate.diff):
        found_reasons.add("new_external_endpoint")
    if len(candidate.sources) == 1 and not candidate.sources[0].trusted:
        found_reasons.add("single_untrusted_source")
    risk_class = _risk_class_key(candidate.risk_class)
    risk_class_budget = _risk_class_changed_line_budget(policy, risk_class)
    if risk_class_budget is not None and changed_line_count > risk_class_budget:
        found_reasons.add("risk_class_changed_lines_exceeded")
    if risk_class in PROHIBITED_RISK_CLASSES:
        found_reasons.add("prohibited_risk_class")
    if risk_class in {"b", "class_b"}:
        review_required_reasons.add("class_b_review_required")
    if risk_class in RESTRICTED_RISK_CLASSES:
        review_required_reasons.add("class_c_explicit_human_review_required")
    reasons = tuple(reason for reason in DENIAL_REASON_ORDER if reason in found_reasons)
    review_reasons = tuple(
        reason
        for reason in (
            "class_b_review_required",
            "class_c_explicit_human_review_required",
        )
        if reason in review_required_reasons
    )
    return PolicyDecision(
        allowed=not reasons,
        reasons=reasons,
        review_required_reasons=review_reasons,
        auto_apply_eligible=not reasons and policy.auto_apply_enabled and risk_class in {"a", "class_a"},
    )


def _risk_class_key(risk_class: str) -> str:
    return risk_class.strip().lower().replace("-", "_").replace(" ", "_")


def _risk_class_changed_line_budget(policy: Policy, risk_class: str) -> int | None:
    for configured_class, budget in policy.risk_class_changed_line_budgets.items():
        if _risk_class_key(configured_class) == risk_class:
            return budget
    return None


def _has_modal_weakening(diff: str) -> bool:
    removed = [_diff_body(line) for line in diff.splitlines() if _is_removed_line(line)]
    added = [_diff_body(line) for line in diff.splitlines() if _is_added_line(line)]
    removed_strong = any(STRONG_MODALS.search(line) for line in removed)
    if not removed_strong:
        return False
    if not added:
        return True
    return any(WEAK_MODALS.search(line) for line in added)


def _has_new_external_endpoint(diff: str) -> bool:
    removed_endpoints = {
        endpoint
        for line in diff.splitlines()
        if _is_removed_line(line)
        for endpoint in EXTERNAL_ENDPOINT.findall(line)
    }
    added_endpoints = {
        endpoint
        for line in diff.splitlines()
        if _is_added_line(line)
        for endpoint in EXTERNAL_ENDPOINT.findall(line)
    }
    return bool(added_endpoints - removed_endpoints)


def _markdown_validation_reasons(base_path: Path, diff: str) -> set[str]:
    if not base_path.exists():
        return set()

    base_text = base_path.read_text(encoding="utf-8")
    preview = apply_unified_diff(base_text, diff)
    if preview is None:
        return {"markdown_parse_invalid"}

    reasons: set[str] = set()
    if _has_invalid_markdown_text(preview):
        reasons.add("markdown_parse_invalid")
    if _has_unbalanced_fenced_block(preview) and not _has_unbalanced_fenced_block(base_text):
        reasons.add("unbalanced_markdown_fence")
    if _has_yaml_frontmatter(base_text) and not _has_yaml_frontmatter(preview):
        reasons.add("frontmatter_removed")
    return reasons


def _has_invalid_markdown_text(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return CONTROL_CHARS.search(text) is not None


def _has_unbalanced_fenced_block(text: str) -> bool:
    fence_char = ""
    fence_length = 0

    for line in text.splitlines():
        match = FENCE_START.match(line)
        if not match:
            continue
        marker = match.group(1)
        marker_char = marker[0]
        if not fence_char:
            fence_char = marker_char
            fence_length = len(marker)
            continue
        if marker_char == fence_char and len(marker) >= fence_length:
            fence_char = ""
            fence_length = 0

    return bool(fence_char)


def _has_yaml_frontmatter(text: str) -> bool:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def _protected_heading_reasons(base_path: Path, diff: str) -> set[str]:
    if not base_path.exists():
        return set()

    base_text = base_path.read_text(encoding="utf-8")
    preview = apply_unified_diff(base_text, diff)
    if preview is None:
        return set()
    if _protected_heading_sections_changed(base_text, preview):
        return {"protected_heading_changed"}
    return set()


def _protected_heading_sections_changed(base_text: str, preview: str) -> bool:
    base_sections = _markdown_heading_sections(base_text)
    if not base_sections:
        return False

    preview_sections = _markdown_heading_sections(preview)
    if len(preview_sections) < len(base_sections):
        return True

    return any(
        _is_protected_heading_path(base_section[0])
        and (index >= len(preview_sections) or preview_sections[index] != base_section)
        for index, base_section in enumerate(base_sections)
    )


def _is_protected_heading_path(heading_path: tuple[str, ...]) -> bool:
    heading_text = " / ".join(heading_path).lower()
    return any(term in heading_text for term in PROTECTED_HEADING_TERMS)


def _markdown_heading_sections(text: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    headings = _find_markdown_headings(text)
    sections: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []

    for index, (start, level, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        stack = [(existing_level, name) for existing_level, name in stack if existing_level < level]
        stack.append((level, heading))
        sections.append(
            (
                tuple(name for _, name in stack),
                CandidatePatch.hash_text(text[start:end]),
            )
        )

    return tuple(sections)


def _find_markdown_headings(text: str) -> list[tuple[int, int, str]]:
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
            match = MARKDOWN_HEADING.match(line.rstrip("\r\n"))
            if match:
                headings.append((char_offset, len(match.group(1)), match.group(2).strip()))

        char_offset += len(line)

    return headings


def _removes_governance_constraint(diff: str, policy: Policy) -> bool:
    protected_terms = _governance_terms(policy)
    removed_terms = {
        term
        for line in diff.splitlines()
        if _is_removed_line(line)
        for term in _line_governance_terms(_diff_body(line), protected_terms)
    }
    if not removed_terms:
        return False

    added_terms = {
        term
        for line in diff.splitlines()
        if _is_added_line(line)
        for term in _line_governance_terms(_diff_body(line), protected_terms)
    }
    return bool(removed_terms - added_terms)


def _governance_terms(policy: Policy) -> frozenset[str]:
    configured_terms = {
        term.lower()
        for term in policy.forbidden_terms
        if term.lower() not in NON_GOVERNANCE_FORBIDDEN_TERMS
    }
    return frozenset(configured_terms | GOVERNANCE_TERMS)


def _line_governance_terms(line: str, terms: frozenset[str]) -> set[str]:
    return {term for term in terms if _contains_governance_term(line, term)}


def _contains_governance_term(line: str, term: str) -> bool:
    if term in {"secret", "secrets"}:
        pattern = r"\bsecrets?\b"
    elif term == "review":
        pattern = r"\breview\w*\b"
    elif term == "deploy":
        pattern = r"\bdeploy\w*\b"
    elif term == "sandbox":
        pattern = r"\bsandbox\w*\b"
    elif term == "test":
        pattern = r"\btest\w*\b"
    else:
        pattern = rf"\b{re.escape(term)}s?\b"
    return re.search(pattern, line, re.IGNORECASE) is not None


def _is_removed_line(line: str) -> bool:
    return line.startswith("-") and not line.startswith("---")


def _is_added_line(line: str) -> bool:
    return line.startswith("+") and not line.startswith("+++")


def _diff_body(line: str) -> str:
    return line[1:] if line else line


def _changed_line_count(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if _is_added_line(line) or _is_removed_line(line))


def _is_markdown_file(path: str) -> bool:
    return Path(path).suffix.lower() in {".md", ".markdown"}


def _is_allowed_base_file(base_file: str, policy: Policy) -> bool:
    entries = policy.instruction_files
    allowed = {entry.path for entry in entries} or {
        "AGENTS.md",
        "CODEX.md",
        "CLAUDE.md",
        "SKILL.md",
    }
    normalized_base = _repo_relative_posix(base_file)
    return any(
        fnmatch.fnmatchcase(normalized_base, _repo_relative_posix(pattern))
        for pattern in allowed
    )


def _is_protected_instruction_file(base_file: str, policy: Policy) -> bool:
    normalized_base = _repo_relative_posix(base_file)
    return any(
        entry.protected
        and fnmatch.fnmatchcase(normalized_base, _repo_relative_posix(entry.path))
        for entry in policy.instruction_files
    )


def _is_pending_eval_definition_edit(base_file: str, candidate: CandidatePatch) -> bool:
    normalized_base = _repo_relative_posix(base_file)
    return any(
        fnmatch.fnmatchcase(normalized_base, _repo_relative_posix(pattern))
        for pattern in candidate.pending_audit_eval_definition_paths
    )


def _has_higher_priority_contradiction(
    repo: Path,
    policy: Policy,
    candidate: CandidatePatch,
) -> bool:
    candidate_entry = next(
        (entry for entry in policy.instruction_files if entry.path == candidate.base_file),
        None,
    )
    if candidate_entry is None:
        return False
    higher_priority_rules = tuple(
        _modal_rule(line)
        for entry in policy.instruction_files
        if entry.precedence > candidate_entry.precedence
        for line in _read_policy_lines(repo / entry.path)
    )
    if not higher_priority_rules:
        return False
    added_rules = tuple(
        rule
        for line in candidate.diff.splitlines()
        if _is_added_line(line)
        for rule in (_modal_rule(_diff_body(line)),)
        if rule is not None
    )
    return any(
        added is not None
        and existing is not None
        and added[0] == existing[0]
        and added[1] != existing[1]
        for added in added_rules
        for existing in higher_priority_rules
    )


def _read_policy_lines(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        return ()
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _modal_rule(line: str) -> tuple[str, str] | None:
    normalized = line.strip().lower()
    if not normalized:
        return None
    strong_match = STRONG_MODALS.search(normalized)
    weak_match = WEAK_MODALS.search(normalized)
    if strong_match is None and weak_match is None:
        return None
    strength = "strong" if strong_match is not None else "weak"
    topic = re.sub(r"\b(must|never|required|shall|should|may|can|could|optional|recommend)\b", "", normalized)
    topic = re.sub(r"\b(agents?|the|a|an|run|skip)\b", "", topic)
    topic = re.sub(r"\s+", " ", topic).strip(" .")
    return (topic, strength) if topic else None


def _repo_relative_posix(path: str) -> str:
    return Path(path).as_posix().lstrip("/")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
