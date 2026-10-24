"""Initial durable campaign schema; immutable migration snapshot."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaigns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("target_version", sa.String(length=128), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("current_version", sa.String(length=128), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("failure_threshold", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("tuning", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("gate_epoch", sa.Integer(), nullable=False),
        sa.Column("gate_minimum", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING','PAUSED','HALTED','COMPLETED')"),
        sa.CheckConstraint("failure_threshold >= 0 AND failure_threshold <= 100"),
        sa.CheckConstraint("max_concurrent > 0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "one_active_campaign",
        "campaigns",
        [sa.literal_column("(true)")],
        unique=True,
        postgresql_where=sa.text("status IN ('RUNNING','PAUSED','HALTED')"),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(length=200), nullable=False),
        sa.Column("firmware", sa.String(length=128), nullable=False),
        sa.Column("last_event", sa.String(length=32), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_table(
        "campaign_devices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.String(length=200), nullable=False),
        sa.Column("initial_version", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("command_key", sa.UUID(), nullable=True),
        sa.Column("ack_status", sa.String(length=20), nullable=True),
        sa.Column("nudge_attempts", sa.Integer(), nullable=False),
        sa.Column("next_nudge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inform_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("boot_version", sa.String(length=128), nullable=True),
        sa.Column("boot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_fault", sa.Integer(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("evidence", sa.String(length=64), nullable=True),
        sa.Column("outcome_epoch", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING','NOTIFIED','DOWNLOAD_ISSUED','AWAITING_RESULT','OUTCOME_UNKNOWN','SUCCEEDED','FAILED','TIMED_OUT')"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "device_id"),
        sa.UniqueConstraint("command_key"),
    )
    op.create_index("campaign_state", "campaign_devices", ["campaign_id", "state"], unique=False)
    op.create_index("due_nudges", "campaign_devices", ["state", "next_nudge_at"], unique=False)
    op.create_index("due_results", "campaign_devices", ["state", "result_deadline"], unique=False)
    op.create_table(
        "device_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("device_id", sa.String(length=200), nullable=True),
        sa.Column("command_key", sa.String(length=200), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("new_state", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "event_history", "device_events", ["campaign_id", "device_id", "id"], unique=False
    )


def downgrade():
    op.drop_index("event_history", table_name="device_events")
    op.drop_table("device_events")
    op.drop_index("due_results", table_name="campaign_devices")
    op.drop_index("due_nudges", table_name="campaign_devices")
    op.drop_index("campaign_state", table_name="campaign_devices")
    op.drop_table("campaign_devices")
    op.drop_table("devices")
    op.drop_index(
        "one_active_campaign",
        table_name="campaigns",
        postgresql_where=sa.text("status IN ('RUNNING','PAUSED','HALTED')"),
    )
    op.drop_table("campaigns")
