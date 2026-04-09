from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TradePreviewRequest(BaseModel):
    trading_account_id: int = Field(gt=0)
    symbol: str = Field(min_length=3, max_length=20)
    side: Literal["buy", "sell"]
    sl_price: float = Field(gt=0)
    risk_mode: Literal["fixed_money", "balance_percent"]
    risk_value: float = Field(gt=0)
    rr_order2: float = Field(gt=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class TradePreviewResponse(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    bid: float
    ask: float
    estimated_entry: float
    sl_price: float
    r_value: float
    tp1_price: float
    tp2_price: float
    total_risk_money: float
    risk_per_order: float
    order1_volume: float
    order2_volume: float
    point: float | None = None
    digits: int | None = None
    trade_contract_size: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    validation_status: Literal["valid"]
    warnings: list[str] = Field(default_factory=list)
