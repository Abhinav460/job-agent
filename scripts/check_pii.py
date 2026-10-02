#!/usr/bin/env python3
"""Block commits that contain personal data or private keys.

Fails if a file:
  * contains an email address, a phone number, or a PEM private key block,
    unless the match is covered by .pii-allowlist;
  * contains the owner's own name, email, or phone (JOBAGENT_OWNER_NAME /
    _EMAIL / _PHONE, read from the environment or .env), which the generic
    patterns can miss (a name, or an unformatted number); or
  * has a path that must never be tracked (.env, secrets/, *.pdf, *.sqlite,
    key files, ...), even if it was force-added past .gitignore.

Findings are reported by file, line, and kind only. The matched text is never
printed, so running this cannot leak what it finds.

Usage:
  check_pii.py FILE...   check the given files (pre-commit passes staged files)
  check_pii.py --all     check every file tracked by git (used in CI)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = REPO_ROOT / ".pii-allowlist"
OWNER_VARS = ("JOBAGENT_OWNER_NAME", "JOBAGENT_OWNER_EMAIL", "JOBAGENT_OWNER_PHONE")

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


def read_owner_vars(dotenv: Path) -> dict[str, str]:
    """OWNER_VARS from .env, overridden by the environment. Other keys are skipped."""
    values: dict[str, str] = {}
    if dotenv.is_file():
        for raw in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = raw.strip().partition("=")
            if sep and key.strip() in OWNER_VARS:
                values[key.strip()] = value.strip().strip("'\"")
    values.update({k: os.environ[k] for k in OWNER_VARS if os.environ.get(k)})
    return values


def owner_patterns(values: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    patterns = []
    if name := values.get("JOBAGENT_OWNER_NAME", "").strip():
        # The full name and each part of 3+ letters (a surname on its own).
        parts = sorted({name, *(p for p in name.split() if len(p) >= 3)}, key=len, reverse=True)
        alternation = "|".join(re.escape(p) for p in parts)
        patterns.append((re.compile(rf"(?<![A-Za-z])(?:{alternation})(?![A-Za-z])", re.I), "owner name"))
    if email := values.get("JOBAGENT_OWNER_EMAIL", "").strip():
        patterns.append((re.compile(re.escape(email), re.I), "owner email"))
    if digits := re.sub(r"\D", "", values.get("JOBAGENT_OWNER_PHONE", ""))[-10:]:
        # Any formatting: 9735550123, (973) 555-0123, +1 973 555 0123, ...
        patterns.append((re.compile(r"(?<!\d)" + r"[\s().-]*".join(digits) + r"(?!\d)"), "owner phone"))
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


def content_problems(
    path: Path,
    allowlist: list[re.Pattern[str]],
    owner: list[tuple[re.Pattern[str], str]] = (),
) -> list[tuple[int, str]]:
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
        for pattern, kind in owner:
            if pattern.search(line):
                problems.append((lineno, kind))
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
    owner = owner_patterns(read_owner_vars(REPO_ROOT / ".env"))
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
        for lineno, kind in content_problems(abs_path, allowlist, owner):
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
