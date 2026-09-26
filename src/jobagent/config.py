"""Configuration loaded from the environment and the repo-local .env file.

Secrets and personal identifiers are SecretStr, so they print as '**********'.
Settings.__repr__ and __str__ are overridden as well, so logging or printing
the whole object shows nothing. Read a secret with .get_secret_value() only at
the point of use, and never log the result.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Env vars that turn on LangSmith/LangChain tracing. All are forced off unless
# the user opts in with LANGSMITH_TRACING=true.
_TRACING_ENV_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Personal data directory, outside the repo.
    jobagent_home: Path = Path("~/.jobagent")

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    coordinator_model: str = "claude-sonnet-5"
    writer_model: str = "claude-opus-5-5"

    # Gemini (optional)
    gemini_enabled: bool = False
    google_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"

    # Qdrant bullet bank
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "bullet_bank"

    # Ollama embeddings
    ollama_base_url: str | None = None
    ollama_embed_model: str = "nomic-embed-text"

    # Google Sheets tracker
    google_sheet_id: SecretStr | None = None
    google_service_account_file: Path | None = None

    # LangSmith: off unless explicitly enabled.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "job-agent"

    # Owner identity: used only for log redaction and the pre-publish history scan.
    jobagent_owner_name: SecretStr | None = None
    jobagent_owner_email: SecretStr | None = None
    jobagent_owner_phone: SecretStr | None = None

    log_level: str = "INFO"

    @field_validator("jobagent_home", "google_service_account_file", mode="after")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator(
        "anthropic_api_key",
        "google_api_key",
        "qdrant_api_key",
        "google_sheet_id",
        "langsmith_api_key",
        "jobagent_owner_name",
        "jobagent_owner_email",
        "jobagent_owner_phone",
        mode="after",
    )
    @classmethod
    def _empty_secret_is_none(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    def __repr__(self) -> str:
        return "Settings(<redacted>)"

    __str__ = __repr__

    # --- Paths inside $JOBAGENT_HOME ---

    @property
    def secrets_dir(self) -> Path:
        return self.jobagent_home / "secrets"

    @property
    def service_account_file(self) -> Path:
        return self.google_service_account_file or self.secrets_dir / "service_account.json"

    @property
    def resume_tex(self) -> Path:
        return self.jobagent_home / "resume.tex"

    @property
    def bullets_file(self) -> Path:
        return self.jobagent_home / "bullets.yaml"

    @property
    def profile_file(self) -> Path:
        return self.jobagent_home / "profile.yaml"

    @property
    def output_dir(self) -> Path:
        return self.jobagent_home / "output"

    @property
    def screenshots_dir(self) -> Path:
        return self.jobagent_home / "screenshots"

    @property
    def browser_profile_dir(self) -> Path:
        return self.jobagent_home / "browser_profile"

    @property
    def checkpoint_db(self) -> Path:
        return self.jobagent_home / "checkpoints" / "graph.sqlite"

    # --- Secret access for the redactor ---

    def secret_values(self) -> list[str]:
        """Plain values of every SecretStr field that is set. For redaction only."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                values.append(value.get_secret_value())
        return values


def apply_tracing_policy(settings: Settings) -> None:
    """Force LangSmith/LangChain tracing off unless the user opted in.

    A globally exported LANGCHAIN_TRACING_V2=true in the user's shell would
    otherwise send resumes and form answers to LangSmith.
    """
    enabled = settings.langsmith_tracing and settings.langsmith_api_key is not None
    for var in _TRACING_ENV_VARS:
        os.environ[var] = "true" if enabled else "false"
    if enabled:
        # pydantic-settings reads .env without exporting it; the LangSmith
        # client only looks at os.environ.
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    apply_tracing_policy(settings)
    return settings
