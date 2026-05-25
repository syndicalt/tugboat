from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_audit(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = run_dir / "audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path
