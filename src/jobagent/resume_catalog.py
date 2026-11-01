"""Private local resume indexing, explainable ranking, and unchanged upload copies.

TXT, DOCX and ODT extraction use the standard library. PDF extraction needs
optional pypdf or pdftotext; unavailable/scanned/encrypted files are explicit
statuses, never ranked by filename. No services are contacted by default.
An explicitly supplied bank.Embedder can provide semantic cosine ranking;
that sends extracted text to the caller's configured embedding service.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Protocol
import xml.etree.ElementTree as ET
import zipfile

from jobagent import safepaths

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED = {".pdf", ".docx", ".odt", ".txt"}
MAX_BYTES = 20_000_000
MAX_XML_BYTES = 5_000_000
MAX_TEXT_CHARS = 200_000
STOP_WORDS = set("a an and are as at be by for from in is it of on or our that the their this to with you your".split())


class CatalogError(ValueError):
    """Fixed messages only: extractor exceptions can include private content."""


@dataclass(frozen=True, repr=False)
class ResumeDocument:
    id: str
    path: Path
    status: str
    text: str = ""
    digest: str | None = None
    detail: str = ""

    def __repr__(self):
        return f"ResumeDocument(status={self.status!r}, <private>)"


@dataclass(frozen=True, repr=False)
class ResumeCatalog:
    root: Path
    documents: tuple[ResumeDocument, ...]
    truncated: bool = False

    def __repr__(self):
        return f"ResumeCatalog(documents={len(self.documents)}, truncated={self.truncated})"


@dataclass(frozen=True, repr=False)
class ResumeMatch:
    document: ResumeDocument
    score: float
    matched_terms: tuple[str, ...]
    method: str

    def __repr__(self):
        return f"ResumeMatch(score={self.score:.3f}, method={self.method!r}, <private>)"


class EmbedderLike(Protocol):
    def embed(self, texts: list[str], kind: str) -> list[list[float]]: ...


def _private_path(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path == REPO_ROOT.resolve() or path.is_relative_to(REPO_ROOT.resolve()):
        raise CatalogError("resume documents and upload copies must stay outside the public repository")
    return path


def _zip_text(raw: bytes, suffix: str) -> str:
    member = "word/document.xml" if suffix == ".docx" else "content.xml"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(member)
        if info.file_size > MAX_XML_BYTES or info.flag_bits & 1:
            raise ValueError("document XML is oversized or encrypted")
        with archive.open(info) as stream:
            xml = stream.read(MAX_XML_BYTES + 1)
        if len(xml) > MAX_XML_BYTES or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError("unsafe document XML")
        root = ET.fromstring(xml)
        if suffix == ".docx":
            paragraphs = [" ".join(n.text or "" for n in p.iter() if n.tag.endswith("}t"))
                          for p in root.iter() if p.tag.endswith("}p")]
        else:
            paragraphs = ["".join(p.itertext()) for p in root.iter()
                          if p.tag.endswith("}p") or p.tag.endswith("}h")]
        return "\n".join(paragraphs)


def _pdf_text(raw: bytes, path: Path) -> tuple[str, str, str]:
    if importlib.util.find_spec("pypdf") is not None:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            return "unavailable", "", "encrypted PDF requires an unlocked copy"
        if len(reader.pages) > 100:
            return "too_large", "", "PDF exceeds 100 pages"
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > MAX_TEXT_CHARS:
                return "too_large", "", "extracted text exceeds the catalog limit"
        return "ready", "\n".join(parts), "pypdf"
    executable = shutil.which("pdftotext")
    if executable is None:
        return "unavailable", "", "PDF text needs optional pypdf or pdftotext; no filename fallback"
    # Fixed arguments and no shell. Temporary extracted text stays in the OS's
    # private temporary file, never in the repository or adjacent to originals.
    with tempfile.TemporaryFile() as output:
        subprocess.run([executable, "-enc", "UTF-8", "-nopgbrk", str(path), "-"],
                       stdout=output, stderr=subprocess.DEVNULL, timeout=20, check=True)
        if output.tell() > MAX_TEXT_CHARS * 4:
            return "too_large", "", "extracted text exceeds the catalog limit"
        output.seek(0)
        return "ready", output.read(MAX_TEXT_CHARS * 4 + 1).decode("utf-8"), "pdftotext"


def _extract(path: Path) -> ResumeDocument:
    identifier = "resume-" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
    digest = None
    try:
        if path.stat().st_size > MAX_BYTES:
            return ResumeDocument(identifier, path, "too_large", detail="document exceeds 20 MB")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return ResumeDocument(identifier, path, "too_large", detail="document exceeds 20 MB")
        digest = hashlib.sha256(raw).hexdigest()
        if path.suffix.lower() == ".txt":
            text, detail = raw.decode("utf-8-sig"), "UTF-8 text"
        elif path.suffix.lower() in (".docx", ".odt"):
            text, detail = _zip_text(raw, path.suffix.lower()), "document XML"
        else:
            status, text, detail = _pdf_text(raw, path)
            if status != "ready":
                return ResumeDocument(identifier, path, status, digest=digest, detail=detail)
        text = text.strip()
        if len(text) > MAX_TEXT_CHARS:
            return ResumeDocument(identifier, path, "too_large", digest=digest, detail="extracted text exceeds the catalog limit")
        if "\x00" in text:
            raise ValueError("binary text")
        status = "ready" if _tokens(text) else "empty"
        return ResumeDocument(identifier, path, status, text, digest,
                              detail if status == "ready" else "no usable text; scanned PDFs need OCR")
    except Exception:
        return ResumeDocument(identifier, path, "invalid", digest=digest,
                              detail="document could not be extracted; no filename fallback")


def index_directory(directory: Path, *, recursive: bool = False, max_files: int = 200) -> ResumeCatalog:
    root = _private_path(directory)
    if not root.is_dir():
        raise CatalogError("resume directory is unavailable")
    if not 1 <= max_files <= 1000:
        raise CatalogError("catalog limit must be between 1 and 1000 files")
    documents, truncated = [], False
    try:
        candidates = root.rglob("*") if recursive else root.iterdir()
        for candidate in sorted(candidates):
            if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED:
                continue
            if len(documents) == max_files:
                truncated = True
                break
            path = safepaths.resolve_within(candidate, [root])
            _private_path(path)
            documents.append(_extract(path))
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("resume directory could not be indexed") from None
    return ResumeCatalog(root, tuple(documents), truncated)


def _tokens(text: str) -> Counter:
    return Counter(term for term in re.findall(r"[a-z][a-z0-9+#]*(?:\.[a-z][a-z0-9]*)*", text.lower())
                   if term not in STOP_WORDS)


def _cosine(left, right) -> float:
    if len(left) != len(right) or not left or not all(math.isfinite(v) for v in [*left, *right]):
        raise CatalogError("embedding service returned invalid vectors")
    denominator = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


def rank_resumes(catalog: ResumeCatalog, job_text: str, *, limit: int = 5,
                 embedder: EmbedderLike | None = None) -> list[ResumeMatch]:
    """Rank content only. Lexical overlap explains TF-IDF; embedding use is opt-in."""
    if not isinstance(job_text, str) or not job_text.strip() or len(job_text) > MAX_TEXT_CHARS:
        raise CatalogError("provide bounded job role or description text")
    if limit < 1:
        raise CatalogError("ranking limit must be positive")
    documents = [d for d in catalog.documents if d.status == "ready"]
    query = _tokens(job_text)
    if not documents or not query:
        return []
    tokens = [_tokens(d.text) for d in documents]
    matched = [tuple(sorted(query.keys() & terms.keys())) for terms in tokens]
    if embedder is not None:
        try:
            # The existing Ollama Embedder accepts document/query task prefixes.
            vectors = embedder.embed([d.text for d in documents], "document")
            query_vectors = embedder.embed([job_text], "query")
            if len(vectors) != len(documents) or len(query_vectors) != 1:
                raise ValueError
            scores = [_cosine(query_vectors[0], vector) for vector in vectors]
        except Exception:
            raise CatalogError("semantic ranking unavailable; choose local content ranking") from None
        method = "embedding"
    else:
        frequency = Counter(term for terms in tokens for term in terms)
        vocabulary = sorted(set(query) | set(frequency))
        idf = {term: math.log((len(documents) + 1) / (frequency[term] + 1)) + 1 for term in vocabulary}
        def vector(terms):
            return [(1 + math.log(terms[term])) * idf[term] if terms[term] else 0.0 for term in vocabulary]
        query_vector = vector(query)
        scores = [_cosine(query_vector, vector(terms)) for terms in tokens]
        method = "local_tfidf"
    results = [ResumeMatch(d, score, terms, method)
               for d, score, terms in zip(documents, scores, matched) if score > 0]
    return sorted(results, key=lambda r: (-r.score, r.document.id))[:limit]


def copy_for_company(document: ResumeDocument, company: str, destination_dir: Path) -> Path:
    """Copy original bytes under a company name; changed originals fail closed."""
    if document.status != "ready" or not document.digest:
        raise CatalogError("select a resume with successfully extracted content before copying")
    source = _private_path(document.path)
    directory = _private_path(destination_dir)
    try:
        source = safepaths.resolve_within(source, [document.path.parent])
        with source.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != document.digest:
            raise CatalogError("resume changed since indexing; reindex before copying")
        safepaths.ensure_private_dir(directory, [directory])
        name = f"{safepaths.slug(company)}_resume_{document.digest[:12]}{source.suffix.lower()}"
        target = safepaths.resolve_within(directory / name, [directory])
        _private_path(target)
        if target == source:
            raise CatalogError("upload copy must differ from the original document")
        if target.exists():
            if target.read_bytes() == raw:
                return target
            raise CatalogError("upload-copy destination conflicts with an existing file")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("could not create a private company-named resume copy") from None
