"""create automation tables

Revision ID: 20260405_0001
Revises:
Create Date: 2026-04-05 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260405_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "controller_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("is_on", sa.Boolean(), nullable=False),
        sa.Column("changed_at_iso", sa.String(length=40), nullable=False),
        sa.Column("last_known_plug_is_on", sa.Boolean(), nullable=True),
        sa.Column("last_known_plug_at_iso", sa.String(length=40), nullable=True),
        sa.Column("last_actuation_error", sa.Text(), nullable=True),
        sa.Column("last_actuation_at_iso", sa.String(length=40), nullable=True),
        sa.Column("updated_at_iso", sa.String(length=40), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_controller_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "control_cycle",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp_unix_ms", sa.BigInteger(), nullable=False),
        sa.Column("timestamp_iso", sa.String(length=40), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("site_name", sa.String(length=255), nullable=False),
        sa.Column("site_identifier", sa.String(length=255), nullable=False),
        sa.Column("power_queried_at_unix_ms", sa.BigInteger(), nullable=True),
        sa.Column("power_queried_at_iso", sa.String(length=40), nullable=True),
        sa.Column("battery_soc_percent", sa.Float(), nullable=True),
        sa.Column("solar_watts", sa.Float(), nullable=True),
        sa.Column("house_watts", sa.Float(), nullable=True),
        sa.Column("house_l1_watts", sa.Float(), nullable=True),
        sa.Column("house_l2_watts", sa.Float(), nullable=True),
        sa.Column("house_l3_watts", sa.Float(), nullable=True),
        sa.Column("generator_watts", sa.Float(), nullable=True),
        sa.Column("active_input_source", sa.Integer(), nullable=True),
        sa.Column("current_temperature_c", sa.Float(), nullable=True),
        sa.Column("today_min_temperature_c", sa.Float(), nullable=True),
        sa.Column("today_max_temperature_c", sa.Float(), nullable=True),
        sa.Column("weather_code", sa.Integer(), nullable=True),
        sa.Column("queried_timezone", sa.String(length=128), nullable=True),
        sa.Column("should_turn_on", sa.Boolean(), nullable=False),
        sa.Column("decision_action", sa.String(length=32), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("decision_weather_mode", sa.String(length=32), nullable=False),
        sa.Column("intended_target_is_on", sa.Boolean(), nullable=False),
        sa.Column("quiet_hours_blocked", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("actuation_status", sa.String(length=64), nullable=False),
        sa.Column("actuation_command_sent", sa.String(length=32), nullable=True),
        sa.Column("actuation_observed_before_is_on", sa.Boolean(), nullable=True),
        sa.Column("actuation_observed_after_is_on", sa.Boolean(), nullable=True),
        sa.Column("actuation_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_control_cycle_timestamp_unix_ms",
        "control_cycle",
        ["timestamp_unix_ms"],
        unique=False,
    )
    op.create_index(
        "ix_control_cycle_site_identifier_timestamp_unix_ms",
        "control_cycle",
        ["site_identifier", "timestamp_unix_ms"],
        unique=False,
    )
    op.create_index(
        "ix_control_cycle_command_sent_timestamp_unix_ms",
        "control_cycle",
        ["actuation_command_sent", "timestamp_unix_ms"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_control_cycle_command_sent_timestamp_unix_ms", table_name="control_cycle")
    op.drop_index("ix_control_cycle_site_identifier_timestamp_unix_ms", table_name="control_cycle")
    op.drop_index("ix_control_cycle_timestamp_unix_ms", table_name="control_cycle")
    op.drop_table("control_cycle")
    op.drop_table("controller_state")
