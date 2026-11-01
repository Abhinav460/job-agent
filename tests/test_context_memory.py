"""Reusable context stays private and searchable with fictional notes."""

from pathlib import Path
import tempfile

import pytest

from jobagent.context_memory import COLLECTION, ContextMemory, ContextMemoryError


def test_saved_context_is_reused_and_qdrant_payload_has_no_text():
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        with ContextMemory(home) as memory:
            python = memory.add("Python systems", "Built Python services and SQL pipelines")
            memory.add("Frontend", "Built React interfaces and accessible forms")
            hits = memory.search("Python SQL backend engineer")
            assert hits and hits[0].note == python
            points, _ = memory._client.scroll(COLLECTION, with_payload=True, with_vectors=False)
            assert all(set(point.payload) == {"id", "digest"} for point in points)
            assert "Python systems" not in str([point.payload for point in points])
        with ContextMemory(home) as memory:
            assert len(memory.list_notes()) == 2
            assert memory.sync() == (0, 0)
            assert memory.search("React forms")[0].note.title == "Frontend"
            memory.delete(python.id)
            assert all(hit.note.id != python.id for hit in memory.search("Python SQL"))


def test_invalid_context_is_rejected_without_leaking_values():
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        with ContextMemory(home) as memory:
            with pytest.raises(ContextMemoryError):
                memory.add("", "secret-marker")
            with pytest.raises(ContextMemoryError):
                memory.search(" ")
            assert memory.list_notes() == []
        (home / "context_notes.json").write_text('not valid "secret-marker"', encoding="utf-8")
        with ContextMemory(home) as memory:
            with pytest.raises(ContextMemoryError) as error:
                memory.list_notes()
            assert "secret-marker" not in str(error.value)


def test_note_writes_are_retry_safe_when_index_is_unavailable(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        with ContextMemory(home) as memory:
            real_sync = memory.sync
            monkeypatch.setattr(memory, "sync", lambda: (_ for _ in ()).throw(
                ContextMemoryError("private context index could not be updated")))
            note = memory.add("Backend", "Python SQL services")
            assert memory.add("Backend", "Python SQL services").id == note.id
            assert len(memory.list_notes()) == 1
            monkeypatch.setattr(memory, "sync", real_sync)
            assert memory.search("Python SQL")[0].note.id == note.id
            monkeypatch.setattr(memory, "sync", lambda: (_ for _ in ()).throw(
                ContextMemoryError("private context index could not be updated")))
            memory.delete(note.id)
            memory.delete(note.id)
        with ContextMemory(home) as reopened:
            assert reopened.search("Python SQL") == []
