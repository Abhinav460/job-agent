"""Token protected loopback API for the optional Chrome side panel.

No document bytes are returned. The first authenticated extension origin is
bound for this server process; a web page can neither call nor read this API.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from jobagent.config import Settings
from jobagent.context_memory import ContextMemoryError
from jobagent.portal import PortalError, PortalState
from jobagent.profile import ApplicantProfile, load_profile
from jobagent.tracker import TrackerError

EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
MAX_BODY = 80_000


def _profile(state: PortalState) -> dict[str, str]:
    if not state.settings.profile_file.is_file():
        return {}
    try:
        profile = load_profile(state.settings.profile_file, state.settings.jobagent_home)
        values = {key: value.get_secret_value() for key in ApplicantProfile.model_fields
                  if key != "answers" and (value := getattr(profile, key)) is not None}
        values["answers_text"] = "\n".join(
            f"{label} = {value.get_secret_value()}" for label, value in profile.answers.items())
        return values
    except Exception:
        return {}


def _status(state: PortalState) -> dict[str, Any]:
    try:
        selected = state.selected_sheet()
        suffix, pending = (selected[-8:], state.pending_count()) if selected else (None, 0)
    except PortalError:
        suffix, pending = None, 0
    return {
        "applications": [{"id": record.id, "company": record.company,
                          "title": record.title, "status": record.status}
                         for record in state.records()],
        "pending": pending,
        "sheet_suffix": suffix,
        "folders": [str(path) for path in state.folders()],
        "profile": _profile(state),
        "notes": [{"id": note.id, "title": note.title} for note in state.context_notes()],
        "review_id": state.review_id,
        "manual_actions_enabled": state.handoff_approved,
    }


def _dispatch(state: PortalState, path: str, payload: dict[str, str]) -> dict[str, Any]:
    if path == "/api/profile":
        state.save_profile(payload)
        message = "Profile saved locally."
    elif path == "/api/folder":
        state.add_folder(payload.get("path", ""))
        message = "Resume folder added."
    elif path == "/api/prepare":
        identifier, method = state.prepare(
            company=payload.get("company", ""), title=payload.get("title", ""),
            url=payload.get("url", ""), description=payload.get("description", ""))
        synced = state.sync(identifier)
        message = (f"Application {identifier} prepared with {method} matching. "
                   + ("Sheet synced." if synced else "Sheet sync pending."))
    elif path == "/api/sheet":
        state.select_sheet(payload.get("sheet", ""))
        message = "Spreadsheet selected. Existing applications are pending for this spreadsheet."
    elif path == "/api/sync-pending":
        done, failed = state.sync_pending()
        message = f"Synced {done} pending application(s); {failed} still pending."
    elif path == "/api/sync":
        message = ("Sheet synced." if state.sync(payload.get("id", ""))
                   else "Sheet sync pending.")
    elif path == "/api/review":
        state.start_review(payload.get("id", ""))
        message = "Application browser opening for review."
    elif path == "/api/handoff":
        state.enable_manual_actions()
        message = "Manual browser actions enabled for your review."
    elif path == "/api/close":
        state.close_review()
        message = "Review browser closing."
    elif path == "/api/submitted":
        state.confirm_submitted(payload.get("id", ""))
        message = "Submission recorded; sheet sync attempted."
    elif path == "/api/context-add":
        state.add_context_note(payload.get("title", ""), payload.get("body", ""))
        message = "Context note saved locally."
    elif path == "/api/context-delete":
        state.delete_context_note(payload.get("id", ""))
        message = "Context note deleted."
    elif path == "/api/context-search":
        suggestions = state.search_context_notes(payload.get("query", ""))
        return {"message": f"Found {len(suggestions)} suggestion(s) for you to review.",
                "suggestions": [{"id": hit.note.id, "title": hit.note.title,
                                 "body": hit.note.body, "score": round(hit.score, 3)}
                                for hit in suggestions]}
    elif path == "/api/autofill":
        return _autofill(state, payload)
    elif path == "/api/applied":
        record = state.store.record_applied(
            source_url=payload.get("url", ""), company=payload.get("company", "")[:120] or "Unknown",
            title=payload.get("title", "")[:160] or "Unknown")
        synced = state.sync(record.id)
        message = "Application logged as submitted. " + ("Sheet synced." if synced else "Sheet sync pending.")
    else:
        raise PortalError("Unknown action.")
    return {"message": message}


def _autofill(state: PortalState, payload: dict[str, str]) -> dict[str, Any]:
    from jobagent import autofill

    try:
        fields = autofill.validate_fields(payload.get("fields", ""))
        complete = None
        if state.settings.groq_api_key is not None:
            complete = autofill.groq_complete(state.settings.groq_api_key.get_secret_value(),
                                              state.settings.groq_model)
        values, note = autofill.plan(fields, _profile(state), complete)
    except autofill.AutofillError as exc:
        raise PortalError(str(exc)) from None
    return {"values": values,
            "message": f"Filled {len(values)} of {len(fields)} field(s). Review before you apply. {note}".strip()}


def make_handler(state: PortalState) -> type[BaseHTTPRequestHandler]:
    """Build a handler for one process-local token and one bound extension."""
    bound_origin: str | None = None
    origin_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _origin(self) -> str | None:
            origin = self.headers.get("Origin", "")
            if (EXTENSION_ORIGIN.fullmatch(origin) is None
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"):
                return None
            return origin

        def _authenticated(self) -> str | None:
            nonlocal bound_origin
            origin = self._origin()
            supplied = self.headers.get("X-JobAgent-Token", "")
            if (origin is None or len(supplied) > 128
                    or not secrets.compare_digest(supplied, state.token)):
                return None
            with origin_lock:
                if bound_origin is None:
                    bound_origin = origin
                return origin if bound_origin == origin else None

        def _json(self, code: int, body: dict[str, Any], origin: str | None = None):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Vary", "Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            origin = self._origin()
            path = urlsplit(self.path)
            method = self.headers.get("Access-Control-Request-Method", "")
            requested = {value.strip().lower() for value in
                         self.headers.get("Access-Control-Request-Headers", "").split(",")}
            if (origin is None or path.query or path.path not in _PATHS
                    or method not in ("GET", "POST")
                    or requested - {"", "x-jobagent-token", "content-type"}):
                self._json(403, {"error": "Forbidden."})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "X-JobAgent-Token, Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def do_GET(self):
            origin = self._authenticated()
            path = urlsplit(self.path)
            if origin is None or path.path != "/api/status" or path.query:
                self._json(403, {"error": "Forbidden."})
                return
            try:
                self._json(200, _status(state), origin)
            except Exception:
                self._json(500, {"error": "Could not read local workspace."}, origin)

        def do_POST(self):
            origin = self._authenticated()
            path = urlsplit(self.path)
            if origin is None or path.path not in _POST_PATHS or path.query:
                self._json(403, {"error": "Forbidden."})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise PortalError("Send JSON data.")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise PortalError("Request too large or empty.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if (not isinstance(payload, dict) or len(payload) > 30
                        or any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in payload.items())):
                    raise PortalError("Invalid action data.")
                self._json(200, _dispatch(state, path.path, payload), origin)
            except (PortalError, TrackerError, ContextMemoryError) as exc:
                self._json(400, {"error": str(exc)}, origin)
            except (ValueError, UnicodeError):
                self._json(400, {"error": "Invalid action data."}, origin)
            except Exception:
                self._json(500, {"error": "Action failed; local files were kept."}, origin)

    return Handler


_POST_PATHS = frozenset({"/api/profile", "/api/folder", "/api/prepare", "/api/sheet",
                         "/api/sync-pending", "/api/sync", "/api/review",
                         "/api/handoff", "/api/close", "/api/submitted",
                         "/api/context-add", "/api/context-delete", "/api/context-search",
                         "/api/autofill", "/api/applied"})
_PATHS = _POST_PATHS | {"/api/status"}


def serve(settings: Settings, port: int = 0) -> None:
    from jobagent.private_storage import secure_home

    secure_home(settings.jobagent_home, force=True)
    state = PortalState(settings)
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler(state)) as server:
        url = f"http://127.0.0.1:{server.server_port}/?token={state.token}"
        print(f"Job Agent extension server: {url}")
        print("Paste this URL into the extension; press Ctrl+C to stop the server.")
        try:
            server.serve_forever()
        finally:
            state.close_review()
