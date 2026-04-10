"""Add account-level Telegram settings

Revision ID: 20260410_0010
Revises: 20260410_0009
Create Date: 2026-04-10 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260410_0010"
down_revision: Union[str, None] = "20260410_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trading_accounts",
        sa.Column("telegram_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("trading_accounts", sa.Column("telegram_chat_id", sa.String(length=120), nullable=True))
    op.add_column(
        "trading_accounts",
        sa.Column("telegram_bot_token_encrypted", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trading_accounts", "telegram_bot_token_encrypted")
    op.drop_column("trading_accounts", "telegram_chat_id")
    op.drop_column("trading_accounts", "telegram_enabled")
