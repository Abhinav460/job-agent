"""LaTeX editing for the resume: marker replacement, rendering, and validation.

The writer only ever changes text between marker comments in resume.tex:

    % BEGIN:EXPERIENCE
    ...generated...
    % END:EXPERIENCE

Everything outside the markers (header, contact line, education, styling)
is the user's and is copied unchanged. Rendering assumes the macros of Jake
Gutierrez's resume template (\\resumeSubheading, \\resumeItem, ...); see
examples/resume.tex.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobagent.bullets import ExperienceEntry, ProjectEntry

MARKER_NAMES = ("EXPERIENCE", "PROJECTS", "SKILLS")

# Commands that can read/write files, run programs, or redefine the document.
# Never allowed in generated content.
FORBIDDEN_COMMANDS = {
    "input", "include", "includeonly", "InputIfFileExists", "write", "write18",
    "immediate", "openout", "openin", "read", "readline", "catcode", "def",
    "edef", "gdef", "xdef", "let", "newcommand", "renewcommand",
    "providecommand", "csname", "usepackage", "RequirePackage", "special",
    "directlua", "ShellEscape", "url", "href", "verbatiminput", "lstinputlisting",
}
# Inline commands a rephrasing may introduce even if the source lacks them.
REPHRASE_ALLOWED_COMMANDS = {"textbf", "textit", "emph", "%", "&", "$", "#", "_"}

_COMMAND = re.compile(r"\\([A-Za-z]+|.)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_DEFINITION = re.compile(r"\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\s*$|\\def\s*$")


class LatexError(ValueError):
    pass


# --- Validation ---------------------------------------------------------------


def _strip_comment(line: str) -> str:
    match = re.search(r"(?<!\\)%", line)
    return line[: match.start()] if match else line


def brace_problems(tex: str) -> list[str]:
    """Unbalanced braces, ignoring escaped braces and % comments."""
    problems = []
    depth = 0
    for lineno, line in enumerate(tex.splitlines(), start=1):
        code = _strip_comment(line)
        i = 0
        while i < len(code):
            ch = code[i]
            if ch == "\\":
                i += 2  # skip the escaped character (\{, \}, \%, \\ ...)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    problems.append(f"line {lineno}: unmatched closing brace")
                    depth = 0
            i += 1
    if depth > 0:
        problems.append(f"{depth} unclosed opening brace(s)")
    return problems


def _matching_brace(tex: str, open_index: int) -> int:
    depth = 0
    i = open_index
    while i < len(tex):
        ch = tex[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            i = tex.find("\n", i)
            if i == -1:
                break
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def resume_item_problems(tex: str) -> list[str]:
    """Each \\resumeItem must take exactly one balanced {...} argument."""
    problems = []
    for match in re.finditer(r"\\resumeItem(?![A-Za-z])", tex):
        if _DEFINITION.search(tex, max(0, match.start() - 30), match.start()):
            continue  # the macro's own \newcommand{\resumeItem}[1]{...}
        lineno = tex.count("\n", 0, match.start()) + 1
        i = match.end()
        while i < len(tex) and tex[i] in " \t":
            i += 1
        if i >= len(tex) or tex[i] != "{":
            problems.append(f"line {lineno}: \\resumeItem without an opening brace")
            continue
        close = _matching_brace(tex, i)
        if close == -1:
            problems.append(f"line {lineno}: \\resumeItem argument never closes")
            continue
        rest = tex[close + 1 :].lstrip(" \t")
        if rest.startswith("{") or rest.startswith("}"):
            problems.append(f"line {lineno}: \\resumeItem has an extra top-level brace")
    return problems


def forbidden_commands(tex: str) -> set[str]:
    return {name for name in _COMMAND.findall(tex) if name in FORBIDDEN_COMMANDS}


def validate_generated(content: str) -> None:
    """Checks for LaTeX this program generated (never the user's own preamble)."""
    problems = brace_problems(content) + resume_item_problems(content)
    if bad := forbidden_commands(content):
        problems.append(f"forbidden command(s): {', '.join(sorted(bad))}")
    if problems:
        raise LatexError("; ".join(problems))


def validate_document(tex: str) -> None:
    problems = brace_problems(tex) + resume_item_problems(tex)
    if problems:
        raise LatexError("; ".join(problems))


# --- Rephrasing guard ---------------------------------------------------------


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _has_term(text: str, term: str) -> bool:
    pattern = r"(?<![\w+#])" + re.escape(term) + r"(?![\w+#])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def rephrase_problem(source: str, rewrite: str, protected_terms: list[str]) -> str | None:
    """Why a rewrite is not a faithful rephrasing of source, or None if it is.

    Rejects anything that adds a number/date, a skill or tool term, or a LaTeX
    command, or that grows the bullet noticeably. False rejections are
    harmless: the caller falls back to the verbatim source text.
    """
    if brace_problems(rewrite):
        return "unbalanced braces"
    if len(rewrite) > len(source) * 1.25 + 20:
        return "longer than source"
    if _numbers(rewrite) - _numbers(source):
        return "adds a number or date"
    new_commands = set(_COMMAND.findall(rewrite)) - set(_COMMAND.findall(source))
    if new_commands - REPHRASE_ALLOWED_COMMANDS or forbidden_commands(rewrite):
        return "adds a LaTeX command"
    for term in protected_terms:
        if term and _has_term(rewrite, term) and not _has_term(source, term):
            return "adds a skill or tool"
    return None


# --- Rendering ----------------------------------------------------------------


def _items(texts: list[str], pad: str) -> list[str]:
    lines = [f"{pad}\\resumeItemListStart"]
    lines += [f"{pad}  \\resumeItem{{{t}}}" for t in texts]
    lines.append(f"{pad}\\resumeItemListEnd")
    return lines


def render_experience(chosen: list[tuple[ExperienceEntry, list[str]]], pad: str = "") -> str:
    lines: list[str] = []
    for entry, texts in chosen:
        lines += [
            f"{pad}\\resumeSubheading",
            f"{pad}  {{{entry.title}}}{{{entry.dates}}}",
            f"{pad}  {{{entry.org}}}{{{entry.location}}}",
            *_items(texts, pad + "  "),
        ]
    return "\n".join(lines)


def render_projects(chosen: list[tuple[ProjectEntry, list[str]]], pad: str = "") -> str:
    lines: list[str] = []
    for entry, texts in chosen:
        title = f"\\textbf{{{entry.name}}}"
        if entry.tech:
            title += f" $|$ \\emph{{{entry.tech}}}"
        lines += [
            f"{pad}\\resumeProjectHeading",
            f"{pad}  {{{title}}}{{{entry.dates}}}",
            *_items(texts, pad + "  "),
        ]
    return "\n".join(lines)


def render_skills(skills: list[tuple[str, list[str]]], pad: str = "") -> str:
    lines = [f"{pad}\\textbf{{{category}}}{{: {', '.join(items)}}}" for category, items in skills]
    return " \\\\\n".join(lines)


# --- Markers ------------------------------------------------------------------


def _marker_re(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<pad>[ \t]*)% BEGIN:{name}[ \t]*\n(?P<body>.*?)^(?P<endpad>[ \t]*)% END:{name}[ \t]*$",
        re.MULTILINE | re.DOTALL,
    )


def marker_indent(tex: str, name: str) -> str:
    match = _marker_re(name).search(tex)
    return match.group("pad") if match else ""


def markers_present(tex: str) -> set[str]:
    return {name for name in MARKER_NAMES if len(_marker_re(name).findall(tex)) == 1}


def replace_marked(tex: str, name: str, content: str) -> str:
    matches = list(_marker_re(name).finditer(tex))
    if len(matches) != 1:
        raise LatexError(f"expected exactly one % BEGIN:{name} / % END:{name} pair, found {len(matches)}")
    m = matches[0]
    replacement = f"{m.group('pad')}% BEGIN:{name}\n{content}\n{m.group('endpad')}% END:{name}"
    return tex[: m.start()] + replacement + tex[m.end() :]
