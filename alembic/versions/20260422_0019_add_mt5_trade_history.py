"""add mt5 trade history

Revision ID: 20260422_0019
Revises: 20260422_0018
Create Date: 2026-04-22 13:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260422_0019"
down_revision = "20260422_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mt5_trade_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trading_account_id", sa.Integer(), nullable=False),
        sa.Column("linked_setup_id", sa.Integer(), nullable=True),
        sa.Column("position_ticket", sa.BigInteger(), nullable=False),
        sa.Column("open_deal_ticket", sa.BigInteger(), nullable=True),
        sa.Column("close_deal_ticket", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("trade_source", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=True),
        sa.Column("volume", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("open_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("close_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["linked_setup_id"], ["trade_setups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trading_account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_account_id", "position_ticket", name="uq_mt5_trade_history_account_position"),
    )
    op.create_index(op.f("ix_mt5_trade_history_id"), "mt5_trade_history", ["id"], unique=False)
    op.create_index(op.f("ix_mt5_trade_history_user_id"), "mt5_trade_history", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_mt5_trade_history_trading_account_id"),
        "mt5_trade_history",
        ["trading_account_id"],
        unique=False,
    )
    op.create_index(op.f("ix_mt5_trade_history_linked_setup_id"), "mt5_trade_history", ["linked_setup_id"], unique=False)
    op.create_index(op.f("ix_mt5_trade_history_symbol"), "mt5_trade_history", ["symbol"], unique=False)
    op.create_index(op.f("ix_mt5_trade_history_open_time"), "mt5_trade_history", ["open_time"], unique=False)
    op.create_index(op.f("ix_mt5_trade_history_close_time"), "mt5_trade_history", ["close_time"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_mt5_trade_history_close_time"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_open_time"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_symbol"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_linked_setup_id"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_trading_account_id"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_user_id"), table_name="mt5_trade_history")
    op.drop_index(op.f("ix_mt5_trade_history_id"), table_name="mt5_trade_history")
    op.drop_table("mt5_trade_history")
