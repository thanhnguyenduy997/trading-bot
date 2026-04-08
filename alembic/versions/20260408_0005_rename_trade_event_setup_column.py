"""Rename trade event setup column

Revision ID: 20260408_0005
Revises: 20260408_0004
Create Date: 2026-04-08 16:00:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "20260408_0005"
down_revision: Union[str, None] = "20260408_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("trade_events", "trade_setup_id", new_column_name="setup_id")
    op.drop_index("ix_trade_events_trade_setup_id", table_name="trade_events")
    op.create_index("ix_trade_events_setup_id", "trade_events", ["setup_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trade_events_setup_id", table_name="trade_events")
    op.create_index("ix_trade_events_trade_setup_id", "trade_events", ["trade_setup_id"], unique=False)
    op.alter_column("trade_events", "setup_id", new_column_name="trade_setup_id")
