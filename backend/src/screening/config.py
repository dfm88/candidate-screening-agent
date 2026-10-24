from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_NAME = "candidate-screening-agent"


class Settings(BaseSettings):
    # Values come from the process environment only (`uv run --env-file .env`, Cloud Run, compose). Reading
    # .env here too would hide it from the Logfire SDK and Google auth, which only read the environment.
    model_config = SettingsConfigDict(extra="ignore")

    environment: str = "local"
    database_url: str = "postgresql+asyncpg://screening:screening@localhost:55432/screening"
    # Prompts and completions contain candidate PII, so they are only sent to Logfire when explicitly enabled.
    llm_content_logging: bool = False
    # Built frontend, served by the API in the container; absent in local dev where Vite serves it.
    static_dir: Path | None = None
