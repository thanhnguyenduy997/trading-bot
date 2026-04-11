"""Add risk management level 2

Revision ID: 20260411_0012
Revises: 20260410_0011
Create Date: 2026-04-11 09:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260411_0012"
down_revision: Union[str, None] = "20260410_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trading_accounts", sa.Column("max_total_setup_volume", sa.Numeric(18, 6), nullable=True))
    op.add_column("trade_setups", sa.Column("result_status", sa.String(length=30), nullable=True))
    op.add_column("trade_setups", sa.Column("result_recorded_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "user_daily_risk_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("consecutive_stoploss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("daily_lock_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_setup_result", sa.String(length=30), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "trading_day", name="uq_user_daily_risk_state"),
    )
    op.create_index(op.f("ix_user_daily_risk_states_id"), "user_daily_risk_states", ["id"], unique=False)
    op.create_index(op.f("ix_user_daily_risk_states_trading_day"), "user_daily_risk_states", ["trading_day"], unique=False)
    op.create_index(op.f("ix_user_daily_risk_states_user_id"), "user_daily_risk_states", ["user_id"], unique=False)

    op.create_table(
        "risk_control_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("setup_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["setup_id"], ["trade_setups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_risk_control_logs_id"), "risk_control_logs", ["id"], unique=False)
    op.create_index(op.f("ix_risk_control_logs_user_id"), "risk_control_logs", ["user_id"], unique=False)
    op.create_index(op.f("ix_risk_control_logs_setup_id"), "risk_control_logs", ["setup_id"], unique=False)
    op.create_index(op.f("ix_risk_control_logs_event_type"), "risk_control_logs", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_risk_control_logs_event_type"), table_name="risk_control_logs")
    op.drop_index(op.f("ix_risk_control_logs_setup_id"), table_name="risk_control_logs")
    op.drop_index(op.f("ix_risk_control_logs_user_id"), table_name="risk_control_logs")
    op.drop_index(op.f("ix_risk_control_logs_id"), table_name="risk_control_logs")
    op.drop_table("risk_control_logs")

    op.drop_index(op.f("ix_user_daily_risk_states_user_id"), table_name="user_daily_risk_states")
    op.drop_index(op.f("ix_user_daily_risk_states_trading_day"), table_name="user_daily_risk_states")
    op.drop_index(op.f("ix_user_daily_risk_states_id"), table_name="user_daily_risk_states")
    op.drop_table("user_daily_risk_states")

    op.drop_column("trade_setups", "result_recorded_at")
    op.drop_column("trade_setups", "result_status")
    op.drop_column("trading_accounts", "max_total_setup_volume")
