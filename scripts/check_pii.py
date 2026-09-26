#!/usr/bin/env python3
"""Block commits that contain personal data or private keys.

Fails if a file:
  * contains an email address, a phone number, or a PEM private key block,
    unless the match is covered by .pii-allowlist; or
  * has a path that must never be tracked (.env, secrets/, *.pdf, *.sqlite,
    key files, ...), even if it was force-added past .gitignore.

Findings are reported by file, line, and kind only. The matched text is never
printed, so running this cannot leak what it finds.

Usage:
  check_pii.py FILE...   check the given files (pre-commit passes staged files)
  check_pii.py --all     check every file tracked by git (used in CI)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = REPO_ROOT / ".pii-allowlist"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

# Formatted phone numbers: US style (NNN) NNN-NNNN, NNN.NNN.NNNN,
# +1 NNN NNN NNNN, and international +CC followed by digit groups. Bare digit
# runs are not flagged; they collide with timestamps and IDs.
PHONE = re.compile(
    r"""
    (?<![\w.+-])
    (?:
        (?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}
      | \+\d{1,3}(?:[\s.-]?\(?\d{1,5}\)?){2,5}
    )
    (?![\w.-]*\d)
    """,
    re.VERBOSE,
)
PRIVATE_KEY = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")

# Paths that must never be committed, regardless of .gitignore.
FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)\.env($|\.(?!example$))"), "environment file"),
    (re.compile(r"(^|/)(secrets|private|data|output|screenshots|browser_profile|checkpoints)/"), "private directory"),
    (re.compile(r"\.(pdf|sqlite3?|db|log|pem|key|p12|pfx)$", re.I), "private file type"),
    (re.compile(r"(^|/)\.langsmith(/|$)"), "LangSmith data"),
]
# JSON is forbidden (service-account keys) except these files.
JSON_ALLOWED = {".claude/settings.json"}
# Real resume/profile/bullets files are only allowed as fake fixtures.
FIXTURE_ONLY = re.compile(r"(\.tex|(^|/)bullets\.yaml|(^|/)profile\.yaml)$")


def load_allowlist() -> list[re.Pattern[str]]:
    if not ALLOWLIST_FILE.exists():
        return []
    patterns = []
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def path_problem(rel: str) -> str | None:
    for pattern, kind in FORBIDDEN_PATHS:
        if pattern.search(rel):
            return kind
    if rel.lower().endswith(".json") and rel not in JSON_ALLOWED:
        return "JSON file (possible key file); add to JSON_ALLOWED if intended"
    if FIXTURE_ONLY.search(rel) and not rel.startswith("examples/"):
        return "resume/profile/bullets file outside examples/"
    return None


def content_problems(path: Path, allowlist: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw[:8192]:
        return []  # binary; forbidden binary types are caught by path rules
    text = raw.decode("utf-8", errors="replace")

    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if PRIVATE_KEY.search(line):
            problems.append((lineno, "private key"))
        for kind, pattern in (("email address", EMAIL), ("phone number", PHONE)):
            for match in pattern.finditer(line):
                found = match.group(0)
                if pattern is PHONE and sum(c.isdigit() for c in found) < 8:
                    continue  # "+1 2 3" in code or math, not a phone number
                if not any(a.fullmatch(found) for a in allowlist):
                    problems.append((lineno, kind))
    return problems


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def main(argv: list[str]) -> int:
    files = tracked_files() if argv == ["--all"] else argv
    allowlist = load_allowlist()
    failures = 0

    for name in files:
        path = Path(name)
        abs_path = path.resolve()
        if abs_path.resolve() == ALLOWLIST_FILE:
            continue
        try:
            rel = abs_path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()

        kind = path_problem(rel)
        if kind and not rel.startswith(".git/"):
            print(f"{rel}: forbidden path ({kind})")
            failures += 1
        for lineno, kind in content_problems(abs_path, allowlist):
            print(f"{rel}:{lineno}: possible {kind}")
            failures += 1

    if failures:
        print(
            f"\ncheck_pii: {failures} finding(s). Remove the data, or if it is a"
            " deliberately fake value, add a narrow pattern to .pii-allowlist."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
