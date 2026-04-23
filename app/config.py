from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "postgresql://nurix:nurix@localhost:5432/nurix"
    app_port: int = 8000

    max_concurrent_default: int = 5
    max_call_duration_seconds: int = 600
    stuck_reclaim_get_status_timeout_seconds: int = 5
    max_retries_default: int = 3
    retry_backoff_base_seconds: int = 30

    scheduler_safety_net_seconds: float = 1.0
    reclaim_sweep_interval_seconds: float = 30.0

    webhook_processor_batch_max: int = 50
    webhook_inbox_retention_days: int = 7

    mock_call_duration_seconds: float = 3.0
    mock_failure_rate: float = 0.1
    mock_no_answer_rate: float = 0.1

    demo_mode: bool = False
    debug_endpoints_enabled: bool = False

    api_pool_min: int = 2
    api_pool_max: int = 10
    scheduler_pool_min: int = 2
    scheduler_pool_max: int = 10
    webhook_pool_min: int = 1
    webhook_pool_max: int = 3

    webhook_signing_secret: str = Field(default="")

    @property
    def stuck_reclaim_seconds(self) -> int:
        # Grace window = max_call_duration + 30s to avoid racing legitimate long
        # calls. The reclaim path confirms with provider.get_status before any
        # CAS, so a wider-than-strict window is safe.
        return self.max_call_duration_seconds + 30

    @property
    def mock_call_duration_effective(self) -> float:
        return 8.0 if self.demo_mode else self.mock_call_duration_seconds

    @property
    def mock_failure_rate_effective(self) -> float:
        return 0.35 if self.demo_mode else self.mock_failure_rate

    @property
    def reclaim_sweep_interval_effective(self) -> float:
        # Demo overrides the default 30s so RECLAIM_EXECUTED lands inside the
        # live demo window instead of behind 30s of dead air.
        return 5.0 if self.demo_mode else self.reclaim_sweep_interval_seconds
