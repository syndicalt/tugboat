from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import (
    BOUNDED_EDIT_OPERATORS,
    SCHEMA_VERSION,
    load_json_object_artifact,
    validate_json_artifact,
)
from tugboat.config import load_policy
from tugboat.db import Store
from tugboat.llmff.runner import inspect_manifest, run_manifest
from tugboat.manifests import manifests_are_allowed_by_policy, materialize_manifests
from tugboat.optimization import (
    LearningRateBudget,
    OptimizationMemory,
    budget_reasons_for_bounded_edit_metadata,
)
from tugboat.paths import latest_run_dir, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.propose.service import write_candidate


@dataclass(frozen=True)
class ProposePipelineResult:
    exit_code: int
    run_dir: Path
    message: str


def run_propose_pipeline(repo: Path, audit_ref: str) -> ProposePipelineResult:
    repo = repo.resolve()
    run_dir = _resolve_audit_run_dir(repo, audit_ref)
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    if not isinstance(audit, dict):
        raise ValueError("audit.json must be a JSON object")
    if not audit.get("edit_warranted", False):
        return ProposePipelineResult(1, run_dir, "audit does not warrant an instruction edit")
    if not (run_dir / "audit.raw.json").exists():
        return ProposePipelineResult(1, run_dir, "propose requires llmff audit output: missing audit.raw.json")

    policy = load_policy(repo)
    try:
        candidate = _run_patch_propose(repo, run_dir, policy, audit_id=int(audit["audit_id"]))
    except (RuntimeError, ValueError) as error:
        return ProposePipelineResult(1, run_dir, str(error))
    decision = evaluate_candidate(repo, policy, candidate)
    memory_reasons = _rejected_memory_policy_reasons(repo, candidate)
    budget_reasons = _learning_rate_budget_policy_reasons(policy, candidate)
    decision_allowed = decision.allowed and not memory_reasons and not budget_reasons
    decision_reasons = [*decision.reasons, *memory_reasons, *budget_reasons]
    artifacts = write_candidate(repo, run_dir.name, candidate)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_id = store.insert_candidate(
            audit_id=int(audit["audit_id"]),
            candidate=candidate,
            diff_path=artifacts.diff_path,
            state="needs_review" if decision_allowed else "rejected",
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
    (run_dir / "decision.json").write_text(
        _decision_json(
            candidate_id=candidate_id,
            decision_value="needs_review" if decision_allowed else "rejected",
            policy_allowed=decision_allowed,
            policy_reasons=decision_reasons,
        ),
        encoding="utf-8",
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="deterministic_policy_gate",
            decision="needs_review" if decision_allowed else "rejected",
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
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    drift_clusters_path = _run_drift_detect(repo, run_dir, policy, manifests)
    manifest = next(record.path for record in manifests if record.name == "patch-propose.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    optimizer_memory_path = _write_optimizer_memory_artifact(repo, run_dir)
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=policy.llmff_timeout_ms,
        retry_attempts=policy.llmff_retry_attempts,
        retry_backoff_ms=policy.llmff_retry_backoff_ms,
        checkpoint_path=run_dir / "patch-propose" / "checkpoint.json",
        input_paths={
            "instruction_index": run_dir / "instruction-snapshot",
            "drift_clusters": drift_clusters_path,
            "optimizer_notes": run_dir / "audit.json",
            "optimizer_memory": optimizer_memory_path,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        output_paths={"candidate_patch": run_dir / "candidate.raw.json"},
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
        raise RuntimeError(f"llmff patch-propose failed with exit code {run.exit_code}")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
    payload = load_json_object_artifact(
        run.output_paths["candidate_patch"],
        "candidate.raw.json",
    )
    validate_json_artifact("candidate.raw.json", payload)
    _validate_reflections_from_payload(payload)
    return _candidate_from_payload(payload, audit_id=audit_id)


def _run_drift_detect(repo: Path, run_dir: Path, policy, manifests) -> Path:
    manifest = next(record.path for record in manifests if record.name == "drift-detect.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    output_path = run_dir / "drift.raw.json"
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=policy.llmff_timeout_ms,
        retry_attempts=policy.llmff_retry_attempts,
        retry_backoff_ms=policy.llmff_retry_backoff_ms,
        checkpoint_path=run_dir / "drift-detect" / "checkpoint.json",
        input_paths={
            "audit_reports": run_dir / "audit.raw.json",
            "instruction_index": run_dir / "instruction-snapshot",
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        output_paths={"drift_clusters": output_path},
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
        raise RuntimeError(f"llmff drift-detect failed with exit code {run.exit_code}")
    payload = load_json_object_artifact(output_path, "drift.raw.json")
    validate_json_artifact("drift.raw.json", payload)
    return output_path


def _write_optimizer_memory_artifact(repo: Path, run_dir: Path) -> Path:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rejected_edits": [
            {
                "semantic_fingerprint": record.semantic_fingerprint,
                "rejection_reason": record.rejection_reason,
                "source_refs": list(record.source_refs),
            }
            for _, record in sorted(memory.rejected_edits.items())
        ],
        "slow_update_notes": list(memory.slow_update_notes),
    }
    validate_json_artifact("optimizer-memory.json", payload)
    path = run_dir / "optimizer-memory.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    raw_metadata = payload.get("bounded_edit_metadata", payload.get("operator_metadata"))
    if raw_metadata is None:
        return ()
    if not isinstance(raw_metadata, list):
        raise ValueError("bounded_edit_metadata must be a JSON list")
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
    budget = LearningRateBudget(
        max_files_touched=policy.roadmap_learning_rate_max_files_touched,
        max_sections_touched=policy.roadmap_learning_rate_max_sections_touched,
        max_changed_lines=policy.roadmap_learning_rate_max_changed_lines,
        max_normative_changes=policy.roadmap_learning_rate_max_normative_changes,
        operator_risk_limits=dict(policy.roadmap_learning_rate_operator_risk_limits),
    )
    return list(budget_reasons_for_bounded_edit_metadata(candidate.bounded_edit_metadata, budget=budget))


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
        validate_json_artifact("reflection.json", reflection)
        artifact_path = run_dir / f"reflection-{index:03d}.json"
        artifact_path.write_text(json.dumps(reflection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    validate_json_artifact("policy-gate.json", payload)
    path = run_dir / "policy-gate.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _decision_json(
    *,
    candidate_id: int,
    decision_value: str,
    policy_allowed: bool,
    policy_reasons: list[str],
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "decision": decision_value,
        "policy_allowed": policy_allowed,
        "policy_reasons": policy_reasons,
    }
    validate_json_artifact("decision.json", payload)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _merge_json(path: Path, updates: dict[str, object]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
