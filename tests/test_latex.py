from pathlib import Path

import pytest

from jobagent import latex

EXAMPLE_TEX = (Path(__file__).resolve().parent.parent / "examples" / "resume.tex").read_text()


def test_brace_balance():
    assert latex.brace_problems(r"\textbf{a} \{ 50\% {b}") == []
    assert latex.brace_problems(r"\textbf{a") == ["1 unclosed opening brace(s)"]
    assert "unmatched closing" in latex.brace_problems("a}")[0]
    assert latex.brace_problems("% {unclosed in a comment") == []


@pytest.mark.parametrize(
    "tex, ok",
    [
        (r"\resumeItem{Built \textbf{x} with 5\%}", True),
        (r"\resumeItem {spaced}", True),
        (r"\resumeItem{one}{two}", False),
        (r"\resumeItem{one}}", False),
        (r"\resumeItem no brace", False),
        (r"\resumeItem{never closed", False),
        (r"\resumeItemListStart \resumeItemListEnd", True),
    ],
)
def test_resume_item_arguments(tex, ok):
    assert (latex.resume_item_problems(tex) == []) is ok


@pytest.mark.parametrize("cmd", [r"\input{/etc/passwd}", r"\immediate\write18{ls}", r"\def\x{y}", r"\usepackage{x}"])
def test_generated_content_rejects_dangerous_commands(cmd):
    with pytest.raises(latex.LatexError, match="forbidden"):
        latex.validate_generated(rf"\resumeItem{{ok}} {cmd}")


def test_replace_marked_only_touches_inside():
    tex = "head\n  % BEGIN:SKILLS\n  old\n  % END:SKILLS\ntail\n"
    out = latex.replace_marked(tex, "SKILLS", "  new")
    assert out == "head\n  % BEGIN:SKILLS\n  new\n  % END:SKILLS\ntail\n"
    assert latex.marker_indent(tex, "SKILLS") == "  "


def test_replace_marked_requires_exactly_one_pair():
    with pytest.raises(latex.LatexError):
        latex.replace_marked("no markers", "EXPERIENCE", "x")
    twice = "% BEGIN:SKILLS\na\n% END:SKILLS\n% BEGIN:SKILLS\nb\n% END:SKILLS\n"
    assert "SKILLS" not in latex.markers_present(twice)


def test_example_resume_is_valid():
    assert latex.markers_present(EXAMPLE_TEX) == {"EXPERIENCE", "PROJECTS", "SKILLS"}
    latex.validate_document(EXAMPLE_TEX)


SOURCE = r"Built a FastAPI service that validated 40k daily uploads, cutting bad records by 35\%"


@pytest.mark.parametrize(
    "rewrite, problem",
    [
        (r"Developed a FastAPI service validating 40k daily uploads, reducing bad records by 35\%", None),
        (r"Built a FastAPI service that validated 400k daily uploads, cutting bad records by 35\%", "number"),
        (r"Built a FastAPI and Kubernetes service validating 40k uploads, cutting bad records 35\%", "skill"),
        (r"Built a \href{x}{FastAPI} service that validated 40k uploads, cutting bad records by 35\%", "LaTeX"),
        (r"Built a FastAPI service {that validated 40k", "braces"),
        (SOURCE + " while also leading a team of engineers across several time zones daily", "longer"),
    ],
)
def test_rephrase_guard(rewrite, problem):
    result = latex.rephrase_problem(SOURCE, rewrite, ["FastAPI", "Kubernetes", "Python"])
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem.lower() in result.lower()
