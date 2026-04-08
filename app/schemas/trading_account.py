from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TradingAccountBase(BaseModel):
    broker_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=120)


class TradingAccountCreate(TradingAccountBase):
    password: str = Field(min_length=1, max_length=255)


class TradingAccountUpdate(BaseModel):
    broker_name: str | None = Field(default=None, min_length=2, max_length=120)
    account_number: str | None = Field(default=None, min_length=2, max_length=120)
    server_name: str | None = Field(default=None, min_length=2, max_length=120)
    password: str | None = Field(default=None, min_length=1, max_length=255)


class TradingAccountRead(TradingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime
