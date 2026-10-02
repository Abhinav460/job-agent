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


def _entry(eid, *scores):
    return {"entry_id": eid, "bullets": [{"bullet_id": f"{eid}-{i}", "score": sc} for i, sc in enumerate(scores)]}


def test_trim_never_goes_below_two_bullets_or_drops_an_entry():
    selection = {
        "experience": [_entry("job", 0.9, 0.1, 0.5, 0.2)],
        "projects": [_entry("proj", 0.05, 0.3)],  # already at the floor: untouchable
        "skills": [],
    }
    assert trim(selection)  # drops job's 0.1 (proj's 0.05 is protected by the floor)
    assert trim(selection)  # drops job's 0.2
    assert [b["score"] for b in selection["experience"][0]["bullets"]] == [0.9, 0.5]
    assert not trim(selection)
    assert len(selection["projects"]) == 1 and len(selection["projects"][0]["bullets"]) == 2


def test_check_traceable_rejects_tampered_selection():
    from jobagent.tailor import UntraceableContent, check_traceable

    good = WriterOutput(
        experience=[EntryChoice(entry_id="example-corp", bullets=[BulletChoice(bullet_id="ec-api", text="x")])],
        projects=[],
        skills=[SkillLine(category="Languages", items=["Python"])],
    )
    selection, _ = run(good)
    check_traceable(selection, BANK, JOB)  # sanitized output passes

    tampered = [
        lambda s: s["experience"][0]["bullets"][0].update(text="Led 12 engineers at a Fortune 500 company"),
        lambda s: s["experience"][0]["bullets"][0].update(bullet_id="invented"),
        lambda s: s["experience"].append({"entry_id": "fake-job", "bullets": []}),
        lambda s: s["skills"].append(["Languages", ["Rust"]]),
    ]
    for tamper in tampered:
        broken, _ = run(good)
        tamper(broken)
        with pytest.raises(UntraceableContent):
            check_traceable(broken, BANK, JOB)


def test_bullets_pasted_with_justify_are_unwrapped(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        "experience:\n"
        "  - id: x\n    org: O\n    title: T\n    dates: D\n"
        "    bullets:\n      - {id: b1, text: '\\justify{Built a thing}', domains: [SWE]}\n"
    )
    assert load_bullets(f).ref("b1").bullet.text == "Built a thing"


def test_bank_rejects_duplicate_ids(tmp_path):
    bad = tmp_path / "b.yaml"
    bad.write_text(
        "experience:\n"
        "  - {id: x, org: O, title: T, dates: D, bullets: [{id: b1, text: t, domains: [IT]}]}\n"
        "  - {id: y, org: O, title: T, dates: D, bullets: [{id: b1, text: t, domains: [IT]}]}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_bullets(bad)
