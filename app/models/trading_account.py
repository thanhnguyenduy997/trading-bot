from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TradingAccount(Base):
    __tablename__ = "trading_accounts"
    __table_args__ = (UniqueConstraint("user_id", "account_number", name="uq_user_account_number"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    broker_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_number: Mapped[str] = mapped_column(String(120), nullable=False)
    server_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="mt5", server_default="mt5")
    terminal_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    connection_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown", server_default="unknown")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    symbols_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    symbols_sync_status: Mapped[str] = mapped_column(String(30), nullable=False, default="never", server_default="never")
    symbols_sync_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    telegram_enabled: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    telegram_bot_token_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    max_total_setup_volume: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner = relationship("User", back_populates="trading_accounts")
    trade_setups = relationship("TradeSetup", back_populates="trading_account", cascade="all, delete-orphan")
    symbols = relationship("TradingAccountSymbol", back_populates="trading_account", cascade="all, delete-orphan")
