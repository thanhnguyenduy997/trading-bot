"""add scratch manual threshold setting

Revision ID: 20260425_0021
Revises: 20260423_0020
Create Date: 2026-04-25 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260425_0021"
down_revision = "20260423_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("scratch_manual_threshold_r", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "scratch_manual_threshold_r")
