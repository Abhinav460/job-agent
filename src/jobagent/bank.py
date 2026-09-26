"""Vector index over the bullet bank (Qdrant + Ollama embeddings).

Qdrant stores only vectors plus ids, domains and a content hash. Bullet text
stays in bullets.yaml and is looked up by id, so the server holds no resume
prose.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
from qdrant_client import QdrantClient, models

from jobagent.bullets import BulletBank, BulletRef
from jobagent.config import Settings

_ID_NAMESPACE = uuid.UUID("5f0c6f2e-4a7e-4c55-9d2b-8a1f3e0b6c11")
# Small boost for bullets tagged with one of the job's domains.
DOMAIN_BOOST = 0.05


@dataclass(frozen=True)
class ScoredBullet:
    bullet_id: str
    score: float


class Embedder:
    """nomic-embed-text via Ollama's /api/embed, with the model's task prefixes."""

    _PREFIX = {"document": "search_document: ", "query": "search_query: "}

    def __init__(self, base_url: str, model: str, timeout: float = 60.0) -> None:
        self._url = base_url.rstrip("/") + "/api/embed"
        self._model = model
        self._timeout = timeout

    def embed(self, texts: list[str], kind: Literal["document", "query"]) -> list[list[float]]:
        if not texts:
            return []
        response = httpx.post(
            self._url,
            json={"model": self._model, "input": [self._PREFIX[kind] + t for t in texts]},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()["embeddings"]


def _point_id(bullet_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, bullet_id))


def _document_text(ref: BulletRef) -> str:
    return f"{ref.entry.heading}: {ref.bullet.text}"


def _content_hash(ref: BulletRef) -> str:
    return hashlib.sha256(_document_text(ref).encode()).hexdigest()


class BulletIndex:
    def __init__(self, client: QdrantClient, collection: str, embedder: Embedder) -> None:
        self._client = client
        self._collection = collection
        self._embedder = embedder

    @classmethod
    def from_settings(cls, settings: Settings) -> BulletIndex:
        if not settings.qdrant_url or not settings.ollama_base_url:
            raise RuntimeError("QDRANT_URL and OLLAMA_BASE_URL must be set in .env")
        api_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        client = QdrantClient(url=settings.qdrant_url, api_key=api_key, timeout=30)
        return cls(client, settings.qdrant_collection, Embedder(settings.ollama_base_url, settings.ollama_embed_model))

    def _existing(self) -> dict[str, str]:
        """point id -> content hash for everything already indexed."""
        existing: dict[str, str] = {}
        offset = None
        while True:
            points, offset = self._client.scroll(
                self._collection, limit=256, offset=offset, with_payload=["content_hash"], with_vectors=False
            )
            existing.update({str(p.id): (p.payload or {}).get("content_hash", "") for p in points})
            if offset is None:
                return existing

    def sync(self, bank: BulletBank) -> tuple[int, int]:
        """Make the index match bullets.yaml. Returns (embedded, deleted)."""
        refs = list(bank.refs())
        if not self._client.collection_exists(self._collection):
            dim = len(self._embedder.embed(["dimension probe"], "document")[0])
            self._client.create_collection(
                self._collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )
        existing = self._existing()
        changed = [r for r in refs if existing.get(_point_id(r.bullet.id)) != _content_hash(r)]
        vectors = self._embedder.embed([_document_text(r) for r in changed], "document")
        if changed:
            self._client.upsert(
                self._collection,
                points=[
                    models.PointStruct(
                        id=_point_id(r.bullet.id),
                        vector=vec,
                        payload={
                            "bullet_id": r.bullet.id,
                            "entry_id": r.entry.id,
                            "section": r.section,
                            "domains": list(r.bullet.domains),
                            "content_hash": _content_hash(r),
                        },
                    )
                    for r, vec in zip(changed, vectors, strict=True)
                ],
            )
        stale = set(existing) - {_point_id(r.bullet.id) for r in refs}
        if stale:
            self._client.delete(self._collection, points_selector=models.PointIdsList(points=list(stale)))
        return len(changed), len(stale)

    def retrieve(self, query: str, domains: list[str], limit: int) -> list[ScoredBullet]:
        vector = self._embedder.embed([query], "query")[0]
        result = self._client.query_points(self._collection, query=vector, limit=limit, with_payload=True)
        scored = []
        for point in result.points:
            payload = point.payload or {}
            boost = DOMAIN_BOOST if set(payload.get("domains", [])) & set(domains) else 0.0
            scored.append(ScoredBullet(payload["bullet_id"], point.score + boost))
        return sorted(scored, key=lambda s: s.score, reverse=True)
