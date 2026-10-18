import pytest

from jobagent.applications import ApplicationStore, application_id
from jobagent.safepaths import PathOutsideWorkingDirs


@pytest.fixture
def saved(tmp_path):
    home = tmp_path / "home"
    pdf = home / "output" / "resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"fake resume")
    store = ApplicationStore(home)
    record = store.save_resume(source_url="https://example.com/job/123", source_text=None,
                              company="Example", title="Engineer", pdf=pdf)
    return store, record, pdf


def test_identity_is_stable_and_distinguishes_postings():
    assert application_id("https://example.com/1", None) == application_id("https://example.com/1", "changed")
    assert application_id("https://example.com/1", None) != application_id("https://example.com/2", None)
    assert application_id(None, "posting") != application_id(None, "another posting")
    with pytest.raises(ValueError):
        application_id(None, "  ")


def test_saved_url_excludes_authentication_material(saved):
    store, _, pdf = saved
    source = "https://fake-user:fake-password@example.com/jobs/123?token=fake-token#fake-fragment"
    record = store.save_resume(source_url=source, source_text=None,
                              company="Example", title="Engineer", pdf=pdf)
    assert record.posting_url == "https://example.com/jobs/123"
    assert record.id == application_id(source, None)


def test_persists_and_regeneration_keeps_human_status(saved):
    store, record, pdf = saved
    assert record.status == "resume_ready"
    with pytest.raises(ValueError, match="explicit human"):
        store.set_status(record.id, "submitted")
    store.set_status(record.id, "submitted", confirmed=True)
    reopened = ApplicationStore(store.home)
    regenerated = reopened.save_resume(source_url=record.posting_url, source_text=None,
                                      company="Example", title="Engineer", pdf=pdf)
    assert regenerated.id == record.id and regenerated.status == "submitted"
    assert reopened.pending("target") == [record.id]
    assert regenerated.resume_path == "output/resume.pdf"
    assert "Example" not in repr(regenerated)


def test_rejects_unknown_and_invalid_status(saved):
    store, record, _ = saved
    with pytest.raises(ValueError, match="not found"):
        store.set_status("missing", "blocked")
    with pytest.raises(ValueError, match="unsupported"):
        store.set_status(record.id, "invented")
    for status in ("blocked", "review_needed", "resume_ready"):
        assert store.set_status(record.id, status).status == status


def test_pdf_must_be_existing_and_inside_output(saved, tmp_path):
    store, _, pdf = saved
    for bad, error in ((tmp_path / "resume.pdf", PathOutsideWorkingDirs),
                       (pdf.parent / "missing.pdf", ValueError)):
        with pytest.raises(error):
            store.save_resume(source_url="https://example.com/other", source_text=None,
                              company="Example", title="Engineer", pdf=bad)
