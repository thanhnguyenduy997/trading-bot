from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    role: str = Field(default="user", pattern="^(admin|user|operator)$")
    is_active: bool = True


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    role: str | None = Field(default=None, pattern="^(admin|user|operator)$")
    is_active: bool | None = None


class UserPasswordUpdate(BaseModel):
    password: str = Field(min_length=8)
