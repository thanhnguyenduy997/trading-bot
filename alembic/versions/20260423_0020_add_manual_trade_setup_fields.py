"""add manual trade setup fields

Revision ID: 20260423_0020
Revises: 20260422_0019
Create Date: 2026-04-23 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260423_0020"
down_revision = "20260422_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_setups",
        sa.Column("setup_source", sa.String(length=20), nullable=False, server_default="system"),
    )
    op.add_column(
        "trade_setups",
        sa.Column("order_count", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "trade_setups",
        sa.Column("manual_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_setups", "manual_confirmed_at")
    op.drop_column("trade_setups", "order_count")
    op.drop_column("trade_setups", "setup_source")
