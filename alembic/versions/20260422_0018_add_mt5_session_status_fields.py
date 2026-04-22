"""add mt5 session status fields

Revision ID: 20260422_0018
Revises: 20260422_0017
Create Date: 2026-04-22 11:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260422_0018"
down_revision = "20260422_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trading_accounts",
        sa.Column("mt5_session_status", sa.String(length=20), server_default="unknown", nullable=False),
    )
    op.add_column("trading_accounts", sa.Column("current_mt5_login", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("trading_accounts", "current_mt5_login")
    op.drop_column("trading_accounts", "mt5_session_status")
