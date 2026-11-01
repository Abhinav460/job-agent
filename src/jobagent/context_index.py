"""Private embedded Qdrant resume index, with no service calls by default.

Only vectors, opaque document IDs and content digests enter Qdrant. Text and
paths stay in the caller's ResumeCatalog. The default feature-hashing vectors
measure lexical context overlap; an explicit Ollama Embedder enables semantic
vectors and sends text to that configured service. Close the index before
opening another instance at the same private home.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from qdrant_client import QdrantClient, models

from jobagent import safepaths
from jobagent.resume_catalog import MAX_TEXT_CHARS, ResumeCatalog

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTION = "resume_context"
DIMENSIONS = 512
NAMESPACE = uuid.UUID("1b1654ea-23e8-4a88-8f0f-4c5a3918ee92")
DOCUMENT_ID = re.compile(r"resume-[0-9a-f]{24}")
DIGEST = re.compile(r"[0-9a-f]{64}")
STOP_WORDS = set("a an and are as at be by for from in is it of on or our that the their this to with you your".split())


class ContextIndexError(ValueError):
    """Fixed messages only; underlying errors can contain private requests."""


@dataclass(frozen=True)
class SyncResult:
    indexed: int
    unchanged: int
    deleted: int
    skipped: int


@dataclass(frozen=True)
class ContextHit:
    document_id: str
    score: float
    digest: str


class FeatureHasher:
    """Deterministic signed term hashing with no downloads, training or network."""

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        vectors = []
        for text in texts:
            terms = Counter(t for t in re.findall(r"[a-z][a-z0-9+#]*(?:\.[a-z][a-z0-9]*)*", text.lower())
                            if t not in STOP_WORDS)
            vector = [0.0] * DIMENSIONS
            for term, count in terms.items():
                digest = hashlib.blake2b(term.encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % DIMENSIONS
                sign = -1.0 if digest[4] & 1 else 1.0
                vector[bucket] += sign * (1 + math.log(count))
            norm = math.sqrt(sum(v * v for v in vector))
            vectors.append([v / norm for v in vector] if norm else vector)
        return vectors


def _private(path: Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve()
        if resolved == REPO_ROOT.resolve() or resolved.is_relative_to(REPO_ROOT.resolve()):
            raise ContextIndexError("context index home must be outside the public repository")
        return resolved
    except ContextIndexError:
        raise
    except Exception:
        raise ContextIndexError("private context index path is unavailable") from None


def _point_id(identifier: str) -> str:
    return str(uuid.uuid5(NAMESPACE, identifier))


class ContextIndex:
    def __init__(self, home: Path, *, embedder=None, embedding_key: str | None = None):
        self._client = None
        self.home = _private(home)
        if embedder is None:
            self._embedder = FeatureHasher()
            key = "local-feature-hash-v1:512"
        else:
            # bank.Embedder exposes its configured model as _model. Other
            # adapters must identify their model/version explicitly.
            model = embedding_key or getattr(embedder, "_model", None)
            if not isinstance(model, str) or not model.strip() or len(model) > 256:
                raise ContextIndexError("an explicit embedding model/version key is required")
            self._embedder = embedder
            key = "configured-embedding:" + model
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        try:
            directory = safepaths.ensure_private_dir(self.home / "indexes" / "resume_context", [self.home])
            self.directory = _private(directory)
            directory.chmod(0o700)
            manifest = directory / "embedding.json"
            safepaths.resolve_within(manifest, [directory])
            if manifest.exists():
                metadata = json.loads(safepaths.read_text(manifest, [directory]))
                if metadata != {"version": 1, "embedding_fingerprint": fingerprint}:
                    raise ContextIndexError("embedding mode changed; use a separate private index home")
            else:
                if (directory / "meta.json").exists():
                    raise ContextIndexError("index embedding metadata is missing; rebuild the private index")
                safepaths.write_text(manifest, json.dumps({"version": 1, "embedding_fingerprint": fingerprint}), [directory])
            self._client = QdrantClient(path=str(directory), check_compatibility=False)
        except ContextIndexError:
            raise
        except Exception:
            raise ContextIndexError("private context index could not be opened") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._client is not None:
            client, self._client = self._client, None
            try:
                client.close()
            except Exception:
                raise ContextIndexError("private context index could not be closed") from None

    def _require_open(self):
        if self._client is None:
            raise ContextIndexError("private context index is closed")

    def _embed(self, texts: list[str], kind: str) -> list[list[float]]:
        try:
            result = self._embedder.embed(texts, kind)
            if len(result) != len(texts):
                raise ValueError
            vectors = [[float(value) for value in vector] for vector in result]
            dimension = len(vectors[0])
            if not 1 <= dimension <= 8192 or any(len(v) != dimension for v in vectors):
                raise ValueError
            if any(not math.isfinite(n) for vector in vectors for n in vector):
                raise ValueError
            return vectors
        except Exception:
            raise ContextIndexError("context embedding failed; existing index remains available") from None

    def _existing(self) -> dict[str, tuple[str, str]]:
        if not self._client.collection_exists(COLLECTION):
            return {}
        existing, offset = {}, None
        while True:
            points, offset = self._client.scroll(COLLECTION, limit=256, offset=offset,
                                                 with_payload=True, with_vectors=False)
            for point in points:
                payload = point.payload or {}
                identifier, digest = payload.get("document_id", ""), payload.get("digest", "")
                if (set(payload) != {"document_id", "digest"}
                        or DOCUMENT_ID.fullmatch(identifier) is None or DIGEST.fullmatch(digest) is None
                        or str(point.id) != _point_id(identifier)):
                    raise ContextIndexError("private context index contains unexpected metadata")
                existing[identifier] = (str(point.id), digest)
            if offset is None:
                return existing

    def _collection(self, dimension: int):
        if not self._client.collection_exists(COLLECTION):
            self._client.create_collection(COLLECTION,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
        else:
            vectors = self._client.get_collection(COLLECTION).config.params.vectors
            if not isinstance(vectors, models.VectorParams) or vectors.size != dimension:
                raise ContextIndexError("embedding dimensions changed; use a separate private index home")

    def sync(self, catalog: ResumeCatalog, *, prune: bool = True) -> SyncResult:
        self._require_open()
        if catalog.truncated:
            raise ContextIndexError("sync requires a complete catalog; increase the document limit")
        documents = [document for document in catalog.documents if document.status == "ready"]
        ids = [d.id for d in documents]
        if (len(set(ids)) != len(ids) or any(DOCUMENT_ID.fullmatch(d.id) is None
                or not isinstance(d.digest, str) or DIGEST.fullmatch(d.digest) is None
                or not d.text.strip() or len(d.text) > MAX_TEXT_CHARS for d in documents)):
            raise ContextIndexError("catalog contains invalid document metadata")
        try:
            existing = self._existing()
            changed = [d for d in documents if d.id not in existing or existing[d.id][1] != d.digest]
            if changed:
                vectors = self._embed([d.text for d in changed], "document")
                self._collection(len(vectors[0]))
                self._client.upsert(COLLECTION, points=[
                    models.PointStruct(id=_point_id(d.id), vector=vector,
                                       payload={"document_id": d.id, "digest": d.digest})
                    for d, vector in zip(changed, vectors)], wait=True)
            stale = (set(existing) - set(ids)) if prune else set()
            if stale:
                self._client.delete(COLLECTION, points_selector=models.PointIdsList(
                    points=[existing[identifier][0] for identifier in sorted(stale)]), wait=True)
            return SyncResult(len(changed), len(documents) - len(changed), len(stale),
                              len(catalog.documents) - len(documents))
        except ContextIndexError:
            raise
        except Exception:
            raise ContextIndexError("private context index sync failed") from None

    def search(self, job_text: str, *, limit: int = 5) -> list[ContextHit]:
        self._require_open()
        if not isinstance(job_text, str) or not job_text.strip() or len(job_text) > MAX_TEXT_CHARS:
            raise ContextIndexError("provide bounded job role or description text")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ContextIndexError("retrieval limit must be between 1 and 1000")
        try:
            if not self._client.collection_exists(COLLECTION):
                return []
            vector = self._embed([job_text], "query")[0]
            self._collection(len(vector))
            points = self._client.query_points(COLLECTION, query=vector, limit=limit,
                                               with_payload=True, with_vectors=False).points
            hits = []
            for point in points:
                payload = point.payload or {}
                identifier, digest = payload.get("document_id", ""), payload.get("digest", "")
                if (set(payload) != {"document_id", "digest"}
                        or DOCUMENT_ID.fullmatch(identifier) is None or DIGEST.fullmatch(digest) is None):
                    raise ContextIndexError("private context index contains unexpected metadata")
                if point.score > 0:
                    hits.append(ContextHit(identifier, float(point.score), digest))
            return hits
        except ContextIndexError:
            raise
        except Exception:
            raise ContextIndexError("private context index retrieval failed") from None
