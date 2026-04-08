from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TradingAccountBase(BaseModel):
    broker_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=120)
    platform: str = Field(default="mt5", min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)


class TradingAccountCreate(TradingAccountBase):
    password: str = Field(min_length=1, max_length=255)


class TradingAccountUpdate(BaseModel):
    broker_name: str | None = Field(default=None, min_length=2, max_length=120)
    account_number: str | None = Field(default=None, min_length=2, max_length=120)
    server_name: str | None = Field(default=None, min_length=2, max_length=120)
    platform: str | None = Field(default=None, min_length=2, max_length=20)
    terminal_path: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, min_length=1, max_length=255)


class TradingAccountRead(TradingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    connection_status: str
    last_heartbeat_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class TradingAccountConnectionTestRead(BaseModel):
    success: bool
    connection_status: str
    last_heartbeat_at: datetime | None
    last_error: str | None
    account_info: dict[str, str | int | float | None] | None = None
    error: dict[str, str | dict[str, str] | None] | None = None


class TradingAccountQuoteRead(BaseModel):
    symbol: str
    bid: float
    ask: float
    connection_status: str
    last_heartbeat_at: datetime | None
    last_error: str | None
