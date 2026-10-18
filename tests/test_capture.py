import argparse
import json
from types import SimpleNamespace

import pytest

from jobagent import capture, cli, graph
from jobagent.applications import ApplicationStore, application_id

URL = "https://example.invalid/jobs/123?token=fake-private-token#posting"
TEXT = "Fictional job description: build Python services and maintain SQL pipelines. " * 8


class FakeBrowser:
    def __init__(self, text=TEXT):
        self.text, self.events, self.closed = text, [], False
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def launch(self, directory, **kwargs):
        assert kwargs == {"headless": False, "accept_downloads": False}
        self.events.append("open")
        return self
    def new_page(self): return self
    def goto(self, url, **kwargs):
        assert url == URL
        self.url = url
        self.events.append("navigate")
    def evaluate(self, expression):
        assert expression == "window.getSelection().toString()"
        assert self.events[-1] == "confirm"
        self.events.append("selection_only")
        return self.text
    def close(self):
        self.closed = True
        self.events.append("close")


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "fake-home"
    home.mkdir()
    return SimpleNamespace(jobagent_home=home, browser_profile_dir=home / "browser_profile")


def test_explicit_selection_only_capture(settings):
    fake = FakeBrowser()
    def confirm():
        assert not fake.closed
        fake.events.append("confirm")
    identifier = capture.capture_posting(URL, settings, confirm=confirm, playwright_factory=lambda: fake)
    assert capture.CAPTURE_ID.fullmatch(identifier)
    assert fake.events == ["open", "navigate", "confirm", "selection_only", "close"]
    posting = capture.load_capture(settings.jobagent_home, identifier)
    assert posting.source_url == URL and posting.source_text == TEXT.strip()
    assert TEXT.strip() not in repr(posting) and "fake-private-token" not in repr(posting)
    path = capture.capture_path(settings.jobagent_home, identifier)
    assert path.parent == settings.jobagent_home / "captures"
    assert json.loads(path.read_text(encoding="utf-8"))["source_text"] == TEXT.strip()


def test_final_posting_url_after_manual_navigation_is_saved(settings):
    fake = FakeBrowser()
    final_url = "https://example.invalid/jobs/456?token=fake-final-token#description"
    def confirm():
        fake.url = final_url
        fake.events.append("confirm")
    identifier = capture.capture_posting(URL, settings, confirm=confirm, playwright_factory=lambda: fake)
    posting = capture.load_capture(settings.jobagent_home, identifier)
    assert posting.source_url == final_url
    pdf = settings.jobagent_home / "output" / "resume.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"fake PDF")
    record = ApplicationStore(settings.jobagent_home).save_resume(
        source_url=posting.source_url, source_text=posting.source_text, company="Fake", title="Engineer", pdf=pdf)
    assert record.posting_url == "https://example.invalid/jobs/456"


def test_invalid_final_url_does_not_read_or_save_selection(settings):
    fake = FakeBrowser()
    def confirm():
        fake.url = "http://example.invalid/private"
        fake.events.append("confirm")
    with pytest.raises(capture.CaptureError):
        capture.capture_posting(URL, settings, confirm=confirm, playwright_factory=lambda: fake)
    assert fake.closed and "selection_only" not in fake.events
    assert not (settings.jobagent_home / "captures").exists()


def test_newline_rich_maximum_selection_round_trips(settings):
    text = "A" + "\n\t\x01" * 9999 + "ZZ"
    assert len(text) == capture.MAX_TEXT
    identifier = capture.save_capture(settings.jobagent_home, URL, text)
    assert capture.load_capture(settings.jobagent_home, identifier).source_text == text


@pytest.mark.parametrize("text", ["", "x" * 199, "x" * 30001, None, "\x00" + "x" * 300])
def test_empty_invalid_or_oversized_selection_is_not_saved(settings, text):
    fake = FakeBrowser(text)
    with pytest.raises(capture.CaptureError):
        capture.capture_posting(URL, settings, confirm=lambda: fake.events.append("confirm"),
                                playwright_factory=lambda: fake)
    assert fake.closed
    assert not (settings.jobagent_home / "captures").exists()


@pytest.mark.parametrize("url", ["http://example.invalid/job", "file:///posting", "https:///job",
    "https://fake" + "@" + "example.invalid/job", "https://example.invalid:bad/job",
    "https://example.invalid:0/job", "https://example.invalid/job\nprivate"])
def test_url_validation_never_opens_browser(settings, url):
    def forbidden(): raise AssertionError("browser should not open")
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_posting(url, settings, playwright_factory=forbidden)
    assert url not in str(error.value)


@pytest.mark.parametrize("identifier", ["../posting", "cap-../posting", "cap-" + "0" * 31,
                                        "cap-" + "A" * 32, "cap-" + "0" * 32 + "/other"])
def test_capture_id_is_not_a_path(settings, identifier):
    with pytest.raises(capture.CaptureError, match="invalid capture ID"):
        capture.load_capture(settings.jobagent_home, identifier)


def test_corrupt_missing_or_external_capture_is_private(settings, monkeypatch, tmp_path):
    identifier = "cap-" + "0" * 32
    path = capture.capture_path(settings.jobagent_home, identifier)
    path.parent.mkdir()
    for source in ('{"source_text": "private content"', '{"version":1}',
                   json.dumps({"version": 1, "source_url": "private-invalid-url", "source_text": TEXT})):
        path.write_text(source, encoding="utf-8")
        with pytest.raises(capture.CaptureError) as error:
            capture.load_capture(settings.jobagent_home, identifier)
        assert "private" not in str(error.value)
    with pytest.raises(capture.CaptureError):
        capture.load_capture(settings.jobagent_home, "cap-" + "1" * 32)
    # Inject an escaped resolution to test confinement without Windows symlinks.
    monkeypatch.setattr(capture, "capture_path", lambda home, identifier: tmp_path / "outside.json")
    with pytest.raises(capture.CaptureError):
        capture.load_capture(settings.jobagent_home, identifier)


def test_cancel_and_browser_exception_do_not_expose_selected_text(settings):
    fake = FakeBrowser()
    def cancel(): raise EOFError("fake-private-content")
    with pytest.raises(capture.CaptureError, match="cancelled"):
        capture.capture_posting(URL, settings, confirm=cancel, playwright_factory=lambda: fake)
    assert fake.closed and "selection_only" not in fake.events
    fake = FakeBrowser()
    def fail(expression): raise RuntimeError("fake-private-content")
    fake.evaluate = fail
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_posting(URL, settings, confirm=lambda: None, playwright_factory=lambda: fake)
    assert "fake-private-content" not in str(error.value) and fake.closed


def test_cli_capture_prints_only_opaque_id(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda settings: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(capture, "capture_posting", lambda url, settings:
                        capture.save_capture(settings.jobagent_home, url, TEXT))
    assert cli.main(["capture", "--url", URL]) == 0
    output = capsys.readouterr().out.strip()
    assert capture.CAPTURE_ID.fullmatch(output)
    assert capture.load_capture(settings.jobagent_home, output).source_text == TEXT.strip()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["capture", "--url", URL]) == 1
    assert capsys.readouterr().out == ""


def test_tailor_capture_passes_url_and_text_without_fetch(settings, monkeypatch):
    from jobagent import bank, bullets, llm

    identifier = capture.save_capture(settings.jobagent_home, URL, TEXT)
    pdf = settings.jobagent_home / "output" / "fake" / "resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"fake PDF")
    monkeypatch.setattr(bullets, "load_bullets", lambda path: object())
    monkeypatch.setattr(bank.BulletIndex, "from_settings", lambda settings: SimpleNamespace(retrieve=lambda *args: []))
    monkeypatch.setattr(llm, "coordinator_llm", lambda settings: object())
    monkeypatch.setattr(llm, "writer_llm", lambda settings: object())
    monkeypatch.setattr(graph, "open_checkpointer", lambda settings: None)
    class App:
        def get_state(self, config): return SimpleNamespace(next=())
        def invoke(self, state, config):
            assert state == {"source_url": URL, "source_text": TEXT.strip()}
            return {"pdf_path": str(pdf), "pages": 1, "job": {"company": "Fake", "title": "Engineer"}}
    monkeypatch.setattr(graph, "build_graph", lambda deps, checkpointer: App())
    settings.bullets_file = settings.jobagent_home / "bullets.yaml"
    # LLM factories and graph are fakes, so source loading is exercised without services.
    args = argparse.Namespace(capture=identifier, file=None, url=None, fresh=False)
    assert cli.cmd_tailor(settings, args) == 0
    record = ApplicationStore(settings.jobagent_home).get(application_id(URL, TEXT))
    assert record.posting_url == "https://example.invalid/jobs/123"


def test_tailor_capture_sources_are_exclusive(monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: (_ for _ in ()).throw(AssertionError("must not load settings")))
    with pytest.raises(SystemExit) as error:
        cli.main(["tailor", "--capture", "cap-" + "0" * 32, "--url", URL])
    assert error.value.code == 2
