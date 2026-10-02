"""Runs the whole pipeline with fake LLMs, a fake index and a fake compiler."""

import shutil
from pathlib import Path

import pytest

from jobagent import graph
from jobagent.bank import ScoredBullet
from jobagent.bullets import load_bullets
from jobagent.config import Settings
from jobagent.job import ParsedJob
from jobagent.tailor import BulletChoice, EntryChoice, SkillLine, WriterOutput

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BANK = load_bullets(EXAMPLES / "bullets.yaml")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBAGENT_HOME", str(tmp_path / "home"))
    s = Settings()
    s.jobagent_home.mkdir()
    shutil.copy(EXAMPLES / "resume.tex", s.resume_tex)
    return s


class Fakes:
    def __init__(self, page_counts=(1,)):
        self.calls = {"parse": 0, "compile": 0}
        self.page_counts = list(page_counts)
        self.fail_compile = False

    def parse(self, text):
        self.calls["parse"] += 1
        return ParsedJob(title="Data Engineer", company="Acme/../Inc", summary="pipelines", domains=["data"])

    def retrieve(self, query, domains, limit):
        return [ScoredBullet(r.bullet.id, 1.0 - i / 20) for i, r in enumerate(BANK.refs())]

    def write(self, job, bank, scores):
        return WriterOutput(
            experience=[
                EntryChoice(entry_id=e.id, bullets=[BulletChoice(bullet_id=b.id, text=b.text) for b in e.bullets])
                for e in bank.experience
            ],
            projects=[
                EntryChoice(entry_id=p.id, bullets=[BulletChoice(bullet_id=b.id, text=b.text) for b in p.bullets])
                for p in bank.projects
            ],
            skills=[SkillLine(category=c, items=items) for c, items in bank.skills.items()],
        )

    def compile(self, tex_path):
        self.calls["compile"] += 1
        if self.fail_compile:
            raise RuntimeError("simulated crash")
        pdf = tex_path.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-fake")
        return pdf

    def overflow(self, pdf, titles):
        self.overflow_titles = titles
        return ["Projects", "Technical Skills"]

    def pages(self, pdf):
        return self.page_counts.pop(0) if len(self.page_counts) > 1 else self.page_counts[0]


def make_app(settings, fakes, checkpointer=None):
    deps = graph.Deps(
        settings=settings,
        bank=BANK,
        fetch=lambda url: "posting text",
        parse=fakes.parse,
        retrieve=fakes.retrieve,
        write=fakes.write,
        compile=fakes.compile,
        pages=fakes.pages,
        overflow=fakes.overflow,
    )
    return graph.build_graph(deps, checkpointer)


def test_pipeline_writes_only_inside_markers(settings):
    fakes = Fakes()
    state = make_app(settings, fakes).invoke({"source_text": "posting text"})
    assert state["pages"] == 1 and not state.get("error")
    out_dir = Path(state["output_dir"])
    assert out_dir.parent == settings.output_dir  # slug neutralized "../"
    tex = Path(state["tex_path"]).read_text()
    original = settings.resume_tex.read_text()
    before = original.split("\\section{Experience}")[0]  # header, contact line, education
    after = original.rsplit("% END:SKILLS", 1)[1]
    assert tex.startswith(before) and tex.endswith(after)
    assert r"\resumeItem{\justify{Built a FastAPI service" in tex
    assert "Placeholder" not in tex


def test_over_one_page_trims_until_it_fits(settings):
    # The example bank has exactly one bullet above the 2-per-entry floor.
    fakes = Fakes(page_counts=(2, 1))
    state = make_app(settings, fakes).invoke({"source_text": "posting text"})
    assert state["pages"] == 1 and state["trims"] == 1 and fakes.calls["compile"] == 2


def test_stops_and_reports_sections_when_nothing_left_to_trim(settings):
    fakes = Fakes(page_counts=(2,))
    state = make_app(settings, fakes).invoke({"source_text": "posting text"})
    assert "over the page: Projects, Technical Skills" in state["error"]
    assert state["overflow_sections"] == ["Projects", "Technical Skills"]
    assert fakes.overflow_titles == ["Education", "Experience", "Projects", "Technical Skills"]
    # Every job and project kept at least its last 2 bullets (or all it had).
    for section in ("experience", "projects"):
        for entry in state["selection"][section]:
            original = len(BANK.entry(entry["entry_id"]).bullets)
            assert len(entry["bullets"]) >= min(2, original)
    assert len(state["selection"]["experience"]) == len(BANK.experience)


def test_generated_bullets_keep_template_justify_wrapper(settings):
    state = make_app(settings, Fakes()).invoke({"source_text": "posting text"})
    tex = Path(state["tex_path"]).read_text()
    body = tex.split("% BEGIN:EXPERIENCE")[-1].split("% END:EXPERIENCE")[0]
    assert body.count(r"\resumeItem{\justify{") == body.count(r"\resumeItem{") > 0


def test_checkpoint_survives_restart(settings):
    fakes = Fakes()
    fakes.fail_compile = True
    config = {"configurable": {"thread_id": graph.thread_id(None, "posting text")}}
    with pytest.raises(RuntimeError):
        make_app(settings, fakes, graph.open_checkpointer(settings)).invoke({"source_text": "posting text"}, config)
    assert settings.checkpoint_db.stat().st_mode & 0o777 == 0o600

    fakes.fail_compile = False  # "restart": new app, new connection, same DB
    app = make_app(settings, fakes, graph.open_checkpointer(settings))
    assert app.get_state(config).next == ("compile_resume",)
    state = app.invoke(None, config)
    assert state["pages"] == 1
    assert fakes.calls["parse"] == 1  # parse_job was not re-run
