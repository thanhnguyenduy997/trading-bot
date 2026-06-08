"""add trade setup mode

Revision ID: 20260608_0022
Revises: 20260425_0021
Create Date: 2026-06-08 00:22:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260608_0022"
down_revision = "20260425_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_setups",
        sa.Column("setup_mode", sa.String(length=30), nullable=False, server_default="split_two_orders"),
    )


def downgrade() -> None:
    op.drop_column("trade_setups", "setup_mode")
