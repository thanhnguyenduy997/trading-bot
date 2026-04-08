"""Initial schema

Revision ID: 20260407_0001
Revises:
Create Date: 2026-04-07 23:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260407_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_id", "users", ["id"], unique=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "trading_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("broker_name", sa.String(length=120), nullable=False),
        sa.Column("account_number", sa.String(length=120), nullable=False),
        sa.Column("server_name", sa.String(length=120), nullable=False),
        sa.Column("password_encrypted", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "account_number", name="uq_user_account_number"),
    )
    op.create_index("ix_trading_accounts_id", "trading_accounts", ["id"], unique=False)
    op.create_index("ix_trading_accounts_user_id", "trading_accounts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trading_accounts_user_id", table_name="trading_accounts")
    op.drop_index("ix_trading_accounts_id", table_name="trading_accounts")
    op.drop_table("trading_accounts")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_table("users")
