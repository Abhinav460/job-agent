"""The bullet bank: the only source of truth for resume content.

bullets.yaml (in $JOBAGENT_HOME) holds every real experience/project bullet
and the skills vocabulary. The resume writer may select and lightly rephrase
these, never add to them. Text is LaTeX-ready: write `\\%`, `\\&`, `\\textbf{}`
exactly as they should appear in the .tex. See examples/bullets.yaml.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobagent.latex import brace_problems

Domain = Literal["IT", "SWE", "data", "ML"]
Section = Literal["experience", "projects"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bullet(_Strict):
    id: str
    text: str
    domains: list[Domain] = Field(min_length=1)


class ExperienceEntry(_Strict):
    id: str
    org: str
    title: str
    location: str = ""
    dates: str
    bullets: list[Bullet] = Field(min_length=1)

    @property
    def heading(self) -> str:
        return f"{self.title}, {self.org} ({self.dates})"


class ProjectEntry(_Strict):
    id: str
    name: str
    tech: str = ""
    dates: str = ""
    bullets: list[Bullet] = Field(min_length=1)

    @property
    def heading(self) -> str:
        return f"{self.name} [{self.tech}]" if self.tech else self.name


Entry = ExperienceEntry | ProjectEntry


@dataclass(frozen=True)
class BulletRef:
    section: Section
    entry: Entry
    bullet: Bullet


class BulletBank(_Strict):
    skills: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> BulletBank:
        ids = [e.id for e in [*self.experience, *self.projects]]
        ids += [ref.bullet.id for ref in self.refs()]
        if dupes := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"duplicate id(s) in bullets.yaml: {', '.join(sorted(dupes))}")
        for ref in self.refs():
            if problems := brace_problems(ref.bullet.text):
                raise ValueError(f"bullet {ref.bullet.id}: {problems[0]}")
        return self

    def refs(self) -> Iterator[BulletRef]:
        for entry in self.experience:
            for bullet in entry.bullets:
                yield BulletRef("experience", entry, bullet)
        for entry in self.projects:
            for bullet in entry.bullets:
                yield BulletRef("projects", entry, bullet)

    def ref(self, bullet_id: str) -> BulletRef | None:
        return next((r for r in self.refs() if r.bullet.id == bullet_id), None)

    def entry(self, entry_id: str) -> Entry | None:
        return next((e for e in [*self.experience, *self.projects] if e.id == entry_id), None)

    def skill_terms(self) -> list[str]:
        return [item for items in self.skills.values() for item in items]


def load_bullets(path: Path) -> BulletBank:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BulletBank.model_validate(data)
