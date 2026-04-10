from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TradingAccountBase(BaseModel):
    broker_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=120)
    platform: str = Field(default="mt5", min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)
    telegram_enabled: bool = True
    telegram_chat_id: str | None = Field(default=None, max_length=120)


class TradingAccountCreate(TradingAccountBase):
    password: str = Field(min_length=1, max_length=255)
    telegram_bot_token: str | None = Field(default=None, min_length=1, max_length=255)


class TradingAccountUpdate(BaseModel):
    broker_name: str | None = Field(default=None, min_length=2, max_length=120)
    account_number: str | None = Field(default=None, min_length=2, max_length=120)
    server_name: str | None = Field(default=None, min_length=2, max_length=120)
    platform: str | None = Field(default=None, min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    telegram_bot_token: str | None = Field(default=None, min_length=1, max_length=255)


class TradingAccountRead(TradingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    connection_status: str
    last_heartbeat_at: datetime | None
    last_error: str | None
    telegram_enabled: bool
    telegram_chat_id: str | None
    created_at: datetime
    updated_at: datetime


class TradingAccountTelegramTestRead(BaseModel):
    success: bool
    message: str


class TradingAccountConnectionTestRead(BaseModel):
    success: bool
    connection_status: str
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
    last_heartbeat_at: datetime | None
    last_error: str | None
    symbol_info: TradingAccountSymbolInfoRead | None = None
