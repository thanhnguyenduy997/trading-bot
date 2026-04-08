"""Add MT5 account fields

Revision ID: 20260408_0003
Revises: 20260408_0002
Create Date: 2026-04-08 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260408_0003"
down_revision: Union[str, None] = "20260408_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trading_accounts", sa.Column("platform", sa.String(length=20), nullable=False, server_default="mt5"))
    op.add_column("trading_accounts", sa.Column("terminal_path", sa.String(length=512), nullable=True))
    op.add_column(
        "trading_accounts",
        sa.Column("connection_status", sa.String(length=30), nullable=False, server_default="unknown"),
    )
    op.add_column("trading_accounts", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("last_error", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("trading_accounts", "last_error")
    op.drop_column("trading_accounts", "last_heartbeat_at")
    op.drop_column("trading_accounts", "connection_status")
    op.drop_column("trading_accounts", "terminal_path")
    op.drop_column("trading_accounts", "platform")
