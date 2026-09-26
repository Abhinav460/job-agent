"""Real pdflatex/pdfinfo run on the fake example. Skipped when TeX Live is missing."""

import shutil
from pathlib import Path

import pytest

from jobagent.compile import compile_tex, page_count

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "resume.tex"

pytestmark = pytest.mark.skipif(
    not (shutil.which("pdflatex") and shutil.which("pdfinfo")), reason="pdflatex/pdfinfo not installed"
)


def test_example_compiles_to_one_page(tmp_path):
    tex = tmp_path / "resume.tex"
    shutil.copy(EXAMPLE, tex)
    pdf = compile_tex(tex)
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
