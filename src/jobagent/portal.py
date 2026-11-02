"""Private loopback portal for profile, resume selection, and application review.

The portal never serves document bytes. A random per-process token protects every
request, and all personal files stay outside the public source tree.
"""

from __future__ import annotations

import html
import hashlib
import json
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from jobagent.applications import ApplicationStore, application_id
from jobagent.config import REPO_ROOT, Settings
from jobagent.profile import ApplicantProfile
from jobagent.private_storage import secure_home
from jobagent.safepaths import ensure_private_dir, read_text, write_text
from jobagent.tracker import TrackerError, spreadsheet_id


class PortalError(ValueError):
    pass


def _safe_dir(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_dir() or path == REPO_ROOT or path.is_relative_to(REPO_ROOT):
        raise PortalError("Choose an existing resume folder outside the repository.")
    return path


class PortalState:
    def __init__(self, settings: Settings):
        secure_home(settings.jobagent_home)
        self.settings = settings
        self.store = ApplicationStore(settings.jobagent_home)
        self.token = secrets.token_urlsafe(32)
        self.notice = ""
        self.review_event: threading.Event | None = None
        self.handoff_event: threading.Event | None = None
        self.handoff_approved = False
        self.review_id: str | None = None
        self.lock = threading.Lock()
        self.context_lock = threading.Lock()
        self.context_query = ""
        self.context_results = []

    @property
    def folders_path(self) -> Path:
        return self.settings.jobagent_home / "resume_folders.json"

    @property
    def tracker_path(self) -> Path:
        return self.settings.jobagent_home / "tracker_config.json"

    def selected_sheet(self) -> str | None:
        if self.tracker_path.is_file():
            try:
                config = json.loads(read_text(self.tracker_path, [self.settings.jobagent_home]))
                return spreadsheet_id(config["spreadsheet_id"])
            except (OSError, ValueError, KeyError, TypeError):
                raise PortalError("Private spreadsheet selection is invalid; choose a spreadsheet again.") from None
        configured = self.settings.google_sheet_id
        return spreadsheet_id(configured.get_secret_value()) if configured else None

    def select_sheet(self, raw: str) -> None:
        identifier = spreadsheet_id(raw)
        write_text(self.tracker_path, json.dumps({"spreadsheet_id": identifier}),
                   [self.settings.jobagent_home])

    def _tracker(self):
        from jobagent.tracker import from_settings

        selected = self.selected_sheet()
        if selected is None:
            raise PortalError("Choose a spreadsheet before syncing.")
        return from_settings(self.settings, self.store, sheet_id=selected)

    def pending_count(self) -> int:
        selected = self.selected_sheet()
        if selected is None:
            return 0
        target = hashlib.sha256(selected.encode()).hexdigest()
        return len(self.store.pending(target))

    def folders(self) -> list[Path]:
        if not self.folders_path.is_file():
            return []
        try:
            values = json.loads(self.folders_path.read_text(encoding="utf-8"))
            return [_safe_dir(value) for value in values if isinstance(value, str)]
        except (ValueError, OSError, TypeError):
            return []

    def add_folder(self, raw: str) -> None:
        folder = _safe_dir(raw)
        values = [str(p) for p in self.folders()]
        if str(folder) not in values:
            values.append(str(folder))
        write_text(self.folders_path, json.dumps(values), [self.settings.jobagent_home])

    def context_notes(self):
        from jobagent.context_memory import ContextMemory

        with self.context_lock, ContextMemory(self.settings.jobagent_home) as memory:
            return memory.list_notes()

    def add_context_note(self, title: str, body: str):
        from jobagent.context_memory import ContextMemory

        with self.context_lock, ContextMemory(self.settings.jobagent_home) as memory:
            return memory.add(title, body)

    def delete_context_note(self, identifier: str) -> None:
        from jobagent.context_memory import ContextMemory

        with self.context_lock, ContextMemory(self.settings.jobagent_home) as memory:
            memory.delete(identifier)
        self.context_results = [hit for hit in self.context_results if hit.note.id != identifier]

    def search_context_notes(self, query: str):
        from jobagent.context_memory import ContextMemory

        with self.context_lock, ContextMemory(self.settings.jobagent_home) as memory:
            results = memory.search(query)
        self.context_query, self.context_results = query, results
        return results

    def save_profile(self, values: dict[str, str]) -> None:
        fields = [key for key in ApplicantProfile.model_fields if key != "answers"]
        payload = {key: value.strip() for key, value in values.items()
                   if key in fields and value.strip()}
        answers = {}
        seen_labels = set()
        for line in values.get("answers_text", "").splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                raise PortalError("Write extra answers as Question = Answer, one per line.")
            label, answer = (part.strip() for part in line.split("=", 1))
            normalized = " ".join(label.casefold().rstrip(":").split())
            if normalized in seen_labels:
                raise PortalError("Extra answer questions must be unique.")
            seen_labels.add(normalized)
            answers[label] = answer
        payload["answers"] = answers
        try:
            profile = ApplicantProfile.model_validate(payload)
        except Exception:
            raise PortalError("Check the name, email, and optional profile fields.") from None
        facts = {key: getattr(profile, key).get_secret_value()
                 for key in fields if getattr(profile, key) is not None}
        facts["answers"] = {key: value.get_secret_value()
                            for key, value in profile.answers.items()}
        write_text(self.settings.profile_file, yaml.safe_dump(facts, sort_keys=False),
                   [self.settings.jobagent_home])

    def prepare(self, *, company: str, title: str, url: str, description: str) -> tuple[str, str]:
        from jobagent.context_index import ContextIndex
        from jobagent.resume_catalog import ResumeCatalog, copy_for_company, index_directory, rank_resumes

        company, title, url, description = (s.strip() for s in (company, title, url, description))
        if not company or not title or len(company) > 120 or len(title) > 160:
            raise PortalError("Enter a company and role title.")
        if not description or len(description) > 30_000:
            raise PortalError("Paste a job description of up to 30,000 characters.")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise PortalError("Use a public HTTPS posting URL.")
        folders = self.folders()
        if not folders:
            raise PortalError("Add your resume folder first.")
        documents = []
        truncated = False
        for folder in folders:
            indexed = index_directory(folder)
            documents.extend(indexed.documents)
            truncated |= indexed.truncated
        pdfs = tuple(d for d in documents if d.status == "ready" and d.path.suffix.lower() == ".pdf")
        if not pdfs:
            raise PortalError("No readable PDF resume was found in the selected folders.")
        catalog = ResumeCatalog(root=Path.home(), documents=tuple(documents), truncated=truncated)
        with ContextIndex(self.settings.jobagent_home) as context:
            context.sync(catalog)
            hits = context.search(title + "\n" + description, limit=min(50, len(documents)))
        candidates = {hit.document_id for hit in hits}
        shortlist = tuple(d for d in pdfs if d.id in candidates) or pdfs
        matches = rank_resumes(ResumeCatalog(root=Path.home(), documents=shortlist),
                               title + "\n" + description, limit=1)
        if not matches:
            raise PortalError("No resume could be matched to this role.")
        identifier = application_id(url or None, description)
        output = ensure_private_dir(self.settings.output_dir / identifier,
                                    [self.settings.jobagent_home])
        pdf = copy_for_company(matches[0].document, company, output)
        record = self.store.save_resume(source_url=url or None, source_text=description,
                                        company=company, title=title, pdf=pdf)
        return record.id, "local Qdrant + " + matches[0].method

    def sync(self, identifier: str) -> bool:
        try:
            self._tracker().sync(identifier)
            return True
        except Exception:
            return False

    def sync_pending(self) -> tuple[int, int]:
        tracker = self._tracker()
        pending = self.store.pending(tracker.target)
        succeeded = failed = 0
        for identifier in pending:
            try:
                tracker.sync(identifier)
                succeeded += 1
            except TrackerError:
                failed += 1
        return succeeded, failed

    def instruction(self, raw: str) -> str:
        command = raw.strip()
        if len(command) > 500:
            raise PortalError("Tracker instruction is too long.")
        if re.fullmatch(r"(?:sync|update)\s+(?:all|pending)(?:\s+applications)?", command, re.I):
            done, failed = self.sync_pending()
            return f"Synced {done} pending application(s); {failed} still pending."
        match = re.fullmatch(r"(?:sync|update)\s+(app-[0-9a-f]{24})", command, re.I)
        if match:
            self.store.get(match.group(1))
            return ("Application synced." if self.sync(match.group(1))
                    else "Sync failed; application remains pending.")
        match = re.fullmatch(r"(?:use|select|change to)\s+(?:spreadsheet\s+)?(.+)", command, re.I)
        if match:
            self.select_sheet(match.group(1))
            return "Spreadsheet selected. Existing applications are pending for this spreadsheet."
        if re.fullmatch(r"(?:show|count)\s+pending", command, re.I):
            return f"{self.pending_count()} application(s) pending for the selected spreadsheet."
        raise PortalError("Supported instructions: use spreadsheet <ID or URL>, sync all, sync app-..., show pending.")

    def start_review(self, identifier: str) -> None:
        from jobagent.browser import run_headed
        from jobagent.profile import load_profile

        with self.lock:
            if self.review_event is not None:
                raise PortalError("A browser review is already open.")
            record = self.store.get(identifier)
            if record.status == "submitted" or not record.posting_url:
                raise PortalError("This application cannot be opened for form filling.")
            profile = load_profile(self.settings.profile_file, self.settings.jobagent_home)
            event = threading.Event()
            handoff = threading.Event()
            self.review_event, self.review_id = event, identifier
            self.handoff_event, self.handoff_approved = handoff, False

        def worker() -> None:
            try:
                def on_result(result):
                    self.store.set_status(identifier, result.status)
                    self.sync(identifier)

                def wait_for_handoff() -> bool:
                    handoff.wait()
                    with self.lock:
                        return self.handoff_approved

                run_headed(record.posting_url, profile,
                           self.settings.jobagent_home / record.resume_path,
                           self.settings, on_result, wait_for_handoff=wait_for_handoff,
                           wait_for_review=event.wait)
            except Exception:
                self.store.set_status(identifier, "blocked")
                self.sync(identifier)
            finally:
                with self.lock:
                    self.review_event = None
                    self.handoff_event = None
                    self.handoff_approved = False
                    self.review_id = None

        threading.Thread(target=worker, daemon=True, name="jobagent-browser-review").start()

    def close_review(self) -> None:
        with self.lock:
            if self.review_event:
                self.review_event.set()
            if self.handoff_event:
                self.handoff_event.set()

    def enable_manual_actions(self) -> None:
        with self.lock:
            if (self.handoff_event is None or self.review_event is None
                    or self.review_event.is_set()):
                raise PortalError("Open a review browser before enabling manual actions.")
            self.handoff_approved = True
            self.handoff_event.set()

    def confirm_submitted(self, identifier: str) -> None:
        with self.lock:
            if self.review_id == identifier:
                raise PortalError("Close the review browser before recording a submission.")
        self.store.set_status(identifier, "submitted", confirmed=True)
        self.sync(identifier)

    def records(self):
        with self.store.transaction() as db:
            return [self.store._get(db, row[0]) for row in db.execute(
                "SELECT id FROM applications ORDER BY updated_at DESC LIMIT 50")]


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(state: PortalState) -> str:
    try:
        selected = state.selected_sheet()
        sheet_label = ("Selected spreadsheet: …" + selected[-8:]) if selected else "No spreadsheet selected"
        pending = state.pending_count()
    except PortalError:
        sheet_label, pending = "Spreadsheet selection needs repair", 0
    profile = {}
    if state.settings.profile_file.is_file():
        try:
            from jobagent.profile import load_profile
            loaded = load_profile(state.settings.profile_file, state.settings.jobagent_home)
            profile = {key: getattr(loaded, key).get_secret_value()
                       for key in ApplicantProfile.model_fields if key != "answers"
                       and getattr(loaded, key) is not None}
            profile["answers_text"] = "\n".join(
                f"{key} = {value.get_secret_value()}" for key, value in loaded.answers.items())
        except Exception:
            pass
    token = _e(state.token)
    inputs = "".join(f'<label>{_e(key.replace("_", " ").title())}<input name="{_e(key)}" '
                     f'value="{_e(profile.get(key, ""))}" autocomplete="on"></label>'
                     for key in ApplicantProfile.model_fields if key != "answers")
    inputs += (f'<label>Extra answers (one per line: Question = Answer)'
               f'<textarea name="answers_text" rows="5">{_e(profile.get("answers_text", ""))}</textarea></label>')
    folders = "".join(f"<li>{_e(folder)}</li>" for folder in state.folders())
    try:
        context_notes = state.context_notes()
        note_rows = "".join(
            f'<li><strong>{_e(note.title)}</strong><p>{_e(note.body)}</p>'
            f'<form method="post" action="/context-delete"><input type="hidden" name="token" value="{token}">'
            f'<input type="hidden" name="id" value="{_e(note.id)}"><button>Delete note</button></form></li>'
            for note in context_notes)
    except Exception:
        note_rows = "<li>Saved context is temporarily unavailable.</li>"
    suggestions = "".join(
        f'<li><strong>{_e(hit.note.title)}</strong><p>{_e(hit.note.body)}</p></li>'
        for hit in state.context_results)
    records = state.records()
    status_counts = {status: sum(record.status == status for record in records)
                     for status in ("resume_ready", "review_needed", "blocked", "submitted")}
    summary = "".join(
        f'<div class="stat"><strong>{count}</strong><span>{_e(label)}</span></div>'
        for label, count in (("Recent applications", len(records)),
                             ("Ready", status_counts["resume_ready"]),
                             ("Review", status_counts["review_needed"]),
                             ("Submitted", status_counts["submitted"]),
                             ("Sheet pending", pending)))
    rows = "".join(
        f'<tr><td>{_e(r.id)}</td><td>{_e(r.company)}</td><td>{_e(r.title)}</td><td>{_e(r.status)}</td>'
        f'<td>{_e(Path(r.resume_path).name)}</td><td>'
        f'<form method="post" action="/sync"><input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="id" value="{_e(r.id)}"><button>Sync sheet</button></form>'
        f'<form method="post" action="/review"><input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="id" value="{_e(r.id)}"><button>Open form</button></form>'
        f'<form method="post" action="/submitted"><input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="id" value="{_e(r.id)}"><button>Mark submitted</button></form></td></tr>'
        for r in records)
    handoff_form = ""
    if state.review_event is not None and not state.handoff_approved:
        handoff_form = (
            f'<form method="post" action="/handoff"><input type="hidden" name="token" value="{token}">'
            '<button>Enable manual actions</button></form>'
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Agent dashboard</title><style>body{{font:16px system-ui;max-width:1050px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f7f9fb}}
h1,h2{{color:#113454}}section{{background:white;padding:1.25rem;margin:1rem 0;border:1px solid #d9e2ea;border-radius:12px}}
nav{{display:flex;flex-wrap:wrap;gap:.7rem;margin:1rem 0}}nav a{{color:#145a85;background:white;padding:.6rem .8rem;border-radius:8px;text-decoration:none;border:1px solid #d9e2ea}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:.7rem}}.stat{{background:white;border:1px solid #d9e2ea;border-radius:10px;padding:.85rem;display:flex;flex-direction:column}}.stat strong{{font-size:1.6rem;color:#113454}}.stat span{{color:#536675}}
label{{display:block;margin:.6rem 0}}input,textarea{{display:block;width:100%;box-sizing:border-box;padding:.6rem;border:1px solid #9cacba;border-radius:6px;font:inherit}}
button{{padding:.55rem .9rem;margin:.4rem .3rem .1rem 0;border:0;border-radius:6px;background:#176699;color:white;cursor:pointer}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:.5rem;border-bottom:1px solid #ddd;text-align:left}}td form{{display:inline}}
.notice{{background:#e8f3fb;padding:.8rem;border-radius:6px}}small{{color:#536675}}</style></head><body>
<h1>Job Agent dashboard</h1><p>Local application workspace. Your profile and resumes stay on this computer.</p>
<nav aria-label="Dashboard sections"><a href="#profile">Profile</a><a href="#context">Saved context</a><a href="#resumes">Resumes</a><a href="#prepare">Prepare</a><a href="#tracker">Tracker</a><a href="#applications">Applications</a></nav>
<div class="stats" aria-label="Application summary">{summary}</div>
<p class="notice">{_e(state.notice)}</p>
<section id="profile"><h2>1. Your facts</h2><p>Enter only facts you can verify. Unknown application questions remain for review.</p>
<form method="post" action="/profile"><input type="hidden" name="token" value="{token}">{inputs}<button>Save profile</button></form></section>
<section id="context"><h2>Saved context</h2><p>Save verified projects, experience, and answers once. Local vector search helps you find relevant notes for later applications. Review each note before using it.</p>
<form method="post" action="/context"><input type="hidden" name="token" value="{token}">
<label>Short title<input name="title" required maxlength="120"></label>
<label>Verified context<textarea name="body" rows="4" required maxlength="5000"></textarea></label><button>Save context</button></form>
<form method="post" action="/context-search"><input type="hidden" name="token" value="{token}">
<label>Find context for a role or question<textarea name="query" rows="2" required maxlength="5000">{_e(state.context_query)}</textarea></label><button>Find relevant notes</button></form>
<h3>Relevant notes</h3><ul>{suggestions or '<li>Search to see matching saved notes.</li>'}</ul>
<details><summary>All saved notes</summary><ul>{note_rows or '<li>No notes saved yet.</li>'}</ul></details></section>
<section id="resumes"><h2>2. Resume folders</h2><ul>{folders}</ul><form method="post" action="/folder"><input type="hidden" name="token" value="{token}">
<label>Folder path<input name="path" required placeholder="/home/you/Resumes"></label><button>Add folder</button></form>
<small>PDF resumes can be attached automatically. Other supported documents can be indexed for future preparation.</small></section>
<section id="prepare"><h2>3. Prepare an application</h2><form method="post" action="/prepare"><input type="hidden" name="token" value="{token}">
<label>Company<input name="company" required></label><label>Role title<input name="title" required></label>
<label>Application URL<input name="url" type="url" placeholder="https://..."></label>
<label>Job description<textarea name="description" rows="10" required maxlength="30000"></textarea></label>
<button>Choose resume and save application</button></form></section>
<section id="tracker"><h2>4. Tracker</h2><p>{_e(sheet_label)} · {pending} application(s) pending for this spreadsheet.</p>
<form method="post" action="/sheet"><input type="hidden" name="token" value="{token}">
<label>Google Sheets spreadsheet ID or editor URL<input name="sheet" required autocomplete="off"></label>
<button>Choose spreadsheet</button></form>
<p><small>The tracker writes the Applications worksheet. Share the spreadsheet with your configured service account as an editor.</small></p>
<form method="post" action="/sync-pending"><input type="hidden" name="token" value="{token}"><button>Sync pending applications</button></form>
<form method="post" action="/instruction"><input type="hidden" name="token" value="{token}">
<label>Tracker instruction<input name="instruction" maxlength="500" placeholder="sync all"></label><button>Run instruction</button></form>
<small>Supported: use spreadsheet &lt;ID or URL&gt;; sync all; sync app-...; show pending.</small></section>
<section id="applications"><h2>Applications</h2><p>Open form fills supported fields and leaves the browser open for review. Click Mark submitted only after you have sent the application.</p>
<table><thead><tr><th>ID</th><th>Company</th><th>Role</th><th>Status</th><th>Resume copy</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table>
<p>After automatic filling, enable manual actions when you are ready to complete the form yourself.</p>{handoff_form}
<form method="post" action="/close"><input type="hidden" name="token" value="{token}"><button>Close review browser</button></form></section>
</body></html>"""


def serve(settings: Settings, port: int = 0, *, open_browser: bool = False) -> None:
    secure_home(settings.jobagent_home, force=True)
    state = PortalState(settings)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _trusted_request(self, *, require_origin: bool = False) -> bool:
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return False
            supplied_origin = self.headers.get("Origin")
            if require_origin and supplied_origin != origin:
                return False
            if supplied_origin is not None and supplied_origin != origin:
                return False
            return self.path.startswith("/") and not self.path.startswith("//")

        def _headers(self, code: int, kind: str):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()

        def do_GET(self):
            parsed = urlsplit(self.path)
            if (not self._trusted_request() or parsed.path != "/"
                    or parse_qs(parsed.query).get("token") != [state.token]):
                self._headers(403, "text/plain; charset=utf-8")
                return
            self._headers(200, "text/html; charset=utf-8")
            self.wfile.write(_page(state).encode("utf-8"))

        def do_POST(self):
            if not self._trusted_request(require_origin=True):
                self._headers(403, "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 80_000:
                    raise PortalError("Request too large.")
                raw = self.rfile.read(length).decode("utf-8")
                form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
                if form.get("token") != state.token:
                    self._headers(403, "text/plain; charset=utf-8")
                    return
                path = urlsplit(self.path).path
                if path == "/profile":
                    state.save_profile(form)
                    state.notice = "Profile saved locally."
                elif path == "/folder":
                    state.add_folder(form.get("path", ""))
                    state.notice = "Resume folder added."
                elif path == "/context":
                    state.add_context_note(form.get("title", ""), form.get("body", ""))
                    state.notice = "Verified context saved locally."
                elif path == "/context-search":
                    results = state.search_context_notes(form.get("query", ""))
                    state.notice = f"Found {len(results)} relevant saved note(s)."
                elif path == "/context-delete":
                    state.delete_context_note(form.get("id", ""))
                    state.notice = "Context note deleted."
                elif path == "/sheet":
                    state.select_sheet(form.get("sheet", ""))
                    state.notice = "Spreadsheet selected. Existing applications are pending for this spreadsheet."
                elif path == "/sync-pending":
                    done, failed = state.sync_pending()
                    state.notice = f"Synced {done} pending application(s); {failed} still pending."
                elif path == "/instruction":
                    state.notice = state.instruction(form.get("instruction", ""))
                elif path == "/prepare":
                    identifier, method = state.prepare(
                        company=form.get("company", ""), title=form.get("title", ""),
                        url=form.get("url", ""), description=form.get("description", ""))
                    synced = state.sync(identifier)
                    state.notice = (f"Application {identifier} prepared with {method} matching. "
                                    + ("Sheet synced." if synced else "Sheet sync pending."))
                elif path == "/review":
                    state.start_review(form.get("id", ""))
                    state.notice = "Application browser opening for review."
                elif path == "/close":
                    state.close_review()
                    state.notice = "Review browser closing."
                elif path == "/handoff":
                    state.enable_manual_actions()
                    state.notice = "Manual browser actions enabled for your review."
                elif path == "/sync":
                    state.notice = "Sheet synced." if state.sync(form.get("id", "")) else "Sheet sync pending."
                elif path == "/submitted":
                    state.confirm_submitted(form.get("id", ""))
                    state.notice = "Submission recorded; sheet sync attempted."
                else:
                    raise PortalError("Unknown action.")
            except (PortalError, TrackerError) as exc:
                state.notice = str(exc)
            except Exception as exc:
                from jobagent.context_memory import ContextMemoryError
                if isinstance(exc, ContextMemoryError):
                    state.notice = str(exc)
                else:
                    state.notice = "Action failed; local files were kept."
            self.send_response(303)
            self.send_header("Location", "/?token=" + state.token)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/?token={state.token}"
        print(f"Job Agent dashboard: {url}")
        print("Press Ctrl+C to stop the portal.")
        if open_browser:
            webbrowser.open(url, new=2)
        try:
            server.serve_forever()
        finally:
            state.close_review()
