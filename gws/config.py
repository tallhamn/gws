from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GWS_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    db_pool_size: int = 10
    db_pool_timeout: int = 30
    db_pool_pre_ping: bool = True
    api_key: Optional[str] = None
    workers_path: str = "workers.yaml"
    planner_provider: Optional[str] = None
    planner_model: Optional[str] = None
    planner_api_key: Optional[str] = None
    planner_timeout: float = 60.0
    gateway_url: Optional[str] = None
