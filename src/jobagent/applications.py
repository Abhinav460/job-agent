"""Private, durable application journal. No network calls or profile answers."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from jobagent import safepaths

Status = Literal["resume_ready", "review_needed", "blocked", "submitted"]
STATUSES = ("resume_ready", "review_needed", "blocked", "submitted")


def public_posting_url(url: str | None) -> str:
    """Persist public location only; authentication links can contain tokens."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        raise ValueError("invalid public posting URL") from None


def application_id(source_url: str | None, source_text: str | None) -> str:
    source = (source_url or source_text or "").strip()
    if not source:
        raise ValueError("an application needs a posting URL or text")
    return "app-" + hashlib.sha256(source.encode()).hexdigest()[:24]


@dataclass(frozen=True, repr=False)
class ApplicationRecord:
    id: str
    company: str
    title: str
    posting_url: str
    resume_path: str
    status: Status
    updated_at: str

    def __repr__(self) -> str:
        return "ApplicationRecord(<private>)"


class ApplicationStore:
    def __init__(self, home: Path):
        self.home = home
        directory = safepaths.ensure_private_dir(home / "checkpoints", [home])
        self.path = safepaths.resolve_within(directory / "applications.sqlite", [home])
        if not self.path.exists():
            self.path.touch(mode=0o600)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                posting_url TEXT NOT NULL, resume_path TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS synced (
                target TEXT NOT NULL, id TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(target, id))""")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, identifier: str) -> ApplicationRecord:
        row = db.execute("SELECT * FROM applications WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("application not found")
        return ApplicationRecord(**dict(row))

    def get(self, identifier: str) -> ApplicationRecord:
        with self.transaction() as db:
            return self._get(db, identifier)

    def save_resume(self, *, source_url: str | None, source_text: str | None,
                    company: str, title: str, pdf: Path) -> ApplicationRecord:
        pdf = safepaths.resolve_within(pdf, [self.home / "output"])
        if not pdf.is_file():
            raise ValueError("resume PDF is missing")
        identifier = application_id(source_url, source_text)
        # Preserve an explicit human status when regenerating the resume.
        with self.transaction() as db:
            db.execute("""INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET company=excluded.company,
                title=excluded.title, posting_url=excluded.posting_url,
                resume_path=excluded.resume_path, updated_at=excluded.updated_at""",
                (identifier, company, title, public_posting_url(source_url),
                 pdf.relative_to(self.home.resolve()).as_posix(), "resume_ready", _now()))
            return self._get(db, identifier)

    def set_status(self, identifier: str, status: Status, *, confirmed: bool = False):
        if status not in STATUSES:
            raise ValueError("unsupported application status")
        if status == "submitted" and not confirmed:
            raise ValueError("submitted requires explicit human confirmation")
        with self.transaction() as db:
            self._get(db, identifier)
            db.execute("UPDATE applications SET status=?, updated_at=? WHERE id=?",
                       (status, _now(), identifier))
            return self._get(db, identifier)

    def pending(self, target: str) -> list[str]:
        with self.transaction() as db:
            return [r[0] for r in db.execute("""SELECT a.id FROM applications a
                LEFT JOIN synced s ON s.id=a.id AND s.target=?
                WHERE s.updated_at IS NULL OR s.updated_at != a.updated_at ORDER BY a.id""",
                (target,))]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
