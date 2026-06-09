"""add trading account default setup mode

Revision ID: 20260609_0023
Revises: 20260608_0022
Create Date: 2026-06-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260609_0023"
down_revision = "20260608_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trading_accounts",
        sa.Column(
            "default_setup_mode",
            sa.String(length=30),
            nullable=False,
            server_default="split_two_orders",
        ),
    )


def downgrade() -> None:
    op.drop_column("trading_accounts", "default_setup_mode")
