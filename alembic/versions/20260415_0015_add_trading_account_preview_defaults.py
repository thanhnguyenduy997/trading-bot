"""add trading account preview defaults

Revision ID: 20260415_0015
Revises: 20260411_0014
Create Date: 2026-04-15 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260415_0015"
down_revision = "20260411_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trading_accounts", sa.Column("default_symbol", sa.String(length=20), nullable=True))
    op.add_column("trading_accounts", sa.Column("default_side", sa.String(length=10), nullable=True))
    op.add_column("trading_accounts", sa.Column("default_risk_mode", sa.String(length=30), nullable=True))
    op.add_column("trading_accounts", sa.Column("default_risk_value", sa.Numeric(18, 6), nullable=True))
    op.add_column("trading_accounts", sa.Column("default_rr_order_2", sa.Numeric(18, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("trading_accounts", "default_rr_order_2")
    op.drop_column("trading_accounts", "default_risk_value")
    op.drop_column("trading_accounts", "default_risk_mode")
    op.drop_column("trading_accounts", "default_side")
    op.drop_column("trading_accounts", "default_symbol")
