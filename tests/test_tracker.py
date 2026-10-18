import pytest

from jobagent.applications import ApplicationStore
from jobagent.tracker import GoogleSheetsTransport, HEADERS, SheetsTracker, TrackerError


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
