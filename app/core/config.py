from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trading Web AE Ho Lai"
    app_env: str = "development"
    debug: bool = False
    secret_key: str = Field(..., alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(60, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    jwt_algorithm: str = Field("HS256", alias="JWT_ALGORITHM")
    database_url: str = Field(..., alias="DATABASE_URL")
    encryption_key: str = Field(..., alias="ENCRYPTION_KEY")
    trade_monitor_enabled: bool = Field(False, alias="TRADE_MONITOR_ENABLED")
    trade_monitor_interval_seconds: int = Field(15, alias="TRADE_MONITOR_INTERVAL_SECONDS")
    telegram_bot_token: str | None = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(None, alias="TELEGRAM_CHAT_ID")
    telegram_timeout_seconds: int = Field(5, alias="TELEGRAM_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
