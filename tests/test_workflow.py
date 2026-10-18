"""Offline demonstration of the real CLI, graph, journal, browser and tracker glue.

All applicant data is fictional. LLMs, fetching, compilation, browser transport,
and Google HTTP are replaced; any attempted socket connection fails the test.
"""

from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
from urllib.parse import unquote
import re

import pytest

from jobagent import bank, browser, capture, cli, compile as tex_compile, job, llm, tailor, tracker
from jobagent.applications import ApplicationStore, application_id
from jobagent.bank import ScoredBullet
from jobagent.job import ParsedJob
from jobagent.tailor import BulletChoice, EntryChoice, SkillLine, WriterOutput

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
URL = "https://boards.greenhouse.io/fakecompany/jobs/123?token=fake-token#section"
PUBLIC_URL = "https://boards.greenhouse.io/fakecompany/jobs/123"
POSTING = ("Fictional data engineer role: Python, SQL, pipelines. No real application. " * 4).strip()


class FakeSheetsSession:
    def __init__(self, events):
        self.events, self.rows = events, []
        self.lose_response = True

    def get(self, url, **kwargs):
        rows = [list(row) for row in self.rows]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"values": rows})

    def put(self, url, *, params, json, **kwargs):
        assert params == {"valueInputOption": "RAW"}
        row = int(re.search(r"!A(\d+):G\d+$", unquote(url)).group(1))
        while len(self.rows) < row:
            self.rows.append([])
        self.rows[row - 1] = list(json["values"][0])
        self.events.append("sheet_write")
        if row > 1 and self.lose_response:
            self.lose_response = False
            raise TimeoutError("fake private API details must not reach output")
        return SimpleNamespace(raise_for_status=lambda: None)


class FakeBrowser:
    def __init__(self, events):
        self.events, self.values, self.guard = events, {}, None
        self.url, self.closed, self.blocked_writes = "about:blank", False, 0
        self.fields = [
            {"id": "first_name", "label": "First Name *", "type": "text"},
            {"id": "last_name", "label": "Last Name *", "type": "text"},
            {"id": "email", "label": "Email *", "type": "email"},
            {"id": "resume", "label": "Resume/CV *", "type": "file"},
            {"id": "country", "label": "Country", "type": "select",
             "options": [{"label": "Canada", "value": "CA"}]},
            {"id": "salary", "label": "Expected Salary", "type": "text"},
            {"id": "consent", "label": "I agree", "type": "checkbox"},
            {"id": "submit", "label": "Submit application", "type": "submit"},
        ]
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def __enter__(self): return self
    def __exit__(self, *args): self.events.append("browser_exit")

    def launch(self, directory, **kwargs):
        assert kwargs["headless"] is False and kwargs["service_workers"] == "block"
        self.events.append("browser_open")
        return self

    def route(self, pattern, handler): self.guard = handler
    def unroute(self, pattern, handler):
        assert self.guard is handler
        self.guard = None

    def new_page(self): return self
    def goto(self, url, **kwargs):
        assert url == PUBLIC_URL
        self.url = url

    def close(self):
        self.closed = True
        self.events.append("browser_close")

    def locator(self, selector):
        if selector == "#application_form":
            return SimpleNamespace(count=lambda: 1, locator=self.form_locator)
        return SimpleNamespace(count=lambda: 0)  # no login or CAPTCHA challenge

    def form_locator(self, selector):
        if selector == "input, select, textarea":
            return SimpleNamespace(evaluate_all=lambda script: self.fields)
        identifier = selector.split('"')[1]

        def enter(value, **kwargs):
            assert identifier not in ("salary", "consent", "submit")
            assert self.guard is not None
            calls = []
            self.guard(SimpleNamespace(request=SimpleNamespace(method="POST"),
                                      abort=lambda: calls.append("abort"),
                                      continue_=lambda: calls.append("continue")))
            assert calls == ["abort"]
            self.blocked_writes += 1
            self.values[identifier] = value
            self.events.append("field_action")

        def upload(value, **kwargs):
            assert Path(value).read_bytes() == b"%PDF-fictional-workflow"
            enter(value)

        def forbidden(*args, **kwargs):
            raise AssertionError("automatic clicks and keypresses are forbidden")

        return SimpleNamespace(count=lambda: 1, fill=enter, select_option=enter,
                               set_input_files=upload, click=forbidden, press=forbidden)


@pytest.mark.parametrize("use_capture", [False, True], ids=["url", "capture"])
def test_offline_application_workflow(tmp_path, monkeypatch, capsys, use_capture):
    events = []

    def no_network(*args, **kwargs):
        raise AssertionError("offline workflow attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    for name in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "false")
    # Inject fake settings rather than loading the developer's environment.
    home = tmp_path / "fake-home"
    home.mkdir()
    settings = SimpleNamespace(
        jobagent_home=home, output_dir=home / "output", resume_tex=home / "resume.tex",
        bullets_file=home / "bullets.yaml", profile_file=home / "profile.yaml",
        browser_profile_dir=home / "browser_profile", checkpoint_db=home / "checkpoints" / "graph.sqlite",
        resume_retrieval_top_k=30, resume_max_bullets_per_entry=4, resume_max_projects=3,
        resume_allow_rephrase=True,
    )
    for name in ("resume.tex", "bullets.yaml"):
        shutil.copyfile(EXAMPLES / name, home / name)
    settings.profile_file.write_text(
        "first_name: Fake\nlast_name: Applicant\nemail: fake@example.invalid\ncountry: Canada\n",
        encoding="utf-8")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda settings: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def fetch(url):
        assert not use_capture, "captured postings must never be fetched again"
        assert url == URL
        events.append("posting_fetch")
        return POSTING

    def parse(text, model):
        assert text == POSTING
        events.append("posting_parse")
        return ParsedJob(company="=FICTIONAL()", title="Data Engineer",
                         summary="Fake pipelines", domains=["data"], required_skills=["Python"])

    def write(model, posting, bullets, scores, *args):
        events.append("resume_write")
        def choices(entries):
            return [EntryChoice(entry_id=e.id, bullets=[
                BulletChoice(bullet_id=b.id, text=b.text) for b in e.bullets]) for e in entries]
        return WriterOutput(experience=choices(bullets.experience), projects=choices(bullets.projects),
                            skills=[SkillLine(category=c, items=items) for c, items in bullets.skills.items()])

    def retrieve(query, domains, top_k):
        from jobagent.bullets import load_bullets
        return [ScoredBullet(ref.bullet.id, 1 - i / 100)
                for i, ref in enumerate(load_bullets(settings.bullets_file).refs())]

    def compile_pdf(tex, **kwargs):
        assert "Placeholder" not in tex.read_text(encoding="utf-8")
        pdf = tex.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-fictional-workflow")
        events.append("resume_compile")
        return pdf

    monkeypatch.setattr(job, "fetch_job_text", fetch)
    monkeypatch.setattr(job, "parse_job", parse)
    monkeypatch.setattr(llm, "coordinator_llm", lambda settings: object())
    monkeypatch.setattr(llm, "writer_llm", lambda settings: object())
    monkeypatch.setattr(bank.BulletIndex, "from_settings", lambda settings: SimpleNamespace(retrieve=retrieve))
    monkeypatch.setattr(tailor, "write", write)
    monkeypatch.setattr(tex_compile, "compile_tex", compile_pdf)
    monkeypatch.setattr(tex_compile, "page_count", lambda path: 1)
    if use_capture:
        capture_id = capture.save_capture(home, URL, POSTING)
        assert cli.main(["tailor", "--capture", capture_id]) == 0
        assert "posting_fetch" not in events
    else:
        assert cli.main(["tailor", "--url", URL]) == 0
    store = ApplicationStore(home)
    identifier = application_id(URL, None)
    record = store.get(identifier)
    assert record.status == "resume_ready" and record.posting_url == PUBLIC_URL
    pdf = home / record.resume_path
    assert identifier in str(pdf.parent)
    original_pdf = pdf.read_bytes()

    fake_browser = FakeBrowser(events)
    actual_headed = browser.run_headed

    def human_review():
        assert not fake_browser.closed and fake_browser.guard is None
        assert store.get(identifier).status == "review_needed"
        assert fake_browser.values == {"first_name": "Fake", "last_name": "Applicant",
                                      "email": "fake@example.invalid", "resume": str(pdf.resolve()), "country": "CA"}
        assert fake_browser.blocked_writes == 5
        events.append("human_review")

    def headed(url, profile, pdf, settings, on_result):
        return actual_headed(url, profile, pdf, settings, on_result,
                             wait_for_review=human_review, playwright_factory=lambda: fake_browser)

    monkeypatch.setattr(browser, "run_headed", headed)
    assert cli.main(["fill", "--application", identifier]) == 0
    assert events.index("human_review") < events.index("browser_close")
    assert store.get(identifier).status == "review_needed"

    remote = FakeSheetsSession(events)
    monkeypatch.setattr(tracker, "from_settings", lambda settings, journal:
        tracker.SheetsTracker(journal, tracker.GoogleSheetsTransport("fake-sheet", remote), "fake-target"))
    assert cli.main(["track", "--application", identifier]) == 1  # response lost after write
    assert store.pending("fake-target") == [identifier]
    assert store.get(identifier).status == "review_needed" and pdf.read_bytes() == original_pdf
    assert cli.main(["sync"]) == 0
    assert cli.main(["track", "--application", identifier]) == 0
    assert len(remote.rows) == 2 and remote.rows[1][0] == identifier
    assert remote.rows[1][1] == "=FICTIONAL()" and remote.rows[1][3] == PUBLIC_URL
    assert remote.rows[1][5] == "review_needed" and store.pending("fake-target") == []
    assert cli.main(["status", "--application", identifier, "--set", "submitted"]) == 1
    assert store.get(identifier).status == "review_needed"
    assert cli.main(["status", "--application", identifier, "--set", "submitted", "--confirm-submitted"]) == 0
    assert cli.main(["sync"]) == 0
    assert len(remote.rows) == 2 and remote.rows[1][5] == "submitted"
    assert ApplicationStore(home).get(identifier).status == "submitted"
    assert pdf.read_bytes() == original_pdf
    output = capsys.readouterr().out
    for private in ("fake@example.invalid", "fake-token", "=FICTIONAL()", "private API details"):
        assert private not in output
