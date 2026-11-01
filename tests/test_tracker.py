import re

import pytest

from jobagent.applications import ApplicationStore
from jobagent import tracker
from jobagent.tracker import GoogleSheetsTransport, HEADERS, SheetsTracker, TrackerError, spreadsheet_id


class FakeSheet:
    def __init__(self):
        self.rows = []
        self.fail_after_write = False
        self.unavailable = False

    def read(self):
        if self.unavailable:
            raise RuntimeError("private API response body")
        return [list(row) for row in self.rows]

    def write(self, row, values):
        while len(self.rows) < row:
            self.rows.append([])
        self.rows[row - 1] = list(values)
        if row > 1 and self.fail_after_write:
            self.fail_after_write = False
            raise TimeoutError("private API response body")


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home"
    pdf = home / "output" / "resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"fake resume")
    store = ApplicationStore(home)
    record = store.save_resume(source_url="https://example.com/1", source_text=None,
                              company="=HYPERLINK(\"example\")", title="Engineer", pdf=pdf)
    sheet = FakeSheet()
    return store, record, pdf, sheet, SheetsTracker(store, sheet, "target")


def test_retry_after_lost_response_has_one_row(setup):
    store, record, pdf, sheet, tracker = setup
    sheet.fail_after_write = True
    with pytest.raises(TrackerError) as exc:
        tracker.sync(record.id)
    assert "private API response" not in str(exc.value)
    assert store.pending("target") == [record.id]
    assert pdf.read_bytes() == b"fake resume"
    # Simulate a process restart after the remote write committed.
    tracker = SheetsTracker(ApplicationStore(store.home), sheet, "target")
    tracker.sync(record.id)
    tracker.sync(record.id)
    assert len(sheet.rows) == 2
    assert sheet.rows[1][0] == record.id
    assert sheet.rows[1][1] == record.company
    assert store.pending("target") == []
    store.set_status(record.id, "review_needed")
    assert store.pending("target") == [record.id]
    tracker.sync(record.id)
    assert sheet.rows[1][5] == "review_needed"
    assert len(sheet.rows) == 2
    assert store.pending("another-target") == [record.id]


def test_network_failure_preserves_local_record(setup):
    store, record, pdf, sheet, tracker = setup
    sheet.unavailable = True
    with pytest.raises(TrackerError):
        tracker.sync(record.id)
    assert store.get(record.id) == record
    assert pdf.exists()
    assert store.pending("target") == [record.id]


def test_existing_remote_rows_and_schema_validation(setup):
    store, record, _, sheet, tracker = setup
    sheet.rows = [HEADERS, ["another-id"], [record.id]]
    tracker.sync(record.id)
    assert len(sheet.rows) == 3 and sheet.rows[1] == ["another-id"]
    sheet.rows.append([record.id])
    with pytest.raises(TrackerError, match="duplicate"):
        tracker.sync(record.id)
    sheet.rows = [["unexpected header"]]
    with pytest.raises(TrackerError, match="unexpected columns"):
        tracker.sync(record.id)


def test_transport_writes_literal_values():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"values": [HEADERS]}

    class Session:
        def get(self, url, **kwargs): return Response()
        def put(self, url, **kwargs):
            self.url, self.kwargs = url, kwargs
            return Response()

    session = Session()
    transport = GoogleSheetsTransport("fake-sheet", session)
    assert transport.read() == [HEADERS]
    transport.write(2, ["=1+1", "+formula", "@formula"])
    assert session.kwargs["params"] == {"valueInputOption": "RAW"}
    assert session.kwargs["json"]["values"] == [["=1+1", "+formula", "@formula"]]
    assert "A2%3AG2" in session.url


def test_spreadsheet_selector_accepts_only_id_or_canonical_editor_url():
    identifier = "aB_0123456789-cD_0123456789"
    assert spreadsheet_id(identifier) == identifier
    assert spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{identifier}/edit#gid=0") == identifier
    for invalid in ("", "short", "https://evil.example/spreadsheets/d/" + identifier,
                    "http://docs.google.com/spreadsheets/d/" + identifier,
                    "https://docs.google.com@evil.example/spreadsheets/d/" + identifier):
        with pytest.raises(TrackerError, match="spreadsheet ID"):
            spreadsheet_id(invalid)


def test_create_spreadsheet_has_applications_and_headers_in_one_api_call(monkeypatch):
    identifier = "S" * 30

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"spreadsheetId": identifier}

    class Session:
        def __init__(self): self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    monkeypatch.setattr(tracker, "_authorized_session", lambda settings: session)
    assert tracker.create_spreadsheet(object(), title="Fake job tracker") == identifier
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == "https://sheets.googleapis.com/v4/spreadsheets"
    assert options["params"] == {"fields": "spreadsheetId"}
    assert options["timeout"] == 20
    body = options["json"]
    assert body["properties"] == {"title": "Fake job tracker"}
    assert body["sheets"][0]["properties"] == {"title": "Applications"}
    values = body["sheets"][0]["data"][0]["rowData"][0]["values"]
    assert [value["userEnteredValue"]["stringValue"] for value in values] == HEADERS
    with pytest.raises(TrackerError, match="title"):
        tracker.create_spreadsheet(object(), title=" ")


def test_ensure_applications_preserves_other_tabs_and_upserts_safely(setup, monkeypatch):
    store, record, pdf, _, _ = setup
    identifier = "S" * 30

    class Response:
        def __init__(self, payload=None, fail=False):
            self.payload, self.fail = payload or {}, fail
        def raise_for_status(self):
            if self.fail:
                raise TimeoutError("private API response body")
        def json(self): return self.payload

    class Session:
        def __init__(self):
            self.tabs = {"Other": [["keep", "this"], ["existing", "data"]]}
            self.posts = []
            self.puts = []
            self.lose_add_response = True
            self.lose_data_response = True

        def get(self, url, **kwargs):
            if "/values/" in url:
                assert "%27Applications%27%21A%3AG" in url
                return Response({"values": [list(row) for row in self.tabs["Applications"]]})
            assert url == "https://sheets.googleapis.com/v4/spreadsheets/" + identifier
            assert kwargs["params"] == {"fields": "sheets(properties(title))"}
            return Response({"sheets": [{"properties": {"title": name}}
                                        for name in self.tabs]})

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            assert url.endswith(":batchUpdate")
            assert kwargs["json"] == {"requests": [
                {"addSheet": {"properties": {"title": "Applications"}}}]}
            self.tabs["Applications"] = []
            fail, self.lose_add_response = self.lose_add_response, False
            return Response(fail=fail)

        def put(self, url, **kwargs):
            self.puts.append((url, kwargs))
            assert kwargs["params"] == {"valueInputOption": "RAW"}
            row = int(re.search(r"A(\d+)%3AG", url).group(1))
            rows = self.tabs["Applications"]
            while len(rows) < row:
                rows.append([])
            rows[row - 1] = list(kwargs["json"]["values"][0])
            fail = row > 1 and self.lose_data_response
            if fail:
                self.lose_data_response = False
            return Response(fail=fail)

    session = Session()
    monkeypatch.setattr(tracker, "_authorized_session", lambda settings: session)
    assert tracker.ensure_applications_worksheet(object(), identifier) is True
    assert session.tabs["Other"] == [["keep", "this"], ["existing", "data"]]
    assert session.tabs["Applications"] == [HEADERS]
    assert len(session.posts) == 1 and len(session.puts) == 1
    assert tracker.ensure_applications_worksheet(object(), identifier) is False
    assert len(session.posts) == 1 and len(session.puts) == 1

    remote = GoogleSheetsTransport(identifier, session)
    synced = SheetsTracker(store, remote, "target")
    with pytest.raises(TrackerError):
        synced.sync(record.id)
    assert store.pending("target") == [record.id] and pdf.exists()
    synced.sync(record.id)
    assert len(session.tabs["Applications"]) == 2
    assert session.tabs["Applications"][1][0] == record.id
    assert store.pending("target") == []
    assert session.tabs["Other"] == [["keep", "this"], ["existing", "data"]]

    session.tabs["Applications"][0] = ["unexpected header"]
    with pytest.raises(TrackerError, match="unexpected columns"):
        tracker.ensure_applications_worksheet(object(), identifier)
    assert session.tabs["Applications"][0] == ["unexpected header"]
