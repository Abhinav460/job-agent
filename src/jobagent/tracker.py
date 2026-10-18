"""Sheets upserts to a dedicated Applications worksheet, without append retries.

Use one local journal per worksheet. Its SQLite lock serializes writers, and
each retry reconciles remote IDs, including writes whose response was lost.
Independent journals writing concurrently to the same sheet are unsupported.
"""

from __future__ import annotations

import hashlib
from typing import Protocol
from urllib.parse import quote

from jobagent.applications import ApplicationStore

HEADERS = ["application_id", "company", "title", "posting_url", "resume_path", "status", "updated_at"]
RANGE = "'Applications'!A:G"


class TrackerError(RuntimeError):
    """A deliberately generic error: API bodies can contain personal data."""


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
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(sheet_id, safe="") + "/values/"

    def read(self) -> list[list[str]]:
        response = self.session.get(self.base + quote(RANGE, safe=""), timeout=20)
        response.raise_for_status()
        return response.json().get("values", [])

    def write(self, row: int, values: list[str]) -> None:
        destination = quote(f"'Applications'!A{row}:G{row}", safe="")
        response = self.session.put(self.base + destination,
            params={"valueInputOption": "RAW"}, json={"values": [values]}, timeout=20)
        response.raise_for_status()


def from_settings(settings, store: ApplicationStore) -> SheetsTracker:
    if settings.google_sheet_id is None:
        raise TrackerError("GOOGLE_SHEET_ID is not configured")
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        # Keys must live inside the private home, even when configured explicitly.
        from jobagent.safepaths import resolve_within
        key = resolve_within(settings.service_account_file, [settings.jobagent_home])
        credentials = Credentials.from_service_account_file(str(key),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        session = AuthorizedSession(credentials)
        sheet_id = settings.google_sheet_id.get_secret_value()
        target = hashlib.sha256(sheet_id.encode()).hexdigest()
        return SheetsTracker(store, GoogleSheetsTransport(sheet_id, session), target)
    except Exception:
        raise TrackerError("Sheets credentials unavailable; check private setup and tracker dependencies") from None
