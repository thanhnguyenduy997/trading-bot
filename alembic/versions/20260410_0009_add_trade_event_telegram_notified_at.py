"""Add Telegram notification tracking to trade events

Revision ID: 20260410_0009
Revises: 20260409_0008
Create Date: 2026-04-10 09:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260410_0009"
down_revision: Union[str, None] = "20260409_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_events", sa.Column("telegram_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_trade_events_telegram_notified_at", "trade_events", ["telegram_notified_at"])


def downgrade() -> None:
    op.drop_index("ix_trade_events_telegram_notified_at", table_name="trade_events")
    op.drop_column("trade_events", "telegram_notified_at")
