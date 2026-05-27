from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.paths import ensure_private_dir, mark_private_file
from tugboat.security.secrets import SecretScanError, scan_text


def write_audit(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = run_dir / "audit.json"
    ensure_private_dir(path.parent)
    artifact = {"schema_version": SCHEMA_VERSION, **payload}
    validate_json_artifact("audit.json", artifact)
    text = json.dumps(artifact, sort_keys=True, indent=2) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    path.write_text(text, encoding="utf-8")
    mark_private_file(path)
    return path
