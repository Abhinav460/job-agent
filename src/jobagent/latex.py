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
_JUSTIFY = re.compile(r"\\justify(?![A-Za-z])")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[./-][A-Za-z0-9+#]+)*")
_DEFINITION = re.compile(r"\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\s*$|\\def\s*$")


class LatexError(ValueError):
    pass


# --- Validation ---------------------------------------------------------------


def _strip_comment(line: str) -> str:
    match = re.search(r"(?<!\\)%", line)
    return line[: match.start()] if match else line


def _blank_comments(tex: str) -> str:
    """Replace % comments with spaces, keeping offsets and line numbers."""
    return "\n".join(
        line[: len(code)] + " " * (len(line) - len(code))
        for line in tex.split("\n")
        for code in [_strip_comment(line)]
    )


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
    """Each \\resumeItem takes exactly one balanced {...} argument.

    The argument is either plain bullet text, or exactly one \\justify{...}
    group wrapping the whole bullet: \\resumeItem{\\justify{text}}.
    """
    problems = []
    tex = _blank_comments(tex)
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
        if problem := _justify_problem(tex[i + 1 : close]):
            problems.append(f"line {lineno}: {problem}")
    return problems


def _justify_problem(argument: str) -> str | None:
    stripped = argument.strip()
    if not stripped.startswith("\\justify"):
        return "\\justify must wrap the whole bullet" if _JUSTIFY.search(argument) else None
    j = len("\\justify")
    while j < len(stripped) and stripped[j] in " \t":
        j += 1
    if j >= len(stripped) or stripped[j] != "{":
        return "\\justify without a {...} group"
    close = _matching_brace(stripped, j)
    if close != len(stripped) - 1:
        return "\\resumeItem must contain exactly one \\justify{...} and nothing else"
    if _JUSTIFY.search(stripped, j):
        return "nested \\justify inside \\justify"
    return None


def unwrap_justify(text: str) -> str:
    """'\\justify{text}' -> 'text'; anything else unchanged."""
    stripped = text.strip()
    if stripped.startswith("\\justify") and _justify_problem(stripped) is None:
        return stripped[stripped.index("{") + 1 : -1].strip()
    return text


def uses_justify(tex: str) -> bool:
    """Whether the user's template writes bullets as \\resumeItem{\\justify{...}}."""
    return re.search(r"\\resumeItem\s*\{\s*\\justify(?![A-Za-z])", _blank_comments(tex)) is not None


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


def _new_names(source: str, rewrite: str) -> set[str]:
    """Name-like tokens (capitals, digits, + # . / -) in rewrite that source lacks.

    Catches tools and proper nouns that aren't in any vocabulary, e.g. 'Kafka'
    or 'Node.js'. The first word of each sentence is exempt.
    """
    plain = lambda t: re.sub(r"\\[A-Za-z]+", " ", t)  # noqa: E731
    known = {tok.lower() for tok in _TOKEN.findall(plain(source))}
    text = plain(rewrite)
    new = set()
    for m in _TOKEN.finditer(text):
        token = m.group(0)
        sentence_start = re.search(r"(^|[.!?;:]\s+)\W*$", text[: m.start()]) is not None
        name_like = token != token.lower() or re.search(r"[0-9+#./]", token)
        if name_like and not (sentence_start and token[1:] == token[1:].lower()) and token.lower() not in known:
            new.add(token)
    return new


def rephrase_problem(source: str, rewrite: str, protected_terms: list[str]) -> str | None:
    """Why a rewrite is not a faithful rephrasing of source, or None if it is.

    Rejects anything that adds a number/date, a skill or tool term (from the
    vocabulary or the posting), a new name-like token, or a LaTeX command, or
    that grows the bullet noticeably. False rejections are harmless: the
    caller falls back to the verbatim source text.
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
    if _new_names(source, rewrite):
        return "adds a name, tool, or term not in the source"
    return None


# --- Rendering ----------------------------------------------------------------


def _items(texts: list[str], pad: str, justify: bool) -> list[str]:
    lines = [f"{pad}\\resumeItemListStart"]
    for t in texts:
        body = f"\\justify{{{t}}}" if justify else t
        lines.append(f"{pad}  \\resumeItem{{{body}}}")
    lines.append(f"{pad}\\resumeItemListEnd")
    return lines


def render_experience(
    chosen: list[tuple[ExperienceEntry, list[str]]], pad: str = "", justify: bool = False
) -> str:
    lines: list[str] = []
    for entry, texts in chosen:
        lines += [
            f"{pad}\\resumeSubheading",
            f"{pad}  {{{entry.title}}}{{{entry.dates}}}",
            f"{pad}  {{{entry.org}}}{{{entry.location}}}",
            *_items(texts, pad + "  ", justify),
        ]
    return "\n".join(lines)


def render_projects(
    chosen: list[tuple[ProjectEntry, list[str]]], pad: str = "", justify: bool = False
) -> str:
    lines: list[str] = []
    for entry, texts in chosen:
        title = f"\\textbf{{{entry.name}}}"
        if entry.tech:
            title += f" $|$ \\emph{{{entry.tech}}}"
        lines += [
            f"{pad}\\resumeProjectHeading",
            f"{pad}  {{{title}}}{{{entry.dates}}}",
            *_items(texts, pad + "  ", justify),
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
