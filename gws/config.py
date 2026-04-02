from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GWS_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    planner_provider: Optional[str] = None
    planner_model: Optional[str] = None
    planner_api_key: Optional[str] = None
