from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trading Web AE Ho Lai"
    app_env: str = "development"
    debug: bool = False
    secret_key: str = Field(..., alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(4320, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    jwt_algorithm: str = Field("HS256", alias="JWT_ALGORITHM")
    database_url: str = Field(..., alias="DATABASE_URL")
    encryption_key: str = Field(..., alias="ENCRYPTION_KEY")
    trade_monitor_enabled: bool = Field(False, alias="TRADE_MONITOR_ENABLED")
    trade_monitor_interval_seconds: int = Field(15, alias="TRADE_MONITOR_INTERVAL_SECONDS")
    mt5_history_auto_sync_enabled: bool = Field(True, alias="MT5_HISTORY_AUTO_SYNC_ENABLED")
    mt5_history_auto_sync_interval_seconds: int = Field(180, alias="MT5_HISTORY_AUTO_SYNC_INTERVAL_SECONDS")
    mt5_history_auto_sync_max_accounts_per_cycle: int = Field(3, alias="MT5_HISTORY_AUTO_SYNC_MAX_ACCOUNTS_PER_CYCLE")
    mt5_history_auto_sync_stale_after_seconds: int = Field(180, alias="MT5_HISTORY_AUTO_SYNC_STALE_AFTER_SECONDS")
    telegram_bot_token: str | None = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(None, alias="TELEGRAM_CHAT_ID")
    telegram_timeout_seconds: int = Field(5, alias="TELEGRAM_TIMEOUT_SECONDS")
    max_preview_drift_percent: float = Field(25.0, alias="MAX_PREVIEW_DRIFT_PERCENT")
    scratch_manual_threshold_r: float = Field(0.5, alias="SCRATCH_MANUAL_THRESHOLD_R")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
