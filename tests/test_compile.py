"""Real pdflatex/pdfinfo run on the fake example. Skipped when TeX Live is missing."""

import shutil
from pathlib import Path

import pytest

from jobagent.compile import CompileError, compile_tex, page_count

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "resume.tex"

pytestmark = pytest.mark.skipif(
    not (shutil.which("pdflatex") and shutil.which("pdfinfo")), reason="pdflatex/pdfinfo not installed"
)


def _latex_errors(log: Path) -> str:
    """'!' error lines plus the next few lines of context from a pdflatex log."""
    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    picked = []
    for i, line in enumerate(lines):
        if line.startswith(("!", "l.")) or "not found" in line:
            picked += lines[i : i + 4]
    return "\n".join(picked) or "\n".join(lines[-20:])


def test_example_compiles_to_one_page(tmp_path):
    tex = tmp_path / "resume.tex"
    shutil.copy(EXAMPLE, tex)
    try:
        pdf = compile_tex(tex)
    except CompileError as exc:
        # Safe to print here: the example is a fake fixture, unlike a real resume log.
        pytest.fail(f"{exc}\n{_latex_errors(tex.with_suffix('.log'))}")
    assert page_count(pdf) == 1


def test_shell_escape_is_blocked(tmp_path):
    tex = tmp_path / "resume.tex"
    marker = tmp_path / "pwned"
    tex.write_text(
        "\\documentclass{article}\\begin{document}"
        f"\\immediate\\write18{{touch {marker}}}x\\end{{document}}"
    )
    compile_tex(tex)
    assert not marker.exists()


def test_spilled_sections_logic():
    from jobagent.compile import spilled_sections

    titles = ["Education", "Experience", "Projects", "Technical Skills"]
    # Page 2 starts mid-Projects, then Skills begins.
    assert spilled_sections("  more project text\nTECHNICAL SKILLS\nPython", titles) == ["Projects", "Technical Skills"]
    # Page 2 starts exactly at a heading.
    assert spilled_sections("Projects\nbullet", titles) == ["Projects", "Technical Skills"]
    # No heading on page 2: the last section continues.
    assert spilled_sections("just a trailing line", titles) == ["Technical Skills"]
