from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TradeSetup(Base):
    __tablename__ = "trade_setups"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trading_account_id: Mapped[int] = mapped_column(
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    setup_source: Mapped[str] = mapped_column(String(20), nullable=False, default="system", server_default="system")
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    sl_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    risk_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_value: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    rr_order2: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    estimated_entry: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    r_value: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    tp1_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    tp2_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    total_risk_money: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    risk_per_order: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    order1_volume: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    order2_volume: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    order1_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    order2_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    execution_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    execution_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    monitoring_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    order2_be_moved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order2_be_move_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order1_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    order2_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    order1_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order2_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order1_close_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    order2_close_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    order1_realized_pnl: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    order2_realized_pnl: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    setup_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    setup_outcome_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner = relationship("User", back_populates="trade_setups")
    trading_account = relationship("TradingAccount", back_populates="trade_setups")
    events = relationship("TradeEvent", back_populates="setup", cascade="all, delete-orphan")
