"""Add trade execution fields and events

Revision ID: 20260408_0004
Revises: 20260408_0003
Create Date: 2026-04-08 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260408_0004"
down_revision: Union[str, None] = "20260408_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_setups", sa.Column("execution_error", sa.String(length=512), nullable=True))
    op.add_column("trade_setups", sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "trade_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_setup_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["trade_setup_id"], ["trade_setups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_trade_events_id", "trade_events", ["id"], unique=False)
    op.create_index("ix_trade_events_trade_setup_id", "trade_events", ["trade_setup_id"], unique=False)
    op.create_index("ix_trade_events_user_id", "trade_events", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trade_events_user_id", table_name="trade_events")
    op.drop_index("ix_trade_events_trade_setup_id", table_name="trade_events")
    op.drop_index("ix_trade_events_id", table_name="trade_events")
    op.drop_table("trade_events")
    op.drop_column("trade_setups", "executed_at")
    op.drop_column("trade_setups", "execution_error")
