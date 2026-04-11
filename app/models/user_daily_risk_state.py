from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserDailyRiskState(Base):
    __tablename__ = "user_daily_risk_states"
    __table_args__ = (UniqueConstraint("user_id", "trading_day", name="uq_user_daily_risk_state"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    consecutive_stoploss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    daily_lock_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_setup_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
