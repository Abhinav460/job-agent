"""Strict private facts used by the deterministic form filler."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from jobagent.safepaths import read_text


class ProfileError(ValueError):
    pass


class ApplicantProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    first_name: SecretStr
    last_name: SecretStr
    email: SecretStr
    phone: SecretStr | None = None
    location: SecretStr | None = None
    country: SecretStr | None = None
    linkedin: SecretStr | None = None
    website: SecretStr | None = None

    @field_validator("first_name", "last_name", "email", "phone", "location", "country",
                     "linkedin", "website")
    @classmethod
    def valid_text(cls, value):
        if value is None:
            return value
        text = value.get_secret_value().strip()
        if not text or len(text) > 500 or any(ord(c) < 32 for c in text):
            raise ValueError("profile fields must contain bounded, nonempty single-line text")
        return SecretStr(text)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.get_secret_value()):
            raise ValueError("invalid email format")
        return value

    @field_validator("linkedin", "website")
    @classmethod
    def valid_url(cls, value):
        if value is not None:
            url = urlsplit(value.get_secret_value())
            if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
                raise ValueError("profile URLs must be public http(s) URLs")
        return value

    def __repr__(self):
        return "ApplicantProfile(<private>)"

    __str__ = __repr__


def load_profile(path: Path, home: Path) -> ApplicantProfile:
    try:
        contents = read_text(path, [home])
        if len(contents) > 20_000:
            raise ProfileError("profile exceeds size limit")
        return ApplicantProfile.model_validate(yaml.safe_load(contents))
    except Exception:
        # YAML/parser errors may include the entire private source line.
        raise ProfileError("profile unavailable or invalid; check private profile.yaml") from None
