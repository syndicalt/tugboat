from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact


def write_audit(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = run_dir / "audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {"schema_version": SCHEMA_VERSION, **payload}
    validate_json_artifact("audit.json", artifact)
    path.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path
