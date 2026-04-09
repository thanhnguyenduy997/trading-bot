"""Expand execution diagnostics storage

Revision ID: 20260409_0007
Revises: 20260409_0006
Create Date: 2026-04-09 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260409_0007"
down_revision: Union[str, None] = "20260409_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("trade_events", "message", existing_type=sa.String(length=512), type_=sa.Text(), existing_nullable=True)
    op.add_column("trade_events", sa.Column("details", sa.Text(), nullable=True))
    op.add_column("trade_setups", sa.Column("execution_details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_setups", "execution_details")
    op.drop_column("trade_events", "details")
    op.alter_column("trade_events", "message", existing_type=sa.Text(), type_=sa.String(length=512), existing_nullable=True)
