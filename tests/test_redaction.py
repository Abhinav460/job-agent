"""Fake secrets are assembled at runtime so no key-shaped literal is committed."""

import logging

import pytest

from jobagent.config import Settings
from jobagent.redaction import RedactingFilter, Redactor, secret_literals, setup_logging

ANTHROPIC = "sk-ant-" + "api03-" + "A1b2C3d4" * 6
GOOGLE = "AI" + "za" + "SyA" + "q" * 32
LANGSMITH = "lsv2_" + "pt_" + "0123abcd" * 4 + "_" + "0123456789"
PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END " + "PRIVATE KEY-----"
EMAIL = "someone.real" + "@" + "gmail.com"
PHONE = "(973) 555-" + "8812"
TAILSCALE = "tskey-" + "auth-" + "kQ7x2" * 4
JWT = "ey" + "J" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2Q"
TAILNET_HOST = "homebox" + ".tail1234" + ".ts.net"
TAILNET_IP = "100." + "101.42.7"


@pytest.mark.parametrize(
    "secret", [ANTHROPIC, GOOGLE, LANGSMITH, PEM, EMAIL, PHONE, TAILSCALE, JWT, TAILNET_HOST, TAILNET_IP],
    ids=["anthropic", "google", "langsmith", "pem", "email", "phone", "tailscale", "jwt",
         "tailnet-host", "tailnet-ip"],  # keep fake values out of test ids
)
def test_patterns(secret):
    out = Redactor()(f"before {secret} after")
    assert secret not in out
    assert out.startswith("before ") and out.endswith(" after")


def test_url_query_key_and_bearer():
    r = Redactor()
    assert "hunter2hunter2" not in r("GET https://x.test/v1?key=hunter2hunter2&alt=json")
    assert "tok123456" not in r("Authorization: Bearer tok123456")
    assert "pw-value" not in r('{"password": "pw-value"}')


def test_service_account_private_key_field():
    sa_type = '"type": "service' + '_account"'
    out = Redactor()("{" + sa_type + ', "private_key": "abc\\ndef"}')
    assert "abc" not in out and sa_type in out


def test_literal_env_and_settings_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QDRANT_API_KEY", "custom-opaque-value-123")
    monkeypatch.setenv("JOBAGENT_OWNER_NAME", "Zebulon Quux")
    monkeypatch.setenv("JOBAGENT_OWNER_PHONE", "973-555-" + "8812")
    monkeypatch.setenv("HARMLESS", "custom-opaque-value-123-not-secret-name")
    redactor = Redactor(secret_literals(Settings()))
    out = redactor("q=custom-opaque-value-123 by zebulon quux, tel 9735558812")
    assert "custom-opaque-value-123" not in out
    assert "zebulon" not in out.lower()
    assert "9735558812" not in out


def test_short_env_values_ignored():
    assert secret_literals(environ={"SOME_TOKEN": "true"}) == []


def test_filter_redacts_args_and_exceptions(caplog):
    logger = logging.getLogger("test.redaction")
    logger.addFilter(RedactingFilter(Redactor([])))
    caplog.set_level(logging.INFO)
    try:
        raise RuntimeError(f"bad key {ANTHROPIC}")
    except RuntimeError:
        logger.exception("calling api with %s", GOOGLE)
    text = caplog.text
    assert ANTHROPIC not in text and GOOGLE not in text
    assert "calling api with [REDACTED]" in text


def test_setup_logging_covers_third_party_loggers(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "not-a-pattern-just-a-value")
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        setup_logging(Settings())
        logging.getLogger("httpx").warning("HTTP Request: GET /x?alt=json not-a-pattern-just-a-value")
    finally:
        root.handlers = saved
    err = capsys.readouterr().err
    assert "not-a-pattern-just-a-value" not in err
    assert "[REDACTED]" in err


def test_benign_text_untouched():
    text = "compiled resume.pdf (1 page) in 2.3s on 2026-09-26, version 1.2.3, port 6333, 10.0.0.5"
    assert Redactor()(text) == text
