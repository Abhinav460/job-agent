"""Side-panel API with fictional data and no live external services."""

from __future__ import annotations

import io
import hashlib
import json
import tempfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

from jobagent.config import Settings
from jobagent.extension_server import make_handler
from jobagent.portal import PortalState
from jobagent.resume_catalog import ResumeCatalog, ResumeDocument


ORIGIN_A = "chrome-extension://" + "a" * 32
ORIGIN_B = "chrome-extension://" + "b" * 32


def _request(handler_type, method, path, *, token=None, origin=ORIGIN_A,
             host="127.0.0.1:8765", data=None, headers=None):
    handler = handler_type.__new__(handler_type)
    handler.path = path
    handler.server = SimpleNamespace(server_port=8765)
    handler.headers = Message()
    handler.headers["Host"] = host
    if origin is not None:
        handler.headers["Origin"] = origin
    if token is not None:
        handler.headers["X-JobAgent-Token"] = token
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
    else:
        body = b""
    for key, value in (headers or {}).items():
        handler.headers[key] = value
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    response = {"headers": {}}
    handler.send_response = lambda status, *_args: response.update(status=status)
    handler.send_header = lambda key, value: response["headers"].update({key: value})
    handler.end_headers = lambda: None
    getattr(handler, "do_" + method)()
    response["body"] = handler.wfile.getvalue()
    if response["body"]:
        response["json"] = json.loads(response["body"])
    return response


def test_extension_origin_token_and_preflight_are_required():
    with tempfile.TemporaryDirectory() as temporary:
        state = PortalState(Settings(_env_file=None, jobagent_home=Path(temporary) / "private"))
        handler = make_handler(state)
        preflight = _request(handler, "OPTIONS", "/api/status", headers={
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-jobagent-token"})
        assert preflight["status"] == 204
        assert preflight["headers"]["Access-Control-Allow-Origin"] == ORIGIN_A
        assert _request(handler, "OPTIONS", "/api/status", origin="https://job.example",
                        headers={"Access-Control-Request-Method": "GET"})["status"] == 403

        for kwargs in ({"token": None}, {"token": "wrong"},
                       {"token": state.token, "origin": "https://job.example"},
                       {"token": state.token, "host": "attacker.example:8765"},
                       {"token": state.token, "origin": None}):
            denied = _request(handler, "GET", "/api/status", **kwargs)
            assert denied["status"] == 403
            assert "Access-Control-Allow-Origin" not in denied["headers"]
        allowed = _request(handler, "GET", "/api/status", token=state.token)
        assert allowed["status"] == 200
        assert allowed["headers"]["Cache-Control"] == "no-store"
        assert allowed["headers"]["Access-Control-Allow-Origin"] == ORIGIN_A
        assert _request(handler, "GET", "/api/status", token=state.token,
                        origin=ORIGIN_B)["status"] == 403
        assert _request(handler, "GET", "/api/status?token=" + state.token,
                        token=state.token)["status"] == 403


def test_panel_actions_save_private_facts_and_context_without_auto_submission():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "private"
        folder = root / "resumes"
        folder.mkdir()
        state = PortalState(Settings(_env_file=None, jobagent_home=home))
        handler = make_handler(state)

        def post(path, data):
            return _request(handler, "POST", path, token=state.token, data=data)

        assert post("/api/profile", {"first_name": "Fake", "last_name": "Applicant",
                                     "email": "fake@example.invalid"})["status"] == 200
        assert post("/api/folder", {"path": str(folder)})["status"] == 200
        assert post("/api/context-add", {"title": "Data work",
                                         "body": "Built Python data pipelines"})["status"] == 200
        status = _request(handler, "GET", "/api/status", token=state.token)["json"]
        assert status["profile"]["email"] == "fake@example.invalid"
        assert status["folders"] == [str(folder)]
        assert status["notes"][0]["title"] == "Data work"
        note_id = status["notes"][0]["id"]
        found = post("/api/context-search", {"query": "Python pipelines"})
        assert found["status"] == 200
        assert found["json"]["suggestions"][0]["body"] == "Built Python data pipelines"
        assert post("/api/context-delete", {"id": note_id})["status"] == 200
        assert _request(handler, "GET", "/api/status", token=state.token)["json"]["notes"] == []

        pdf = home / "output" / "resume.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"fictional PDF bytes")
        record = state.store.save_resume(source_url="https://example.org/jobs/123",
                                         source_text=None, company="Example", title="Engineer", pdf=pdf)
        assert post("/api/sync", {"id": record.id})["status"] == 200
        assert post("/api/submitted", {"id": record.id})["status"] == 200
        assert state.store.get(record.id).status == "submitted"
        assert post("/api/submitted", {"id": "wrong"})["status"] in (400, 500)


def test_rejects_bad_json_and_does_not_echo_private_values():
    with tempfile.TemporaryDirectory() as temporary:
        state = PortalState(Settings(_env_file=None, jobagent_home=Path(temporary) / "private"))
        handler = make_handler(state)
        private = "PRIVATE_MARKER_FOR_TEST"
        rejected = _request(handler, "POST", "/api/profile", token=state.token,
                            data={"first_name": {"unexpected": private}})
        assert rejected["status"] == 400
        assert private.encode() not in rejected["body"]
        assert _request(handler, "POST", "/api/missing", token=state.token,
                        data={})["status"] == 403


def test_extension_resume_preview_and_explicit_selection(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        folder = root / "resumes"
        folder.mkdir()
        source = folder / "fictional.pdf"
        source.write_bytes(b"fictional PDF bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        document = ResumeDocument(
            "resume-" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:24],
            source, "ready", "Python SQL pipelines", digest)
        monkeypatch.setattr("jobagent.resume_catalog.index_directory",
                            lambda path: ResumeCatalog(path, (document,)))
        state = PortalState(Settings(_env_file=None, jobagent_home=root / "private"))
        state.add_folder(str(folder))
        handler = make_handler(state)

        def post(path, data):
            return _request(handler, "POST", path, token=state.token, data=data)

        denied = post("/api/prepare", {"resume_id": document.id, "digest": digest})
        assert denied["status"] == 400
        preview = post("/api/resume-preview", {
            "company": "Example", "title": "Python Engineer", "url": "",
            "description": "Python SQL pipelines"})
        assert preview["status"] == 200
        assert state.records() == []
        assert not (root / "private" / "output").exists()
        choice = preview["json"]["choices"][0]
        assert choice["name"] == "fictional.pdf"
        assert choice["digest"] == digest
        preview_id = preview["json"]["preview_id"]
        assert isinstance(preview_id, str) and len(preview_id) >= 32
        assert "Python SQL pipelines" not in preview["body"].decode()
        assert str(folder) not in preview["body"].decode()
        denied = post("/api/prepare", {"preview_id": preview_id,
                                       "resume_id": document.id, "digest": "0" * 64})
        assert denied["status"] == 400
        prepared = post("/api/prepare", {"preview_id": preview_id,
                                         "resume_id": choice["id"], "digest": choice["digest"]})
        assert prepared["status"] == 200
        assert len(state.records()) == 1
        assert post("/api/prepare", {"preview_id": preview_id,
                                      "resume_id": choice["id"], "digest": choice["digest"]})["status"] == 400
