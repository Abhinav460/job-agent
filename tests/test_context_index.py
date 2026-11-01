from dataclasses import replace
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from jobagent import context_index as context
from jobagent import resume_catalog


def test_windows_cli_index_requires_private_storage_support(monkeypatch):
    from jobagent import cli

    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="Windows ACL"):
        cli.cmd_index_resumes(SimpleNamespace(jobagent_home=Path("unused")),
                              SimpleNamespace(folder="unused", recursive=False))


@pytest.fixture
def private(tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir()
    monkeypatch.setattr(context, "REPO_ROOT", public)
    monkeypatch.setattr(resume_catalog, "REPO_ROOT", public)
    source = tmp_path / "private-resumes"
    source.mkdir()
    (source / "python.txt").write_text("Python SQL data pipelines analytics PRIVATE_DOCUMENT_MARKER", encoding="utf-8")
    (source / "web.txt").write_text("JavaScript React frontend browser interfaces", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("default context index must not contact network services")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return source, tmp_path / "private-home", public


def test_default_embedded_index_persists_and_has_private_payload_only(private):
    source, home, _ = private
    catalog = resume_catalog.index_directory(source)
    expected = {d.id: d.digest for d in catalog.documents}
    python_id = next(d.id for d in catalog.documents if d.path.name == "python.txt")
    with context.ContextIndex(home) as index:
        assert index.sync(catalog) == context.SyncResult(2, 0, 0, 0)
        assert index.sync(catalog) == context.SyncResult(0, 2, 0, 0)
        hits = index.search("Python SQL data pipelines")
        assert hits[0].document_id == python_id and hits[0].score > 0
        assert hits[0].digest == expected[python_id]
        points, _ = index._client.scroll(context.COLLECTION, with_payload=True, with_vectors=True)
        for point in points:
            assert set(point.payload) == {"document_id", "digest"}
            assert point.payload["digest"] == expected[point.payload["document_id"]]
            assert len(point.vector) == context.DIMENSIONS
            serialized = json.dumps(point.payload)
            assert "PRIVATE_DOCUMENT_MARKER" not in serialized and str(source) not in serialized
        assert index.directory.is_relative_to(home)
        metadata = json.loads((index.directory / "embedding.json").read_text(encoding="utf-8"))
        assert set(metadata) == {"version", "embedding_fingerprint"}
    with context.ContextIndex(home) as reopened:
        assert reopened.search("Python SQL data pipelines")[0].document_id == python_id


def test_sync_updates_and_removes_deleted_or_unextractable_documents(private):
    source, home, _ = private
    with context.ContextIndex(home) as index:
        index.sync(resume_catalog.index_directory(source))
        (source / "python.txt").write_text("Python SQL orchestration", encoding="utf-8")
        assert index.sync(resume_catalog.index_directory(source)) == context.SyncResult(1, 1, 0, 0)
        (source / "web.txt").unlink()
        assert index.sync(resume_catalog.index_directory(source)) == context.SyncResult(0, 1, 1, 0)
        (source / "python.txt").write_bytes(b"invalid\x00text")
        assert index.sync(resume_catalog.index_directory(source)) == context.SyncResult(0, 0, 1, 1)
        assert index.search("Python") == []


def test_empty_and_truncated_catalogs_do_not_delete_unseen_documents(private):
    source, home, _ = private
    catalog = resume_catalog.index_directory(source)
    with context.ContextIndex(home) as index:
        assert index.search("Python") == []
        empty = resume_catalog.ResumeCatalog(source, ())
        assert index.sync(empty) == context.SyncResult(0, 0, 0, 0)
        index.sync(catalog)
        with pytest.raises(context.ContextIndexError, match="complete catalog"):
            index.sync(replace(catalog, documents=(), truncated=True))
        assert index.search("Python")
        assert index.sync(empty).deleted == 2


def test_incremental_folder_sync_keeps_other_folder_documents(private):
    source, home, _ = private
    other = source.parent / "other-resumes"
    other.mkdir()
    (other / "cloud.txt").write_text("Cloud infrastructure Kubernetes", encoding="utf-8")
    with context.ContextIndex(home) as index:
        index.sync(resume_catalog.index_directory(source), prune=False)
        assert index.sync(resume_catalog.index_directory(other), prune=False) == context.SyncResult(1, 0, 0, 0)
        assert len(index._existing()) == 3


def test_feature_hashing_is_deterministic_and_normalized():
    first, second = context.FeatureHasher(), context.FeatureHasher()
    document = first.embed(["Python SQL Python"], "document")[0]
    assert document == second.embed(["Python SQL Python"], "query")[0]
    assert len(document) == context.DIMENSIONS
    assert sum(v * v for v in document) == pytest.approx(1.0)
    assert first.embed(["the and of"], "query")[0] == [0.0] * context.DIMENSIONS


def test_explicit_embedder_is_opt_in_and_unchanged_docs_are_not_embedded(private):
    source, home, _ = private
    calls = []
    class FakeEmbedder:
        def embed(self, texts, kind):
            calls.append((list(texts), kind))
            return [[1.0, 0.0] if "Python" in text else [0.0, 1.0] for text in texts]
    catalog = resume_catalog.index_directory(source)
    with context.ContextIndex(home, embedder=FakeEmbedder(), embedding_key="fake-model-v1") as index:
        index.sync(catalog)
        assert len(calls) == 1 and calls[0][1] == "document"
        index.sync(catalog)
        assert len(calls) == 1
        assert index.search("Python")[0].score == pytest.approx(1.0)
        assert calls[-1] == (["Python"], "query")
    with pytest.raises(context.ContextIndexError, match="mode changed"):
        context.ContextIndex(home)
    with pytest.raises(context.ContextIndexError, match="mode changed"):
        context.ContextIndex(home, embedder=FakeEmbedder(), embedding_key="fake-model-v2")
    with context.ContextIndex(home, embedder=FakeEmbedder(), embedding_key="fake-model-v1") as index:
        assert index.search("Python")


def test_embedding_failure_preserves_previous_index_and_hides_error(private):
    source, home, _ = private
    class FakeEmbedder:
        fail = False
        def embed(self, texts, kind):
            if self.fail:
                raise RuntimeError("PRIVATE_DOCUMENT_MARKER and private server URL")
            return [[1.0, 0.0] for text in texts]
    embedder = FakeEmbedder()
    with context.ContextIndex(home, embedder=embedder, embedding_key="fake") as index:
        catalog = resume_catalog.index_directory(source)
        index.sync(catalog)
        (source / "python.txt").write_text("changed Python data", encoding="utf-8")
        (source / "web.txt").unlink()
        embedder.fail = True
        with pytest.raises(context.ContextIndexError) as error:
            index.sync(resume_catalog.index_directory(source))
        assert "PRIVATE_DOCUMENT_MARKER" not in str(error.value)
        embedder.fail = False
        assert len(index.search("Python")) == 2


@pytest.mark.parametrize("vectors", [[], [[float("nan")]], [[1.0], [1.0, 2.0]]])
def test_malformed_embedding_vectors_fail_privately(private, vectors):
    source, home, _ = private
    class BadEmbedder:
        def embed(self, texts, kind): return vectors
    with context.ContextIndex(home, embedder=BadEmbedder(), embedding_key="bad-model") as index:
        with pytest.raises(context.ContextIndexError, match="embedding failed"):
            index.sync(resume_catalog.index_directory(source))


def test_repository_home_invalid_payload_and_closed_index_are_rejected(private):
    source, home, public = private
    with pytest.raises(context.ContextIndexError, match="outside the public"):
        context.ContextIndex(public / "index")
    with context.ContextIndex(home) as index:
        catalog = resume_catalog.index_directory(source)
        bad = replace(catalog.documents[0], id="private filename")
        with pytest.raises(context.ContextIndexError, match="invalid document metadata"):
            index.sync(replace(catalog, documents=(bad,)))
        index.sync(catalog)
        points, _ = index._client.scroll(context.COLLECTION, with_payload=True)
        index._client.set_payload(context.COLLECTION, payload={"text": "PRIVATE_DOCUMENT_MARKER"},
                                  points=[points[0].id])
        with pytest.raises(context.ContextIndexError, match="unexpected metadata"):
            index.sync(catalog)
    with pytest.raises(context.ContextIndexError, match="closed"):
        index.search("Python")
