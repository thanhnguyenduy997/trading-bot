"""Add trade setups

Revision ID: 20260408_0002
Revises: 20260407_0001
Create Date: 2026-04-08 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260408_0002"
down_revision: Union[str, None] = "20260407_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_setups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trading_account_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("sl_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("risk_mode", sa.String(length=30), nullable=False),
        sa.Column("risk_value", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("rr_order2", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("estimated_entry", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("r_value", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("tp1_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("tp2_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("total_risk_money", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("risk_per_order", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("order1_volume", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("order2_volume", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["trading_account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_trade_setups_id", "trade_setups", ["id"], unique=False)
    op.create_index("ix_trade_setups_trading_account_id", "trade_setups", ["trading_account_id"], unique=False)
    op.create_index("ix_trade_setups_user_id", "trade_setups", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trade_setups_user_id", table_name="trade_setups")
    op.drop_index("ix_trade_setups_trading_account_id", table_name="trade_setups")
    op.drop_index("ix_trade_setups_id", table_name="trade_setups")
    op.drop_table("trade_setups")
