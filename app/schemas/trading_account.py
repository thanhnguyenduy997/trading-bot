from typing import Literal

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TradingAccountBase(BaseModel):
    broker_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=120)
    platform: str = Field(default="mt5", min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)
    telegram_enabled: bool = True
    telegram_chat_id: str | None = Field(default=None, max_length=120)
    max_total_setup_volume: float | None = Field(default=None, gt=0)
    default_symbol: str | None = Field(default=None, min_length=3, max_length=20)
    default_side: Literal["buy", "sell"] | None = None
    default_setup_mode: Literal["split_two_orders", "single_full_volume"] = "split_two_orders"
    default_risk_mode: Literal["fixed_money", "balance_percent"] | None = None
    default_risk_value: float | None = Field(default=None, gt=0)
    default_rr_order_2: float | None = Field(default=None, gt=0)
    max_preview_drift_percent_override: float | None = Field(default=None, ge=0)


class TradingAccountCreate(TradingAccountBase):
    password: str = Field(min_length=1, max_length=255)
    telegram_bot_token: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("default_symbol")
    @classmethod
    def normalize_default_symbol(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class TradingAccountUpdate(BaseModel):
    broker_name: str | None = Field(default=None, min_length=2, max_length=120)
    account_number: str | None = Field(default=None, min_length=2, max_length=120)
    server_name: str | None = Field(default=None, min_length=2, max_length=120)
    platform: str | None = Field(default=None, min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=120)
    max_total_setup_volume: float | None = Field(default=None, gt=0)
    default_symbol: str | None = Field(default=None, min_length=3, max_length=20)
    default_side: Literal["buy", "sell"] | None = None
    default_setup_mode: Literal["split_two_orders", "single_full_volume"] | None = None
    default_risk_mode: Literal["fixed_money", "balance_percent"] | None = None
    default_risk_value: float | None = Field(default=None, gt=0)
    default_rr_order_2: float | None = Field(default=None, gt=0)
    max_preview_drift_percent_override: float | None = Field(default=None, ge=0)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    telegram_bot_token: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("default_symbol")
    @classmethod
    def normalize_default_symbol(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class TradingAccountRead(TradingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    connection_status: str
    mt5_session_status: str
    current_mt5_login: str | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    telegram_enabled: bool
    telegram_chat_id: str | None
    max_total_setup_volume: float | None
    created_at: datetime
    updated_at: datetime


class TradingAccountTelegramTestRead(BaseModel):
    success: bool
    message: str


class TradingAccountConnectionTestRead(BaseModel):
    success: bool
    connection_status: str
    mt5_session_status: str
    current_mt5_login: str | None = None
    last_heartbeat_at: datetime | None
    last_error: str | None
    account_info: dict[str, object] | None = None
    error: dict[str, object] | None = None


class TradingAccountSymbolInfoRead(BaseModel):
    symbol: str
    point: float | None = None
    digits: int | None = None
    trade_contract_size: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None


class TradingAccountQuoteRead(BaseModel):
    symbol: str
    bid: float
    ask: float
    connection_status: str
    mt5_session_status: str
    current_mt5_login: str | None = None
    last_heartbeat_at: datetime | None
    last_error: str | None
    symbol_info: TradingAccountSymbolInfoRead | None = None
