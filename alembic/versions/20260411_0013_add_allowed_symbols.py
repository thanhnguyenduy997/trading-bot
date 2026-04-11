"""Add allowed symbols policy table

Revision ID: 20260411_0013
Revises: 20260411_0012
Create Date: 2026-04-11 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260411_0013"
down_revision: Union[str, None] = "20260411_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "allowed_symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol_name", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_allowed_symbols_id"), "allowed_symbols", ["id"], unique=False)
    op.create_index(op.f("ix_allowed_symbols_symbol_name"), "allowed_symbols", ["symbol_name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_allowed_symbols_symbol_name"), table_name="allowed_symbols")
    op.drop_index(op.f("ix_allowed_symbols_id"), table_name="allowed_symbols")
    op.drop_table("allowed_symbols")
