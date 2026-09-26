from pathlib import Path

import pytest

from jobagent.bullets import load_bullets
from jobagent.job import JobPosting
from jobagent.tailor import BulletChoice, EntryChoice, SkillLine, WriterOutput, sanitize, trim

BANK = load_bullets(Path(__file__).resolve().parent.parent / "examples" / "bullets.yaml")
JOB = JobPosting(title="Data Engineer", company="Acme", summary="s", required_skills=["Airflow"], domains=["data"])
SCORES = {"ec-api": 0.9, "ec-dash": 0.8, "ec-ci": 0.3, "pr-model": 0.7, "pr-eval": 0.6, "pt-app": 0.2}


def run(output, allow_rephrase=True):
    return sanitize(output, BANK, JOB, SCORES, max_bullets=3, max_projects=1, allow_rephrase=allow_rephrase)


def test_invented_content_is_dropped():
    output = WriterOutput(
        experience=[
            EntryChoice(
                entry_id="example-corp",
                bullets=[
                    BulletChoice(bullet_id="ec-api", text=BANK.ref("ec-api").bullet.text),
                    BulletChoice(bullet_id="made-up", text="Led a team of 50 at Google"),
                    BulletChoice(bullet_id="fu-tickets", text="wrong entry"),  # belongs elsewhere
                ],
            ),
            EntryChoice(entry_id="fake-job", bullets=[BulletChoice(bullet_id="ec-ci", text="x")]),
        ],
        projects=[EntryChoice(entry_id="proj-recommender", bullets=[BulletChoice(bullet_id="pr-model", text="x")])],
        skills=[SkillLine(category="languages", items=["python", "Rust"]), SkillLine(category="Magic", items=["x"])],
    )
    selection, _ = run(output)
    ids = [b["bullet_id"] for e in selection["experience"] for b in e["bullets"]]
    assert ids[0] == "ec-api" and "made-up" not in ids
    # Every real job stays, even if the writer skipped it; unknown entries vanish.
    assert [e["entry_id"] for e in selection["experience"]] == ["example-corp", "fictional-university-helpdesk"]
    assert selection["skills"] == [["Languages", ["Python"]]]


def test_rephrasing_that_adds_facts_is_reverted():
    source = BANK.ref("ec-api").bullet.text
    padded = source.replace("FastAPI", "FastAPI and Airflow")
    output = WriterOutput(
        experience=[EntryChoice(entry_id="example-corp", bullets=[BulletChoice(bullet_id="ec-api", text=padded)])],
        projects=[],
        skills=[],
    )
    selection, rejected = run(output)
    assert selection["experience"][0]["bullets"][0]["text"] == source
    assert rejected == 1


def test_rephrasing_disabled_uses_source():
    source = BANK.ref("ec-api").bullet.text
    output = WriterOutput(
        experience=[EntryChoice(entry_id="example-corp", bullets=[BulletChoice(bullet_id="ec-api", text="Made an API")])],
        projects=[],
        skills=[],
    )
    selection, rejected = run(output, allow_rephrase=False)
    assert selection["experience"][0]["bullets"][0]["text"] == source and rejected == 0


def test_trim_drops_least_relevant_then_projects():
    selection = {
        "experience": [{"entry_id": "e", "bullets": [{"bullet_id": "a", "score": 0.9}, {"bullet_id": "b", "score": 0.1}]}],
        "projects": [{"entry_id": "p", "bullets": [{"bullet_id": "c", "score": 0.5}]}],
        "skills": [],
    }
    assert trim(selection)
    assert [b["bullet_id"] for b in selection["experience"][0]["bullets"]] == ["a"]
    assert trim(selection) and selection["projects"] == []
    assert not trim(selection)


def test_bank_rejects_duplicate_ids(tmp_path):
    bad = tmp_path / "b.yaml"
    bad.write_text(
        "experience:\n"
        "  - {id: x, org: O, title: T, dates: D, bullets: [{id: b1, text: t, domains: [IT]}]}\n"
        "  - {id: y, org: O, title: T, dates: D, bullets: [{id: b1, text: t, domains: [IT]}]}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_bullets(bad)
