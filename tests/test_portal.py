"""Local portal behavior with fictional applicant data and no external services."""

from __future__ import annotations

import hashlib
import io
import tempfile
import threading
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from jobagent.config import Settings
from jobagent.portal import PortalError, PortalState, _page
from jobagent.resume_catalog import ResumeCatalog, ResumeDocument


def test_prepare_copies_ranked_pdf_and_records_application(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        folder = root / "resumes"
        folder.mkdir()
        original = folder / "generic.pdf"
        original.write_bytes(b"fictional-pdf-bytes")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        document_id = "resume-" + hashlib.sha256(str(original.resolve()).encode()).hexdigest()[:24]
        document = ResumeDocument(document_id, original, "ready",
                                  "Python automation and data analysis", digest)
        monkeypatch.setattr("jobagent.resume_catalog.index_directory",
                            lambda path: ResumeCatalog(path, (document,)))
        settings = Settings(_env_file=None, jobagent_home=root / "private")
        state = PortalState(settings)
        state.add_folder(str(folder))
        state.save_profile({"first_name": "Fake", "last_name": "Applicant",
                            "email": "fake@example.invalid",
                            "answers_text": "Available start date = June 2027"})

        identifier, method = state.prepare(company="Example Co", title="Python Intern",
                                            url="https://example.org/jobs/123",
                                            description="Python automation internship")
        record = state.store.get(identifier)
        prepared = settings.jobagent_home / record.resume_path

        assert method == "local Qdrant + local_tfidf"
        assert prepared.name.startswith("example-co_resume_")
        assert prepared.read_bytes() == original.read_bytes()
        assert record.status == "resume_ready"
        assert record.posting_url == "https://example.org/jobs/123"
        assert state.sync(identifier) is False  # no credentials, local record stays
        with pytest.raises(PortalError):
            state.prepare(company="Example", title="", url="", description="Python")


def test_folder_must_be_outside_public_repository():
    from jobagent.config import REPO_ROOT

    with pytest.raises(PortalError):
        from jobagent.portal import _safe_dir

        _safe_dir(str(REPO_ROOT))


def test_explicit_extra_answer_only_fills_matching_text_field():
    from jobagent.browser import plan_fields
    from jobagent.profile import ApplicantProfile

    profile = ApplicantProfile.model_validate({
        "first_name": "Fake", "last_name": "Applicant", "email": "fake@example.invalid",
        "answers": {"Available start date": "June 2027"},
    })
    fields = [
        {"id": "start", "label": "Available start date", "type": "text"},
        {"id": "consent", "label": "Available start date", "type": "checkbox"},
    ]
    actions, skipped = plan_fields(fields, profile, Path("resume.pdf"))
    assert actions == []  # duplicate labels cannot select a unique field
    assert skipped == 2
    actions, skipped = plan_fields(fields[:1], profile, Path("resume.pdf"))
    assert [(action.field_id, action.value) for action in actions] == [("start", "June 2027")]
    assert skipped == 0


def test_tracker_instructions_select_target_and_sync_only_pending(monkeypatch):
    from jobagent import tracker

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "private"
        pdf = home / "output" / "resume.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"fictional resume")
        from jobagent.private_storage import HOME_MARKER_NAME
        (home / HOME_MARKER_NAME).write_text("jobagent-private-home-v1\n")
        state = PortalState(Settings(_env_file=None, jobagent_home=home))
        record = state.store.save_resume(source_url="https://example.org/one", source_text=None,
                                         company="Example", title="Engineer", pdf=pdf)
        sheet_a = "A" * 30
        sheet_b = "B" * 30
        transports = {}

        class FakeTransport:
            def __init__(self):
                self.rows = []

            def read(self):
                return self.rows

            def write(self, row, values):
                while len(self.rows) < row:
                    self.rows.append([])
                self.rows[row - 1] = list(values)

        def fake_from_settings(settings, store, *, sheet_id):
            remote = transports.setdefault(sheet_id, FakeTransport())
            return tracker.SheetsTracker(store, remote, hashlib.sha256(sheet_id.encode()).hexdigest())

        monkeypatch.setattr(tracker, "from_settings", fake_from_settings)
        assert state.instruction(f"use spreadsheet https://docs.google.com/spreadsheets/d/{sheet_a}/edit")
        assert state.pending_count() == 1
        assert state.instruction("sync all") == "Synced 1 pending application(s); 0 still pending."
        assert state.pending_count() == 0
        assert transports[sheet_a].rows[1][0] == record.id
        state.instruction(f"use spreadsheet {sheet_b}")
        assert state.pending_count() == 1
        page = _page(state)
        assert "Choose spreadsheet" in page and "Sync pending applications" in page
        assert "Tracker instruction" in page and record.id in page
        assert "Job Agent dashboard" in page and 'class="stats"' in page
        assert 'href="#applications"' in page
        assert sheet_b not in page  # only a suffix is shown
        assert state.instruction("show pending") == "1 application(s) pending for the selected spreadsheet."
        assert state.instruction(f"sync {record.id}") == "Application synced."
        assert transports[sheet_b].rows[1][0] == record.id
        assert len(transports[sheet_a].rows) == 2
        assert PortalState(Settings(_env_file=None, jobagent_home=home)).selected_sheet() == sheet_b
        with pytest.raises(PortalError, match="Supported instructions"):
            state.instruction("mark every application submitted")
        assert state.store.get(record.id).status == "resume_ready"


def test_duplicate_extra_answer_labels_do_not_overwrite(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        state = PortalState(Settings(_env_file=None, jobagent_home=home))
        with pytest.raises(PortalError, match="unique"):
            state.save_profile({
                "first_name": "Fake", "last_name": "Applicant", "email": "fake@example.invalid",
                "answers_text": "Available start date = June 2027\n available START date: = July 2027",
            })
        assert not state.settings.profile_file.exists()


def test_portal_stops_if_private_storage_cannot_be_secured(monkeypatch):
    from jobagent import portal
    from jobagent.private_storage import PrivateStorageError

    def denied(_home, **_kwargs):
        raise PrivateStorageError("private Windows storage could not be secured")

    monkeypatch.setattr(portal, "secure_home", denied)
    with tempfile.TemporaryDirectory() as temporary:
        settings = Settings(_env_file=None, jobagent_home=Path(temporary) / "private")
        with pytest.raises(PrivateStorageError, match="private Windows storage"):
            portal.serve(settings)


def test_http_handler_rejects_foreign_host_origin_and_missing_token(monkeypatch):
    from jobagent import portal

    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        state = PortalState(Settings(_env_file=None, jobagent_home=home))
        holder = {}

        class FakeServer:
            server_port = 8765

            def __init__(self, address, handler):
                assert address == ("127.0.0.1", 0)
                holder["handler"] = handler

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def serve_forever(self):
                return None

        monkeypatch.setattr(portal, "PortalState", lambda settings: state)
        monkeypatch.setattr(portal, "ThreadingHTTPServer", FakeServer)
        opened = []
        monkeypatch.setattr(portal.webbrowser, "open", lambda url, new: opened.append((url, new)))
        portal.serve(state.settings, open_browser=True)
        assert opened == [(f"http://127.0.0.1:8765/?token={state.token}", 2)]
        handler_type = holder["handler"]

        def request(method, path, *, host="127.0.0.1:8765", origin=None, form=None):
            handler = handler_type.__new__(handler_type)
            handler.path = path
            handler.server = SimpleNamespace(server_port=8765)
            handler.headers = Message()
            handler.headers["Host"] = host
            if origin is not None:
                handler.headers["Origin"] = origin
            body = urlencode(form or {}).encode("utf-8")
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            response = {"headers": {}}
            handler.send_response = lambda status, *_args: response.update(status=status)
            handler.send_header = lambda key, value: response["headers"].update({key: value})
            handler.end_headers = lambda: None
            getattr(handler, "do_" + method)()
            response["body"] = handler.wfile.getvalue()
            return response

        page = request("GET", "/?token=" + state.token)
        assert page["status"] == 200 and b"Local application workspace" in page["body"]
        assert page["headers"]["X-Frame-Options"] == "DENY"
        assert request("GET", "/?token=" + state.token, host="attacker.example")["status"] == 403
        assert request("GET", "/?token=" + state.token,
                       origin="http://attacker.example")["status"] == 403
        assert request("GET", "/")["status"] == 403

        form = {"token": state.token, "sheet": "A" * 30}
        assert request("POST", "/sheet", form=form)["status"] == 403
        assert request("POST", "/sheet", form=form,
                       origin="http://attacker.example")["status"] == 403
        assert request("POST", "/sheet", form=form, origin="http://127.0.0.1:8765",
                       host="attacker.example")["status"] == 403
        assert not state.tracker_path.exists()
        allowed = request("POST", "/sheet", form=form, origin="http://127.0.0.1:8765")
        assert allowed["status"] == 303 and state.selected_sheet() == "A" * 30
        assert request("POST", "/sheet", form={"sheet": "B" * 30},
                       origin="http://127.0.0.1:8765")["status"] == 403
        assert state.selected_sheet() == "A" * 30


@pytest.mark.parametrize("approve", [False, True])
def test_review_requires_explicit_manual_handoff(monkeypatch, approve):
    from jobagent import browser

    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary) / "private"
        state = PortalState(Settings(_env_file=None, jobagent_home=home))
        state.save_profile({"first_name": "Fake", "last_name": "Applicant",
                            "email": "fake@example.invalid"})
        pdf = home / "output" / "resume.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"fictional pdf bytes")
        record = state.store.save_resume(source_url="https://example.org/jobs/123",
                                         source_text=None, company="Example", title="Engineer",
                                         pdf=pdf)
        entered, decided, ended = threading.Event(), threading.Event(), threading.Event()
        outcomes = []

        def fake_run_headed(*_args, wait_for_handoff, wait_for_review, **_kwargs):
            entered.set()
            try:
                outcomes.append(wait_for_handoff())
                decided.set()
                if outcomes[-1]:
                    wait_for_review()
            finally:
                ended.set()

        monkeypatch.setattr(browser, "run_headed", fake_run_headed)
        state.start_review(record.id)
        assert entered.wait(2)
        assert "Enable manual actions" in _page(state)
        if approve:
            state.enable_manual_actions()
            assert decided.wait(2) and outcomes == [True]
            state.close_review()
        else:
            state.close_review()
            assert decided.wait(2) and outcomes == [False]
            with pytest.raises(PortalError, match="Open a review browser"):
                state.enable_manual_actions()
        assert ended.wait(2)
