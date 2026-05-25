from pathlib import Path

import pytest

from tugboat.db import Store


def test_store_initializes_core_tables(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
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
    with Store.open(tmp_path / "db.sqlite") as store:
        first = store.append_audit_event("run.created", {"run_id": "run-1"})
        second = store.append_audit_event("run.completed", {"run_id": "run-1"})

        assert first.sequence == 1
        assert first.previous_hash == ""
        assert second.sequence == 2
        assert second.previous_hash == first.event_hash


def test_audit_event_update_is_not_supported(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        event = store.append_audit_event("run.created", {"run_id": "run-1"})

        with pytest.raises(PermissionError):
            store.update_audit_event(event.sequence, {"event_type": "tampered"})


def test_store_can_be_used_as_context_manager(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.append_audit_event("run.created", {"run_id": "run-1"})

    with pytest.raises(Exception, match="closed"):
        store.table_names()
