"""pdflatex and pdfinfo, run with fixed argument lists and a scrubbed environment."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


class CompileError(RuntimeError):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise CompileError(f"{name} not found on PATH")
    return path


def _env(tex_inputs: Path | None) -> dict[str, str]:
    # No API keys or other secrets reach the subprocess.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "openout_any": "p",  # TeX may only write inside the output directory
    }
    if tex_inputs is not None:
        env["TEXINPUTS"] = f"{tex_inputs}:"  # user's .cls/.sty next to resume.tex
    return env


def compile_tex(tex_path: Path, tex_inputs: Path | None = None, timeout: int = 120) -> Path:
    """Compile tex_path in its own directory (twice, for references). Returns the PDF path."""
    pdflatex = _tool("pdflatex")
    workdir = tex_path.parent
    args = [
        pdflatex,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-no-shell-escape",
        f"-output-directory={workdir}",
        tex_path.name,
    ]
    for _ in range(2):
        result = subprocess.run(
            args, cwd=workdir, env=_env(tex_inputs), capture_output=True, timeout=timeout, check=False
        )
        if result.returncode != 0:
            # The log can quote resume content, so point at it instead of printing it.
            raise CompileError(f"pdflatex exited {result.returncode}; see {tex_path.with_suffix('.log')}")
    pdf = tex_path.with_suffix(".pdf")
    if not pdf.exists():
        raise CompileError("pdflatex produced no PDF")
    return pdf


def page_count(pdf_path: Path, timeout: int = 30) -> int:
    result = subprocess.run(
        [_tool("pdfinfo"), str(pdf_path)], env=_env(None), capture_output=True, text=True, timeout=timeout, check=False
    )
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    if result.returncode != 0 or match is None:
        raise CompileError("pdfinfo could not read the PDF")
    return int(match.group(1))
