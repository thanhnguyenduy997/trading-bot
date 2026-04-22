from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MT5TradeHistory(Base):
    __tablename__ = "mt5_trade_history"
    __table_args__ = (
        UniqueConstraint("trading_account_id", "position_ticket", name="uq_mt5_trade_history_account_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trading_account_id: Mapped[int] = mapped_column(
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    linked_setup_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_setups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    position_ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_deal_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    close_deal_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    trade_source: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown")
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    volume: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    open_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
