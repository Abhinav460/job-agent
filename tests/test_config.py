import os

import pytest

from jobagent.config import Settings, apply_tracing_policy

FAKE_KEY = "sk-ant-" + "api03-" + "x" * 40


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("JOBAGENT_HOME", "~/fake-jobagent-home")
    return Settings()


def test_repr_and_str_hide_everything(settings):
    for text in (repr(settings), str(settings), f"{settings}"):
        assert FAKE_KEY not in text
        assert text == "Settings(<redacted>)"


def test_secret_fields_are_masked(settings):
    assert FAKE_KEY not in str(settings.anthropic_api_key)
    assert FAKE_KEY not in str(settings.model_dump())
    assert settings.anthropic_api_key.get_secret_value() == FAKE_KEY


def test_home_is_expanded_and_outside_repo(settings):
    assert settings.jobagent_home.is_absolute()
    assert settings.checkpoint_db.is_relative_to(settings.jobagent_home)
    assert settings.service_account_file.is_relative_to(settings.secrets_dir)


def test_empty_secret_becomes_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    assert Settings().google_api_key is None


def test_tracing_forced_off_by_default(settings, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")  # e.g. exported globally
    apply_tracing_policy(settings)
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        assert os.environ[var] == "false"


def test_tracing_needs_opt_in_and_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    apply_tracing_policy(Settings())
    assert os.environ["LANGSMITH_TRACING"] == "false"
