"""Add trade setup execution tickets

Revision ID: 20260409_0006
Revises: 20260408_0005
Create Date: 2026-04-09 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260409_0006"
down_revision: Union[str, None] = "20260408_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_setups", sa.Column("order1_ticket", sa.BigInteger(), nullable=True))
    op.add_column("trade_setups", sa.Column("order2_ticket", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_setups", "order2_ticket")
    op.drop_column("trade_setups", "order1_ticket")
