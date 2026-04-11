from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TradingAccountSymbol(Base):
    __tablename__ = "trading_account_symbols"
    __table_args__ = (
        UniqueConstraint("trading_account_id", "symbol_name", name="uq_account_symbol_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    trading_account_id: Mapped[int] = mapped_column(
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    sync_status: Mapped[str] = mapped_column(String(30), nullable=False, default="synced", server_default="synced")
    sync_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    trading_account = relationship("TradingAccount", back_populates="symbols")
