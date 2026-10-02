"""Redact secrets and personal data from log records and uncaught tracebacks.

setup_logging() installs a RedactingFilter on every root handler. The filter
rewrites each record before any handler formats it, so third-party loggers
(httpx logs request URLs, which can carry ?key=AIza...) are covered too.

Redacted:
  * known credential formats (Anthropic, Google, LangSmith, GitHub, AWS,
    Tailscale, JWTs, OAuth bearer tokens, PEM private keys, service-account
    private_key fields);
  * email addresses and formatted phone numbers;
  * Tailscale hostnames (*.ts.net) and tailnet IPs (100.64.0.0/10);
  * the literal value of every SecretStr setting (API keys, owner name/email/
    phone) and of every environment variable whose name looks secret.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterable
from types import TracebackType

from jobagent.config import Settings

REDACTED = "[REDACTED]"

# (pattern, replacement). Replacements keep a non-secret prefix where useful.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), REDACTED),
    (re.compile(r"(\"private_key\"\s*:\s*\")[^\"]*"), rf"\1{REDACTED}"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), REDACTED),
    (re.compile(r"ya29\.[0-9A-Za-z_\-.]{10,}"), REDACTED),
    (re.compile(r"lsv2_[A-Za-z0-9_]{20,}|ls__[0-9a-f]{32}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"tskey-[a-z]+-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),  # JWT
    (re.compile(r"(?i)(authorization:?\s*[\"']?\s*bearer\s+)[^\s\"',}]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&\s\"']+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)((?:x-api-key|api[_-]?key|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), rf"\1{REDACTED}"),
    # Personal data.
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w.]*\d)"), "[PHONE]"),
    # Tailscale MagicDNS names and 100.64.0.0/10 addresses reveal the user's tailnet.
    (re.compile(r"\b[\w-]+\.[\w-]+\.ts\.net\b"), "[TAILNET-HOST]"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "[TAILNET-IP]"),
]

# Environment variable names whose values are always redacted.
_SECRET_ENV_NAME = re.compile(
    r"(API_?KEY|_KEY$|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|OWNER_)", re.I
)
# Shorter values are too likely to collide with ordinary words ("true", "1").
_MIN_ENV_VALUE_LEN = 8
_MIN_SETTING_VALUE_LEN = 4


class Redactor:
    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._literal_re: re.Pattern[str] | None = None
        self.update(literals)

    def update(self, literals: Iterable[str]) -> None:
        values = {v for v in literals if v}
        # Also catch a phone number written with different separators.
        values |= {re.sub(r"\D", "", v) for v in values if re.fullmatch(r"[\d\s().+-]{7,}", v)}
        values = sorted((v for v in values if v), key=len, reverse=True)
        self._literal_re = (
            re.compile("|".join(re.escape(v) for v in values), re.I) if values else None
        )

    def __call__(self, text: str) -> str:
        if self._literal_re is not None:
            text = self._literal_re.sub(REDACTED, text)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def secret_literals(settings: Settings | None = None, environ: dict[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    literals = [
        value
        for name, value in environ.items()
        if _SECRET_ENV_NAME.search(name) and len(value) >= _MIN_ENV_VALUE_LEN
    ]
    if settings is not None:
        literals += [v for v in settings.secret_values() if len(v) >= _MIN_SETTING_VALUE_LEN]
    return literals


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # bad %-args; don't let logging crash
            message = str(record.msg)
        record.msg = self.redactor(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redactor(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redactor(record.stack_info)
        return True


def setup_logging(settings: Settings) -> Redactor:
    """Configure root logging to stderr with redaction, and redact uncaught tracebacks."""
    redactor = Redactor(secret_literals(settings))
    redacting = RedactingFilter(redactor)

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redacting)
    root.setLevel(settings.log_level.upper())
    logging.captureWarnings(True)

    def excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("jobagent").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    return redactor
