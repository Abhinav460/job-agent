"""Filesystem access confined to the agent's working directories.

Every read and write of personal data goes through these helpers. Paths are
resolved (following symlinks) before the containment check, so neither
`../` nor a symlink can escape the allowed roots. New files are private (0600)
and new directories 0700.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path


class PathOutsideWorkingDirs(ValueError):
    pass


def resolve_within(path: str | os.PathLike[str], roots: Iterable[Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    for root in roots:
        root = Path(root).expanduser().resolve()
        if resolved.is_relative_to(root):
            return resolved
    # Only the file name: the full path may contain personal details.
    raise PathOutsideWorkingDirs(f"refusing path outside working dirs: {Path(path).name!r}")


def read_text(path: str | os.PathLike[str], roots: Iterable[Path]) -> str:
    return resolve_within(path, roots).read_text(encoding="utf-8")


def ensure_private_dir(path: str | os.PathLike[str], roots: Iterable[Path]) -> Path:
    target = resolve_within(path, roots)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    return target


def write_text(path: str | os.PathLike[str], text: str, roots: Iterable[Path]) -> Path:
    roots = list(roots)
    target = resolve_within(path, roots)
    ensure_private_dir(target.parent, roots)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return target


def slug(text: str, max_len: int = 40) -> str:
    """Filesystem-safe name from untrusted text (e.g. an LLM-parsed company name)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return cleaned[:max_len].rstrip("-") or "unknown"
