from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import (
    ArtifactValidationError,
    SCHEMA_VERSION,
    load_json_object_artifact,
    validate_json_artifact,
)
from tugboat.audit.service import write_audit
from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo, instruction_chunk_refs
from tugboat.db import Store
from tugboat.llmff.contracts import InspectPolicyError
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest, run_manifest
from tugboat.manifests import (
    ManifestContractError,
    manifests_are_allowed_by_policy,
    materialize_manifests,
    require_manifest_contracts,
)
from tugboat.paths import ensure_private_dir, mark_private_file, new_run_dir, sidecar_dir
from tugboat.scoring import ScoreOutcome, score_episode
from tugboat.security.redaction import redact_payload
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
    trace_format: str = "auto",
    mock_llmff_inspect: bool = False,
) -> AuditPipelineResult:
    policy = load_policy(repo)
    run_dir = new_run_dir(repo)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="audit",
            manifest_hash="preflight",
            status="running",
            run_dir=run_dir,
        )
    shutil.copyfile(trace, run_dir / "trace-input.jsonl")
    mark_private_file(run_dir / "trace-input.jsonl")
    _write_instruction_snapshot(repo, run_dir)
    try:
        scan_path(run_dir / "trace-input.jsonl")
        scan_path(run_dir / "instruction-snapshot")
    except SecretScanError as error:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="audit",
                manifest_hash="preflight",
                status="failed",
                run_dir=run_dir,
            )
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
    bundle = _ingest_trace(trace, trace_format)
    redacted_trace = run_dir / "trace-redacted.jsonl"
    _write_redacted_trace(bundle, redacted_trace)
    canonical_episode_path = run_dir / "canonical-episode.json"
    _write_canonical_episode(
        bundle,
        canonical_episode_path,
        instruction_snapshot_dir=run_dir / "instruction-snapshot",
    )
    manifests = materialize_manifests(repo)
    try:
        require_manifest_contracts(manifests)
    except ManifestContractError as error:
        return AuditPipelineResult(1, run_dir, str(error))
    if not manifests_are_allowed_by_policy(manifests, policy):
        return AuditPipelineResult(1, run_dir, "manifest hash is not allowed by policy")
    instruction_index_path = run_dir / "instruction-snapshot"
    if not mock_llmff_inspect:
        index_manifest = next(
            record.path for record in manifests if record.name == "instruction-index.yaml"
        )
        try:
            index_inspect = inspect_manifest(index_manifest, run_dir=run_dir, policy=policy)
        except InspectPolicyError as error:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.insert_run(
                    run_id=run_dir.name,
                    stage="instruction_index",
                    manifest_hash=_file_sha256(index_manifest),
                    status="failed",
                    run_dir=run_dir,
                )
            write_audit(run_dir, _llmff_inspect_failure_audit_payload(str(error)))
            return AuditPipelineResult(1, run_dir, f"instruction index blocked: {error}")
        except SecretScanError as error:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.insert_run(
                    run_id=run_dir.name,
                    stage="instruction_index",
                    manifest_hash=_file_sha256(index_manifest),
                    status="failed",
                    run_dir=run_dir,
                )
            write_audit(run_dir, _secret_scan_audit_payload(error))
            return AuditPipelineResult(1, run_dir, "instruction index blocked: secret detected")
        try:
            index_run = run_manifest(
                index_manifest,
                run_dir=run_dir,
                policy=policy,
                timeout_ms=policy.llmff_timeout_ms,
                retry_attempts=policy.llmff_retry_attempts,
                retry_backoff_ms=policy.llmff_retry_backoff_ms,
                checkpoint_path=run_dir / "instruction-index" / "checkpoint.json",
                input_paths={
                    "instruction_corpus": run_dir / "instruction-snapshot",
                    "policy": sidecar_dir(repo) / "policy.yaml",
                },
                output_paths={"instruction_index": run_dir / "instruction-index.raw.json"},
                validate_output_artifacts=False,
            )
        except SecretScanError as error:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.insert_run(
                    run_id=run_dir.name,
                    stage="instruction_index",
                    manifest_hash=index_inspect.manifest_hash,
                    status="failed",
                    run_dir=run_dir,
                )
            write_audit(run_dir, _secret_scan_audit_payload(error))
            return AuditPipelineResult(1, run_dir, "instruction index blocked: secret detected")
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
        try:
            raw_instruction_index = load_json_object_artifact(
                index_run.output_paths["instruction_index"],
                "instruction-index.raw.json",
            )
            validate_json_artifact("instruction-index.raw.json", raw_instruction_index)
        except ArtifactValidationError as error:
            return _failed_instruction_index_result(
                repo,
                run_dir,
                manifest_hash=index_inspect.manifest_hash,
                message=f"instruction index rejected: {error}",
            )
        instruction_index_path = index_run.output_paths["instruction_index"]
    manifest = next(record.path for record in manifests if record.name == "episode-audit.yaml")
    runner = (
        FixtureLlmffRunner(
            {
                "manifest": "episode-audit",
                "network_required": False,
                "providers": [],
                "external_calls": [],
            }
        )
        if mock_llmff_inspect
        else None
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        episode_id = store.record_trace_episode(repo=repo, bundle=bundle)
    audit_payload = _scored_audit_payload(bundle)
    try:
        inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy, runner=runner)
    except InspectPolicyError as error:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="audit",
                manifest_hash=_file_sha256(manifest),
                status="failed",
                run_dir=run_dir,
                episode_id=episode_id,
            )
        write_audit(
            run_dir,
            _llmff_inspect_failure_audit_payload(
                str(error),
                evidence_refs=[str(ref) for ref in audit_payload.get("evidence_refs", [])],
            ),
        )
        return AuditPipelineResult(1, run_dir, f"audit blocked: {error}")
    except SecretScanError as error:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="audit",
                manifest_hash=_file_sha256(manifest),
                status="failed",
                run_dir=run_dir,
                episode_id=episode_id,
            )
        write_audit(
            run_dir,
            _secret_scan_audit_payload(
                error,
                evidence_refs=[str(ref) for ref in audit_payload.get("evidence_refs", [])],
            ),
        )
        return AuditPipelineResult(1, run_dir, "audit blocked: secret detected")
    if not mock_llmff_inspect:
        try:
            run = run_manifest(
                manifest,
                run_dir=run_dir,
                policy=policy,
                timeout_ms=policy.llmff_timeout_ms,
                retry_attempts=policy.llmff_retry_attempts,
                retry_backoff_ms=policy.llmff_retry_backoff_ms,
                input_paths={
                    "episode_trace": canonical_episode_path,
                    "instruction_index": instruction_index_path,
                    "policy": sidecar_dir(repo) / "policy.yaml",
                },
                output_paths={
                    "audit_report": run_dir / "audit.raw.json",
                    "evidence_ids": run_dir / "evidence-ids.raw.json",
                },
                validate_output_artifacts=False,
            )
        except SecretScanError as error:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
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
                _secret_scan_audit_payload(
                    error,
                    evidence_refs=[str(ref) for ref in audit_payload.get("evidence_refs", [])],
                ),
            )
            return AuditPipelineResult(1, run_dir, "audit blocked: secret detected")
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
        try:
            raw_audit = load_json_object_artifact(
                run.output_paths["audit_report"],
                "audit.raw.json",
            )
            validate_json_artifact("audit.raw.json", raw_audit)
            raw_evidence_ids = load_json_object_artifact(
                run.output_paths["evidence_ids"],
                "evidence-ids.raw.json",
            )
            validate_json_artifact("evidence-ids.raw.json", raw_evidence_ids)
            citation_failure = _edit_warranted_citation_failure(raw_audit)
            if citation_failure is not None:
                return _failed_audit_result(
                    repo,
                    run_dir,
                    manifest_hash=inspect.manifest_hash,
                    episode_id=episode_id,
                    message=f"audit rejected: {citation_failure}",
                )
            unknown_declared_refs = _undeclared_evidence_refs(
                raw_evidence_ids["evidence_ids"],
                [event.evidence_id for event in bundle.events],
            )
            if unknown_declared_refs:
                return _failed_audit_result(
                    repo,
                    run_dir,
                    manifest_hash=inspect.manifest_hash,
                    episode_id=episode_id,
                    message=(
                        "audit rejected: evidence_ids not present in canonical episode: "
                        f"{', '.join(unknown_declared_refs)}"
                    ),
                )
            missing_evidence_refs = _undeclared_evidence_refs(
                raw_audit["evidence_refs"],
                raw_evidence_ids["evidence_ids"],
            )
            if missing_evidence_refs:
                return _failed_audit_result(
                    repo,
                    run_dir,
                    manifest_hash=inspect.manifest_hash,
                    episode_id=episode_id,
                    message=(
                        "audit rejected: audit evidence refs not declared by evidence_ids: "
                        f"{', '.join(missing_evidence_refs)}"
                    ),
                )
        except ArtifactValidationError as error:
            return _failed_audit_result(
                repo,
                run_dir,
                manifest_hash=inspect.manifest_hash,
                episode_id=episode_id,
                message=f"audit rejected: {error}",
            )
        audit_payload.update(raw_audit)
    indexed_instructions = index_repo(repo, policy)
    evidence_refs = [str(ref) for ref in audit_payload.get("evidence_refs", [])]
    raw_instruction_refs = audit_payload.get("instruction_refs")
    if raw_instruction_refs is None:
        instruction_refs = instruction_chunk_refs(indexed_instructions)
    elif isinstance(raw_instruction_refs, list):
        instruction_refs = [str(ref) for ref in raw_instruction_refs]
    else:
        raise ValueError("llmff audit_report instruction_refs must be a JSON array")
    known_instruction_refs = set(instruction_chunk_refs(indexed_instructions))
    unknown_instruction_refs = sorted(set(instruction_refs) - known_instruction_refs)
    if unknown_instruction_refs:
        return _failed_audit_result(
            repo,
            run_dir,
            manifest_hash=inspect.manifest_hash,
            episode_id=episode_id,
            message=(
                "audit rejected: audit instruction refs not present in instruction graph: "
                f"{', '.join(unknown_instruction_refs)}"
            ),
        )
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


def _failed_audit_result(
    repo: Path,
    run_dir: Path,
    *,
    manifest_hash: str,
    episode_id: int,
    message: str,
) -> AuditPipelineResult:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="audit",
            manifest_hash=manifest_hash,
            status="failed",
            run_dir=run_dir,
            episode_id=episode_id,
        )
    return AuditPipelineResult(1, run_dir, message)


def _undeclared_evidence_refs(
    audit_refs: list[str],
    declared_refs: list[str],
) -> list[str]:
    declared = set(declared_refs)
    return sorted({ref for ref in audit_refs if ref not in declared})


def _edit_warranted_citation_failure(raw_audit: dict[str, object]) -> str | None:
    if raw_audit.get("edit_warranted") is not True:
        return None
    if not raw_audit["evidence_refs"]:
        return "edit-warranted audit requires evidence_refs"
    if not raw_audit["instruction_refs"]:
        return "edit-warranted audit requires instruction_refs"
    return None


def _secret_scan_audit_payload(
    error: SecretScanError,
    *,
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "audit_id": 0,
        "edit_warranted": False,
        "evidence_refs": list(evidence_refs or []),
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
    }


def _llmff_inspect_failure_audit_payload(
    message: str,
    *,
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "audit_id": 0,
        "edit_warranted": False,
        "evidence_refs": list(evidence_refs or []),
        "failure_class": "llmff_inspect_failed",
        "severity": "high",
        "confidence": 1.0,
        "llmff_failure_kind": "inspect_failed",
        "llmff_failure_message": message,
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _failed_instruction_index_result(
    repo: Path,
    run_dir: Path,
    *,
    manifest_hash: str,
    message: str,
) -> AuditPipelineResult:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="instruction_index",
            manifest_hash=manifest_hash,
            status="failed",
            run_dir=run_dir,
        )
    return AuditPipelineResult(1, run_dir, message)


def _write_instruction_snapshot(repo: Path, run_dir: Path) -> None:
    snapshot = run_dir / "instruction-snapshot"
    ensure_private_dir(snapshot)
    result = index_repo(repo, load_policy(repo))
    for document in result.documents:
        source = repo / document.path
        target = snapshot / document.path
        ensure_private_dir(target.parent)
        shutil.copyfile(source, target)
        mark_private_file(target)
    graph_path = run_dir / "instruction-graph.json"
    graph_payload = {
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
                        "source_ref": _instruction_source_ref(document, chunk),
                        "byte_start": chunk.byte_start,
                        "byte_end": chunk.byte_end,
                        "text_hash": chunk.text_hash,
                    }
                    for chunk in document.chunks
                ],
            }
            for document in result.documents
        ],
    }
    validate_json_artifact("instruction-graph.json", graph_payload)
    graph_path.write_text(
        json.dumps(graph_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    mark_private_file(graph_path)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for document in result.documents:
            store.record_instruction_snapshot(
                run_id=run_dir.name,
                path=document.path,
                artifact_path=snapshot / document.path,
            )
        store.record_instruction_graph(run_id=run_dir.name, artifact_path=graph_path)


def _instruction_source_ref(document, chunk) -> str:
    if chunk.anchor:
        return f"{document.path}#{chunk.anchor}"
    return f"{document.path}#bytes-{chunk.byte_start}-{chunk.byte_end}"


def _ingest_trace(trace: Path, trace_format: str):
    if trace_format == "auto":
        trace_format = detect_trace_format(trace)
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


def detect_trace_format(trace: Path) -> str:
    sample = _trace_sample(trace)
    if isinstance(sample, dict):
        if _looks_like_claude_json(sample):
            return "claude"
        if _looks_like_ci_failure_json(sample):
            return "ci"
        return "generic-jsonl"
    for row in sample:
        if _looks_like_mcp_jsonl_row(row):
            return "mcp"
        if _looks_like_codex_jsonl_row(row):
            return "codex"
        if _looks_like_claude_jsonl_row(row):
            return "claude"
    return "generic-jsonl"


def _trace_sample(trace: Path) -> dict | list[dict]:
    if trace.suffix == ".json":
        payload = json.loads(trace.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        raise ValueError("JSON trace must contain an object")

    rows: list[dict] = []
    with trace.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("trace line must be a JSON object")
            rows.append(payload)
            if len(rows) >= 20:
                break
    return rows


def _looks_like_claude_json(payload: dict) -> bool:
    return isinstance(payload.get("messages"), list)


def _looks_like_ci_failure_json(payload: dict) -> bool:
    return (
        "exit_code" in payload
        and "command" in payload
        and ("suite" in payload or "output" in payload)
    )


def _looks_like_mcp_jsonl_row(row: dict) -> bool:
    return isinstance(row.get("event"), str)


def _looks_like_codex_jsonl_row(row: dict) -> bool:
    if row.get("type") in {"response_item", "session_meta"} and isinstance(
        row.get("payload"),
        dict,
    ):
        return True
    return row.get("role") in {"user", "assistant"} and "content" in row


def _looks_like_claude_jsonl_row(row: dict) -> bool:
    return isinstance(row.get("message"), dict)


def _write_redacted_trace(bundle, path: Path) -> None:
    episode = canonical_episode_from_bundle(bundle)
    rows = [
        {
            "evidence_id": event.evidence_id,
            "event_type": event.event_type,
            "source_trust": event.source_trust,
            "line_number": event.line_number,
            "payload": event.payload,
        }
        for event in episode.redacted_events()
    ]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    mark_private_file(path)


def _write_canonical_episode(
    bundle,
    path: Path,
    *,
    instruction_snapshot_dir: Path | None = None,
) -> None:
    episode = canonical_episode_from_bundle(bundle)
    instruction_snapshot = [
        *list(episode.instruction_snapshot),
        *_active_instruction_snapshot(instruction_snapshot_dir),
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trace_path": bundle.trace_path.as_posix(),
        "request": redact_payload(episode.request),
        "final_answer": redact_payload(episode.final_answer),
        "instruction_snapshot": redact_payload(instruction_snapshot),
        "tool_calls": _event_group_json(episode.tool_calls),
        "command_outputs": _event_group_json(episode.command_outputs),
        "diffs": _event_group_json(episode.diffs),
        "test_results": _event_group_json(episode.test_results),
        "policy_events": _event_group_json(episode.policy_events),
        "user_corrections": _event_group_json(episode.user_corrections),
        "subagent_reports": _event_group_json(episode.subagent_reports),
        "events": _event_group_json(episode.redacted_events()),
        "outcome_labels": list(episode.outcome_labels),
        "verifier_scores": episode.verifier_scores,
    }
    validate_json_artifact("canonical-episode.json", payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mark_private_file(path)


def _active_instruction_snapshot(snapshot_dir: Path | None) -> list[dict[str, object]]:
    if snapshot_dir is None:
        return []
    graph_path = snapshot_dir.parent / "instruction-graph.json"
    if not graph_path.is_file():
        return []
    graph = load_json_object_artifact(graph_path, "instruction-graph.json")
    documents = graph.get("documents", [])
    if not isinstance(documents, list):
        return []
    snapshots: list[dict[str, object]] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        source = document.get("path")
        sha256 = document.get("hash")
        if not isinstance(source, str) or not isinstance(sha256, str):
            continue
        snapshot_path = snapshot_dir / source
        if not snapshot_path.is_file():
            continue
        snapshots.append(
            {
                "type": "instruction_snapshot",
                "source": source,
                "origin": "audit_snapshot",
                "sha256": sha256,
                "text": snapshot_path.read_text(encoding="utf-8"),
            }
        )
    return snapshots


def _event_group_json(events) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": event.evidence_id,
            "event_type": event.event_type,
            "source_trust": event.source_trust,
            "line_number": event.line_number,
            "payload": redact_payload(event.payload),
        }
        for event in events
    ]


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
