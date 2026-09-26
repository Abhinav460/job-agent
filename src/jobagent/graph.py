"""The resume pipeline as a LangGraph graph with a SQLite checkpointer.

parse_job -> retrieve_bullets -> tailor_resume -> compile_resume -> verify_one_page
                                                       ^                 |
                                                       +-- trim_resume <-+  (over one page)

State holds ids, scores and paths, not whole documents. The checkpoint
database lives in $JOBAGENT_HOME/checkpoints/ (0600) because the state still
contains the posting and the chosen bullet text.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from jobagent import latex, safepaths, tailor
from jobagent.bank import ScoredBullet
from jobagent.bullets import BulletBank
from jobagent.config import Settings
from jobagent.job import JobPosting, ParsedJob

log = logging.getLogger(__name__)


class ResumeState(TypedDict, total=False):
    source_url: str | None
    source_text: str | None
    job: dict[str, Any]
    scores: dict[str, float]
    selection: dict[str, Any]
    rephrase_rejections: int
    output_dir: str
    tex_path: str
    pdf_path: str
    pages: int
    trims: int
    error: str | None


@dataclass
class Deps:
    """Everything the nodes touch. Tests swap these for fakes."""

    settings: Settings
    bank: BulletBank
    fetch: Callable[[str], str]
    parse: Callable[[str], ParsedJob]
    retrieve: Callable[[str, list[str], int], list[ScoredBullet]]
    write: Callable[[JobPosting, BulletBank, dict[str, float]], tailor.WriterOutput]
    compile: Callable[[Path], Path]
    pages: Callable[[Path], int]


def build_graph(deps: Deps, checkpointer: Any = None) -> Any:
    s = deps.settings
    read_roots = [s.jobagent_home]
    write_roots = [s.output_dir]

    def parse_job(state: ResumeState) -> ResumeState:
        text = state.get("source_text") or deps.fetch(state["source_url"])
        parsed = deps.parse(text)
        job = JobPosting(**parsed.model_dump(), url=state.get("source_url"), description=text)
        out = s.output_dir / f"{safepaths.slug(job.company)}_{safepaths.slug(job.title)}"
        safepaths.ensure_private_dir(out, write_roots)
        log.info("parsed posting")
        return {"job": job.model_dump(), "output_dir": str(out), "trims": 0}

    def retrieve_bullets(state: ResumeState) -> ResumeState:
        job = JobPosting(**state["job"])
        hits = deps.retrieve(job.search_text(), list(job.domains), s.resume_retrieval_top_k)
        known = {r.bullet.id for r in deps.bank.refs()}
        scores = {h.bullet_id: h.score for h in hits if h.bullet_id in known}
        log.info("retrieved %d candidate bullets", len(scores))
        return {"scores": scores}

    def tailor_resume(state: ResumeState) -> ResumeState:
        job = JobPosting(**state["job"])
        output = deps.write(job, deps.bank, state["scores"])
        selection, rejected = tailor.sanitize(
            output,
            deps.bank,
            job,
            state["scores"],
            max_bullets=s.resume_max_bullets_per_entry,
            max_projects=s.resume_max_projects,
            allow_rephrase=s.resume_allow_rephrase,
        )
        log.info("selected bullets; %d rephrasing(s) reverted to source text", rejected)
        return {"selection": selection, "rephrase_rejections": rejected}

    def compile_resume(state: ResumeState) -> ResumeState:
        tex = safepaths.read_text(s.resume_tex, read_roots)
        sel = state["selection"]
        experience = [(deps.bank.entry(e["entry_id"]), [b["text"] for b in e["bullets"]]) for e in sel["experience"]]
        projects = [(deps.bank.entry(e["entry_id"]), [b["text"] for b in e["bullets"]]) for e in sel["projects"]]
        sections = {
            "EXPERIENCE": lambda pad: latex.render_experience(experience, pad),
            "PROJECTS": lambda pad: latex.render_projects(projects, pad),
            "SKILLS": lambda pad: latex.render_skills([(c, i) for c, i in sel["skills"]], pad),
        }
        present = latex.markers_present(tex)
        if "EXPERIENCE" not in present:
            raise latex.LatexError("resume.tex needs a % BEGIN:EXPERIENCE / % END:EXPERIENCE pair")
        for name, render in sections.items():
            if name in present:
                content = render(latex.marker_indent(tex, name))
                latex.validate_generated(content)
                tex = latex.replace_marked(tex, name, content)
        latex.validate_document(tex)
        tex_path = safepaths.write_text(Path(state["output_dir"]) / "resume.tex", tex, write_roots)
        pdf = deps.compile(tex_path)
        return {"tex_path": str(tex_path), "pdf_path": str(pdf)}

    def verify_one_page(state: ResumeState) -> ResumeState:
        pages = deps.pages(Path(state["pdf_path"]))
        log.info("compiled resume: %d page(s)", pages)
        return {"pages": pages, "error": None}

    def trim_resume(state: ResumeState) -> ResumeState:
        selection = state["selection"]
        if not tailor.trim(selection):
            return {"error": "resume is over one page even with minimal content"}
        return {"selection": selection, "trims": state.get("trims", 0) + 1}

    def after_verify(state: ResumeState) -> str:
        if state["pages"] <= 1:
            return END
        if state.get("trims", 0) >= s.resume_max_trim_attempts:
            return "give_up"
        return "trim_resume"

    def give_up(state: ResumeState) -> ResumeState:
        return {"error": f"still {state['pages']} pages after {state.get('trims', 0)} trims"}

    graph = StateGraph(ResumeState)
    graph.add_node("parse_job", parse_job)
    graph.add_node("retrieve_bullets", retrieve_bullets)
    graph.add_node("tailor_resume", tailor_resume)
    graph.add_node("compile_resume", compile_resume)
    graph.add_node("verify_one_page", verify_one_page)
    graph.add_node("trim_resume", trim_resume)
    graph.add_node("give_up", give_up)
    graph.add_edge(START, "parse_job")
    graph.add_edge("parse_job", "retrieve_bullets")
    graph.add_edge("retrieve_bullets", "tailor_resume")
    graph.add_edge("tailor_resume", "compile_resume")
    graph.add_edge("compile_resume", "verify_one_page")
    graph.add_conditional_edges("verify_one_page", after_verify, ["trim_resume", "give_up", END])
    graph.add_conditional_edges(
        "trim_resume", lambda st: END if st.get("error") else "compile_resume", ["compile_resume", END]
    )
    graph.add_edge("give_up", END)
    return graph.compile(checkpointer=checkpointer)


def open_checkpointer(settings: Settings) -> SqliteSaver:
    db = settings.checkpoint_db
    safepaths.ensure_private_dir(db.parent, [settings.jobagent_home])
    if not db.exists():
        db.touch(mode=0o600)
    return SqliteSaver(sqlite3.connect(db, check_same_thread=False))


def thread_id(source_url: str | None, source_text: str | None) -> str:
    """Stable id per posting, so re-running the same posting resumes its checkpoint."""
    key = (source_url or source_text or "").strip()
    return "resume-" + hashlib.sha256(key.encode()).hexdigest()[:16]
