import io
from pathlib import Path
from types import SimpleNamespace
import sys
import zipfile

import pytest

from jobagent import resume_catalog as catalog


@pytest.fixture
def private(tmp_path, monkeypatch):
    # Model a private document folder beside a public repo, without user data.
    public = tmp_path / "public-repo"
    public.mkdir()
    monkeypatch.setattr(catalog, "REPO_ROOT", public)
    directory = tmp_path / "private-resumes"
    directory.mkdir()
    return directory, tmp_path / "private-upload", public


def archive(path, member, xml):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(member, xml)


def test_extracts_real_content_from_text_docx_and_odt(private):
    directory, _, _ = private
    (directory / "resume.txt").write_text("Python backend services", encoding="utf-8")
    archive(directory / "other.docx", "word/document.xml",
            '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>SQL pipelines</w:t></w:r>'
            '<w:r><w:t>and databases</w:t></w:r></w:p></w:body></w:document>')
    archive(directory / "data.odt", "content.xml",
            '<office:document xmlns:office="urn:office" xmlns:text="urn:text">'
            '<text:h>Data Engineer</text:h><text:p>Python <text:span>SQL</text:span> systems</text:p>'
            '</office:document>')
    (directory / "ignored.exe").write_bytes(b"not a resume")
    indexed = catalog.index_directory(directory)
    assert len(indexed.documents) == 3
    assert all(d.status == "ready" and d.digest for d in indexed.documents)
    by_suffix = {d.path.suffix: d.text for d in indexed.documents}
    assert by_suffix[".txt"] == "Python backend services"
    assert by_suffix[".docx"] == "SQL pipelines and databases"
    assert by_suffix[".odt"] == "Data Engineer\nPython SQL systems"
    for document in indexed.documents:
        assert document.text not in repr(document)
        assert str(directory) not in repr(document)


def test_content_ranking_ignores_misleading_filename_and_explains_overlap(private):
    directory, _, _ = private
    (directory / "python_data_engineer.txt").write_text("Nursing patient care hospital medication", encoding="utf-8")
    (directory / "general.txt").write_text("Data engineer Python SQL pipelines analytics", encoding="utf-8")
    (directory / "web.txt").write_text("JavaScript React HTML web frontend", encoding="utf-8")
    indexed = catalog.index_directory(directory)
    results = catalog.rank_resumes(indexed, "Data engineer Python SQL pipelines")
    assert results[0].document.path.name == "general.txt"
    assert results[0].method == "local_tfidf" and results[0].score > 0
    assert results[0].matched_terms == ("data", "engineer", "pipelines", "python", "sql")
    assert all(r.document.path.name != "python_data_engineer.txt" for r in results)
    assert catalog.rank_resumes(indexed, "unrelated geology") == []
    assert catalog.rank_resumes(indexed, "the and of") == []


def test_optional_embedding_ranking_uses_document_content_only(private):
    directory, _, _ = private
    (directory / "one.txt").write_text("Python SQL data", encoding="utf-8")
    (directory / "two.txt").write_text("React TypeScript browser", encoding="utf-8")
    calls = []
    class Embedder:
        def embed(self, texts, kind):
            calls.append((texts, kind))
            return [[1.0, 0.0] if "Python" in t else [0.0, 1.0] for t in texts]
    results = catalog.rank_resumes(catalog.index_directory(directory), "Python engineer", embedder=Embedder())
    assert results[0].document.path.name == "one.txt" and results[0].method == "embedding"
    assert results[0].score == 1.0 and "python" in results[0].matched_terms
    assert calls == [(["Python SQL data", "React TypeScript browser"], "document"), (["Python engineer"], "query")]
    class BadEmbedder:
        def embed(self, texts, kind): raise RuntimeError("private source text")
    with pytest.raises(catalog.CatalogError) as error:
        catalog.rank_resumes(catalog.index_directory(directory), "Python", embedder=BadEmbedder())
    assert "private source text" not in str(error.value)


def test_missing_pdf_extractor_is_explicit_and_not_filename_ranked(private, monkeypatch):
    directory, _, _ = private
    (directory / "python_sql_resume.pdf").write_bytes(b"%PDF-fictional")
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(catalog.shutil, "which", lambda name: None)
    indexed = catalog.index_directory(directory)
    document = indexed.documents[0]
    assert document.status == "unavailable" and "pypdf or pdftotext" in document.detail
    assert document.text == "" and catalog.rank_resumes(indexed, "Python SQL") == []


@pytest.mark.parametrize("encrypted,text,status", [(False, "Python SQL", "ready"),
                                                  (True, "private", "unavailable"), (False, "", "empty")])
def test_optional_pdf_reader_status(private, monkeypatch, encrypted, text, status):
    directory, _, _ = private
    (directory / "fake.pdf").write_bytes(b"%PDF-fictional")
    def reader(stream, strict):
        assert isinstance(stream, io.BytesIO) and strict is True
        return SimpleNamespace(is_encrypted=encrypted, pages=[SimpleNamespace(extract_text=lambda: text)])
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=reader))
    document = catalog.index_directory(directory).documents[0]
    assert document.status == status
    if status != "ready": assert document.text == ""


def test_pdftotext_fallback_has_fixed_args_and_private_output(private, monkeypatch):
    directory, _, _ = private
    path = directory / "fake.pdf"
    path.write_bytes(b"%PDF-fictional")
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(catalog.shutil, "which", lambda name: "fake-pdftotext")
    def run(arguments, **kwargs):
        assert arguments == ["fake-pdftotext", "-enc", "UTF-8", "-nopgbrk", str(path), "-"]
        assert kwargs["timeout"] == 20 and kwargs["check"] is True
        kwargs["stdout"].write(b"Python SQL systems")
    monkeypatch.setattr(catalog.subprocess, "run", run)
    document = catalog.index_directory(directory).documents[0]
    assert document.status == "ready" and document.text == "Python SQL systems"


def test_bad_or_unsafe_documents_have_fixed_status(private, monkeypatch):
    directory, _, _ = private
    (directory / "invalid.docx").write_bytes(b"private invalid archive")
    (directory / "invalid.txt").write_bytes(b"private\x00binary")
    (directory / "empty.txt").write_text("   ", encoding="utf-8")
    archive(directory / "unsafe.odt", "content.xml", '<!DOCTYPE doc [<!ENTITY x "private">]><doc>&x;</doc>')
    statuses = {d.path.name: d.status for d in catalog.index_directory(directory).documents}
    assert statuses == {"invalid.docx": "invalid", "invalid.txt": "invalid", "empty.txt": "empty", "unsafe.odt": "invalid"}
    monkeypatch.setattr(catalog, "MAX_BYTES", 2)
    assert all(d.status == "too_large" for d in catalog.index_directory(directory).documents)


def test_company_copy_preserves_original_and_is_retry_safe(private):
    directory, output, _ = private
    source = directory / "original.txt"
    raw = b"Python engineer SQL pipelines"
    source.write_bytes(raw)
    document = catalog.index_directory(directory).documents[0]
    target = catalog.copy_for_company(document, "../Fake Company / Inc", output)
    assert target.parent == output and target.name.startswith("fake-company-inc_resume_")
    assert target.suffix == ".txt" and target.read_bytes() == raw and source.read_bytes() == raw
    assert catalog.copy_for_company(document, "../Fake Company / Inc", output) == target
    target.write_bytes(b"existing conflicting bytes")
    with pytest.raises(catalog.CatalogError, match="conflicts"):
        catalog.copy_for_company(document, "../Fake Company / Inc", output)
    assert target.read_bytes() == b"existing conflicting bytes" and source.read_bytes() == raw
    source.write_bytes(b"changed original")
    with pytest.raises(catalog.CatalogError, match="changed since indexing"):
        catalog.copy_for_company(document, "Other Company", output)


def test_repo_paths_and_unextracted_copies_are_rejected(private):
    directory, _, public = private
    (directory / "resume.txt").write_text("Python engineer", encoding="utf-8")
    document = catalog.index_directory(directory).documents[0]
    with pytest.raises(catalog.CatalogError, match="outside the public"):
        catalog.index_directory(public)
    with pytest.raises(catalog.CatalogError, match="outside the public"):
        catalog.copy_for_company(document, "Fake", public)
    unavailable = catalog.ResumeDocument("id", document.path, "unavailable")
    with pytest.raises(catalog.CatalogError, match="successfully extracted"):
        catalog.copy_for_company(unavailable, "Fake", directory)


def test_recursion_limit_and_invalid_requests(private):
    directory, _, _ = private
    (directory / "first.txt").write_text("Python", encoding="utf-8")
    nested = directory / "nested"
    nested.mkdir()
    (nested / "second.txt").write_text("SQL", encoding="utf-8")
    assert len(catalog.index_directory(directory).documents) == 1
    assert len(catalog.index_directory(directory, recursive=True).documents) == 2
    assert catalog.index_directory(directory, recursive=True, max_files=1).truncated
    with pytest.raises(catalog.CatalogError): catalog.index_directory(directory, max_files=0)
    with pytest.raises(catalog.CatalogError): catalog.rank_resumes(catalog.index_directory(directory), "")
