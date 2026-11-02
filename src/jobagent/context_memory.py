"""Private, reusable applicant notes with a local Qdrant search index.

The source notes live in a private JSON file. Qdrant receives only opaque IDs,
digests, and local feature-hash vectors; it never receives note text or paths.
Search results are suggestions for the applicant to review, never form answers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from qdrant_client import QdrantClient, models

from jobagent.context_index import DIGEST, DIMENSIONS, FeatureHasher, _private
from jobagent.safepaths import ensure_private_dir, read_text, resolve_within
from jobagent.private_storage import secure_home


COLLECTION = "applicant_notes"
NOTE_ID = re.compile(r"[0-9a-f]{32}")
MAX_NOTES = 200


class ContextMemoryError(ValueError):
    """Fixed messages avoid leaking note contents through diagnostics."""


@dataclass(frozen=True, repr=False)
class ContextNote:
    id: str
    title: str
    body: str

    def __repr__(self) -> str:
        return f"ContextNote(id={self.id!r}, <private>)"


@dataclass(frozen=True, repr=False)
class ContextSuggestion:
    note: ContextNote
    score: float

    def __repr__(self) -> str:
        return f"ContextSuggestion(score={self.score:.3f}, <private>)"


def _valid_note(note: object) -> bool:
    return (isinstance(note, dict) and set(note) == {"id", "title", "body"}
            and isinstance(note["id"], str) and NOTE_ID.fullmatch(note["id"]) is not None
            and isinstance(note["title"], str) and 1 <= len(note["title"].strip()) <= 120
            and not any(ord(char) < 32 for char in note["title"])
            and isinstance(note["body"], str) and 1 <= len(note["body"].strip()) <= 5000
            and not any(ord(char) < 32 and char not in "\n\t" for char in note["body"]))


class ContextMemory:
    def __init__(self, home: Path):
        self.home = _private(home)
        secure_home(self.home)
        self.directory = ensure_private_dir(self.home / "indexes" / "applicant_context", [self.home])
        self.notes_path = resolve_within(self.home / "context_notes.json", [self.home])
        self._client = None
        self._closed = False

    def __enter__(self) -> ContextMemory:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        if self._client is not None:
            client, self._client = self._client, None
            try:
                client.close()
            except Exception:
                raise ContextMemoryError("private context index could not be closed") from None

    def _require_open(self) -> None:
        if self._closed:
            raise ContextMemoryError("private context index is closed")

    def _open_index(self) -> None:
        self._require_open()
        if self._client is None:
            try:
                self._client = QdrantClient(path=str(self.directory), check_compatibility=False)
            except Exception:
                raise ContextMemoryError("private context index could not be opened") from None

    def list_notes(self) -> list[ContextNote]:
        self._require_open()
        if not self.notes_path.exists():
            return []
        try:
            if self.notes_path.stat().st_size > 1_100_000:
                raise ValueError
            raw = json.loads(read_text(self.notes_path, [self.home]))
            if not isinstance(raw, list) or len(raw) > MAX_NOTES or not all(_valid_note(n) for n in raw):
                raise ValueError
            notes = [ContextNote(**note) for note in raw]
            if len({note.id for note in notes}) != len(notes):
                raise ValueError
            return notes
        except Exception:
            raise ContextMemoryError("private context notes are invalid") from None

    def _write(self, notes: list[ContextNote]) -> None:
        serialized = json.dumps([note.__dict__ for note in notes], ensure_ascii=False)
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".context_notes_", dir=self.home)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(serialized)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.notes_path)
        except Exception:
            raise ContextMemoryError("private context notes could not be saved") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def add(self, title: str, body: str) -> ContextNote:
        self._require_open()
        title, body = title.strip(), body.strip()
        candidate = {"id": uuid.uuid4().hex, "title": title, "body": body}
        if not _valid_note(candidate):
            raise ContextMemoryError("enter a short title and up to 5,000 characters of verified context")
        notes = self.list_notes()
        for existing in notes:
            if existing.title == title and existing.body == body:
                return existing
        if len(notes) >= MAX_NOTES:
            raise ContextMemoryError("private context note limit reached")
        note = ContextNote(**candidate)
        self._write([*notes, note])
        try:
            self.sync()
        except ContextMemoryError:
            pass  # The source note is durable; the next search rebuilds the index.
        return note

    def delete(self, identifier: str) -> None:
        self._require_open()
        if NOTE_ID.fullmatch(identifier) is None:
            raise ContextMemoryError("invalid context note ID")
        notes = self.list_notes()
        kept = [note for note in notes if note.id != identifier]
        if len(kept) == len(notes):
            return  # Safe to repeat if a previous response was lost.
        self._write(kept)
        try:
            self.sync()
        except ContextMemoryError:
            pass  # Search prunes this stale vector when the index is available.

    def sync(self) -> tuple[int, int]:
        self._open_index()
        notes = self.list_notes()
        try:
            existing = {}
            if self._client.collection_exists(COLLECTION):
                offset = None
                while True:
                    points, offset = self._client.scroll(COLLECTION, limit=256, offset=offset,
                                                         with_payload=True, with_vectors=False)
                    for point in points:
                        payload = point.payload or {}
                        identifier, digest = payload.get("id"), payload.get("digest")
                        if (set(payload) != {"id", "digest"} or not isinstance(identifier, str)
                                or NOTE_ID.fullmatch(identifier) is None or not isinstance(digest, str)
                                or DIGEST.fullmatch(digest) is None
                                or str(point.id) != str(uuid.UUID(identifier))):
                            raise ContextMemoryError("private context index contains unexpected metadata")
                        existing[identifier] = digest
                    if offset is None:
                        break
            current = {n.id: hashlib.sha256((n.title + "\n" + n.body).encode()).hexdigest()
                       for n in notes}
            changed = [n for n in notes if existing.get(n.id) != current[n.id]]
            if changed:
                if not self._client.collection_exists(COLLECTION):
                    self._client.create_collection(COLLECTION, vectors_config=models.VectorParams(
                        size=DIMENSIONS, distance=models.Distance.COSINE))
                else:
                    config = self._client.get_collection(COLLECTION).config.params.vectors
                    if not isinstance(config, models.VectorParams) or config.size != DIMENSIONS:
                        raise ContextMemoryError("private context index has incompatible vectors")
                vectors = FeatureHasher().embed([n.title + "\n" + n.body for n in changed], "document")
                self._client.upsert(COLLECTION, points=[models.PointStruct(
                    id=str(uuid.UUID(n.id)), vector=vector,
                    payload={"id": n.id, "digest": current[n.id]})
                    for n, vector in zip(changed, vectors)], wait=True)
            stale = set(existing) - set(current)
            if stale:
                self._client.delete(COLLECTION, points_selector=models.PointIdsList(
                    points=[str(uuid.UUID(identifier)) for identifier in sorted(stale)]), wait=True)
            return len(changed), len(stale)
        except ContextMemoryError:
            raise
        except Exception:
            raise ContextMemoryError("private context index could not be updated") from None

    def search(self, query: str, *, limit: int = 5) -> list[ContextSuggestion]:
        self._require_open()
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 5000:
            raise ContextMemoryError("enter up to 5,000 characters to search saved context")
        if not 1 <= limit <= 20:
            raise ContextMemoryError("context search limit must be between 1 and 20")
        self.sync()
        notes = {note.id: note for note in self.list_notes()}
        if not notes:
            return []
        try:
            vector = FeatureHasher().embed([query], "query")[0]
            if not any(math.isfinite(value) and value != 0 for value in vector):
                return []
            points = self._client.query_points(COLLECTION, query=vector, limit=limit,
                                               with_payload=True, with_vectors=False).points
            return [ContextSuggestion(notes[p.payload["id"]], float(p.score)) for p in points
                    if p.score > 0 and p.payload["id"] in notes]
        except Exception:
            raise ContextMemoryError("private context search failed") from None
