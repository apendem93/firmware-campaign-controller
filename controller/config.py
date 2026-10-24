from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Tuning(BaseModel):
    model_config = {"extra": "forbid"}
    scheduler_interval: float = Field(default=1, gt=0)
    http_timeout: float = Field(default=5, gt=0)
    nudge_attempts: int = Field(default=3, ge=1)
    nudge_backoff: float = Field(default=5, gt=0)
    inform_timeout: float = Field(default=90, gt=0)
    ack_timeout: float = Field(default=30, gt=0)
    result_timeout: float = Field(default=900, gt=0)
    verification_grace: float = Field(default=30, gt=0)
    lease_seconds: float = Field(default=15, gt=0)
    minimum_sample: int = Field(default=20, ge=1)
    max_http_concurrency: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def deadlines(self):
        if self.lease_seconds <= self.http_timeout:
            raise ValueError("lease_seconds must exceed http_timeout")
        if self.result_timeout <= self.ack_timeout:
            raise ValueError("result_timeout must exceed ack_timeout")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://firmware:firmware@localhost:5432/firmware"
    simulator_url: str = "http://localhost:8001"
    config_file: str = "config.yaml"
    worker_metrics_port: int = 9001

    def tuning(self) -> Tuning:
        return Tuning.model_validate(yaml.safe_load(Path(self.config_file).read_text()) or {})


@lru_cache
def settings() -> Settings:
    return Settings()
