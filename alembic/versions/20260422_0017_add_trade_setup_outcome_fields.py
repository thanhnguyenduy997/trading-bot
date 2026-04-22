"""add trade setup outcome fields

Revision ID: 20260422_0017
Revises: 20260420_0016
Create Date: 2026-04-22 09:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260422_0017"
down_revision = "20260420_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trade_setups", sa.Column("order1_outcome", sa.String(length=30), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_outcome", sa.String(length=30), nullable=True))
    op.add_column("trade_setups", sa.Column("order1_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trade_setups", sa.Column("order1_close_price", sa.Numeric(18, 6), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_close_price", sa.Numeric(18, 6), nullable=True))
    op.add_column("trade_setups", sa.Column("order1_realized_pnl", sa.Numeric(18, 6), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_realized_pnl", sa.Numeric(18, 6), nullable=True))
    op.add_column("trade_setups", sa.Column("setup_outcome", sa.String(length=40), nullable=True))
    op.add_column("trade_setups", sa.Column("setup_outcome_recorded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_setups", "setup_outcome_recorded_at")
    op.drop_column("trade_setups", "setup_outcome")
    op.drop_column("trade_setups", "order2_realized_pnl")
    op.drop_column("trade_setups", "order1_realized_pnl")
    op.drop_column("trade_setups", "order2_close_price")
    op.drop_column("trade_setups", "order1_close_price")
    op.drop_column("trade_setups", "order2_closed_at")
    op.drop_column("trade_setups", "order1_closed_at")
    op.drop_column("trade_setups", "order2_outcome")
    op.drop_column("trade_setups", "order1_outcome")
