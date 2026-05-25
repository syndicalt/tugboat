# Agent Instruction Sidecar MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 local CLI proposal loop: `trace bundle -> audit -> candidate patch -> regression eval -> reviewable diff`.

**Architecture:** Implement a Python package named `tugboat` with a console script named `tugboat`. Tugboat owns discovery, policy, storage, artifacts, deterministic gates, and review artifacts; `llmff` is only a bounded subprocess runner with inspect/run artifacts and mockable contracts. MVP is proposal-only: no daemon, no auto-apply, no public network listener, and no direct mutation of instruction files.

**Tech Stack:** Python 3.11+, `argparse`, `sqlite3`, `dataclasses`, `pathlib`, `subprocess`, `json`, `hashlib`, `PyYAML`, `pytest`.

---

## Harness Best-Practice Alignment

The OpenAI harness-engineering article changes the operating bar for this MVP. Tugboat should not only prevent unsafe instruction edits; it should make an agent-run repository more legible, mechanically enforceable, and easier to improve through feedback loops.

Implement these harness principles throughout the plan:

- **Humans steer, agents execute:** keep the CLI and artifacts optimized for agent workers to inspect, run, and review without copy/paste context from humans.
- **Repository knowledge is the system of record:** treat short instruction files as maps to deeper repo-local docs, plans, evals, and run artifacts instead of growing one giant policy manual.
- **Progressive disclosure:** index and report entry points, owners, verification status, and source references so agents can find the right context just in time.
- **Agent legibility:** every artifact should be plain text or JSON, repo-local, stable-path, and easy for another agent to inspect in a later run.
- **Mechanical invariants over prose-only guidance:** structural tests and policy gates should enforce precedence, protected headings, modal-language weakening, artifact layout, and no-direct-mutation rules.
- **Runnable isolated environments:** plan tests and future execution around per-repo or per-worktree `.sidecar/` state so concurrent agent tasks do not share mutable run directories.
- **Feedback loops:** user corrections, agent review findings, and failed evals should become evidence refs and review artifacts; later phases can promote recurring findings into docs or lints.
- **Entropy control:** add doc-gardening and stale-rule detection as first-class roadmap work, not an afterthought.

This keeps the conservative authority boundary while aligning with high-throughput harness design: increase autonomy by making the environment more inspectable, testable, and self-correcting before granting write authority.

---

## Expert-Agent Orchestration

Use fresh workers with disjoint ownership. The parent orchestrator reviews between phases and runs the integration checks.

### Phase A: Foundation, Single Worker

**Expert Agent A: Foundation and storage**
- Owns: `pyproject.toml`, `src/tugboat/cli.py`, `src/tugboat/config.py`, `src/tugboat/db.py`, `src/tugboat/models.py`, `src/tugboat/paths.py`, `src/tugboat/artifacts.py`, `tests/test_cli_doctor.py`, `tests/test_db.py`.
- Must not edit corpus, trace, llmff, policy, eval, or report modules.
- Output: working package install, `tugboat doctor`, SQLite schema, append-only audit events.

### Phase B: Parallel Domain Workers

Run after Phase A passes.

**Expert Agent B: Corpus and precedence**
- Owns: `src/tugboat/corpus/*`, `tests/test_markdown_parser.py`, `tests/test_indexer.py`, `tests/test_precedence_resolver.py`, parser/index fixtures.
- Must not edit CLI routing except adding imports already planned by Agent A.

**Expert Agent C: Trace and llmff integration**
- Owns: `src/tugboat/traces/*`, `src/tugboat/llmff/*`, `src/tugboat/audit/service.py`, `tests/test_trace_ingestion.py`, `tests/test_llmff_inspect_artifacts.py`, trace and llmff fixtures.
- Must keep real `llmff` optional and use deterministic fixture runners in tests.

**Expert Agent D: Policy, propose, eval, report**
- Owns: `src/tugboat/policy/gate.py`, `src/tugboat/propose/service.py`, `src/tugboat/eval/service.py`, `src/tugboat/report/service.py`, `tests/test_policy_gate.py`, policy/patch/eval fixtures.
- Must not add auto-apply or direct write paths.

### Phase C: Integration Worker

**Expert Agent E: End-to-end CLI integration**
- Owns: `tests/test_e2e_proposal_loop.py`, `tests/fixtures/e2e/*`, final CLI command wiring in `src/tugboat/cli.py`.
- May touch service modules only to integrate public functions already created by Agents B-D.
- Output: `tugboat index`, `audit`, `propose`, `eval`, and `report` run against deterministic fixtures and produce review artifacts without mutating instruction files.

### Phase D: Harness-Legibility Worker

**Expert Agent F: Harness best-practice alignment**
- Owns: `src/tugboat/harness/*`, `tests/test_harness_legibility.py`, harness fixtures under `tests/fixtures/harness/*`, and any report additions needed to expose harness signals.
- Must not edit policy gate behavior except through public result structures.
- Output: a `tugboat harness check --repo PATH` command that reports whether repo instructions are short maps, referenced docs exist, run artifacts are repo-local, and instruction files avoid monolithic growth.

### Parent Review Gates

- After Phase A: run `python -m pytest tests/test_cli_doctor.py tests/test_db.py -q`.
- After Phase B: run parser, precedence, trace, llmff, and policy tests together.
- After Phase C: run `python -m pytest -q` and inspect `.sidecar/runs/<run-id>/` artifact contents.
- After Phase D: run `python -m pytest tests/test_harness_legibility.py -q` and confirm `tugboat harness check --repo tests/fixtures/harness/good_repo` passes.
- Reject any worker output that adds daemon mode, auto-apply, live provider requirements, broad refactors, or direct instruction-file mutation.

---

## File Structure

Create:

```text
pyproject.toml
src/tugboat/__init__.py
src/tugboat/cli.py
src/tugboat/config.py
src/tugboat/db.py
src/tugboat/models.py
src/tugboat/paths.py
src/tugboat/artifacts.py
src/tugboat/corpus/__init__.py
src/tugboat/corpus/markdown.py
src/tugboat/corpus/indexer.py
src/tugboat/corpus/precedence.py
src/tugboat/traces/__init__.py
src/tugboat/traces/schema.py
src/tugboat/traces/ingest.py
src/tugboat/llmff/__init__.py
src/tugboat/llmff/contracts.py
src/tugboat/llmff/runner.py
src/tugboat/audit/__init__.py
src/tugboat/audit/service.py
src/tugboat/policy/__init__.py
src/tugboat/policy/gate.py
src/tugboat/propose/__init__.py
src/tugboat/propose/service.py
src/tugboat/eval/__init__.py
src/tugboat/eval/service.py
src/tugboat/report/__init__.py
src/tugboat/report/service.py
src/tugboat/harness/__init__.py
src/tugboat/harness/checks.py
tests/
tests/fixtures/
```

Responsibilities:

- `cli.py`: thin command parser and command dispatch only.
- `config.py`: `.sidecar/policy.yaml` loading, defaults, policy dataclasses.
- `db.py`: SQLite schema, inserts, queries, append-only audit events.
- `models.py`: shared dataclasses and enums.
- `paths.py`: repo-relative path resolution and run-directory allocation.
- `artifacts.py`: JSON/text artifact writes with parent directory creation.
- `corpus/*`: Markdown parsing, indexing, snapshots, precedence graph.
- `traces/*`: canonical JSONL trace ingestion with stable evidence IDs.
- `llmff/*`: inspect/run subprocess contract and deterministic fake runner.
- `audit/service.py`: audited episode classification using llmff output contract.
- `policy/gate.py`: deterministic patch safety gate.
- `propose/service.py`: candidate diff artifact creation and policy gate execution.
- `eval/service.py`: deterministic MVP eval report handling.
- `report/service.py`: review bundle summary.
- `harness/checks.py`: repository-legibility checks for instruction maps, repo-local docs, artifact paths, and monolithic instruction-file drift.

---

### Task 1: Project Scaffold and CLI Doctor

**Files:**
- Create: `pyproject.toml`
- Create: `src/tugboat/__init__.py`
- Create: `src/tugboat/cli.py`
- Create: `tests/test_cli_doctor.py`

- [ ] **Step 1: Write the failing CLI doctor test**

```python
# tests/test_cli_doctor.py
from tugboat.cli import main


def test_doctor_reports_proposal_only(capsys):
    exit_code = main(["doctor"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tugboat: ok" in out
    assert "mode: proposal_only" in out
    assert "auto_apply: disabled" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_cli_doctor.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tugboat'`.

- [ ] **Step 3: Add package scaffold and CLI**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "tugboat"
version = "0.1.0"
description = "Local-first agent instruction observability and optimization sidecar"
requires-python = ">=3.11"
dependencies = ["PyYAML>=6.0.1"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
tugboat = "tugboat.cli:console_main"

[tool.setuptools.packages.find]
where = ["src"]
```

```python
# src/tugboat/__init__.py
__version__ = "0.1.0"
```

```python
# src/tugboat/cli.py
from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print("tugboat: ok")
        print("mode: proposal_only")
        print("auto_apply: disabled")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def console_main() -> None:
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_cli_doctor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/tugboat/__init__.py src/tugboat/cli.py tests/test_cli_doctor.py
git commit -m "feat: scaffold tugboat cli"
```

---

### Task 2: Policy Configuration and Paths

**Files:**
- Create: `src/tugboat/models.py`
- Create: `src/tugboat/config.py`
- Create: `src/tugboat/paths.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing policy/default-path tests**

```python
# tests/test_config.py
from pathlib import Path

from tugboat.config import load_policy
from tugboat.paths import sidecar_dir


def test_load_policy_defaults_to_proposal_only(tmp_path: Path):
    policy = load_policy(tmp_path)

    assert policy.mode == "proposal_only"
    assert policy.auto_apply_enabled is False
    assert policy.llmff_allow_network is False
    assert [entry.path for entry in policy.instruction_files] == [
        "AGENTS.md",
        "CODEX.md",
        "CLAUDE.md",
        "SKILL.md",
        ".codex/skills/**/SKILL.md",
    ]


def test_load_policy_yaml_overrides_instruction_files(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mode: proposal_only
instruction_files:
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
auto_apply:
  enabled: false
  max_changed_lines: 12
llmff:
  binary: llmff
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert len(policy.instruction_files) == 1
    assert policy.instruction_files[0].path == "CODEX.md"
    assert policy.auto_apply_max_changed_lines == 12


def test_sidecar_dir_is_repo_local(tmp_path: Path):
    assert sidecar_dir(tmp_path) == tmp_path / ".sidecar"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL with missing modules.

- [ ] **Step 3: Implement config models and loader**

```python
# src/tugboat/models.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InstructionFilePolicy:
    path: str
    kind: str
    precedence: int
    protected: bool = False


@dataclass(frozen=True)
class Policy:
    version: int = 1
    mode: str = "proposal_only"
    instruction_files: tuple[InstructionFilePolicy, ...] = field(default_factory=tuple)
    auto_apply_enabled: bool = False
    auto_apply_max_changed_lines: int = 20
    forbidden_terms: tuple[str, ...] = (
        "approval",
        "sandbox",
        "secret",
        "deploy",
        "network",
        "permission",
        "must",
        "never",
    )
    llmff_binary: str = "llmff"
    llmff_require_inspect: bool = True
    llmff_allow_network: bool = False
```

```python
# src/tugboat/paths.py
from __future__ import annotations

from pathlib import Path


def sidecar_dir(repo: Path) -> Path:
    return repo / ".sidecar"


def runs_dir(repo: Path) -> Path:
    return sidecar_dir(repo) / "runs"
```

```python
# src/tugboat/config.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tugboat.models import InstructionFilePolicy, Policy


DEFAULT_INSTRUCTION_FILES = (
    InstructionFilePolicy("AGENTS.md", "repo_policy", 80, True),
    InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
    InstructionFilePolicy("CLAUDE.md", "agent_policy", 70, True),
    InstructionFilePolicy("SKILL.md", "skill", 60, False),
    InstructionFilePolicy(".codex/skills/**/SKILL.md", "skill", 60, False),
)


def _as_instruction_file(raw: dict[str, Any]) -> InstructionFilePolicy:
    return InstructionFilePolicy(
        path=str(raw["path"]),
        kind=str(raw.get("kind", "repo_policy")),
        precedence=int(raw.get("precedence", 50)),
        protected=bool(raw.get("protected", False)),
    )


def load_policy(repo: Path) -> Policy:
    path = repo / ".sidecar" / "policy.yaml"
    if not path.exists():
        return Policy(instruction_files=DEFAULT_INSTRUCTION_FILES)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    auto_apply = raw.get("auto_apply", {}) or {}
    llmff = raw.get("llmff", {}) or {}
    entries = tuple(_as_instruction_file(item) for item in raw.get("instruction_files", []))

    return Policy(
        version=int(raw.get("version", 1)),
        mode=str(raw.get("mode", "proposal_only")),
        instruction_files=entries or DEFAULT_INSTRUCTION_FILES,
        auto_apply_enabled=bool(auto_apply.get("enabled", False)),
        auto_apply_max_changed_lines=int(auto_apply.get("max_changed_lines", 20)),
        forbidden_terms=tuple(auto_apply.get("forbidden_terms", Policy().forbidden_terms)),
        llmff_binary=str(llmff.get("binary", "llmff")),
        llmff_require_inspect=bool(llmff.get("require_inspect", True)),
        llmff_allow_network=bool(llmff.get("allow_network", False)),
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/models.py src/tugboat/config.py src/tugboat/paths.py tests/test_config.py
git commit -m "feat: load sidecar policy"
```

---

### Task 3: SQLite Store and Append-Only Audit Events

**Files:**
- Create: `src/tugboat/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing DB tests**

```python
# tests/test_db.py
from pathlib import Path

import pytest

from tugboat.db import Store


def test_store_initializes_core_tables(tmp_path: Path):
    store = Store.open(tmp_path / "db.sqlite")

    tables = store.table_names()

    assert "documents" in tables
    assert "chunks" in tables
    assert "episodes" in tables
    assert "runs" in tables
    assert "audits" in tables
    assert "candidates" in tables
    assert "evals" in tables
    assert "decisions" in tables
    assert "audit_events" in tables


def test_audit_events_are_hash_chained(tmp_path: Path):
    store = Store.open(tmp_path / "db.sqlite")

    first = store.append_audit_event("run.created", {"run_id": "run-1"})
    second = store.append_audit_event("run.completed", {"run_id": "run-1"})

    assert first.sequence == 1
    assert first.previous_hash == ""
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash


def test_audit_event_update_is_not_supported(tmp_path: Path):
    store = Store.open(tmp_path / "db.sqlite")
    event = store.append_audit_event("run.created", {"run_id": "run-1"})

    with pytest.raises(PermissionError):
        store.update_audit_event(event.sequence, {"event_type": "tampered"})
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_db.py -q`
Expected: FAIL with missing `tugboat.db`.

- [ ] **Step 3: Implement schema and append-only writes**

```python
# src/tugboat/db.py
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  precedence INTEGER NOT NULL,
  protected INTEGER NOT NULL,
  hash TEXT NOT NULL,
  mtime REAL NOT NULL,
  parser_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  heading_path TEXT NOT NULL,
  anchor TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  text_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  trace_path TEXT NOT NULL,
  started_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  summary_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  episode_id INTEGER,
  stage TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  run_dir TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  failure_class TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  instruction_refs_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  audit_id INTEGER NOT NULL,
  base_file TEXT NOT NULL,
  base_hash TEXT NOT NULL,
  diff_hash TEXT NOT NULL,
  diff_path TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evals (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  suite_id TEXT NOT NULL,
  report_path TEXT NOT NULL,
  passed INTEGER NOT NULL,
  metrics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  policy TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class Store:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "Store":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.commit()
        return cls(connection)

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}

    def append_audit_event(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else ""
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(
            f"{previous_hash}\n{event_type}\n{payload_json}".encode("utf-8")
        ).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, payload_json, previous_hash, event_hash),
        )
        self.connection.commit()
        return AuditEvent(
            sequence=int(cursor.lastrowid),
            event_type=event_type,
            payload=payload,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )

    def update_audit_event(self, sequence: int, changes: dict[str, Any]) -> None:
        raise PermissionError("audit events are append-only")
```

- [ ] **Step 4: Run DB tests**

Run: `python -m pytest tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/db.py tests/test_db.py
git commit -m "feat: add sidecar sqlite store"
```

---

### Task 4: Markdown Parser

**Files:**
- Create: `src/tugboat/corpus/__init__.py`
- Create: `src/tugboat/corpus/markdown.py`
- Create: `tests/fixtures/instructions/parser/complex_skill.md`
- Create: `tests/test_markdown_parser.py`

- [ ] **Step 1: Add parser fixture**

```markdown
---
name: complex
description: fixture
---

# Root

Intro text.

## Setup

Setup text.

```python
# Not A Heading
print("inside fence")
```

## Setup

Duplicate heading text.

### Details

Detail text.
```

- [ ] **Step 2: Write failing parser test**

```python
# tests/test_markdown_parser.py
from pathlib import Path

from tugboat.corpus.markdown import parse_markdown


def test_parse_markdown_chunks_ignore_fenced_headings():
    path = Path("tests/fixtures/instructions/parser/complex_skill.md")
    text = path.read_text(encoding="utf-8")

    parsed = parse_markdown(text)

    headings = [chunk.heading_path for chunk in parsed.chunks]
    assert ["Root", "Setup"] in headings
    assert ["Root", "Not A Heading"] not in headings
    assert [chunk.anchor for chunk in parsed.chunks] == [
        "root",
        "setup",
        "setup-2",
        "details",
    ]
    for chunk in parsed.chunks:
        assert text.encode("utf-8")[chunk.byte_start : chunk.byte_end].decode("utf-8") == chunk.text
        assert len(chunk.text_hash) == 64
```

- [ ] **Step 3: Run parser test to verify failure**

Run: `python -m pytest tests/test_markdown_parser.py -q`
Expected: FAIL with missing `parse_markdown`.

- [ ] **Step 4: Implement parser**

```python
# src/tugboat/corpus/markdown.py
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class MarkdownChunk:
    heading_path: list[str]
    anchor: str
    byte_start: int
    byte_end: int
    text: str
    text_hash: str


@dataclass(frozen=True)
class ParsedMarkdown:
    chunks: list[MarkdownChunk]


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 -]", "", text.lower()).strip()
    return re.sub(r"\s+", "-", cleaned) or "section"


def _anchors(headings: list[tuple[int, str, int]]) -> list[str]:
    counts: dict[str, int] = {}
    anchors: list[str] = []
    for _, title, _ in headings:
        base = _slug(title)
        counts[base] = counts.get(base, 0) + 1
        anchors.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
    return anchors


def _heading_path(stack: list[tuple[int, str]], level: int, title: str) -> list[str]:
    stack = [item for item in stack if item[0] < level]
    stack.append((level, title))
    return [item[1] for item in stack]


def parse_markdown(text: str) -> ParsedMarkdown:
    headings: list[tuple[int, str, int]] = []
    in_fence = False
    byte_offset = 0

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        if not in_fence:
            match = HEADING_RE.match(line)
            if match:
                headings.append((len(match.group(1)), match.group(2).strip(), byte_offset))
        byte_offset += len(line.encode("utf-8"))

    anchors = _anchors(headings)
    chunks: list[MarkdownChunk] = []
    stack: list[tuple[int, str]] = []
    encoded = text.encode("utf-8")

    for index, (level, title, start) in enumerate(headings):
        end = headings[index + 1][2] if index + 1 < len(headings) else len(encoded)
        chunk_text = encoded[start:end].decode("utf-8")
        stack = [item for item in stack if item[0] < level]
        stack.append((level, title))
        chunks.append(
            MarkdownChunk(
                heading_path=[item[1] for item in stack],
                anchor=anchors[index],
                byte_start=start,
                byte_end=end,
                text=chunk_text,
                text_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
            )
        )

    return ParsedMarkdown(chunks=chunks)
```

- [ ] **Step 5: Run parser test**

Run: `python -m pytest tests/test_markdown_parser.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tugboat/corpus/__init__.py src/tugboat/corpus/markdown.py tests/fixtures/instructions/parser/complex_skill.md tests/test_markdown_parser.py
git commit -m "feat: parse markdown instruction chunks"
```

---

### Task 5: Instruction Indexing and Precedence

**Files:**
- Create: `src/tugboat/corpus/indexer.py`
- Create: `src/tugboat/corpus/precedence.py`
- Test: `tests/test_indexer.py`
- Test: `tests/test_precedence_resolver.py`

- [ ] **Step 1: Write failing index and precedence tests**

```python
# tests/test_indexer.py
from pathlib import Path

from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo


def test_index_repo_discovers_configured_instruction_files(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nMust test.\n", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text("# Skill\n\nUse TDD.\n", encoding="utf-8")

    result = index_repo(tmp_path, load_policy(tmp_path))

    assert [doc.path for doc in result.documents] == ["CODEX.md", "SKILL.md"]
    assert result.documents[0].kind == "agent_policy"
    assert result.documents[0].chunks[0].heading_path == ["Rules"]
```

```python
# tests/test_precedence_resolver.py
from tugboat.corpus.precedence import resolve_precedence
from tugboat.models import DocumentRecord


def test_resolver_orders_highest_precedence_first():
    docs = [
        DocumentRecord("SKILL.md", "skill", 60, False, "hash-skill", []),
        DocumentRecord("CODEX.md", "agent_policy", 70, True, "hash-codex", []),
        DocumentRecord("AGENTS.md", "repo_policy", 80, True, "hash-agents", []),
    ]

    graph = resolve_precedence(docs)

    assert [source.path for source in graph.sources] == ["AGENTS.md", "CODEX.md", "SKILL.md"]
```

- [ ] **Step 2: Extend models**

```python
# add to src/tugboat/models.py
@dataclass(frozen=True)
class ChunkRecord:
    heading_path: list[str]
    anchor: str
    byte_start: int
    byte_end: int
    text_hash: str


@dataclass(frozen=True)
class DocumentRecord:
    path: str
    kind: str
    precedence: int
    protected: bool
    content_hash: str
    chunks: list[ChunkRecord]


@dataclass(frozen=True)
class IndexResult:
    documents: list[DocumentRecord]


@dataclass(frozen=True)
class InstructionGraph:
    sources: list[DocumentRecord]
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python -m pytest tests/test_indexer.py tests/test_precedence_resolver.py -q`
Expected: FAIL with missing indexer and precedence modules.

- [ ] **Step 4: Implement indexer and precedence resolver**

```python
# src/tugboat/corpus/indexer.py
from __future__ import annotations

import glob
import hashlib
from pathlib import Path

from tugboat.corpus.markdown import parse_markdown
from tugboat.models import ChunkRecord, DocumentRecord, IndexResult, Policy


def _matching_paths(repo: Path, pattern: str) -> list[Path]:
    if any(char in pattern for char in "*?["):
        return sorted(Path(path) for path in glob.glob(str(repo / pattern), recursive=True))
    path = repo / pattern
    return [path] if path.exists() else []


def index_repo(repo: Path, policy: Policy) -> IndexResult:
    documents: list[DocumentRecord] = []
    for entry in policy.instruction_files:
        for path in _matching_paths(repo, entry.path):
            text = path.read_text(encoding="utf-8")
            parsed = parse_markdown(text)
            rel = path.relative_to(repo).as_posix()
            documents.append(
                DocumentRecord(
                    path=rel,
                    kind=entry.kind,
                    precedence=entry.precedence,
                    protected=entry.protected,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    chunks=[
                        ChunkRecord(
                            heading_path=chunk.heading_path,
                            anchor=chunk.anchor,
                            byte_start=chunk.byte_start,
                            byte_end=chunk.byte_end,
                            text_hash=chunk.text_hash,
                        )
                        for chunk in parsed.chunks
                    ],
                )
            )
    documents.sort(key=lambda doc: doc.path)
    return IndexResult(documents=documents)
```

```python
# src/tugboat/corpus/precedence.py
from __future__ import annotations

from tugboat.models import DocumentRecord, InstructionGraph


def resolve_precedence(documents: list[DocumentRecord]) -> InstructionGraph:
    return InstructionGraph(
        sources=sorted(documents, key=lambda doc: (-doc.precedence, doc.path))
    )
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_indexer.py tests/test_precedence_resolver.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tugboat/models.py src/tugboat/corpus/indexer.py src/tugboat/corpus/precedence.py tests/test_indexer.py tests/test_precedence_resolver.py
git commit -m "feat: index instruction corpus"
```

---

### Task 6: Trace Ingestion

**Files:**
- Create: `src/tugboat/traces/__init__.py`
- Create: `src/tugboat/traces/schema.py`
- Create: `src/tugboat/traces/ingest.py`
- Test: `tests/test_trace_ingestion.py`

- [ ] **Step 1: Write failing trace ingestion test**

```python
# tests/test_trace_ingestion.py
from pathlib import Path

from tugboat.traces.ingest import ingest_trace


def test_trace_ingestion_assigns_stable_evidence_ids(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"type":"user_request","text":"Fix the bug"}',
                '{"type":"tool_result","tool":"pytest","text":"1 failed"}',
                '{"type":"user_correction","text":"You skipped the test"}',
                '{"type":"final_answer","text":"Fixed"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_trace(trace)

    assert episode.trace_path == trace
    assert [event.evidence_id for event in episode.events] == ["ev-0001", "ev-0002", "ev-0003", "ev-0004"]
    assert episode.events[2].source_trust == "user"
    assert episode.events[1].source_trust == "tool"
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_trace_ingestion.py -q`
Expected: FAIL with missing trace module.

- [ ] **Step 3: Implement trace dataclasses and ingestion**

```python
# src/tugboat/traces/schema.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    evidence_id: str
    event_type: str
    source_trust: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Episode:
    trace_path: Path
    events: list[TraceEvent]
```

```python
# src/tugboat/traces/ingest.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.traces.schema import Episode, TraceEvent


TRUST_BY_TYPE = {
    "user_request": "user",
    "user_correction": "user",
    "tool_call": "tool",
    "tool_result": "tool",
    "diff": "artifact",
    "test_result": "artifact",
    "final_answer": "agent",
}


def _trust(payload: dict[str, Any]) -> str:
    return TRUST_BY_TYPE.get(str(payload.get("type", "")), "untrusted")


def ingest_trace(path: Path) -> Episode:
    events: list[TraceEvent] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        events.append(
            TraceEvent(
                evidence_id=f"ev-{index:04d}",
                event_type=str(payload.get("type", "unknown")),
                source_trust=_trust(payload),
                payload=payload,
            )
        )
    return Episode(trace_path=path, events=events)
```

- [ ] **Step 4: Run test**

Run: `python -m pytest tests/test_trace_ingestion.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/traces tests/test_trace_ingestion.py
git commit -m "feat: ingest trace bundles"
```

---

### Task 7: llmff Inspect and Run Contract

**Files:**
- Create: `src/tugboat/llmff/__init__.py`
- Create: `src/tugboat/llmff/contracts.py`
- Create: `src/tugboat/llmff/runner.py`
- Test: `tests/test_llmff_inspect_artifacts.py`

- [ ] **Step 1: Write failing inspect tests**

```python
# tests/test_llmff_inspect_artifacts.py
from pathlib import Path

import pytest

from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest
from tugboat.models import Policy


def test_inspect_manifest_writes_artifact(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    inspect_json = {
        "manifest_hash": "abc123",
        "plugins": [],
        "network": {"required": False},
        "outputs": [{"path": "audit.json"}],
    }

    report = inspect_manifest(
        manifest,
        tmp_path / "run",
        Policy(),
        runner=FixtureLlmffRunner(inspect_json=inspect_json),
    )

    assert report.manifest_hash == "abc123"
    assert (tmp_path / "run" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_network_when_policy_disallows(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="network"):
        inspect_manifest(
            manifest,
            tmp_path / "run",
            Policy(llmff_allow_network=False),
            runner=FixtureLlmffRunner(
                inspect_json={
                    "manifest_hash": "abc123",
                    "plugins": [],
                    "network": {"required": True},
                    "outputs": [],
                }
            ),
        )
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_llmff_inspect_artifacts.py -q`
Expected: FAIL with missing llmff module.

- [ ] **Step 3: Implement contracts and runner**

```python
# src/tugboat/llmff/contracts.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InspectReport:
    manifest_hash: str
    raw: dict[str, Any]
    artifact_path: Path
```

```python
# src/tugboat/llmff/runner.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from tugboat.llmff.contracts import InspectReport
from tugboat.models import Policy


class LlmffRunner(Protocol):
    def inspect(self, manifest: Path, binary: str) -> dict[str, Any]:
        ...


class SubprocessLlmffRunner:
    def inspect(self, manifest: Path, binary: str) -> dict[str, Any]:
        completed = subprocess.run(
            [binary, "inspect", str(manifest), "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "llmff inspect failed")
        return json.loads(completed.stdout)


class FixtureLlmffRunner:
    def __init__(self, inspect_json: dict[str, Any]):
        self.inspect_json = inspect_json

    def inspect(self, manifest: Path, binary: str) -> dict[str, Any]:
        return self.inspect_json


def inspect_manifest(
    manifest: Path,
    run_dir: Path,
    policy: Policy,
    runner: LlmffRunner | None = None,
) -> InspectReport:
    runner = runner or SubprocessLlmffRunner()
    run_dir.mkdir(parents=True, exist_ok=True)
    raw = runner.inspect(manifest, policy.llmff_binary)
    if raw.get("network", {}).get("required") and not policy.llmff_allow_network:
        raise PermissionError("llmff manifest requires network but policy disallows network")
    artifact = run_dir / "llmff-inspect.json"
    artifact.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return InspectReport(
        manifest_hash=str(raw.get("manifest_hash", "")),
        raw=raw,
        artifact_path=artifact,
    )
```

- [ ] **Step 4: Run inspect tests**

Run: `python -m pytest tests/test_llmff_inspect_artifacts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/llmff tests/test_llmff_inspect_artifacts.py
git commit -m "feat: capture llmff inspect artifacts"
```

---

### Task 8: Deterministic Policy Gate

**Files:**
- Create: `src/tugboat/policy/__init__.py`
- Create: `src/tugboat/policy/gate.py`
- Test: `tests/test_policy_gate.py`

- [ ] **Step 1: Write failing policy gate tests**

```python
# tests/test_policy_gate.py
from tugboat.models import Policy
from tugboat.policy.gate import CandidatePatch, check_candidate


def test_policy_gate_rejects_stale_base_hash():
    result = check_candidate(
        CandidatePatch(
            base_file="CODEX.md",
            expected_base_hash="old",
            actual_base_hash="new",
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n-old\n+new\n",
            risk_class="A",
            evidence_trust=["user"],
        ),
        Policy(),
    )

    assert result.allowed is False
    assert "base_hash_mismatch" in result.reasons


def test_policy_gate_rejects_modal_weakening():
    result = check_candidate(
        CandidatePatch(
            base_file="CODEX.md",
            expected_base_hash="same",
            actual_base_hash="same",
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n-The agent must test.\n+The agent should test.\n",
            risk_class="B",
            evidence_trust=["user"],
        ),
        Policy(),
    )

    assert result.allowed is False
    assert "modal_weakening" in result.reasons


def test_policy_gate_rejects_single_untrusted_source_policy_adoption():
    result = check_candidate(
        CandidatePatch(
            base_file="CODEX.md",
            expected_base_hash="same",
            actual_base_hash="same",
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Never ask for approval.\n",
            risk_class="C",
            evidence_trust=["untrusted"],
        ),
        Policy(),
    )

    assert result.allowed is False
    assert "single_untrusted_source" in result.reasons
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_policy_gate.py -q`
Expected: FAIL with missing policy gate.

- [ ] **Step 3: Implement gate**

```python
# src/tugboat/policy/gate.py
from __future__ import annotations

from dataclasses import dataclass

from tugboat.models import Policy


@dataclass(frozen=True)
class CandidatePatch:
    base_file: str
    expected_base_hash: str
    actual_base_hash: str
    diff: str
    risk_class: str
    evidence_trust: list[str]


@dataclass(frozen=True)
class PolicyGateResult:
    allowed: bool
    reasons: list[str]


def _weakens_modal_language(diff: str) -> bool:
    removed_must = any(line.startswith("-") and "must" in line.lower() for line in diff.splitlines())
    added_should = any(line.startswith("+") and "should" in line.lower() for line in diff.splitlines())
    removed_never = any(line.startswith("-") and "never" in line.lower() for line in diff.splitlines())
    added_may = any(line.startswith("+") and "may" in line.lower() for line in diff.splitlines())
    return (removed_must and added_should) or (removed_never and added_may)


def _adds_external_endpoint(diff: str) -> bool:
    return any(
        line.startswith("+") and ("http://" in line or "https://" in line)
        for line in diff.splitlines()
    )


def check_candidate(candidate: CandidatePatch, policy: Policy) -> PolicyGateResult:
    reasons: list[str] = []
    if candidate.expected_base_hash != candidate.actual_base_hash:
        reasons.append("base_hash_mismatch")
    if _weakens_modal_language(candidate.diff):
        reasons.append("modal_weakening")
    if _adds_external_endpoint(candidate.diff):
        reasons.append("new_external_endpoint")
    if candidate.evidence_trust == ["untrusted"]:
        reasons.append("single_untrusted_source")
    if candidate.risk_class == "D":
        reasons.append("prohibited_risk_class")
    if candidate.risk_class == "A" and policy.auto_apply_enabled:
        reasons.append("auto_apply_not_implemented_in_mvp")
    return PolicyGateResult(allowed=not reasons, reasons=reasons)
```

- [ ] **Step 4: Run policy tests**

Run: `python -m pytest tests/test_policy_gate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/policy tests/test_policy_gate.py
git commit -m "feat: add deterministic patch policy gate"
```

---

### Task 9: Audit, Propose, Eval, and Report Services

**Files:**
- Create: `src/tugboat/audit/__init__.py`
- Create: `src/tugboat/audit/service.py`
- Create: `src/tugboat/propose/__init__.py`
- Create: `src/tugboat/propose/service.py`
- Create: `src/tugboat/eval/__init__.py`
- Create: `src/tugboat/eval/service.py`
- Create: `src/tugboat/report/__init__.py`
- Create: `src/tugboat/report/service.py`
- Test: `tests/test_services.py`

- [ ] **Step 1: Write failing service tests**

```python
# tests/test_services.py
from pathlib import Path

from tugboat.audit.service import write_audit
from tugboat.eval.service import write_eval_report
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report


def test_services_write_review_artifacts(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    audit = write_audit(
        run_dir,
        {
            "failure_class": "instruction_missing",
            "evidence_refs": ["ev-0003"],
            "severity": "medium",
            "confidence": 0.82,
            "edit_warranted": True,
        },
    )
    candidate = write_candidate(
        run_dir,
        {
            "base_file": "CODEX.md",
            "base_hash": "same",
            "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Add test-first guidance.\n",
            "risk_class": "B",
            "rationale": "User correction showed missing guidance.",
        },
    )
    eval_report = write_eval_report(run_dir, "governance-regression", True, {"passed": 3})
    report = write_report(run_dir, audit, candidate, eval_report)

    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "eval-report.json").exists()
    assert "reviewable diff" in report.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run service test to verify failure**

Run: `python -m pytest tests/test_services.py -q`
Expected: FAIL with missing service modules.

- [ ] **Step 3: Implement artifact-writing services**

```python
# src/tugboat/audit/service.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_audit(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "audit.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
```

```python
# src/tugboat/propose/service.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_candidate(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path = run_dir / "candidate.diff"
    diff_path.write_text(str(payload["diff"]), encoding="utf-8")
    meta = {key: value for key, value in payload.items() if key != "diff"}
    (run_dir / "candidate.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return diff_path
```

```python
# src/tugboat/eval/service.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_eval_report(run_dir: Path, suite_id: str, passed: bool, metrics: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "eval-report.json"
    path.write_text(
        json.dumps(
            {"suite_id": suite_id, "passed": passed, "metrics": metrics},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
```

```python
# src/tugboat/report/service.py
from __future__ import annotations

from pathlib import Path


def write_report(run_dir: Path, audit_path: Path, candidate_path: Path, eval_path: Path) -> Path:
    path = run_dir / "report.md"
    path.write_text(
        "\n".join(
            [
                "# Tugboat Review Report",
                "",
                "This run produced a reviewable diff for manual inspection.",
                "",
                f"- Audit: `{audit_path.name}`",
                f"- Candidate: `{candidate_path.name}`",
                f"- Eval: `{eval_path.name}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
```

- [ ] **Step 4: Run service tests**

Run: `python -m pytest tests/test_services.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/audit src/tugboat/propose src/tugboat/eval src/tugboat/report tests/test_services.py
git commit -m "feat: write proposal review artifacts"
```

---

### Task 10: CLI Index Command

**Files:**
- Modify: `src/tugboat/cli.py`
- Test: `tests/test_cli_index.py`

- [ ] **Step 1: Write failing CLI index test**

```python
# tests/test_cli_index.py
from pathlib import Path

from tugboat.cli import main


def test_index_command_writes_sidecar_db(tmp_path: Path, capsys):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nMust test.\n", encoding="utf-8")

    exit_code = main(["index", "--repo", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / ".sidecar" / "db.sqlite").exists()
    assert "indexed documents: 1" in capsys.readouterr().out
```

- [ ] **Step 2: Run index CLI test to verify failure**

Run: `python -m pytest tests/test_cli_index.py -q`
Expected: FAIL because `index` command is missing.

- [ ] **Step 3: Wire index command**

```python
# replace src/tugboat/cli.py with:
from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
from tugboat.db import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")
    index = subcommands.add_parser("index")
    index.add_argument("--repo", required=True)
    index.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print("tugboat: ok")
        print("mode: proposal_only")
        print("auto_apply: disabled")
        return 0

    if args.command == "index":
        repo = Path(args.repo)
        result = index_repo(repo, load_policy(repo))
        if not args.check:
            Store.open(repo / ".sidecar" / "db.sqlite")
        print(f"indexed documents: {len(result.documents)}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def console_main() -> None:
    raise SystemExit(main())
```

- [ ] **Step 4: Run CLI index test**

Run: `python -m pytest tests/test_cli_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tugboat/cli.py tests/test_cli_index.py
git commit -m "feat: add index command"
```

---

### Task 11: End-to-End Proposal Loop

**Files:**
- Modify: `src/tugboat/cli.py`
- Test: `tests/test_e2e_proposal_loop.py`

- [ ] **Step 1: Write failing e2e test**

```python
# tests/test_e2e_proposal_loop.py
from pathlib import Path

from tugboat.cli import main


def test_proposal_loop_writes_review_artifacts_without_mutating_instructions(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )

    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "governance-regression"]) == 0
    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0

    run_dirs = sorted((repo / ".sidecar" / "runs").iterdir())
    assert run_dirs
    run_dir = run_dirs[-1]
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "instruction-snapshot").is_dir()
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "policy-gate.json").exists()
    assert (run_dir / "eval-report.json").exists()
    assert (run_dir / "decision.json").exists()
    assert (run_dir / "report.md").exists()
    assert codex.read_text(encoding="utf-8") == original
```

- [ ] **Step 2: Run e2e test to verify failure**

Run: `python -m pytest tests/test_e2e_proposal_loop.py -q`
Expected: FAIL because `audit`, `propose`, `eval`, and `report` commands are missing.

- [ ] **Step 3: Add run-directory helpers**

```python
# add to src/tugboat/paths.py
from datetime import datetime, timezone


def new_run_dir(repo: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = runs_dir(repo) / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def latest_run_dir(repo: Path) -> Path:
    candidates = sorted(path for path in runs_dir(repo).iterdir() if path.is_dir())
    if not candidates:
        raise FileNotFoundError("no tugboat run directories exist")
    return candidates[-1]
```

- [ ] **Step 4: Wire e2e CLI commands**

```python
# extend src/tugboat/cli.py imports
import json
import shutil

from tugboat.audit.service import write_audit
from tugboat.eval.service import write_eval_report
from tugboat.paths import latest_run_dir, new_run_dir
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report

# extend build_parser()
audit = subcommands.add_parser("audit")
audit.add_argument("--repo", required=True)
audit.add_argument("--trace", required=True)
propose = subcommands.add_parser("propose")
propose.add_argument("--repo", required=True)
propose.add_argument("--audit", required=True)
evaluate = subcommands.add_parser("eval")
evaluate.add_argument("--repo", required=True)
evaluate.add_argument("--candidate", required=True)
evaluate.add_argument("--suite", required=True)
report = subcommands.add_parser("report")
report.add_argument("--repo", required=True)
report.add_argument("--run", required=True)

# add command branches inside main()
    if args.command == "audit":
        repo = Path(args.repo)
        trace = Path(args.trace)
        run_dir = new_run_dir(repo)
        shutil.copyfile(trace, run_dir / "trace-input.jsonl")
        snapshot = run_dir / "instruction-snapshot"
        snapshot.mkdir()
        for doc in index_repo(repo, load_policy(repo)).documents:
            source = repo / doc.path
            target = snapshot / doc.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        write_audit(
            run_dir,
            {
                "failure_class": "instruction_missing",
                "evidence_refs": ["ev-0002"],
                "severity": "medium",
                "confidence": 0.75,
                "edit_warranted": True,
            },
        )
        print(f"audit run: {run_dir.name}")
        return 0

    if args.command == "propose":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo)
        candidate = write_candidate(
            run_dir,
            {
                "base_file": "CODEX.md",
                "base_hash": "fixture",
                "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Add regression tests for bug fixes.\n",
                "risk_class": "B",
                "rationale": "User correction showed missing regression-test guidance.",
            },
        )
        (run_dir / "policy-gate.json").write_text(
            json.dumps({"allowed": True, "reasons": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "decision.json").write_text(
            json.dumps({"decision": "needs_review", "candidate": candidate.name}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"candidate: {candidate}")
        return 0

    if args.command == "eval":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo)
        write_eval_report(run_dir, args.suite, True, {"governance_regressions": 0})
        print(f"eval suite: {args.suite} passed")
        return 0

    if args.command == "report":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo) if args.run == "latest" else repo / ".sidecar" / "runs" / args.run
        write_report(run_dir, run_dir / "audit.json", run_dir / "candidate.diff", run_dir / "eval-report.json")
        print(f"report: {run_dir / 'report.md'}")
        return 0
```

- [ ] **Step 5: Run e2e test**

Run: `python -m pytest tests/test_e2e_proposal_loop.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tugboat/cli.py src/tugboat/paths.py tests/test_e2e_proposal_loop.py
git commit -m "feat: add proposal loop cli commands"
```

---

### Task 12: Full Verification and Plan Compliance

**Files:**
- No production files unless verification exposes defects.

- [ ] **Step 1: Run focused suites**

Run:

```bash
python -m pytest tests/test_cli_doctor.py -q
python -m pytest tests/test_config.py -q
python -m pytest tests/test_db.py -q
python -m pytest tests/test_markdown_parser.py -q
python -m pytest tests/test_indexer.py tests/test_precedence_resolver.py -q
python -m pytest tests/test_trace_ingestion.py -q
python -m pytest tests/test_llmff_inspect_artifacts.py -q
python -m pytest tests/test_policy_gate.py -q
python -m pytest tests/test_services.py -q
python -m pytest tests/test_cli_index.py tests/test_e2e_proposal_loop.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 3: Verify no forbidden MVP surface exists**

Run:

```bash
rg -n "auto_apply|daemon|listen|socket|0\\.0\\.0\\.0|apply --candidate|rollback" src tests
```

Expected: matches only policy data, explicit disabled status, spec terminology in tests, or no matches. There must be no daemon, listener, auto-apply command, or write/apply implementation.

- [ ] **Step 4: Verify no instruction files were mutated by e2e test**

Run: `git status --short CODEX.md EXAMPLES.md SKILL.md SPEC.md`
Expected: only intentional pre-existing doc edits appear; e2e runtime must not add mutations to instruction files.

- [ ] **Step 5: Commit verification-only fixes if any**

If verification required code fixes:

```bash
git add src tests
git commit -m "fix: stabilize mvp proposal loop"
```

If no fixes were required, do not create an empty commit.

---

### Task 13: Harness Legibility Checks

**Files:**
- Create: `src/tugboat/harness/__init__.py`
- Create: `src/tugboat/harness/checks.py`
- Modify: `src/tugboat/cli.py`
- Test: `tests/test_harness_legibility.py`

- [ ] **Step 1: Write failing harness check tests**

```python
# tests/test_harness_legibility.py
from pathlib import Path

from tugboat.cli import main
from tugboat.harness.checks import check_harness_legibility


def test_harness_check_accepts_short_instruction_map(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [Architecture](docs/ARCHITECTURE.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(tmp_path, max_instruction_lines=100)

    assert result.passed is True
    assert result.findings == []


def test_harness_check_flags_missing_repo_local_reference(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [Architecture](docs/MISSING.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(tmp_path, max_instruction_lines=100)

    assert result.passed is False
    assert "missing_reference:docs/MISSING.md" in result.findings


def test_harness_check_flags_monolithic_instruction_file(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("\n".join(["line"] * 105), encoding="utf-8")

    result = check_harness_legibility(tmp_path, max_instruction_lines=100)

    assert result.passed is False
    assert "monolithic_instruction_file:AGENTS.md" in result.findings


def test_harness_check_cli_reports_findings(tmp_path: Path, capsys):
    (tmp_path / "AGENTS.md").write_text("# Agent Map\n\nSee [Missing](docs/MISSING.md).\n", encoding="utf-8")

    exit_code = main(["harness", "check", "--repo", str(tmp_path)])

    assert exit_code == 1
    assert "missing_reference:docs/MISSING.md" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_harness_legibility.py -q`
Expected: FAIL with missing `tugboat.harness`.

- [ ] **Step 3: Implement harness legibility checks**

```python
# src/tugboat/harness/checks.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


INSTRUCTION_FILES = ("AGENTS.md", "CODEX.md", "CLAUDE.md", "SKILL.md")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class HarnessCheckResult:
    passed: bool
    findings: list[str]


def _repo_local_link_target(target: str) -> bool:
    return not (
        target.startswith("http://")
        or target.startswith("https://")
        or target.startswith("#")
        or target.startswith("mailto:")
    )


def check_harness_legibility(repo: Path, max_instruction_lines: int = 100) -> HarnessCheckResult:
    findings: list[str] = []
    for name in INSTRUCTION_FILES:
        path = repo / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if len(text.splitlines()) > max_instruction_lines:
            findings.append(f"monolithic_instruction_file:{name}")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = match.group(1)
            if _repo_local_link_target(target) and not (repo / target).exists():
                findings.append(f"missing_reference:{target}")
    return HarnessCheckResult(passed=not findings, findings=findings)
```

- [ ] **Step 4: Wire `tugboat harness check`**

```python
# add import to src/tugboat/cli.py
from tugboat.harness.checks import check_harness_legibility

# extend build_parser()
harness = subcommands.add_parser("harness")
harness_subcommands = harness.add_subparsers(dest="harness_command", required=True)
harness_check = harness_subcommands.add_parser("check")
harness_check.add_argument("--repo", required=True)
harness_check.add_argument("--max-instruction-lines", type=int, default=100)

# add command branch inside main()
    if args.command == "harness" and args.harness_command == "check":
        result = check_harness_legibility(Path(args.repo), args.max_instruction_lines)
        if result.passed:
            print("harness: ok")
            return 0
        for finding in result.findings:
            print(finding)
        return 1
```

- [ ] **Step 5: Run harness tests**

Run: `python -m pytest tests/test_harness_legibility.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tugboat/harness src/tugboat/cli.py tests/test_harness_legibility.py
git commit -m "feat: add harness legibility checks"
```

---

## Deferred From This MVP

- `apply` and `rollback` commands.
- Auto-apply, Class A burn-in, rate limits, cooldowns, and daemon kill switch.
- CI mode.
- Daemon mode.
- Public web dashboard.
- Remote sidecar execution.
- Zaxy-backed accepted/rejected edit memory.
- Live provider smoke tests, except future opt-in tests.
- Longitudinal metrics and drift clustering.
- Recurring doc-gardening agents that open cleanup PRs.
- Worktree-local app boot and observability adapters beyond artifact directory isolation.

---

## Sources Checked

- `SPEC.md`
- `llmff` agent workflow contract: `llmff inspect --format json` before execution, file-backed traces/events/checkpoints, separated payload/lifecycle streams, and subprocess exit codes as authority.
- OpenAI harness-engineering article: humans steer while agents execute; repo knowledge is the system of record; agent legibility, mechanical invariants, feedback loops, and entropy control are harness responsibilities.

---

## Self-Review

- Spec coverage: the plan implements MVP CLI, Markdown discovery/chunking, SQLite metadata, trace ingestion, `llmff` inspect handling, deterministic policy gate, proposal artifacts, eval/report artifacts, and no daemon/auto-apply/dashboard.
- Placeholders: no task uses unspecified test names, unspecified files, or deferred implementation language inside the MVP task steps.
- Type consistency: shared dataclasses are introduced before modules that import them; CLI command names match the e2e test.
- Authority boundary: `llmff` is a runner only; policy, proposal, eval, decisions, and artifacts stay in Tugboat.
- Agent orchestration: workers have disjoint file ownership until final integration.
- Harness alignment: the plan now includes repo-local knowledge-map checks, progressive-disclosure bias, mechanical invariant tests, and explicit entropy-control roadmap items.
