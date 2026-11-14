"""
config.py — Application configuration via pydantic-settings.

All settings are loaded from the .env file in the project root (if present),
then from real environment variables.  The module-level ``settings`` singleton
is the single source of truth for the entire application — every module that
needs configuration should import it from here.

No module may call ``os.environ.get()`` for application settings; that logic
belongs here so that the full configuration is validated and typed in one place.

All fields have defaults so the application starts correctly without any .env
file (useful for CI and first-run scenarios).  Values that should be changed
before production use are flagged in .env.example.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Read from .env in the current working directory if present; silently
        # ignored when the file does not exist (no error).
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Flask ─────────────────────────────────────────────────────────────────
    secret_key: str = Field(default="dev-key-local-only")
    flask_env: Literal["development", "production", "testing"] = "development"
    max_upload_mb: int = Field(default=200, ge=1, le=2000)

    # ── LLM ──────────────────────────────────────────────────────────────────
    # "disabled" is a valid runtime value (equivalent to ENABLE_LLM=false);
    # it is primarily useful in CI where no LLM server is available.
    # "reasoning" can be: "high", "medium", "low", "none"
    llm_provider: Literal["ollama", "llamacpp", "disabled"] = "ollama"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_context_limit: int = Field(default=24000, ge=1000)
    enable_llm: bool = True
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_reasoning_effort: str = "none"
    # Comma-separated hosts allowed even though they resolve to public IPs
    # (institutional servers). Loopback / private addresses need no entry.
    llm_allowed_hosts: str = ""

    # ── Prompt cache ──────────────────────────────────────────────────────────
    # In-memory, per job (app/services/cache.py). Set to false to disable it,
    # e.g. for benchmark runs. Runs without the LLM never use it.
    enable_llm_cache: bool = True

    # ── Redis / RQ ────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Paths (stored as strings; resolved to Path objects via properties) ────
    upload_folder: str = "data/uploads"
    output_folder: str = "data/out"
    jobs_folder: str = "data/jobs"
    registry_path: str = "config/Registry_base.xlsx"

    # ── Extraction behaviour ──────────────────────────────────────────────────
    hard_extraction_threads: int = Field(default=1, ge=1, le=16)

    # ── Derived path properties ───────────────────────────────────────────────
    @property
    def upload_folder_path(self) -> Path:
        """Return the upload staging directory as a Path object."""
        return Path(self.upload_folder)

    @property
    def output_folder_path(self) -> Path:
        """Return the output (ZIP) directory as a Path object."""
        return Path(self.output_folder)

    @property
    def jobs_folder_path(self) -> Path:
        """Return the per-job state JSON directory as a Path object."""
        return Path(self.jobs_folder)

    @property
    def registry_path_obj(self) -> Path:
        """Return the Registry_base.xlsx path as a Path object."""
        return Path(self.registry_path)

    @property
    def max_content_length(self) -> int:
        """Flask MAX_CONTENT_LENGTH in bytes."""
        return self.max_upload_mb * 1024 * 1024

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        """Ensure LLM_BASE_URL begins with http:// or https:// and strip trailing slash."""
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"LLM_BASE_URL must start with http:// or https://, got: {v!r}")
        return v.rstrip("/")

    @model_validator(mode="after")
    def ensure_data_dirs_exist(self) -> "Settings":
        """Create required directories on startup if they don't exist."""
        for folder in (
            self.upload_folder_path,
            self.output_folder_path,
            self.jobs_folder_path,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def to_flask_config(self) -> dict:
        """Return a dict suitable for app.config.update()."""
        return {
            "SECRET_KEY": self.secret_key,
            "MAX_CONTENT_LENGTH": self.max_content_length,
            "UPLOAD_FOLDER": str(self.upload_folder_path),
            "OUTPUT_FOLDER": str(self.output_folder_path),
            "JOBS_FOLDER": str(self.jobs_folder_path),
        }


# Module-level singleton — import this everywhere.
settings = Settings()
