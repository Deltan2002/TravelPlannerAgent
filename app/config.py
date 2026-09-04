from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AI Travel Planner"
    app_mode: Literal["demo", "live"] = "demo"
    database_path: Path = Path("data/travel_planner.sqlite3")

    serper_api_key: str | None = None

    llm_provider: Literal["deterministic", "openai"] = "deterministic"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    redis_url: str | None = None
    cache_ttl_seconds: float = Field(default=900.0, ge=0, le=86400)
    max_revisions: int = Field(default=5, ge=1, le=20)

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
