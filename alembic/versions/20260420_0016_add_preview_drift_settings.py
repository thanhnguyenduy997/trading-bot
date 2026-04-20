"""add preview drift settings

Revision ID: 20260420_0016
Revises: 20260415_0015
Create Date: 2026-04-20 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260420_0016"
down_revision = "20260415_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("max_preview_drift_percent", sa.Numeric(10, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "trading_accounts",
        sa.Column("max_preview_drift_percent_override", sa.Numeric(10, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trading_accounts", "max_preview_drift_percent_override")
    op.drop_table("app_settings")
