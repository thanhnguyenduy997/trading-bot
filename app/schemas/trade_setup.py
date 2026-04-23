from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TradeSetupCreate(BaseModel):
    trading_account_id: int = Field(gt=0)
    setup_source: Literal["system", "manual"] = "system"
    order_count: int = Field(default=2, ge=1, le=2)
    symbol: str = Field(min_length=3, max_length=20)
    side: Literal["buy", "sell"]
    sl_price: float = Field(gt=0)
    risk_mode: Literal["fixed_money", "balance_percent"]
    risk_value: float = Field(gt=0)
    rr_order2: float = Field(gt=0)
    estimated_entry: float = Field(gt=0)
    r_value: float = Field(gt=0)
    tp1_price: float = Field(gt=0)
    tp2_price: float = Field(gt=0)
    total_risk_money: float = Field(gt=0)
    risk_per_order: float = Field(gt=0)
    order1_volume: float = Field(gt=0)
    order2_volume: float = Field(gt=0)
    status: Literal["draft", "queued", "executing", "executed", "failed"] = "draft"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class TradeSetupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    trading_account_id: int
    setup_source: Literal["system", "manual"]
    order_count: int
    symbol: str
    side: Literal["buy", "sell"]
    sl_price: float
    risk_mode: Literal["fixed_money", "balance_percent"]
    risk_value: float
    rr_order2: float
    estimated_entry: float
    r_value: float
    tp1_price: float
    tp2_price: float
    total_risk_money: float
    risk_per_order: float
    order1_volume: float
    order2_volume: float
    order1_ticket: int | None
    order2_ticket: int | None
    status: Literal["draft", "queued", "executing", "executed", "failed"]
    execution_error: str | None
    execution_details: str | None
    monitoring_status: str | None
    order2_be_moved_at: datetime | None
    order2_be_move_error: str | None
    result_status: str | None
    result_recorded_at: datetime | None
    manual_confirmed_at: datetime | None
    order1_outcome: str | None
    order2_outcome: str | None
    order1_closed_at: datetime | None
    order2_closed_at: datetime | None
    order1_close_price: float | None
    order2_close_price: float | None
    order1_realized_pnl: float | None
    order2_realized_pnl: float | None
    setup_outcome: str | None
    setup_outcome_recorded_at: datetime | None
    executed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TradeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    setup_id: int
    event_type: str
    message: str | None
    details: str | None
    created_at: datetime


class TradeSetupExecutionRead(BaseModel):
    success: bool
    status: Literal["queued", "executing", "executed", "failed"]
    order1_ticket: int | None
    order2_ticket: int | None
    execution_error: str | None
    execution_details: str | None
    executed_at: datetime | None


class TradeSetupMonitoringRead(BaseModel):
    success: bool
    monitoring_status: str
    order1_status: str
    order2_status: str
    be_moved: bool
    order2_be_moved_at: datetime | None
    order2_be_move_error: str | None


class TradeSetupReconciliationRead(BaseModel):
    success: bool
    setup_outcome: str
    order1_outcome: str
    order2_outcome: str
    order1_closed_at: datetime | None
    order2_closed_at: datetime | None
    order1_close_price: float | None
    order2_close_price: float | None
    order1_realized_pnl: float | None
    order2_realized_pnl: float | None
    setup_outcome_recorded_at: datetime | None


class ManualTradeSetupCreate(BaseModel):
    trading_account_id: int = Field(gt=0)
    symbol: str = Field(min_length=3, max_length=20)
    side: Literal["buy", "sell"]
    estimated_entry: float = Field(gt=0)
    sl_price: float = Field(gt=0)
    total_risk_money: float = Field(gt=0)
    rr_order2: float = Field(gt=0)
    tp1_price: float | None = Field(default=None, gt=0)
    tp2_price: float | None = Field(default=None, gt=0)
    order_count: int = Field(default=2, ge=1, le=2)
    order1_ticket: int = Field(gt=0)
    order2_ticket: int | None = Field(default=None, gt=0)

    @field_validator("symbol")
    @classmethod
    def normalize_manual_symbol(cls, value: str) -> str:
        return value.upper()
