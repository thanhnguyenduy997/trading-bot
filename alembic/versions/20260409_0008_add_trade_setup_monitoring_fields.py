"""Add trade setup monitoring fields

Revision ID: 20260409_0008
Revises: 20260409_0007
Create Date: 2026-04-09 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260409_0008"
down_revision: Union[str, None] = "20260409_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_setups", sa.Column("monitoring_status", sa.String(length=30), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_be_moved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_be_move_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_setups", "order2_be_move_error")
    op.drop_column("trade_setups", "order2_be_moved_at")
    op.drop_column("trade_setups", "monitoring_status")
