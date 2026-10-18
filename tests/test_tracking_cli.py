from types import SimpleNamespace

import pytest

from jobagent import browser, cli, tracker
from jobagent.applications import ApplicationStore


@pytest.fixture
def local(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pdf = home / "output" / "resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"fake resume")
    store = ApplicationStore(home)
    record = store.save_resume(source_url="https://example.com/1", source_text=None,
                              company="Private Company", title="Private Role", pdf=pdf)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(jobagent_home=home))
    monkeypatch.setattr(cli, "setup_logging", lambda settings: None)
    return store, record


def test_status_requires_human_confirmation(local, capsys):
    store, record = local
    assert cli.main(["status", "--application", record.id]) == 0
    assert "resume_ready" in capsys.readouterr().out
    assert cli.main(["status", "--application", record.id, "--set", "submitted"]) == 1
    assert store.get(record.id).status == "resume_ready"
    assert cli.main(["status", "--application", record.id, "--set", "submitted",
                     "--confirm-submitted"]) == 0
    assert store.get(record.id).status == "submitted"
    assert "Private" not in capsys.readouterr().out


def test_sync_failure_is_pending_and_retry_succeeds(local, monkeypatch, capsys):
    store, record = local

    class Transport:
        rows = []
        fail = True
        def read(self):
            if self.fail:
                raise RuntimeError("Private API details")
            return self.rows
        def write(self, row, values):
            while len(self.rows) < row:
                self.rows.append([])
            self.rows[row - 1] = values

    remote = Transport()
    monkeypatch.setattr(tracker, "from_settings", lambda settings, journal:
                        tracker.SheetsTracker(journal, remote, "target"))
    assert cli.main(["sync"]) == 1
    assert store.pending("target") == [record.id]
    assert "Private" not in capsys.readouterr().out
    remote.fail = False
    assert cli.main(["sync"]) == 0
    assert store.pending("target") == []
    assert cli.main(["track", "--application", record.id]) == 0
    assert len(remote.rows) == 2


def test_fill_journals_review_before_return(local, monkeypatch, capsys):
    store, old_record = local
    record = store.save_resume(source_url="https://boards.greenhouse.io/fake/jobs/123?token=fake-token",
                              source_text=None, company="Private Company", title="Private Role",
                              pdf=store.home / old_record.resume_path)
    profile = store.home / "profile.yaml"
    profile.write_text("first_name: Fake\nlast_name: Applicant\nemail: fake@example.invalid\n", encoding="utf-8")
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(
        jobagent_home=store.home, profile_file=profile, browser_profile_dir=store.home / "browser_profile"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def fake_run(url, profile, pdf, settings, on_result):
        assert url == "https://boards.greenhouse.io/fake/jobs/123"
        assert pdf.is_file()
        result = browser.FillResult("review_needed", 3, 2)
        on_result(result)
        assert store.get(record.id).status == "review_needed"
        return result

    monkeypatch.setattr(browser, "run_headed", fake_run)
    assert cli.main(["fill", "--application", record.id]) == 0
    output = capsys.readouterr().out
    assert "3 filled, 2 skipped" in output
    assert "Private" not in output and "fake@example.invalid" not in output
    store.set_status(record.id, "submitted", confirmed=True)
    assert cli.main(["fill", "--application", record.id]) == 1
    assert store.get(record.id).status == "submitted"


def test_fill_requires_interactive_review(local, monkeypatch):
    store, record = local
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["fill", "--application", record.id]) == 1
    assert store.get(record.id).status == "resume_ready"
