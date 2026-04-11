from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AllowedSymbolCreate(BaseModel):
    symbol_name: str = Field(min_length=2, max_length=50)
    display_name: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True

    @field_validator("symbol_name")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class AllowedSymbolUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class AllowedSymbolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol_name: str
    display_name: str | None
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
