from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION
from tugboat.audit.service import write_audit
from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo, instruction_chunk_refs
from tugboat.db import Store
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest, run_manifest
from tugboat.manifests import manifests_are_allowed_by_policy, materialize_manifests
from tugboat.paths import new_run_dir, sidecar_dir
from tugboat.scoring import ScoreOutcome, score_episode
from tugboat.security.redaction import redact_text
from tugboat.security.secrets import SecretScanError, scan_path
from tugboat.traces.adapters import (
    ingest_ci_failure_bundle,
    ingest_claude_transcript_bundle,
    ingest_codex_session_bundle,
    ingest_mcp_session_bundle,
)
from tugboat.traces.ingest import canonical_episode_from_bundle, ingest_jsonl_trace
from tugboat.traces.threats import detect_trace_threats


@dataclass(frozen=True)
class AuditPipelineResult:
    exit_code: int
    run_dir: Path
    message: str


def run_audit_pipeline(
    repo: Path,
    trace: Path,
    *,
    trace_format: str = "generic-jsonl",
    mock_llmff_inspect: bool = False,
) -> AuditPipelineResult:
    policy = load_policy(repo)
    run_dir = new_run_dir(repo)
    shutil.copyfile(trace, run_dir / "trace-input.jsonl")
    _write_instruction_snapshot(repo, run_dir)
    try:
        scan_path(run_dir / "trace-input.jsonl")
        scan_path(run_dir / "instruction-snapshot")
    except SecretScanError as error:
        write_audit(
            run_dir,
            {
                "audit_id": 0,
                "edit_warranted": False,
                "evidence_refs": [],
                "failure_class": "secret_detected",
                "severity": "critical",
                "confidence": 1.0,
                "secret_findings": [
                    {
                        "path": finding.path,
                        "line_number": finding.line_number,
                        "kind": finding.kind,
                    }
                    for finding in error.findings
                ],
            },
        )
        return AuditPipelineResult(1, run_dir, "audit blocked: secret detected")
    redacted_trace = run_dir / "trace-redacted.jsonl"
    redacted_trace.write_text(
        redact_text((run_dir / "trace-input.jsonl").read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    manifests = materialize_manifests(repo)
    if not manifests_are_allowed_by_policy(manifests, policy):
        return AuditPipelineResult(1, run_dir, "manifest hash is not allowed by policy")
    instruction_index_path = run_dir / "instruction-snapshot"
    if not mock_llmff_inspect:
        index_manifest = next(
            record.path for record in manifests if record.name == "instruction-index.yaml"
        )
        index_inspect = inspect_manifest(index_manifest, run_dir=run_dir, policy=policy)
        index_run = run_manifest(
            index_manifest,
            run_dir=run_dir,
            policy=policy,
            timeout_ms=60_000,
            retry_attempts=0,
            retry_backoff_ms=0,
            checkpoint_path=run_dir / "instruction-index" / "checkpoint.json",
            input_paths={
                "instruction_snapshot": run_dir / "instruction-snapshot",
                "policy": sidecar_dir(repo) / "policy.yaml",
            },
            output_paths={"instruction_index": run_dir / "instruction-index.raw.json"},
        )
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_llmff_run(
                run_id=run_dir.name,
                manifest_hash=index_inspect.manifest_hash,
                result=index_run,
            )
        if index_run.exit_code != 0:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.insert_run(
                    run_id=run_dir.name,
                    stage="instruction_index",
                    manifest_hash=index_inspect.manifest_hash,
                    status="failed",
                    run_dir=run_dir,
                )
            write_audit(
                run_dir,
                {
                    "edit_warranted": False,
                    "evidence_refs": [],
                    "failure_class": "llmff_run_failed",
                    "severity": "high",
                    "confidence": 1.0,
                    "llmff_exit_code": index_run.exit_code,
                    "llmff_failure_kind": index_run.failure_kind,
                    "llmff_failure_message": index_run.failure_message,
                },
            )
            return AuditPipelineResult(
                index_run.exit_code,
                run_dir,
                f"instruction index run failed: {index_run.exit_code}",
            )
        instruction_index_path = index_run.output_paths["instruction_index"]
    manifest = next(record.path for record in manifests if record.name == "episode-audit.yaml")
    runner = (
        FixtureLlmffRunner(
            {
                "manifest": "episode-audit",
                "network_required": False,
                "providers": [],
            }
        )
        if mock_llmff_inspect
        else None
    )
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy, runner=runner)
    bundle = _ingest_trace(trace, trace_format)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        episode_id = store.record_trace_episode(repo=repo, bundle=bundle)
    audit_payload = _scored_audit_payload(bundle)
    if not mock_llmff_inspect:
        run = run_manifest(
            manifest,
            run_dir=run_dir,
            policy=policy,
            timeout_ms=60_000,
            retry_attempts=0,
            retry_backoff_ms=0,
            input_paths={
                "episode_trace": redacted_trace,
                "instruction_index": instruction_index_path,
                "policy": sidecar_dir(repo) / "policy.yaml",
            },
            output_paths={"audit_report": run_dir / "audit.raw.json"},
        )
        if run.exit_code != 0:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.record_llmff_run(
                    run_id=run_dir.name,
                    manifest_hash=inspect.manifest_hash,
                    result=run,
                )
                store.insert_run(
                    run_id=run_dir.name,
                    stage="audit",
                    manifest_hash=inspect.manifest_hash,
                    status="failed",
                    run_dir=run_dir,
                    episode_id=episode_id,
                )
            write_audit(
                run_dir,
                {
                    "edit_warranted": False,
                    "evidence_refs": audit_payload["evidence_refs"],
                    "failure_class": "llmff_run_failed",
                    "severity": "high",
                    "confidence": 1.0,
                    "llmff_exit_code": run.exit_code,
                    "llmff_failure_kind": run.failure_kind,
                    "llmff_failure_message": run.failure_message,
                },
            )
            return AuditPipelineResult(run.exit_code, run_dir, f"audit run failed: {run.exit_code}")
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_llmff_run(
                run_id=run_dir.name,
                manifest_hash=inspect.manifest_hash,
                result=run,
            )
        raw_audit = json.loads(run.output_paths["audit_report"].read_text(encoding="utf-8"))
        if not isinstance(raw_audit, dict):
            raise ValueError("llmff audit_report output must be a JSON object")
        audit_payload.update(raw_audit)
    evidence_refs = [str(ref) for ref in audit_payload.get("evidence_refs", [])]
    raw_instruction_refs = audit_payload.get("instruction_refs")
    if raw_instruction_refs is None:
        instruction_refs = instruction_chunk_refs(index_repo(repo, policy))
    elif isinstance(raw_instruction_refs, list):
        instruction_refs = [str(ref) for ref in raw_instruction_refs]
    else:
        raise ValueError("llmff audit_report instruction_refs must be a JSON array")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="audit",
            manifest_hash=inspect.manifest_hash,
            status="completed",
            run_dir=run_dir,
            episode_id=episode_id,
        )
        audit_id = store.insert_audit(
            run_id=run_dir.name,
            failure_class=str(audit_payload["failure_class"]),
            severity=str(audit_payload["severity"]),
            confidence=float(audit_payload["confidence"]),
            evidence_refs=evidence_refs,
            instruction_refs=instruction_refs,
        )
    audit_payload["audit_id"] = audit_id
    audit_payload["evidence_refs"] = evidence_refs
    audit_payload["instruction_refs"] = instruction_refs
    write_audit(run_dir, audit_payload)
    return AuditPipelineResult(0, run_dir, f"audit run: {run_dir.name}")


def _write_instruction_snapshot(repo: Path, run_dir: Path) -> None:
    snapshot = run_dir / "instruction-snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    result = index_repo(repo, load_policy(repo))
    for document in result.documents:
        source = repo / document.path
        target = snapshot / document.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    graph_path = run_dir / "instruction-graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "documents": [
                    {
                        "path": document.path,
                        "kind": document.kind,
                        "precedence": document.precedence,
                        "protected": document.protected,
                        "hash": document.hash,
                        "parser_version": document.parser_version,
                        "chunks": [
                            {
                                "heading_path": list(chunk.heading_path),
                                "anchor": chunk.anchor,
                                "byte_start": chunk.byte_start,
                                "byte_end": chunk.byte_end,
                                "text_hash": chunk.text_hash,
                            }
                            for chunk in document.chunks
                        ],
                    }
                    for document in result.documents
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for document in result.documents:
            store.record_instruction_snapshot(
                run_id=run_dir.name,
                path=document.path,
                artifact_path=snapshot / document.path,
            )
        store.record_instruction_graph(run_id=run_dir.name, artifact_path=graph_path)


def _ingest_trace(trace: Path, trace_format: str):
    if trace_format == "generic-jsonl":
        return ingest_jsonl_trace(trace)
    if trace_format == "codex":
        return ingest_codex_session_bundle(trace)
    if trace_format == "claude":
        return ingest_claude_transcript_bundle(trace)
    if trace_format == "ci":
        return ingest_ci_failure_bundle(trace)
    if trace_format == "mcp":
        return ingest_mcp_session_bundle(trace)
    raise ValueError(f"unsupported trace format: {trace_format}")


def _scored_audit_payload(bundle) -> dict[str, object]:
    episode = canonical_episode_from_bundle(bundle)
    outcomes = score_episode(episode)
    evidence_refs = _score_evidence_refs(outcomes) or [event.evidence_id for event in bundle.events]
    payload: dict[str, object] = {
        "edit_warranted": True,
        "evidence_refs": evidence_refs,
        "failure_class": "instruction_missing",
        "severity": "medium",
        "confidence": 0.75,
        "scoring": [_score_outcome_json(outcome) for outcome in outcomes],
        "trace_risk_findings": [finding.to_json() for finding in detect_trace_threats(episode)],
    }
    if any(outcome.label == "policy-violation" for outcome in outcomes):
        payload.update(
            {
                "failure_class": "unsafe_instruction_pressure",
                "severity": "critical",
                "confidence": 0.90,
            }
        )
    elif any(outcome.label == "failed-tests" for outcome in outcomes):
        payload.update(
            {
                "failure_class": "agent_ignored_instruction",
                "severity": "high",
                "confidence": 0.85,
            }
        )
    elif any(outcome.label == "recurring-user-correction" for outcome in outcomes):
        payload.update(
            {
                "failure_class": "user_preference_not_encoded",
                "severity": "medium",
                "confidence": 0.80,
            }
        )
    return payload


def _score_evidence_refs(outcomes: tuple[ScoreOutcome, ...]) -> list[str]:
    return list(dict.fromkeys(ref for outcome in outcomes for ref in outcome.evidence))


def _score_outcome_json(outcome: ScoreOutcome) -> dict[str, object]:
    return {
        "plugin": outcome.plugin,
        "label": outcome.label,
        "metrics": outcome.metrics,
        "evidence": list(outcome.evidence),
    }
