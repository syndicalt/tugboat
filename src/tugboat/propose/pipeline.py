from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from tugboat.artifacts import (
    BOUNDED_EDIT_OPERATORS,
    SCHEMA_VERSION,
    load_json_object_artifact,
    validate_json_artifact,
)
from tugboat.config import load_policy
from tugboat.db import Store
from tugboat.llmff.contracts import LlmffRunFailed
from tugboat.llmff.runner import inspect_manifest, run_manifest
from tugboat.manifests import (
    manifests_are_allowed_by_policy,
    materialize_manifests,
    require_manifest_contracts,
)
from tugboat.optimization import (
    LearningRateBudget,
    OptimizationMemory,
    budget_reasons_for_bounded_edit_metadata,
)
from tugboat.patches import apply_unified_diff, bounded_edit_metadata_mismatch_fields
from tugboat.paths import latest_run_dir, mark_private_file, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.propose.service import write_candidate
from tugboat.security.secrets import SecretScanError, scan_text


@dataclass(frozen=True)
class ProposePipelineResult:
    exit_code: int
    run_dir: Path
    message: str


def run_propose_pipeline(repo: Path, audit_ref: str) -> ProposePipelineResult:
    repo = repo.resolve()
    run_dir = _resolve_audit_run_dir(repo, audit_ref)
    audit = load_json_object_artifact(run_dir / "audit.json", "audit.json")
    validate_json_artifact("audit.json", audit)
    if not audit.get("edit_warranted", False):
        return ProposePipelineResult(1, run_dir, "audit does not warrant an instruction edit")
    if not (run_dir / "audit.raw.json").exists():
        return ProposePipelineResult(1, run_dir, "propose requires llmff audit output: missing audit.raw.json")
    if not (run_dir / "instruction-index.raw.json").exists():
        return ProposePipelineResult(
            1,
            run_dir,
            "propose requires llmff instruction index output: missing instruction-index.raw.json",
        )

    policy = load_policy(repo)
    try:
        candidate = _run_patch_propose(repo, run_dir, policy, audit_id=int(audit["audit_id"]))
    except LlmffRunFailed as error:
        return ProposePipelineResult(error.exit_code, run_dir, str(error))
    except (RuntimeError, ValueError) as error:
        return ProposePipelineResult(1, run_dir, str(error))
    decision = evaluate_candidate(repo, policy, candidate)
    memory_reasons = _rejected_memory_policy_reasons(repo, candidate)
    budget_reasons = _learning_rate_budget_policy_reasons(policy, candidate)
    decision_allowed = decision.allowed and not memory_reasons and not budget_reasons
    decision_reasons = list(dict.fromkeys([*decision.reasons, *memory_reasons, *budget_reasons]))
    try:
        artifacts = write_candidate(repo, run_dir.name, candidate)
    except ValueError as error:
        return ProposePipelineResult(1, run_dir, str(error))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_id = store.insert_candidate(
            audit_id=int(audit["audit_id"]),
            candidate=candidate,
            diff_path=artifacts.diff_path,
            state="artifact_pending",
        )
        _record_candidate_provenance(
            store,
            run_id=run_dir.name,
            run_dir=run_dir,
            candidate_id=candidate_id,
            candidate=candidate,
        )
    _merge_json(artifacts.json_path, {"candidate_id": candidate_id})
    _write_policy_gate(run_dir, allowed=decision_allowed, reasons=decision_reasons)
    _write_private_json_artifact(
        run_dir / "decision.json",
        "decision.json",
        _decision_payload(
            candidate_id=candidate_id,
            decision_value="needs_review" if decision_allowed else "rejected",
            policy_allowed=decision_allowed,
            policy_reasons=decision_reasons,
        ),
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="deterministic_policy_gate",
            decision="needs_review" if decision_allowed else "rejected",
            reason=",".join(decision_reasons),
        )
        store.update_candidate_state(
            candidate_id=candidate_id,
            state="needs_review" if decision_allowed else "rejected",
            reason=",".join(decision_reasons),
        )
    return ProposePipelineResult(
        0 if decision_allowed else 1,
        run_dir,
        f"candidate: {run_dir / 'candidate.diff'}",
    )


def _resolve_audit_run_dir(repo: Path, audit_ref: str) -> Path:
    if audit_ref == "latest":
        return latest_run_dir(repo)
    direct_run_dir = runs_dir(repo) / audit_ref
    if (direct_run_dir / "audit.json").exists():
        return direct_run_dir
    try:
        audit_id = int(audit_ref)
    except ValueError:
        return direct_run_dir
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            "SELECT run_id FROM audits WHERE id = ?",
            (audit_id,),
        ).fetchone()
    if row is None:
        return direct_run_dir
    return runs_dir(repo) / str(row[0])


def _run_patch_propose(repo: Path, run_dir: Path, policy, *, audit_id: int) -> CandidatePatch:
    manifests = materialize_manifests(repo)
    require_manifest_contracts(manifests)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    instruction_index_path = run_dir / "instruction-index.raw.json"
    drift_clusters_path, optimizer_notes_path = _run_drift_detect(
        repo,
        run_dir,
        policy,
        manifests,
        instruction_index_path=instruction_index_path,
    )
    manifest = next(record.path for record in manifests if record.name == "patch-propose.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    optimizer_memory_path = _write_optimizer_memory_artifact(repo, run_dir)
    optional_inputs = _optional_patch_propose_inputs(run_dir)
    input_paths = _filter_declared_manifest_inputs(
        manifest,
        {
            "instruction_index": run_dir / "instruction-snapshot",
            "instruction_index_artifact": instruction_index_path,
            "drift_clusters": drift_clusters_path,
            "optimizer_notes": optimizer_notes_path,
            "optimizer_memory": optimizer_memory_path,
            **optional_inputs,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        required_inputs={
            "instruction_index",
            "drift_clusters",
            "optimizer_notes",
            "policy",
        },
    )
    output_paths = _filter_declared_manifest_outputs(
        manifest,
        {
            "candidate_patch": run_dir / "candidate.raw.json",
            "proposal_rationale": run_dir / "proposal-rationale.raw.json",
        },
        required_outputs={"candidate_patch", "proposal_rationale"},
    )
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=policy.llmff_timeout_ms,
        retry_attempts=policy.llmff_retry_attempts,
        retry_backoff_ms=policy.llmff_retry_backoff_ms,
        checkpoint_path=run_dir / "patch-propose" / "checkpoint.json",
        input_paths=input_paths,
        output_paths=output_paths,
        validate_output_artifacts=False,
    )
    if run.exit_code != 0:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
            store.insert_run(
                run_id=run_dir.name,
                stage="propose",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
        raise LlmffRunFailed(
            f"llmff patch-propose failed with exit code {run.exit_code}",
            exit_code=run.exit_code,
        )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
    payload = load_json_object_artifact(
        run.output_paths["candidate_patch"],
        "candidate.raw.json",
    )
    audit_evidence_refs = _audit_evidence_refs(run_dir)
    if "proposal_rationale" in run.output_paths:
        rationale_payload = load_json_object_artifact(
            run.output_paths["proposal_rationale"],
            "proposal-rationale.raw.json",
        )
        validate_json_artifact("proposal-rationale.raw.json", rationale_payload)
        _validate_proposal_rationale_declared_by_audit(
            audit_evidence_refs,
            rationale_payload,
        )
        _validate_no_batch_training_evidence_refs(
            run_dir,
            "proposal_rationale",
            _unique_json_strings(rationale_payload.get("evidence_refs", [])),
        )
    payload = _select_candidate_payload(repo, run_dir, payload)
    validate_json_artifact("candidate.raw.json", payload)
    _validate_reflections_from_payload(payload)
    _validate_reflections_declared_by_audit(audit_evidence_refs, payload)
    _validate_payload_batch_training_evidence_refs(run_dir, payload)
    candidate = _candidate_from_payload(payload, audit_id=audit_id)
    _validate_bounded_edit_metadata_matches_diff(repo, candidate)
    _validate_candidate_sources_declared_by_audit(
        audit_evidence_refs,
        candidate,
        canonical_evidence_trusts=_canonical_evidence_trusts(run_dir),
    )
    return candidate


def _optional_patch_propose_inputs(run_dir: Path) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    optimization_batch = run_dir / "optimization-batch.json"
    if optimization_batch.exists():
        validate_json_artifact(
            "optimization-batch.json",
            load_json_object_artifact(optimization_batch, "optimization-batch.json"),
        )
        inputs["optimization_batch"] = optimization_batch
    reflection_artifact = run_dir / "reflection.json"
    if reflection_artifact.exists():
        validate_json_artifact(
            "reflection.json",
            load_json_object_artifact(reflection_artifact, "reflection.json"),
        )
        inputs["reflection_artifact"] = reflection_artifact
    return inputs


def _validate_payload_batch_training_evidence_refs(
    run_dir: Path,
    payload: dict[str, object],
) -> None:
    _validate_no_batch_training_evidence_refs(
        run_dir,
        "candidate sources",
        [
            str(source.get("source_id", ""))
            for source in payload.get("sources", [])
            if isinstance(source, dict)
        ],
    )
    _validate_no_batch_training_evidence_refs(
        run_dir,
        "candidate reflections",
        [
            str(reflection.get("source_ref", ""))
            for reflection in payload.get("reflections", [])
            if isinstance(reflection, dict)
        ],
    )


def _validate_no_batch_training_evidence_refs(
    run_dir: Path,
    label: str,
    refs: Sequence[str],
) -> None:
    protected_refs = _batch_non_training_refs(run_dir)
    if not protected_refs:
        return
    cited = sorted({ref for ref in refs if ref in protected_refs})
    if cited:
        raise ValueError(
            f"{label} cannot cite held-out or unseen batch refs as training evidence: "
            + ", ".join(cited)
        )


def _batch_non_training_refs(run_dir: Path) -> set[str]:
    batch_path = run_dir / "optimization-batch.json"
    if not batch_path.exists():
        return set()
    batch = load_json_object_artifact(batch_path, "optimization-batch.json")
    validate_json_artifact("optimization-batch.json", batch)
    protected = set(_unique_json_strings(batch.get("held_out_episodes", [])))
    unseen_suites = _unique_json_strings(batch.get("unseen_suites", []))
    protected.update(unseen_suites)
    protected.update(f"suite:{suite}" for suite in unseen_suites)
    return protected


def _select_candidate_payload(repo: Path, run_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    if "candidates" not in payload:
        return payload
    validate_json_artifact("candidate-set.raw.json", payload)
    candidate_set_path = run_dir / "candidate-set.raw.json"
    _write_private_json_artifact(candidate_set_path, "candidate-set.raw.json", payload)
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValueError("candidate-set.raw.json candidates must be a JSON list")
    policy = load_policy(repo)
    selected_payload, ranking = _rank_and_merge_candidate_payloads(repo, raw_candidates, policy)
    _write_private_json_artifact(run_dir / "candidate-ranking.json", "candidate-ranking.json", ranking)
    _write_private_json_artifact(run_dir / "candidate.raw.json", "candidate.raw.json", selected_payload)
    return selected_payload


def _rank_and_merge_candidate_payloads(
    repo: Path,
    raw_candidates: list[object],
    policy,
) -> tuple[dict[str, object], dict[str, object]]:
    candidates = [
        candidate for candidate in raw_candidates if isinstance(candidate, dict)
    ]
    if not candidates:
        raise ValueError("candidate-set.raw.json candidates must contain JSON objects")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
    selected: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", f"candidate-{len(selected) + len(rejected) + 1}"))
        candidate_reasons = _candidate_set_diff_rejection_reasons(repo, candidate)
        if not candidate_reasons:
            candidate_reasons = _candidate_set_memory_rejection_reasons(memory, candidate)
        if not candidate_reasons:
            candidate_reasons = _candidate_set_budget_rejection_reasons(policy, candidate)
        if not candidate_reasons:
            candidate_reasons = _candidate_set_merge_rejection_reasons(repo, selected, candidate)
        if candidate_reasons:
            rejected.append({"candidate_id": candidate_id, "reasons": candidate_reasons})
            continue
        selected.append(candidate)
    if not selected:
        reason_summary = "; ".join(
            f"{item['candidate_id']}={','.join(item['reasons'])}" for item in rejected
        )
        raise ValueError(f"candidate-set.raw.json candidates were all rejected: {reason_summary}")
    merged_payload = _merged_candidate_payload(repo, selected)
    ranking = {
        "schema_version": SCHEMA_VERSION,
        "selected_candidate_ids": [
            str(candidate.get("candidate_id", f"candidate-{index}"))
            for index, candidate in enumerate(selected, start=1)
        ],
        "merged": len(selected) > 1,
        "rejected_candidates": rejected,
    }
    return merged_payload, ranking


def _candidate_set_budget_rejection_reasons(policy, candidate: dict[str, object]) -> list[str]:
    raw_metadata = candidate.get("bounded_edit_metadata", [])
    if not raw_metadata:
        return ["bounded_edit_metadata_required"]
    if not isinstance(raw_metadata, list):
        return ["bounded_edit_metadata_required"]
    metadata = tuple(item for item in raw_metadata if isinstance(item, dict))
    if not metadata:
        return ["bounded_edit_metadata_required"]
    return _learning_rate_budget_reasons_for_metadata(policy, metadata)


def _candidate_set_diff_rejection_reasons(repo: Path, candidate: dict[str, object]) -> list[str]:
    raw_metadata = candidate.get("bounded_edit_metadata", [])
    if not isinstance(raw_metadata, list):
        return []
    metadata = tuple(item for item in raw_metadata if isinstance(item, dict))
    if not metadata:
        return []
    base_file = str(candidate.get("base_file", ""))
    if Path(base_file).suffix.lower() not in {".md", ".markdown"}:
        return []
    diff = str(candidate.get("diff", ""))
    base_path = (repo / base_file).resolve()
    repo_root = repo.resolve()
    if not _is_relative_to(base_path, repo_root) or not base_path.exists():
        return []
    mismatches = bounded_edit_metadata_mismatch_fields(
        base_path.read_text(encoding="utf-8"),
        diff,
        metadata,
        expected_path=base_file,
    )
    return ["bounded_edit_diff_mismatch"] if mismatches else []


def _candidate_set_memory_rejection_reasons(
    memory: OptimizationMemory,
    candidate: dict[str, object],
) -> list[str]:
    for item in candidate.get("bounded_edit_metadata", []):
        if not isinstance(item, dict):
            continue
        operator = str(item.get("operator", "unknown"))
        target_file = str(item.get("file", candidate.get("base_file", "")))
        section = str(item.get("section", ""))
        fingerprint = _bounded_edit_fingerprint(operator, target_file, section)
        if fingerprint in memory.rejected_edits:
            return ["suppressed_by_rejected_edit_memory"]
    return []


def _candidate_set_merge_rejection_reasons(
    repo: Path,
    selected: list[dict[str, object]],
    candidate: dict[str, object],
) -> list[str]:
    if not selected:
        return []
    first = selected[0]
    reasons: list[str] = []
    if candidate.get("base_file") != first.get("base_file"):
        reasons.append("base_file_mismatch")
    if candidate.get("base_hash") != first.get("base_hash"):
        reasons.append("base_hash_mismatch")
    existing_sections = {
        (str(item.get("file")), str(item.get("section")))
        for selected_candidate in selected
        for item in selected_candidate.get("bounded_edit_metadata", [])
        if isinstance(item, dict)
    }
    candidate_sections = {
        (str(item.get("file")), str(item.get("section")))
        for item in candidate.get("bounded_edit_metadata", [])
        if isinstance(item, dict)
    }
    if existing_sections & candidate_sections:
        reasons.append("incompatible_bounded_edit")
    if not reasons and _combined_diff_applies(repo, [*selected, candidate]) is None:
        reasons.append("diffs_do_not_compose")
    return reasons


def _merged_candidate_payload(repo: Path, candidates: list[dict[str, object]]) -> dict[str, object]:
    if len(candidates) == 1:
        payload = dict(candidates[0])
        payload.pop("candidate_id", None)
        return payload
    combined_diff = _combined_diff_applies(repo, candidates)
    if combined_diff is None:
        raise ValueError("candidate-set.raw.json selected diffs do not compose")
    first = candidates[0]
    return {
        "base_file": str(first["base_file"]),
        "base_hash": str(first["base_hash"]),
        "diff": combined_diff,
        "risk_class": str(first["risk_class"]),
        "rationale": " ".join(str(candidate["rationale"]) for candidate in candidates),
        "expected_behavior_change": " ".join(
            str(candidate["expected_behavior_change"]) for candidate in candidates
        ),
        "evals_required": _unique_json_strings(
            item for candidate in candidates for item in candidate.get("evals_required", [])
        ),
        "rollback_plan": list(first.get("rollback_plan", [])),
        "sources": _unique_sources(candidates),
        "reflections": [
            reflection
            for candidate in candidates
            for reflection in candidate.get("reflections", [])
            if isinstance(reflection, dict)
        ],
        "bounded_edit_metadata": [
            item
            for candidate in candidates
            for item in candidate.get("bounded_edit_metadata", [])
            if isinstance(item, dict)
        ],
    }


def _combined_diff_applies(repo: Path, candidates: list[dict[str, object]]) -> str | None:
    if not candidates:
        return None
    base_file = str(candidates[0].get("base_file", ""))
    base_path = repo / base_file
    if not base_path.exists():
        return None
    base_text = base_path.read_text(encoding="utf-8")
    combined_diff = _combine_unified_diffs(base_file, [str(candidate.get("diff", "")) for candidate in candidates])
    return combined_diff if apply_unified_diff(base_text, combined_diff) is not None else None


def _combine_unified_diffs(base_file: str, diffs: list[str]) -> str:
    body: list[str] = []
    for diff in diffs:
        for line in diff.splitlines():
            if line.startswith(("---", "+++")):
                continue
            body.append(line)
    return f"--- a/{base_file}\n+++ b/{base_file}\n" + "\n".join(body) + "\n"


def _unique_json_strings(items) -> list[str]:
    result: list[str] = []
    for item in items:
        value = str(item)
        if value not in result:
            result.append(value)
    return result


def _unique_sources(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, bool]] = set()
    for candidate in candidates:
        for source in candidate.get("sources", []):
            if not isinstance(source, dict):
                continue
            key = (str(source.get("source_id", "")), bool(source.get("trusted", False)))
            if key not in seen:
                seen.add(key)
                result.append({"source_id": key[0], "trusted": key[1]})
    return result


def _audit_evidence_refs(run_dir: Path) -> set[str]:
    batch_audits = run_dir / "batch-audit-reports.json"
    if batch_audits.exists():
        batch_payload = load_json_object_artifact(batch_audits, "batch-audit-reports.json")
        validate_json_artifact("batch-audit-reports.json", batch_payload)
        refs: set[str] = set()
        for report in batch_payload.get("reports", []):
            if not isinstance(report, dict):
                continue
            source_refs = report.get("source_refs", [])
            if not isinstance(source_refs, list):
                raise ValueError("batch-audit-reports.json source_refs must be JSON lists")
            refs.update(str(ref) for ref in source_refs)
            evidence_refs = report.get("evidence_refs", [])
            if not isinstance(evidence_refs, list):
                raise ValueError("batch-audit-reports.json evidence_refs must be JSON lists")
            if report.get("split") == "trigger":
                refs.update(str(ref) for ref in evidence_refs)
        return refs
    audit_payload = load_json_object_artifact(run_dir / "audit.json", "audit.json")
    audit_evidence_refs = audit_payload.get("evidence_refs", [])
    if not isinstance(audit_evidence_refs, list):
        raise ValueError("audit.evidence_refs must be a JSON list")
    return {str(ref) for ref in audit_evidence_refs}


def _validate_candidate_sources_declared_by_audit(
    declared: set[str],
    candidate: CandidatePatch,
    *,
    canonical_evidence_trusts: dict[str, str],
) -> None:
    missing = sorted(
        source.source_id for source in candidate.sources if source.source_id not in declared
    )
    if missing:
        raise ValueError(
            "candidate source refs not declared by audit evidence: " + ", ".join(missing)
        )
    trust_escalations = sorted(
        source.source_id
        for source in candidate.sources
        if source.trusted
        and canonical_evidence_trusts.get(source.source_id) is not None
        and canonical_evidence_trusts[source.source_id] not in _AUTHORITATIVE_SOURCE_TRUSTS
    )
    if trust_escalations:
        raise ValueError(
            "candidate source trust exceeds canonical evidence trust: "
            + ", ".join(trust_escalations)
        )


_AUTHORITATIVE_SOURCE_TRUSTS = frozenset({"artifact", "policy", "tool", "user", "verifier"})


def _canonical_evidence_trusts(run_dir: Path) -> dict[str, str]:
    path = run_dir / "canonical-episode.json"
    if not path.exists():
        return {}
    payload = load_json_object_artifact(path, "canonical-episode.json")
    return {
        str(event["evidence_id"]): str(event["source_trust"])
        for event in payload.get("events", [])
        if isinstance(event, dict) and "evidence_id" in event and "source_trust" in event
    }


def _validate_proposal_rationale_declared_by_audit(
    declared: set[str],
    rationale_payload: dict[str, object],
) -> None:
    evidence_refs = rationale_payload.get("evidence_refs", [])
    if not isinstance(evidence_refs, list):
        raise ValueError("proposal-rationale.raw.json evidence_refs must be a JSON list")
    missing = sorted(str(ref) for ref in evidence_refs if str(ref) not in declared)
    if missing:
        raise ValueError(
            "proposal rationale evidence refs not declared by audit evidence: "
            + ", ".join(missing)
        )


def _validate_reflections_declared_by_audit(
    declared: set[str],
    candidate_payload: dict[str, object],
) -> None:
    reflections = candidate_payload.get("reflections", [])
    if not isinstance(reflections, list):
        raise ValueError("reflections must be a JSON list")
    source_refs: list[str] = []
    for reflection in reflections:
        if not isinstance(reflection, dict):
            continue
        source_ref = reflection.get("source_ref")
        if isinstance(source_ref, str):
            source_refs.append(source_ref)
    missing = sorted(ref for ref in source_refs if ref not in declared)
    if missing:
        raise ValueError(
            "reflection source refs not declared by audit evidence: " + ", ".join(missing)
        )


def _run_drift_detect(
    repo: Path,
    run_dir: Path,
    policy,
    manifests,
    *,
    instruction_index_path: Path,
) -> tuple[Path, Path]:
    manifest = next(record.path for record in manifests if record.name == "drift-detect.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    output_path = run_dir / "drift.raw.json"
    optimizer_notes_path = run_dir / "optimizer-notes.raw.json"
    input_paths = _filter_declared_manifest_inputs(
        manifest,
        {
            "audit_reports": _audit_reports_input_path(run_dir),
            "instruction_index": run_dir / "instruction-snapshot",
            "instruction_index_artifact": instruction_index_path,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        required_inputs={"audit_reports", "instruction_index"},
    )
    output_paths = _filter_declared_manifest_outputs(
        manifest,
        {
            "drift_clusters": output_path,
            "optimizer_notes": optimizer_notes_path,
        },
        required_outputs={"drift_clusters", "optimizer_notes"},
    )
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=policy.llmff_timeout_ms,
        retry_attempts=policy.llmff_retry_attempts,
        retry_backoff_ms=policy.llmff_retry_backoff_ms,
        checkpoint_path=run_dir / "drift-detect" / "checkpoint.json",
        input_paths=input_paths,
        output_paths=output_paths,
        validate_output_artifacts=False,
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
        if run.exit_code != 0:
            store.insert_run(
                run_id=run_dir.name,
                stage="drift_detect",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
    if run.exit_code != 0:
        raise LlmffRunFailed(
            f"llmff drift-detect failed with exit code {run.exit_code}",
            exit_code=run.exit_code,
        )
    payload = load_json_object_artifact(output_path, "drift.raw.json")
    validate_json_artifact("drift.raw.json", payload)
    optimizer_payload = load_json_object_artifact(
        run.output_paths["optimizer_notes"],
        "optimizer-notes.raw.json",
    )
    validate_json_artifact("optimizer-notes.raw.json", optimizer_payload)
    declared_evidence_refs = _audit_evidence_refs(run_dir)
    _validate_drift_evidence_refs_declared_by_audit(declared_evidence_refs, payload)
    _validate_optimizer_notes_evidence_refs_declared_by_audit(
        declared_evidence_refs,
        optimizer_payload,
    )
    return output_path, optimizer_notes_path


def _validate_drift_evidence_refs_declared_by_audit(
    declared: set[str],
    payload: dict[str, object],
) -> None:
    refs = [
        str(ref)
        for cluster in payload.get("clusters", [])
        if isinstance(cluster, dict)
        for ref in cluster.get("evidence_refs", [])
    ]
    missing = sorted({ref for ref in refs if ref not in declared})
    if missing:
        raise ValueError(
            "drift.raw.json evidence refs not declared by audit evidence: "
            + ", ".join(missing)
        )


def _validate_optimizer_notes_evidence_refs_declared_by_audit(
    declared: set[str],
    payload: dict[str, object],
) -> None:
    refs = [
        str(ref)
        for note in payload.get("notes", [])
        if isinstance(note, dict)
        for ref in note.get("evidence_refs", [])
    ]
    missing = sorted({ref for ref in refs if ref not in declared})
    if missing:
        raise ValueError(
            "optimizer-notes.raw.json evidence refs not declared by audit evidence: "
            + ", ".join(missing)
        )


def _audit_reports_input_path(run_dir: Path) -> Path:
    batch_audits = run_dir / "batch-audit-reports.json"
    if batch_audits.exists():
        validate_json_artifact(
            "batch-audit-reports.json",
            load_json_object_artifact(batch_audits, "batch-audit-reports.json"),
        )
        return batch_audits
    return run_dir / "audit.raw.json"


def _filter_declared_manifest_inputs(
    manifest: Path,
    input_paths: dict[str, Path],
    *,
    required_inputs: set[str],
) -> dict[str, Path]:
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{manifest.name} must be a YAML object")
    declared_inputs = payload.get("inputs")
    if not isinstance(declared_inputs, list):
        raise RuntimeError(f"{manifest.name} must declare llmff inputs as a list")
    declared = {str(input_name) for input_name in declared_inputs}
    missing_required = sorted(required_inputs - declared)
    if missing_required:
        raise RuntimeError(
            f"{manifest.name} missing required llmff inputs: {', '.join(missing_required)}"
        )
    return {name: path for name, path in input_paths.items() if name in declared}


def _filter_declared_manifest_outputs(
    manifest: Path,
    output_paths: dict[str, Path],
    *,
    required_outputs: set[str],
) -> dict[str, Path]:
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{manifest.name} must be a YAML object")
    declared_outputs = payload.get("outputs")
    if not isinstance(declared_outputs, list):
        raise RuntimeError(f"{manifest.name} must declare llmff outputs as a list")
    declared = {str(output_name) for output_name in declared_outputs}
    missing_required = sorted(required_outputs - declared)
    if missing_required:
        raise RuntimeError(
            f"{manifest.name} missing required llmff outputs: {', '.join(missing_required)}"
        )
    return {name: path for name, path in output_paths.items() if name in declared}


def _write_optimizer_memory_artifact(repo: Path, run_dir: Path) -> Path:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rejected_edits": [
            {
                "future_proposal_suppression_signal": record.future_proposal_suppression_signal,
                "semantic_fingerprint": record.semantic_fingerprint,
                "rejection_reason": record.rejection_reason,
                "source_refs": list(record.source_refs),
            }
            for _, record in sorted(memory.rejected_edits.items())
        ],
        "slow_update_notes": list(memory.slow_update_notes),
        "slow_update_records": [
            {
                "category": record.category,
                "note": record.note,
            }
            for record in memory.slow_update_records
        ],
        "validation_baselines": [
            {
                "candidate_id": record.candidate_id,
                "held_out_score": record.held_out_score,
                "suite_id": record.suite_id,
            }
            for _, record in sorted(memory.validation_baselines.items())
        ],
    }
    path = run_dir / "optimizer-memory.json"
    _write_private_json_artifact(path, "optimizer-memory.json", payload)
    return path


def _candidate_from_payload(payload: dict[str, object], *, audit_id: int) -> CandidatePatch:
    return CandidatePatch(
        audit_id=audit_id,
        base_file=_required_non_empty_string(payload, "base_file", "candidate"),
        base_hash=_required_non_empty_string(payload, "base_hash", "candidate"),
        diff=_required_non_empty_string(payload, "diff", "candidate"),
        risk_class=_required_non_empty_string(payload, "risk_class", "candidate"),
        rationale=_required_non_empty_string(payload, "rationale", "candidate"),
        expected_behavior_change=_required_non_empty_string(
            payload, "expected_behavior_change", "candidate"
        ),
        evals_required=_required_non_empty_string_list(payload, "evals_required", "candidate"),
        rollback_plan=_required_non_empty_string_list(payload, "rollback_plan", "candidate"),
        sources=_source_refs_from_payload(payload),
        pending_audit_eval_definition_paths=_optional_string_list(
            payload,
            "pending_audit_eval_definition_paths",
            "candidate",
        ),
        bounded_edit_metadata=_bounded_edit_metadata_from_payload(payload),
    )


def _validate_reflections_from_payload(payload: dict[str, object]) -> None:
    reflections = payload.get("reflections", [])
    if not isinstance(reflections, list):
        raise ValueError("reflections must be a JSON list")
    for index, reflection in enumerate(reflections):
        if not isinstance(reflection, dict):
            raise ValueError(f"reflections[{index}] must be a JSON object")
        validate_json_artifact("reflection.json", reflection)


def _source_refs_from_payload(payload: dict[str, object]) -> tuple[SourceRef, ...]:
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("sources must be a JSON list")
    return tuple(_source_ref_from_payload(source, index=index) for index, source in enumerate(raw_sources))


def _source_ref_from_payload(source: object, *, index: int) -> SourceRef:
    prefix = f"sources[{index}]"
    if not isinstance(source, dict):
        raise ValueError(f"{prefix} must be a JSON object")
    source_id = _required_non_empty_string(source, "source_id", prefix)
    trusted = source.get("trusted")
    if not isinstance(trusted, bool):
        raise ValueError(f"{prefix}.trusted must be a boolean")
    return SourceRef(source_id, trusted=trusted)


def _bounded_edit_metadata_from_payload(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw_metadata = payload.get("bounded_edit_metadata")
    if raw_metadata is None:
        raise ValueError("candidate.bounded_edit_metadata is required")
    if not isinstance(raw_metadata, list):
        raise ValueError("bounded_edit_metadata must be a JSON list")
    if not raw_metadata:
        raise ValueError("candidate.bounded_edit_metadata must not be empty")
    return tuple(
        _bounded_edit_metadata_item_from_payload(item, index=index)
        for index, item in enumerate(raw_metadata)
    )


def _bounded_edit_metadata_item_from_payload(item: object, *, index: int) -> dict[str, object]:
    prefix = f"bounded_edit_metadata[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{prefix} must be a JSON object")
    operator = _required_non_empty_string(item, "operator", prefix)
    if operator not in BOUNDED_EDIT_OPERATORS:
        allowed = ", ".join(sorted(BOUNDED_EDIT_OPERATORS))
        raise ValueError(f"{prefix}.operator must be one of: {allowed}")
    return {
        "operator": operator,
        "file": _required_non_empty_string(item, "file", prefix),
        "section": _required_non_empty_string(item, "section", prefix),
        "changed_lines": _required_non_negative_int(item, "changed_lines", prefix),
        "normative_changes": _required_non_negative_int(item, "normative_changes", prefix),
    }


def _validate_bounded_edit_metadata_matches_diff(repo: Path, candidate: CandidatePatch) -> None:
    if Path(candidate.base_file).suffix.lower() not in {".md", ".markdown"}:
        return
    base_path = (repo / candidate.base_file).resolve()
    repo_root = repo.resolve()
    if not _is_relative_to(base_path, repo_root) or not base_path.exists():
        return
    mismatches = bounded_edit_metadata_mismatch_fields(
        base_path.read_text(encoding="utf-8"),
        candidate.diff,
        candidate.bounded_edit_metadata,
        expected_path=candidate.base_file,
    )
    if mismatches:
        if mismatches == ("diff",):
            raise ValueError("candidate diff cannot be applied to base file")
        raise ValueError(
            "bounded_edit_diff_mismatch: "
            + ", ".join(mismatches)
        )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _required_non_empty_string(item: dict[str, object], field: str, prefix: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}.{field} is required")
    return value


def _required_non_empty_string_list(
    item: dict[str, object], field: str, prefix: str
) -> tuple[str, ...]:
    value = item.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{prefix}.{field} must be a non-empty JSON list of strings")
    if not all(isinstance(entry, str) and entry.strip() for entry in value):
        raise ValueError(f"{prefix}.{field} must be a non-empty JSON list of strings")
    return tuple(value)


def _optional_string_list(
    item: dict[str, object], field: str, prefix: str
) -> tuple[str, ...]:
    value = item.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"{prefix}.{field} must be a JSON list of strings")
    return tuple(value)


def _required_non_negative_int(item: dict[str, object], field: str, prefix: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{prefix}.{field} must be a non-negative integer")
    return value


def _learning_rate_budget_policy_reasons(policy, candidate: CandidatePatch) -> list[str]:
    if not candidate.bounded_edit_metadata:
        return ["bounded_edit_metadata_required"]
    return _learning_rate_budget_reasons_for_metadata(policy, candidate.bounded_edit_metadata)


def _learning_rate_budget_reasons_for_metadata(
    policy,
    bounded_edit_metadata: tuple[dict[str, object], ...],
) -> list[str]:
    budget = LearningRateBudget(
        max_files_touched=policy.roadmap_learning_rate_max_files_touched,
        max_sections_touched=policy.roadmap_learning_rate_max_sections_touched,
        max_changed_lines=policy.roadmap_learning_rate_max_changed_lines,
        max_normative_changes=policy.roadmap_learning_rate_max_normative_changes,
        operator_risk_limits=dict(policy.roadmap_learning_rate_operator_risk_limits),
    )
    return list(budget_reasons_for_bounded_edit_metadata(bounded_edit_metadata, budget=budget))


def _rejected_memory_policy_reasons(repo: Path, candidate: CandidatePatch) -> list[str]:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
    for item in candidate.bounded_edit_metadata:
        operator = str(item.get("operator", "unknown"))
        target_file = str(item.get("file", candidate.base_file))
        section = str(item.get("section", ""))
        fingerprint = _bounded_edit_fingerprint(operator, target_file, section)
        if fingerprint in memory.rejected_edits:
            return ["suppressed_by_rejected_edit_memory"]
    return []


def _bounded_edit_fingerprint(operator: str, target_file: str, section: str) -> str:
    import hashlib

    value = f"{operator}\n{target_file}\n{section}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _record_candidate_provenance(
    store: Store,
    *,
    run_id: str,
    run_dir: Path,
    candidate_id: int,
    candidate: CandidatePatch,
) -> None:
    for item in candidate.bounded_edit_metadata:
        operator = str(item.get("operator", "unknown"))
        target_path = str(item.get("file", candidate.base_file))
        edit_operation_id = store.record_edit_operation(
            candidate_id=candidate_id,
            operator=operator,
            target_path=target_path,
            payload=item,
        )
        store.record_candidate_edit(
            candidate_id=candidate_id,
            edit_operation_id=edit_operation_id,
            target_path=target_path,
            risk_class=candidate.risk_class,
        )
    raw_candidate = run_dir / "candidate.raw.json"
    if not raw_candidate.exists():
        return
    payload = json.loads(raw_candidate.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return
    reflections = payload.get("reflections", [])
    if not isinstance(reflections, list):
        return
    for index, reflection in enumerate(reflections, start=1):
        if not isinstance(reflection, dict):
            continue
        artifact_path = run_dir / f"reflection-{index:03d}.json"
        _write_private_json_artifact(artifact_path, "reflection.json", reflection)
        store.record_reflection(
            run_id=run_id,
            source_ref=str(reflection.get("source_ref", f"candidate:{candidate_id}")),
            artifact_path=artifact_path,
        )


def _write_policy_gate(run_dir: Path, *, allowed: bool, reasons: Sequence[object]) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "allowed": allowed,
        "reasons": [str(reason) for reason in reasons],
    }
    path = run_dir / "policy-gate.json"
    _write_private_json_artifact(path, "policy-gate.json", payload)
    return path


def _decision_payload(
    *,
    candidate_id: int,
    decision_value: str,
    policy_allowed: bool,
    policy_reasons: list[str],
) -> dict[str, object]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "decision": decision_value,
        "policy_allowed": policy_allowed,
        "policy_reasons": policy_reasons,
    }
    return payload


def _merge_json(path: Path, updates: dict[str, object]) -> None:
    payload = load_json_object_artifact(path, path.name)
    payload.update(updates)
    _write_private_json_artifact(path, path.name, payload)


def _write_private_json_artifact(path: Path, artifact_name: str, payload: dict[str, object]) -> None:
    validate_json_artifact(artifact_name, payload)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    path.write_text(text, encoding="utf-8")
    mark_private_file(path)
