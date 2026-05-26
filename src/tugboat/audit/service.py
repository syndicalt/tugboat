from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.paths import ensure_private_dir, mark_private_file


def write_audit(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = run_dir / "audit.json"
    ensure_private_dir(path.parent)
    artifact = {"schema_version": SCHEMA_VERSION, **payload}
    validate_json_artifact("audit.json", artifact)
    path.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    mark_private_file(path)
    return path
