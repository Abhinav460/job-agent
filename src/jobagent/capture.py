"""Explicit, selection-only posting capture. Never inspect the page or forms."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from jobagent import safepaths

CAPTURE_ID = re.compile(r"cap-[0-9a-f]{32}")
MIN_TEXT = 200
MAX_TEXT = 30_000
# JSON can encode one selected character as six characters (e.g. control
# escapes); this bound covers every accepted selection and URL.
MAX_RECORD_CHARS = 6 * (MAX_TEXT + 4096) + 256
SELECTION_SCRIPT = "window.getSelection().toString()"


class CaptureError(ValueError):
    """Only fixed messages: browser and parser errors may include private data."""


@dataclass(frozen=True, repr=False)
class CapturedPosting:
    source_url: str
    source_text: str

    def __repr__(self):
        return "CapturedPosting(<private>)"


def validate_url(url: str) -> str:
    try:
        if not isinstance(url, str) or len(url) > 4096 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port == 0):
            raise ValueError
        return url
    except ValueError:
        raise CaptureError("capture requires an HTTPS URL without credentials") from None


def validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise CaptureError("select only the job description before confirming capture")
    text = text.strip()
    if not MIN_TEXT <= len(text) <= MAX_TEXT or "\x00" in text:
        raise CaptureError("selected job description must contain 200 to 30000 characters")
    return text


def capture_path(home: Path, identifier: str) -> Path:
    if not isinstance(identifier, str) or CAPTURE_ID.fullmatch(identifier) is None:
        raise CaptureError("invalid capture ID")
    try:
        return safepaths.resolve_within(home / "captures" / f"{identifier}.json", [home / "captures"])
    except Exception:
        raise CaptureError("capture location is outside the private working directory") from None


def save_capture(home: Path, url: str, text: str) -> str:
    posting = CapturedPosting(validate_url(url), validate_text(text))
    identifier = "cap-" + uuid.uuid4().hex
    try:
        directory = safepaths.ensure_private_dir(home / "captures", [home])
        path = capture_path(home, identifier)
        # Also enforce the home root: a captures-directory symlink cannot escape.
        safepaths.resolve_within(directory, [home])
        payload = {"version": 1, "source_url": posting.source_url, "source_text": posting.source_text}
        safepaths.write_text(path, json.dumps(payload, ensure_ascii=False), [home, directory])
        return identifier
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("could not save the private posting capture") from None


def load_capture(home: Path, identifier: str) -> CapturedPosting:
    path = capture_path(home, identifier)
    try:
        safepaths.resolve_within(path, [home])
        raw = safepaths.read_text(path, [home / "captures"])
        if len(raw) > MAX_RECORD_CHARS:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"version", "source_url", "source_text"} or data["version"] != 1:
            raise ValueError
        return CapturedPosting(validate_url(data["source_url"]), validate_text(data["source_text"]))
    except Exception:
        raise CaptureError("posting capture unavailable or invalid") from None


def capture_posting(url: str, settings, *, confirm=None, playwright_factory=None) -> str:
    url = validate_url(url)
    if confirm is None:
        confirm = lambda: input("Select ONLY job-description text in the opened tab. Press Enter here to save it and close the browser: ")
    if playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
            playwright_factory = sync_playwright
        except ImportError:
            raise CaptureError("install browser dependencies and Chromium before capture") from None
    try:
        directory = safepaths.ensure_private_dir(settings.browser_profile_dir, [settings.jobagent_home])
        with playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(directory), headless=False, accept_downloads=False)
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                confirm()
                final_url = validate_url(page.url)
                # No body text, HTML, inputs, screenshot, or other page inspection.
                selection = page.evaluate(SELECTION_SCRIPT)
                return save_capture(settings.jobagent_home, final_url, selection)
            finally:
                context.close()
    except CaptureError:
        raise
    except (EOFError, KeyboardInterrupt):
        raise CaptureError("posting capture cancelled") from None
    except Exception:
        raise CaptureError("posting capture failed; selected text was not displayed") from None
