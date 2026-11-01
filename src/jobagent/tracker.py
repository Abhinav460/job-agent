"""Sheets upserts to a dedicated Applications worksheet, without append retries.

Use one local journal per worksheet. Its SQLite lock serializes writers, and
each retry reconciles remote IDs, including writes whose response was lost.
Independent journals writing concurrently to the same sheet are unsupported.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol
from urllib.parse import quote, urlsplit

from jobagent.applications import ApplicationStore

HEADERS = ["application_id", "company", "title", "posting_url", "resume_path", "status", "updated_at"]
RANGE = "'Applications'!A:G"


class TrackerError(RuntimeError):
    """A deliberately generic error: API bodies can contain personal data."""


def spreadsheet_id(value: str) -> str:
    """Accept a Sheets ID or its canonical editor URL, never arbitrary URLs."""
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]{20,200})(?:/edit)?/?", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or not match:
            raise TrackerError("Enter a Google Sheets spreadsheet ID or editor URL")
        value = match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,200}", value) is None:
        raise TrackerError("Enter a Google Sheets spreadsheet ID or editor URL")
    return value


class SheetTransport(Protocol):
    def read(self) -> list[list[str]]: ...
    def write(self, row: int, values: list[str]) -> None: ...


class SheetsTracker:
    def __init__(self, store: ApplicationStore, transport: SheetTransport, target: str):
        self.store, self.transport, self.target = store, transport, target

    def sync(self, identifier: str) -> None:
        try:
            with self.store.transaction() as db:
                record = self.store._get(db, identifier)
                rows = self.transport.read()
                if rows and rows[0] != HEADERS:
                    raise TrackerError("Applications worksheet has unexpected columns")
                if not rows:
                    self.transport.write(1, HEADERS)
                    rows = [HEADERS]
                matches = [i + 1 for i, r in enumerate(rows[1:], 1) if r and r[0] == identifier]
                if len(matches) > 1:
                    raise TrackerError("Applications worksheet contains duplicate application IDs")
                row = matches[0] if matches else len(rows) + 1
                self.transport.write(row, [record.id, record.company, record.title,
                    record.posting_url, record.resume_path, record.status, record.updated_at])
                db.execute("INSERT OR REPLACE INTO synced VALUES (?, ?, ?)",
                           (self.target, identifier, record.updated_at))
        except TrackerError:
            raise
        except Exception:
            raise TrackerError("Sheets sync failed; local application remains saved") from None


class GoogleSheetsTransport:
    def __init__(self, sheet_id: str, session):
        self.session = session
        self.spreadsheet_url = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(sheet_id, safe="")
        self.base = self.spreadsheet_url + "/values/"

    @staticmethod
    def create(session, title: str) -> str:
        """Create a spreadsheet with an Applications tab and literal headers in one request."""
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise TrackerError("Enter a spreadsheet title of up to 100 characters")
        body = {
            "properties": {"title": title.strip()},
            "sheets": [{
                "properties": {"title": "Applications"},
                "data": [{"startRow": 0, "startColumn": 0, "rowData": [{"values": [
                    {"userEnteredValue": {"stringValue": header}} for header in HEADERS
                ]}]}],
            }],
        }
        try:
            response = session.post("https://sheets.googleapis.com/v4/spreadsheets",
                                    params={"fields": "spreadsheetId"}, json=body, timeout=20)
            response.raise_for_status()
            return spreadsheet_id(response.json()["spreadsheetId"])
        except TrackerError:
            raise
        except Exception:
            raise TrackerError("Could not create spreadsheet; check Sheets access") from None

    def _sheet_titles(self) -> list[str]:
        response = self.session.get(self.spreadsheet_url,
                                    params={"fields": "sheets(properties(title))"}, timeout=20)
        response.raise_for_status()
        sheets = response.json()["sheets"]
        if not isinstance(sheets, list):
            raise ValueError("invalid sheet list")
        titles = [sheet["properties"]["title"] for sheet in sheets]
        if not all(isinstance(title, str) for title in titles):
            raise ValueError("invalid sheet title")
        return titles

    def ensure_worksheet(self) -> bool:
        """Add only a missing Applications tab, then verify or write its headers."""
        try:
            created = "Applications" not in self._sheet_titles()
            if created:
                try:
                    response = self.session.post(self.spreadsheet_url + ":batchUpdate",
                        json={"requests": [{"addSheet": {"properties": {"title": "Applications"}}}]},
                        timeout=20)
                    response.raise_for_status()
                except Exception:
                    # A lost response or another writer may have added the tab.
                    if "Applications" not in self._sheet_titles():
                        raise
            rows = self.read()
            if rows and rows[0] != HEADERS:
                raise TrackerError("Applications worksheet has unexpected columns")
            if not rows:
                self.write(1, HEADERS)
            return created
        except TrackerError:
            raise
        except Exception:
            raise TrackerError("Could not prepare Applications worksheet; other tabs were kept") from None

    def read(self) -> list[list[str]]:
        response = self.session.get(self.base + quote(RANGE, safe=""), timeout=20)
        response.raise_for_status()
        return response.json().get("values", [])

    def write(self, row: int, values: list[str]) -> None:
        destination = quote(f"'Applications'!A{row}:G{row}", safe="")
        response = self.session.put(self.base + destination,
            params={"valueInputOption": "RAW"}, json={"values": [values]}, timeout=20)
        response.raise_for_status()


def _authorized_session(settings):
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        # Keys must live inside the private home, even when configured explicitly.
        from jobagent.safepaths import resolve_within
        key = resolve_within(settings.service_account_file, [settings.jobagent_home])
        credentials = Credentials.from_service_account_file(str(key),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return AuthorizedSession(credentials)
    except Exception:
        raise TrackerError("Sheets credentials unavailable; check private setup and tracker dependencies") from None


def create_spreadsheet(settings, *, title: str = "Job Applications") -> str:
    """Create a new private tracker sheet using the configured service account."""
    return GoogleSheetsTransport.create(_authorized_session(settings), title)


def ensure_applications_worksheet(settings, sheet_id: str) -> bool:
    """Prepare an existing sheet; return True only when Applications was added."""
    return GoogleSheetsTransport(spreadsheet_id(sheet_id), _authorized_session(settings)).ensure_worksheet()


def from_settings(settings, store: ApplicationStore, *, sheet_id: str | None = None) -> SheetsTracker:
    if sheet_id is None and settings.google_sheet_id is None:
        raise TrackerError("GOOGLE_SHEET_ID is not configured")
    sheet_id = spreadsheet_id(sheet_id if sheet_id is not None
                              else settings.google_sheet_id.get_secret_value())
    try:
        session = _authorized_session(settings)
        target = hashlib.sha256(sheet_id.encode()).hexdigest()
        return SheetsTracker(store, GoogleSheetsTransport(sheet_id, session), target)
    except TrackerError:
        raise
