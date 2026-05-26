from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION, load_json_object_artifact, validate_json_artifact
from tugboat.config import load_policy
from tugboat.db import Store
from tugboat.eval.service import write_eval_report
from tugboat.evals import run_offline_eval_suite, run_provider_smoke_suite
from tugboat.llmff.runner import inspect_manifest, run_manifest
from tugboat.manifests import manifests_are_allowed_by_policy, materialize_manifests
from tugboat.optimization import REJECTED_EDIT_SUPPRESSION_SIGNAL
from tugboat.paths import latest_run_dir, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


@dataclass(frozen=True)
class EvalPipelineResult:
    exit_code: int
    run_dir: Path
    message: str


def run_eval_pipeline(repo: Path, candidate_ref: str, suite_id: str) -> EvalPipelineResult:
    repo = repo.resolve()
    run_dir = _resolve_candidate_run_dir(repo, candidate_ref)
    candidate_meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    if not isinstance(candidate_meta, dict):
        raise ValueError("candidate.json must be a JSON object")
    candidate_id = int(candidate_meta["candidate_id"])
    policy = load_policy(repo)
    passed = True
    metrics = {"governance_regressions": 0}
    trigger_score = 1.0
    held_out_score = 1.0
    governance_passed = True
    recommendation = "accept"
    live_provider_required = False
    policy_decision_payload: dict[str, object] | None = None
    offline_report = None

    if suite_id == "provider-smoke" and not (run_dir / "candidate.raw.json").exists():
        offline_report = run_provider_smoke_suite(
            opted_in=policy.provider_smoke_enabled,
            provider=(
                os.environ.get("TUGBOAT_PROVIDER_SMOKE_PROVIDER")
                or policy.provider_smoke_provider
                or None
            ),
            smoke_command=(
                os.environ.get("TUGBOAT_PROVIDER_SMOKE_COMMAND")
                or policy.provider_smoke_command
                or None
            ),
        )
        passed = offline_report.passed
        metrics = offline_report.metrics
        trigger_score = offline_report.trigger_score
        held_out_score = offline_report.held_out_score
        governance_passed = offline_report.governance_passed
        recommendation = offline_report.recommendation
        live_provider_required = offline_report.live_provider_required
    elif suite_id == "all" and not (run_dir / "candidate.raw.json").exists():
        try:
            preview_root = _candidate_preview_root(repo, run_dir)
        except ValueError as error:
            return EvalPipelineResult(1, run_dir, f"eval rejected: {error}")
        offline_report = run_offline_eval_suite(repo, suite_id=suite_id, preview_root=preview_root)
        passed = offline_report.passed
        metrics = offline_report.metrics
        trigger_score = offline_report.trigger_score
        held_out_score = offline_report.held_out_score
        governance_passed = offline_report.governance_passed
        recommendation = offline_report.recommendation
        live_provider_required = offline_report.live_provider_required
    elif (run_dir / "candidate.raw.json").exists():
        try:
            eval_payload, raw_policy_decision_payload = _run_patch_eval(
                repo,
                run_dir,
                policy,
                suite_id=suite_id,
            )
            policy_decision_payload = _policy_decision_from_payload(raw_policy_decision_payload)
            passed = _required_eval_bool(eval_payload, "passed", "llmff eval_report")
            raw_metrics = eval_payload.get("metrics", {})
            if not isinstance(raw_metrics, dict):
                raise ValueError("llmff eval_report.metrics must be a JSON object")
            metrics = raw_metrics
            trigger_score = _float_eval_field(eval_payload, metrics, "trigger_score", passed)
            held_out_score = _float_eval_field(eval_payload, metrics, "held_out_score", passed)
            governance_passed = _bool_eval_field(
                eval_payload,
                metrics,
                "governance_passed",
                bool(policy_decision_payload["allowed"]),
            )
            recommendation = _str_eval_field(
                eval_payload,
                metrics,
                "recommendation",
                "accept" if passed and governance_passed else "reject",
            )
        except ValueError as error:
            return EvalPipelineResult(1, run_dir, f"eval rejected: {error}")
        except RuntimeError as error:
            return EvalPipelineResult(1, run_dir, str(error))
    else:
        return EvalPipelineResult(1, run_dir, f"unsupported offline eval suite: {suite_id}")

    report_path = write_eval_report(
        repo,
        run_dir.name,
        candidate_id=candidate_id,
        suite_id=suite_id,
        passed=passed,
        metrics=metrics,
        trigger_score=trigger_score,
        held_out_score=held_out_score,
        governance_passed=governance_passed,
        recommendation=recommendation,
        live_provider_required=live_provider_required,
    )
    if policy_decision_payload is not None:
        _write_policy_gate(
            run_dir,
            allowed=bool(policy_decision_payload["allowed"]),
            reasons=list(policy_decision_payload.get("reasons", [])),
        )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id=suite_id,
            report_path=report_path,
            passed=passed,
            metrics=metrics,
        )
        if offline_report is not None:
            for case in offline_report.eval_cases:
                store.record_eval_case(
                    suite_id=suite_id,
                    case_id=case.case_id,
                    case_hash=case.case_hash,
                )
            if offline_report.validation_splits is not None:
                for split_name, case_ids in offline_report.validation_splits.items():
                    store.record_validation_split(
                        suite_id=suite_id,
                        split_name=split_name,
                        case_ids=case_ids,
                    )
        if not passed or recommendation == "reject":
            _record_rejected_candidate_memory(store, repo=repo, run_dir=run_dir, reason=recommendation)
    return EvalPipelineResult(
        0 if passed else 1,
        run_dir,
        f"eval suite: {suite_id} {'passed' if passed else 'failed'}",
    )


def _resolve_candidate_run_dir(repo: Path, candidate_ref: str) -> Path:
    if candidate_ref == "latest":
        return latest_run_dir(repo)
    direct_run_dir = runs_dir(repo) / candidate_ref
    if (direct_run_dir / "candidate.json").exists():
        return direct_run_dir
    try:
        candidate_id = int(candidate_ref)
    except ValueError:
        return direct_run_dir
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            "SELECT diff_path FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    if row is None:
        return direct_run_dir
    return Path(str(row[0])).resolve().parent


def _candidate_preview_root(repo: Path, run_dir: Path) -> Path:
    manifest_path = run_dir / "candidate-preview.json"
    if not manifest_path.exists():
        raise ValueError("candidate preview artifact is required for offline eval suite all")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_json_artifact("candidate-preview.json", manifest)
    preview_path = (repo / str(manifest["preview_path"])).resolve()
    preview_root = (run_dir / "candidate-preview").resolve()
    if not preview_path.is_relative_to(preview_root):
        raise ValueError("candidate preview path must resolve inside candidate-preview")
    if not preview_path.exists():
        raise ValueError("candidate preview file is missing")
    if CandidatePatch.hash_file(preview_path) != manifest["preview_hash"]:
        raise ValueError("candidate preview hash does not match preview file")
    return preview_root


def _run_patch_eval(
    repo: Path,
    run_dir: Path,
    policy,
    *,
    suite_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    manifests = materialize_manifests(repo)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    manifest = next(record.path for record in manifests if record.name == "patch-eval.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    suite_path = run_dir / "eval-suite.json"
    suite_payload = {"schema_version": SCHEMA_VERSION, "suite_id": suite_id}
    validate_json_artifact("eval-suite.json", suite_payload)
    suite_path.write_text(json.dumps(suite_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=policy.llmff_timeout_ms,
        retry_attempts=policy.llmff_retry_attempts,
        retry_backoff_ms=policy.llmff_retry_backoff_ms,
        checkpoint_path=run_dir / "patch-eval" / "checkpoint.json",
        input_paths={
            "candidate_patch": run_dir / "candidate.raw.json",
            "eval_suite": suite_path,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        output_paths={
            "eval_report": run_dir / "eval-report.raw.json",
            "policy_decision": run_dir / "policy-decision.raw.json",
        },
    )
    if run.exit_code != 0:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
            store.insert_run(
                run_id=run_dir.name,
                stage="eval",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
        raise RuntimeError(f"llmff patch-eval failed with exit code {run.exit_code}")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_llmff_run(run_id=run_dir.name, manifest_hash=inspect.manifest_hash, result=run)
    eval_payload = load_json_object_artifact(
        run.output_paths["eval_report"],
        "eval-report.raw.json",
    )
    validate_json_artifact("eval-report.raw.json", eval_payload)
    raw_policy_decision_payload = load_json_object_artifact(
        run.output_paths["policy_decision"],
        "policy-decision.raw.json",
    )
    validate_json_artifact("policy-decision.raw.json", raw_policy_decision_payload)
    return eval_payload, raw_policy_decision_payload


def _float_eval_field(
    payload: dict[str, object],
    metrics: dict[str, object],
    field: str,
    passed: bool,
) -> float:
    value = payload.get(field, metrics.get(field))
    if value is None:
        return 1.0 if passed else 0.0
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"llmff eval_report.{field} must be a number")
    return float(value)


def _bool_eval_field(
    payload: dict[str, object],
    metrics: dict[str, object],
    field: str,
    default: bool,
) -> bool:
    value = payload.get(field, metrics.get(field))
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"llmff eval_report.{field} must be a boolean")
    return value


def _str_eval_field(
    payload: dict[str, object],
    metrics: dict[str, object],
    field: str,
    default: str,
) -> str:
    value = payload.get(field, metrics.get(field))
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"llmff eval_report.{field} must be a non-empty string")
    return value


def _required_eval_bool(payload: dict[str, object], field: str, prefix: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{prefix}.{field} must be a boolean")
    return value


def _policy_decision_from_payload(payload: dict[str, object]) -> dict[str, object]:
    allowed = payload.get("allowed")
    if not isinstance(allowed, bool):
        raise ValueError("llmff policy_decision.allowed must be a boolean")
    raw_reasons = payload.get("reasons", [])
    if not isinstance(raw_reasons, list) or not all(isinstance(reason, str) for reason in raw_reasons):
        raise ValueError("llmff policy_decision.reasons must be a JSON list of strings")
    return {"allowed": allowed, "reasons": list(raw_reasons)}


def _record_rejected_candidate_memory(
    store: Store,
    *,
    repo: Path,
    run_dir: Path,
    reason: str,
) -> None:
    if not (run_dir / "candidate.diff").exists():
        return
    candidate = _candidate_from_artifacts(run_dir)
    source_refs = list(dict.fromkeys(source.source_id for source in candidate.sources if source.source_id))
    if not source_refs:
        source_refs = [f"audit:{candidate.audit_id}"]
    for item in candidate.bounded_edit_metadata:
        operator = str(item.get("operator", "unknown"))
        target_file = str(item.get("file", candidate.base_file))
        section = str(item.get("section", ""))
        fingerprint = _bounded_edit_fingerprint(operator, target_file, section)
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "future_proposal_suppression_signal": REJECTED_EDIT_SUPPRESSION_SIGNAL,
                "semantic_fingerprint": fingerprint,
                "rejection_reason": reason,
                "source_refs": source_refs,
            },
        )


def _candidate_from_artifacts(run_dir: Path) -> CandidatePatch:
    metadata = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    diff = (run_dir / "candidate.diff").read_text(encoding="utf-8")
    return CandidatePatch(
        audit_id=int(metadata["audit_id"]),
        base_file=str(metadata["base_file"]),
        base_hash=str(metadata["base_hash"]),
        diff=diff,
        risk_class=str(metadata["risk_class"]),
        rationale=str(metadata["rationale"]),
        expected_behavior_change=str(metadata.get("expected_behavior_change", "Not specified.")),
        evals_required=tuple(str(item) for item in metadata.get("evals_required", [])),
        rollback_plan=tuple(str(item) for item in metadata.get("rollback_plan", [])),
        sources=tuple(
            SourceRef(str(source["source_id"]), trusted=bool(source["trusted"]))
            for source in metadata.get("sources", [])
        ),
        bounded_edit_metadata=tuple(
            dict(item) for item in metadata.get("bounded_edit_metadata", []) if isinstance(item, dict)
        ),
    )


def _bounded_edit_fingerprint(operator: str, target_file: str, section: str) -> str:
    value = f"{operator}\n{target_file}\n{section}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _write_policy_gate(run_dir: Path, *, allowed: bool, reasons: list[object]) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "allowed": allowed,
        "reasons": [str(reason) for reason in reasons],
    }
    validate_json_artifact("policy-gate.json", payload)
    path = run_dir / "policy-gate.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
