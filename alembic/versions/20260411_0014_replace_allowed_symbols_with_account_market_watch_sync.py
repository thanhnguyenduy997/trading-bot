"""Replace allowed symbols with per-account market watch sync

Revision ID: 20260411_0014
Revises: 20260411_0013
Create Date: 2026-04-11 16:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260411_0014"
down_revision: Union[str, None] = "20260411_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_allowed_symbols_symbol_name"), table_name="allowed_symbols")
    op.drop_index(op.f("ix_allowed_symbols_id"), table_name="allowed_symbols")
    op.drop_table("allowed_symbols")

    op.add_column("trading_accounts", sa.Column("symbols_last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "trading_accounts",
        sa.Column("symbols_sync_status", sa.String(length=30), nullable=False, server_default="never"),
    )
    op.add_column("trading_accounts", sa.Column("symbols_sync_error", sa.String(length=512), nullable=True))

    op.create_table(
        "trading_account_symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trading_account_id", sa.Integer(), nullable=False),
        sa.Column("symbol_name", sa.String(length=64), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sync_status", sa.String(length=30), nullable=False, server_default="synced"),
        sa.Column("sync_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["trading_account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_account_id", "symbol_name", name="uq_account_symbol_name"),
    )
    op.create_index(op.f("ix_trading_account_symbols_id"), "trading_account_symbols", ["id"], unique=False)
    op.create_index(
        op.f("ix_trading_account_symbols_trading_account_id"),
        "trading_account_symbols",
        ["trading_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_trading_account_symbols_trading_account_id"), table_name="trading_account_symbols")
    op.drop_index(op.f("ix_trading_account_symbols_id"), table_name="trading_account_symbols")
    op.drop_table("trading_account_symbols")

    op.drop_column("trading_accounts", "symbols_sync_error")
    op.drop_column("trading_accounts", "symbols_sync_status")
    op.drop_column("trading_accounts", "symbols_last_synced_at")

    op.create_table(
        "allowed_symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol_name", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_allowed_symbols_id"), "allowed_symbols", ["id"], unique=False)
    op.create_index(op.f("ix_allowed_symbols_symbol_name"), "allowed_symbols", ["symbol_name"], unique=True)
